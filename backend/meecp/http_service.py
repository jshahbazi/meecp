"""Foreground HTTP wrapper for a selected tools-only low-level MCP factory.

Copied into an external prepared package. Source factories execute only when the
host launches its explicitly installed resident, never during package inspection.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager, contextmanager
import importlib
import json
import logging
import os
import re

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

try:
    from .policy import LocalPolicy, load_json
except ImportError:
    # The versioned generator copies these two modules into a sealed package.
    from http_policy import LocalPolicy, load_json


def read_config(descriptor: int) -> dict:
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("runtime_config_limit")
    config = load_json(raw)
    endpoint = config.get("endpoint", {}) if isinstance(config, dict) else {}
    if (not isinstance(endpoint, dict) or set(endpoint) != {"host", "port"}
            or endpoint["host"] != "127.0.0.1" or type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65535
            or type(config.get("generation")) is not int or config["generation"] < 1
            or not isinstance(config.get("nonce"), str) or not re.fullmatch("[0-9a-f]{64}", config["nonce"])
            or not isinstance(config.get("api_key"), str) or not re.fullmatch("[A-Za-z0-9_-]{32,128}", config["api_key"])
            or config.get("model") is not None or config.get("dependencies") != {}):
        raise ValueError("invalid_http_configuration")
    lifecycle = config.get("lifecycle", {})
    if not isinstance(lifecycle, dict):
        raise ValueError("invalid_http_configuration")
    for field, default, minimum, maximum in (("max_sessions", 8, 1, 64), ("idle_timeout_seconds", 300, 1, 300),
                                           ("max_request_bytes", 1048576, 1, 16 * 1048576), ("stop_grace_seconds", 2, 1, 10)):
        value = lifecycle.get(field, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("invalid_http_configuration")
    return config


def protect_server(server: Server) -> None:
    """Require the supported capabilities and sanitize SDK/tool error results."""
    if not isinstance(server, Server):
        raise ValueError("http_factory_contract_required")
    initial = server.create_initialization_options()
    capabilities = initial.capabilities.model_dump(exclude_none=True)
    if capabilities.get("experimental") == {}:
        capabilities.pop("experimental")
    if set(capabilities) != {"tools"} or types.CallToolRequest not in server.request_handlers:
        raise ValueError("unsupported_mcp_capability")
    original_options = server.create_initialization_options
    def options(*args, **kwargs):
        result = original_options(*args, **kwargs)
        result.capabilities.experimental = None
        return result
    server.create_initialization_options = options
    original = server.request_handlers[types.CallToolRequest]
    async def call(request):
        try:
            result = await original(request)
            if isinstance(result.root, types.CallToolResult) and not result.root.isError:
                return result
        except Exception:
            pass
        return types.ServerResult(types.CallToolResult(isError=True,
            content=[types.TextContent(type="text", text="tool_execution_failed")]))
    server.request_handlers[types.CallToolRequest] = call


class EnvelopePolicy(LocalPolicy):
    """Validate method-specific envelopes before SDK errors can echo arguments."""
    def __init__(self, app, *args, **kwargs):
        self.service = app
        super().__init__(self.dispatch, *args, **kwargs)

    async def dispatch(self, scope, receive, send):
        message = scope.get("meecp.message")
        if message is not None:
            try:
                if "id" in message:
                    types.ClientRequest.model_validate(message)
                else:
                    types.ClientNotification.model_validate(message)
            except Exception:
                return await self.error(scope, receive, send, 400, "invalid_protocol")
        await self.service(scope, receive, send)


def make_http_app(server: Server, config: dict):
    protect_server(server)
    port = config["endpoint"]["port"]
    lifecycle = config.get("lifecycle", {})
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=False,
        security_settings=TransportSecuritySettings(allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"]),
        max_sessions=lifecycle.get("max_sessions", 8), session_idle_timeout=lifecycle.get("idle_timeout_seconds", 300),
        max_request_body_size=lifecycle.get("max_request_bytes", 1048576))
    ready = False
    @asynccontextmanager
    async def lifespan(app):
        nonlocal ready
        async with manager.run():
            ready = True
            try:
                yield
            finally:
                ready = False
    async def health(request):
        return JSONResponse({"ready": ready, "generation": config["generation"], "nonce": config["nonce"]})
    class MCPApplication:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)
    # Route delegates ASGI directly, preserving GET streams and explicit DELETE.
    application = Starlette(routes=[Route("/health", health),
        Route("/mcp", MCPApplication(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
    policy = EnvelopePolicy(application, config["api_key"], port, max_bytes=lifecycle.get("max_request_bytes", 1048576))
    return policy, manager


class ForegroundServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        import signal
        originals = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in originals.items():
                signal.signal(sig, handler)


def serve_factory(module: str, factory: str, descriptor: int) -> None:
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "wb") as discarded:
        os.dup2(discarded.fileno(), 1)
        os.dup2(discarded.fileno(), 2)
    config = read_config(descriptor)
    selected = getattr(importlib.import_module(module), factory)
    server = selected()
    app, _ = make_http_app(server, config)
    runtime = ForegroundServer(uvicorn.Config(app, host="127.0.0.1", port=config["endpoint"]["port"],
        log_config=None, access_log=False, log_level="critical", timeout_graceful_shutdown=config.get("lifecycle", {}).get("stop_grace_seconds", 2)))
    runtime.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meecp-config-fd", "--mcpme-config-fd", dest="meecp_config_fd", type=int, required=True)
    args = parser.parse_args()
    try:
        from pathlib import Path
        selected = load_json((Path(__file__).resolve().parent / "http-factory.json").read_bytes())
        if (set(selected) != {"module", "factory"} or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", selected["module"])
                or not re.fullmatch(r"[A-Za-z_]\w*", selected["factory"])):
            raise ValueError()
        serve_factory(selected["module"], selected["factory"], args.meecp_config_fd)
    except Exception:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
