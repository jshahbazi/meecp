"""Pure validation tests for untimed deep scoring acceptance."""

import copy
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_uzu_scoring_deep as validator


def case():
    return {"id": "synthetic-case", "tier": "synthetic", "question": {
        "type": "choice", "instructions": "Choose the matching option.",
        "criteria": {"a": "Option A", "b": "Option B"}}}


def response():
    logits = [1.0, 0.0]
    partition = math.log(math.exp(1.0) + 1.0)
    log_probs = [value - partition for value in logits]
    return {"candidate_token_ids": [10, 11], "logits": logits,
        "log_probabilities": log_probs,
        "candidate_probabilities": validator.base.calibrated(logits),
        "temperature": validator.base.TEMPERATURE,
        "cache_hit": False, "truncated": False,
        "metrics": {name: 0.01 for name in validator.deep.METRICS}}


def expected():
    value = response()
    tokens = [1, 2, 3]
    return {"input_token_ids": tokens, "candidate_token_ids": value["candidate_token_ids"],
        "logits": value["logits"], "log_probabilities": value["log_probabilities"],
        "probabilities": value["candidate_probabilities"], "decision": "a"}


def worker_row():
    c = case()
    value = response()
    tokens = [1, 2, 3]
    value["input_tokens"] = len(tokens)
    value["prompt_tokens_sha256"] = validator.base.token_digest(tokens)
    decision = validator.base.typed_decision(c["question"], value["candidate_probabilities"], len(tokens))
    return ({"case_id": c["id"], "condition": "fresh", "input_token_ids": tokens,
        "response": value, "decision": decision}, c)


def test_worker_row_rejects_each_exact_output_mutation():
    row, c = worker_row()
    reference = validator.normalized_row(row)
    validator.validate_worker_row(row, c, reference)
    for field in ("input_token_ids", "candidate_token_ids", "logits", "log_probabilities", "probabilities"):
        changed = copy.deepcopy(row)
        if field == "input_token_ids":
            changed[field] = [1, 2, 4]
        elif field == "candidate_token_ids":
            changed["response"][field][0] += 1
        elif field == "logits":
            changed["response"][field][0] += 0.1
        elif field == "log_probabilities":
            changed["response"][field][0] += 0.1
        else:
            changed["response"]["candidate_probabilities"][0] += 0.01
        with pytest.raises(validator.base.EvaluationError):
            validator.validate_worker_row(changed, c, reference)


def test_worker_row_rejects_typed_decision_mutation():
    row, c = worker_row()
    row["decision"] = "b"
    with pytest.raises(validator.base.EvaluationError, match="typed_decision_invalid"):
        validator.validate_worker_row(row, c, expected())


def test_boundary_rejects_identity_and_response_mutations():
    value = response()
    value["input_tokens"] = 3
    value["prompt_tokens_sha256"] = validator.base.token_digest([1, 2, 3])
    reference = {"length": 3, "kind": "fresh", "tokens": [1, 2, 3], "response": value}
    validator.validate_boundary(reference, reference)
    changed = copy.deepcopy(reference)
    changed["tokens"][-1] = 4
    with pytest.raises(validator.base.EvaluationError, match="boundary_identity_invalid"):
        validator.validate_boundary(changed, reference)
    changed = copy.deepcopy(reference)
    changed["response"]["logits"][0] += 1e-8
    partition = math.log(sum(math.exp(v) for v in changed["response"]["logits"]))
    changed["response"]["log_probabilities"] = [v - partition for v in changed["response"]["logits"]]
    changed["response"]["candidate_probabilities"] = validator.base.calibrated(changed["response"]["logits"])
    with pytest.raises(validator.base.EvaluationError, match="boundary_equality_failed"):
        validator.validate_boundary(changed, reference)


def test_digest_requires_exact_vocabulary_identity():
    value = {"input_tokens": 3, "prompt_tokens_sha256": validator.base.token_digest([1, 2, 3]),
        "cache_hit": False, "truncated": False, "vocab_size": 100,
        "vocab_logits_sha256": "a" * 64}
    validator.validate_digest(value, [1, 2, 3], value, 100)
    for field in ("vocab_logits_sha256", "vocab_size", "prompt_tokens_sha256", "input_tokens"):
        changed = dict(value)
        changed[field] = ("b" * 64 if field == "vocab_logits_sha256"
                          else ("c" * 64 if field == "prompt_tokens_sha256" else value[field] + 1))
        with pytest.raises(validator.base.EvaluationError):
            validator.validate_digest(changed, [1, 2, 3], value, 100)


def test_calibration_error_reports_formula_mismatch():
    value = response()
    assert validator.calibration_error(value) < 1e-12
    value["candidate_probabilities"][0] += 0.02
    assert validator.calibration_error(value) > 0.01


def test_exported_configuration_is_scalar_and_content_free():
    config = {"files": {"/Users/private/model": "a" * 64,
                        "/workspace/file": "b" * 64},
        "candidate_receipt": {"variant": "all", "upstream_revision": "rev",
                               "features": ["probability-scoring"]},
        "case_ids": ["case-a", "case-b"], "context_tokens": 8192,
        "temperature": 1.22}
    result = validator.exported_configuration(config)
    assert result["ordered_case_ids_sha256"] == validator.base.sha(validator.base.encoded(config["case_ids"]))
    assert all("path" not in item or "/Users" not in item["path"] for item in result["artifacts"])
    assert "case_ids" not in result and "input_token_ids" not in result
    assert all(isinstance(item.get("sha256"), str) for item in result["artifacts"])
