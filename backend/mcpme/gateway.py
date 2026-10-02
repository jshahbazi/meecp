"""Tools-only MCP transport. Package process ownership stays in the supervisor."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.types import (
    CallToolRequestParams, CallToolResult, CancelledNotificationParams,
    InitializeRequestParams, InitializeResult, JSONRPCMessage, ListToolsRequest,
    ProgressNotificationParams,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .policy import LocalPolicy, load_json
from .activity import Activity, RequestActivity
from .limits import DEFAULT_REQUEST_BYTES, MAX_REQUEST_BYTES

PROTOCOL_VERSION = "2025-11-25"
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_OPERATION_BYTES = 8 * MAX_MESSAGE_BYTES
MAX_EVENTS = 256
MAX_PENDING = 16


def request_id_digest(request_id: int | str) -> bytes:
    # Bound lifetime duplicate tracking without changing wire IDs or the pending
    # response map. Include type: JSON-RPC integer 1 and string "1" are distinct.
    return hashlib.sha256(encode(["int" if type(request_id) is int else "str", request_id])).digest()


@dataclass(frozen=True)
class SessionPipes:
    instance_id: str
    stdout_fd: int
    stdin_fd: int


@dataclass
class Event:
    sequence: int
    stream: str
    data: bytes | None
    terminal: bool = False


@dataclass
class Pending:
    request_id: str | int
    method: str
    stream: str
    progress_token: Any = None
    output_bytes: int = 0
    deadline: asyncio.Task | None = None
    activity: RequestActivity | None = None


def encode(message: dict) -> bytes:
    import json
    return json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def error_message(request_id, code=-32000, message="The slot request could not be completed."):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


@dataclass
class Session:
    session_id: str
    pipes: SessionPipes | None
    gateway: Gateway
    reader: asyncio.StreamReader | None
    read_transport: asyncio.ReadTransport | None
    initialized: bool = False
    client_ready: bool = False
    closed: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    events: deque = field(default_factory=deque)
    event_bytes: int = 0
    sequence: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict = field(default_factory=dict)
    server_pending: dict = field(default_factory=dict)
    seen_ids: set[bytes] = field(default_factory=set)
    followers: set = field(default_factory=set)
    reader_task: asyncio.Task | None = None

    async def publish(self, stream, data=None, terminal=False):
        if data is not None and len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("output_limit")
        async with self.condition:
            self.sequence += 1
            self.events.append(Event(self.sequence, stream, data, terminal))
            self.event_bytes += len(data or b"")
            while len(self.events) > MAX_EVENTS or self.event_bytes > MAX_OPERATION_BYTES:
                self.event_bytes -= len(self.events.popleft().data or b"")
            self.condition.notify_all()
            return self.sequence

    async def write(self, message):
        body = encode(message)
        if len(body) > self.gateway.max_request_bytes:
            raise ValueError("message_limit")
        data = body + b"\n"
        async with self.write_lock, asyncio.timeout(self.gateway.request_timeout):
            view = memoryview(data)
            loop = asyncio.get_running_loop()
            while view:
                try:
                    written = os.write(self.pipes.stdin_fd, view)
                    view = view[written:]
                except BlockingIOError:
                    ready = loop.create_future()
                    loop.add_writer(self.pipes.stdin_fd,
                                    lambda: None if ready.done() else ready.set_result(None))
                    try:
                        await ready
                    finally:
                        loop.remove_writer(self.pipes.stdin_fd)

    async def accept_message(self, message, message_bytes):
        JSONRPCMessage.model_validate(message)
        if "method" in message:
            method = message["method"]
            if "id" in message:
                if method != "ping" or not self.client_ready or len(self.server_pending) >= MAX_PENDING:
                    raise ValueError("unsupported_server_request")
                key = (type(message["id"]), message["id"])
                if key in self.server_pending:
                    raise ValueError("duplicate_server_request")
                self.server_pending[key] = asyncio.create_task(self.gateway.server_deadline(self))
                await self.publish("notifications", encode(message))
                return
            if method not in {"notifications/progress", "notifications/tools/list_changed"}:
                raise ValueError("unsupported_server_message")
            stream = "notifications"
            if method == "notifications/progress":
                ProgressNotificationParams.model_validate(message.get("params"))
                token = (message.get("params") or {}).get("progressToken")
                for pending in self.pending.values():
                    if token is not None and pending.progress_token == token:
                        stream = pending.stream
                        pending.output_bytes += message_bytes
                        if pending.output_bytes > MAX_OPERATION_BYTES:
                            raise ValueError("operation_limit")
                        break
            await self.publish(stream, encode(message))
            return
        key = (type(message["id"]), message["id"])
        pending = self.pending.get(key)
        if pending is None:
            raise ValueError("unexpected_response")
        if pending.method == "initialize" and "result" in message:
            result = InitializeResult.model_validate(message["result"])
            caps = message["result"].get("capabilities", {})
            if result.protocolVersion != PROTOCOL_VERSION or set(caps) - {"tools"}:
                await self.publish(pending.stream, encode(error_message(pending.request_id,
                                   message="The package requires unsupported MCP capabilities or version.")), True)
                raise ValueError("unsupported_profile")
            self.initialized = True
        if pending.method == "tools/list" and "result" in message:
            from mcp.types import ListToolsResult
            listed = ListToolsResult.model_validate(message["result"])
            if any(tool.execution and tool.execution.taskSupport not in {None, "forbidden"}
                   for tool in listed.tools):
                raise ValueError("unsupported_task_tool")
            if self.gateway.expected_tools is not None:
                tools = message["result"].get("tools", [])
                if tools != self.gateway.expected_tools:
                    raise ValueError("tool_contract_changed")
        if pending.method == "tools/call" and "result" in message:
            CallToolResult.model_validate(message["result"])
        pending.output_bytes += message_bytes
        if pending.output_bytes > MAX_OPERATION_BYTES:
            raise ValueError("operation_limit")
        if pending.deadline:
            pending.deadline.cancel()
        if pending.activity:
            pending.activity.finish("error" not in message and not message.get("result", {}).get("isError", False))
        await self.publish(pending.stream, encode(message), True)
        self.pending.pop(key)
        self.last_activity = time.monotonic()

    async def read_messages(self):
        try:
            while not self.closed:
                line = await self.reader.readline()
                if not line or not line.endswith(b"\n") or len(line) > MAX_MESSAGE_BYTES:
                    raise ValueError("invalid_output")
                message = load_json(line)
                await self.accept_message(message, len(line))
        except asyncio.CancelledError:
            raise
        except Exception:
            for pending in list(self.pending.values()):
                if not any(e.stream == pending.stream and e.terminal for e in self.events):
                    await self.publish(pending.stream, encode(error_message(pending.request_id)), True)
            await self.gateway.close(self.session_id)


class Gateway:
    def __init__(self, *, launch_session: Callable[[str], Awaitable[SessionPipes]],
                 close_session: Callable[[str], Awaitable[None]], max_sessions=8,
                 idle_timeout=300, request_timeout=60, active=None, expected_tools=None,
                 activity_report=None, max_request_bytes=DEFAULT_REQUEST_BYTES):
        if (not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions < 1 or
                not isinstance(max_request_bytes, int) or isinstance(max_request_bytes, bool) or
                not 1 <= max_request_bytes <= MAX_REQUEST_BYTES or
                any(not math.isfinite(v) or v <= 0 for v in (idle_timeout, request_timeout))):
            raise ValueError("invalid_limits")
        self.launch_session, self.close_session = launch_session, close_session
        self.max_sessions, self.idle_timeout, self.request_timeout = max_sessions, idle_timeout, request_timeout
        self.max_request_bytes = max_request_bytes
        self.active = active or (lambda: True)
        self.expected_tools = expected_tools
        self.activity = Activity()
        self.activity_report = activity_report
        self.sessions: dict[str, Session] = {}
        self.reservations: set[str] = set()
        self.lock = asyncio.Lock()
        self.stopping = False
        self.error_code = None
        self.sweeper = None

    async def start(self):
        self.sweeper = asyncio.create_task(self.expire())

    async def expire(self):
        while True:
            await asyncio.sleep(min(1, self.idle_timeout / 2))
            now = time.monotonic()
            for session_id, session in list(self.sessions.items()):
                if not session.pending and not session.server_pending and now - session.last_activity >= self.idle_timeout:
                    await self.close(session_id)

    async def new_session(self):
        session_id = secrets.token_hex(24)
        async with self.lock:
            if self.stopping or not self.active():
                raise RuntimeError("slot_stopped")
            if len(self.reservations) >= self.max_sessions:
                raise RuntimeError("session_limit")
            self.reservations.add(session_id)
        pipes = None
        read_transport = None
        try:
            async with asyncio.timeout(self.request_timeout):
                pipes = await self.launch_session(session_id)
            if self.stopping or not self.active():
                raise RuntimeError("slot_stopped")
            reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
            protocol = asyncio.StreamReaderProtocol(reader)
            read_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, os.fdopen(pipes.stdout_fd, "rb", buffering=0))
            os.set_blocking(pipes.stdin_fd, False)
            session = Session(session_id, pipes, self, reader, read_transport)
            self.sessions[session_id] = session
            session.reader_task = asyncio.create_task(session.read_messages())
            return session
        except BaseException:
            if read_transport:
                read_transport.close()
            elif pipes:
                with contextlib.suppress(OSError):
                    os.close(pipes.stdout_fd)
            if pipes:
                with contextlib.suppress(OSError):
                    os.close(pipes.stdin_fd)
                await self.close_session(pipes.instance_id)
            self.reservations.discard(session_id)
            raise

    async def close(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        session.closed = True
        for pending in list(session.pending.values()):
            if pending.activity:
                pending.activity.finish(False)
            if pending.deadline and pending.deadline is not asyncio.current_task():
                pending.deadline.cancel()
        for task in session.server_pending.values():
            if task is not asyncio.current_task():
                task.cancel()
        async with session.condition:
            session.condition.notify_all()
        if session.reader_task is not asyncio.current_task():
            session.reader_task.cancel()
            await asyncio.gather(session.reader_task, return_exceptions=True)
        session.read_transport.close()
        with contextlib.suppress(OSError):
            os.close(session.pipes.stdin_fd)
        try:
            async with asyncio.timeout(self.request_timeout):
                await self.close_session(session.pipes.instance_id)
        except Exception:
            self.error_code = "ownership_lost"
            self.stopping = True
        else:
            self.reservations.discard(session_id)
        session.pending.clear()
        session.server_pending.clear()

    async def shutdown(self):
        self.stopping = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        await asyncio.gather(*(self.close(sid) for sid in list(self.sessions)))
        if self.error_code:
            raise RuntimeError(self.error_code)

    async def deadline(self, session, pending):
        await asyncio.sleep(self.request_timeout)
        with contextlib.suppress(Exception):
            async with asyncio.timeout(min(2, self.request_timeout / 4)):
                await session.write({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                     "params": {"requestId": pending.request_id}})
        with contextlib.suppress(Exception):
            await session.publish(pending.stream, encode(error_message(pending.request_id,
                                  message="The slot request deadline expired.")), True)
        await self.close(session.session_id)

    async def server_deadline(self, session):
        await asyncio.sleep(self.request_timeout)
        await self.close(session.session_id)

    async def handle(self, request: Request):
        message = request.scope.get("mcpme.message") or {}
        counted = request.method == "POST" and message.get("method") == "tools/call"
        activity = RequestActivity(self.activity, self.activity_report) if counted else None
        try:
            if activity:
                activity.begin()
            return await self._handle(request, activity)
        finally:
            if activity and not activity.transferred:
                activity.finish(False)

    async def _handle(self, request: Request, activity=None):
        # Raw ASGI streaming is returned below so HTTP disconnect never cancels a package request.
        if self.stopping or not self.active():
            return JSONResponse({"error": {"code": "slot_stopped"}}, status_code=409)
        message = request.scope.get("mcpme.message")
        sid = request.headers.get("mcp-session-id")
        if request.method == "POST":
            accept = request.headers.get("accept", "")
            if "application/json" not in accept or "text/event-stream" not in accept:
                return Response(status_code=406)
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return Response(status_code=415)
        initialize = request.method == "POST" and message.get("method") == "initialize"
        if initialize:
            params = message.get("params") or {}
            caps = params.get("capabilities", {})
            if (sid or "id" not in message or params.get("protocolVersion") != PROTOCOL_VERSION or
                    not isinstance(caps, dict)):
                return JSONResponse({"error": {"code": "unsupported_profile"}}, status_code=400)
            try:
                InitializeRequestParams.model_validate(params)
            except Exception:
                return JSONResponse({"error": {"code": "invalid_initialize"}}, status_code=400)
            # Client capabilities advertise optional services the client offers.
            # They do not require this tools-only server to use those services.
            # The private package must never be promised capabilities that this
            # gateway cannot forward, so its client side advertises none.
            message = {**message, "params": {**params, "capabilities": {}}}
            try:
                session = await self.new_session()
            except RuntimeError as error:
                code = "session_limit" if str(error) == "session_limit" else "slot_unavailable"
                return JSONResponse({"error": {"code": code}}, status_code=429 if code == "session_limit" else 503)
            except Exception:
                return JSONResponse({"error": {"code": "slot_unavailable"}}, status_code=503)
        else:
            session = self.sessions.get(sid)
            if session is None:
                return JSONResponse({"error": {"code": "session_not_found"}}, status_code=404)
            if request.headers.get("mcp-protocol-version") != PROTOCOL_VERSION:
                return JSONResponse({"error": {"code": "unsupported_version"}}, status_code=400)
        if request.method == "DELETE":
            await self.close(session.session_id)
            return Response(status_code=200)
        if request.method == "GET":
            if "text/event-stream" not in request.headers.get("accept", ""):
                return Response(status_code=406)
            last_id = request.headers.get("last-event-id")
            if last_id:
                event = next((e for e in session.events if self.event_id(session, e.sequence) == last_id), None)
                if event is None:
                    return JSONResponse({"error": {"code": "event_not_found"}}, status_code=400)
                stream, after = event.stream, event.sequence
                if event.terminal:
                    return Response(status_code=200, headers={"content-type": "text/event-stream",
                                    "mcp-session-id": session.session_id})
            else:
                stream, after = "notifications", 0
            if stream in session.followers:
                return Response(status_code=409)
            # Notification streams reconnect automatically on transport timeouts.
            # Opening or replaying a stream is not MCP activity and must not keep
            # a quiet session (and its child process) leased indefinitely.
            return SSE(self, session, stream, after)
        if request.method != "POST":
            return Response(status_code=405)
        method = message.get("method")
        if method is None and "id" in message:
            key = (type(message["id"]), message["id"])
            task = session.server_pending.pop(key, None)
            if task is None:
                return JSONResponse({"error": {"code": "unexpected_response"}}, status_code=400)
            task.cancel()
            session.last_activity = time.monotonic()
            try:
                await session.write(message)
            except Exception:
                await self.close(session.session_id)
                return Response(status_code=503)
            return Response(status_code=202)
        allowed = {"initialize", "ping", "tools/list", "tools/call", "notifications/initialized",
                   "notifications/cancelled", "notifications/progress"}
        if method not in allowed or (not initialize and not session.initialized) or (message.get("params") or {}).get("task") is not None:
            return JSONResponse({"error": {"code": "unsupported_method"}}, status_code=400)
        if method in {"tools/list", "tools/call"} and not session.client_ready:
            return JSONResponse({"error": {"code": "not_initialized"}}, status_code=400)
        if (method.startswith("notifications/") and "id" in message) or (not method.startswith("notifications/") and "id" not in message):
            return JSONResponse({"error": {"code": "invalid_method_envelope"}}, status_code=400)
        try:
            if method == "tools/call":
                CallToolRequestParams.model_validate(message.get("params"))
            elif method == "tools/list":
                ListToolsRequest.model_validate(message)
            elif method == "notifications/progress":
                ProgressNotificationParams.model_validate(message.get("params"))
            elif method == "notifications/cancelled":
                cancelled = CancelledNotificationParams.model_validate(message.get("params"))
                if cancelled.requestId is None:
                    raise ValueError("task_cancellation_unsupported")
            elif method == "notifications/initialized" and session.client_ready:
                raise ValueError("already_initialized")
        except Exception:
            return JSONResponse({"error": {"code": "invalid_params"}}, status_code=400)
        if "id" not in message:
            session.last_activity = time.monotonic()
            try:
                await session.write(message)
            except Exception:
                await self.close(session.session_id)
                return Response(status_code=503)
            if method == "notifications/initialized":
                session.client_ready = True
            return Response(status_code=202)
        key = (type(message["id"]), message["id"])
        digest = request_id_digest(message["id"])
        if digest in session.seen_ids or len(session.pending) >= MAX_PENDING or len(session.seen_ids) >= 4096:
            return JSONResponse({"error": {"code": "request_limit"}}, status_code=429)
        session.seen_ids.add(digest)
        session.last_activity = time.monotonic()
        stream = secrets.token_hex(16)
        pending = Pending(message["id"], method, stream,
                          ((message.get("params") or {}).get("_meta") or {}).get("progressToken"))
        pending.activity = activity
        session.pending[key] = pending
        if activity:
            activity.transferred = True
        pending.deadline = asyncio.create_task(self.deadline(session, pending))
        try:
            priming = await session.publish(stream)
            await session.write(message)
        except asyncio.CancelledError:
            await self.close(session.session_id)
            raise
        except Exception:
            await self.close(session.session_id)
            return Response(status_code=503)
        return SSE(self, session, stream, priming - 1)

    @staticmethod
    def event_id(session, sequence):
        return f"{session.session_id}.{sequence}"


class SSE(Response):
    """Bounded SSE follower over a session event ring; replay and live delivery share one cursor."""
    def __init__(self, gateway, session, stream, after):
        super().__init__(status_code=200, headers={"content-type": "text/event-stream",
                         "cache-control": "no-cache", "mcp-session-id": session.session_id})
        self.raw_headers = [(k, v) for k, v in self.raw_headers if k != b"content-length"]
        self.gateway, self.session, self.stream, self.after = gateway, session, stream, after

    async def __call__(self, scope, receive, send):
        session = self.session
        if self.stream in session.followers:
            await Response(status_code=409)(scope, receive, send)
            return
        session.followers.add(self.stream)

        async def disconnected():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    return

        watcher = asyncio.create_task(disconnected())
        output = 0
        ended = False
        try:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            async with asyncio.timeout(self.gateway.request_timeout + 5 if self.stream != "notifications" else self.gateway.idle_timeout):
                while not watcher.done():
                    async with session.condition:
                        events = [e for e in session.events if e.sequence > self.after and e.stream == self.stream]
                    if events:
                        for event in events:
                            data = (f"id: {self.gateway.event_id(session, event.sequence)}\nevent: message\ndata: ".encode()
                                    + (event.data or b"") + b"\n\n")
                            output += len(data)
                            if output > MAX_OPERATION_BYTES:
                                return
                            await send({"type": "http.response.body", "body": data, "more_body": True})
                            self.after = event.sequence
                            if event.terminal:
                                await send({"type": "http.response.body", "body": b"", "more_body": False})
                                ended = True
                                return
                    elif session.closed:
                        return
                    else:
                        # A short bounded condition wait observes disconnects without retaining a cancelled tool call.
                        async with session.condition:
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(session.condition.wait(), 0.25)
        except TimeoutError:
            pass
        finally:
            if not ended and not watcher.done():
                with contextlib.suppress(Exception):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            session.followers.discard(self.stream)


def make_app(*, token: str, port: int, launch_session, close_session, max_sessions=8,
             idle_timeout=300, request_timeout=60, active=None, expected_tools=None,
             activity_report=None, max_request_bytes=DEFAULT_REQUEST_BYTES):
    gateway = Gateway(launch_session=launch_session, close_session=close_session, max_sessions=max_sessions,
                      idle_timeout=idle_timeout, request_timeout=request_timeout, active=active,
                      expected_tools=expected_tools, activity_report=activity_report,
                      max_request_bytes=max_request_bytes)

    @asynccontextmanager
    async def lifespan(app):
        await gateway.start()
        try:
            yield
        finally:
            await gateway.shutdown()

    app = Starlette(routes=[Route("/mcp", gateway.handle, methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
    app.router.redirect_slashes = False
    return LocalPolicy(app, token, port, max_bytes=max_request_bytes), gateway
