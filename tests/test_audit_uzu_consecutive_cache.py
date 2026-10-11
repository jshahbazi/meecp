"""Independent audit must reject cache, calibration and bit-level tampering."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC = importlib.util.spec_from_file_location("cache_independent_audit", SCRIPTS / "audit_uzu_consecutive_cache.py")
    audit = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(audit)
finally:
    sys.path.pop(0)


def response(hit=False, calls=1, hits=0, enabled=True, temperature=1.22):
    logits = [2.0, -1.0]
    weights = [math.exp((z - max(logits)) / temperature) for z in logits]
    total = math.fsum(weights)
    return {"input_tokens": 2, "prompt_tokens_sha256": audit.old.token_digest([1, 2]),
            "candidate_token_ids": [3, 4], "temperature": temperature, "truncated": False, "cache_hit": hit,
            "logits": logits, "log_probabilities": [-0.1, -3.1],
            "candidate_probabilities": [w / total for w in weights],
            "metrics": dict.fromkeys(("state_creation_seconds", "prefill_seconds", "readout_seconds",
                                      "score_seconds", "total_seconds"), 0.0),
            "cache": {"enabled": enabled, "hits": hits, "native_score_calls": calls}}


def test_replay_uses_exact_tokens_context_and_only_immediately_preceding_score():
    replay = audit.Replay()
    first = audit.request([1, 2], [3, 4])
    assert replay.score(response(), first) is False
    assert replay.score(response(True, 1, 1), first) is True
    assert replay.score(response(False, 2, 1), audit.request([2, 1], [3, 4])) is False
    assert replay.score(response(False, 3, 1), first) is False
    assert replay.score(response(False, 4, 1), audit.request([1, 2], [3, 4], 8191)) is False


def test_readout_candidates_and_temperature_do_not_change_distribution_key():
    replay = audit.Replay()
    assert not replay.score(response(), audit.request([1, 2], [3, 4]))
    assert replay.score(response(True, 1, 1), audit.request([1, 2], [4, 5], temperature=.61))


def test_errors_and_reset_clear_identity_without_resetting_counters():
    replay = audit.Replay()
    req = audit.request([1, 2], [3, 4])
    replay.score(response(), req)
    replay.clear()
    assert not replay.score(response(False, 2), req)


def test_default_off_never_hits():
    replay = audit.Replay(False)
    req = audit.request([1, 2], [3, 4])
    assert not replay.score(response(enabled=False), req)
    assert not replay.score(response(calls=2, enabled=False), req)


@pytest.mark.parametrize("hit,calls,hits,enabled", [(True, 0, 1, True), (False, 2, 0, True),
                                                   (False, True, 0, True), (False, 1, 0, 1)])
def test_replay_rejects_false_hits_native_work_and_counter_types(hit, calls, hits, enabled):
    with pytest.raises(audit.old.AuditError):
        audit.Replay().score(response(hit, calls, hits, enabled), audit.request([1, 2], [3, 4]))


@pytest.mark.parametrize("temperature", [1.22, .61])
def test_arbitrary_temperature_calibration_is_independently_recomputed(temperature):
    req = audit.request([1, 2], [3, 4], temperature=temperature)
    assert audit.response(response(temperature=temperature), req, 16, False) <= 1e-12


@pytest.mark.parametrize("field,value", [
    ("candidate_probabilities", [.5, .5]), ("logits", [float("nan"), -1.0]),
    ("log_probabilities", [.1, -2.9]), ("candidate_token_ids", [4, 3]),
    ("temperature", .61), ("truncated", True), ("cache_hit", True),
    ("metrics", {"prefill_seconds": 0.0}), ("prompt_tokens_sha256", "0" * 64),
])
def test_response_rejects_content_calibration_and_identity_tampering(field, value):
    item = response()
    item[field] = value
    with pytest.raises(audit.old.AuditError):
        audit.response(item, audit.request([1, 2], [3, 4]), 16, False)


def test_hit_cannot_report_native_prefill_work():
    value = response(True, 1, 1)
    value["metrics"]["prefill_seconds"] = .1
    with pytest.raises(audit.old.AuditError, match="cache_hit_native_work_invalid"):
        audit.response(value, audit.request([1, 2], [3, 4]), 16, True)


def test_bit_equality_rejects_signed_zero_types_and_one_ulp():
    assert not audit.exact(0.0, -0.0)
    assert not audit.exact(1.0, 1)
    assert not audit.exact(1.0, math.nextafter(1.0, math.inf))


def test_whole_vocabulary_digest_tampering_is_rejected():
    req = audit.request([1, 2], [3, 4])
    expected = {"input_tokens": 2, "prompt_tokens_sha256": audit.old.token_digest([1, 2]),
                "vocab_size": 16, "vocab_logits_sha256": "a" * 64}
    value = {**expected, "cache_hit": False, "truncated": False,
             "cache": {"enabled": True, "hits": 0, "native_score_calls": 1}}
    audit.vocabulary(value, req, expected, 16, audit.Replay())
    value["vocab_logits_sha256"] = "b" + "a" * 63
    with pytest.raises(audit.old.AuditError, match="vocabulary_bits_invalid"):
        audit.vocabulary(value, req, expected, 16, audit.Replay())


def test_sequence_plan_includes_all_error_and_readout_controls():
    plan = audit.sequence_plan([1, 2], list(range(16)))
    assert len(plan) == len(audit.NAMES) == 20
    assert plan[2]["input_token_ids"] != plan[0]["input_token_ids"]
    assert plan[6]["context_tokens"] != plan[0]["context_tokens"]
    assert plan[9]["candidate_token_ids"] == list(reversed(plan[0]["candidate_token_ids"]))
    assert plan[10]["candidate_token_ids"] == [2, 3]
    assert plan[11]["temperature"] == .61
