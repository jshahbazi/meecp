"""The pinned upstream readout with strict coverage and conservative reuse."""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

from jevk5.gguf import JevK5GGUF
from jevk5.prompt import LETTERS, decision_options

from .contracts import ServiceError


class LocalScorer(JevK5GGUF):
    def __init__(self, url: str, api_key: str, context_tokens: int, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or parsed.port is None or not (1 <= parsed.port <= 65535)):
            raise ValueError("The local model endpoint must be an http loopback URL.")
        url = url.rstrip("/")
        super().__init__(url=url, top_k=40, timeout_s=120, **kwargs)
        self.api_key = api_key
        self.context_tokens = context_tokens
        self.previous_ids: tuple[int, ...] | None = None
        self.cache_hit = False
        self.expected_letters = ""
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def clear(self):
        self.previous_ids = None
        self.cache_hit = False

    def _transport(self, path, payload):
        if path not in ("/tokenize", "/completion"):
            raise ValueError("Unsupported local model path")
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload, allow_nan=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
        )
        with self._opener.open(request, timeout=self.timeout_s) as response:
            return json.loads(response.read(4 * 1024 * 1024))

    def _post(self, path, payload):
        return self._transport(path, payload)

    def _logprobs(self, prompt):
        try:
            tokens = self._post("/tokenize", {"content": prompt, "add_special": False, "parse_special": True})["tokens"]
            if not isinstance(tokens, list) or not tokens or any(type(t) is not int for t in tokens):
                raise ServiceError("invalid_model_response", "The model returned an invalid token sequence.")
            if len(tokens) >= self.context_tokens:
                raise ServiceError("context_limit", "The decision exceeds the model context budget.", 400)
            ids = tuple(tokens)
            hit = ids == self.previous_ids
            out = self._post("/completion", {
                "prompt": tokens, "n_predict": 1, "n_probs": self.top_k,
                "temperature": 0, "cache_prompt": hit,
            })
            if out.get("truncated") is not False:
                raise ServiceError("truncated_prompt", "The model did not confirm complete prompt evaluation.")
            top = out["completion_probabilities"][0]["top_logprobs"]
            seen = {entry["token"]: entry["logprob"] for entry in top}
            if any(letter not in seen or not math.isfinite(seen[letter]) for letter in self.expected_letters):
                raise ServiceError("incomplete_options", "The model did not cover every declared option.")
            timings = out.get("timings") or {}
            seconds = (timings.get("prompt_ms", 0) + timings.get("predicted_ms", 0)) / 1000
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError()
            # Only a complete, validated response establishes the next reusable sequence.
            self.previous_ids, self.cache_hit = ids, hit
            return seen, len(tokens), seconds
        except ServiceError:
            self.clear()
            raise
        except Exception:
            self.clear()
            raise ServiceError("inference_failed", "The local model could not complete this decision.") from None

    def probabilities(self, state, question):
        self.expected_letters = LETTERS[:len(decision_options(question))]
        try:
            return super().probabilities(state, question)
        except Exception:
            self.clear()
            raise
