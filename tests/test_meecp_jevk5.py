import asyncio
import threading
import json
from pathlib import Path

import pytest

from jevk5_app.contracts import ServiceError
from jevk5_app.server import make_app
from mcp.server.lowlevel import NotificationOptions
from meecp_jevk5.runtime import AttachedModel


QUESTION = {"type": "noul", "instructions": "Synthetic proposition?"}


def config():
    return {"generation": 7, "nonce": "synthetic-generation", "public_endpoint": "http://127.0.0.1:19385",
            "dependencies": {"engine": {"endpoint": "http://127.0.0.1:12345", "api_key": "synthetic-key",
                                        "pid": 123456, "instance_id": "owned-engine"}},
            "model": {"model_id": "JevK5-4B-v0.3-Q8_0", "sha256": "a" * 64, "profile": "m5-benchmark-v1",
                      "context_tokens": 8192, "parameters": {"temperature": 1.22, "knockout_temperature": 0.93}}}


class Scorer:
    last_seconds = 0.01
    cache_hit = False

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = self.max_active = self.cleared = 0

    def clear(self):
        self.cleared += 1

    def decide(self, state, question):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        self.release.wait(3)
        self.active -= 1
        return {"type": "noul", "noul": 0.5, "confidence": 0.5, "input_tokens": 3}


async def entered(scorer):
    async with asyncio.timeout(2):
        while not scorer.entered.is_set():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_adapter_keeps_original_tool_contracts_and_advertises_only_tools():
    _, legacy = make_app(None, "synthetic")
    _, adapter = make_app(None, "synthetic", port=23456, tools_only=True)
    assert await legacy.state.mcp.list_tools() == await adapter.state.mcp.list_tools()
    capabilities = adapter.state.mcp._mcp_server.get_capabilities(NotificationOptions(), {})
    assert set(capabilities.model_dump(exclude_none=True)) == {"tools"}
    contract = json.loads((Path(__file__).resolve().parents[1] / "backend/meecp/legacy_jevk5_contract.json").read_text())
    assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in await legacy.state.mcp.list_tools()] == contract["tools"]


@pytest.mark.asyncio
async def test_attached_calls_serialize_and_shutdown_drains_without_engine_ownership():
    manager = AttachedModel(config())
    manager.ready = True
    manager.scorer = scorer = Scorer()
    manager.queue_limit = 2
    first = asyncio.create_task(manager.decide("fixture", QUESTION))
    await entered(scorer)
    second = asyncio.create_task(manager.decide("fixture", QUESTION))
    await asyncio.sleep(0)
    with pytest.raises(ServiceError, match="queue"):
        await manager.decide("fixture", QUESTION)
    stop = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0.01)
    assert not stop.done()
    scorer.release.set()
    result = await first
    assert result["cache"]["policy"] == "identical-consecutive"
    with pytest.raises(ServiceError, match="paused"):
        await second
    await stop
    assert scorer.max_active == 1 and scorer.cleared == 1
    assert not hasattr(manager, "child") and manager.pending == 0


@pytest.mark.asyncio
async def test_attached_cancel_holds_serialization_until_blocking_call_finishes():
    manager = AttachedModel(config())
    manager.ready = True
    manager.scorer = scorer = Scorer()
    first = asyncio.create_task(manager.decide("fixture", QUESTION))
    await entered(scorer)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(manager.decide("fixture", QUESTION))
    await asyncio.sleep(0.02)
    assert scorer.active == 1 and not second.done()
    scorer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert scorer.max_active == 1 and scorer.cleared == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_engine_loss_invalidates_readiness_and_cache(monkeypatch):
    manager = AttachedModel(config())
    manager.ready = True
    manager.scorer = scorer = Scorer()
    async def unavailable():
        return False
    monkeypatch.setattr(manager, "_engine_ready", unavailable)
    await manager._monitor()
    assert not manager.health()["ready"] and manager.health()["generation"] == 7
    assert scorer.cleared == 1 and manager.status()["last_error"] == "engine_unavailable"
    with pytest.raises(ServiceError, match="not ready"):
        await manager.decide("fixture", QUESTION)


@pytest.mark.asyncio
async def test_adapter_cannot_control_or_launch_engine():
    manager = AttachedModel(config())
    with pytest.raises(ServiceError) as error:
        await manager.control("resume")
    assert error.value.code == "supervisor_control_required"


def test_unsupported_scoring_profile_rejected_before_ready():
    value = config()
    value["model"]["parameters"]["temperature"] = 1.0
    with pytest.raises(ValueError):
        AttachedModel(value)
