"""Credential-free MCP startup and live private-lease authorization journeys."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import plistlib
import secrets
import sys
import time
import uuid

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from meecp.authorization_client import AuthorizationError, SessionAuthorization, SessionCredential, verified_containing_app
from meecp.management import Management, serve_streams
from meecp.gateway import PROTOCOL_VERSION, encode
from meecp.private import ensure_private_directory, write_private_json
from meecp_local_transport import socket_path
from meecp import __main__ as cli
from test_meecp_management_cli import private_manager
from test_meecp_packages import make_package

ROOT = Path(__file__).resolve().parents[1]


async def test_real_sdk_credential_free_initialization_and_catalog_without_host(tmp_path):
    state = tmp_path / "absent"
    parameters = StdioServerParameters(command=sys.executable, args=["-P", "-m", "meecp", "--state-dir",
        str(state), "management", "connect", "--profile", str(uuid.uuid4())],
        env={"PYTHONPATH": str(ROOT / "backend")})
    errors = tmp_path / "stderr"
    with errors.open("w") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as client:
                started = time.monotonic()
                async with asyncio.timeout(2):
                    initialized = await client.initialize()
                    tools = await client.list_tools()
                    await client.send_ping()
                assert time.monotonic() - started < 2
                assert initialized.serverInfo.name == "meecp-management"
                assert len(tools.tools) == 45
                assert {"meecp_request_access", "meecp_get_authorization_status"} <= {tool.name for tool in tools.tools}
                denied = await client.call_tool("meecp_capabilities", {})
                assert denied.isError and denied.structuredContent["error"]["code"] in {
                    "authorization_required", "authorization_pending"}
                status = await client.call_tool("meecp_get_authorization_status", {})
                assert status.structuredContent["status"] in {"pending", "unavailable"}
    assert errors.read_text() == ""
    assert not state.exists()


@pytest.mark.parametrize("invalid", ["missing", "unsafe", "malformed"])
async def test_invalid_explicit_file_exits_before_native_authorization(tmp_path, invalid):
    credential = tmp_path / "private" / "grant.json"
    if invalid != "missing":
        write_private_json(credential, {"schema_version": 1, "grant_id": str(uuid.uuid4()),
            "token": secrets.token_urlsafe(32)} if invalid == "unsafe" else {"schema_version": 1})
        if invalid == "unsafe":
            credential.chmod(0o644)
    state = tmp_path / "absent"
    process = await asyncio.create_subprocess_exec(sys.executable, "-P", "-m", "meecp", "--state-dir", str(state),
        "--credential-file", str(credential), "management", "connect", env={**os.environ,
            "PYTHONPATH": str(ROOT / "backend")}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(encode({"jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "SDK", "version": "1"}}}) + b"\n"), 5)
        assert process.returncode == 2 and stdout == stderr == b""
        assert not state.exists()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_initialized_notification_is_required_before_any_bootstrap(tmp_path):
    class Observer:
        def __init__(self):
            self.calls = []
        def on_initialized(self, client):
            self.calls.append(client)
        async def close(self):
            pass
    authorization = Observer()
    wire = asyncio.StreamReader()
    output = []
    initialized = asyncio.Event()
    async def writer(value):
        output.append(json.loads(value))
        initialized.set()
    task = asyncio.create_task(serve_streams(Management(tmp_path, authorization=authorization), wire, writer))
    try:
        wire.feed_data(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}) + b"\n")
        wire.feed_data(encode({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "SDK", "version": "1"}}}) + b"\n")
        await asyncio.wait_for(initialized.wait(), 2)
        assert authorization.calls == []
        wire.feed_data(encode({"jsonrpc": "2.0", "method": "notifications/initialized"}) + b"\n")
        async with asyncio.timeout(2):
            while not authorization.calls:
                await asyncio.sleep(0.01)
        assert authorization.calls == [{"name": "SDK", "version": "1"}]
    finally:
        wire.feed_eof()
        await asyncio.wait_for(task, 2)


@asynccontextmanager
async def bootstrap(tmp_path):
    state = ensure_private_directory(tmp_path / "state")
    path = socket_path(state, "manager", create=True).parent / "authorization.sock"
    generation = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    grant_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    approved, disconnected = asyncio.Event(), asyncio.Event()
    frames = []
    async def serve(reader, writer):
        handed_off = False
        current = "pending"
        kind = "base"
        try:
            while line := await reader.readline():
                value = json.loads(line)
                frames.append(value)
                response = {"version": 1, "status": current, "request_id": request_id,
                            "generation": generation, "scopes": ["inspect", "plan"]}
                if value["type"] == "request_action":
                    current, kind, handed_off = "pending", value["kind"], False
                    response["status"] = current
                if value["type"] == "ack":
                    assert value["request_id"] == request_id and value["grant_id"] == grant_id
                    current = response["status"] = "approved"
                elif approved.is_set() and not handed_off:
                    response["handoff"] = {"request_id": request_id, "grant_id": grant_id, "token": token,
                        "expires_at": time.time() + 60, "kind": kind}
                    handed_off = True
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        finally:
            disconnected.set()
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_unix_server(serve, path=str(path))
    path.chmod(0o600)
    write_private_json(state / "authorization-bootstrap.json", {"schema_version": 1, "socket_path": str(path),
        "installation_id": str(uuid.uuid4()), "generation": generation, "supervisor_pid": os.getpid(),
        "manager_endpoint": "http://127.0.0.1:12345"})
    try:
        yield state, token, approved, disconnected, frames
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)
        path.parent.rmdir()


async def wait_status(authorization, wanted):
    async with asyncio.timeout(3):
        while authorization.status()["status"] != wanted:
            await asyncio.sleep(0.01)


async def test_private_acknowledged_token_never_reads_owner_and_eof_ends_lease(tmp_path):
    async with bootstrap(tmp_path) as (state, token, approved, disconnected, frames):
        calls = []
        async def caller(*args, **kwargs):
            calls.append((args, kwargs))
            return {"api_version": 1, "capabilities": []}
        authorization = SessionAuthorization(state, str(uuid.uuid4()), poll_interval=0.01, caller=caller)
        management = Management(state, authorization=authorization)
        management.initialized({"name": "SDK", "version": "1"})
        denied = await management.invoke("meecp_capabilities", {})
        assert denied.isError and not calls
        approved.set()
        await wait_status(authorization, "approved")
        assert not (state / "manager-connection.json").exists()
        assert any(frame["type"] == "ack" for frame in frames)
        assert token not in json.dumps(authorization.status())
        assert not (await management.invoke("meecp_capabilities", {})).isError
        assert calls[0][1]["token"] == token
        await management.close()
        await asyncio.wait_for(disconnected.wait(), 2)
        assert not authorization.credentials


@pytest.mark.parametrize("kind", ["preparation", "install", "upgrade"])
async def test_exact_action_requires_separate_request_and_explicit_invocation(tmp_path, kind):
    async with bootstrap(tmp_path) as (state, token, approved, disconnected, frames):
        calls = []
        async def caller(*args, **kwargs):
            calls.append(args[2])
            return {"operation_id": str(uuid.uuid4())}
        authorization = SessionAuthorization(state, str(uuid.uuid4()), poll_interval=0.01, caller=caller)
        management = Management(state, authorization=authorization)
        management.initialized({"name": "SDK", "version": "1"})
        approved.set()
        await wait_status(authorization, "approved")
        action = {"kind": kind, "plan_id": str(uuid.uuid4()), "plan_digest": "f" * 64}
        if kind == "preparation":
            action["idempotency_key"] = str(uuid.uuid4())
        arguments = ({key: value for key, value in action.items() if key != "kind"}
                     if kind == "preparation" else {"plan_id": action["plan_id"]})
        tool = "meecp_start_preparation" if kind == "preparation" else "meecp_apply_" + kind
        route = "/v1/preparations/start" if kind == "preparation" else "/v1/" + kind + "/apply"
        denied = await management.invoke(tool, arguments)
        assert denied.structuredContent["error"]["code"] == "authorization_required" and calls == []
        requested = await management.invoke("meecp_request_access", {"action": action})
        assert requested.structuredContent["status"] == "pending"
        pending = await management.invoke(tool, arguments)
        assert pending.structuredContent["error"]["code"] == "authorization_pending" and calls == []
        await wait_status(authorization, "approved")
        assert calls == []
        assert not (await management.invoke(tool, arguments)).isError
        assert calls == [route]
        changed = {**arguments, ("idempotency_key" if kind == "preparation" else "plan_id"): str(uuid.uuid4())}
        assert (await management.invoke(tool, changed)).structuredContent["error"]["code"] == "authorization_required"
        assert len(calls) == 1
        await management.close()


async def test_upgrade_does_not_reuse_install_authority_and_operation_reads_use_the_matching_grant(tmp_path):
    calls = []
    operation_ids = {kind: str(uuid.uuid4()) for kind in ("install", "upgrade")}
    async def caller(state, method, route, body, *, token, endpoint):
        calls.append((route, token))
        if route.endswith("/apply"):
            return {"operation_id": operation_ids[route.split("/")[2]]}
        return {"status": "completed"}
    authorization = SessionAuthorization(tmp_path, caller=caller)
    authorization.public = {"status": "approved"}
    authorization.credentials["base"] = SessionCredential("base" * 8, str(uuid.uuid4()), time.time() + 60,
        {"scopes": ["inspect", "plan"]})
    actions = {kind: {"kind": kind, "plan_id": str(uuid.uuid4()), "plan_digest": "a" * 64}
               for kind in ("install", "upgrade")}
    authorization.credentials["install"] = SessionCredential("install" * 8, str(uuid.uuid4()), time.time() + 60,
        {"action": actions["install"]})
    management = Management(tmp_path, authorization=authorization)
    denied = await management.invoke("meecp_apply_upgrade", {"plan_id": actions["upgrade"]["plan_id"]})
    assert denied.structuredContent["error"]["code"] == "authorization_required" and calls == []
    authorization.credentials["upgrade"] = SessionCredential("upgrade" * 8, str(uuid.uuid4()), time.time() + 60,
        {"action": actions["upgrade"]})
    for kind in ("upgrade", "install"):
        assert not (await management.invoke("meecp_apply_" + kind, {"plan_id": actions[kind]["plan_id"]})).isError
    for kind in ("upgrade", "install"):
        result = await management.invoke("meecp_get_operation", {"operation_id": operation_ids[kind]})
        assert not result.isError and calls[-1][1] == kind * 8
    authorization.credentials.pop("upgrade")
    result = await management.invoke("meecp_get_operation", {"operation_id": operation_ids["upgrade"]})
    assert not result.isError and calls[-1][1] == "base" * 8
    authorization.credentials["install"] = SessionCredential("replacement" * 8, str(uuid.uuid4()), time.time() + 60,
        {"action": {**actions["install"], "plan_id": str(uuid.uuid4())}})
    result = await management.invoke("meecp_get_operation", {"operation_id": operation_ids["install"]})
    assert not result.isError and calls[-1][1] == "base" * 8
    authorization.credentials.pop("base")
    before = len(calls)
    result = await management.invoke("meecp_get_operation", {"operation_id": operation_ids["install"]})
    assert result.structuredContent["error"]["code"] == "authorization_required" and len(calls) == before
    await management.close()


async def test_single_launch_attempt_is_bounded_despite_polling(tmp_path):
    launches = []
    async def launch():
        launches.append(True)
        return True
    authorization = SessionAuthorization(tmp_path / "absent", str(uuid.uuid4()), lifetime=0.05,
                                         poll_interval=0.005, launcher=launch)
    authorization.on_initialized({"name": "SDK", "version": "1"})
    await wait_status(authorization, "unavailable")
    for _ in range(5):
        assert authorization.status()["status"] == "unavailable"
    assert launches == [True]
    await authorization.request_access({})
    await wait_status(authorization, "unavailable")
    assert launches == [True]
    await authorization.close()


async def test_expired_session_and_missing_exact_credentials_never_use_owner(tmp_path):
    async def forbidden(*args, **kwargs):
        pytest.fail("expired_session_reached_manager")
    authorization = SessionAuthorization(tmp_path, caller=forbidden)
    authorization.credentials["base"] = SessionCredential("x" * 32, str(uuid.uuid4()), time.time() - 1,
                                                          {"scopes": ["inspect"]})
    management = Management(tmp_path, authorization=authorization, caller=forbidden)
    result = await management.invoke("meecp_capabilities", {})
    assert result.structuredContent["error"]["code"] == "authorization_expired"
    assert not authorization.credentials


async def test_exact_artifact_validation_has_a_separate_bounded_exchange_deadline(tmp_path):
    class Writer:
        def write(self, value):
            pass
        async def drain(self):
            pass
    class Reader:
        async def readline(self):
            await asyncio.sleep(0.04)
            return b'{"version":1,"status":"pending"}\n'
    authorization = SessionAuthorization(tmp_path, exchange_timeout=0.01, action_timeout=0.5)
    authorization.writer, authorization.reader = Writer(), Reader()
    authorization.deadline = time.monotonic() + 1
    assert (await authorization._exchange({"type": "request_action"}))["status"] == "pending"
    with pytest.raises(TimeoutError):
        await authorization._exchange({"type": "status"})
    authorization.deadline = time.monotonic() + 0.01
    with pytest.raises(TimeoutError):
        await authorization._exchange({"type": "request_action"})


async def test_proposal_switch_drains_delayed_status_before_action_handoff(tmp_path):
    blocked, release = asyncio.Event(), asyncio.Event()
    frames = []
    base_token, action_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    request_id, grant_id = str(uuid.uuid4()), str(uuid.uuid4())
    class Delayed(SessionAuthorization):
        async def _connect(self):
            self.writer = True
        async def _close_socket(self):
            self.writer = None
        async def _exchange(self, value):
            frames.append(value["type"])
            response = {"version": 1, "status": "approved", "request_id": request_id}
            if value["type"] == "status" and not blocked.is_set():
                blocked.set()
                await release.wait()
            if value["type"] in {"request_access", "request_action"}:
                kind = "base" if value["type"] == "request_access" else value["kind"]
                response.update(status="pending", handoff={"request_id": request_id, "grant_id": grant_id,
                    "kind": kind, "token": base_token if kind == "base" else action_token,
                    "expires_at": time.time() + 300})
            return response
    authorization = Delayed(tmp_path, str(uuid.uuid4()), poll_interval=0.001)
    authorization.on_initialized({"name": "SDK", "version": "1"})
    await wait_status(authorization, "approved")
    await asyncio.wait_for(blocked.wait(), 2)
    original_worker = authorization.task
    action = {"kind": "install", "plan_id": str(uuid.uuid4()), "plan_digest": "f" * 64}
    assert (await authorization.request_access({"action": action}))["status"] == "pending"
    assert authorization.task is original_worker and not original_worker.cancelled()
    assert "request_action" not in frames
    release.set()
    async with asyncio.timeout(2):
        while "install" not in authorization.credentials:
            await asyncio.sleep(0.01)
    assert frames[:5] == ["request_access", "ack", "status", "request_action", "ack"]
    assert authorization.credentials["base"].token == base_token
    assert authorization.credentials["install"].token == action_token
    await authorization.close()


async def test_app_launch_checks_containing_bundle_and_signature_before_open(tmp_path, monkeypatch):
    app = (tmp_path / "meecp.app").resolve()
    module = app / "Contents/Resources/host/backend/meecp/authorization_client.py"
    module.parent.mkdir(parents=True)
    module.write_text("# synthetic embedded bridge\n")
    binary = app / "Contents/MacOS/meecp"
    binary.parent.mkdir()
    binary.write_bytes(b"synthetic executable")
    binary.chmod(0o755)
    metadata = app / "Contents/Info.plist"
    info = {"CFBundleIdentifier": "com.jshahbazi.mcpme", "CFBundleExecutable": "meecp"}
    metadata.write_bytes(plistlib.dumps(info))
    (app / "Contents/Resources/host/native-app.json").write_text(json.dumps({"schema_version": 1,
        "bundle_identifier": "com.jshahbazi.mcpme", "native_executable": "../../MacOS/meecp"}))
    calls = []
    async def verify(arguments, timeout):
        calls.append((arguments, timeout))
        return True
    monkeypatch.setattr("meecp.authorization_client._quiet_process", verify)
    assert await verified_containing_app(app, module_path=module)
    assert calls == [(["/usr/bin/codesign", "--verify", "--strict", "--deep", str(app)], 15)]
    calls.clear()
    assert not await verified_containing_app(app, module_path=__file__)
    assert calls == []
    metadata.write_bytes(plistlib.dumps({**info, "CFBundleIdentifier": "org.example.other"}))
    assert not await verified_containing_app(app, module_path=module)
    assert calls == []
    metadata.write_bytes(plistlib.dumps(info))
    binary.unlink()
    binary.symlink_to(module)
    assert not await verified_containing_app(app, module_path=module)
    assert calls == []


async def test_real_sdk_pending_owner_approval_and_disconnect_revoke(tmp_path):
    async with private_manager(tmp_path) as host:
        endpoint = cli.read_private_json(host.root / "manager-connection.json")["endpoint"]
        await host.authorization.start(endpoint)
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        parameters = StdioServerParameters(command=sys.executable, args=["-P", "-m", "meecp", "--state-dir",
            str(host.root), "management", "connect", "--profile", profile],
            env={"PYTHONPATH": str(ROOT / "backend")})
        with (tmp_path / "stderr").open("w") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(read, write) as client:
                    started = time.monotonic()
                    async with asyncio.timeout(2):
                        await client.initialize()
                        assert len((await client.list_tools()).tools) == 45
                        await client.send_ping()
                    assert time.monotonic() - started < 2
                    async with asyncio.timeout(2):
                        while not host.authorization.list_requests()["requests"]:
                            await asyncio.sleep(0.01)
                    request = host.authorization.list_requests()["requests"][0]
                    assert (await client.call_tool("meecp_capabilities", {})).isError
                    host.authorization.approve(request["request_id"], {"remember": False,
                        "account_scoped_consent": False, "scopes": request["scopes"],
                        **{key: request[key] for key in ("scope_digest", "policy_revision", "generation")}})
                    async with asyncio.timeout(3):
                        while (await client.call_tool("meecp_get_authorization_status", {})).structuredContent["status"] != "approved":
                            await asyncio.sleep(0.01)
                    assert not (await client.call_tool("meecp_capabilities", {})).isError
                    session = host.authorization.list_sessions()["sessions"][0]["session_id"]
                    assert host.authorization.credentials[session]["active"] == 1
        async with asyncio.timeout(2):
            while host.authorization.credentials[session]["active"]:
                await asyncio.sleep(0.01)
        assert (tmp_path / "stderr").read_text() == ""


async def test_live_broker_approval_remember_reconnect_revoke_and_rotation(tmp_path):
    async with private_manager(tmp_path) as host:
        endpoint = cli.read_private_json(host.root / "manager-connection.json")["endpoint"]
        await host.authorization.start(endpoint)
        exported = await cli.execute(cli.parser().parse_args(["--state-dir", str(host.root), "management", "export"]))
        profile = exported["profile_id"]
        authorization = SessionAuthorization(host.root, profile, poll_interval=0.01)
        management = Management(host.root, authorization=authorization)
        management.initialized({"name": "SDK", "version": "1"})
        async with asyncio.timeout(2):
            while not host.authorization.list_requests()["requests"]:
                await asyncio.sleep(0.01)
        request = host.authorization.list_requests()["requests"][0]
        denied = await management.invoke("meecp_capabilities", {})
        assert denied.isError
        await cli.call(host.root, "POST", "/v1/authorization/requests/" + request["request_id"] + "/approve", {
            "remember": True, "account_scoped_consent": True, "scopes": ["inspect", "plan"],
            "scope_digest": request["scope_digest"], "policy_revision": request["policy_revision"],
            "generation": request["generation"]})
        await wait_status(authorization, "approved")
        first = authorization.credentials["base"].token
        assert not (await management.invoke("meecp_capabilities", {})).isError
        # The client's renewal threshold rotates through the live broker ACK
        # protocol while retaining the exact remembered scope.
        old_grant = authorization.credentials["base"].grant_id
        authorization.credentials["base"].expires_at = time.time() + 30
        async with asyncio.timeout(2):
            while authorization.credentials["base"].grant_id == old_grant:
                await asyncio.sleep(0.01)
        assert authorization.credentials["base"].token != first
        assert host.authorization.credentials[old_grant]["active"] == 0
        assert not (await management.invoke("meecp_capabilities", {})).isError
        await management.close()
        reconnect = SessionAuthorization(host.root, profile, poll_interval=0.01)
        reconnected = Management(host.root, authorization=reconnect)
        reconnected.initialized({"name": "A different reported name", "version": "1"})
        await wait_status(reconnect, "approved")
        assert reconnect.credentials["base"].token != first
        assert not host.authorization.list_requests()["requests"]
        assert not (await reconnected.invoke("meecp_capabilities", {})).isError
        await cli.call(host.root, "POST", "/v1/authorization/profiles/" + profile + "/revoke", {})
        assert (await reconnected.invoke("meecp_capabilities", {})).isError
        await reconnected.close()


async def test_live_exact_denial_expiry_base_renewal_and_explicit_install(tmp_path):
    package = make_package(tmp_path / "package")
    async with private_manager(tmp_path) as host:
        endpoint = cli.read_private_json(host.root / "manager-connection.json")["endpoint"]
        await host.authorization.start(endpoint)
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        authorization = SessionAuthorization(host.root, profile, poll_interval=0.01)
        management = Management(host.root, authorization=authorization)
        management.initialized({"name": "SDK", "version": "1"})
        async def pending():
            async with asyncio.timeout(2):
                while not host.authorization.list_requests()["requests"]:
                    await asyncio.sleep(0.01)
            return host.authorization.list_requests()["requests"][0]
        def approve(request, *, remember=False):
            host.authorization.approve(request["request_id"], {"remember": remember,
                "account_scoped_consent": remember, "scopes": request["scopes"],
                **{key: request[key] for key in ("scope_digest", "policy_revision", "generation")}})
        approve(await pending(), remember=True)
        await wait_status(authorization, "approved")
        first_base = authorization.credentials["base"].grant_id
        plan = host.plan_install({"package": str(package)})
        action = {"kind": "install", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"]}
        assert (await management.invoke("meecp_apply_install", {"plan_id": plan["plan_id"]})).isError
        await management.invoke("meecp_request_access", {"action": action})
        request = await pending()
        assert request["kind"] == "install"
        host.authorization.owner_route("POST", "/v1/authorization/requests/" + request["request_id"] + "/deny", {})
        await wait_status(authorization, "denied")
        assert authorization.credentials["base"].grant_id == first_base
        assert not (await management.invoke("meecp_capabilities", {})).isError
        await management.invoke("meecp_request_access", {"action": action})
        request = await pending()
        host.authorization.requests[request["request_id"]]["expires_at"] = time.time() - 1
        await wait_status(authorization, "expired")
        assert authorization.credentials["base"].grant_id == first_base
        assert not (await management.invoke("meecp_capabilities", {})).isError
        authorization.credentials["base"].expires_at = time.time() + 30
        async with asyncio.timeout(2):
            while authorization.credentials["base"].grant_id == first_base:
                await asyncio.sleep(0.01)
        assert host.authorization.credentials[first_base]["active"] == 0
        await management.invoke("meecp_request_access", {"action": action})
        approve(await pending())
        await wait_status(authorization, "approved")
        assert len(host.registry.list_operations()) == 0
        result = await management.invoke("meecp_apply_install", {"plan_id": plan["plan_id"]})
        assert not result.isError and "operation_id" in result.structuredContent
        assert len(host.registry.list_operations()) == 1
        changed = await management.invoke("meecp_apply_install", {"plan_id": str(uuid.uuid4())})
        assert changed.structuredContent["error"]["code"] == "authorization_required"
        assert len(host.registry.list_operations()) == 1
        grants = [credential.grant_id for credential in authorization.credentials.values()]
        await management.close()
        async with asyncio.timeout(2):
            while any(host.authorization.credentials[grant]["active"] for grant in grants):
                await asyncio.sleep(0.01)


async def test_owner_reduced_scopes_require_new_consent_for_expansion(tmp_path):
    async with private_manager(tmp_path) as host:
        await host.authorization.start(cli.read_private_json(host.root / "manager-connection.json")["endpoint"])
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        authorization = SessionAuthorization(host.root, profile, poll_interval=0.01)
        management = Management(host.root, authorization=authorization)
        management.initialized({"name": "SDK", "version": "1"})
        async def request():
            async with asyncio.timeout(2):
                while not host.authorization.list_requests()["requests"]:
                    await asyncio.sleep(0.01)
            return host.authorization.list_requests()["requests"][0]
        def approve(value, selected):
            host.authorization.approve(value["request_id"], {"remember": True, "account_scoped_consent": True,
                "scopes": selected, **{key: value[key] for key in ("scope_digest", "policy_revision", "generation")}})
        approve(await request(), ["inspect"])
        await wait_status(authorization, "approved")
        original = authorization.credentials["base"].grant_id
        assert authorization.credentials["base"].proposal == {"scopes": ["inspect"]}
        result = await management.invoke("meecp_request_access", {"scopes": ["plan", "inspect"]})
        assert result.structuredContent["status"] == "pending"
        pending = await request()
        assert pending["scopes"] == ["inspect", "plan"] and pending["policy_revision"] == 1
        assert authorization.credentials["base"].grant_id == original
        assert not (await management.invoke("meecp_capabilities", {})).isError
        approve(pending, ["inspect", "plan"])
        await wait_status(authorization, "approved")
        assert authorization.credentials["base"].grant_id != original
        assert authorization.credentials["base"].proposal == {"scopes": ["inspect", "plan"]}
        assert host.authorization.credentials[original]["active"] == 0
        await management.close()


async def test_expired_base_scope_replacement_preserves_prior_live_ceiling(tmp_path):
    async with private_manager(tmp_path) as host:
        await host.authorization.start(cli.read_private_json(host.root / "manager-connection.json")["endpoint"])
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        authorization = SessionAuthorization(host.root, profile, poll_interval=0.01)
        management = Management(host.root, authorization=authorization)
        management.initialized({"name": "SDK", "version": "1"})
        async def request():
            async with asyncio.timeout(2):
                while not host.authorization.list_requests()["requests"]:
                    await asyncio.sleep(0.01)
            return host.authorization.list_requests()["requests"][0]
        initial = await request()
        host.authorization.approve(initial["request_id"], {"remember": True, "account_scoped_consent": True,
            "scopes": ["inspect"], **{key: initial[key] for key in ("scope_digest", "policy_revision", "generation")}})
        await wait_status(authorization, "approved")
        original = authorization.credentials["base"].grant_id
        await management.invoke("meecp_request_access", {"scopes": ["inspect", "plan"]})
        replacement = await request()
        host.authorization.requests[replacement["request_id"]]["expires_at"] = time.time() - 1
        await wait_status(authorization, "expired")
        assert authorization.status()["base_status"] == "approved"
        assert authorization.credentials["base"].grant_id == original
        assert authorization.credentials["base"].proposal == {"scopes": ["inspect"]}
        assert host.authorization.credentials[original]["active"] == 1
        assert not (await management.invoke("meecp_capabilities", {})).isError
        denied = await management.invoke("meecp_plan_install", {"package": str(tmp_path / "unavailable")})
        assert denied.structuredContent["error"]["code"] == "grant_scope"
        assert host.authorization.profiles[profile]["scopes"] == ["inspect"]
        await management.close()


@pytest.mark.parametrize("arguments", [{"request_id": str(uuid.uuid4())}, {"scopes": ["owner"]},
    {"scopes": ["inspect"], "action": {"kind": "install", "plan_id": str(uuid.uuid4()), "plan_digest": "f" * 64}},
    {"action": {"kind": "preparation", "plan_id": str(uuid.uuid4()), "plan_digest": "f" * 64}}])
async def test_authorization_tools_reject_unknown_authority_and_unbound_actions(tmp_path, arguments):
    result = await Management(tmp_path).invoke("meecp_request_access", arguments)
    assert result.structuredContent == {"api_version": 1, "error": {"code": "invalid_tool_arguments"}}
