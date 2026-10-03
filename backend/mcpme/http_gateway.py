"""Bounded tools-only MCP gateway for one supervisor-owned private HTTP service."""
from __future__ import annotations

import asyncio
import contextlib
import math
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .bridge import Bridge
from .activity import RequestActivity
from .gateway import (DEFAULT_REQUEST_BYTES, Gateway, MAX_MESSAGE_BYTES, MAX_PENDING, PROTOCOL_VERSION,
                      Session, encode, error_message)
from .policy import LocalPolicy, load_json


def checked_endpoint(endpoint):
    if endpoint is None:
        return None
    value = urlsplit(endpoint)
    if (value.scheme != "http" or value.hostname != "127.0.0.1" or not value.port or
            value.username or value.password or value.path != "/mcp" or value.query or value.fragment):
        raise ValueError("invalid_private_endpoint")
    return endpoint


async def bounded_json(response):
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("output_limit")
    return load_json(bytes(data))


class HTTPSession(Session):
    def __init__(self, session_id, gateway):
        super().__init__(session_id, None, gateway, None, None)
        self.endpoint, self.token = gateway.upstream_endpoint, gateway.upstream_token
        self.private_session_id = None
        self.tasks = set()
        self.notification_task = None
        self.notification_event_id = None

    def headers(self):
        result = {"authorization": "Bearer " + self.token, "accept": "application/json, text/event-stream",
                  "content-type": "application/json", "mcp-protocol-version": PROTOCOL_VERSION}
        if self.private_session_id:
            result["mcp-session-id"] = self.private_session_id
        return result

    async def write(self, message):
        if self.closed:
            raise RuntimeError("session_closed")
        if "id" in message and "method" in message:
            if len(self.tasks) >= MAX_PENDING:
                raise RuntimeError("request_limit")
            task = asyncio.create_task(self.post(message))
            self.tasks.add(task)
            task.add_done_callback(self.finished)
        else:
            await self.post(message)
            if message.get("method") == "notifications/initialized" and self.private_session_id:
                self.notification_task = asyncio.create_task(self.notifications())

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Never emit arbitrary background exception text.

    async def fail(self):
        for pending in list(self.pending.values()):
            if not any(event.stream == pending.stream and event.terminal for event in self.events):
                await self.publish(pending.stream, encode(error_message(pending.request_id)), True)
        await self.gateway.close(self.session_id)

    async def post(self, message):
        is_request = "id" in message and "method" in message
        request_id = message.get("id")
        received = False
        try:
            body = encode(message)
            if len(body) > self.gateway.max_request_bytes:
                raise ValueError("request_limit")
            async with asyncio.timeout(self.gateway.request_timeout):
                async with self.gateway.client.stream("POST", self.endpoint, headers=self.headers(), content=body) as response:
                    if response.status_code not in {200, 202}:
                        raise ValueError("private_request_failed")
                    if message.get("method") == "initialize":
                        private_id = response.headers.get("mcp-session-id")
                        if private_id is not None:
                            if (not private_id or len(private_id) > 128 or not private_id.isascii() or
                                    not all(33 <= ord(c) <= 126 for c in private_id)):
                                raise ValueError("invalid_private_session")
                            self.private_session_id = private_id
                    if response.status_code == 202:
                        if is_request:
                            raise ValueError("missing_response")
                        return
                    if not is_request:
                        raise ValueError("unexpected_response")
                    content_type = response.headers.get("content-type", "").split(";")[0].strip()
                    if content_type == "application/json":
                        result = await bounded_json(response)
                        if (result.get("id") != request_id or type(result.get("id")) is not type(request_id)):
                            raise ValueError("response_mismatch")
                        await self.accept_message(result, len(encode(result)))
                        received = True
                    elif content_type == "text/event-stream":
                        parser = Bridge(self.endpoint, self.token, max_request_bytes=self.gateway.max_request_bytes)
                        async for result in parser.messages(response):
                            if "id" in result and "method" not in result:
                                if (received or result["id"] != request_id or type(result["id"]) is not type(request_id)):
                                    raise ValueError("response_mismatch")
                                received = True
                            await self.accept_message(result, len(encode(result)))
                    else:
                        raise ValueError("invalid_content_type")
                    if not received:
                        raise ValueError("missing_response")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.fail()
            if not is_request:
                raise RuntimeError("private_request_failed") from None

    async def notifications(self):
        def remember(value):
            self.notification_event_id = value
        failures = 0
        while not self.closed:
            try:
                headers = self.headers()
                if self.notification_event_id:
                    headers["last-event-id"] = self.notification_event_id
                async with self.gateway.client.stream("GET", self.endpoint, headers=headers) as response:
                    if response.status_code != 200:
                        raise ValueError("notification_transport_unavailable")
                    if response.headers.get("content-type", "").split(";")[0].strip() != "text/event-stream":
                        raise ValueError("invalid_content_type")
                    parser = Bridge(self.endpoint, self.token, max_request_bytes=self.gateway.max_request_bytes)
                    async for result in parser.messages(response, remember):
                        if "method" not in result:
                            raise ValueError("unexpected_response")
                        await self.accept_message(result, len(encode(result)))
                        failures = 0
            except asyncio.CancelledError:
                raise
            except httpx.ReadTimeout:
                pass
            except httpx.TransportError:
                failures += 1
                if failures > 2:
                    await self.fail()
                    return
            except Exception:
                await self.fail()
                return
            await asyncio.sleep(0.1)


