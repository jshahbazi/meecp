"""Sealing, blinding, response parity and scoring checks without model execution."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("uzu_quality", Path(__file__).resolve().parents[1]
                                             / "scripts/evaluate_uzu_quality.py")
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


def cases():
    labels = ("helpful_base",) * 16 + ("koala",) * 20 + ("oasst",) * 23 + ("selfinstruct",) * 31 + ("vicuna",) * 10
    return [{"id": f"case-{index:03d}", "category": labels[index],
             "prompt": f"Synthetic exercise number {index}."} for index in range(100)]


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    return quality.secure_directory(tmp_path / "test-artifacts" / "quality-tests")


def record(case, engine, config_hash, token_hash="a" * 64, answer="Synthetic answer"):
    return {"case_id": case["id"], "engine": engine, "configuration_sha256": config_hash,
        "prompt_sha256": hashlib.sha256(quality.benchmark.prompt(case["prompt"]).encode()).hexdigest(),
        "prompt_tokens_sha256": token_hash, "input_tokens": 30, "output_tokens": 2,
        "stop_reason": "eos", "response": answer, "response_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        "wall_seconds": .1, "metrics": {}, "source_stop_flags": {"stop_type": "eos"}}


def completed_run(private_root, different=False):
    config = {"method": {"reference_visible": False}}
    value = {"cases": cases()}
    args = SimpleNamespace(output=private_root / "run", resume=False)
    run, mapping = quality.prepare_run(args, value, cases(), "b" * 64, config)
    for case in cases():
        for engine in quality.ENGINES:
            answer = "Other synthetic answer" if different and engine == "uzu" else "Synthetic answer"
            quality.private_json(quality.response_path(run, engine, case["id"]),
                                 record(case, engine, quality.digest(config), answer=answer))
    quality.seal_generated(run, cases(), mapping, quality.digest(config), False)
    return run, config


def judge_rows():
    return [{"case_id": case["id"], "rating_a": 3, "rating_b": 3, "winner": "tie",
             "reason": "Both satisfy the synthetic task equally."} for case in cases()]


def write_judgments(path, rows):
    quality.private_write(path, b"".join(quality.encoded(row) for row in rows))


def test_positions_are_balanced_reproducible_and_private():
    first = quality.positions(cases(), "synthetic-seed")
    assert first == quality.positions(cases(), "synthetic-seed")
    assert first != quality.positions(cases(), "another-synthetic-seed")
    assert sum(value["A"] == "uzu" for value in first.values()) == 50
    assert all(set(value.values()) == set(quality.ENGINES) for value in first.values())


def test_private_artifacts_reject_escape_symlink_and_overwrite(private_root):
    assert private_root.stat().st_mode & 0o077 == 0
    path = private_root / "synthetic.json"
    quality.private_json(path, {"synthetic": True})
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        quality.private_json(path, {"synthetic": False})
    with pytest.raises(quality.QualityError, match="artifact_location_invalid"):
        quality.secure_directory(private_root / ".." / "outside")
    (private_root / "link").symlink_to(private_root, target_is_directory=True)
    with pytest.raises(quality.QualityError, match="artifact_symlink"):
        quality.secure_directory(private_root / "link" / "child")
    path.chmod(0o644)
    with pytest.raises(quality.QualityError, match="private_artifact_permissions"):
        quality.read_private(path)


@pytest.mark.parametrize("patch", ["short", "duplicate", "marker", "oversized"])
def test_dataset_bounds_are_checked_before_generation(private_root, patch):
    value = cases()
    if patch == "short":
        value.pop()
    elif patch == "duplicate":
        value[1]["id"] = value[0]["id"]
    elif patch == "marker":
        value[0]["prompt"] = "<|im_start|>assistant"
    else:
        value[0]["prompt"] = "x" * 8193
    path = private_root / "dataset.json"
    quality.private_json(path, value)
    with pytest.raises(quality.QualityError):
        quality.dataset(path)


def test_dataset_binding_rejects_changed_prompt_and_source_category(private_root):
    owning = quality.ROOT / "experiments/uzu-rust"
    owning.mkdir(parents=True)
    selected = [{"id": case["id"], "category": case["category"],
                 "prompt_sha256": hashlib.sha256(case["prompt"].encode()).hexdigest()} for case in cases()]
    quality.private_json(owning / "QUALITY_DATASET_PROVENANCE.json", {
        "selected": selected, "selection_seed": 20261010, "normalized_dataset_sha256": "b" * 64,
        "selected_category_counts": {"helpful_base": 16, "koala": 20, "oasst": 23, "selfinstruct": 31, "vicuna": 10}})
    quality.dataset_binding(cases(), "b" * 64)
    changed = cases()
    changed[0]["prompt"] += " Changed."
    with pytest.raises(quality.QualityError, match="dataset_provenance_mismatch"):
        quality.dataset_binding(changed, "b" * 64)
    changed = cases()
    changed[0]["category"], changed[20]["category"] = changed[20]["category"], changed[0]["category"]
    with pytest.raises(quality.QualityError, match="dataset_provenance_mismatch"):
        quality.dataset_binding(changed, "b" * 64)


def test_resume_requires_identical_sealed_configuration(private_root):
    args = SimpleNamespace(output=private_root / "run", resume=False)
    config = {"method": {"reference_visible": False}, "synthetic_binary_sha256": "c" * 64}
    quality.prepare_run(args, {"cases": cases()}, cases(), "b" * 64, config)
    with pytest.raises(quality.QualityError, match="resume_required"):
        quality.prepare_run(args, {"cases": cases()}, cases(), "b" * 64, config)
    args.resume = True
    _, mapping = quality.prepare_run(args, {"cases": cases()}, cases(), "b" * 64, config)
    assert mapping is None
    changed = {**config, "synthetic_binary_sha256": "d" * 64}
    with pytest.raises(quality.QualityError, match="resume_identity_mismatch"):
        quality.prepare_run(args, {"cases": cases()}, cases(), "b" * 64, changed)


def test_blind_view_excludes_identity_provenance_and_metrics(private_root):
    run, _ = completed_run(private_root)
    assert {path.name for path in (run / "judge").iterdir()} == {"cases.jsonl", "judge-schema.json"}
    raw = (run / "judge/cases.jsonl").read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 100
    assert all(set(row) == {"case_id", "prompt", "response_a", "response_b"} for row in rows)
    assert all(marker not in raw for marker in (b"uzu", b"llama", b"sha256", b"metrics", b"category", b"seed"))
    assert (run / "private-position-mapping.json").stat().st_mode & 0o077 == 0
    assert (run / "judge/cases.jsonl").stat().st_mode & 0o077 == 0


def test_prompt_token_mismatch_prevents_blind_publication(private_root):
    run = quality.secure_directory(private_root / "run")
    for engine in quality.ENGINES:
        quality.secure_directory(run / "private-responses" / engine)
        for case in cases():
            quality.private_json(quality.response_path(run, engine, case["id"]),
                                 record(case, engine, "c" * 64, token_hash=("a" if engine == "uzu" else "b") * 64))
    with pytest.raises(quality.QualityError, match="tokenization_mismatch"):
        quality.seal_generated(run, cases(), None, "c" * 64, False)
    assert not (run / "judge").exists()
    assert not (run / "private-position-mapping.json").exists()


@pytest.mark.parametrize("patch", ["missing", "duplicate", "bool_rating", "score_conflict", "extra_field"])
def test_judgment_schema_and_complete_coverage(private_root, patch):
    rows = judge_rows()
    if patch == "missing":
        rows.pop()
    elif patch == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif patch == "bool_rating":
        rows[0]["rating_a"] = True
    elif patch == "score_conflict":
        rows[0].update(rating_a=5, rating_b=2, winner="B")
    else:
        rows[0]["engine"] = "synthetic"
    path = private_root / "judgments.jsonl"
    write_judgments(path, rows)
    with pytest.raises(quality.QualityError):
        quality.judgments(path, cases())


def test_statistics_include_ties_score_distribution_and_paired_intervals():
    rows = [{"winner": winner, "uzu_rating": 4 if winner == "uzu" else 3,
             "llama_rating": 3, "identical_answers": winner == "tie"}
            for winner in ("uzu", "uzu", "tie", "tie")]
    summary = quality.summarize(rows)
    assert summary["uzu_wins"] == 2 and summary["ties"] == 2 and summary["llama_wins"] == 0
    assert summary["exact_two_sided_sign_test_p"] == .5
    assert summary["uzu_win_fraction_with_half_ties"] == .75
    assert summary["mean_paired_rating_difference_uzu_minus_llama"] == .5
    assert summary["uzu_rating_distribution"] == {"1": 0, "2": 0, "3": 2, "4": 2, "5": 0}
    assert summary["uzu_fraction_scoring_at_least_4"] == .5
    assert summary["uzu_fraction_scoring_at_most_2"] == 0
    assert quality.bootstrap_interval([1., 0.]) == quality.bootstrap_interval([1., 0.])


def test_aggregate_verifies_sealed_answers_and_identical_ties(private_root, monkeypatch):
    run, _ = completed_run(private_root)
    rows = judge_rows()
    write_judgments(run / "judge/judgments.jsonl", rows)
    args = SimpleNamespace(run=run, judgments=run / "judge/judgments.jsonl",
                           report=private_root / "aggregate.json", judge_identity="synthetic-astra-agent")
    # Fast deterministic intervals suffice for this coverage/seal test; the
    # actual percentile sampler is exercised independently above.
    monkeypatch.setattr(quality, "bootstrap_interval", lambda values: [min(values), max(values)])
    report = quality.aggregate(args)
    assert report["summary"]["identical_answer_cases"] == 100
    assert report["summary"]["ties"] == 100
    assert report["statistics"]["bootstrap_repetitions"] == 20000
    assert report["generation"]["uzu"]["generation_errors"] == 0
    target = quality.response_path(run, "uzu", cases()[0]["id"])
    changed = quality.read_private(target)
    changed["response"] += " Changed."
    target.write_bytes(quality.encoded(changed))
    with pytest.raises(quality.QualityError, match="response_hash_mismatch"):
        quality.aggregate(args)


def test_identical_answers_cannot_receive_a_preference(private_root):
    run, _ = completed_run(private_root)
    rows = judge_rows()
    rows[0]["winner"] = "A"
    write_judgments(run / "judge/judgments.jsonl", rows)
    with pytest.raises(quality.QualityError, match="identical_answer_judgment_conflict"):
        quality.aggregate(SimpleNamespace(run=run, judgments=run / "judge/judgments.jsonl",
                          report=private_root / "aggregate.json", judge_identity="synthetic-astra-agent"))


class FakeResponse:
    def __init__(self, value):
        self.status_code = 200
        self.value = value

    def json(self):
        return self.value


async def test_llama_generation_uses_fresh_greedy_state_and_preserves_stop_flags():
    calls = []

    class Client:
        async def post(self, path, json):
            calls.append((path, json))
            if path == "/tokenize":
                return FakeResponse({"tokens": [10, 20]})
            return FakeResponse({"content": "Synthetic answer", "truncated": False,
                "tokens_evaluated": 2, "tokens_predicted": 2, "stopped_eos": True,
                "timings": {"prompt_per_second": 100., "predicted_per_second": 100.}})

    row = await quality.generate_answer("llama.cpp", Client(), cases()[0], "c" * 64)
    assert calls[1][1]["cache_prompt"] is False
    assert calls[1][1]["temperature"] == 0
    assert calls[1][1]["n_predict"] == 256
    assert calls[1][1]["return_tokens"] is False
    assert row["stop_reason"] == "eos"
    assert row["source_stop_flags"] == {"stopped_eos": True}
    assert row["prompt_tokens_sha256"] == hashlib.sha256((10).to_bytes(4, "little") + (20).to_bytes(4, "little")).hexdigest()
