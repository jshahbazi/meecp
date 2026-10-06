"""MCP wrapper for one host-owned exact CPU engine generation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from typing import Annotated, Any, Literal
import urllib.request

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ServerCapabilities, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from jevk5_app.server import LocalPolicy
from .adapter import BoundedTextGenerator, LoopbackTransport, TextGenerationError, request_fields
from .fixture import ENGINE_BUILD, MODEL_FILENAME, MODEL_SHA256, TEMPLATE_SHA256

PROFILE = "qwen2.5-0.5b-q4_k_m-cpu-text-v1"
DESCRIPTION = "Generate bounded text from one message using the local Qwen2.5 0.5B model. Maximum 64 output tokens and 512 total context tokens; no tools or retrieval."


def model_declaration():
    return {"artifact": "models/" + MODEL_FILENAME, "model_id": "Qwen2.5-0.5B-Instruct-Q4_K_M",
            "sha256": MODEL_SHA256, "profile": PROFILE, "context_tokens": 512,
            "engine_identity": {"runtime": "llama.cpp", "build": ENGINE_BUILD},
            "parameters": {"max_output_tokens": 64, "queue_limit": 4, "context_guard": 8}}


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str
    input_tokens: int = Field(ge=1, le=503)
    output_tokens: int = Field(ge=0, le=64)
    stop_reason: Literal["eos", "stop", "length"]


class AttachedTextModel:
    def __init__(self, config):
        if json.dumps(config["model"], sort_keys=True) != json.dumps(model_declaration(), sort_keys=True):
            raise TextGenerationError("invalid_request")
        self.config = config
        self.engine = config["dependencies"]["engine"]
        self.transport = LoopbackTransport(self.engine["endpoint"], self.engine["api_key"], timeout=10)
        self.fatal = asyncio.Event()
        self.ready = False
        self.adapter = BoundedTextGenerator(self.transport, self.fence)

    async def fence(self):
        # This process has no authority to reap its sibling engine. Exit so the
        # host monitor tears down the graph while retaining the reservation.
        # False deliberately preserves the adapter's uncertain-ownership flag.
        self.ready = False
        self.fatal.set()
        return False

    def _verify_engine(self):
        request = urllib.request.Request(self.transport.endpoint + "/props",
            headers={"Authorization": "Bearer " + self.transport.api_key})
        with self.transport.opener.open(request, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise TextGenerationError("invalid_engine_response")
        props = json.loads(raw)
        if (props["total_slots"] != 1 or props["default_generation_settings"]["n_ctx"] != 512
                or hashlib.sha256(props["chat_template"].encode()).hexdigest() != TEMPLATE_SHA256):
            raise TextGenerationError("invalid_engine_response")

    async def start(self):
        # Startup runs before the host admits public calls. Process ownership
        # remains with the host if startup is interrupted or fails.
        await asyncio.to_thread(self._verify_engine)
        self.ready = True

    async def shutdown(self):
        self.ready = False
        self.adapter.closed = True
        async with self.adapter.lock:
            pass

    def health(self):
        return {"ready": self.ready and not self.adapter.closed,
                "generation": self.config["generation"], "nonce": self.config["nonce"]}

    def status(self):
        return {"ready": self.ready and not self.adapter.closed, "model_id": model_declaration()["model_id"],
                "model_sha256": MODEL_SHA256, "profile": PROFILE, "context_tokens": 512,
                "max_output_tokens": 64, "queue_depth": self.adapter.pending,
                "completed": self.adapter.completed, "generation": self.config["generation"],
                "engine_instance_id": self.engine["instance_id"]}

    async def generate(self, text, maximum):
        if not self.ready:
            raise TextGenerationError("engine_unavailable")
        return await self.adapter.generate({"text": text, "max_output_tokens": maximum})


class SafeTextMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            if name == "text_generate":
                request_fields(arguments)
            elif name != "text_status" or arguments:
                raise TextGenerationError("invalid_request")
            return await super().call_tool(name, arguments)
        except Exception as error:
            for _ in range(8):
                if isinstance(error, TextGenerationError):
                    raise ToolError(error.code) from None
                if error.__cause__ is None:
                    break
                error = error.__cause__
            raise ToolError("invalid_request") from None

    async def read_resource(self, uri):
        raise ToolError("invalid_request")

    async def get_prompt(self, name, arguments=None):
        raise ToolError("invalid_request")


class LiteralTextMetadata(FuncMetadata):
    def pre_parse_json(self, data):
        return data


def make_app(manager, token, *, port):
    mcp = SafeTextMCP("Local text generation", instructions=DESCRIPTION, host="127.0.0.1", port=port,
        # Cancellation is a separate HTTP POST. Keep its request identity in
        # the same session until the inference owner has joined. The model's
        # execution queue remains bounded independently of client connections.
        log_level="CRITICAL", stateless_http=False, json_response=True, max_request_body_size=32768,
        max_sessions=None, session_idle_timeout=300,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"]))

    @mcp.tool(description=DESCRIPTION, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def text_generate(text: Annotated[str, Field(strict=True, min_length=1, max_length=8192)],
                            max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=64)]) -> GenerationResult:
        return GenerationResult(**await manager.generate(text, max_output_tokens))

    @mcp.tool(description="Report the pinned local model identity, readiness, engine generation and bounded queue counts.",
              annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def text_status() -> dict[str, Any]:
        return manager.status()

    tool = mcp._tool_manager.get_tool("text_generate")
    tool.fn_metadata = LiteralTextMetadata(**tool.fn_metadata.__dict__)
    original = mcp._mcp_server.get_capabilities
    def capabilities(*args, **kwargs):
        return ServerCapabilities(tools=original(*args, **kwargs).tools)
    mcp._mcp_server.get_capabilities = capabilities
    sdk = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        await manager.start()
        try:
            async with sdk.router.lifespan_context(sdk):
                yield
        finally:
            await manager.shutdown()

    async def health(request):
        value = manager.health()
        return JSONResponse(value, status_code=200 if value["ready"] else 503)

    app = Starlette(routes=[Route("/health", health), Mount("/", app=sdk)], lifespan=lifespan)
    app.state.mcp = mcp
    return LocalPolicy(app, token, max_bytes=32768, port=port), app
