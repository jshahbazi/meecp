"""Bounded nonstreaming generation for the independently pinned Qwen fixture.

The host must share one adapter per owned engine generation. Transport failures
cannot prove engine settlement: retirement must stop and reap that generation.
No host route, recipe enablement, resource admission or installation is added.
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable

import anyio

CONTEXT_TOKENS = 512
MAX_OUTPUT_TOKENS = 64
MAX_INPUT_BYTES = 8192
MAX_RESPONSE_BYTES = 1024 * 1024
CONTEXT_GUARD = 8
STOP_WORDS = ("<|im_end|>", "<|endoftext|>")
SPECIAL_MARKERS = (*STOP_WORDS, "<|im_start|>")
SYSTEM_TEXT = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."
ERROR_CODES = frozenset({"invalid_request", "input_limit", "context_limit", "queue_full",
                         "engine_unavailable", "inference_failed", "invalid_engine_response",
                         "output_limit", "ownership_unsettled"})


class TextGenerationError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else "inference_failed"
        super().__init__(self.code)


def request_fields(request: object) -> tuple[str, int]:
    if (type(request) is not dict or set(request) != {"text", "max_output_tokens"}
            or type(request["text"]) is not str or not request["text"].strip()
            or type(request["max_output_tokens"]) is not int
            or not 1 <= request["max_output_tokens"] <= MAX_OUTPUT_TOKENS):
        raise TextGenerationError("invalid_request")
    text = request["text"]
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise TextGenerationError("invalid_request") from None
    if size > MAX_INPUT_BYTES:
        raise TextGenerationError("input_limit")
    # The fixed ChatML template has no content escaping rule. Reject structural
    # delimiters instead of turning user text into additional message roles.
    if any(marker in text for marker in SPECIAL_MARKERS):
        raise TextGenerationError("invalid_request")
    return text, request["max_output_tokens"]


def render_prompt(text: str) -> str:
    """Exact single-user branch of the pinned embedded chat template."""
    return ("<|im_start|>system\n" + SYSTEM_TEXT + "<|im_end|>\n"
            "<|im_start|>user\n" + text + "<|im_end|>\n<|im_start|>assistant\n")


class LoopbackTransport:
    """Bounded, proxy-independent, redirect-denying private engine transport."""
    def __init__(self, endpoint: str, api_key: str, *, timeout: float = 30):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or not isinstance(api_key, str) or not api_key
                or not 0 < timeout <= 120):
            raise TextGenerationError("invalid_request")
        self.endpoint, self.api_key, self.timeout = endpoint.rstrip("/"), api_key, timeout

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, path: str, payload: dict) -> dict:
        if path not in ("/tokenize", "/completion"):
            raise TextGenerationError("invalid_request")
        try:
            request = urllib.request.Request(self.endpoint + path,
                data=json.dumps(payload, allow_nan=False).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TextGenerationError("invalid_engine_response")
            result = json.loads(raw)
            if type(result) is not dict:
                raise TextGenerationError("invalid_engine_response")
            return result
        except TextGenerationError:
            raise
        except Exception:
            raise TextGenerationError("inference_failed") from None


def _token_list(value, *, allow_empty=False):
    if (type(value) is not list or not value and not allow_empty
            or any(type(token) is not int or not 0 <= token < 151936 for token in value)):
        raise TextGenerationError("invalid_engine_response")
    return value


class BoundedTextGenerator:
    """One engine generation, one inference owner, bounded admitted callers.

    `retire` must return exactly True only after the host verifies stop/reap.
    False or exceptions retain the ownership flag and permanently fence this
    adapter. External resource owners must inspect ownership_settled before
    releasing reservations. A replacement generation needs a new adapter.
    """
    def __init__(self, transport: Callable[[str, dict], dict],
                 retire: Callable[[], Awaitable[bool]], *, queue_limit=4):
        if type(queue_limit) is not int or not 1 <= queue_limit <= 16:
            raise TextGenerationError("invalid_request")
        self.transport, self.retire = transport, retire
        self.queue_limit, self.pending = queue_limit, 0
        self.lock = asyncio.Lock()
        self.closed = False
        self.ownership_settled = True
        self.completed = 0

    def _generate(self, text: str, maximum: int) -> dict:
        tokens = _token_list(self.transport("/tokenize", {
            "content": render_prompt(text), "add_special": False, "parse_special": True})["tokens"])
        if len(tokens) + maximum + CONTEXT_GUARD > CONTEXT_TOKENS:
            raise TextGenerationError("context_limit")
        response = self.transport("/completion", {
            "prompt": tokens, "n_predict": maximum, "temperature": 0,
            "cache_prompt": False, "stream": False, "return_tokens": True,
            "stop": list(STOP_WORDS), "ignore_eos": False,
        })
        output = _token_list(response.get("tokens"), allow_empty=True)
        predicted = response.get("tokens_predicted")
        if type(predicted) is not int or predicted != len(output):
            raise TextGenerationError("invalid_engine_response")
        if predicted > maximum:
            raise TextGenerationError("output_limit")
        content, reason = response.get("content"), response.get("stop_type")
        if (type(content) is not str or response.get("truncated") is not False
                or response.get("stop") is not True
                or type(response.get("tokens_evaluated")) is not int
                or response["tokens_evaluated"] != len(tokens)
                or reason not in ("eos", "word", "limit")
                or reason == "word" and response.get("stopping_word") not in STOP_WORDS
                or any(marker in content for marker in SPECIAL_MARKERS)):
            raise TextGenerationError("invalid_engine_response")
        if len(content.encode("utf-8")) > maximum * 256:
            raise TextGenerationError("output_limit")
        return {"text": content, "input_tokens": len(tokens), "output_tokens": predicted,
                "stop_reason": {"eos": "eos", "word": "stop", "limit": "length"}[reason]}

    async def _operation(self, text: str, maximum: int) -> dict:
        self.ownership_settled = False
        try:
            result = await asyncio.to_thread(self._generate, text, maximum)
        except BaseException as error:
            if isinstance(error, TextGenerationError) and error.code == "context_limit":
                # Tokenization completed and inference was never dispatched.
                self.ownership_settled = True
                raise
            self.closed = True
            try:
                self.ownership_settled = await self.retire() is True
            except BaseException:
                self.ownership_settled = False
            if not self.ownership_settled:
                raise TextGenerationError("ownership_unsettled") from None
            if isinstance(error, TextGenerationError):
                raise error from None
            raise TextGenerationError("inference_failed") from None
        self.ownership_settled = True
        self.completed += 1
        return result

    async def generate(self, request: object) -> dict:
        text, maximum = request_fields(request)
        if self.closed:
            raise TextGenerationError("engine_unavailable")
        if self.pending >= self.queue_limit:
            raise TextGenerationError("queue_full")
        self.pending += 1
        try:
            async with self.lock:
                if self.closed:
                    raise TextGenerationError("engine_unavailable")
                operation = asyncio.create_task(self._operation(text, maximum))
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # Keep the queue lock across repeated asyncio cancellation
                    # and AnyIO level cancellation until thread AND retirement
                    # settle. A timeout/disconnect is not engine completion.
                    with anyio.CancelScope(shield=True):
                        while not operation.done():
                            try:
                                await asyncio.shield(operation)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                    if not operation.cancelled():
                        operation.exception()
                    raise
        finally:
            self.pending -= 1
