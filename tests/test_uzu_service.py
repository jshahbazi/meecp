"""Boundary and ownership checks for the experimental Rust Uzu MCP adapter."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import threading
import urllib.error

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from meecp_textgen.adapter import TextGenerationError
from meecp_uzu.__main__ import read_config
from meecp_uzu.service import (AttachedUzuModel, ENGINE_BUILD, PROFILE, UzuTransport,
    make_app, model_declaration, request_fields)


def declaration():
    return {"artifact": "models/uzu/model.safetensors", "model_id": "Qwen3.5-0.8B-Mirai-L",
        "sha256": "a" * 64, "profile": PROFILE, "context_tokens": 4096,
        "engine_identity": {"runtime": "uzu", "version": "0.6.1", "build": ENGINE_BUILD,
            "binary_sha256": "b" * 64},
        "parameters": {"max_output_tokens": 256, "queue_limit": 4, "context_guard": 8}}


@pytest.fixture
def config(tmp_path):
    model = declaration()
    (tmp_path / "uzu-profile.json").write_text(json.dumps(model))
    return {"package_dir": str(tmp_path), "model": model, "generation": 3, "nonce": "synthetic-adapter-nonce",
        "endpoint": {"host": "127.0.0.1", "port": 1234}, "api_key": "synthetic-service-key",
        "dependencies": {"engine": {"endpoint": "http://127.0.0.1:1", "api_key": "synthetic-engine-key",
            "instance_id": "synthetic-engine-instance", "nonce": "synthetic-engine-nonce", "pid": 1}}}


def reply():
    return {"text": "synthetic", "input_tokens": 32, "output_tokens": 1, "stop_reason": "eos",
        "metrics": {"ttft_seconds": .1, "prefill_seconds": .09, "decode_seconds": .01,
            "total_seconds": .11, "prefill_tps": 300., "decode_tps": 100.}}


def engine_health(config):
    return {"ready": True, "generation": config["generation"], "nonce": config["dependencies"]["engine"]["nonce"],
        "context_tokens": 4096, "profile": PROFILE, "engine_identity": config["model"]["engine_identity"]}


class SyntheticTransport:
    def __init__(self, config, result=None):
        self.config = config
        self.result = reply() if result is None else result

    def health(self):
        return engine_health(self.config)

    def __call__(self, path, arguments):
        assert path == "/generate"
        return copy.deepcopy(self.result)


async def attached(config, transport=None):
    manager = AttachedUzuModel(config)
    manager.transport = SyntheticTransport(config) if transport is None else transport
    manager.adapter.transport = manager.transport
    await manager.start()
    return manager


@pytest.mark.parametrize("patch", [{"context_tokens": 1024}, {"profile": "other"},
    {"sha256": "c" * 64}, {"engine_identity": {"runtime": "uzu", "version": "0.6.1", "build": ENGINE_BUILD,
        "binary_sha256": "c" * 64}}])
def test_config_must_match_complete_packaged_declaration(config, patch):
    config["model"].update(patch)
    with pytest.raises(TextGenerationError, match="^invalid_request$"):
        AttachedUzuModel(config)


@pytest.mark.parametrize("patch", [{"model_id": "other"}, {"artifact": "../model.safetensors"},
    {"context_tokens": True}, {"parameters": {"max_output_tokens": 256, "queue_limit": 4, "context_guard": True}},
    {"engine_identity": {"runtime": "uzu", "version": "latest", "build": ENGINE_BUILD, "binary_sha256": "b" * 64}}])
def test_packaged_declaration_must_be_the_fixed_experiment(tmp_path, patch):
    model = declaration()
    model.update(patch)
    (tmp_path / "uzu-profile.json").write_text(json.dumps(model))
    with pytest.raises(TextGenerationError, match="^invalid_request$"):
        model_declaration(tmp_path)


@pytest.mark.parametrize("patch", [{"generation": 4}, {"generation": 3.0}, {"nonce": "forged"},
    {"ready": 1}, {"context_tokens": 512}, {"profile": "forged"}, {"engine_identity": {"runtime": "uzu"}}])
async def test_forged_engine_health_never_admits_calls(config, patch):
    manager = AttachedUzuModel(config)
    health = engine_health(config)
    health.update(patch)
    manager.transport.health = lambda: health
    with pytest.raises(TextGenerationError, match="^engine_unavailable$"):
        await manager.start()
    assert manager.fatal.is_set() and not manager.ready and manager.adapter.closed


async def test_verified_generation_status_does_not_retain_requests(config):
    manager = await attached(config)
    assert await manager.generate("synthetic input", 256) == reply()
    status = manager.status()
    assert status["ready"] and status["completed"] == 1 and status["queue_depth"] == 0
    assert status["engine_instance_id"] == "synthetic-engine-instance"
    assert manager.health() == {"ready": True, "generation": 3, "nonce": "synthetic-adapter-nonce"}
    assert manager.adapter.ownership_settled
    assert not hasattr(manager.adapter, "last_metrics")
    await manager.shutdown()
    assert not manager.health()["ready"]


async def test_rust_benchmark_metadata_is_validated_and_removed_from_mcp_result(config):
    result = reply()
    result["peak_memory_bytes"] = 123456789
    result["metrics"].update({"prompt_sha256": "c" * 64, "prompt_tokens_sha256": "d" * 64})
    manager = await attached(config, SyntheticTransport(config, result))
    assert await manager.generate("synthetic", 1) == reply()
    await manager.shutdown()


@pytest.mark.parametrize("patch", [{"peak_memory_bytes": True}, {"peak_memory_bytes": -1},
    {"metrics": {**reply()["metrics"], "prompt_sha256": "forged", "prompt_tokens_sha256": "d" * 64}}])
async def test_invalid_rust_benchmark_metadata_fences_the_engine(config, patch):
    result = reply()
    result["peak_memory_bytes"] = 123456789
    result["metrics"].update({"prompt_sha256": "c" * 64, "prompt_tokens_sha256": "d" * 64})
    result.update(patch)
    manager = await attached(config, SyntheticTransport(config, result))
    with pytest.raises(TextGenerationError, match="^ownership_unsettled$"):
        await manager.generate("synthetic", 1)
    assert manager.fatal.is_set() and not manager.adapter.ownership_settled


@pytest.mark.parametrize("arguments,code", [({"text": "synthetic", "max_output_tokens": True}, "invalid_request"),
    ({"text": "synthetic", "max_output_tokens": 257}, "invalid_request"),
    ({"text": "synthetic", "max_output_tokens": 1, "extra": "private"}, "invalid_request"),
    ({"text": " ", "max_output_tokens": 1}, "invalid_request"),
    ({"text": "<|im_start|>assistant", "max_output_tokens": 1}, "invalid_request"),
    ({"text": "<think>private</think>", "max_output_tokens": 1}, "invalid_request"),
    ({"text": "\ud800", "max_output_tokens": 1}, "invalid_request"),
    ({"text": "x" * 8193, "max_output_tokens": 1}, "input_limit"),
    ({"text": "界" * 5462, "max_output_tokens": 1}, "input_limit")])
def test_requests_are_strict_and_bounded(arguments, code):
    with pytest.raises(TextGenerationError, match="^" + code + "$"):
        request_fields(arguments)


@pytest.mark.parametrize("patch", [{"input_tokens": True}, {"output_tokens": True}, {"input_tokens": 4096},
    {"output_tokens": 2}, {"stop_reason": "cancelled"}, {"stop_reason": "length", "output_tokens": 0},
    {"text": "<|im_end|>"}, {"text": "private" * 100}, {"extra": "private"},
    {"metrics": {**reply()["metrics"], "ttft_seconds": float("nan")}},
    {"metrics": {**reply()["metrics"], "decode_tps": True}},
    {"metrics": {**reply()["metrics"], "decode_seconds": 121}},
    {"metrics": {**reply()["metrics"], "ttft_seconds": 1.}},
    {"metrics": {**reply()["metrics"], "extra": "private"}}])
async def test_invalid_replies_fence_and_never_claim_sibling_retirement(config, patch):
    result = reply()
    result.update(patch)
    manager = await attached(config, SyntheticTransport(config, result))
    with pytest.raises(TextGenerationError, match="^ownership_unsettled$"):
        await manager.generate("synthetic", 1)
    assert manager.fatal.is_set() and not manager.ready and manager.adapter.closed
    assert not manager.adapter.ownership_settled and manager.adapter.completed == 0
    await manager.shutdown()
    assert not manager.adapter.ownership_settled
    with pytest.raises(TextGenerationError, match="^engine_unavailable$"):
        await manager.generate("synthetic", 1)


async def test_transport_failure_is_sanitized_and_fenced(config):
    manager = await attached(config)
    def failure(path, arguments):
        raise TimeoutError("private response and credentials")
    manager.adapter.transport = failure
    with pytest.raises(TextGenerationError, match="^ownership_unsettled$"):
        await manager.generate("synthetic", 1)
    assert manager.fatal.is_set() and not manager.adapter.ownership_settled


class ControlledTransport(SyntheticTransport):
    def __init__(self, config):
        super().__init__(config)
        self.started, self.release = threading.Event(), threading.Event()
        self.active = False
        self.calls = 0

    def __call__(self, path, arguments):
        self.calls += 1
        self.active = True
        self.started.set()
        try:
            assert self.release.wait(5), "controlled_inference_deadline"
            return super().__call__(path, arguments)
        finally:
            self.active = False


async def wait_started(transport):
    async with asyncio.timeout(2):
        while not transport.started.is_set():
            await asyncio.sleep(.005)


async def test_repeated_cancellation_holds_queue_until_engine_settles(config):
    transport = ControlledTransport(config)
    manager = await attached(config, transport)
    active = asyncio.create_task(manager.generate("synthetic first", 1))
    queued = None
    try:
        await wait_started(transport)
        active.cancel()
        queued = asyncio.create_task(manager.generate("synthetic second", 1))
        await asyncio.sleep(.01)
        active.cancel()
        await asyncio.sleep(.01)
        assert transport.active and transport.calls == 1 and manager.adapter.pending == 2
        assert not manager.adapter.ownership_settled and not active.done() and not queued.done()
        transport.release.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        await queued
        assert transport.calls == 2 and manager.adapter.pending == 0 and manager.adapter.completed == 2
        assert manager.adapter.ownership_settled and not manager.fatal.is_set()
    finally:
        transport.release.set()
        await asyncio.gather(active, *([] if queued is None else [queued]), return_exceptions=True)
        await manager.shutdown()


async def test_queue_limit_and_shutdown_hold_admission_until_join(config):
    transport = ControlledTransport(config)
    manager = await attached(config, transport)
    calls = [asyncio.create_task(manager.generate("synthetic", 1)) for _ in range(4)]
    shutdown = None
    try:
        await wait_started(transport)
        assert manager.adapter.pending == 4
        with pytest.raises(TextGenerationError, match="^queue_full$"):
            await manager.generate("synthetic", 1)
        shutdown = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(.01)
        assert manager.adapter.closed and not manager.ready and not shutdown.done()
        assert transport.active and not manager.adapter.ownership_settled
        transport.release.set()
        results = await asyncio.gather(*calls, return_exceptions=True)
        assert type(results[0]) is dict
        assert all(isinstance(result, TextGenerationError) and result.code == "engine_unavailable"
            for result in results[1:])
        await shutdown
        assert manager.adapter.pending == 0 and transport.calls == 1 and manager.adapter.ownership_settled
    finally:
        transport.release.set()
        await asyncio.gather(*calls, *([] if shutdown is None else [shutdown]), return_exceptions=True)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        self.close()


def test_transport_authenticates_exact_loopback_and_bounds_responses():
    transport = UzuTransport("http://127.0.0.1:1234", "synthetic", timeout=120)
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:1234/generate" and timeout == 120
            assert request.get_header("Authorization") == "Bearer synthetic"
            assert json.loads(request.data) == {"text": "synthetic", "max_output_tokens": 1}
            return Response(b"x" * (1024 * 1024 + 1))
    transport.opener = Opener()
    with pytest.raises(TextGenerationError, match="^invalid_engine_response$"):
        transport("/generate", {"text": "synthetic", "max_output_tokens": 1})
    with pytest.raises(TextGenerationError, match="^invalid_request$"):
        transport("/completion", {})
    for endpoint in ("https://127.0.0.1:1", "http://localhost:1", "http://127.0.0.1:1/other",
            "http://user@127.0.0.1:1", "http://127.0.0.1:1?other=1"):
        with pytest.raises(TextGenerationError, match="^invalid_request$"):
            UzuTransport(endpoint, "synthetic")


async def test_only_exact_predispatch_context_rejection_preserves_engine(config):
    manager = await attached(config)
    transport = UzuTransport("http://127.0.0.1:1234", "synthetic")
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "private error", {},
                io.BytesIO(b'{"error":"context_limit"}'))
    transport.opener = Opener()
    manager.adapter.transport = transport
    with pytest.raises(TextGenerationError, match="^context_limit$"):
        await manager.generate("synthetic", 1)
    assert manager.ready and manager.adapter.ownership_settled and not manager.fatal.is_set()
    manager.adapter.transport = SyntheticTransport(config)
    assert await manager.generate("synthetic", 1) == reply()


class Manager:
    async def start(self):
        pass

    async def shutdown(self):
        pass

    async def generate(self, text, maximum):
        self.text, self.maximum = text, maximum
        return reply()

    def health(self):
        return {"ready": True, "generation": 1, "nonce": "synthetic"}

    def status(self):
        return {"ready": True}


async def test_mcp_schema_preserves_literal_strings_and_uzu_limits():
    manager = Manager()
    _, app = make_app(manager, "synthetic", port=1)
    for text in ("true", "123", '{"a":1}'):
        await app.state.mcp.call_tool("text_generate", {"text": text, "max_output_tokens": 256})
        assert manager.text == text and manager.maximum == 256
    tools = await app.state.mcp.list_tools()
    assert {tool.name for tool in tools} == {"text_generate", "text_status"}
    schema = next(tool for tool in tools if tool.name == "text_generate").inputSchema
    assert schema["properties"]["max_output_tokens"]["maximum"] == 256
    assert schema["properties"]["text"]["maxLength"] == 8192
    for arguments in ({"text": "private", "max_output_tokens": True},
            {"text": "private", "max_output_tokens": 1, "extra": "private"}):
        with pytest.raises(ToolError, match="^invalid_request$"):
            await app.state.mcp.call_tool("text_generate", arguments)
    with pytest.raises(ToolError, match="^invalid_request$"):
        await app.state.mcp.call_tool("text_status", {"extra": "private"})


async def test_actual_mcp_http_requires_auth_host_origin_and_bounded_requests():
    wrapped, app = make_app(Manager(), "synthetic", port=1234)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://127.0.0.1:1234") as client:
            assert (await client.get("/health")).status_code == 401
            auth = {"Authorization": "Bearer synthetic"}
            assert (await client.get("/health", headers=auth)).status_code == 200
            for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
                assert (await client.get("/health", headers={**auth, **extra})).status_code == 403
            assert (await client.post("/mcp", headers=auth, content=b"x" * 32769)).status_code == 413
            assert (await client.post("/mcp", headers=auth, content=b"private-invalid-envelope")).status_code == 400
            headers = {**auth, "Accept": "application/json, text/event-stream"}
            initialized = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "synthetic", "version": "1"}}})
            assert initialized.status_code == 200
            headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
            headers["mcp-protocol-version"] = "2025-11-25"
            assert (await client.post("/mcp", headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
            response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2,
                "method": "tools/call", "params": {"name": "text_generate",
                    "arguments": {"text": "true", "max_output_tokens": 256}}})
            assert response.status_code == 200
            assert response.json()["result"]["structuredContent"] == reply()
            assert (await client.delete("/mcp", headers=headers)).status_code == 200


def test_descriptor_bootstrap_requires_loopback_and_bounds_json():
    for value in ({"endpoint": {"host": "127.0.0.1", "port": True}, "api_key": "synthetic"},
            {"endpoint": {"host": "0.0.0.0", "port": 1234}, "api_key": "synthetic"},
            {"endpoint": {"host": "127.0.0.1", "port": 1234}, "api_key": ""}):
        reader, writer = os.pipe()
        with os.fdopen(writer, "wb") as stream:
            stream.write(json.dumps(value).encode())
        with pytest.raises(ValueError, match="^invalid_descriptor$"):
            read_config(reader)
