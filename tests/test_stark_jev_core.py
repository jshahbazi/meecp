from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from jevk5.prompt import LETTERS, answer, decision_options, prompt_text
from jevk5_app.contracts import ServiceError
from jevk5_app.stark_scorer import StarkScorer
from meecp_stark.engine import make_app
from meecp_stark.worker import StarkWorker, _hash


ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "engines/stark.lock.json").read_text())
QUESTION = {"type": "choice", "instructions": "Choose", "criteria": ["allow", "deny"]}
PROBS = [0.12345678901234568, 0.8765432109876543]


def ready(enabled):
    protocol = LOCK["protocol"]
    return {"ready": True, "engine": "stark", "version": protocol["ready_version"],
            "mode": protocol["worker_mode"], "upstream_revision": protocol["upstream_revision"],
            "scoring_patch_sha256": protocol["scoring_patch_sha256"], "cache_enabled": enabled,
            "cache_policy": protocol["cache_policy_enabled" if enabled else "cache_policy_disabled"],
            "cache_key": protocol["cache_key"], "vocab_size": 1000,
            "cache": {"enabled": enabled, "hits": 0, "native_score_calls": 0}}


def score_reply(payload, hit=False, enabled=True):
    return {"input_tokens": len(payload["input_token_ids"]),
            "prompt_tokens_sha256": _hash(payload["input_token_ids"]),
            "candidate_token_ids": payload["candidate_token_ids"],
            "candidate_probabilities": list(PROBS), "logits": [1.0, 2.0],
            "log_probabilities": [-100.0, -200.0], "temperature": payload["temperature"],
            "truncated": False, "cache_hit": hit,
            "metrics": dict.fromkeys(("state_creation_seconds", "prefill_seconds", "readout_seconds",
                                       "score_seconds", "total_seconds"), 0.01),
            "cache": {"enabled": enabled, "hits": int(hit), "native_score_calls": 1}}


class FakeScorer(StarkScorer):
    def __init__(self, context_tokens=8192):
        super().__init__("http://127.0.0.1:19001", "private-key", context_tokens,
                         temperature=1.22, knockout_temperature=0.93)
        self.requests = []
        self.tokens = [1, 2, 3]
        self.previous = None
        self.bad_letter = None
        self.change_reply = lambda out: None
        self.fail = False

    def _transport(self, path, payload):
        self.requests.append((path, copy.deepcopy(payload)))
        if self.fail:
            raise RuntimeError("private-native-response")
        if path == "/reset_cache":
            self.previous = None
            return {"cache_reset": True}
        if path == "/tokenize":
            text = payload["text"]
            if text in LETTERS and len(text) == 1:
                return {"token_ids": [100 + LETTERS.index(text)] if text != self.bad_letter else [100, 101]}
            return {"token_ids": list(self.tokens)}
        key = (tuple(payload["input_token_ids"]), payload["context_tokens"])
        out = score_reply(payload, key == self.previous)
        self.previous = key
        self.change_reply(out)
        return out


def test_scorer_preserves_vendor_prompt_and_native_probability_bits():
    scorer = FakeScorer()
    result = scorer.decide({"synthetic": "value"}, QUESTION)
    expected_prompt = prompt_text({"synthetic": "value"}, QUESTION["instructions"],
                                  [text for _, text in decision_options(QUESTION)])
    assert scorer.requests[0] == ("/tokenize", {"text": expected_prompt, "add_special_tokens": False})
    assert [payload["text"] for path, payload in scorer.requests[1:17]] == list(LETTERS)
    assert all(payload["add_special_tokens"] is False for path, payload in scorer.requests if path == "/tokenize")
    assert result == answer(QUESTION, dict(zip(QUESTION["criteria"], PROBS)), 3)
    assert [p.hex() for p in result["probabilities"].values()] == [p.hex() for p in PROBS]
    assert scorer.requests[-1][1] == {"input_token_ids": [1, 2, 3], "candidate_token_ids": [100, 101],
                                      "context_tokens": 8192, "temperature": 1.22}
    assert scorer.last_seconds == 0.01


def test_scorer_exact_sequence_consecutive_cache_and_tokenize_helpers():
    scorer = FakeScorer()
    for tokens, hit in (([1, 2], False), ([1, 2], True), ([2, 1], False), ([1, 2], False)):
        scorer.tokens = tokens
        scorer.decide("synthetic", QUESTION)
        assert scorer.cache_hit is hit
    # The generation's letter IDs are verified once; every prompt uses native
    # tokenization without resetting the immediately previous scoring identity.
    assert sum(path == "/tokenize" for path, _ in scorer.requests) == 16 + 4
    assert not any(path == "/reset_cache" for path, _ in scorer.requests)


