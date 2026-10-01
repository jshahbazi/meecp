"""Real locked-SDK transport checks through owned test stdio processes."""
import asyncio
import contextlib
import json
import os
import socket
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcpme.bridge import Bridge
from mcpme.gateway import PROTOCOL_VERSION, SessionPipes, encode, make_app

TOOL = {"name": "echo", "description": "Return synthetic data", "inputSchema": {
    "type": "object", "properties": {"value": {}}, "required": ["value"]},
    "outputSchema": {"type": "object", "properties": {"value": {}}, "required": ["value"]}}

CHILD = "\n".join([
    "import sys,json,time",
    "tool=" + repr(TOOL),
    "def send(x): print(json.dumps(x,separators=(',',':')),flush=True)",
    "for line in sys.stdin:",
    " m=json.loads(line); method=m.get('method'); rid=m.get('id')",
    " if 'id' not in m: continue",
    " if method=='initialize': result={'protocolVersion':'2025-11-25','capabilities':{'tools':{'listChanged':True}},'serverInfo':{'name':'fixture','version':'1'}}",
    " elif method=='tools/list': result={'tools':[tool]}",
    " elif method=='ping': result={}",
    " elif method=='tools/call':",
    "  p=m.get('params',{}); value=p.get('arguments',{}).get('value')",
    "  if value=='slow': time.sleep(0.3)",
    "  if value=='hang': time.sleep(10)",
    "  if value=='oversize': value='x'*(1024*1024)",
    "  if value=='cancel':",
    "   cancellation=json.loads(sys.stdin.readline())",
    "   value=cancellation.get('params',{})",
    "  if value=='server-ping':",
    "   send({'jsonrpc':'2.0','id':'server/string','method':'ping'})",
    "   value=json.loads(sys.stdin.readline())",
    "  token=p.get('_meta',{}).get('progressToken')",
    "  if token is not None: send({'jsonrpc':'2.0','method':'notifications/progress','params':{'progressToken':token,'progress':1,'total':1}})",
    "  result={'content':[{'type':'text','text':json.dumps({'value':value})}],'structuredContent':{'value':value},'isError':False}",
    "  if value=='notify': send({'jsonrpc':'2.0','method':'notifications/tools/list_changed'})",
    " else: continue",
    " send({'jsonrpc':'2.0','id':rid,'result':result})",
])


class Owner:
    def __init__(self):
        self.processes = {}
        self.launched = 0
        self.closed = []

    async def launch(self, sid):
        stdin_read, stdin_write = os.pipe()
        stdout_read, stdout_write = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", CHILD,
                stdin=stdin_read, stdout=stdout_write, stderr=asyncio.subprocess.DEVNULL)
        finally:
            os.close(stdin_read)
            os.close(stdout_write)
        self.processes[sid] = process
        self.launched += 1
        return SessionPipes(sid, stdout_read, stdin_write)

    async def close(self, iid):
        process = self.processes.pop(iid)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        await process.wait()
        self.closed.append(iid)


@asynccontextmanager
async def live_gateway(**options):
    owner = Owner()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    app, gateway = make_app(token="secret", port=port, launch_session=owner.launch, close_session=owner.close, **options)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp", owner, gateway
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


def client(endpoint):
    return httpx.AsyncClient(headers={"authorization": "Bearer secret"}, trust_env=False, timeout=5)


async def test_locked_sdk_schema_structured_output_progress_and_cleanup():
    async with live_gateway() as (endpoint, owner, gateway):
        progress = []
        async with client(endpoint) as http:
            async with streamable_http_client(endpoint, http_client=http) as (read, write, get_session_id):
                async with ClientSession(read, write) as session:
                    result = await session.initialize()
                    assert result.protocolVersion == PROTOCOL_VERSION
                    tools = await session.list_tools()
                    assert tools.tools[0].model_dump(exclude_none=True) == TOOL
                    async def on_progress(value, total, message):
                        progress.append((value, total))
                    output = await session.call_tool("echo", {"value": {"nested": [1, "two"]}}, progress_callback=on_progress)
                    assert output.structuredContent == {"value": {"nested": [1, "two"]}}
                    assert progress == [(1, 1)]
                    assert get_session_id() in gateway.sessions
                    assert owner.launched == 1
        assert gateway.sessions == {}
        assert len(owner.closed) == 1


async def test_locked_sdk_unsolicited_list_changed_notification():
    async with live_gateway() as (endpoint, owner, gateway):
        received = asyncio.Event()
        async def handler(message):
            if getattr(getattr(message, "root", None), "method", None) == "notifications/tools/list_changed":
                received.set()
        async with client(endpoint) as http:
            async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
                async with ClientSession(read, write, message_handler=handler) as session:
                    await session.initialize()
                    await session.call_tool("echo", {"value": "notify"})
                    await asyncio.wait_for(received.wait(), 2)


