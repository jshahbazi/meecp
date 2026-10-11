"""Length-only selection and quartet/process-control analysis are deterministic."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_uzu_scoring_latency as latency


def selection_input():
    cases, rows = [], []
    for bucket, start in (("short", 100), ("medium", 300), ("long", 1100)):
        for index in range(8):
            case_id = f"{bucket}-{index}"
            cases.append({"id": case_id, "tier": "easy"})
            rows.append({"case_id": case_id, "condition": "fresh", "input_tokens": start + index,
                         "wall_seconds": 10 - index})
    return cases, rows


def test_selection_uses_lengths_only_and_has_four_per_bucket():
    cases, rows = selection_input()
    chosen = latency.selected_cases(cases, rows)
    assert [s["case_id"] for s in chosen] == [f"{bucket}-{i}" for bucket in ("short", "medium", "long")
                                            for i in (1, 3, 5, 7)]
    for row in rows:
        row["wall_seconds"] *= 100
    assert latency.selected_cases(cases, rows) == chosen


def test_selection_preserves_dataset_order_for_equal_lengths():
    cases, rows = selection_input()
    for row in rows[:8]:
        row["input_tokens"] = 101
    assert [s["case_id"] for s in latency.selected_cases(cases, rows)[:4]] == [f"short-{i}" for i in (1, 3, 5, 7)]


def test_selection_excludes_synthetic_and_accepts_exactly_four_per_bucket():
    cases, rows = selection_input()
    for i, case in enumerate(cases):
        if i % 2:
            case["tier"] = "synthetic"
    selected = latency.selected_cases(cases, rows)
    assert len(selected) == 12
    assert all(int(s["case_id"].split("-")[-1]) % 2 == 0 for s in selected)


def test_insufficient_bucket_fails():
    cases, rows = selection_input()
    with pytest.raises(latency.base.EvaluationError, match="selection_bucket_too_small"):
        latency.selected_cases(cases[5:], rows)


def test_schedule_is_counterbalanced_with_independent_controls():
    blocks = latency.schedule(4)
    assert len(blocks) == 20
    assert sum(b["phase"] == "control" for b in blocks) == 4
    assert [b["block"] for b in blocks] == list(range(1, 21))
    for q, expected in enumerate(("ABBA", "BAAB", "ABBA", "BAAB"), 1):
        group = [b for b in blocks if b["quartet"] == q]
        assert group[0]["phase"] == "control" and group[0]["engine"] == "baseline"
        assert "".join("A" if b["engine"] == "baseline" else "B" for b in group[1:]) == expected
    assert len(latency.schedule(2)) == 10
    with pytest.raises(latency.base.EvaluationError):
        latency.schedule(3)


def timing_row(case, seconds):
    return {"case_id": case, "condition": "fresh", "wall_seconds": seconds,
        "score_request_seconds": seconds, "tokenization_seconds": 0, "cache_hit": False,
        "engine_metrics": {key: seconds for key in latency.opt.METRICS}}


def quartet():
    return [{"phase": "comparison", "engine": engine,
             "rows": [timing_row("a", value), timing_row("b", value * 2)]}
            for engine, value in (("baseline", 1), ("candidate", .5), ("candidate", 1.5), ("baseline", 3))]


def test_quartet_averages_within_case_before_ratio():
    result = latency.quartet_summary(quartet(), "fresh")
    wall = result["metrics"]["wall_seconds"]
    assert result["cases"] == 2
    assert wall["baseline_over_candidate_ratio_of_means"] == 2
    assert wall["baseline_case_means"]["p50"] == 3
    assert wall["candidate_case_means"]["p50"] == 1.5
    assert wall["paired_candidate_minus_baseline_seconds"]["p50"] == -1.5
    assert "bootstrap" not in json.dumps(result)


def test_quartet_rejects_unbalanced_or_misaligned_processes():
    blocks = quartet()
    with pytest.raises(latency.base.EvaluationError, match="quartet_incomplete"):
        latency.quartet_summary(blocks[:3], "fresh")
    blocks[0]["engine"] = "candidate"
    with pytest.raises(latency.base.EvaluationError, match="quartet_unbalanced"):
        latency.quartet_summary(blocks, "fresh")
    blocks = quartet()
    blocks[2]["rows"].reverse()
    with pytest.raises(latency.base.EvaluationError, match="quartet_pairing_invalid"):
        latency.quartet_summary(blocks, "fresh")


def test_controls_report_process_range_separately():
    blocks = [{"block": i, "phase": "control", "rows": [timing_row("a", v)]}
              for i, v in enumerate((1, 2, 3, 4), 1)]
    result = latency.control_summary(blocks, "fresh")
    assert result["independent_processes"] == 4
    assert result["metrics"]["wall_seconds"]["between_process_case_range_seconds"]["p50"] == 3
    assert result["metrics"]["wall_seconds"]["between_process_case_range_over_mean"]["p50"] == 1.2


def test_incomplete_sealed_blocks_rejected(tmp_path):
    cases, rows = selection_input()
    config = {"schedule": latency.schedule(2), "quartets": 2, "warmups": 3,
              "selection": latency.selected_cases(cases, rows)}
    digest = latency.base.sha(latency.base.encoded(config))
    latency.base.private_json(tmp_path / "pre-run-seal.json", {"configuration_sha256": digest})
    latency.base.private_json(tmp_path / "post-run-seal.json", {"configuration_sha256": digest,
                                                              "ownership_settled": True, "blocks": {}})
    with pytest.raises(latency.base.EvaluationError, match="blocks_incomplete"):
        latency.load_blocks(tmp_path, config, cases, rows)


def test_selection_change_cannot_be_resealed_as_valid(tmp_path):
    cases, rows = selection_input()
    config = {"schedule": latency.schedule(2), "quartets": 2, "warmups": 3,
              "selection": latency.selected_cases(cases, rows)}
    config["selection"][0]["case_id"] = "short-0"
    with pytest.raises(latency.base.EvaluationError, match="configuration_invalid"):
        latency.load_blocks(tmp_path, config, cases, rows)


def test_export_configuration_keeps_private_paths_and_case_ids_out():
    binding = {"schema_version": 1, "smoke": False, "rounds": 2, "case_ids": ["private-case"],
        "warmups": 3, "boundary_lengths": [], "temperature": 1.22, "context_tokens": 8192,
        "files": {str(latency.ROOT / "scripts/benchmark_uzu_scoring_latency.py"): "a" * 64,
                  "/Users/private-user/external": "b" * 64},
        "candidate_receipt": {"variant": "combined", "upstream_revision": latency.opt.REVISION,
            "features": ["probability-scoring"], "private": "/Users/private-user/receipt"},
        "digest_binaries": {}, "machine": {"architecture": "arm64", "os_version": "26.7.1",
            "chip_model": "Apple M5 Max", "hardware_model": "Mac17,1", "physical_ram_bytes": 128 * 1024**3,
            "serial_number": "private-serial", "unknown": "/Users/private-user/home"}}
    config = {"binding": binding, "quartets": 4, "warmups": 3, "schedule": latency.schedule(4),
              "selection": [{"case_id": "private-case", "bucket": "short", "input_tokens": 137}],
              "reference": "/Users/private-user/reference"}
    original = copy.deepcopy(config)
    result = latency.exported_configuration(config)
    text = json.dumps(result)
    assert "private-case" not in text and "private-user" not in text and "private-serial" not in text
    assert "/Users/" not in text and str(latency.ROOT) not in text
    assert result["selected_case_count"] == 1
    assert result["machine"]["chip_model"] == "Apple M5 Max"
    assert config == original
    binding["machine"]["chip_model"] = "/Users/private-user/machine"
    assert "chip_model" not in latency.exported_configuration(config)["machine"]