def test_scorer_context_full_fit_and_overflow_clear_cache():
    scorer = FakeScorer(context_tokens=3)
    scorer.decide("synthetic", QUESTION)
    scorer.tokens.append(4)
    with pytest.raises(ServiceError) as caught:
        scorer.decide("synthetic", QUESTION)
    assert caught.value.code == "context_limit"
    assert scorer.requests[-1] == ("/reset_cache", {})
    assert scorer.previous_ids is None
    scorer.tokens.pop()
    scorer.decide("synthetic", QUESTION)
    assert not scorer.cache_hit


@pytest.mark.parametrize("change", [
    lambda out: out.update(truncated=True),
    lambda out: out.update(candidate_token_ids=[101, 100]),
    lambda out: out.update(candidate_probabilities=[float("nan"), 0.5]),
    lambda out: out.update(candidate_probabilities=[0.3, 0.3]),
    lambda out: out.update(input_tokens=True),
    lambda out: out.update(temperature=0.93),
    lambda out: out["metrics"].update(total_seconds=-1),
])
def test_scorer_invalid_native_reply_clears_cache_without_echo(change):
    scorer = FakeScorer()
    scorer.change_reply = change
    with pytest.raises(ServiceError) as caught:
        scorer.decide("private-evidence", QUESTION)
    assert caught.value.code == "invalid_model_response"
    assert scorer.requests[-1] == ("/reset_cache", {})
    assert "private" not in str(caught.value)


def test_scorer_checks_all_letter_ids_and_invalid_decision_clears_cache():
    scorer = FakeScorer()
    scorer.bad_letter = "P"
    with pytest.raises(ServiceError) as caught:
        scorer.decide("synthetic", QUESTION)
    assert caught.value.code == "incomplete_options"
    assert not any(path == "/score" for path, _ in scorer.requests)
    scorer.bad_letter = None
    scorer.decide("synthetic", QUESTION)
    with pytest.raises(ServiceError):
        scorer.decide("synthetic", {**QUESTION, "criteria": [str(i) for i in range(17)]})
    assert scorer.requests[-1] == ("/reset_cache", {})
    scorer.decide("synthetic", QUESTION)
    assert not scorer.cache_hit


def test_scorer_failed_reset_is_retried_before_any_scoring():
    scorer = FakeScorer()
    scorer.decide("synthetic", QUESTION)
    scorer.fail = True
    scorer.clear()
    assert scorer._needs_reset
    count = len(scorer.requests)
    with pytest.raises(ServiceError):
        scorer.decide("synthetic", QUESTION)
    assert not any(path == "/score" for path, _ in scorer.requests[count:])
    scorer.fail = False
    count = len(scorer.requests)
    scorer.decide("synthetic", QUESTION)
    assert scorer.requests[count] == ("/reset_cache", {})
    assert not scorer.cache_hit and not scorer._needs_reset


@pytest.mark.parametrize("kwargs", [{"url": "https://private-host:123"}, {"api_key": "private\nkey"},
                                    {"context_tokens": True}, {"temperature": float("nan")},
                                    {"temperature": 0}, {"knockout_temperature": -1}])
def test_scorer_constructor_failures_are_fixed_service_errors(kwargs):
    config = {"url": "http://127.0.0.1:19001", "api_key": "synthetic-key", "context_tokens": 8192}
    with pytest.raises(ServiceError) as caught:
        StarkScorer(**{**config, **kwargs})
    assert caught.value.code == "unsupported_model_profile"
    assert "private" not in str(caught.value)


def test_reset_transport_timeout_is_short_and_score_timeout_preserved():
    calls = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return b"{}"
    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout, request.get_header("Authorization")))
            return Response()
    scorer = StarkScorer("http://127.0.0.1:19001", "synthetic-key", 8192,
                         temperature=0.93, knockout_temperature=1.22)
    scorer._opener = Opener()
    scorer._transport("/reset_cache", {})
    scorer._transport("/score", SCORE)
    assert calls == [("http://127.0.0.1:19001/reset_cache", 2.0, "Bearer synthetic-key"),
                     ("http://127.0.0.1:19001/score", 120, "Bearer synthetic-key")]
    assert scorer.temperature == 0.93 and scorer.knockout_temperature == 1.22


class FakeWriter:
    def __init__(self, process):
        self.process = process
        self.gate = None

    def write(self, raw):
        value = json.loads(raw)
        self.process.requests.append(value)
        response = self.process.respond(value)
        if response is not None:
            self.process.stdout.feed_data(json.dumps(response).encode() + b"\n")
        else:
            self.process.stdout.feed_eof()

    async def drain(self):
        if self.gate is not None:
            await self.gate.wait()