async def test_locked_sdk_bidirectional_ping_preserves_server_id():
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                output = await session.call_tool("echo", {"value": "server-ping"})
                assert output.structuredContent == {"value": {"jsonrpc": "2.0", "id": "server/string", "result": {}}}


async def test_real_sdk_stdio_bridge_initializes_calls_and_reaps():
    async with live_gateway() as (endpoint, owner, gateway):
        program = "import asyncio; from mcpme.bridge import run_bridge; asyncio.run(run_bridge(" + repr(endpoint) + ",'secret'))"
        parameters = StdioServerParameters(command=sys.executable, args=["-c", program],
                    env={"PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend")})
        with open(os.devnull, "w") as errlog:
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.protocolVersion == PROTOCOL_VERSION
                    listed = await session.list_tools()
                    assert listed.tools[0].inputSchema == TOOL["inputSchema"]
                    output = await session.call_tool("echo", {"value": ["through", "stdio"]})
                    assert output.structuredContent == {"value": ["through", "stdio"]}
                    output = await session.call_tool("echo", {"value": "server-ping"})
                    assert output.structuredContent == {"value": {"jsonrpc": "2.0", "id": "server/string", "result": {}}}
        for _ in range(100):
            if not gateway.sessions:
                break
            await asyncio.sleep(0.01)
        assert not gateway.sessions
        assert owner.launched == 1
        assert len(owner.closed) == 1


HEADERS = {"authorization": "Bearer secret", "accept": "application/json, text/event-stream",
           "content-type": "application/json", "mcp-protocol-version": PROTOCOL_VERSION}
INIT = {"jsonrpc": "2.0", "id": "original-init", "method": "initialize", "params": {
    "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


async def initialize_raw(http, endpoint):
    response = await http.post(endpoint, json=INIT, headers=HEADERS)
    assert response.status_code == 200
    sid = response.headers["mcp-session-id"]
    headers = {**HEADERS, "mcp-session-id": sid}
    response = await http.post(endpoint, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    assert response.status_code == 202
    return sid, headers


async def test_disconnect_replay_preserves_child_and_original_request_id():
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        payload = {"jsonrpc": "2.0", "id": "request/string", "method": "tools/call",
                   "params": {"name": "echo", "arguments": {"value": "slow"}}}
        event_id = None
        async with http.stream("POST", endpoint, json=payload, headers=headers) as response:
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    event_id = line[4:]
                    break
        assert event_id
        await asyncio.sleep(0.4)
        assert sid in gateway.sessions
        assert owner.launched == 1
        async with http.stream("GET", endpoint, headers={**headers, "last-event-id": event_id}) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line[6:]:
                    output = json.loads(line[6:])
                    assert output["id"] == "request/string"
                    assert output["result"]["structuredContent"] == {"value": "slow"}
                    break
        assert owner.launched == 1
        assert (await http.delete(endpoint, headers=headers)).status_code == 200
        assert (await http.get(endpoint, headers=headers)).status_code == 404
        assert owner.processes == {}


async def test_capacity_reserved_before_launch_and_idle_expiry_reaps():
    async with live_gateway(max_sessions=1, idle_timeout=0.15) as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        refused = await http.post(endpoint, json=INIT, headers=HEADERS)
        assert refused.status_code == 429
        assert owner.launched == 1
        await asyncio.sleep(0.4)
        assert sid not in gateway.sessions
        assert (await http.get(endpoint, headers=headers)).status_code == 404
        assert len(owner.closed) == 1
        await initialize_raw(http, endpoint)
        assert owner.launched == 2


async def test_rejected_requests_do_not_renew_idle_lease():
    async with live_gateway(idle_timeout=0.3) as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        last_activity = gateway.sessions[sid].last_activity
        requests = [
            ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, headers, 400),
            ({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": []}}, headers, 400),
            ({"jsonrpc": "2.0", "id": 3, "method": "ping"}, {**headers, "accept": "application/json"}, 406),
            ({"jsonrpc": "2.0", "id": 4, "method": "ping"}, {**headers, "mcp-protocol-version": "unsupported"}, 400),
        ]
        for payload, request_headers, expected in requests:
            assert (await http.post(endpoint, headers=request_headers, json=payload)).status_code == expected
            assert gateway.sessions[sid].last_activity == last_activity
        assert (await http.get(endpoint, headers={**headers, "last-event-id": "unknown"})).status_code == 400
        assert gateway.sessions[sid].last_activity == last_activity
        await asyncio.sleep(0.5)
        assert sid not in gateway.sessions


async def test_request_deadline_reaps_even_after_disconnect():
    async with live_gateway(request_timeout=0.15) as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        async with http.stream("POST", endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 8,
                 "method": "tools/call", "params": {"name": "echo", "arguments": {"value": "hang"}}}) as response:
            async for line in response.aiter_lines():
                if line.startswith("id:"):
                    break
        await asyncio.sleep(0.4)
        assert sid not in gateway.sessions
        assert not owner.processes


async def test_oversized_package_output_is_sanitized_and_reaped():
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        response = await http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 9,
                    "method": "tools/call", "params": {"name": "echo", "arguments": {"value": "oversize"}}})
        values = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:]]
        assert values[-1]["error"]["message"] == "The slot request could not be completed."
        assert "xxx" not in response.text
        assert sid not in gateway.sessions
        for _ in range(100):
            if not owner.processes:
                break
            await asyncio.sleep(0.01)
        assert not owner.processes


