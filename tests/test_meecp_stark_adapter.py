"""Engine selection and generation fencing in the shared Jev adapter."""
import asyncio

import httpx
import pytest

from meecp_jevk5 import runtime
from test_meecp_jevk5 import QUESTION, config


def stark_config(cache=False):
    value = config()
    value["dependencies"]["engine"]["nonce"] = "owned-nonce"
    value["model"]["engine_identity"] = {"name": "stark", "revision": "a" * 40}
    value["model"]["profile"] = "jevk5-stark-bf16-v1"
    value["model"]["parameters"]["consecutive_cache"] = cache
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{}, {"nonce": "old"}, {"generation": 6}, {"generation": True},
                                    {"ready": False}, {"engine": "llama.cpp"}, {"cache_policy": "wrong"}])
async def test_stark_readiness_requires_authenticated_current_generation(monkeypatch, change):
    health = {"ready": True, "engine": "stark", "generation": 7, "nonce": "owned-nonce",
              "cache_policy": "fresh-state", **change}
    def handle(request):
        assert request.url.path == "/health"
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        return httpx.Response(200, json=health)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(runtime.httpx, "AsyncClient", lambda **kw: client_type(
        **kw, transport=httpx.MockTransport(handle)))
    assert await runtime.AttachedModel(stark_config())._engine_ready() is (not change)


@pytest.mark.asyncio
async def test_stark_dispatch_preserves_native_answer_and_cache_metadata(monkeypatch):
    result = {"type": "noul", "noul": 0.12345678912345678, "confidence": 0.7654321098765432,
              "input_tokens": 3}
    class Scorer:
        cache_hit = True
        cache_policy = "consecutive-full-vocabulary-logits-v1"
        last_seconds = .02
        def __init__(self, *args, **kwargs):
            assert kwargs["temperature"] == 1.22 and kwargs["knockout_temperature"] == .93
        def decide(self, state, question):
            return result
        def clear(self):
            pass
    async def ready(_self):
        return True
    monkeypatch.setattr(runtime, "StarkScorer", Scorer)
    monkeypatch.setattr(runtime.AttachedModel, "_engine_ready", ready)
    manager = runtime.AttachedModel(stark_config(True))
    await manager.start()
    try:
        response = await manager.decide("fixture", QUESTION)
        assert response["answer"] is result
        assert response["cache"] == {"hit": True, "policy": Scorer.cache_policy}
        status = manager.status()
        assert status["engine"] == "stark" and status["engine_pid"] == 123456
        assert "llama_pid" not in status
        assert response["model"]["engine"] == stark_config()["model"]["engine_identity"]
    finally:
        await manager.shutdown()


def test_llama_identity_and_status_contract_remain_available():
    manager = runtime.AttachedModel(config())
    assert manager.engine_name == "llama.cpp"
    assert manager.status()["llama_pid"] == 123456
    assert "engine" not in manager.model_identity()


def test_unknown_engine_cannot_fall_back_to_llama():
    value = stark_config()
    value["model"]["engine_identity"]["name"] = "unknown"
    with pytest.raises(ValueError, match="unsupported_model_engine"):
        runtime.AttachedModel(value)


@pytest.mark.asyncio
async def test_invalid_stark_decision_waits_for_active_call_then_resets_cache():
    from test_meecp_jevk5 import Scorer, entered
    manager = runtime.AttachedModel(stark_config(True))
    manager.ready = True
    manager.scorer = scorer = Scorer()
    scorer.cache_policy = "consecutive-full-vocabulary-logits-v1"
    active = asyncio.create_task(manager.decide("fixture", QUESTION))
    await entered(scorer)
    invalid = asyncio.create_task(manager.decide("fixture", {"type": "unknown"}))
    await asyncio.sleep(.01)
    assert not invalid.done() and scorer.cleared == 0
    scorer.release.set()
    await active
    from jevk5_app.contracts import ServiceError
    with pytest.raises(ServiceError):
        await invalid
    assert scorer.cleared == 1 and manager.pending == 0
    await manager.shutdown()