class FakeProcess:
    def __init__(self, owner, metadata):
        self.owner = owner
        self.instance_id = "worker-instance"
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(json.dumps(metadata).encode() + b"\n")
        self.stdin = FakeWriter(self)
        self.requests = []
        self.done = asyncio.get_running_loop().create_future()
        self.previous = None
        self.enabled = metadata["cache_enabled"]
        self.reply_change = lambda response: None
        self.native_error = None

    def respond(self, value):
        if self.native_error is not None:
            self.previous = None
            return {"error": self.native_error, "cache": {"enabled": self.enabled, "hits": 0, "native_score_calls": 0}}
        if value["operation"] == "reset_cache":
            self.previous = None
            response = {"cache_reset": True, "cache": {"enabled": self.enabled, "hits": 0, "native_score_calls": 0}}
        elif value["operation"] == "tokenize":
            ids = [1, 2]
            response = {"token_ids": ids, "token_count": 2, "token_ids_sha256": _hash(ids), "vocab_size": 1000,
                        "cache": {"enabled": self.enabled, "hits": 0, "native_score_calls": 0}}
        else:
            key = (tuple(value["input_token_ids"]), value["context_tokens"])
            response = score_reply(value, self.enabled and key == self.previous, self.enabled)
            self.previous = key if self.enabled else None
        self.reply_change(response)
        return response

    async def wait(self):
        return await asyncio.shield(self.done)


class FakeOwner:
    def __init__(self, metadata):
        self.metadata = metadata
        self.instances = {}
        self.closed = False
        self.launch_args = None

    async def launch(self, executable, arguments, cwd, environment, **kwargs):
        self.launch_args = (executable, arguments, cwd, environment, kwargs)
        process = FakeProcess(self, self.metadata)
        self.instances[process.instance_id] = process
        return process

    def holds_live_instances(self, ids):
        return not self.closed and ids == set(self.instances)

    async def close(self):
        self.closed = True
        for process in self.instances.values():
            if not process.done.done():
                process.done.set_result(0)
        self.instances.clear()


def fixture_worker(tmp_path, *, enabled=False, metadata=None):
    (tmp_path / "engines").mkdir(exist_ok=True)
    (tmp_path / "engines/stark.lock.json").write_text(json.dumps(LOCK))
    owner = FakeOwner(ready(enabled) if metadata is None else metadata)
    worker = StarkWorker(tmp_path, {"artifact": "models/checkpoint/model.safetensors",
                                    "context_tokens": 8192, "parameters": {"consecutive_cache": enabled}}, owner=owner)
    return worker, owner


SCORE = {"input_token_ids": [1, 2], "candidate_token_ids": [100, 101], "context_tokens": 8192, "temperature": 1.22}


