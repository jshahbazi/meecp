"""Pinned Jev prompt and answer mapping with native Stark probabilities."""
from __future__ import annotations

import math

from jevk5.prompt import LETTERS, answer, decision_options, prompt_text

from .contracts import ServiceError, validate_decision
from .scorer import LocalScorer


class StarkScorer(LocalScorer):
    def __init__(self, url: str, api_key: str, context_tokens: int, *,
                 temperature: float = 1.22, knockout_temperature: float = 0.93):
        try:
            if (type(context_tokens) is not int or not 1 <= context_tokens <= 16384
                    or not isinstance(api_key, str) or not api_key or len(api_key) > 65536
                    or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
                    or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                           for value in (temperature, knockout_temperature))):
                raise ValueError()
            super().__init__(url, api_key, context_tokens, temperature=temperature,
                             knockout_temperature=knockout_temperature)
        except Exception:
            raise ServiceError("unsupported_model_profile", "The local model configuration is unsupported.") from None
        self.last_seconds = 0.0
        self.last_pass_tokens = 0
        self.cache_policy = "fresh-state"
        self._candidate_ids = None
        self._needs_reset = False

    def _transport(self, path, payload):
        if path not in {"/tokenize", "/score", "/reset_cache"}:
            raise ValueError("unsupported_stark_path")
        # Reuse the proxy-disabled, redirect-disabled loopback opener, without
        # the llama.cpp-only path allowlist in LocalScorer._transport.
        import json
        import urllib.request
        request = urllib.request.Request(self.url + path,
                                        data=json.dumps(payload, allow_nan=False).encode(),
                                        headers={"Content-Type": "application/json",
                                                 "Authorization": "Bearer " + self.api_key})
        timeout = min(self.timeout_s, 2.0) if path == "/reset_cache" else self.timeout_s
        with self._opener.open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError()
        return json.loads(raw)

    def clear(self):
        super().clear()
        self._needs_reset = True
        try:
            if self._post("/reset_cache", {}).get("cache_reset") is True:
                self._needs_reset = False
        except Exception:
            # A failed transport does not confirm native cache invalidation.
            # The next decision must confirm reset before attempting scoring.
            pass

    def _tokenize(self, text):
        out = self._post("/tokenize", {"text": text, "add_special_tokens": False})
        ids = out["token_ids"]
        if not isinstance(ids, list) or not ids or any(type(t) is not int or t < 0 for t in ids):
            raise ServiceError("invalid_model_response", "The model returned an invalid token sequence.")
        return ids

    def probabilities(self, state, question):
        self.cache_hit = False
        self.last_seconds = 0.0
        self.last_pass_tokens = 0
        try:
            state, question = validate_decision(state, question)
            if self._needs_reset:
                if self._post("/reset_cache", {}).get("cache_reset") is not True:
                    raise ServiceError("engine_unavailable", "The managed engine is unavailable.")
                self._needs_reset = False
            options = decision_options(question)
            if not 2 <= len(options) <= 16:
                raise ServiceError("invalid_decision", "Invalid evidence or question criteria.", 400)
            prompt = prompt_text(state, question["instructions"], [text for _, text in options])
            tokens = self._tokenize(prompt)
            if len(tokens) > self.context_tokens:
                raise ServiceError("context_limit", "The decision exceeds the model context budget.", 400)
            if self._candidate_ids is None:
                candidates = []
                for letter in LETTERS:
                    ids = self._tokenize(letter)
                    if len(ids) != 1 or ids[0] in candidates:
                        raise ServiceError("incomplete_options", "The model did not cover every declared option.")
                    candidates.append(ids[0])
                self._candidate_ids = tuple(candidates)
            candidates = list(self._candidate_ids[:len(options)])
            out = self._post("/score", {"input_token_ids": tokens, "candidate_token_ids": candidates,
                                        "context_tokens": self.context_tokens, "temperature": self.temperature})
            probs = out["candidate_probabilities"]
            seconds = out["metrics"]["total_seconds"]
            if (out.get("truncated") is not False or out.get("candidate_token_ids") != candidates
                    or type(out.get("input_tokens")) is not int or out["input_tokens"] != len(tokens)
                    or out.get("temperature") != self.temperature
                    or type(out.get("cache_hit")) is not bool
                    or not isinstance(probs, list) or len(probs) != len(options)
                    or any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in probs)
                    or not math.isclose(sum(probs), 1.0, rel_tol=0, abs_tol=1e-12)
                    or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0):
                raise ServiceError("invalid_model_response", "The model returned an invalid scoring response.")
            cache = out.get("cache")
            if not isinstance(cache, dict) or type(cache.get("enabled")) is not bool:
                raise ServiceError("invalid_model_response", "The model returned an invalid scoring response.")
            self.cache_policy = "consecutive-full-vocabulary-logits-v1" if cache["enabled"] else "fresh-state"
            self.previous_ids = tuple(tokens)
            self.cache_hit = out["cache_hit"]
            self.last_seconds = seconds
            self.last_pass_tokens = len(tokens)
            # These values are already calibrated in the native worker. Mapping
            # their option IDs must not apply another normalization or power.
            return {key: p for (key, _), p in zip(options, probs, strict=True)}, len(tokens)
        except ServiceError:
            self.clear()
            raise
        except Exception:
            self.clear()
            raise ServiceError("inference_failed", "The local model could not complete this decision.") from None

    def decide(self, state, question):
        probs, count = self.probabilities(state, question)
        return answer(question, probs, count)
