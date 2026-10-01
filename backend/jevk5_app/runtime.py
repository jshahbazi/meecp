"""Single-model lifecycle and bounded, serialized inference."""
from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import httpx

from .contracts import ServiceError, validate_decision
from .scorer import LocalScorer
from .state import ENDPOINT, verified_model


class ModelManager:
    def __init__(self, resources: Path, state_dir: Path, queue_limit: int = 8):
        self.resources, self.state_dir = resources, state_dir
        self.queue_limit = queue_limit
        self.lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.state = "loading"
        self.paused = False
        self.stopping = False
        self.child: subprocess.Popen | None = None
        self.scorer: LocalScorer | None = None
        self.key_file: Path | None = None
        self.manifest: dict | None = None
        self.last_error: str | None = None
        self.pending = 0
        self.counts = {"decisions": 0, "cache_hits": 0, "errors": 0}
        self.load_task: asyncio.Task | None = None
        self.monitor_task: asyncio.Task | None = None
        self.restart_count = 0

    def model_identity(self):
        m = self.manifest or {}
        return {"id": m.get("model_id", "JevK5-4B-v0.3-Q8_0"), "sha256": m.get("sha256"),
                "profile": m.get("profile", "m5-benchmark-v1"), "context_tokens": m.get("context_tokens", 8192)}

    def status(self):
        running = self.child is not None and self.child.poll() is None
        ready = self.state == "ready" and running and not self.paused
        return {"state": self.state if running or self.state != "ready" else "error", "ready": ready,
                "paused": self.paused, "model": self.model_identity(), "endpoint": ENDPOINT,
                "mcp_endpoint": ENDPOINT + "/mcp", "counts": dict(self.counts), "queue_depth": self.pending,
                "last_error": self.last_error, "backend_pid": os.getpid(),
                "llama_pid": self.child.pid if running else None}

    async def start(self):
        self._schedule_load()
        self.monitor_task = asyncio.create_task(self._monitor())

    def _schedule_load(self):
        if self.stopping or self.paused or (self.load_task and not self.load_task.done()):
            return
        self.state, self.last_error = "loading", None
        self.load_task = asyncio.create_task(self._load())

    async def _load(self):
        async with self.lock:
            if self.stopping or self.paused:
                return
            try:
                await self._stop_child()
                self.manifest, model = await asyncio.to_thread(verified_model, self.resources, self.state_dir)
                if self.stopping or self.paused:
                    return
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                api_key = secrets.token_urlsafe(48)
                self.key_file = self.state_dir / ("llama-key-" + secrets.token_hex(12))
                descriptor = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(api_key + "\n")
                command = [str(self.resources / "llama" / "llama-server"), "--model", str(model),
                           "--host", "127.0.0.1", "--port", str(port), "--api-key-file", str(self.key_file),
                           "--ctx-size", "8192", "--n-gpu-layers", "99", "--parallel", "1",
                           "--flash-attn", "auto", "--batch-size", "2048", "--ubatch-size", "512",
                           "--no-context-shift"]
                environment = dict(os.environ)
                # This ggml build treats GGML_BACKEND_PATH as a single file. The
                # relocated runtime searches cwd for its bundled plugins.
                environment.pop("GGML_BACKEND_PATH", None)
                self.child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, env=environment,
                                              cwd=self.resources / "llama" / "plugins")
                url = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        if self.stopping or self.paused:
                            await self._stop_child()
                            return
                        if self.child.poll() is not None:
                            raise RuntimeError()
                        try:
                            health = await client.get(url + "/health", headers={"Authorization": "Bearer " + api_key})
                            if health.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.2)
                    else:
                        raise RuntimeError()
                self.scorer = LocalScorer(url, api_key, context_tokens=8192,
                                          temperature=self.manifest["temperature"],
                                          knockout_temperature=self.manifest["knockout_temperature"])
                self.state, self.last_error = "ready", None
            except Exception:
                await self._stop_child()
                if not self.paused and not self.stopping:
                    self.state, self.last_error = "error", "model_start_failed"

    async def _stop_child(self):
        if self.scorer:
            self.scorer.clear()
        self.scorer = None
        child, self.child = self.child, None
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
            else:
                child.wait()
        if self.key_file:
            self.key_file.unlink(missing_ok=True)
            self.key_file = None

    async def _monitor(self):
        while not self.stopping:
            await asyncio.sleep(1)
            if self.paused or (self.load_task and not self.load_task.done()):
                continue
            if self.child and self.child.poll() is not None:
                async with self.lock:
                    await self._stop_child()
                    self.state, self.last_error = "error", "model_crashed"
                if self.restart_count < 3:
                    self.restart_count += 1
                    self._schedule_load()

    async def control(self, action: str):
        async with self.control_lock:
            return await self._control(action)

    async def _control(self, action: str):
        if action == "pause":
            self.paused = True
            async with self.lock:
                await self._stop_child()
                self.state, self.last_error = "paused", None
        elif action == "resume":
            self.paused = False
            self.restart_count = 0
            if self.state != "ready":
                if self.load_task and not self.load_task.done():
                    await self.load_task
                self._schedule_load()
        else:
            raise ServiceError("invalid_control", "Unknown control action.", 400)
        return self.status()

    async def decide(self, state, question):
        state, question = validate_decision(state, question)
        if self.paused or self.stopping:
            raise ServiceError("paused", "The model is paused. Resume it from the menu bar.", 409)
        if self.pending >= self.queue_limit:
            raise ServiceError("queue_full", "The local decision queue is full. Retry shortly.", 429)
        self.pending += 1
        started = time.perf_counter()
        try:
            if self.state == "loading" and self.load_task:
                await asyncio.shield(self.load_task)
            # Startup owns this same lock, so callers may await readiness safely.
            async with self.lock:
                if self.paused or self.stopping:
                    raise ServiceError("paused", "The model is paused. Resume it from the menu bar.", 409)
                if self.state != "ready" or not self.scorer or not self.child or self.child.poll() is not None:
                    raise ServiceError("not_ready", "The local model is not ready.")
                try:
                    operation = asyncio.create_task(asyncio.to_thread(self.scorer.decide, state, question))
                    try:
                        result = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        # A disconnected client must not release serialization while its
                        # worker still owns the single-slot model.
                        await asyncio.gather(operation, return_exceptions=True)
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
        self.stopping, self.paused = True, True
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        if self.load_task:
            await asyncio.gather(self.load_task, return_exceptions=True)
        async with self.lock:
            await self._stop_child()
            self.state = "paused"