@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_owns_model_directory_and_default_off_cache(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("STARK_CAPTURE_FIRST_PREFILL", "private")
    monkeypatch.setenv("API_KEY", "private")
    worker, owner = fixture_worker(tmp_path, enabled=enabled)
    await worker.start()
    try:
        executable, args, cwd, env, kwargs = owner.launch_args
        assert executable == tmp_path / "bin/stark-score"
        assert args == ["--score-model-path", str(tmp_path / "models/checkpoint")] + (["--consecutive-cache"] if enabled else [])
        assert kwargs == {"owned_group": True, "io_mode": "streams"}
        assert "STARK_CAPTURE_FIRST_PREFILL" not in env and "API_KEY" not in env
        assert worker.is_ready()
        first = await worker.request("score", SCORE)
        await worker.request("tokenize", {"text": "synthetic", "add_special_tokens": False})
        second = await worker.request("score", SCORE)
        assert first["cache_hit"] is False and second["cache_hit"] is enabled
        assert [p.hex() for p in second["candidate_probabilities"]] == [p.hex() for p in PROBS]
    finally:
        await worker.close()
    assert owner.closed and not worker.is_ready()


@pytest.mark.parametrize("key,value", [("version", "wrong"), ("engine", "uzu"), ("ready", 1),
                                      ("mode", "generation"), ("cache_enabled", True),
                                      ("scoring_patch_sha256", "wrong"), ("upstream_revision", "wrong")])
async def test_worker_rejects_unbound_readiness_and_reaps(tmp_path, key, value):
    metadata = ready(False)
    metadata[key] = value
    worker, owner = fixture_worker(tmp_path, metadata=metadata)
    with pytest.raises(ServiceError) as caught:
        await worker.start()
    assert caught.value.code == "engine_unavailable"
    assert owner.closed and not worker.is_ready()


@pytest.mark.parametrize("fault", ["eof", "native_scoring_failed", "invalid_score_values", "private-native-error", "bad_reply"])
async def test_worker_fatal_or_invalid_reply_fences_and_reaps(tmp_path, fault):
    worker, owner = fixture_worker(tmp_path, enabled=True)
    await worker.start()
    process = worker.process
    if fault == "eof":
        process.respond = lambda value: None
    elif fault == "bad_reply":
        process.reply_change = lambda out: out.update(candidate_probabilities=[1.0])
    else:
        process.native_error = fault
    with pytest.raises(ServiceError) as caught:
        await worker.request("score", SCORE)
    assert caught.value.code == "inference_failed"
    assert "private" not in str(caught.value)
    assert not worker.is_ready() and owner.closed
    await worker.close()


async def test_worker_validation_error_clears_native_cache_and_stays_ready(tmp_path):
    worker, _owner = fixture_worker(tmp_path, enabled=True)
    await worker.start()
    try:
        await worker.request("score", SCORE)
        worker.process.native_error = "invalid_scoring_request"
        with pytest.raises(ServiceError) as caught:
            await worker.request("score", SCORE)
        assert caught.value.status == 400 and worker.is_ready()
        worker.process.native_error = None
        assert (await worker.request("score", SCORE))["cache_hit"] is False
    finally:
        await worker.close()


async def test_worker_queue_bounded_and_cancellation_settles_then_clears(tmp_path):
    worker, _owner = fixture_worker(tmp_path, enabled=True)
    await worker.start()
    worker.queue_limit = 1
    gate = asyncio.Event()
    worker.process.stdin.gate = gate
    task = asyncio.create_task(worker.request("score", SCORE))
    while not worker.process.requests:
        await asyncio.sleep(0)
    with pytest.raises(ServiceError) as caught:
        await worker.request("score", SCORE)
    assert caught.value.code == "queue_full"
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and worker.pending == 1
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.process.requests[-1] == {"operation": "reset_cache"}
    assert worker.pending == 0
    assert (await worker.request("score", SCORE))["cache_hit"] is False
    await worker.close()


async def test_all_engine_routes_auth_host_origin_and_health_preserves_cache(tmp_path):
    worker, _owner = fixture_worker(tmp_path, enabled=True)
    await worker.start()
    app = make_app(worker, "private-key", port=19001, generation=7, nonce="private-nonce")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:19001") as client:
        auth = {"Authorization": "Bearer private-key"}
        for path in ("/health", "/tokenize", "/score", "/reset_cache", "/missing"):
            method = "GET" if path == "/health" else "POST"
            assert (await client.request(method, path, json={})).status_code == 401
            assert (await client.request(method, path, json={}, headers={**auth, "Host": "external:19001"})).status_code == 403
            assert (await client.request(method, path, json={}, headers={**auth, "Origin": "https://external"})).status_code == 403
        assert (await client.post("/score", json=SCORE, headers=auth)).status_code == 200
        count = len(worker.process.requests)
        health = (await client.get("/health", headers=auth)).json()
        assert health == {"ready": True, "generation": 7, "nonce": "private-nonce", "engine": "stark",
                          "cache_policy": "consecutive-full-vocabulary-logits-v1"}
        assert len(worker.process.requests) == count
        assert (await client.post("/score", json=SCORE, headers=auth)).json()["cache_hit"] is True
        bad = await client.post("/score", json={**SCORE, "private-extra": "private-evidence"}, headers=auth)
        assert bad.status_code == 400 and "private" not in bad.text
        assert (await client.post("/score", json=SCORE, headers=auth)).json()["cache_hit"] is False
        bad_context = await client.post("/score", json={**SCORE, "context_tokens": 16384}, headers=auth)
        assert bad_context.status_code == 400
        assert worker.process.requests[-1] == {"operation": "reset_cache"}
        # A smaller native context remains valid for the scoped boundary checks.
        assert (await client.post("/score", json={**SCORE, "context_tokens": 2}, headers=auth)).status_code == 200
        oversized = await client.post("/tokenize", content=b"x" * (1024 * 1024 + 1), headers=auth)
        assert oversized.status_code == 413
        worker.ready = False
        assert (await client.get("/health", headers=auth)).json()["ready"] is False
    await worker.close()


@pytest.fixture(scope="module")
def native_reaper(tmp_path_factory):
    target = tmp_path_factory.mktemp("stark-reaper") / "meecp-reap"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "scripts/meecp-reap.c"), "-o", str(target)], check=True, capture_output=True)
    return target


