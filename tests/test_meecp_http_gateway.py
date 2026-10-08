"""HTTP runtime isolation and transport checks using the locked MCP SDK."""
import asyncio
import contextlib
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from meecp.gateway import PROTOCOL_VERSION
from meecp.http_gateway import make_http_app
from meecp.policy import LocalPolicy
from test_meecp_gateway import CHILD, HEADERS, INIT, TOOL, VSCODE_CLIENT_CAPABILITIES, live_gateway


@asynccontextmanager
async def serve_app(factory):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app, auxiliary = factory(port)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp", auxiliary
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


@asynccontextmanager
async def public_proxy(endpoint, **options):
    async with serve_app(lambda port: make_http_app(token="public-key", port=port,
                           upstream_endpoint=endpoint, upstream_token="secret" if endpoint else None, **options)) as result:
        yield result


def public_client():
    return httpx.AsyncClient(headers={"authorization": "Bearer public-key"}, trust_env=False, timeout=5)


def public_headers(sid=None):
    return {**HEADERS, "authorization": "Bearer public-key", **({"mcp-session-id": sid} if sid else {})}


async def initialize(http, endpoint):
    response = await http.post(endpoint, json=INIT, headers=public_headers())
    assert response.status_code == 200
    sid = response.headers["mcp-session-id"]
    assert (await http.post(endpoint, headers=public_headers(sid), json={"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
    return sid


@asynccontextmanager
async def stateless_runtime():
    stats = {"calls": 0, "active": 0, "max_active": 0, "legacy_calls": 0, "client_capabilities": []}
    lock = asyncio.Lock()
    async def handle(request):
        if request.url.path == "/api/status":
            return JSONResponse({"ready": True, "paused": False})
        if request.url.path == "/api/decide":
            stats["legacy_calls"] += 1
            return JSONResponse({"synthetic": True})
        if request.method != "POST":
            return Response(status_code=405)
        message = request.scope["meecp.message"]
        method = message.get("method")
        if "id" not in message:
            return Response(status_code=202)
        if method == "initialize":
            stats["client_capabilities"].append(message["params"]["capabilities"])
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "shared", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            async with lock:
                stats["calls"] += 1
                stats["active"] += 1
                stats["max_active"] = max(stats["max_active"], stats["active"])
                try:
                    value = message["params"]["arguments"]["value"]
                    if value == "slow":
                        await asyncio.sleep(0.3)
                    result = {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": {"value": value}}
                finally:
                    stats["active"] -= 1
        else:
            result = {}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})
    def factory(port):
        app = Starlette(routes=[Route("/mcp", handle, methods=["GET", "POST", "DELETE"]),
                               Route("/api/status", handle), Route("/api/decide", handle, methods=["POST"])])
        return LocalPolicy(app, "secret", port), stats
    async with serve_app(factory) as result:
        yield result


async def sdk_call(endpoint, value):
    async with public_client() as http:
        async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.protocolVersion == PROTOCOL_VERSION
                listed = await session.list_tools()
                assert listed.tools[0].inputSchema == TOOL["inputSchema"]
                output = await session.call_tool("echo", {"value": value})
                return output.structuredContent


async def test_stateless_runtime_two_sdk_clients_share_service_and_private_auth():
    async with stateless_runtime() as (private, stats), public_proxy(private, expected_tools=[TOOL]) as (public, gateway):
        outputs = await asyncio.gather(sdk_call(public, "slow"), sdk_call(public, {"literal": [1, 2]}))
        assert outputs == [{"value": "slow"}, {"value": {"literal": [1, 2]}}]
        assert stats["calls"] == 2
        assert stats["max_active"] == 1
        assert not gateway.sessions
        async with public_client() as http:
            assert (await http.get(private)).status_code == 401
            assert (await http.get(public, headers={"authorization": "Bearer secret"})).status_code == 401
            assert (await http.get(public.removesuffix("/mcp") + "/api/status")).status_code == 404
            assert (await http.get(public.removesuffix("/mcp") + "/health")).status_code == 404


async def test_vscode_client_offers_do_not_enable_private_http_capabilities():
    async with stateless_runtime() as (private, stats), public_proxy(private) as (public, gateway), public_client() as http:
        payload = {**INIT, "params": {**INIT["params"], "capabilities": VSCODE_CLIENT_CAPABILITIES}}
        response = await http.post(public, json=payload, headers=public_headers())
        values = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line[6:]]
        assert response.status_code == 200
        assert values[-1]["id"] == payload["id"]
        assert values[-1]["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert set(values[-1]["result"]["capabilities"]) == {"tools"}
        assert stats["client_capabilities"] == [{}]
        assert (await http.delete(public, headers=public_headers(response.headers["mcp-session-id"]))).status_code == 200


async def test_stateful_session_mapping_progress_ping_notification_and_delete():
    async with live_gateway() as (private, owner, upstream), public_proxy(private) as (public, gateway):
        notified = asyncio.Event()
        async def handler(message):
            if getattr(getattr(message, "root", None), "method", None) == "notifications/tools/list_changed":
                notified.set()
        progress = []
        async def on_progress(value, total, message):
            progress.append((value, total))
        async with public_client() as http:
            async with streamable_http_client(public, http_client=http) as (read, write, get_sid):
                async with ClientSession(read, write, message_handler=handler) as session:
                    await session.initialize()
                    sid = get_sid()
                    private_sid = gateway.sessions[sid].private_session_id
                    assert private_sid != sid
                    assert private_sid in upstream.sessions
                    assert (await http.get(public, headers=public_headers(private_sid))).status_code == 404
                    output = await session.call_tool("echo", {"value": "notify"}, progress_callback=on_progress)
                    assert output.structuredContent == {"value": "notify"}
                    await asyncio.wait_for(notified.wait(), 2)
                    assert progress == [(1, 1)]
                    output = await session.call_tool("echo", {"value": "server-ping"})
                    assert output.structuredContent == {"value": {"jsonrpc": "2.0", "id": "server/string", "result": {}}}
        assert not upstream.sessions
        assert not gateway.sessions
        assert len(owner.closed) == 1


async def test_http_disconnect_replays_result_without_reexecuting_or_replacing_upstream():
    async with stateless_runtime() as (private, stats), public_proxy(private) as (public, gateway), public_client() as http:
        sid = await initialize(http, public)
        async with http.stream("POST", public, headers=public_headers(sid), json={"jsonrpc": "2.0", "id": "original/string",
                "method": "tools/call", "params": {"name": "echo", "arguments": {"value": "slow"}}}) as response:
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    last_event_id = line[4:]
                    break
        await asyncio.sleep(0.4)
        assert stats["calls"] == 1
        async with http.stream("GET", public, headers={**public_headers(sid), "last-event-id": last_event_id}) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line[6:]:
                    value = json.loads(line[6:])
                    assert value["id"] == "original/string"
                    assert value["result"]["structuredContent"] == {"value": "slow"}
                    break
        assert stats["calls"] == 1


async def test_http_cancellation_preserves_request_id_reason():
    async with live_gateway() as (private, owner, upstream), public_proxy(private) as (public, gateway), public_client() as http:
        sid = await initialize(http, public)
        operation = asyncio.create_task(http.post(public, headers=public_headers(sid), json={"jsonrpc": "2.0", "id": "cancel/target",
                     "method": "tools/call", "params": {"name": "echo", "arguments": {"value": "cancel"}}}))
        private_sid = gateway.sessions[sid].private_session_id
        for _ in range(100):
            if (str, "cancel/target") in upstream.sessions[private_sid].pending:
                break
            await asyncio.sleep(0.01)
        cancellation = {"requestId": "cancel/target", "reason": "synthetic cancellation"}
        response = await http.post(public, headers=public_headers(sid), json={"jsonrpc": "2.0", "method": "notifications/cancelled", "params": cancellation})
        assert response.status_code == 202
        result = await asyncio.wait_for(operation, 2)
        output = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ") and line[6:]][-1]
        assert output["id"] == "cancel/target"
        assert output["result"]["structuredContent"] == {"value": cancellation}
        assert sid in gateway.sessions


async def test_http_capacity_expiry_and_generation_replacement_close_mappings():
    async with live_gateway() as (private, owner, upstream), public_proxy(private, max_sessions=1, idle_timeout=0.2) as (public, gateway), public_client() as http:
        sid = await initialize(http, public)
        assert (await http.post(public, headers=public_headers(), json=INIT)).status_code == 429
        assert owner.launched == 1
        await asyncio.sleep(0.4)
        assert not gateway.sessions
        assert not upstream.sessions
        assert (await http.get(public, headers=public_headers(sid))).status_code == 404
        sid = await initialize(http, public)
        await gateway.configure(None, None)
        assert not gateway.sessions and not upstream.sessions
        assert not owner.processes
        await gateway.configure(private, "secret")
        assert (await http.get(public, headers=public_headers(sid))).status_code == 404


async def test_legacy_routes_only_enabled_marker_and_scoped_control_works_stopped():
    async with stateless_runtime() as (private, stats):
        calls = []
        gateway_ref = {}
        async def control(action):
            calls.append(action)
            if action == "pause":
                await gateway_ref["gateway"].configure(None, None)
            elif action == "resume":
                await gateway_ref["gateway"].configure(private, "secret")
            return {"ready": action == "resume", "paused": action != "resume"}
        async with public_proxy(private, legacy_jev=True, control=control) as (public, gateway), public_client() as http:
            gateway_ref["gateway"] = gateway
            base = public.removesuffix("/mcp")
            assert (await http.get(base + "/api/status")).json() == {"ready": True, "paused": False}
            assert (await http.post(base + "/api/decide", json={"state": "synthetic", "question": {}})).json() == {"synthetic": True}
            assert stats["legacy_calls"] == 1
            assert (await http.post(base + "/api/control", json={"action": "pause"})).status_code == 200
            assert (await http.get(base + "/api/status")).json() == {"ready": False, "paused": True}
            assert (await http.post(base + "/api/decide", json={})).status_code == 409
            assert (await http.post(base + "/api/control", json={"action": "install"})).status_code == 409
            assert (await http.post(base + "/api/control", json={"action": "resume", "slot": "other"})).status_code == 409
            assert calls == ["pause", "status"]
            assert (await http.post(base + "/api/control", json={"action": "resume"})).status_code == 200
            assert await sdk_call(public, "ready") == {"value": "ready"}


async def test_private_redirect_never_followed_or_replayed():
    count = 0
    def response(request):
        nonlocal count
        count += 1
        return httpx.Response(307, headers={"location": "http://127.0.0.1:9/secret"})
    private_client = httpx.AsyncClient(transport=httpx.MockTransport(response), follow_redirects=False)
    try:
        async with public_proxy("http://127.0.0.1:9/mcp", client=private_client) as (public, gateway), public_client() as http:
            result = await http.post(public, json=INIT, headers=public_headers())
            assert "The slot request could not be completed." in result.text
            assert count == 1
            assert not gateway.sessions
    finally:
        await private_client.aclose()


async def test_legacy_resume_uses_lifecycle_deadline_and_stays_bounded():
    canceled = asyncio.Event()
    async def control(action):
        assert action == "resume"
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            canceled.set()
            raise
        return {"ready": True, "paused": False}
    delay = 0.08
    async with public_proxy(None, legacy_jev=True, control=control, request_timeout=0.02,
                            control_timeout=0.2) as (public, gateway), public_client() as http:
        control_url = public.removesuffix("/mcp") + "/api/control"
        result = await http.post(control_url, json={"action": "resume"})
        assert result.status_code == 200
        assert result.json() == {"ready": True, "paused": False}
        assert not canceled.is_set()
        delay = 10
        result = await asyncio.wait_for(http.post(control_url, json={"action": "resume"}), 1)
        assert result.status_code == 409
        assert result.json() == {"error": {"code": "control_failed"}}
        assert canceled.is_set()


@pytest.mark.parametrize("control_timeout", [0, -1, True, float("inf"), float("nan")])
def test_lifecycle_deadline_requires_finite_positive_bound(control_timeout):
    with pytest.raises(ValueError, match="invalid_control_timeout"):
        make_http_app(token="public-key", port=19386, control_timeout=control_timeout)