async def test_server_unsupported_capabilities_close_session(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "CHILD", CHILD.replace("'tools':{'listChanged':True}", "'resources':{}"))
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        response = await http.post(endpoint, json=INIT, headers=HEADERS)
        values = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:]]
        assert values[-1]["error"]["message"] == "The package requires unsupported MCP capabilities or version."
        assert not gateway.sessions


async def test_close_ownership_failure_retains_capacity_and_blocks_admission():
    stdout_read, stdout_write = os.pipe()
    stdin_read, stdin_write = os.pipe()
    async def launch(sid):
        return SessionPipes("unresolved", stdout_read, stdin_write)
    async def close(iid):
        raise RuntimeError("ownership_lost")
    _, gateway = make_app(token="secret", port=19386, launch_session=launch, close_session=close)
    try:
        session = await gateway.new_session()
        await gateway.close(session.session_id)
        assert gateway.reservations == {session.session_id}
        assert gateway.error_code == "ownership_lost"
        with pytest.raises(RuntimeError, match="slot_stopped"):
            await gateway.new_session()
        with pytest.raises(RuntimeError, match="ownership_lost"):
            await gateway.shutdown()
    finally:
        os.close(stdout_write)
        os.close(stdin_read)


async def test_cancellation_notification_preserves_target_and_reason():
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        call = asyncio.create_task(http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": "cancel/string",
                 "method": "tools/call", "params": {"name": "echo", "arguments": {"value": "cancel"}}}))
        for _ in range(100):
            if (str, "cancel/string") in gateway.sessions[sid].pending:
                break
            await asyncio.sleep(0.01)
        cancellation = {"requestId": "cancel/string", "reason": "synthetic cancellation"}
        response = await http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                                   "params": cancellation})
        assert response.status_code == 202
        result = await asyncio.wait_for(call, 2)
        values = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ") and line[6:]]
        assert values[-1]["id"] == "cancel/string"
        assert values[-1]["result"]["structuredContent"] == {"value": cancellation}
        assert sid in gateway.sessions
        assert owner.launched == 1


async def test_integer_and_string_ids_are_distinct_and_duplicate_rejected():
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        sid, headers = await initialize_raw(http, endpoint)
        for rid in (1, "1"):
            result = await http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": rid, "method": "ping"})
            values = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ") and line[6:]]
            assert values[-1]["id"] == rid
            assert type(values[-1]["id"]) is type(rid)
        assert (await http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 429


@pytest.mark.parametrize("change", [{"protocolVersion": "2025-03-26"}, {"capabilities": {"sampling": {}}}])
async def test_unsupported_profile_never_launches(change):
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        payload = {**INIT, "params": {**INIT["params"], **change}}
        assert (await http.post(endpoint, json=payload, headers=HEADERS)).status_code == 400
        assert owner.launched == 0


async def test_bridge_forwarding_and_eof_delete():
    async with live_gateway() as (endpoint, owner, gateway):
        stdin = asyncio.StreamReader()
        stdout = asyncio.Queue()
        bridge = Bridge(endpoint, "secret")
        async def write(data):
            await stdout.put(load_line(data))
        task = asyncio.create_task(bridge.run(stdin, write))
        stdin.feed_data(encode(INIT) + b"\n")
        response = await asyncio.wait_for(stdout.get(), 3)
        assert response["id"] == "original-init"
        stdin.feed_data(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}) + b"\n")
        stdin.feed_data(encode({"jsonrpc": "2.0", "id": "same-id", "method": "tools/list"}) + b"\n")
        response = await asyncio.wait_for(stdout.get(), 3)
        assert response["id"] == "same-id"
        assert response["result"]["tools"] == [TOOL]
        stdin.feed_eof()
        await asyncio.wait_for(task, 3)
        assert not gateway.sessions
        assert len(owner.closed) == 1


def load_line(data):
    return json.loads(data)
