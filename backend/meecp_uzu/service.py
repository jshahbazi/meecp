"""MCP adapter for one directly host-owned experimental Rust Uzu engine."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import math
from pathlib import Path
import re
from typing import Annotated, Any, Literal
import urllib.error
import urllib.request

import anyio
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ServerCapabilities, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from jevk5_app.server import LocalPolicy
from meecp_textgen.adapter import BoundedTextGenerator, LoopbackTransport, TextGenerationError
from meecp_textgen.service import LiteralTextMetadata, SafeTextMCP

PROFILE = "qwen3.5-0.8b-mirai-l-uzu-metal-text-v1"
ENGINE_BUILD = "8d01581474760bd1fc7f4a547ff1d02f7dada437"
CONTEXT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 256
CONTEXT_GUARD = 8
MAX_INPUT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
SPECIAL_MARKERS = ("<|", "<think>", "</think>", "<tool_call>", "</tool_call>")
DESCRIPTION = ("Generate bounded text from one message using the experimental local Uzu "
    "Qwen3.5 0.8B model. Maximum 256 output tokens and 4096 total context tokens; "
    "no tools or retrieval.")


def model_declaration(root: Path | None = None) -> dict:
    """Read the packaged declaration; the host pins its complete content."""
    root = Path(__file__).resolve().parents[2] if root is None else root
    try:
        with (root / "uzu-profile.json").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        model = json.loads(raw)
        if (type(model) is not dict or set(model) != {"artifact", "model_id", "sha256",
                "profile", "context_tokens", "engine_identity", "parameters"}
                or model["artifact"] != "models/uzu/model.safetensors"
                or model["model_id"] != "Qwen3.5-0.8B-Mirai-L"
                or model["profile"] != PROFILE or type(model["context_tokens"]) is not int
                or model["context_tokens"] != CONTEXT_TOKENS
                or type(model["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", model["sha256"]) is None
                or type(model["engine_identity"]) is not dict
                or set(model["engine_identity"]) != {"runtime", "version", "build", "binary_sha256"}
                or model["engine_identity"]["runtime"] != "uzu"
                or model["engine_identity"]["version"] != "0.6.1"
                or model["engine_identity"]["build"] != ENGINE_BUILD
                or type(model["engine_identity"]["binary_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", model["engine_identity"]["binary_sha256"]) is None
                or type(model["parameters"]) is not dict
                or set(model["parameters"]) != {"max_output_tokens", "queue_limit", "context_guard"}
                or any(type(value) is not int for value in model["parameters"].values())
                or model["parameters"] != {"max_output_tokens": MAX_OUTPUT_TOKENS,
                    "queue_limit": 4, "context_guard": CONTEXT_GUARD}):
            raise ValueError
        return model
    except Exception:
        raise TextGenerationError("invalid_request") from None


def request_fields(request: object) -> tuple[str, int]:
    if (type(request) is not dict or set(request) != {"text", "max_output_tokens"}
            or type(request["text"]) is not str or not request["text"].strip()
            or type(request["max_output_tokens"]) is not int
            or not 1 <= request["max_output_tokens"] <= MAX_OUTPUT_TOKENS):
        raise TextGenerationError("invalid_request")
    text = request["text"]
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise TextGenerationError("invalid_request") from None
    if len(text) > 8192 or size > MAX_INPUT_BYTES:
        raise TextGenerationError("input_limit")
    if any(marker in text for marker in SPECIAL_MARKERS):
        raise TextGenerationError("invalid_request")
    return text, request["max_output_tokens"]


class UzuTransport(LoopbackTransport):
    """Reuse the proxy-free, redirect-denying authenticated loopback opener."""
    def _request(self, path: str, payload: dict | None = None) -> dict:
        try:
            request = urllib.request.Request(self.endpoint + path,
                data=None if payload is None else json.dumps(payload, allow_nan=False).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as error:
                # The pinned Rust engine rejects this before native dispatch.
                # A timeout or any other error cannot establish settlement.
                try:
                    raw = error.read(MAX_RESPONSE_BYTES + 1)
                finally:
                    error.close()
                if (path == "/generate" and error.code == 400 and len(raw) <= MAX_RESPONSE_BYTES
                        and json.loads(raw) == {"error": "context_limit"}):
                    raise TextGenerationError("context_limit") from None
                raise TextGenerationError("inference_failed") from None
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TextGenerationError("invalid_engine_response")
            result = json.loads(raw)
            if type(result) is not dict:
                raise TextGenerationError("invalid_engine_response")
            return result
        except TextGenerationError:
            raise
        except Exception:
            raise TextGenerationError("inference_failed") from None

    def health(self) -> dict:
        return self._request("/health")

    def __call__(self, path: str, payload: dict) -> dict:
        if path != "/generate":
            raise TextGenerationError("invalid_request")
        return self._request(path, payload)


class GenerationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    ttft_seconds: float = Field(ge=0, le=120)
    prefill_seconds: float = Field(ge=0, le=120)
    decode_seconds: float = Field(ge=0, le=120)
    total_seconds: float = Field(ge=0, le=120)
    prefill_tps: float = Field(ge=0, le=1e9)
    decode_tps: float = Field(ge=0, le=1e9)


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str
    input_tokens: int = Field(ge=1, le=CONTEXT_TOKENS - CONTEXT_GUARD - 1)
    output_tokens: int = Field(ge=0, le=MAX_OUTPUT_TOKENS)
    stop_reason: Literal["eos", "stop", "length"]
    metrics: GenerationMetrics


class UzuTextGenerator(BoundedTextGenerator):
    def _generate(self, text: str, maximum: int) -> dict:
        result = self.transport("/generate", {"text": text, "max_output_tokens": maximum})
        try:
            if type(result) is not dict or type(result.get("metrics")) is not dict:
                raise ValueError
            result = dict(result)
            metrics = dict(result["metrics"])
            # The Rust benchmark adds only bounded identities and memory to its
            # reply. Validate them and keep the MCP result free of those fields.
            if "peak_memory_bytes" in result:
                memory = result.pop("peak_memory_bytes")
                if memory is not None and (type(memory) is not int or not 0 <= memory <= 2**63 - 1):
                    raise ValueError
                for key in ("prompt_sha256", "prompt_tokens_sha256"):
                    value = metrics.pop(key, None)
                    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                        raise ValueError
            if (set(metrics) != set(GenerationMetrics.model_fields)
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                        for value in metrics.values())):
                raise ValueError
            result["metrics"] = metrics
            parsed = GenerationResult.model_validate(result)
            if (parsed.input_tokens + maximum + CONTEXT_GUARD > CONTEXT_TOKENS
                    or parsed.stop_reason == "length" and parsed.output_tokens != maximum
                    or parsed.output_tokens == 0 and parsed.text
                    or any(marker in parsed.text for marker in SPECIAL_MARKERS)
                    or any(metrics[key] > metrics["total_seconds"] + .001
                        for key in ("ttft_seconds", "prefill_seconds", "decode_seconds"))):
                raise ValueError
            if parsed.output_tokens > maximum or len(parsed.text.encode("utf-8")) > maximum * 256:
                raise TextGenerationError("output_limit")
            return parsed.model_dump()
        except TextGenerationError:
            raise
        except Exception:
            raise TextGenerationError("invalid_engine_response") from None

    async def generate(self, request: object) -> dict:
        text, maximum = request_fields(request)
        if self.closed:
            raise TextGenerationError("engine_unavailable")
        if self.pending >= self.queue_limit:
            raise TextGenerationError("queue_full")
        self.pending += 1
        try:
            async with self.lock:
                if self.closed:
                    raise TextGenerationError("engine_unavailable")
                operation = asyncio.create_task(self._operation(text, maximum))
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # Keep admission until both transport and retirement settle.
                    # Cancellation of a client does not cancel a sibling engine.
                    with anyio.CancelScope(shield=True):
                        while not operation.done():
                            try:
                                await asyncio.shield(operation)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                    if not operation.cancelled():
                        operation.exception()
                    raise
        finally:
            self.pending -= 1


class AttachedUzuModel:
    def __init__(self, config):
        try:
            root = Path(config["package_dir"])
            if not root.is_absolute():
                raise ValueError
            self.model = model_declaration(root)
            if json.dumps(config["model"], sort_keys=True) != json.dumps(self.model, sort_keys=True):
                raise ValueError
            self.config, self.engine = config, config["dependencies"]["engine"]
            if (type(config["generation"]) is not int or config["generation"] < 1
                    or type(config["nonce"]) is not str or not config["nonce"]
                    or type(self.engine["nonce"]) is not str or not self.engine["nonce"]
                    or type(self.engine["instance_id"]) is not str or not self.engine["instance_id"]):
                raise ValueError
            self.transport = UzuTransport(self.engine["endpoint"], self.engine["api_key"], timeout=120)
        except Exception:
            raise TextGenerationError("invalid_request") from None
        self.fatal = asyncio.Event()
        self.ready = False
        self.adapter = UzuTextGenerator(self.transport, self.fence)

    async def fence(self):
        # Only the host can stop/reap the sibling. Exit and retain ownership.
        self.ready = False
        self.fatal.set()
        return False

    def _verify_engine(self):
        expected = {"ready": True, "generation": self.config["generation"], "nonce": self.engine["nonce"],
            "context_tokens": CONTEXT_TOKENS, "profile": PROFILE,
            "engine_identity": self.model["engine_identity"]}
        if json.dumps(self.transport.health(), sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True):
            raise TextGenerationError("invalid_engine_response")

    async def start(self):
        try:
            await asyncio.to_thread(self._verify_engine)
        except Exception:
            self.adapter.closed = True
            await self.fence()
            raise TextGenerationError("engine_unavailable") from None
        self.ready = True

    async def shutdown(self):
        self.ready = False
        self.adapter.closed = True
        async def settle():
            async with self.adapter.lock:
                pass
        operation = asyncio.create_task(settle())
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
            raise

    def health(self):
        return {"ready": self.ready and not self.adapter.closed,
            "generation": self.config["generation"], "nonce": self.config["nonce"]}

    def status(self):
        return {"ready": self.ready and not self.adapter.closed, "model_id": self.model["model_id"],
            "model_sha256": self.model["sha256"], "profile": PROFILE, "context_tokens": CONTEXT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS, "queue_depth": self.adapter.pending,
            "completed": self.adapter.completed, "generation": self.config["generation"],
            "engine_instance_id": self.engine["instance_id"]}

    async def generate(self, text, maximum):
        if not self.ready:
            raise TextGenerationError("engine_unavailable")
        return await self.adapter.generate({"text": text, "max_output_tokens": maximum})


class SafeUzuMCP(SafeTextMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            if name == "text_generate":
                request_fields(arguments)
            elif name != "text_status" or arguments:
                raise TextGenerationError("invalid_request")
            # SafeTextMCP enforces the smaller llama.cpp profile, so enter the
            # SDK directly after validating this profile's request boundary.
            return await super(SafeTextMCP, self).call_tool(name, arguments)
        except Exception as error:
            for _ in range(8):
                if isinstance(error, TextGenerationError):
                    raise ToolError(error.code) from None
                if error.__cause__ is None:
                    break
                error = error.__cause__
            raise ToolError("invalid_request") from None


def make_app(manager, token, *, port):
    mcp = SafeUzuMCP("Experimental Uzu text generation", instructions=DESCRIPTION,
        host="127.0.0.1", port=port, log_level="CRITICAL", stateless_http=False,
        json_response=True, max_request_body_size=32768, max_sessions=8, session_idle_timeout=300,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"]))

    @mcp.tool(description=DESCRIPTION, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def text_generate(text: Annotated[str, Field(strict=True, min_length=1, max_length=8192)],
            max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=MAX_OUTPUT_TOKENS)]) -> GenerationResult:
        return GenerationResult(**await manager.generate(text, max_output_tokens))

    @mcp.tool(description="Report the pinned experimental model identity, readiness, engine generation and bounded queue counts.",
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
