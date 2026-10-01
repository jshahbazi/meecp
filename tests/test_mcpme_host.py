"""Real two-slot host acceptance using owned native stdio packages and SDK clients."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcpme.supervisor import HostError, Supervisor, bind_listener, management_app

ROOT = Path(__file__).resolve().parents[1]


def test_reserved_listener_rejects_another_owner():
    listener = bind_listener()
    try:
        with pytest.raises(HostError, match="port_conflict"):
            bind_listener(listener.getsockname()[1])
    finally:
        listener.close()


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-package").resolve()
    reaper = directory / "mcpme-reap"
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "scripts/mcpme-reap.c"), "-o", str(reaper)], check=True, capture_output=True)
    spec = importlib.util.spec_from_file_location("prepare_json_package", ROOT / "scripts/prepare_json_package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = module.prepare(directory / "package", idle_seconds=2)
    return package, reaper


async def finish(host, operation):
    async with asyncio.timeout(20):
        while True:
            value = host.registry.get_operation(operation["operation_id"])
            if value.status not in {"pending", "running"}:
                return value
            await asyncio.sleep(0.02)


async def install(host, package, name, start=True):
    plan = host.plan_install({"package": str(package), "name": name, "start": start})
    applied = await host.apply_install(plan["plan_id"])
    operation = await finish(host, applied)
    assert operation.status == "completed", operation.error_code
    return host.registry.get_slot(operation.slot_id)


async def tool_call(host, slot_id, document='{"synthetic":true}'):
    connection = host.connection(slot_id)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                listed = await client.list_tools()
                assert [tool.name for tool in listed.tools] == ["validate_json"]
                result = await client.call_tool("validate_json", {"document": document})
                return result.structuredContent


@pytest.mark.asyncio
async def test_two_slots_independent_controls_credentials_and_stdio(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        await host.initialize()
        first = await install(host, package, "First")
        second = await install(host, package, "Second")
        assert first.id != second.id and first.public_port != second.public_port
        results = await asyncio.gather(tool_call(host, first.id), tool_call(host, second.id, "[1,2]"))
        assert results == [{"valid": True, "kind": "object"}, {"valid": True, "kind": "array"}]
        wrong_token = host.connection(first.id)["token"]
        async with httpx.AsyncClient(trust_env=False) as http:
            wrong = await http.get(host.connection(second.id)["endpoint"], headers={"Authorization": "Bearer " + wrong_token})
            assert wrong.status_code == 401
        await host.control(first.id, "stop")
        assert host.registry.get_slot(first.id).wake_suppressed
        assert await tool_call(host, second.id, "false") == {"valid": True, "kind": "boolean"}
        async with httpx.AsyncClient(trust_env=False) as http:
            stopped = await http.post(host.connection(first.id)["endpoint"],
                headers={"Authorization": "Bearer " + wrong_token, "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            assert stopped.status_code == 409
            assert stopped.json()["error"]["code"] == "slot_stopped"
        await host.control(first.id, "start")
        assert host.registry.get_slot(first.id).public_port == first.public_port
        assert await tool_call(host, first.id, "invalid") == {"valid": False, "kind": "invalid"}
        params = StdioServerParameters(command=str(ROOT / "scripts/mcpme"), args=["--state-dir", str(host.root), "connect", "--slot", second.id])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool("validate_json", {"document": "null"})
                assert result.structuredContent == {"valid": True, "kind": "null"}
        for slot_id in (first.id, second.id):
            assert len(host.owner(slot_id).instances) == 1  # resident gateway only
    finally:
        await host.close()
    assert all(not ledger for ledger in host.ledgers.values())


@pytest.mark.asyncio
async def test_install_retry_snapshot_tamper_and_restart_persistence(tmp_path, prepared):
    package, reaper = prepared
    state = tmp_path.resolve() / "host"
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package), "name": "Manual"})
        first, retry = await asyncio.gather(host.apply_install(plan["plan_id"]), host.apply_install(plan["plan_id"]))
        assert first["operation_id"] == retry["operation_id"]
        operation = await finish(host, first)
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        assert slot.observed_state == "stopped" and slot.start_policy == "manual"
        assert len(host.registry.list_slots()) == 1
        bad_plan = host.plan_install({"package": str(package)})
        (Path(bad_plan["package_path"]) / "main.swift").write_text("modified after planning")
        failed = await finish(host, await host.apply_install(bad_plan["plan_id"]))
        assert failed.status == "failed"
        assert len(host.registry.list_slots()) == 1
        await host.control(slot.id, "start")
        await host.control(slot.id, "stop")
    finally:
        await host.close()
    reopened = Supervisor(state, reaper)
    try:
        await reopened.initialize()
        same = reopened.registry.get_slot(slot.id)
        assert same.public_port == slot.public_port and same.wake_suppressed
        assert same.observed_state == "stopped"
        await reopened.control(slot.id, "start")
        assert await tool_call(reopened, slot.id) == {"valid": True, "kind": "object"}
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_management_authority_and_safe_errors(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        slot = await install(host, package, "Managed", start=False)
        app = management_app(host, "manager-credential", 19499)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19499") as http:
            assert (await http.get("/v1/slots")).status_code == 401
            assert (await http.get("/v1/slots", headers={"Authorization": "Bearer " + host.connection(slot.id)["token"]})).status_code == 401
            headers = {"Authorization": "Bearer manager-credential"}
            listed = (await http.get("/v1/slots", headers=headers)).json()
            assert listed["slots"][0]["slot_id"] == slot.id
            assert "token" not in json.dumps(listed)
            malformed = await http.post("/v1/install/plan", headers=headers, json={"package": 123, "secret": "never-log-this"})
            assert malformed.status_code == 400
            assert "never-log-this" not in malformed.text
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_start_with_host_restarts_runtime_but_manual_stop_suppresses_it(tmp_path, prepared):
    package, reaper = prepared
    state = tmp_path.resolve() / "host"
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package), "start_policy": "host", "start": True})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed"
        slot_id = operation.slot_id
    finally:
        await host.close()
    restarted = Supervisor(state, reaper)
    try:
        await restarted.initialize()
        assert restarted.registry.get_slot(slot_id).observed_state == "ready"
        assert await tool_call(restarted, slot_id) == {"valid": True, "kind": "object"}
        await restarted.control(slot_id, "stop")
    finally:
        await restarted.close()
    stopped = Supervisor(state, reaper)
    try:
        await stopped.initialize()
        assert stopped.registry.get_slot(slot_id).observed_state == "stopped"
        assert stopped.registry.get_slot(slot_id).wake_suppressed
    finally:
        await stopped.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_blocked_install_before_closing_registry(tmp_path, prepared, monkeypatch):
    package, reaper = prepared
    state = tmp_path.resolve() / "host"
    host = Supervisor(state, reaper)
    entered = asyncio.Event()
    async def candidate(_):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(host, "check_candidate", candidate)
    plan = host.plan_install({"package": str(package), "start": True})
    operation = await host.apply_install(plan["plan_id"])
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(host.close(), 5)
    reopened = Supervisor(state, reaper)
    try:
        await reopened.initialize()
        record = reopened.registry.get_operation(operation["operation_id"])
        assert record.status == "recovery_required"
        assert not reopened.workers
    finally:
        await reopened.close()
