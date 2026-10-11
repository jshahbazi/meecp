"""Independent audit rejects type/bit/content tampering without native inference."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/audit_uzu_sol.py"
SPEC = importlib.util.spec_from_file_location("independent_sol_audit", SOURCE)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def response():
    values = [2.0, -1.0]
    return {"input_tokens": 2, "prompt_tokens_sha256": audit.token_digest([1, 2]),
        "candidate_token_ids": [3, 4], "temperature": 1.22, "truncated": False, "cache_hit": False,
        "logits": values, "log_probabilities": [-0.1, -3.1],
        "candidate_probabilities": audit.recalibrate(values),
        "metrics": dict.fromkeys(("state_creation_seconds", "prefill_seconds", "readout_seconds",
                                  "score_seconds", "total_seconds"), 0.0)}


def case_and_row():
    case = {"id": "synthetic-audit", "question": {"type": "choice", "criteria": {"a": "a", "b": "b"}}}
    value = response()
    row = {"case_id": case["id"], "condition": "fresh", "input_token_ids": [1, 2], "response": value,
        "decision": audit.decision(case, value["candidate_probabilities"], 2)}
    return case, row, audit.normalized_row(row)


def test_response_recalibration_and_metrics():
    assert audit.validate_response(response(), [1, 2], [3, 4], 16) <= 1e-12


@pytest.mark.parametrize("key,value", [
    ("cache_hit", True), ("truncated", True), ("temperature", 1.0),
    ("prompt_tokens_sha256", "0" * 64), ("candidate_token_ids", [4, 3]),
    ("log_probabilities", [0.1, -2.9]), ("candidate_probabilities", [0.5, 0.5]),
    ("logits", [float("nan"), -1.0]), ("metrics", {"prefill_seconds": 0.0}),
])
def test_response_rejects_invalid_identity_or_calibration(key, value):
    mutated = response()
    mutated[key] = value
    with pytest.raises(audit.AuditError):
        audit.validate_response(mutated, [1, 2], [3, 4], 16)


def test_worker_equality_rejects_single_float_bit_change():
    case, row, expected = case_and_row()
    assert audit.validate_worker_row(row, case, expected, 16) <= 1e-12
    expected["logits"] = list(expected["logits"])
    expected["logits"][0] = math.nextafter(expected["logits"][0], math.inf)
    with pytest.raises(audit.AuditError, match="candidate_output_bits_invalid"):
        audit.validate_worker_row(row, case, expected, 16)


def test_worker_equality_rejects_typed_decision_change():
    case, row, expected = case_and_row()
    row["decision"] = dict(row["decision"], choice="b")
    with pytest.raises(audit.AuditError):
        audit.validate_worker_row(row, case, expected, 16)


def test_binary64_equality_preserves_types_and_signed_zero():
    assert not audit.exact(0.0, -0.0)
    assert not audit.exact(1.0, 1)
    assert not audit.exact(True, 1)
    assert audit.exact([0.0, 1.0], [0.0, 1.0])


def test_boundary_token_digest_accepts_full_context_and_rejects_oversize():
    assert len(audit.token_digest([1] * 8192)) == 64
    with pytest.raises(audit.AuditError):
        audit.token_digest([1] * 8193)
    with pytest.raises(audit.AuditError):
        audit.token_digest([True])


def test_whole_vocabulary_digest_rejects_single_hex_change():
    value = {"input_tokens": 2, "prompt_tokens_sha256": audit.token_digest([1, 2]),
        "cache_hit": False, "truncated": False, "vocab_size": 16, "vocab_logits_sha256": "a" * 64}
    audit.validate_digest(value, [1, 2], dict(value), 16)
    expected = dict(value, vocab_logits_sha256="b" + "a" * 63)
    with pytest.raises(audit.AuditError, match="vocabulary_bits_invalid"):
        audit.validate_digest(value, [1, 2], expected, 16)
