"""Real scoped management SDK journeys and bounded stdio/privacy regressions."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import uuid

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcpme.gateway import MAX_MESSAGE_BYTES, MAX_PENDING, PROTOCOL_VERSION, encode
from mcpme.management import Management, serve_streams
from mcpme.private import write_private_json
from test_mcpme_host import finish, prepared, tool_call
from test_mcpme_management_cli import execute, private_manager

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "synthetic-private-request-must-not-appear-in-error"


@asynccontextmanager
async def sdk(state, credential, errors):
    parameters = StdioServerParameters(command=sys.executable, args=["-P", "-m", "mcpme", "--state-dir", str(state),
        "--credential-file", str(credential), "management", "connect"], env={"PYTHONPATH": str(ROOT / "backend")})
    with errors.open("w") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as client:
                yield client
    assert errors.read_text() == ""


async def test_sdk_prepare_plan_exact_grant_duplicate_apply_and_revocation(tmp_path, prepared):
    package, reaper = prepared
    async with private_manager(tmp_path, reaper) as host:
        grant = await execute(host.root, "authorize", "--prepare")
        async with sdk(host.root, grant["credential_file"], tmp_path / "prepare-stderr") as client:
            initialized = await client.initialize()
            assert initialized.protocolVersion == PROTOCOL_VERSION
            assert set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert len(names) == 18
            assert not any("authorize" in name or "revoke" in name for name in names)
            assert all(tool.inputSchema["additionalProperties"] is False for tool in listed.tools)
            assert not host.workers and not host.owners and not host.registry.list_slots()
            output = await client.call_tool("mcpme_create_import", {
                "source": str(package), "name": "Managed SDK fixture", "start": True})
            assert not output.isError
            draft = output.structuredContent
            inspected = await client.call_tool("mcpme_inspect_import", {"draft_id": draft["draft_id"]})
            assert inspected.structuredContent["revision"] == draft["revision"]
            exported = await client.call_tool("mcpme_export_import", {"draft_id": draft["draft_id"], "revision": draft["revision"]})
            assert not exported.isError
            result = await client.call_tool("mcpme_plan_install", {"import_id": draft["draft_id"], "revision": draft["revision"]})
            assert not result.isError
            plan = result.structuredContent
            denied = await client.call_tool("mcpme_apply_install", {"plan_id": plan["plan_id"]})
            assert denied.isError and denied.structuredContent["error"]["code"] == "grant_scope"
            assert not host.registry.list_operations()
        installation = await execute(host.root, "authorize", "--plan", plan["plan_id"])
        async with sdk(host.root, installation["credential_file"], tmp_path / "install-stderr") as client:
            await client.initialize()
            inspected = await client.call_tool("mcpme_inspect_install_plan", {"plan_id": plan["plan_id"]})
            assert inspected.structuredContent["plan_id"] == plan["plan_id"]
            wrong = await client.call_tool("mcpme_apply_install", {"plan_id": str(uuid.uuid4())})
            assert wrong.isError and wrong.structuredContent["error"]["code"] == "grant_scope"
            first, duplicate = await asyncio.gather(*(client.call_tool("mcpme_apply_install", {"plan_id": plan["plan_id"]}) for _ in range(2)))
            assert not first.isError and not duplicate.isError
            assert first.structuredContent["operation_id"] == duplicate.structuredContent["operation_id"]
            operation = await finish(host, first.structuredContent)
            assert operation.status == "completed"
            status = await client.call_tool("mcpme_get_operation", {"operation_id": operation.id})
            assert status.structuredContent["status"] == "completed"
            assert len(host.registry.list_slots()) == 1
            assert await tool_call(host, operation.slot_id) == {"valid": True, "kind": "object"}
            await execute(host.root, "grants", "revoke", installation["grant_id"])
            denied = await client.call_tool("mcpme_capabilities", {})
            assert denied.isError and denied.structuredContent["error"]["code"] == "grant_credential_unavailable"


async def test_sdk_handshake_and_tool_list_require_no_host_or_slot_start(tmp_path):
    state = tmp_path / "absent-host"
    credential = tmp_path / "private" / "grant.json"
    write_private_json(credential, {"schema_version": 1, "grant_id": str(uuid.uuid4()), "token": secrets.token_urlsafe(32)})
    async with sdk(state, credential, tmp_path / "stderr") as client:
        initialized = await client.initialize()
        assert initialized.serverInfo.name == "mcpme-management"
        assert len((await client.list_tools()).tools) == 18
        invalid = await client.call_tool("mcpme_apply_install", {"plan_id": str(uuid.uuid4()), "approved": SENTINEL})
        assert invalid.isError and invalid.structuredContent["error"]["code"] == "invalid_tool_arguments"
        unknown = await client.call_tool("mcpme_authorize", {"token": SENTINEL})
        assert unknown.isError and unknown.structuredContent["error"]["code"] == "unknown_tool"
        unavailable = await client.call_tool("mcpme_capabilities", {})
        assert unavailable.isError and unavailable.structuredContent["error"]["code"] == "host_unavailable"
        assert SENTINEL not in invalid.model_dump_json() + unknown.model_dump_json() + unavailable.model_dump_json()
    assert not state.exists()


@pytest.mark.parametrize("name,arguments", [
    ("mcpme_plan_install", {"package": "/pkg", "import_id": str(uuid.uuid4()), "revision": 1}),
    ("mcpme_plan_install", {"import_id": str(uuid.uuid4())}),
    ("mcpme_revise_import", {"draft_id": str(uuid.uuid4()), "revision": 1}),
    ("mcpme_revise_import", {"draft_id": str(uuid.uuid4()), "revision": True, "intent": SENTINEL}),
    ("mcpme_revise_import", {"draft_id": str(uuid.uuid4()), "revision": 1, "requirements": {"api_key": SENTINEL}}),
    ("mcpme_create_import", {"source": "/src", "name": "Source", "files": ["same.py", "same.py"]}),
    ("mcpme_start_slot", {"slot_id": SENTINEL}),
])
async def test_narrow_schemas_reject_unknown_and_ambiguous_arguments_without_api(name, arguments):
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid_arguments_reached_api")
    result = await Management(Path("unused"), Path("unused"), caller=forbidden).invoke(name, arguments)
    assert result.structuredContent == {"api_version": 1, "error": {"code": "invalid_tool_arguments"}}
    assert SENTINEL not in result.model_dump_json()


@pytest.mark.parametrize("outcome", ["exception", "error", "large", "escaped"])
async def test_errors_and_oversized_outputs_are_content_free(outcome):
    async def caller(*args, **kwargs):
        if outcome == "exception":
            raise RuntimeError(SENTINEL)
        if outcome == "large":
            return {"payload": "x" * MAX_MESSAGE_BYTES}
        if outcome == "escaped":
            return {"payload": '"' * 200000}
        return {"error": {"code": SENTINEL, "message": SENTINEL, "details": {"token": SENTINEL}}}
    result = await Management(Path("unused"), Path("unused"), caller=caller).invoke("mcpme_capabilities", {})
    assert result.isError and SENTINEL not in result.model_dump_json()
    if outcome in {"large", "escaped"}:
        assert result.structuredContent["error"]["code"] == "response_limit"


async def test_pending_requests_are_bounded_and_eof_cancels_waiting_handlers():
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    initialized, saturated = asyncio.Event(), asyncio.Event()
    active = maximum = 0
    async def caller(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.Future()
        finally:
            active -= 1
    output = []
    async def writer(data):
        value = json.loads(data)
        output.append(value)
        if value.get("id") == 0:
            initialized.set()
        if value.get("id") == MAX_PENDING + 1:
            saturated.set()
    reader.feed_data(encode({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}) + b"\n")
    task = asyncio.create_task(serve_streams(Management(Path("unused"), Path("unused"), caller=caller), reader, writer))
    try:
        await asyncio.wait_for(initialized.wait(), 2)
        reader.feed_data(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}) + b"\n")
        for request_id in range(1, MAX_PENDING + 2):
            reader.feed_data(encode({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                "name": "mcpme_capabilities", "arguments": {}}}) + b"\n")
        await asyncio.wait_for(saturated.wait(), 2)
        assert maximum <= MAX_PENDING
        assert output[-1]["error"]["code"] == -32000
        reader.feed_eof()
        await asyncio.wait_for(task, 2)
        assert active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("data", [b'{"jsonrpc":"2.0","id":1,"id":2}\n', b'{"jsonrpc":"2.0","id":NaN}\n',
    b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n", b'{"jsonrpc":"2.0"}'])
async def test_invalid_or_oversized_stdio_frames_close_without_raw_output(data):
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    reader.feed_data(data)
    reader.feed_eof()
    output = []
    async def writer(value):
        output.append(value)
    await asyncio.wait_for(serve_streams(Management(Path("unused"), Path("unused")), reader, writer), 2)
    assert output == []


@pytest.mark.parametrize("shutdown", ["eof", signal.SIGTERM])
async def test_management_cli_eof_and_sigterm_exit_without_diagnostics(tmp_path, shutdown):
    credential = tmp_path / "private" / "grant.json"
    write_private_json(credential, {"schema_version": 1, "grant_id": str(uuid.uuid4()), "token": secrets.token_urlsafe(32)})
    process = await asyncio.create_subprocess_exec(sys.executable, "-P", "-m", "mcpme", "--state-dir", str(tmp_path / "absent"),
        "--credential-file", str(credential), "management", "connect", env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        process.stdin.write(encode({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}) + b"\n")
        await process.stdin.drain()
        assert json.loads(await asyncio.wait_for(process.stdout.readline(), 5))["result"]["serverInfo"]["name"] == "mcpme-management"
        if shutdown == "eof":
            process.stdin.close()
        else:
            process.send_signal(shutdown)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0
        assert stdout == stderr == b""
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
