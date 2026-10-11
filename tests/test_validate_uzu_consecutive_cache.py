"""Pure mocks: cache acceptance completeness, exactness, and sequence rejection."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_uzu_consecutive_cache as cache


def telemetry(enabled=True, hits=0, calls=0):
    return {"enabled": enabled, "hits": hits, "native_score_calls": calls}


def response(request, *, hit=False, enabled=True, hits=0, calls=1):
    # Deterministic mocked full vocabulary; controls recompute independently.
    full = [float(index) / 4.0 for index in range(32)]
    logits = [full[index] for index in request["candidate_token_ids"]]
    partition = math.log(sum(math.exp(v) for v in full))
    maximum = max(logits)
    exp = [math.exp((v - maximum) / request["temperature"]) for v in logits]
    value = {"input_tokens": len(request["input_token_ids"]),
        "prompt_tokens_sha256": cache.base.token_digest(request["input_token_ids"]),
        "candidate_token_ids": request["candidate_token_ids"], "logits": logits,
        "log_probabilities": [v - partition for v in logits],
        "candidate_probabilities": [v / sum(exp) for v in exp], "temperature": request["temperature"],
        "cache_hit": hit, "truncated": False, "cache": telemetry(enabled, hits, calls),
        "metrics": {key: .01 for key in cache.original.METRICS}}
    if hit:
        value["metrics"].update(state_creation_seconds=0.0, prefill_seconds=0.0)
    return value


class MockClient:
    def __init__(self, enabled=True, *, digest=False):
        self.oracle = cache.CacheExpectation(enabled)
        self.enabled, self.digest = enabled, digest
        self.hits = self.calls = self.resets = 0
        self.readiness = {"cache_enabled": enabled}

    def counters(self):
        return telemetry(self.enabled, self.hits, self.calls)

    async def reset(self):
        self.oracle.reset()
        self.resets += 1

    async def post(self, request):
        if request["operation"] == "tokenize":
            return {"token_ids": [2], "token_count": 1,
                    "token_ids_sha256": cache.base.token_digest([2]), "cache": self.counters()}
        hit = self.oracle.successful_score(request)
        self.hits += int(hit)
        self.calls += int(not hit)
        if self.digest:
            return {"input_tokens": len(request["input_token_ids"]),
                "prompt_tokens_sha256": cache.base.token_digest(request["input_token_ids"]),
                "cache_hit": hit, "truncated": False, "vocab_size": 32,
                "vocab_logits_sha256": "a" * 64, "cache": self.counters()}
        return response(request, hit=hit, enabled=self.enabled, hits=self.hits, calls=self.calls)

    async def exchange(self, request=None, *, raw=None):
        self.oracle.error()
        code = "invalid_request"
        if request and request["operation"] == "score":
            code = "invalid_scoring_request"
        elif request and request.get("text") == "":
            code = "invalid_tokenization_request"
        return {"error": code, "cache": self.counters()}


def request():
    return cache.score_request([1, 2], [3, 4])


def plan():
    return cache.sequence_plan([1, 2], list(range(3, 19)))


async def sequence():
    client, control = MockClient(), MockClient(False)
    rows = []
    for item in plan():
        row = copy.deepcopy(item)
        if item["name"] in cache.ERROR_NAMES:
            row["response"] = await client.exchange(item["request"])
        else:
            row["response"] = await client.post(item["request"])
            if item["request"]["operation"] == "score":
                row["control"] = await control.post(item["request"])
        rows.append(row)
    return rows


def test_default_off_never_reuses_identical_requests():
    oracle = cache.CacheExpectation(False)
    assert [oracle.successful_score(request()) for _ in range(3)] == [False, False, False]


def test_exact_consecutive_key_and_single_entry_replacement():
    oracle = cache.CacheExpectation()
    a, b = request(), cache.score_request([1, 3], [3, 4])
    assert [oracle.successful_score(item) for item in (a, a, b, a)] == [False, True, False, False]


def test_context_change_misses_candidate_order_set_and_temperature_reuse():
    oracle = cache.CacheExpectation()
    assert not oracle.successful_score(request())
    assert oracle.successful_score(cache.score_request([1, 2], [4, 3]))
    assert oracle.successful_score(cache.score_request([1, 2], [5, 6], temperature=.61))
    assert not oracle.successful_score(cache.score_request([1, 2], [3, 4], context=8191))
    assert not oracle.successful_score(request())


def test_successful_helper_preserves_score_and_error_reset_invalidates():
    oracle = cache.CacheExpectation()
    assert not oracle.successful_score(request())
    # Successful tokenization has no score transition.
    assert oracle.successful_score(request())
    oracle.error()
    assert not oracle.successful_score(request())
    oracle.reset()
    assert not oracle.successful_score(request())


def test_expectation_retains_exact_copy_of_tokens():
    oracle = cache.CacheExpectation()
    item = request()
    assert not oracle.successful_score(item)
    item["input_token_ids"][0] = 9
    assert not oracle.successful_score(item)


def test_restart_new_oracle_always_first_miss():
    first = cache.CacheExpectation()
    assert not first.successful_score(request())
    assert first.successful_score(request())
    assert not cache.CacheExpectation().successful_score(request())


def test_hit_requires_zero_native_creation_and_prefill_metrics():
    value = response(request(), hit=True, hits=1)
    cache.validate_response(value, request(), True)
    value["metrics"]["prefill_seconds"] = .001
    with pytest.raises(cache.base.EvaluationError, match="cache_hit_executed_native"):
        cache.validate_response(value, request(), True)


@pytest.mark.parametrize("key,value", [
    ("cache_hit", 1), ("cache_hit", False), ("temperature", 1.0),
    ("prompt_tokens_sha256", "0" * 64), ("candidate_token_ids", [4, 3]),
    ("logits", [math.nan, 2.0]), ("candidate_probabilities", [.5, .5]),
])
def test_response_rejects_invalid_identity_and_outputs(key, value):
    item = response(request(), hit=True, hits=1)
    item[key] = value
    with pytest.raises(cache.base.EvaluationError):
        cache.validate_response(item, request(), True)


def test_native_telemetry_rejects_a_hit_that_executed_native():
    checker = cache.CacheTelemetry(True)
    fresh = response(request())
    checker.validate(fresh, request())
    hit = response(request(), hit=True, hits=1, calls=2)
    with pytest.raises(cache.base.EvaluationError, match="cache_telemetry_invalid"):
        checker.validate(hit, request())


def test_reset_and_tokenize_telemetry_preserve_counters():
    checker = cache.CacheTelemetry(True)
    checker.validate(response(request()), request())
    checker.validate({"cache_reset": True, "cache": telemetry(calls=1)}, {"operation": "reset_cache"})
    checker.validate({"cache": telemetry(calls=1)}, {"operation": "tokenize"})
    checker.validate(response(request(), hit=False, calls=2), request())


@pytest.mark.parametrize("key,value", [("enabled", 1), ("hits", False), ("native_score_calls", True), ("hits", -1)])
def test_telemetry_rejects_bool_integer_substitution_and_negative_counts(key, value):
    counters = telemetry(calls=1)
    counters[key] = value
    with pytest.raises(cache.base.EvaluationError, match="cache_telemetry_invalid"):
        cache.CacheTelemetry(True).validate(dict(response(request()), cache=counters), request())


@pytest.mark.parametrize("enabled", [False, True])
def test_readiness_explicit_default_off_and_opt_in(enabled):
    value = {"ready": True, "mode": "exact-token-scoring-cache-v1", "cache_enabled": enabled,
             "cache_policy": cache.CACHE_POLICY if enabled else "fresh-state", "cache_key": cache.CACHE_KEY,
             "cache": telemetry(enabled), "upstream_revision": cache.original.REVISION, "vocab_size": 32,
             "scoring_patch_sha256": cache.base.file_hash(cache.ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch")}
    cache.validate_readiness(value, enabled=enabled)
    value["cache_enabled"] = not enabled
    with pytest.raises(cache.base.EvaluationError, match="native_readiness_invalid"):
        cache.validate_readiness(value, enabled=enabled)


@pytest.mark.asyncio
async def test_full_sequence_accepts_native_controls_and_expected_reuse():
    rows = await sequence()
    cache.validate_sequence_rows(rows, plan())
    assert [row["response"]["cache_hit"] for row in rows if "cache_hit" in row["response"]] == [
        False, True, False, False, True, False, True, False, True, True, True, False, False, False, False]
    assert [row["name"] for row in rows] == list(cache.SEQUENCE_NAMES)


@pytest.mark.parametrize("index", [1, 3, 5, 6, 8, 9, 10, 11, 13, 15, 17, 19])
@pytest.mark.asyncio
async def test_sequence_rejects_incorrect_reuse_or_error_invalidation(index):
    rows = await sequence()
    rows[index]["response"]["cache_hit"] = not rows[index]["response"]["cache_hit"]
    with pytest.raises(cache.base.EvaluationError):
        cache.validate_sequence_rows(rows, plan())


@pytest.mark.parametrize("field", ["logits", "log_probabilities", "candidate_probabilities"])
@pytest.mark.asyncio
async def test_sequence_rejects_single_float_bit_change_against_native_control(field):
    rows = await sequence()
    values = rows[9]["response"][field]
    values[0] = math.nextafter(values[0], math.inf)
    with pytest.raises(cache.base.EvaluationError):
        cache.validate_sequence_rows(rows, plan())


@pytest.mark.asyncio
async def test_sequence_rejects_mutated_requests_and_incomplete_rows():
    rows = await sequence()
    rows[6]["request"]["context_tokens"] = 8192
    with pytest.raises(cache.base.EvaluationError, match="sequence_request_invalid"):
        cache.validate_sequence_rows(rows, plan())
    with pytest.raises(cache.base.EvaluationError, match="sequence_incomplete"):
        cache.validate_sequence_rows(rows[:-1], plan())


@pytest.mark.asyncio
async def test_digest_pair_checks_full_vocab_after_reset():
    client = MockClient(digest=True)
    expected = await client.post(request())
    await client.post(request())
    values = await cache.pair(client, request(), expected, vocab_size=32)
    assert client.resets == 1
    assert [value["cache_hit"] for value in values] == [False, True]
    assert values[1]["cache"]["native_score_calls"] == 2
    altered = dict(values[1], vocab_logits_sha256="b" + "a" * 63)
    with pytest.raises(cache.base.EvaluationError, match="vocabulary_equality_failed"):
        cache.validate_digest(altered, request(), expected, 32, True)


@pytest.mark.asyncio
async def test_case_pair_checks_each_corresponding_frozen_reference():
    case = cache.base.synthetic_cases()[0]
    item = request()
    client = MockClient()
    value = response(item)
    row = {"case_id": case["id"], "input_token_ids": item["input_token_ids"], "response": value,
           "decision": cache.base.typed_decision(case["question"], value["candidate_probabilities"], 2)}
    reference = cache.prior.normalized_row(row)
    values = await cache.pair(client, item, [reference, reference], case=case)
    assert [row["condition"] for row in values] == ["fresh", "identical_repeat"]
    changed = copy.deepcopy(reference)
    changed["logits"][0] = math.nextafter(changed["logits"][0], math.inf)
    with pytest.raises(cache.base.EvaluationError, match="native_bit_equality_failed"):
        await cache.pair(client, item, [reference, changed], case=case)


def test_configuration_protocol_declares_timing_deferred_and_all_fixed_steps():
    text = cache.PROTOCOL.read_text()
    assert "Timing is deferred" in text
    assert "1068" in text and "486" in text and "96" in text
    assert "reset_cache" in text and "EOF" in text
    assert "default" in text and "flush" in text
    assert tuple(item["name"] for item in plan()) == cache.SEQUENCE_NAMES


@pytest.mark.asyncio
async def test_sequence_launcher_is_serial_default_off_and_restart_first_miss(monkeypatch):
    owner = SimpleNamespace(instances={})
    launches = []

    class Stdin:
        def __init__(self, process):
            self.process = process
        def close(self):
            self.process.returncode = 0
        async def wait_closed(self):
            pass

    class Process:
        ownership_lost = False
        returncode = None
        def __init__(self):
            self.stdin = Stdin(self)
        async def stop(self):
            owner.instances.clear()
        async def wait(self):
            return self.returncode

    async def fake_launch(owner_arg, binary, model, scratch, *, enabled, digest=False):
        assert owner_arg is owner and not owner.instances
        launches.append(enabled)
        process, client = Process(), MockClient(enabled)
        owner.instances["process"] = process
        return process, client

    monkeypatch.setattr(cache, "launch", fake_launch)
    result = await cache.run_sequences(owner, SimpleNamespace(candidate_binary=Path("unused"), model=Path("unused")),
                                       Path("unused"), [1, 2], list(range(3, 19)))
    assert launches == [False, True, True]
    assert result["eof_exit_code"] == 0 and not owner.instances
    assert [value["cache_hit"] for value in result["default_off"]["responses"]] == [False, False]
    assert result["restart"]["response"]["cache_hit"] is False
    assert result["restart"]["response"]["cache"]["native_score_calls"] == 1
