"""Independent timing arithmetic and fail-closed audit checks; no native work."""
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/analyze_uzu_deep_timing.py"
spec = importlib.util.spec_from_file_location("independent_deep_timing", SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def quartet():
    # Paired case means are baseline [3, 30], candidate [1.5, 5].
    # The ratio of means is 33/6.5, distinct from the mean ratios (2+6)/2.
    result = []
    for engine, values in (("baseline", (2., 20.)), ("candidate", (1., 4.)),
                           ("candidate", (2., 6.)), ("baseline", (4., 40.))):
        result.append({"phase": "comparison", "engine": engine, "rows": [
            {"case_id": str(i), "condition": condition, "wall_seconds": value,
             "score_request_seconds": value / 2, "engine_metrics": {m: value / 4 for m in audit.NATIVE}}
            for i, value in enumerate(values) for condition in audit.CONDITIONS]})
    return result


def test_quartet_averages_replicates_before_ratio_of_means():
    value = audit.quartet_summary(quartet(), "fresh")
    assert value["cases"] == 2
    assert value["metrics"]["wall_seconds"]["baseline_over_candidate_ratio_of_means"] == 33 / 6.5
    assert value["metrics"]["wall_seconds"]["paired_case_ratio"]["p50"] == 4
    assert value["metrics"]["wall_seconds"]["paired_candidate_minus_baseline_seconds"]["p50"] == -13.25


def test_bucket_subset_preserves_pairing_and_repeat_condition():
    value = audit.quartet_summary(quartet(), "identical_repeat", {"1"})
    assert value["cases"] == 1
    assert all(m["baseline_over_candidate_ratio_of_means"] == 6 for m in value["metrics"].values())


def test_quartet_rejects_permuted_case_order():
    blocks = quartet()
    blocks[1]["rows"] = list(reversed(blocks[1]["rows"]))
    with pytest.raises(audit.AuditError, match="quartet_pairing_invalid"):
        audit.quartet_summary(blocks, "fresh")


def test_zero_component_reports_unavailable_ratio():
    blocks = quartet()
    for block in blocks:
        for row in block["rows"]:
            row["engine_metrics"]["readout_seconds"] = 0
    value = audit.quartet_summary(blocks, "fresh")["metrics"]["readout_seconds"]
    assert value["baseline_over_candidate_ratio_of_means"] is None
    assert value["paired_case_ratio"]["samples"] == 0


def test_float_exactness_distinguishes_signed_zero():
    assert audit.exact({"value": [1., -0.]}, {"value": [1., -0.]})
    assert not audit.exact({"value": [1., -0.]}, {"value": [1., 0.]})


def test_selection_is_length_only_and_stable_for_ties():
    cases, rows = [], []
    for bucket, length in enumerate((100, 500, 1500)):
        for i in range(8):
            identity = f"{bucket}-{i}"
            cases.append({"id": identity, "tier": "easy"})
            rows.append({"case_id": identity, "condition": "fresh", "input_tokens": length})
    chosen = audit.selection(cases, rows)
    assert [s["case_id"] for s in chosen] == [f"{b}-{i}" for b in range(3) for i in (1, 3, 5, 7)]
    assert [s["bucket"] for s in chosen] == [b for b in ("short", "medium", "long") for _ in range(4)]


def test_schedule_has_four_controls_and_counterbalanced_quartets():
    result = audit.schedule()
    assert len(result) == 20
    assert sum(b["phase"] == "control" for b in result) == 4
    assert [b["engine"] for b in result if b["quartet"] == 2 and b["phase"] == "comparison"] == [
        "candidate", "baseline", "baseline", "candidate"]


def test_private_read_rejects_public_permissions(tmp_path):
    path = tmp_path / "record.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(audit.AuditError, match="private_file_invalid"):
        audit.read(path)


def test_bind_files_rejects_changed_artifact(tmp_path):
    path = tmp_path / "artifact"
    path.write_bytes(b"first")
    expected = audit.file_hash(path)
    path.write_bytes(b"second")
    with pytest.raises(audit.AuditError, match="artifact_changed"):
        audit.bind_files({str(path): expected}, {})
