"""Opt-in actual private tools and confined service probe; no model/provider."""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
import shutil

import pytest

from mcpme.contracts import validate_package
from mcpme.preparation_runner import PreparationRunner
from mcpme.preparation_service import descriptor, validate_components
from mcpme.preparation_service_source import source
from test_service_preparation import service


async def installed_sdk(host, slot_id):
    import httpx
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    async def check(client):
        await client.initialize()
        assert [item.name for item in (await client.list_tools()).tools] == ["generate", "check_connection"]
        assert (await client.call_tool("check_connection", {})).structuredContent == {"compatible": True}
        generated = await client.call_tool("generate", {"prompt":"Synthetic installed input", "max_output_tokens":8})
        assert not generated.isError and generated.structuredContent == {"text":"synthetic-result","finish_reason":"stop"}
    connection = host.connection(slot_id)
    async with asyncio.timeout(30):
        async with httpx.AsyncClient(headers={"Authorization":"Bearer "+connection["token"]}, trust_env=False) as http:
            async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client: await check(client)
        params = StdioServerParameters(command=str(Path(__file__).resolve().parents[1] / "scripts/mcpme"),
            args=["--state-dir",str(host.root),"connect","--slot",slot_id])
        with open(os.devnull, "w") as discarded:
            async with stdio_client(params, errlog=discarded) as (read, write):
                async with ClientSession(read, write) as client: await check(client)


