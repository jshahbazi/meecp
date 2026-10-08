"""One serialized scorer attached to an externally owned engine generation."""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from jevk5_app.contracts import ServiceError, validate_decision
from jevk5_app.scorer import LocalScorer


class AttachedModel:
    def __init__(self, config: dict):
        self.config = config
        self.engine = config["dependencies"]["engine"]
        self.manifest = config["model"]
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
        return {"id": self.manifest["model_id"], "sha256": self.manifest["sha256"],
                "profile": self.manifest["profile"], "context_tokens": self.manifest["context_tokens"]}

    def status(self):
        endpoint = self.config["public_endpoint"]
        return {"state": "ready" if self.ready else "error", "ready": self.ready,
                "paused": self.stopping, "model": self.model_identity(), "endpoint": endpoint,
                "mcp_endpoint": endpoint + "/mcp", "counts": dict(self.counts),
                "queue_depth": self.pending, "last_error": self.last_error,
                "backend_pid": os.getpid(), "llama_pid": self.engine["pid"],
                "generation": self.config["generation"], "engine_instance_id": self.engine["instance_id"]}

    def health(self):
        return {"ready": self.ready, "generation": self.config["generation"], "nonce": self.config["nonce"]}

    async def _engine_ready(self):
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False) as client:
                response = await client.get(self.engine["endpoint"] + "/props",
                                            headers={"Authorization": "Bearer " + self.engine["api_key"]})
                return response.status_code == 200
        except Exception:
            return False

    async def start(self):
        if not await self._engine_ready():
            raise ServiceError("engine_unavailable", "The managed engine is unavailable.")
        self.scorer = LocalScorer(self.engine["endpoint"], self.engine["api_key"],
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
                        self.scorer.clear()
                return

    async def control(self, action: str):
        raise ServiceError("supervisor_control_required", "Control this model through its host.", 409)

    async def decide(self, state, question):
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
                        self.scorer.clear()
                        raise
                    self.counts["decisions"] += 1
                    hit = self.scorer.cache_hit
                    self.counts["cache_hits"] += int(hit)
                    return {"answer": result, "latency_ms": (time.perf_counter() - started) * 1000,
                            "engine_ms": self.scorer.last_seconds * 1000, "model": self.model_identity(),
                            "cache": {"hit": hit, "policy": "identical-consecutive"},
                            "input_tokens": result["input_tokens"]}
                except ServiceError:
                    self.counts["errors"] += 1
                    raise
                except Exception:
                    self.scorer.clear()
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
                self.scorer.clear()
            self.scorer = None
