"""Connection admission is independent of deprecated manifest session counts."""
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
import shutil

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcpme.gateway import SessionPipes, make_app
from mcpme.supervisor import Supervisor
from test_mcpme_host import install, prepared
from test_mcpme_http_gateway import initialize, public_client, public_headers, public_proxy, stateless_runtime


async def test_default_stdio_gateway_admits_more_than_historical_schema_ceiling():
    # Real session ownership/readers, with idle pipes instead of 65 processes.
    # Separate duplicated descriptors let every session close independently.
    source_read, source_write = os.pipe()
    sink_read, sink_write = os.pipe()
    launched, closed = [], []
    async def launch(sid):
        launched.append(sid)
        return SessionPipes(sid, os.dup(source_read), os.dup(sink_write))
    async def close(sid):
        closed.append(sid)
    _, gateway = make_app(token="synthetic-token", port=19387,
        launch_session=launch, close_session=close)
    try:
        assert gateway.max_sessions is None
        for _ in range(65):
            await gateway.new_session()
        assert len(gateway.sessions) == len(gateway.reservations) == len(launched) == 65
    finally:
        await gateway.shutdown()
        for fd in (source_read, source_write, sink_read, sink_write):
            os.close(fd)
    assert not gateway.sessions and not gateway.reservations
    assert sorted(closed) == sorted(launched)


async def test_default_http_gateway_admits_more_than_historical_schema_ceiling():
    async with stateless_runtime() as (private, stats), public_proxy(private) as (public, gateway), public_client() as http:
        sessions = [await initialize(http, public) for _ in range(65)]
        assert gateway.max_sessions is None
        assert len(gateway.sessions) == len(gateway.reservations) == len(set(sessions)) == 65
        discovery = await http.post(public, headers=public_headers(sessions[0]), json={
            "jsonrpc": "2.0", "id": "discover", "method": "tools/list"})
        assert discovery.status_code == 200
        result = await http.post(public, headers=public_headers(sessions[-1]), json={
            "jsonrpc": "2.0", "id": "execute", "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": "synthetic"}}})
        assert result.status_code == 200 and stats["calls"] == 1
        for sid in sessions:
            assert (await http.delete(public, headers=public_headers(sid))).status_code == 200
        assert not gateway.sessions and not gateway.reservations


async def test_installed_legacy_one_session_package_serves_discovery_and_multiple_clients(tmp_path, prepared):
    original, reaper = prepared
    package = tmp_path / "legacy-one-client-package"
    shutil.copytree(original, package)
    manifest_path = package / "mcpme.package.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["lifecycle"].update(max_sessions=1, idle_timeout_seconds=30)
    manifest_path.write_text(json.dumps(manifest))
    lock_path = package / "mcpme.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock))
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        slot = await install(host, package, "Legacy single-client app")
        assert host.package(slot).manifest.lifecycle.max_sessions == 1
        connection = host.connection(slot.id)
        async with AsyncExitStack() as clients:
            sessions = []
            for _ in range(3):
                http = await clients.enter_async_context(httpx.AsyncClient(
                    headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False))
                read, write, _ = await clients.enter_async_context(
                    streamable_http_client(connection["endpoint"], http_client=http))
                session = await clients.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions.append(session)
            assert len(host.owner(slot.id).instances) == 4  # three runtimes and their resident gateway
            assert [tool.name for tool in (await sessions[0].list_tools()).tools] == ["validate_json"]
            results = await asyncio.gather(*(session.call_tool("validate_json", {"document": "null"})
                for session in sessions[1:]))
            assert all(not result.isError and result.structuredContent == {"valid": True, "kind": "null"}
                for result in results)
            assert [tool.name for tool in (await sessions[0].list_tools()).tools] == ["validate_json"]
        assert len(host.owner(slot.id).instances) == 1
    finally:
        await host.close()
    assert all(not ledger for ledger in host.ledgers.values())