async def test_real_reaper_owns_worker_and_drops_native_stderr(tmp_path, native_reaper, capfd):
    from meecp.processes import ProcessOwner

    (tmp_path / "engines").mkdir()
    (tmp_path / "engines/stark.lock.json").write_text(json.dumps(LOCK))
    (tmp_path / "bin").mkdir()
    executable = tmp_path / "bin/stark-score"
    script = (
        "#!" + sys.executable + "\n"
        "import json,sys\n"
        "print('private-native-stderr-canary',file=sys.stderr,flush=True)\n"
        "print(" + repr(json.dumps(ready(False))) + ",flush=True)\n"
        "for line in sys.stdin:\n"
        " value=json.loads(line)\n"
        " print(json.dumps({'cache_reset':True,'cache':{'enabled':False,'hits':0,'native_score_calls':0}}),flush=True)\n"
    )
    executable.write_text(script)
    executable.chmod(0o700)
    owner = ProcessOwner(native_reaper)
    worker = StarkWorker(tmp_path, {"artifact": "models/checkpoint/model.safetensors", "context_tokens": 8192,
                                    "parameters": {}}, owner=owner)
    await worker.start()
    pid = worker.process.pid
    assert type(pid) is int and pid > 1
    assert worker.is_ready() and owner.holds_live_instances({worker.process.instance_id})
    assert (await worker.request("reset_cache", {}))["cache_reset"] is True
    await worker.close()
    assert not owner.instances and not worker.is_ready()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert "private-native-stderr-canary" not in "".join(capfd.readouterr())


def facade_fixture(monkeypatch):
    from meecp_stark import __main__ as entrypoint

    class Worker:
        ready = False
        checks = 0
        closed = False
        def __init__(self):
            self.start_gate = asyncio.Event()
            self.close_gate = asyncio.Event()
            self.close_gate.set()
        async def start(self):
            await self.start_gate.wait()
            self.ready = True
        def is_ready(self):
            self.checks += 1
            return self.ready
        async def close(self):
            await self.close_gate.wait()
            self.ready = False
            self.closed = True

    worker = Worker()
    servers = []
    class Server:
        started = False
        should_exit = False
        def __init__(self, config):
            assert config.host == "127.0.0.1" and not config.access_log
            self.listening = asyncio.Event()
            self.return_normally = asyncio.Event()
            servers.append(self)
        async def serve(self):
            await worker.start()
            assert not self.should_exit
            self.started = True
            self.listening.set()
            while not self.should_exit and not self.return_normally.is_set():
                await asyncio.sleep(0.01)

    monkeypatch.setattr(entrypoint, "StarkWorker", lambda *_args: worker)
    monkeypatch.setattr(entrypoint, "make_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(entrypoint, "Server", Server)
    config = {"api_key": "synthetic-key", "endpoint": {"host": "127.0.0.1", "port": 19001},
              "generation": 7, "nonce": "synthetic-nonce", "model": {}}
    return entrypoint, worker, servers, config


async def test_facade_monitor_waits_for_startup_then_exits_after_native_loss(monkeypatch):
    entrypoint, worker, servers, config = facade_fixture(monkeypatch)
    task = asyncio.create_task(entrypoint.run_engine(config))
    await asyncio.sleep(0.3)
    assert not task.done() and not servers[0].should_exit
    assert worker.checks == 0
    worker.start_gate.set()
    await asyncio.wait_for(servers[0].listening.wait(), 1)
    worker.ready = False
    await asyncio.wait_for(task, 1)
    assert servers[0].should_exit and worker.closed


async def test_normal_facade_shutdown_settles_monitor_and_closes_worker(monkeypatch):
    entrypoint, worker, servers, config = facade_fixture(monkeypatch)
    worker.start_gate.set()
    task = asyncio.create_task(entrypoint.run_engine(config))
    while not servers:
        await asyncio.sleep(0)
    await asyncio.wait_for(servers[0].listening.wait(), 1)
    servers[0].return_normally.set()
    await asyncio.wait_for(task, 1)
    assert worker.closed
    checks = worker.checks
    await asyncio.sleep(0.3)
    assert worker.checks == checks and not servers[0].should_exit


async def test_cancelled_facade_shutdown_waits_for_worker_cleanup(monkeypatch):
    entrypoint, worker, servers, config = facade_fixture(monkeypatch)
    worker.start_gate.set()
    worker.close_gate.clear()
    task = asyncio.create_task(entrypoint.run_engine(config))
    while not servers:
        await asyncio.sleep(0)
    await asyncio.wait_for(servers[0].listening.wait(), 1)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done() and not worker.closed
    worker.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert worker.closed