class HTTPGateway(Gateway):
    """Shared runtime, independent host sessions and generation-local credentials."""
    def __init__(self, *, upstream_endpoint, upstream_token, legacy_jev=False, control=None,
                 control_timeout=None, client=None, **options):
        self.upstream_endpoint = checked_endpoint(upstream_endpoint)
        if bool(upstream_endpoint) != bool(upstream_token):
            raise ValueError("invalid_private_connection")
        self.upstream_token = upstream_token
        self.legacy_jev, self.control = legacy_jev, control
        active = options.pop("active", None)
        self.runtime_active = bool(self.upstream_endpoint) and (active() if active else True)
        self.client = client
        self.owns_client = client is None
        async def unused(*args):
            raise RuntimeError("http_processes_are_supervisor_owned")
        # Public admission is host-local; no package process is spawned per client.
        super().__init__(launch_session=unused, close_session=unused,
                         active=lambda: self.runtime_active, **options)
        self.control_timeout = self.request_timeout if control_timeout is None else control_timeout
        if (not isinstance(self.control_timeout, (int, float)) or isinstance(self.control_timeout, bool)
                or not math.isfinite(self.control_timeout) or self.control_timeout <= 0):
            raise ValueError("invalid_control_timeout")

    async def start(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.request_timeout),
                                           trust_env=False, follow_redirects=False)
        await super().start()

    async def configure(self, endpoint, token):
        endpoint = checked_endpoint(endpoint)
        if bool(endpoint) != bool(token):
            raise ValueError("invalid_private_connection")
        self.runtime_active = False
        await asyncio.gather(*(self.close(sid) for sid in list(self.sessions)))
        self.upstream_endpoint, self.upstream_token = endpoint, token
        self.runtime_active = endpoint is not None

    async def new_session(self):
        async with self.lock:
            if self.stopping or not self.active():
                raise RuntimeError("slot_stopped")
            if len(self.reservations) >= self.max_sessions:
                raise RuntimeError("session_limit")
            sid = secrets.token_hex(24)
            self.reservations.add(sid)
            session = HTTPSession(sid, self)
            self.sessions[sid] = session
            return session

    async def close(self, sid):
        session = self.sessions.pop(sid, None)
        if session is None:
            return
        session.closed = True
        for pending in list(session.pending.values()):
            if pending.activity:
                pending.activity.finish(False)
            if pending.deadline is not asyncio.current_task():
                pending.deadline.cancel()
        for task in session.server_pending.values():
            if task is not asyncio.current_task():
                task.cancel()
        async with session.condition:
            session.condition.notify_all()
        tasks = [task for task in session.tasks if task is not asyncio.current_task()]
        if session.notification_task and session.notification_task is not asyncio.current_task():
            tasks.append(session.notification_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if session.private_session_id:
            try:
                async with asyncio.timeout(2):
                    async with self.client.stream("DELETE", session.endpoint, headers=session.headers()) as response:
                        if response.status_code not in {200, 204, 404}:
                            raise ValueError("private_termination_failed")
            except Exception:
                self.error_code = "upstream_termination_failed"
                self.stopping = True
        self.reservations.discard(sid)
        session.pending.clear()
        session.server_pending.clear()

    async def shutdown(self):
        try:
            await super().shutdown()
        finally:
            if self.owns_client and self.client:
                await self.client.aclose()

    async def handle(self, request):
        if request.method == "DELETE":
            result = await super().handle(request)
            if self.error_code == "upstream_termination_failed":
                return JSONResponse({"error": {"code": self.error_code}}, status_code=502)
            return result
        return await super().handle(request)

    async def legacy(self, request: Request):
        activity = (RequestActivity(self.activity, self.activity_report)
                    if self.legacy_jev and request.url.path == "/api/decide" else None)
        succeeded = False
        try:
            if activity:
                activity.begin()
            response = await self._legacy(request)
            succeeded = response.status_code == 200
            return response
        finally:
            if activity:
                activity.finish(succeeded)

    async def _legacy(self, request: Request):
        if self.inactive_code == "updating":
            return JSONResponse({"error": {"code": "updating"}}, status_code=409)
        if not self.legacy_jev:
            return Response(status_code=404)
        path = request.url.path
        if path == "/api/control":
            try:
                message = load_json(await request.body())
                if set(message) != {"action"} or message["action"] not in {"pause", "resume"} or self.control is None:
                    raise ValueError("invalid_control")
                async with asyncio.timeout(self.control_timeout):
                    result = await self.control(message["action"])
                if len(encode(result)) > MAX_MESSAGE_BYTES:
                    raise ValueError("output_limit")
                return JSONResponse(result)
            except Exception:
                return JSONResponse({"error": {"code": "control_failed"}}, status_code=409)
        if not self.runtime_active:
            if path == "/api/status" and self.control:
                try:
                    async with asyncio.timeout(min(5, self.request_timeout)):
                        return JSONResponse(await self.control("status"))
                except Exception:
                    return JSONResponse({"error": {"code": "slot_unavailable"}}, status_code=503)
            return JSONResponse({"error": {"code": "paused", "message": "The model is paused."}}, status_code=409)
        endpoint = self.upstream_endpoint.removesuffix("/mcp") + path
        try:
            body = await request.body()
            if len(body) > self.max_request_bytes:
                raise ValueError("request_limit")
            async with asyncio.timeout(self.request_timeout):
                async with self.client.stream(request.method, endpoint,
                        headers={"authorization": "Bearer " + self.upstream_token, "content-type": "application/json"},
                        content=body) as response:
                    if response.is_redirect or response.status_code not in {200, 400, 409, 429, 503}:
                        raise ValueError("private_legacy_request_failed")
                    value = await bounded_json(response)
                    return JSONResponse(value, status_code=response.status_code)
        except Exception:
            return JSONResponse({"error": {"code": "slot_unavailable"}}, status_code=503)


def make_http_app(*, token, port, upstream_endpoint=None, upstream_token=None, max_sessions=8,
                  idle_timeout=300, request_timeout=180, expected_tools=None, active=None,
                  legacy_jev=False, control=None, control_timeout=None, client=None,
                  activity_report=None, max_request_bytes=DEFAULT_REQUEST_BYTES, inactive_code="slot_stopped"):
    gateway = HTTPGateway(upstream_endpoint=upstream_endpoint, upstream_token=upstream_token,
                          max_sessions=max_sessions, idle_timeout=idle_timeout, request_timeout=request_timeout,
                          expected_tools=expected_tools, active=active, legacy_jev=legacy_jev, control=control,
                          control_timeout=control_timeout, client=client, activity_report=activity_report,
                          max_request_bytes=max_request_bytes, inactive_code=inactive_code)

    @asynccontextmanager
    async def lifespan(app):
        await gateway.start()
        try:
            yield
        finally:
            await gateway.shutdown()

    routes = [Route("/mcp", gateway.handle, methods=["GET", "POST", "DELETE"])]
    if legacy_jev:
        routes += [Route("/api/status", gateway.legacy, methods=["GET"]),
                   Route("/api/decide", gateway.legacy, methods=["POST"]),
                   Route("/api/control", gateway.legacy, methods=["POST"])]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.router.redirect_slashes = False
    return LocalPolicy(app, token, port, max_bytes=max_request_bytes), gateway
