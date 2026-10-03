import asyncio

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcpme_textgen.adapter import TextGenerationError
from mcpme_textgen.service import AttachedTextModel, make_app, model_declaration


def config():
    return {"model": model_declaration(), "generation": 3, "nonce": "synthetic-nonce",
            "dependencies": {"engine": {"endpoint": "http://127.0.0.1:1", "api_key": "synthetic",
                                         "instance_id": "synthetic-instance"}}}


async def test_fencing_never_claims_sibling_engine_settlement():
    manager = AttachedTextModel(config())
    manager.ready = True
    def failure(path, payload):
        raise TimeoutError("private response")
    manager.adapter.transport = failure
    with pytest.raises(TextGenerationError, match="^ownership_unsettled$"):
        await manager.generate("synthetic", 1)
    assert manager.fatal.is_set() and not manager.ready
    assert not manager.adapter.ownership_settled
    assert manager.health() == {"ready": False, "generation": 3, "nonce": "synthetic-nonce"}
    await manager.shutdown()
    assert not manager.adapter.ownership_settled


@pytest.mark.parametrize("patch", [{"context_tokens": 1024}, {"profile": "other"},
                                  {"parameters": {"max_output_tokens": 64, "queue_limit": 4, "context_guard": 9}}])
def test_untested_profile_rejected(patch):
    value = config()
    value["model"].update(patch)
    with pytest.raises(TextGenerationError):
        AttachedTextModel(value)


class Manager:
    async def generate(self, text, maximum):
        self.text = text
        return {"text": "synthetic", "input_tokens": 1, "output_tokens": 1, "stop_reason": "eos"}

    def health(self):
        return {"ready": True, "generation": 1, "nonce": "synthetic"}

    def status(self):
        return {"ready": True}


async def test_mcp_preserves_literal_strings_and_rejects_extra_fields():
    manager = Manager()
    _, app = make_app(manager, "synthetic", port=1)
    for text in ("true", "123", '{"a":1}'):
        await app.state.mcp.call_tool("text_generate", {"text": text, "max_output_tokens": 1})
        assert manager.text == text
    for arguments in ({"text": "secret", "max_output_tokens": True},
                      {"text": "secret", "max_output_tokens": 1, "extra": "secret"}):
        with pytest.raises(ToolError, match="^invalid_request$"):
            await app.state.mcp.call_tool("text_generate", arguments)
    with pytest.raises(ToolError, match="^invalid_request$"):
        await app.state.mcp.call_tool("text_status", {"extra": "secret"})


async def test_every_route_has_host_origin_authentication_and_body_bounds():
    wrapped, _ = make_app(Manager(), "synthetic", port=1234)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://127.0.0.1:1234") as client:
        assert (await client.get("/health")).status_code == 401
        auth = {"Authorization": "Bearer synthetic"}
        assert (await client.get("/health", headers=auth)).status_code == 200
        for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
            assert (await client.get("/health", headers={**auth, **extra})).status_code == 403
        assert (await client.post("/mcp", headers=auth, content=b"x" * 32769)).status_code == 413
        assert (await client.post("/mcp", headers=auth, content=b"private-invalid-envelope")).status_code == 400


async def test_shutdown_closes_admission_then_joins_generation():
    manager = AttachedTextModel(config())
    manager.ready = True
    await manager.adapter.lock.acquire()
    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    assert manager.adapter.closed and not manager.ready and not shutdown.done()
    manager.adapter.lock.release()
    await shutdown
