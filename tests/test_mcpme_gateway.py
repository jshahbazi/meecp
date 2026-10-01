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

# Installed VS Code Insiders 1.141.0 advertises these optional client services.
VSCODE_CLIENT_CAPABILITIES = {
    "roots": {"listChanged": True}, "sampling": {}, "elicitation": {"form": {}, "url": {}},
    "tasks": {"list": {}, "cancel": {}, "requests": {
        "sampling": {"createMessage": {}}, "elicitation": {"create": {}}}},
    "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}},
}

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


async def test_repeated_abrupt_client_exits_reclaim_children_and_preserve_survivor():
    # os._exit closes the client socket without HTTP DELETE or context cleanup.
    # Alternate idle sessions with abandoned calls whose children remain blocked.
    program = "\n".join([
        "import asyncio,json,os,sys,httpx",
        "endpoint,mode=sys.argv[1:]",
        "headers=" + repr(HEADERS),
        "initial=" + repr(INIT),
        "async def run():",
        " async with httpx.AsyncClient(trust_env=False,timeout=3) as http:",
        "  response=await http.post(endpoint,json=initial,headers=headers)",
        "  assert response.status_code==200",
        "  sid=response.headers['mcp-session-id']; headers['mcp-session-id']=sid",
        "  response=await http.post(endpoint,json={'jsonrpc':'2.0','method':'notifications/initialized'},headers=headers)",
        "  assert response.status_code==202",
        "  if mode=='deadline':",
        "   payload={'jsonrpc':'2.0','id':'abandoned','method':'tools/call','params':{'name':'echo','arguments':{'value':'hang'}}}",
        "   async with http.stream('POST',endpoint,json=payload,headers=headers) as response:",
        "    assert response.status_code==200",
        "    async for line in response.aiter_lines():",
        "     if line.startswith('id:'): break",
        "    print(sid,flush=True); os._exit(0)",
        "  print(sid,flush=True); os._exit(0)",
        "asyncio.run(run())",
    ])
    capacity = 2
    async with live_gateway(max_sessions=capacity, idle_timeout=0.6, request_timeout=0.2) as (endpoint, owner, gateway), client(endpoint) as http:
        survivor_id, survivor_headers = await initialize_raw(http, endpoint)
        survivor_child = owner.processes[survivor_id]

        async def keep_survivor_alive():
            sequence = 0
            while True:
                sequence += 1
                response = await http.post(endpoint, headers=survivor_headers,
                    json={"jsonrpc": "2.0", "id": f"keepalive-{sequence}", "method": "ping"})
                assert response.status_code == 200
                await asyncio.sleep(0.1)

        heartbeat = asyncio.create_task(keep_survivor_alive())
        abandoned_children = []
        try:
            for cycle in range(capacity * 3):
                process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", program,
                    endpoint, "idle" if cycle % 2 == 0 else "deadline",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    stdout, _ = await asyncio.wait_for(process.communicate(), 5)
                    assert process.returncode == 0
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                sid = stdout.decode().strip()
                abandoned = owner.processes[sid]
                abandoned_children.append(abandoned)
                assert sid != survivor_id
                assert len(owner.processes) == capacity
                # Capacity is truly occupied until the lease/deadline reaps it.
                assert (await http.post(endpoint, json=INIT, headers=HEADERS)).status_code == 429
                await asyncio.wait_for(abandoned.wait(), 3)
                for _ in range(100):
                    if sid not in gateway.sessions and sid in owner.closed:
                        break
                    await asyncio.sleep(0.01)
                assert abandoned.returncode is not None
                assert sid not in gateway.sessions
                assert sid in owner.closed
                assert (await http.get(endpoint, headers={**HEADERS, "mcp-session-id": sid})).status_code == 404
                assert owner.processes == {survivor_id: survivor_child}
                assert survivor_child.returncode is None
                assert not heartbeat.done()

            response = await http.post(endpoint, headers=survivor_headers, json={
                "jsonrpc": "2.0", "id": "survived-churn", "method": "tools/call",
                "params": {"name": "echo", "arguments": {"value": "still-responsive"}}})
            values = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:]]
            assert values[-1]["result"]["structuredContent"] == {"value": "still-responsive"}
            assert owner.launched == capacity * 3 + 1
            assert len(owner.closed) == capacity * 3
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        assert (await http.delete(endpoint, headers=survivor_headers)).status_code == 200
        assert survivor_child.returncode is not None
        assert all(process.returncode is not None for process in abandoned_children)
        assert owner.processes == {}
        assert gateway.sessions == {}


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


@pytest.mark.parametrize("change", [{"protocolVersion": "2025-03-26"}, {"capabilities": {"sampling": []}}])
async def test_unsupported_profile_never_launches(change):
    async with live_gateway() as (endpoint, owner, gateway), client(endpoint) as http:
        payload = {**INIT, "params": {**INIT["params"], **change}}
        assert (await http.post(endpoint, json=payload, headers=HEADERS)).status_code == 400
        assert owner.launched == 0


async def test_vscode_capability_offers_initialize_over_stdio_bridge_without_private_promises(monkeypatch):
    monkeypatch.setitem(globals(), "CHILD", CHILD.replace(
        "if method=='initialize': result=", "if method=='initialize':\n  assert m['params']['capabilities']=={}\n  result="))
    async with live_gateway() as (endpoint, owner, gateway):
        stdin = asyncio.StreamReader()
        stdout = asyncio.Queue()
        bridge = Bridge(endpoint, "secret")
        async def write(data):
            await stdout.put(load_line(data))
        task = asyncio.create_task(bridge.run(stdin, write))
        try:
            payload = {**INIT, "params": {**INIT["params"], "capabilities": VSCODE_CLIENT_CAPABILITIES,
                        "clientInfo": {"name": "Visual Studio Code - Insiders", "version": "1.141.0-insider"}}}
            stdin.feed_data(encode(payload) + b"\n")
            response = await asyncio.wait_for(stdout.get(), 3)
            assert response["id"] == payload["id"]
            assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
            assert set(response["result"]["capabilities"]) == {"tools"}
            stdin.feed_data(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}) + b"\n")
            stdin.feed_data(encode({"jsonrpc": "2.0", "id": "vscode-tools", "method": "tools/list"}) + b"\n")
            response = await asyncio.wait_for(stdout.get(), 3)
            assert response["id"] == "vscode-tools" and response["result"]["tools"] == [TOOL]
            async with client(endpoint) as http:
                for method in ("roots/list", "sampling/createMessage", "elicitation/create", "tasks/list"):
                    response = await http.post(endpoint, headers={**HEADERS, "mcp-session-id": bridge.session_id},
                        json={"jsonrpc": "2.0", "id": method, "method": method})
                    assert response.status_code == 400
            assert owner.launched == 1
        finally:
            stdin.feed_eof()
            await asyncio.wait_for(task, 3)
        assert not owner.processes and not gateway.sessions


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
