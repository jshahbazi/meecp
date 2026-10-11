"""Full benchmark acceptance and instrument tests without native inference."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_jev_sol as bench
import monitor_jev_sol as monitor


def example(engine="uzu", condition="fresh"):
    case = bench.base.synthetic_cases()[0]
    probabilities = bench.base.calibrated([1., 0.])
    tokens = list(range(10))
    row = {"case_id": case["id"], "engine": engine, "condition": condition,
        "input_tokens": 10, "input_token_ids": tokens, "prompt_tokens_sha256": bench.base.token_digest(tokens),
        "prompt_sha256": bench.base.sha(bench.base.rendered(case).encode()),
        "candidate_token_ids": [100, 101], "logits": [1., 0.] if engine in ("uzu", "uzu-control") else None,
        "log_probabilities": [1.-1.5, 0.-1.5], "probabilities": probabilities,
        "decision": bench.base.typed_decision(case["question"], probabilities, 10),
        "cache_hit": engine == "llama.cpp" and condition == "identical_repeat", "truncated": False,
        "tokenization_seconds": .001, "score_request_seconds": .01, "wall_seconds": .012,
        "engine_metrics": {k: .001 for k in bench.deep.METRICS} if engine in ("uzu", "uzu-control") else {
            "prefill_seconds": .001, "one_token_seconds": .001,
            "prompt_tokens_evaluated": 4 if condition == "identical_repeat" else 10}}
    reference = {k: copy.deepcopy(row[k]) for k in bench.deep.IDENTITY}
    return case, row, reference


def test_schedule_counterbalances_complete_engine_loads():
    schedule = bench.schedule()
    assert len(schedule) == 12
    for cycle in (1, 2):
        group = [b["engine"] for b in schedule if b["cycle"] == cycle]
        assert group == group[::-1]
        assert all(group.count(e) == 2 for e in bench.ENGINES)
    assert all([b["engine"] for b in schedule].count(e) == 4 for e in bench.ENGINES)


@pytest.mark.parametrize("engine", ["uzu", "uzu-control"])
def test_native_probability_preservation_rejects_tiny_difference(engine):
    case, row, reference = example(engine)
    assert bench.validate_row(row, case, reference) < 1e-12
    row["probabilities"][0] += 1e-16
    row["decision"] = bench.base.typed_decision(case["question"], row["probabilities"], 10)
    with pytest.raises(bench.base.EvaluationError, match="uzu_preservation_failed"):
        bench.validate_row(row, case, reference)


@pytest.mark.parametrize("condition,evaluated", [("fresh", 4), ("identical_repeat", 10), ("identical_repeat", 0)])
def test_actual_llama_evaluation_count_rejects_invalid_reuse(condition, evaluated):
    case, row, reference = example("llama.cpp", condition)
    row["engine_metrics"]["prompt_tokens_evaluated"] = evaluated
    with pytest.raises(bench.base.EvaluationError, match="llama_evaluation_count_invalid"):
        bench.validate_row(row, case, reference)


def test_exactness_distinguishes_signed_zero():
    assert not bench.exact([0.], [-0.])


def test_generic_runtime_classification_keeps_argv_private():
    assert monitor.generic_inference("Python", "python serve.py --model /fixture/model")
    assert monitor.generic_inference("Python", "python /fixture/train_model.py")
    assert not monitor.generic_inference("Python", "python /fixture/recover.py")
    assert not monitor.generic_inference("claude", "claude --model remote-model")


def test_quartet_pairs_process_means_before_population_ratio():
    blocks = []
    for engine, value in (("llama.cpp", 2.), ("uzu", 1.), ("uzu", 3.), ("llama.cpp", 4.)):
        blocks.append({"engine": engine, "rows": [{"condition": "fresh", "case_id": "fixture",
            "wall_seconds": value, "score_request_seconds": value / 2}]})
    result = bench.pair_metrics(blocks, "fresh", {"fixture"}, "llama.cpp")
    assert result["wall_ratio_of_means"] == 1.5
    assert result["median_paired_wall_ratio"] == 1.5
    assert bench.pair_metrics(blocks, "fresh", set(), "llama.cpp")["cases"] == 0


def test_control_pair_uses_control_timings_and_ignores_llama():
    blocks = [{"engine": e, "rows": [{"condition": "fresh", "case_id": "fixture",
        "wall_seconds": t, "score_request_seconds": t}]}
        for e, t in (("uzu-control", 3.), ("uzu", 1.), ("llama.cpp", 99.),
                     ("uzu", 2.), ("uzu-control", 6.), ("llama.cpp", 99.))]
    assert bench.pair_metrics(blocks, "fresh", {"fixture"}, "uzu-control")["wall_ratio_of_means"] == 3.


def test_control_may_not_claim_identical_repeat_cache():
    case, row, reference = example("uzu-control", "identical_repeat")
    row["cache_hit"] = True
    with pytest.raises(bench.base.EvaluationError, match="cache_policy_invalid"):
        bench.validate_row(row, case, reference)
