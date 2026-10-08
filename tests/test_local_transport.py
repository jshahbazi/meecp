"""Private transport controls and live TCP-impersonator regressions."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import stat
import uuid

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from jevk5_app import bridge as legacy
from jevk5_app.contracts import ServiceError
from meecp_local_transport import bind_private_listener, checked_socket, private_transport, socket_path
from meecp.__main__ import call
from meecp.bridge import Bridge
from meecp.policy import LocalPolicy
from meecp.private import write_private_json


@pytest.fixture
def private_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    yield state
    directory = socket_path(state, "manager", create=True).parent
    directory.chmod(0o700)
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()


@asynccontextmanager
async def impostor():
    received = []
    async def handle(reader, writer):
        received.append(await reader.read(4096))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", received
    finally:
        server.close()
        await server.wait_closed()


async def test_stopped_slot_bridge_never_sends_to_tcp_impostor(private_state):
    service = str(uuid.uuid4())
    listener = bind_private_listener(private_state, service)
    listener.close()  # Stale owner-controlled socket; public port is now hostile.
    async with impostor() as (endpoint, received):
        bridge = Bridge(endpoint + "/mcp", "synthetic-invocation-token",
                        socket_path=socket_path(private_state, service), timeout=1)
        stdin = asyncio.StreamReader()
        stdin.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        output = []
        async def write(data):
            output.append(data)
        await asyncio.wait_for(bridge.run(stdin, write), 2)
        assert len(output) == 1 and b'"error"' in output[0]
        assert received == []
        # A forgotten private transport is also an error, never an HTTP default.
        with pytest.raises(ValueError, match="private_transport_required"):
            await Bridge(endpoint + "/mcp", "synthetic-token").run(stdin, write)
        assert received == []


async def test_manager_and_legacy_requests_never_send_to_tcp_impostor(private_state, monkeypatch):
    for name in ("manager", "jevk5"):
        bind_private_listener(private_state, name).close()
    async with impostor() as (endpoint, received):
        write_private_json(private_state / "manager-connection.json", {"endpoint": endpoint, "token": "synthetic-manager-token"})
        with pytest.raises(httpx.ConnectError):
            await call(private_state, "GET", "/v1/capabilities")
        monkeypatch.setattr(legacy, "ENDPOINT", endpoint)
        write_private_json(private_state / "connection.json", {"endpoint": endpoint, "token": "synthetic-legacy-token"})
        with pytest.raises(ServiceError) as caught:
            await legacy.Bridge(private_state, Path("unused.app")).request("/api/decide", {"state": "synthetic evidence"})
        assert caught.value.code == "app_unavailable"
        assert received == []


async def test_owner_socket_preserves_manager_legacy_policy_and_payload(private_state, monkeypatch):
    listener = bind_private_listener(private_state, "manager")
    # Legacy client uses a separate alias of the same authenticated application.
    legacy_listener = bind_private_listener(private_state, "jevk5")
    async with impostor() as (endpoint, received):
        port = int(endpoint.rsplit(":", 1)[1])
        token = "synthetic-private-token"
        async def reply(request):
            return JSONResponse({"api_version": 1, "payload": await request.json() if request.method == "POST" else None})
        app = LocalPolicy(Starlette(routes=[Route("/v1/capabilities", reply), Route("/api/decide", reply, methods=["POST"])]), token, port)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="critical"))
        task = asyncio.create_task(server.serve(sockets=[listener, legacy_listener]))
        try:
            async with asyncio.timeout(2):
                while not server.started:
                    await asyncio.sleep(0.01)
            write_private_json(private_state / "manager-connection.json", {"endpoint": endpoint, "token": token})
            assert await call(private_state, "GET", "/v1/capabilities") == {"api_version": 1, "payload": None}
            monkeypatch.setattr(legacy, "ENDPOINT", endpoint)
            write_private_json(private_state / "connection.json", {"endpoint": endpoint, "token": token})
            assert (await legacy.Bridge(private_state, Path("unused.app")).request("/api/decide", {"state": "synthetic evidence"}))["payload"] == {"state": "synthetic evidence"}
            async with httpx.AsyncClient(transport=private_transport(socket_path(private_state, "manager")), trust_env=False) as client:
                assert (await client.get(endpoint + "/v1/capabilities")).status_code == 401
                assert (await client.get(endpoint + "/v1/capabilities", headers={"Authorization": "Bearer " + token, "Origin": "http://example.com"})).status_code == 403
            assert received == []
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            listener.close()
            legacy_listener.close()


def test_private_socket_rejects_permissions_symlinks_and_foreign_owner(private_state, monkeypatch):
    listener = bind_private_listener(private_state, "manager")
    path = socket_path(private_state, "manager")
    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert len(str(path).encode()) < 104
        path.chmod(0o666)
        with pytest.raises(ValueError, match="unsafe_private_transport"):
            private_transport(path)
        path.chmod(0o600)
        path.parent.chmod(0o755)
        with pytest.raises(ValueError, match="unsafe_private_transport"):
            socket_path(private_state, "manager")
        path.parent.chmod(0o700)
        actual = path.with_name("actual.sock")
        path.rename(actual)
        path.symlink_to(actual)
        with pytest.raises(ValueError, match="unsafe_private_transport"):
            private_transport(path)
        path.unlink()
        actual.rename(path)
        # Model a different UID in the metadata check without needing privileges.
        monkeypatch.setattr("meecp_local_transport.os.getuid", lambda: path.lstat().st_uid + 1)
        with pytest.raises(ValueError, match="unsafe_private_transport"):
            checked_socket(path)
    finally:
        listener.close()
        monkeypatch.undo()
