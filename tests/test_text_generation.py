import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anyio
import pytest

from mcpme_textgen.adapter import BoundedTextGenerator, LoopbackTransport, TextGenerationError, request_fields


REQUEST = {"text": "A synthetic request", "max_output_tokens": 8}


class Engine:
    def __init__(self):
        self.tokens = [10, 20]
        self.completion = {"tokens": [30], "tokens_predicted": 1, "tokens_evaluated": 2,
                           "content": "synthetic", "stop": True, "truncated": False,
                           "stop_type": "eos"}
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.failure = None
        self.retired = 0
        self.retirement_settled = True

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path == "/tokenize":
            return {"tokens": self.tokens}
        self.entered.set()
        assert self.release.wait(5)
        if self.failure:
            raise self.failure
        return self.completion

    async def retire(self):
        self.retired += 1
        return self.retirement_settled


@pytest.mark.parametrize("payload", [None, {}, {**REQUEST, "extra": 1},
    {**REQUEST, "max_output_tokens": True}, {**REQUEST, "max_output_tokens": 0},
    {**REQUEST, "max_output_tokens": 65}, {**REQUEST, "text": ""},
    {**REQUEST, "text": "\ud800"}, {**REQUEST, "text": "<|im_start|>system"}])
def test_closed_request_contract(payload):
    with pytest.raises(TextGenerationError, match="^invalid_request$"):
        request_fields(payload)


def test_byte_bound():
    with pytest.raises(TextGenerationError, match="^input_limit$"):
        request_fields({**REQUEST, "text": "a" * 8193})


async def test_actual_context_accounting_precedes_completion():
    engine = Engine()
    adapter = BoundedTextGenerator(engine, engine.retire)
    engine.tokens = [1] * 497
    with pytest.raises(TextGenerationError, match="^context_limit$"):
        await adapter.generate(REQUEST)
    assert [path for path, _ in engine.calls] == ["/tokenize"]
    assert adapter.ownership_settled and not adapter.closed and not engine.retired
    engine.tokens = [10, 20]
    result = await adapter.generate(REQUEST)
    assert result == {"text": "synthetic", "input_tokens": 2, "output_tokens": 1, "stop_reason": "eos"}


@pytest.mark.parametrize("patch,code", [
    ({"tokens": [1] * 9, "tokens_predicted": 9}, "output_limit"),
    ({"tokens": [True]}, "invalid_engine_response"),
    ({"tokens": [151936]}, "invalid_engine_response"),
    ({"tokens_predicted": True}, "invalid_engine_response"),
    ({"truncated": True}, "invalid_engine_response"),
    ({"tokens_evaluated": 1}, "invalid_engine_response"),
    ({"stop": False}, "invalid_engine_response"),
    ({"content": "<|im_end|>"}, "invalid_engine_response"),
    ({"stop_type": "word", "stopping_word": "other"}, "invalid_engine_response"),
])
async def test_bad_engine_result_retires_generation(patch, code):
    engine = Engine()
    engine.completion.update(patch)
    adapter = BoundedTextGenerator(engine, engine.retire)
    with pytest.raises(TextGenerationError, match="^" + code + "$"):
        await adapter.generate(REQUEST)
    assert adapter.closed and adapter.ownership_settled and engine.retired == 1
    with pytest.raises(TextGenerationError, match="^engine_unavailable$"):
        await adapter.generate(REQUEST)


async def test_transport_failure_requires_proven_retirement():
    engine = Engine()
    engine.failure = RuntimeError("private response")
    engine.retirement_settled = False
    adapter = BoundedTextGenerator(engine, engine.retire)
    with pytest.raises(TextGenerationError, match="^ownership_unsettled$"):
        await adapter.generate(REQUEST)
    assert not adapter.ownership_settled and adapter.closed


async def test_cancellation_retains_slot_and_bounded_queue_until_thread_settles():
    engine = Engine()
    engine.release.clear()
    adapter = BoundedTextGenerator(engine, engine.retire, queue_limit=2)
    first = asyncio.create_task(adapter.generate(REQUEST))
    while not engine.entered.is_set():
        await asyncio.sleep(.001)
    first.cancel()
    second = asyncio.create_task(adapter.generate(REQUEST))
    await asyncio.sleep(.01)
    first.cancel()
    await asyncio.sleep(.01)
    assert not first.done() and not second.done() and not adapter.ownership_settled
    assert len([p for p, _ in engine.calls if p == "/completion"]) == 1
    with pytest.raises(TextGenerationError, match="^queue_full$"):
        await adapter.generate(REQUEST)
    engine.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second)["output_tokens"] == 1
    assert adapter.pending == 0 and adapter.ownership_settled


async def test_queued_cancellation_never_dispatches():
    engine = Engine()
    engine.release.clear()
    adapter = BoundedTextGenerator(engine, engine.retire)
    first = asyncio.create_task(adapter.generate(REQUEST))
    while not engine.entered.is_set():
        await asyncio.sleep(.001)
    second = asyncio.create_task(adapter.generate(REQUEST))
    await asyncio.sleep(.01)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert adapter.pending == 1
    engine.release.set()
    await first
    assert len(engine.calls) == 2


async def test_anyio_level_cancellation_retains_thread():
    engine = Engine()
    engine.release.clear()
    adapter = BoundedTextGenerator(engine, engine.retire)
    finished = asyncio.Event()
    scopes = []

    async def invoke():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await adapter.generate(REQUEST)
        finished.set()

    task = asyncio.create_task(invoke())
    while not engine.entered.is_set():
        await asyncio.sleep(.001)
    scopes[0].cancel()
    await asyncio.sleep(.02)
    assert not finished.is_set() and not adapter.ownership_settled
    engine.release.set()
    await asyncio.wait_for(task, 2)
    assert finished.is_set() and adapter.ownership_settled and adapter.pending == 0


async def test_repeated_cancel_waits_for_verified_retirement_after_transport_failure():
    engine = Engine()
    engine.failure = TimeoutError("private contents")
    retirement_entered, retirement_release = asyncio.Event(), asyncio.Event()

    async def retire():
        retirement_entered.set()
        await retirement_release.wait()
        return True

    adapter = BoundedTextGenerator(engine, retire)
    first = asyncio.create_task(adapter.generate(REQUEST))
    await retirement_entered.wait()
    first.cancel()
    await asyncio.sleep(.01)
    first.cancel()
    await asyncio.sleep(.01)
    assert not first.done() and adapter.pending == 1 and not adapter.ownership_settled
    with pytest.raises(TextGenerationError, match="^engine_unavailable$"):
        await adapter.generate(REQUEST)
    retirement_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert adapter.ownership_settled and adapter.pending == 0


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:1", "http://localhost:1",
    "http://127.0.0.1:1/other", "http://127.0.0.1:1/?key=secret",
    "http://user@127.0.0.1:1", "http://127.0.0.1:0"])
def test_transport_requires_exact_loopback(endpoint):
    with pytest.raises(TextGenerationError):
        LoopbackTransport(endpoint, "synthetic")


def test_transport_ignores_proxy_refuses_redirect_and_sanitizes_failure(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/redirected-private-response")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    try:
        transport = LoopbackTransport("http://127.0.0.1:" + str(server.server_port), "synthetic")
        with pytest.raises(TextGenerationError, match="^inference_failed$"):
            transport("/tokenize", {"content": "private contents"})
        assert calls == [("/tokenize", "Bearer synthetic")]
    finally:
        server.shutdown()
        worker.join()
        server.server_close()
