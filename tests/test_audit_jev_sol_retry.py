"""Independent acceptance arithmetic and rejection checks; no model inference."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_jev_sol_retry as audit


def fixture(engine="uzu", condition="fresh"):
    case = {"id": "fixture", "state": "fixture", "question": {"type": "choice",
        "instructions": "Choose.", "criteria": {"first": "One", "second": "Two"}}, "expected": "first"}
    probabilities = audit.recalibrate([1., 0.])
    tokens = list(range(10))
    row = {"case_id": "fixture", "condition": condition, "engine": engine,
        "input_tokens": 10, "input_token_ids": tokens, "candidate_token_ids": [100, 101],
        "prompt_sha256": audit.sha(audit.rendered(case).encode()), "prompt_tokens_sha256": audit.token_digest(tokens),
        "logits": [1., 0.] if engine in ("uzu", "uzu-control") else None, "log_probabilities": [-.5, -1.5],
        "probabilities": probabilities, "decision": audit.decision(case, probabilities, 10),
        "truncated": False, "cache_hit": engine == "llama.cpp" and condition == "identical_repeat",
        "tokenization_seconds": .001, "score_request_seconds": .01, "wall_seconds": .012,
        "engine_metrics": {k: .001 for k in ("state_creation_seconds", "prefill_seconds", "readout_seconds",
                                            "score_seconds", "total_seconds")} if engine in ("uzu", "uzu-control") else {
            "prefill_seconds": .001, "one_token_seconds": .001,
            "prompt_tokens_evaluated": 4 if condition == "identical_repeat" else 10}}
    return case, row, copy.deepcopy(row)


def test_exactness_retains_signed_zero_and_dictionary_order():
    assert not audit.exact([0.], [-0.])
    assert not audit.exact({"a": 1, "b": 2}, {"b": 2, "a": 1})


@pytest.mark.parametrize("engine", ["uzu", "uzu-control"])
def test_bit_preservation_rejects_one_ulp_change(engine):
    case, row, reference = fixture(engine)
    assert audit.validate_row(row, case, reference, row["engine"], "fresh") < 1e-12
    row["probabilities"][0] += 1e-16
    row["decision"] = audit.decision(case, row["probabilities"], 10)
    with pytest.raises(audit.AuditError, match="native_output_bits_invalid"):
        audit.validate_row(row, case, reference, row["engine"], "fresh")


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
        audit.validate_row(row, case, reference, row["engine"], "fresh")


def test_ordered_argmax_and_brier_ece_definitions():
    case, row, _ = fixture()
    row["probabilities"] = [.5, .5]
    row["decision"] = audit.decision(case, row["probabilities"], 10)
    quality = audit.quality_summary([audit.quality_row(case, row)])
    assert row["decision"]["choice"] == "first"
    assert quality["brier"] == .5
    assert quality["ece_10_bins"] == .5
    assert quality["argmax_correct"] == 1


@pytest.mark.parametrize("left", ["llama.cpp", "uzu-control"])
def test_cycle_arithmetic_uses_process_means_and_rejects_mismatch(left):
    blocks = []
    for engine, value in ((left, 2.), ("uzu", 1.), ("uzu", 3.), (left, 4.)):
        blocks.append({"engine": engine, "rows": [{"condition": "fresh", "case_id": "fixture",
            "wall_seconds": value, "score_request_seconds": value / 2}]})
    assert audit.pair_metrics(blocks, "fresh", {"fixture"}, left)["wall_ratio_of_means"] == 1.5
    assert audit.pair_metrics(blocks, "fresh", set(), left)["cases"] == 0
    blocks[1]["rows"] = []
    with pytest.raises(audit.AuditError, match="cycle_pairing_invalid"):
        audit.pair_metrics(blocks, "fresh", {"fixture"}, left)


def test_schedule_mirrors_each_engine_twice_per_cycle():
    schedule = audit.schedule()
    assert len(schedule) == 12
    for cycle in (1, 2):
        engines = [b["engine"] for b in schedule if b["cycle"] == cycle]
        assert engines == engines[::-1]
        assert all(engines.count(engine) == 2 for engine in audit.ENGINES)
    assert [b["block"] for b in schedule] == list(range(1, 13))


@pytest.mark.parametrize("changed", ["status", "report", "configuration_sha256",
    "whole_vocabulary_logits_exactly_equal", "candidate_float_bits_equal", "public_report_matches_independent_recalculation"])
def test_acceptance_requires_bound_passing_independent_audit(monkeypatch, changed):
    receipt = {"variant": "combined", "binary_sha256": "fixture"}
    report = {"status": "complete", "candidate_outputs_exactly_equal": True,
        "whole_vocabulary_logits_exactly_equal": True, "configuration_sha256": "config",
        "configuration": {"candidate_receipt_sha256": audit.sha(audit.encoded(receipt))}}
    audited = {"status": "passed", "report": {"sha256": "reporthash"},
        "configuration_sha256": "config", "whole_vocabulary_logits_exactly_equal": True,
        "candidate_float_bits_equal": True, "public_report_matches_independent_recalculation": True}
    monkeypatch.setattr(audit, "read", lambda path, private=False: report if path == "report" else audited)
    monkeypatch.setattr(audit, "file_hash", lambda path: "reporthash")
    assert audit.checked_acceptance("report", "audit", receipt) == report
    audited[changed] = {"sha256": "wrong"} if changed == "report" else "wrong"
    with pytest.raises(audit.AuditError, match="acceptance_audit_invalid"):
        audit.checked_acceptance("report", "audit", receipt)


def test_acceptance_requires_exact_receipt(monkeypatch):
    report = {"status": "complete", "candidate_outputs_exactly_equal": True,
        "whole_vocabulary_logits_exactly_equal": True,
        "configuration": {"candidate_receipt_sha256": "wrong"}}
    monkeypatch.setattr(audit, "read", lambda path, private=False: report)
    with pytest.raises(audit.AuditError, match="acceptance_receipt_invalid"):
        audit.checked_acceptance("report", "audit", {"variant": "combined"})


@pytest.mark.parametrize("cpu,passes", [(200., True), (200.01, False)])
def test_host_audit_enforces_background_cpu_ceiling(monkeypatch, cpu, passes):
    samples = [{"external_models": 0, "generic_inference": 0, "compilers": 0,
        "owned_models": 0, "background_large_rss": 0, "elapsed_seconds": 0,
        "background_process_max_cpu": cpu, "completed_blocks": 12}]
    receipt = {"benchmark_exit": 0, "failure_type": None, "host_samples_sha256": "hash",
        "controller_sha256": "hash", "sampling_interval_seconds": 5, "samples": 1,
        "suspended_processes": 0, "external_model_samples": 0, "generic_inference_samples": 0,
        "compiler_samples": 0, "background_cpu_limit_percent": 200,
        "high_background_cpu_samples": int(cpu > 200), "max_owned_models": 0,
        "final_active_models": 0, "maximum_background_process_cpu": cpu,
        "maximum_background_large_rss": 0}
    monkeypatch.setattr(audit, "read", lambda path, private=True:
        samples if path.name == "HOST_SAMPLES.json" else receipt)
    monkeypatch.setattr(audit, "file_hash", lambda path: "hash")
    config = {"files": {str(audit.ROOT / "scripts/monitor_jev_sol_retry.py"): "hash"}}
    if passes:
        assert audit.host_audit(Path("fixture"), config)["high_background_cpu_samples"] == 0
    else:
        with pytest.raises(audit.AuditError, match="host_isolation_invalid"):
            audit.host_audit(Path("fixture"), config)


def test_retry_audit_changes_only_versioned_monitor_binding_and_keeps_stdlib():
    import ast
    import sys
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/audit_jev_sol_retry.py").read_text()
    assert source == (root / "scripts/audit_jev_sol.py").read_text().replace(
        "monitor_jev_sol.py", "monitor_jev_sol_retry.py")
    tree = ast.parse(source)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            modules.add(node.module.split(".")[0])
    assert modules <= sys.stdlib_module_names
