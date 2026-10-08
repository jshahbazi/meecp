"""Private, connection-bound authorization for the management stdio facade.

Only bootstrap discovery is read here. Owner credentials are never read, and
session bearers never leave this process except in private manager requests.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import socket
import stat
import struct
import time
import uuid

import httpx
from meecp_local_transport import checked_socket, private_transport, socket_path

from .policy import load_json
from .private import read_private_json

STATUSES = frozenset({"not_requested", "pending", "approved", "denied", "expired", "revoked", "unavailable"})
DEFAULT_SCOPES = ("inspect", "plan")
MAX_FRAME = 16384


class AuthorizationError(ValueError):
    def __init__(self, code: str, status: dict | None = None):
        self.code, self.status = code, status or {}
        super().__init__(code)


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _label(value, limit):
    return "".join(character for character in value if character.isprintable())[:limit] if isinstance(value, str) else ""


def check_peer(peer, expected_pid: int):
    """Check the connected socket, separately from filesystem permissions."""
    if hasattr(socket, "SO_PEERCRED"):
        pid, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid() or pid != expected_pid:
            raise AuthorizationError("authorization_unavailable")
    else:
        # Darwin LOCAL_PEERCRED uses xucred (version, uid, group count, groups).
        credentials = peer.getsockopt(0, 1, 80)
        version, uid = struct.unpack_from("II", credentials)
        if version != 0 or uid != os.getuid():
            raise AuthorizationError("authorization_unavailable")
        # LOCAL_PEERPID is available for connected macOS Unix stream sockets.
        pid = struct.unpack("i", peer.getsockopt(0, 2, 4))[0]
        if pid != expected_pid:
            raise AuthorizationError("authorization_unavailable")


def manager_endpoint(value):
    from .__main__ import endpoint
    return endpoint({"endpoint": value})


async def _quiet_process(arguments, timeout):
    process = await asyncio.create_subprocess_exec(*arguments,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            return await process.wait() == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def verified_containing_app(app: Path, *, module_path=None):
    """Launch only the sealed app containing this exact embedded bridge."""
    try:
        if app.suffix != ".app" or app.resolve(strict=True) != app or not app.is_dir():
            return False
        embedded = app / "Contents/Resources/host/backend/meecp/authorization_client.py"
        if Path(module_path or __file__).resolve(strict=True) != embedded.resolve(strict=True):
            return False
        for relative in ("Contents", "Contents/MacOS", "Contents/Resources", "Contents/Resources/host"):
            path = app / relative
            if path.resolve(strict=True) != path or not path.is_dir():
                return False
        binary = app / "Contents/MacOS/meecp"
        metadata = app / "Contents/Info.plist"
        binding = app / "Contents/Resources/host/native-app.json"
        for path in (binary, metadata, binding):
            details = path.lstat()
            if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                    or path.resolve(strict=True) != path):
                return False
        if not binary.stat().st_mode & 0o111 or metadata.stat().st_size > 65536 or binding.stat().st_size > 4096:
            return False
        info = plistlib.loads(metadata.read_bytes())
        receipt = load_json(binding.read_bytes())
        if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != "com.jshahbazi.mcpme"
                or info.get("CFBundleExecutable") != "meecp" or not isinstance(receipt, dict)
                or receipt.get("schema_version") != 1 or receipt.get("bundle_identifier") != "com.jshahbazi.mcpme"
                or receipt.get("native_executable") != "../../MacOS/meecp"):
            return False
        return await _quiet_process(["/usr/bin/codesign", "--verify", "--strict", "--deep", str(app)], 15)
    except (OSError, ValueError, TypeError, TimeoutError):
        return False


async def session_call(state: Path, method: str, route: str, body: dict | None, *, token: str, endpoint: str):
    if not isinstance(token, str) or not token:
        raise AuthorizationError("authorization_required")
    timeout = 300 if route in {"/v1/install/plan", "/v1/upgrade/plan", "/v1/upgrade/apply", "/v1/imports", "/v1/preparations/plan",
        "/v1/preparations/inspect-source", "/v1/resolutions/plan"} or route.endswith(("/attach", "/engine")) else 30
    async with httpx.AsyncClient(base_url=manager_endpoint(endpoint), timeout=timeout, follow_redirects=False,
            trust_env=False, transport=private_transport(socket_path(state, "manager")),
            headers={"Authorization": "Bearer " + token}) as client:
        async with client.stream(method, route, json=body) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise ValueError("response_limit")
            value = load_json(bytes(data))
    if not isinstance(value, dict):
        raise ValueError("invalid_response")
    return value


@dataclass
class SessionCredential:
    token: str
    grant_id: str
    expires_at: float
    proposal: dict


class SessionAuthorization:
    """A single bootstrap lease; polling can never recover another lease."""
    def __init__(self, state: Path, profile: str | None = None, *, lifetime=120, poll_interval=0.5,
                 launcher=None, caller=session_call, exchange_timeout=5, action_timeout=60):
        self.state, self.profile = state, profile
        self.lifetime, self.poll_interval = lifetime, poll_interval
        self.launcher, self.caller = launcher, caller
        self.exchange_timeout, self.action_timeout = exchange_timeout, action_timeout
        self.client = {"name": "MCP client", "version": ""}
        self.initialized = False
        self.public = {"status": "not_requested"}
        self.credentials: dict[str, SessionCredential] = {}
        self.operation_credentials: dict[str, tuple[str, str]] = {}
        self.reader = self.writer = None
        self.endpoint = None
        self.generation = None
        self.task = None
        self.lock = asyncio.Lock()
        self.launch_attempted = False
        self.deadline = 0.0
        self.proposal = None
        self.next_proposal = None
        self.wakeup = asyncio.Event()

    def on_initialized(self, client):
        if self.initialized:
            return
        self.initialized = True
        self.client = {"name": _label(client.get("name"), 128) or "MCP client",
                       "version": _label(client.get("version"), 64) or "unknown"}
        if self.profile is not None:
            self.begin({"scopes": list(DEFAULT_SCOPES)})

    def begin(self, proposal):
        if not self.initialized:
            raise AuthorizationError("authorization_required", self.status())
        if self.task and not self.task.done():
            if self.proposal == proposal and self.public["status"] in {"pending", "approved"}:
                return
            if self.public["status"] == "pending":
                raise AuthorizationError("authorization_pending", self.status())
            # One worker drains every response before switching proposals;
            # cancelling a read could associate its reply with the next ACK.
            self.proposal = dict(proposal)
            self.next_proposal = dict(proposal)
            self.public = {"status": "pending"}
            self.deadline = time.monotonic() + self.lifetime
            self.wakeup.set()
            return
        self.proposal = dict(proposal)
        self.public = {"status": "pending"}
        self.deadline = time.monotonic() + self.lifetime
        self.task = asyncio.create_task(self._run(dict(proposal)))

    async def request_access(self, proposal):
        if "action" in proposal:
            if "base" not in self.credentials:
                raise AuthorizationError("authorization_required", self.status())
            self.begin({"action": dict(proposal["action"])})
        else:
            scopes = sorted(proposal.get("scopes", list(DEFAULT_SCOPES)))
            base = self.credentials.get("base")
            if base and base.proposal.get("scopes") == scopes and base.expires_at > time.time():
                return self.status()
            self.begin({"scopes": scopes})
        return self.status()

    def status(self):
        return {"api_version": 1, **self.public}

    async def _launch(self):
        if self.launch_attempted:
            return False
        self.launch_attempted = True
        if self.launcher is not None:
            return await self.launcher()
        from .__main__ import DEFAULT_STATE, bundled_app
        app = bundled_app()
        if (app is None or self.state.resolve() != DEFAULT_STATE.resolve()
                or not await verified_containing_app(app)):
            return False
        return await _quiet_process(["/usr/bin/open", "-g", str(app)], 5)

    async def _connect(self):
        discovery = read_private_json(self.state / "authorization-bootstrap.json", max_bytes=4096)
        if (discovery.get("schema_version") != 1 or not _uuid(discovery.get("installation_id"))
                or not isinstance(discovery.get("generation"), str)
                or type(discovery.get("supervisor_pid")) is not int or discovery["supervisor_pid"] <= 0):
            raise AuthorizationError("authorization_unavailable")
        path = Path(discovery["socket_path"])
        # Discovery cannot redirect to a different private state's transport.
        if path.parent != socket_path(self.state, "manager").parent or path.name != "authorization.sock":
            raise AuthorizationError("authorization_unavailable")
        reader, writer = await asyncio.open_unix_connection(checked_socket(path), limit=MAX_FRAME)
        try:
            check_peer(writer.get_extra_info("socket"), discovery["supervisor_pid"])
            endpoint = manager_endpoint(discovery["manager_endpoint"])
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
        self.reader, self.writer, self.endpoint = reader, writer, endpoint
        self.generation = discovery["generation"]

    async def _exchange(self, value):
        import json
        # An exact installation proposal verifies immutable package artifacts in
        # the host. Keep that work asynchronous to MCP startup and tool replies,
        # but allow a bounded artifact read beyond the short control deadline.
        timeout = self.exchange_timeout
        if value.get("type") == "request_action":
            timeout = min(self.action_timeout, max(0, self.deadline - time.monotonic()))
        async with self.lock, asyncio.timeout(timeout):
            self.writer.write(json.dumps({"version": 1, **value}, separators=(",", ":")).encode() + b"\n")
            await self.writer.drain()
            line = await self.reader.readline()
            if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
                raise AuthorizationError("authorization_unavailable")
            response = load_json(line)
            if (not isinstance(response, dict) or response.get("version") != 1
                    or response.get("status") not in STATUSES
                    or response.get("generation", self.generation) != self.generation):
                raise AuthorizationError("authorization_unavailable")
            return response

    async def _accept(self, response, proposal):
        status = response["status"]
        public = {"status": status}
        for key in ("request_id", "scope_digest", "generation", "identity_mode", "expires_at", "scopes",
                    "policy_revision", "selected_scopes", "kind", "base_status"):
            if key in response:
                public[key] = response[key]
        # A status already in flight describes the previous proposal. Drain it
        # without publishing approval for a newly queued action.
        if self.next_proposal is None:
            self.public = public
        handoff = response.get("handoff")
        if handoff is not None:
            kind = handoff.get("kind") if isinstance(handoff, dict) else None
            if (kind not in {"base", "preparation", "resolution", "install", "upgrade"}
                    or not _uuid(handoff.get("request_id")) or not _uuid(handoff.get("grant_id"))
                    or not isinstance(handoff.get("token"), str) or not 32 <= len(handoff["token"]) <= 256
                    or type(handoff.get("expires_at")) not in {int, float}
                    or handoff["expires_at"] <= time.time()
                    or handoff["request_id"] != response.get("request_id")):
                raise AuthorizationError("authorization_unavailable")
            expected_kind = proposal.get("action", {}).get("kind", "base")
            if kind != expected_kind:
                raise AuthorizationError("authorization_unavailable")
            credential = SessionCredential(handoff["token"], handoff["grant_id"], handoff["expires_at"], proposal)
            acknowledged = await self._exchange({"type": "ack", "request_id": handoff["request_id"],
                                                  "grant_id": handoff["grant_id"]})
            if (acknowledged.get("status") != "approved"
                    or acknowledged.get("request_id") != handoff["request_id"]):
                raise AuthorizationError("authorization_unavailable")
            if kind == "base":
                credential.proposal = {"scopes": acknowledged.get("selected_scopes", proposal["scopes"])}
                if self.next_proposal is None:
                    self.proposal = dict(credential.proposal)
            self.credentials[kind] = credential
            await self._accept(acknowledged, proposal)
        elif status in {"revoked", "expired", "unavailable"}:
            if (response.get("base_status") == "approved" and status != "unavailable"
                    and ("action" in proposal or status == "expired")):
                if "action" in proposal:
                    self.credentials.pop(proposal["action"]["kind"], None)
            else:
                self.credentials.clear()

    async def _run(self, proposal):
        try:
            while self.writer is None:
                try:
                    await self._connect()
                except Exception:
                    if not self.launch_attempted and not await self._launch():
                        self.public = {"status": "unavailable", "instruction": "Open meecp and request access again."}
                        return
                    if time.monotonic() >= self.deadline:
                        self.public = {"status": "unavailable", "instruction": "Open meecp and request access again."}
                        return
                    await asyncio.sleep(self.poll_interval)
            await self._accept(await self._exchange(self._request_frame(proposal)), proposal)
            while True:
                if self.next_proposal is not None:
                    proposal, self.next_proposal = self.next_proposal, None
                    self.wakeup.clear()
                    await self._accept(await self._exchange(self._request_frame(proposal)), proposal)
                    continue
                base = self.credentials.get("base")
                if (self.public["status"] != "pending" and self.profile is not None and base is not None
                        and self.public.get("policy_revision", 0) > 0
                        and base.expires_at - time.time() < 60):
                    # Renewal rotates only the existing remembered scope and
                    # still requires a delivered, acknowledged fresh bearer.
                    proposal = dict(base.proposal)
                    self.proposal = dict(proposal)
                    self.deadline = time.monotonic() + self.lifetime
                    await self._accept(await self._exchange({"type": "renew"}), proposal)
                    continue
                if self.public["status"] not in {"pending", "approved"} and base is None:
                    await self._close_socket()
                    return
                if self.public["status"] == "pending" and time.monotonic() >= self.deadline:
                    # Settle through the broker so an expired exact request can
                    # preserve its separately approved base session.
                    await self._accept(await self._exchange({"type": "status"}), proposal)
                    if self.public["status"] == "pending":
                        self.public = {"status": "expired"}
                        self.credentials.clear()
                        await self._close_socket()
                        return
                try:
                    await asyncio.wait_for(self.wakeup.wait(), self.poll_interval)
                except TimeoutError:
                    await self._accept(await self._exchange({"type": "status"}), proposal)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.public = {"status": "unavailable", "instruction": "Open meecp and request access again."}
            self.credentials.clear()
            await self._close_socket()

    def _request_frame(self, proposal):
        if "action" in proposal:
            return {"type": "request_action", **proposal["action"]}
        return {"type": "request_access", "profile_id": self.profile,
                "client": self.client, "scopes": proposal["scopes"]}

    async def invoke(self, name, arguments, method, route, body):
        kind = "base"
        required = {"meecp_start_preparation": "preparation", "meecp_start_resolution": "resolution",
                    "meecp_apply_install": "install", "meecp_apply_upgrade": "upgrade"}.get(name)
        if required:
            credential = self.credentials.get(required)
            action = credential.proposal.get("action", {}) if credential else {}
            keys = ("plan_id",) if required in {"install", "upgrade"} else ("plan_id", "plan_digest", "idempotency_key")
            if not credential or any(action.get(key) != arguments.get(key) for key in keys):
                code = "authorization_pending" if self.public["status"] == "pending" else "authorization_required"
                raise AuthorizationError(code, self.status())
            kind = required
        else:
            for action_kind in ("preparation", "resolution"):
                if name in {"meecp_get_" + action_kind, "meecp_cancel_" + action_kind,
                            "meecp_list_" + action_kind + "s"} and action_kind in self.credentials:
                    kind = action_kind
            if name == "meecp_get_operation":
                binding = self.operation_credentials.get(arguments.get("operation_id"))
                if binding is not None:
                    action_kind, grant_id = binding
                    active = self.credentials.get(action_kind)
                    if active is not None and active.grant_id == grant_id:
                        kind = action_kind
        credential = self.credentials.get(kind)
        if not credential:
            code = "authorization_pending" if self.public["status"] == "pending" else "authorization_required"
            raise AuthorizationError(code, self.status())
        if credential.expires_at <= time.time():
            self.credentials.pop(kind, None)
            raise AuthorizationError("authorization_expired", {"status": "expired"})
        value = await self.caller(self.state, method, route, body, token=credential.token, endpoint=self.endpoint)
        if required in {"install", "upgrade"} and isinstance(value, dict) and "error" not in value:
            operation_id = value.get("operation_id")
            if _uuid(operation_id):
                self.operation_credentials[operation_id] = (required, credential.grant_id)
        return value

    async def _close_socket(self):
        writer, self.writer, self.reader = self.writer, None, None
        if writer:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.credentials.clear()
        self.operation_credentials.clear()
        await self._close_socket()
