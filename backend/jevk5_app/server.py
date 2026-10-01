"""Authenticated loopback MCP and menu control APIs."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import JSONRPCMessage, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .contracts import Question, ServiceError
from .state import ENDPOINT, PORT


class SafeMCP(FastMCP):
    """Keep SDK validation/tool exceptions from echoing caller-supplied content."""
    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            return await super().call_tool(name, arguments)
        except Exception as error:
            cause = error
            for _ in range(8):
                if isinstance(cause, ServiceError):
                    raise ToolError(cause.code + ": " + cause.message) from None
                if cause.__cause__ is None:
                    break
                cause = cause.__cause__
            raise ToolError("invalid_tool_call: The tool call could not be completed.") from None

    async def read_resource(self, uri):
        raise ValueError("Resources are unavailable; use the decision tools.")

    async def get_prompt(self, name, arguments=None):
        raise ValueError("Prompts are unavailable; use the decision tools.")


DECIDE_DESCRIPTION = (
    "Score supplied evidence against a declared criterion with the local JevK5 decision model. "
    "Use for boolean propositions (noul), 2–16 named choices, or score levels. "
    "The model has no retrieval or external knowledge source and generates no explanation. "
    "Return calibrated option probabilities and model/cache metadata. Evidence may be a JSON "
    "string, object, or list. Choice criteria are an ordered object or distinct labels; score "
    "criteria must be an ordered array of descriptions for levels 0 through N−1, from lowest "
    "to highest. Confidence thresholds require task-specific validation."
)


class LiteralEvidenceMetadata(FuncMetadata):
    """Preserve string evidence across the SDK's convenience JSON preprocessing."""
    def pre_parse_json(self, data):
        parsed = super().pre_parse_json(data)
        if "state" in data:
            parsed["state"] = data["state"]
        return parsed


def preserve_literal_evidence(mcp: FastMCP):
    # This seam is covered by a real SDK transport regression and the SDK is
    # locked. Keep the official schemas, validation, and result conversion.
    tool = mcp._tool_manager.get_tool("jevk5_decide")
    tool.fn_metadata = LiteralEvidenceMetadata(**tool.fn_metadata.__dict__)


class LocalPolicy:
    """Protect every route, including SDK routes, before reading request content."""
    def __init__(self, app, token: str, max_bytes: int = 1024 * 1024):
        self.app, self.token, self.max_bytes = app, token, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        duplicates = set()
        for key, value in scope.get("headers", []):
            key = key.lower()
            if key in headers:
                duplicates.add(key)
            headers[key] = value.decode("latin1")
        host = headers.get(b"host", "")
        origin = headers.get(b"origin")
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        allowed_origins = {ENDPOINT, f"http://localhost:{PORT}"}
        if duplicates & {b"host", b"origin", b"authorization", b"content-length"} or host not in allowed_hosts or (origin is not None and origin not in allowed_origins):
            return await self._error(scope, receive, send, 403, "forbidden", "Local origin validation failed.")
        expected = "Bearer " + self.token
        if not hmac.compare_digest(headers.get(b"authorization", "").encode(), expected.encode()):
            return await self._error(scope, receive, send, 401, "unauthorized", "Authentication required.")
        try:
            length = int(headers.get(b"content-length", "0"))
            if length < 0:
                raise ValueError()
        except ValueError:
            return await self._error(scope, receive, send, 400, "invalid_request", "Invalid request framing.")
        if length > self.max_bytes:
            return await self._error(scope, receive, send, 413, "body_limit", "Request body is too large.")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                return await self._error(scope, receive, send, 413, "body_limit", "Request body is too large.")
            if not message.get("more_body", False):
                break
        # The SDK includes Pydantic input values in malformed protocol errors.
        # Validate the same envelope first and substitute a content-free failure.
        if scope.get("method") == "POST" and scope.get("path") in {"/mcp", "/mcp/"}:
            try:
                JSONRPCMessage.model_validate(json.loads(body))
            except Exception:
                return await self._error(scope, receive, send, 400, "invalid_protocol", "Invalid MCP request envelope.")
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, safe_send)
        except Exception:
            if not started:
                await self._error(scope, receive, send, 500, "service_error", "The local service could not complete this request.")

    @staticmethod
    async def _error(scope, receive, send, status, code, message):
        await JSONResponse({"error": {"code": code, "message": message}}, status_code=status)(scope, receive, send)


def make_app(manager, token: str):
    mcp = SafeMCP("JevK5", instructions=DECIDE_DESCRIPTION, host="127.0.0.1", port=PORT,
                  log_level="CRITICAL", stateless_http=True, json_response=True,
                  max_request_body_size=1024 * 1024,
                  transport_security=TransportSecuritySettings(
                      allowed_hosts=[f"127.0.0.1:{PORT}", f"localhost:{PORT}"],
                      allowed_origins=[ENDPOINT, f"http://localhost:{PORT}"]))

    @mcp.tool(description=DECIDE_DESCRIPTION, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def jevk5_decide(state: str | dict[str, Any] | list[Any], question: Question) -> dict[str, Any]:
        return await manager.decide(state, question)

    @mcp.tool(description="Report local model readiness, pause state, model identity, aggregate counts, and MCP endpoint. Contains no evidence or decisions.",
              annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def jevk5_status() -> dict[str, Any]:
        return manager.status()

    preserve_literal_evidence(mcp)

    async def status(request: Request):
        return JSONResponse(manager.status())

    async def control(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"action"} or not isinstance(payload["action"], str):
                raise ServiceError("invalid_control", "Invalid control request.", 400)
            return JSONResponse(await manager.control(payload["action"]))
        except ServiceError as error:
            return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)
        except Exception:
            return JSONResponse({"error": {"code": "invalid_control", "message": "Invalid control request."}}, status_code=400)

    async def decide(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"state", "question"}:
                raise ServiceError("invalid_decision", "Invalid decision request.", 400)
            return JSONResponse(await manager.decide(payload["state"], payload["question"]))
        except ServiceError as error:
            return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)
        except Exception:
            return JSONResponse({"error": {"code": "invalid_decision", "message": "Invalid decision request."}}, status_code=400)

    sdk_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        await manager.start()
        try:
            async with sdk_app.router.lifespan_context(sdk_app):
                yield
        finally:
            await manager.shutdown()

    app = Starlette(routes=[Route("/api/status", status, methods=["GET"]),
                            Route("/api/control", control, methods=["POST"]),
                            Route("/api/decide", decide, methods=["POST"]), Mount("/", app=sdk_app)], lifespan=lifespan)
    return LocalPolicy(app, token), app


async def parent_watchdog(parent_pid: int, server):
    while not server.should_exit:
        await asyncio.sleep(0.5)
        if os.getppid() != parent_pid:
            server.should_exit = True
            return
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            server.should_exit = True
            return
        except PermissionError:
            server.should_exit = True
            return
