"""Content-free loopback HTTP policy shared by slot workers."""
from __future__ import annotations

import asyncio
import hmac
import json

from mcp.types import JSONRPCMessage
from starlette.responses import JSONResponse


class AuthenticationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def load_json(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_number")))


class LocalPolicy:
    """Authenticate before consuming bounded bodies; suppress raw exceptions."""

    def __init__(self, app, token: str, port: int, max_bytes: int = 1024 * 1024,
                 body_timeout: float = 15, authenticate=None):
        self.app, self.token, self.port = app, token, port
        self.max_bytes, self.body_timeout = max_bytes, body_timeout
        self.authenticate = authenticate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers, duplicates = {}, set()
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers:
                duplicates.add(key)
            headers[key] = value.decode("latin1")
        sensitive = {b"host", b"origin", b"authorization", b"content-length", b"transfer-encoding",
                     b"content-type", b"accept", b"mcp-session-id", b"mcp-protocol-version", b"last-event-id"}
        allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        allowed_origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        if (duplicates & sensitive or headers.get(b"host") not in allowed_hosts or
                (b"origin" in headers and headers[b"origin"] not in allowed_origins)):
            return await self.error(scope, receive, send, 403, "forbidden")
        authorization = headers.get(b"authorization", "")
        if self.authenticate is not None:
            try:
                scope["meecp.principal"] = self.authenticate(authorization)
            except AuthenticationError as error:
                return await self.error(scope, receive, send, 401, error.code)
            except Exception:
                return await self.error(scope, receive, send, 401, "unauthorized")
        elif not hmac.compare_digest(authorization.encode(), ("Bearer " + self.token).encode()):
            return await self.error(scope, receive, send, 401, "unauthorized")
        try:
            raw_length = headers.get(b"content-length", "0")
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise ValueError()
            length = int(raw_length)
            if b"transfer-encoding" in headers and b"content-length" in headers:
                raise ValueError()
        except ValueError:
            return await self.error(scope, receive, send, 400, "invalid_framing")
        if length > self.max_bytes:
            return await self.error(scope, receive, send, 413, "body_limit")
        body = bytearray()
        try:
            async with asyncio.timeout(self.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_bytes:
                        return await self.error(scope, receive, send, 413, "body_limit")
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await self.error(scope, receive, send, 408, "body_timeout")
        if b"content-length" in headers and len(body) != length:
            return await self.error(scope, receive, send, 400, "invalid_framing")
        if scope.get("method") == "POST" and scope.get("path") in {"/mcp", "/mcp/"}:
            try:
                payload = load_json(bytes(body))
                JSONRPCMessage.model_validate(payload)
                scope["meecp.message"] = payload
            except Exception:
                return await self.error(scope, receive, send, 400, "invalid_protocol")
        delivered, started = False, False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, replay, safe_send)
        except Exception:
            if not started:
                await self.error(scope, receive, send, 500, "service_error")

    @staticmethod
    async def error(scope, receive, send, status, code):
        await JSONResponse({"error": {"code": code}}, status_code=status)(scope, receive, send)
