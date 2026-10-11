"""Independent acceptance arithmetic and rejection checks; no model inference."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_jev_optimized as audit


def fixture(engine="uzu", condition="fresh"):
    case = {"id": "fixture", "state": "fixture", "question": {"type": "choice",
        "instructions": "Choose.", "criteria": {"first": "One", "second": "Two"}}, "expected": "first"}
    probabilities = audit.recalibrate([1., 0.])
    tokens = list(range(10))
    row = {"case_id": "fixture", "condition": condition, "engine": engine,
        "input_tokens": 10, "input_token_ids": tokens, "candidate_token_ids": [100, 101],
        "prompt_sha256": audit.sha(audit.rendered(case).encode()), "prompt_tokens_sha256": audit.token_digest(tokens),
        "logits": [1., 0.] if engine == "uzu" else None, "log_probabilities": [-.5, -1.5],
        "probabilities": probabilities, "decision": audit.decision(case, probabilities, 10),
        "truncated": False, "cache_hit": engine == "llama.cpp" and condition == "identical_repeat",
        "tokenization_seconds": .001, "score_request_seconds": .01, "wall_seconds": .012,
        "engine_metrics": {k: .001 for k in ("state_creation_seconds", "prefill_seconds", "readout_seconds",
                                            "score_seconds", "total_seconds")} if engine == "uzu" else {
            "prefill_seconds": .001, "one_token_seconds": .001,
            "prompt_tokens_evaluated": 4 if condition == "identical_repeat" else 10}}
    return case, row, copy.deepcopy(row)


def test_exactness_retains_signed_zero_and_dictionary_order():
    assert not audit.exact([0.], [-0.])
    assert not audit.exact({"a": 1, "b": 2}, {"b": 2, "a": 1})


def test_bit_preservation_rejects_one_ulp_change():
    case, row, reference = fixture()
    assert audit.validate_row(row, case, reference, "uzu", "fresh") < 1e-12
    row["probabilities"][0] += 1e-16
    row["decision"] = audit.decision(case, row["probabilities"], 10)
    with pytest.raises(audit.AuditError, match="native_output_bits_invalid"):
        audit.validate_row(row, case, reference, "uzu", "fresh")


@pytest.mark.parametrize("condition,evaluated", [("fresh", 4), ("identical_repeat", 10), ("identical_repeat", 0)])
def test_llama_actual_cache_work_is_required(condition, evaluated):
    case, row, reference = fixture("llama.cpp", condition)
    row["engine_metrics"]["prompt_tokens_evaluated"] = evaluated
    with pytest.raises(audit.AuditError, match="llama_cache_evaluation_invalid"):
        audit.validate_row(row, case, reference, "llama.cpp", condition)


def test_changed_prompt_hash_is_rejected():
    case, row, reference = fixture()
    row["prompt_sha256"] = "0" * 64
    with pytest.raises(audit.AuditError, match="prompt_identity_invalid"):
        audit.validate_row(row, case, reference, "uzu", "fresh")


def test_ordered_argmax_and_brier_ece_definitions():
    case, row, _ = fixture()
    row["probabilities"] = [.5, .5]
    row["decision"] = audit.decision(case, row["probabilities"], 10)
    quality = audit.quality_summary([audit.quality_row(case, row)])
    assert row["decision"]["choice"] == "first"
    assert quality["brier"] == .5
    assert quality["ece_10_bins"] == .5
    assert quality["argmax_correct"] == 1


def test_quartet_arithmetic_uses_process_means_and_rejects_mismatch():
    blocks = []
    for engine, value in (("llama.cpp", 2.), ("uzu", 1.), ("uzu", 3.), ("llama.cpp", 4.)):
        blocks.append({"engine": engine, "rows": [{"condition": "fresh", "case_id": "fixture",
            "wall_seconds": value, "score_request_seconds": value / 2}]})
    assert audit.quartet_metrics(blocks, "fresh", {"fixture"})["wall_ratio_of_means"] == 1.5
    assert audit.quartet_metrics(blocks, "fresh", set())["cases"] == 0
    blocks[1]["rows"] = []
    with pytest.raises(audit.AuditError, match="quartet_pairing_invalid"):
        audit.quartet_metrics(blocks, "fresh", {"fixture"})
