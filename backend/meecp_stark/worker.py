"""One reaper-owned, serialized Stark worker; protocol data never becomes a log."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import struct

from jevk5_app.contracts import ServiceError
from meecp.processes import ProcessOwner


MAX_BYTES = 1024 * 1024
_RECOVERABLE = {
    "invalid_request", "invalid_tokenization_request", "invalid_scoring_request",
    "context_overflow", "request_too_large",
}


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _ids(value, vocab):
    return isinstance(value, list) and all(type(t) is int and 0 <= t < vocab for t in value)


def _hash(ids):
    return hashlib.sha256(b"".join(struct.pack("<I", token) for token in ids)).hexdigest()


async def _settle(action):
    task = asyncio.ensure_future(action)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class StarkWorker:
    def __init__(self, root: Path, model: dict, *, owner=None, timeout=120.0, queue_limit=8):
        self.root = Path(root).resolve()
        artifact = Path(model["artifact"])
        if artifact.is_absolute() or ".." in artifact.parts:
            raise ValueError("invalid_model_artifact")
        self.model_path = (self.root / artifact).parent
        self.context_tokens = model["context_tokens"]
        if type(self.context_tokens) is not int or not 1 <= self.context_tokens <= 16384:
            raise ValueError("unsupported_model_profile")
        self.cache_enabled = model["parameters"].get("consecutive_cache") is True
        self.owner = owner if owner is not None else ProcessOwner(self.root / "native/meecp-reap")
        self.timeout = timeout
        self.queue_limit = queue_limit
        self.lock = asyncio.Lock()
        self.pending = 0
        self.process = None
        self.monitor = None
        self.ready = False
        self.stopping = False
        self.vocab_size = 0
        self.cache_policy = "consecutive-full-vocabulary-logits-v1" if self.cache_enabled else "fresh-state"

    def is_ready(self):
        process = self.process
        return bool(self.ready and not self.stopping and process is not None
                    and self.owner.holds_live_instances({process.instance_id}))

    async def start(self):
        if self.process is not None or self.stopping:
            raise ServiceError("engine_unavailable", "The managed engine is unavailable.")
        try:
            lock = _json((self.root / "engines/stark.lock.json").read_bytes())
            protocol = lock["protocol"]
            args = ["--score-model-path", str(self.model_path)]
            if self.cache_enabled:
                args.append("--consecutive-cache")
            # Native runtime configuration is package-bound. Capture switches and
            # credentials from the host environment are not inherited.
            environment = {key: value for key, value in os.environ.items()
                           if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
                                      "__CF_USER_TEXT_ENCODING"}}
            self.process = await self.owner.launch(self.root / "bin/stark-score", args, self.root,
                                                   environment, owned_group=True, io_mode="streams")
            async with asyncio.timeout(self.timeout):
                ready = await self._read()
            expected = {
                "ready": True, "engine": protocol["ready_engine"], "version": protocol["ready_version"],
                "mode": protocol["worker_mode"], "upstream_revision": protocol["upstream_revision"],
                "scoring_patch_sha256": protocol["scoring_patch_sha256"],
                "cache_enabled": self.cache_enabled, "cache_policy": self.cache_policy,
                "cache_key": protocol["cache_key"],
            }
            if (any(type(ready.get(key)) is not type(value) or ready.get(key) != value
                    for key, value in expected.items())
                    or type(ready.get("vocab_size")) is not int
                    or not 1 <= ready["vocab_size"] <= 2**32):
                raise ValueError()
            self.vocab_size = ready["vocab_size"]
            self._cache(ready)
            self.ready = True
            if not self.is_ready():
                raise ValueError()
            self.monitor = asyncio.create_task(self._watch())
        except asyncio.CancelledError:
            await _settle(self.close())
            raise
        except Exception:
            await _settle(self.close())
            raise ServiceError("engine_unavailable", "The managed engine is unavailable.") from None

    async def _watch(self):
        try:
            await self.process.wait()
        except Exception:
            pass
        finally:
            self.ready = False

    async def _read(self):
        if self.process is None or self.process.stdout is None:
            raise ValueError()
        raw = await self.process.stdout.readline()
        if not raw or len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
            raise ValueError()
        value = _json(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value

    def _cache(self, response):
        cache = response.get("cache")
        if (not isinstance(cache, dict) or type(cache.get("enabled")) is not bool
                or cache["enabled"] != self.cache_enabled
                or any(type(cache.get(key)) is not int or cache[key] < 0
                       for key in ("hits", "native_score_calls"))):
            raise ValueError()

    def _validate_reply(self, request, response):
        self._cache(response)
        operation = request["operation"]
        if operation == "reset_cache":
            if response.get("cache_reset") is not True:
                raise ValueError()
        elif operation == "tokenize":
            ids = response.get("token_ids")
            if (not _ids(ids, self.vocab_size) or not ids
                    or type(response.get("token_count")) is not int or response["token_count"] != len(ids)
                    or response.get("token_ids_sha256") != _hash(ids)
                    or response.get("vocab_size") != self.vocab_size):
                raise ValueError()
        elif operation == "score":
            candidates = request["candidate_token_ids"]
            probabilities = response.get("candidate_probabilities")
            metrics = response.get("metrics")
            if (type(response.get("input_tokens")) is not int
                    or response["input_tokens"] != len(request["input_token_ids"])
                    or response.get("prompt_tokens_sha256") != _hash(request["input_token_ids"])
                    or response.get("candidate_token_ids") != candidates
                    or response.get("temperature") != request["temperature"]
                    or response.get("truncated") is not False
                    or type(response.get("cache_hit")) is not bool
                    or (response["cache_hit"] and not self.cache_enabled)
                    or not isinstance(probabilities, list) or len(probabilities) != len(candidates)
                    or any(not _number(p) or not 0 <= p <= 1 for p in probabilities)
                    or not math.isclose(sum(probabilities), 1.0, rel_tol=0, abs_tol=1e-12)
                    or any(not isinstance(response.get(key), list) or len(response[key]) != len(candidates)
                           or any(not _number(p) for p in response[key])
                           for key in ("logits", "log_probabilities"))
                    or not isinstance(metrics, dict)
                    or any(not _number(metrics.get(key)) or metrics[key] < 0 for key in
                           ("state_creation_seconds", "prefill_seconds", "readout_seconds",
                            "score_seconds", "total_seconds"))):
                raise ValueError()

    async def _exchange(self, request):
        try:
            if not self.is_ready() or self.process.stdin is None:
                raise ValueError()
            encoded = json.dumps(request, allow_nan=False, separators=(",", ":")).encode() + b"\n"
            if len(encoded) > MAX_BYTES:
                await self._exchange({"operation": "reset_cache"})
                raise ServiceError("body_limit", "Request body is too large.", 413)
            async with asyncio.timeout(self.timeout):
                self.process.stdin.write(encoded)
                await self.process.stdin.drain()
                response = await self._read()
            if "error" in response:
                code = response["error"]
                self._cache(response)
                if code not in _RECOVERABLE:
                    raise ValueError()
                public_code = "context_limit" if code == "context_overflow" else "invalid_request"
                raise ServiceError(public_code, "The scoring request was rejected.", 400)
            self._validate_reply(request, response)
            return response
        except ServiceError:
            raise
        except Exception:
            self.ready = False
            await _settle(self.owner.close())
            raise ServiceError("inference_failed", "The local model could not complete this decision.") from None

    async def request(self, operation: str, payload: dict):
        if operation not in {"tokenize", "score", "reset_cache"}:
            raise ServiceError("invalid_request", "Invalid scoring operation.", 400)
        if self.pending >= self.queue_limit:
            raise ServiceError("queue_full", "The local decision queue is full. Retry shortly.", 429)
        self.pending += 1
        try:
            async with self.lock:
                action = asyncio.create_task(self._exchange({"operation": operation, **payload}))
                try:
                    return await asyncio.shield(action)
                except asyncio.CancelledError:
                    await _settle(asyncio.gather(action, return_exceptions=True))
                    # Keep the operation lock until the cancelled request settles
                    # and the native cache is cleared.
                    if self.is_ready():
                        await _settle(self._exchange({"operation": "reset_cache"}))
                    raise
        finally:
            self.pending -= 1

    async def close(self):
        self.ready = False
        self.stopping = True
        async with self.lock:
            await _settle(self.owner.close())
        if self.monitor is not None:
            await _settle(asyncio.gather(self.monitor, return_exceptions=True))
