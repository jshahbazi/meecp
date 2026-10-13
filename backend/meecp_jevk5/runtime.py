"""One serialized scorer attached to an externally owned engine generation."""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from jevk5_app.contracts import ServiceError, validate_decision
from jevk5_app.scorer import LocalScorer
from jevk5_app.stark_scorer import StarkScorer


class AttachedModel:
    def __init__(self, config: dict):
        self.config = config
        self.engine = config["dependencies"]["engine"]
        self.manifest = config["model"]
        identity = self.manifest.get("engine_identity", {})
        self.engine_name = identity.get("name", identity.get("runtime", "llama.cpp"))
        if self.engine_name not in {"llama.cpp", "stark"}:
            raise ValueError("unsupported_model_engine")
        if self.engine_name == "stark" and self.manifest["profile"] != "jevk5-stark-bf16-v1":
            raise ValueError("unsupported_model_profile")
        parameters = self.manifest["parameters"]
        if (parameters["temperature"], parameters["knockout_temperature"], self.manifest["context_tokens"]) != (1.22, 0.93, 8192):
            raise ValueError("unsupported_model_profile")
        self.lock = asyncio.Lock()
        self.pending = 0
        self.queue_limit = 8
        self.counts = {"decisions": 0, "cache_hits": 0, "errors": 0}
        self.ready = False
        self.stopping = False
        self.last_error = None
        self.scorer = None
        self.monitor = None

    def model_identity(self):
        identity = {"id": self.manifest["model_id"], "sha256": self.manifest["sha256"],
                    "profile": self.manifest["profile"], "context_tokens": self.manifest["context_tokens"]}
        if self.engine_name == "stark":
            identity["engine"] = dict(self.manifest["engine_identity"])
        return identity

    def status(self):
        endpoint = self.config["public_endpoint"]
        value = {"state": "ready" if self.ready else "error", "ready": self.ready,
                "paused": self.stopping, "model": self.model_identity(), "endpoint": endpoint,
                "mcp_endpoint": endpoint + "/mcp", "counts": dict(self.counts),
                "queue_depth": self.pending, "last_error": self.last_error,
                "backend_pid": os.getpid(),
                "generation": self.config["generation"], "engine_instance_id": self.engine["instance_id"]}
        if self.engine_name == "stark":
            value.update(engine="stark", engine_pid=self.engine["pid"])
        else:
            value["llama_pid"] = self.engine["pid"]
        return value

    def health(self):
        return {"ready": self.ready, "generation": self.config["generation"], "nonce": self.config["nonce"]}

    async def _engine_ready(self):
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as client:
                path = "/health" if self.engine_name == "stark" else "/props"
                response = await client.get(self.engine["endpoint"] + path,
                                            headers={"Authorization": "Bearer " + self.engine["api_key"]})
                if response.status_code != 200:
                    return False
                if self.engine_name == "stark":
                    health = response.json()
                    policy = ("consecutive-full-vocabulary-logits-v1"
                              if self.manifest["parameters"].get("consecutive_cache") is True else "fresh-state")
                    return (health.get("ready") is True and health.get("engine") == "stark"
                            and type(health.get("generation")) is int
                            and health["generation"] == self.config["generation"]
                            and health.get("nonce") == self.engine["nonce"]
                            and health.get("cache_policy") == policy)
                return True
        except Exception:
            return False

    async def start(self):
        if not await self._engine_ready():
            raise ServiceError("engine_unavailable", "The managed engine is unavailable.")
        scorer_type = StarkScorer if self.engine_name == "stark" else LocalScorer
        self.scorer = scorer_type(self.engine["endpoint"], self.engine["api_key"],
                                 context_tokens=self.manifest["context_tokens"],
                                 temperature=self.manifest["parameters"]["temperature"],
                                 knockout_temperature=self.manifest["parameters"]["knockout_temperature"])
        self.ready = True
        self.monitor = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while not self.stopping:
            await asyncio.sleep(0.5)
            if not await self._engine_ready():
                self.ready = False
                self.last_error = "engine_unavailable"
                async with self.lock:
                    if self.scorer:
                        await self._clear()
                return

    async def _clear(self):
        if self.engine_name != "stark":
            self.scorer.clear()
            return
        # The private reset is a bounded HTTP operation. Keep the decision slot
        # until it settles, while allowing health and status to remain responsive.
        action = asyncio.create_task(asyncio.to_thread(self.scorer.clear))
        while True:
            try:
                await asyncio.shield(action)
                return
            except asyncio.CancelledError:
                if action.done():
                    action.result()
                    return

    async def control(self, action: str):
        raise ServiceError("supervisor_control_required", "Control this model through its host.", 409)

    async def decide(self, state, question):
        if self.engine_name != "stark":
            state, question = validate_decision(state, question)
        if self.stopping:
            raise ServiceError("paused", "The model is paused. Resume it from the menu bar.", 409)
        if self.pending >= self.queue_limit:
            raise ServiceError("queue_full", "The local decision queue is full. Retry shortly.", 429)
        self.pending += 1
        started = time.perf_counter()
        try:
            async with self.lock:
                if self.stopping:
                    raise ServiceError("paused", "The model is paused. Resume it from the menu bar.", 409)
                if not self.ready or self.scorer is None:
                    raise ServiceError("not_ready", "The local model is not ready.")
                try:
                    if self.engine_name == "stark":
                        state, question = validate_decision(state, question)
                    operation = asyncio.create_task(asyncio.to_thread(self.scorer.decide, state, question))
                    try:
                        result = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        # Cancellation never releases the single engine while its
                        # blocking operation still runs or retains a token cache.
                        settled = asyncio.gather(operation, return_exceptions=True)
                        while not settled.done():
                            try:
                                await asyncio.shield(settled)
                            except asyncio.CancelledError:
                                continue
                        await self._clear()
                        raise
                    self.counts["decisions"] += 1
                    hit = self.scorer.cache_hit
                    self.counts["cache_hits"] += int(hit)
                    return {"answer": result, "latency_ms": (time.perf_counter() - started) * 1000,
                            "engine_ms": self.scorer.last_seconds * 1000, "model": self.model_identity(),
                            "cache": {"hit": hit, "policy": (self.scorer.cache_policy if self.engine_name == "stark"
                                                              else "identical-consecutive")},
                            "input_tokens": result["input_tokens"]}
                except ServiceError:
                    if self.engine_name == "stark":
                        await self._clear()
                    self.counts["errors"] += 1
                    raise
                except Exception:
                    await self._clear()
                    self.counts["errors"] += 1
                    raise ServiceError("inference_failed", "The local model could not complete this decision.") from None
        finally:
            self.pending -= 1

    async def shutdown(self):
        self.ready = False
        self.stopping = True
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        async with self.lock:
            if self.scorer:
                await self._clear()
            self.scorer = None