@pytest.mark.parametrize("mode", ["complete", "cancel-probe", "revoke-probe", "resolve-revoked"])
async def test_actual_private_service_preparation(service, monkeypatch, mode):
    selected = os.environ.get("MCPME_TEST_SERVICE_HOST")
    if not selected:
        pytest.skip("explicit composed private service host required")
    host = Path(selected)
    assert host.is_absolute() and host.resolve() == host
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    imports, secrets, draft, recipe, family, store = service
    requests = []; tasks = set(); entered = asyncio.Event(); release = asyncio.Event()
    async def endpoint(reader, writer):
        task = asyncio.current_task(); tasks.add(task)
        try:
            async with asyncio.timeout(5):
                raw = await reader.readuntil(b"\r\n\r\n")
                headers = dict(line.split(b": ", 1) for line in raw.split(b"\r\n")[1:] if b": " in line)
                body = json.loads(await reader.readexactly(int(headers[b"Content-Length"])))
                assert headers[b"Authorization"] == b"Bearer synthetic-nonsecret-token"
                requests.append(body)
                entered.set()
                if mode in {"cancel-probe", "revoke-probe"}: await release.wait()
                response = json.dumps({"model":"synthetic-model","choices":[{"message":{"role":"assistant","content":"synthetic-result"},"finish_reason":"stop"}]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(response)).encode() + b"\r\n\r\n" + response)
                await writer.drain()
        finally:
            writer.close(); await writer.wait_closed(); tasks.discard(task)
    server = await asyncio.start_server(endpoint, "127.0.0.1", 0)
    runner = None; installed = None
    try:
        connection = draft["source"]["connection"]
        connection["endpoint"]["port"] = server.sockets[0].getsockname()[1]
        draft = imports.revise_service(draft["draft_id"], 1, source(connection, secrets, validate_components=validate_components))
        value = descriptor(draft, recipe, name=draft["name"], memory_mb=512)
        runner = PreparationRunner(store, host, host / "bin/mcpme-reap", enabled=True, enabled_families=(family.component.reference.key,))
        plan = await runner.plan(draft["draft_id"], draft["revision"], value.model_dump(exclude_none=True))
        if mode == "resolve-revoked":
            resolve = secrets.resolve
            def revoke_at_resolve(references):
                values = resolve(references)
                secrets.revoke(connection["secret_references"]["api_key"])
                return values
            monkeypatch.setattr(secrets, "resolve", revoke_at_resolve)
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        if mode in {"cancel-probe", "revoke-probe"}:
            async with asyncio.timeout(180): await entered.wait()
            if mode == "revoke-probe":
                from types import SimpleNamespace
                from mcpme.preparation_service_routes import revoke
                owned = SimpleNamespace(secrets=secrets, preparations=store, imports=imports,
                    preparation_runner=runner, registry=SimpleNamespace(list_slots=lambda: []))
                await revoke(owned, connection["secret_references"]["api_key"])
            else:
                await runner.cancel(job["job_id"])
        async with asyncio.timeout(300):
            await runner.tasks[job["job_id"]]
        result = store.get(job["job_id"])
        if mode != "complete":
            assert result["state"] in {"canceled", "failed"}
            assert result["ownership_settled"] and not result["reservation_retained"]
            assert not store.pending_ownership(job["job_id"]) and imports.inspect(draft["draft_id"])["attachment"] is None
            assert not Path(result["workspace"]).exists()
            assert len(requests) == (0 if mode == "resolve-revoked" else 1)
            assert all(not owner.instances and not owner._launches and not owner._pending_ids
                       for owner in runner.attempts[job["job_id"]].owners)
            assert server.is_serving()
            return
        assert result["state"] == "succeeded", result["error_code"]
        assert result["ownership_settled"] and not result["reservation_retained"]
        assert not store.pending_ownership(job["job_id"])
        assert result["progress"]["commands_finished"] == 5
        assert result["verification"]["tools_checked"] == 2 and result["verification"]["fixtures_checked"] == 1
        assert len(requests) == 1 and requests[0]["messages"] == [{"role":"user","content":"Hi"}]
        attached = imports.inspect(draft["draft_id"])
        package = validate_package(Path(attached["attachment"]["path"]))
        assert package.digest == result["expected_package_digest"]
        assert attached["source"]["kind"] == "service_connection" and "locator" not in attached["source"]
        receipt = json.loads((package.root / "mcpme.preparation.json").read_bytes())
        assert receipt["schema_version"] == 3 and receipt["inputs"]["source"]["kind"] == "service_connection"
        assert "captured_collection" not in receipt["inputs"]
        for name in ("service.json", "mcpme.preparation.json", "mcpme.package.json", "mcpme.lock.json"):
            assert connection["secret_references"]["api_key"].encode() not in (package.root / name).read_bytes()
        assert attached["requirements"]["secret_references"] == connection["secret_references"]
        assert not Path(result["workspace"]).exists()
        assert all(not owner.instances and not owner._launches and not owner._pending_ids
                   for owner in runner.attempts[job["job_id"]].owners)
        await runner.close()
        from mcpme.supervisor import Supervisor
        from test_mcpme_host import finish
        def private_runner(store, root, reaper, **kwargs):
            kwargs["enabled"] = True
            return PreparationRunner(store, root, reaper, enabled_families=(family.component.reference.key,), **kwargs)
        installed = Supervisor(imports.root, host / "bin/mcpme-reap", preparation_runner_factory=private_runner)
        await installed.initialize()
        installation = await installed.plan_install_async({"import_id":draft["draft_id"],"revision":draft["revision"]})
        operation = await finish(installed, await installed.apply_install(installation["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot_id = operation.slot_id
        await installed.control(slot_id, "start")
        await installed_sdk(installed, slot_id)
        # The original source was metadata from intake. Remove only the
        # derived attachment and author recipe; the installed closure survives.
        shutil.rmtree(package.root)
        shutil.rmtree(imports.root.parent / "recipe")
        await installed.close()
        installed = Supervisor(imports.root, host / "bin/mcpme-reap", preparation_runner_factory=private_runner)
        await installed.initialize(); await installed.control(slot_id, "start")
        await installed_sdk(installed, slot_id)
        from mcpme.preparation_service_routes import revoke
        unrelated = installed.secrets.create("Unrelated synthetic", "synthetic-unrelated-value")
        await revoke(installed, unrelated["secret_id"])
        assert not installed.registry.get_slot(slot_id).wake_suppressed
        await installed_sdk(installed, slot_id)
        await revoke(installed, connection["secret_references"]["api_key"])
        assert installed.registry.get_slot(slot_id).wake_suppressed
        assert not installed.owner(slot_id).instances and not installed.ledger(slot_id)
        assert server.is_serving()  # The external service was never ours.
    finally:
        release.set()
        if installed is not None: await installed.close()
        if runner is not None: await runner.close()
        server.close(); await server.wait_closed()
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
