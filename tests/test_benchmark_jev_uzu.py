"""Numerical calibration, typed semantics, and fail-closed score validation."""
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("benchmark_jev_uzu", ROOT / "scripts/benchmark_jev_uzu.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
from jevk5.gguf import JevK5GGUF
from jevk5.prompt import spread


def response(logits=(2.0, -1.0)):
    tokens, candidates = [10, 11, 12], [32, 33]
    normalizer = math.log(sum(math.exp(z) for z in logits))
    value = {"input_tokens": len(tokens), "prompt_tokens_sha256": evaluation.token_digest(tokens),
        "candidate_token_ids": candidates, "logits": list(logits),
        "log_probabilities": [z - normalizer for z in logits],
        "candidate_probabilities": evaluation.calibrated(list(logits)), "temperature": 1.22,
        "cache_hit": False, "truncated": False, "metrics": {"prefill_seconds": .01, "score_seconds": .011}}
    return value, tokens, candidates


@pytest.mark.parametrize("logits", [[2.5, 1.1, -2.0, 0.3], [0.0, 0.0],
                                   [float(i) / 7 for i in range(16)], [10000.0, -10000.0]])
def test_calibration_matches_official_readout_and_offset_invariance(logits):
    class Official(JevK5GGUF):
        def _logprobs(self, prompt):
            return dict(zip(evaluation.LETTERS, logits)), 17, .1
    model = Official(temperature=1.22, knockout_temperature=.93)
    question = {"type": "choice", "instructions": "Choose.", "criteria": [str(i) for i in range(len(logits))]}
    probabilities, count = model.probabilities("state", question)
    expected = evaluation.calibrated(logits)
    assert list(probabilities.values()) == pytest.approx(expected, abs=1e-12)
    assert count == 17
    assert evaluation.calibrated([z - 200 for z in logits]) == pytest.approx(expected, abs=1e-12)


def test_stable_softmax_handles_extreme_scores():
    assert evaluation.calibrated([10000.0, -10000.0]) == [1.0, 0.0]
    assert evaluation.calibrated([0.0] * 16) == [1 / 16] * 16


@pytest.mark.parametrize("values", [[1], [0] * 17, [0, float("nan")], [0, float("inf")], [True, 0]])
def test_calibration_rejects_invalid_candidates(values):
    with pytest.raises(evaluation.EvaluationError, match="logits_invalid"):
        evaluation.calibrated(values)


def test_knockout_temperature_inactive_for_supported_count():
    values = [.25, .75]
    assert spread(lambda _: values, ["x", "y"], "knockout", .93) == values


def test_knockout_temperature_preserved_above_boundary():
    texts = [str(i) for i in range(17)]
    def read(group):
        return evaluation.calibrated([float(text) / 10 for text in group])
    unsharpened = spread(read, texts, "knockout", 1.0)
    official = spread(read, texts, "knockout", .93)
    weights = [p ** (1 / .93) for p in unsharpened]
    assert official == pytest.approx([p / sum(weights) for p in weights], abs=1e-12)
    assert official != pytest.approx(unsharpened, abs=1e-6)


def test_typed_answers_and_stable_ordered_tie():
    noul = evaluation.typed_decision({"type": "noul", "instructions": "Check."}, [.2, .8], 12)
    assert noul == {"type": "noul", "confidence": .8, "input_tokens": 12, "noul": .2}
    choice = evaluation.typed_decision({"type": "choice", "instructions": "Pick.",
        "criteria": {"later": "L", "earlier": "E"}}, [.5, .5], 12)
    assert choice["choice"] == "later"
    score = evaluation.typed_decision({"type": "score", "instructions": "Rate.",
        "criteria": ["zero", "one", "two"]}, [.2, .3, .5], 12)
    assert score["score"] == 1.3
    assert score["probabilities"] == {"0": .2, "1": .3, "2": .5}


def test_valid_uzu_response_passes_independent_calibration():
    value, tokens, candidates = response()
    assert evaluation.validate_uzu_response(value, tokens, candidates)[2] == value["candidate_probabilities"]


@pytest.mark.parametrize("field,replacement,code", [
    ("candidate_token_ids", [33, 32], "uzu_score_identity_invalid"),
    ("prompt_tokens_sha256", "0" * 64, "uzu_score_identity_invalid"),
    ("truncated", True, "uzu_score_identity_invalid"),
    ("cache_hit", True, "uzu_score_identity_invalid"),
    ("candidate_probabilities", [.5, .5], "uzu_calibration_mismatch"),
    ("log_probabilities", [-1.0, -1.0], "uzu_calibration_mismatch"),
    ("logits", [1.0], "uzu_candidate_coverage"),
    ("metrics", {"prefill_seconds": -1}, "uzu_timing_invalid"),
])
def test_uzu_response_rejects_mismatch(field, replacement, code):
    value, tokens, candidates = response()
    value[field] = replacement
    with pytest.raises(evaluation.EvaluationError, match=code):
        evaluation.validate_uzu_response(value, tokens, candidates)


def llama_response(entries):
    return {"truncated": False, "tokens_evaluated": 3,
            "completion_probabilities": [{"top_logprobs": entries}]}


def test_llama_reads_strict_coverage_and_id_or_exact_text():
    value = llama_response([{"id": 32, "token": "A", "logprob": -1.0},
                            {"token": "B", "logprob": -3.0}, {"token": "other", "logprob": -.1}])
    logprobs, probabilities = evaluation.llama_candidates(value, [32, 33], "AB", 3)
    assert logprobs == [-1.0, -3.0]
    assert probabilities == evaluation.calibrated(logprobs)


def test_llama_explicit_candidate_id_overrides_duplicate_rendered_token_text():
    value = llama_response([{"id": 99, "token": "A", "logprob": -.1},
                            {"id": 32, "token": "A", "logprob": -1.0},
                            {"id": 33, "token": "B", "logprob": -3.0},
                            {"token_id": 100, "token": "B", "logprob": -.2}])
    logprobs, probabilities = evaluation.llama_candidates(value, [32, 33], "AB", 3)
    assert logprobs == [-1.0, -3.0]
    assert probabilities == evaluation.calibrated(logprobs)


def test_llama_missing_explicit_candidate_cannot_be_filled_by_other_id_same_text():
    value = llama_response([{"id": 99, "token": "A", "logprob": -.1},
                            {"id": 33, "token": "B", "logprob": -3.0}])
    with pytest.raises(evaluation.EvaluationError, match="llama_candidate_coverage"):
        evaluation.llama_candidates(value, [32, 33], "AB", 3)


def test_llama_text_only_candidates_require_whole_exact_letter_strings():
    value = llama_response([{"token": "", "logprob": -.1}, {"token": "AB", "logprob": -.2},
                            {"token": "A", "logprob": -1.0}, {"token": "B", "logprob": -3.0}])
    logprobs, probabilities = evaluation.llama_candidates(value, [32, 33], "AB", 3)
    assert logprobs == [-1.0, -3.0]
    assert probabilities == evaluation.calibrated(logprobs)


@pytest.mark.parametrize("token", ["", "AB"])
def test_llama_empty_or_multiletter_tokens_cannot_supply_missing_candidates(token):
    value = llama_response([{"token": token, "logprob": -.1}, {"token": "B", "logprob": -3.0}])
    with pytest.raises(evaluation.EvaluationError, match="llama_candidate_coverage"):
        evaluation.llama_candidates(value, [32, 33], "AB", 3)


@pytest.mark.parametrize("entries", [
    [{"token": "A", "logprob": -1}],
    [{"token": "A", "logprob": -1}, {"token": "A", "logprob": -2}, {"token": "B", "logprob": -3}],
    [{"id": 32, "token": "B", "logprob": -1}, {"id": 33, "token": "A", "logprob": -3}],
    [{"token": "A", "logprob": float("nan")}, {"token": "B", "logprob": -3}],
])
def test_llama_refuses_missing_duplicate_nonfinite_or_misbound_candidates(entries):
    with pytest.raises(evaluation.EvaluationError):
        evaluation.llama_candidates(llama_response(entries), [32, 33], "AB", 3)


def test_probability_comparison_uses_total_variation_and_ordered_argmax():
    result = evaluation.probability_difference([.5, .5], [.4, .6])
    assert result["l1"] == pytest.approx(.2)
    assert result["total_variation"] == pytest.approx(.1)
    assert result["argmax_agrees"] is False


def test_gold_metrics_binary_brier_and_score_ev_are_distinct():
    noul = next(c for c in evaluation.synthetic_cases() if c["id"] == "synthetic-noul-true")
    row = {"probabilities": [.75, .25], "decision": evaluation.typed_decision(noul["question"], [.75, .25], 12)}
    quality = evaluation.quality_row(noul, row)
    assert quality["brier"] == .125
    assert quality["correct"] is True
    case = {"question": {"type": "score", "instructions": "Rate", "criteria": ["0", "1", "2"]},
            "expected": 1}
    row = {"probabilities": [.4, .3, .3], "decision": evaluation.typed_decision(case["question"], [.4, .3, .3], 12)}
    quality = evaluation.quality_row(case, row)
    assert quality["correct"] is False
    assert quality["rounded_ev_correct"] is True
    assert quality["ordinal_mae"] == pytest.approx(.1)


def test_ece_perfect_and_unit_confidence_boundary():
    assert evaluation.ece([1.0, 1.0], [True, True]) == 0
    assert evaluation.ece([1.0, 1.0], [True, False]) == .5
    assert evaluation.percentile([1, 2, 3, 4], .95) == pytest.approx(3.85)


@pytest.mark.parametrize("count,bucket", [(1, "under_256"), (255, "under_256"),
    (256, "256_to_1023"), (1023, "256_to_1023"), (1024, "1024_to_4095"),
    (4095, "1024_to_4095"), (4096, "4096_plus"), (8191, "4096_plus")])
def test_timing_length_bucket_boundaries(count, bucket):
    assert evaluation.token_bucket(count) == bucket


def test_timing_summary_includes_native_prefill_readout_and_empty_buckets():
    rows = [{"cache_hit": False, "tokenization_seconds": .01, "score_request_seconds": .1,
        "wall_seconds": .11, "engine_metrics": {"prefill_seconds": .07, "readout_seconds": .03}},
        {"cache_hit": False, "tokenization_seconds": .02, "score_request_seconds": .2,
         "wall_seconds": .22, "engine_metrics": {"prefill_seconds": .14, "readout_seconds": .06}}]
    result = evaluation.timing_summary(rows)
    assert result["engine_metrics_seconds"]["prefill_seconds"]["p50"] == pytest.approx(.105)
    assert result["engine_metrics_seconds"]["readout_seconds"]["p95"] == pytest.approx(.0585)
    assert result["engine_metrics_seconds"]["readout_seconds"]["samples"] == 2
    empty = evaluation.timing_summary([])
    assert empty["samples"] == 0
    assert empty["wall_seconds"] == {"p50": None, "p95": None}
    assert empty["engine_metrics_seconds"] == {}


def test_paired_speed_ratios_require_matching_tokens_and_positive_timings():
    llama = {"input_tokens": 250, "wall_seconds": .4, "score_request_seconds": .3}
    uzu = {"input_tokens": 250, "wall_seconds": .2, "score_request_seconds": .1}
    assert evaluation.paired_speed_summary([(llama, uzu)]) == {
        "pairs": 1, "median_llama_over_uzu_wall_ratio": 2,
        "median_llama_over_uzu_score_request_ratio": pytest.approx(3)}
    assert evaluation.paired_speed_summary([])["pairs"] == 0
    with pytest.raises(evaluation.EvaluationError, match="timing_pair_token_mismatch"):
        evaluation.paired_speed_summary([(llama, {**uzu, "input_tokens": 251})])
    with pytest.raises(evaluation.EvaluationError, match="timing_pair_invalid"):
        evaluation.paired_speed_summary([(llama, {**uzu, "wall_seconds": 0})])


def test_bootstrap_deterministic_paired_and_zero_for_identical_results():
    rows = [{"correct": True, "confidence": .8, "brier": .08, "ordinal_mae": None,
             "rounded_ev_correct": None}, {"correct": False, "confidence": .6, "brier": .72,
             "ordinal_mae": None, "rounded_ev_correct": None}]
    assert evaluation.bootstrap_differences(rows, rows, draws=40) == {
        "argmax_accuracy": [0, 0], "brier": [0, 0], "ece_10_bins": [0, 0]}


def test_all_synthetic_gold_labels_follow_the_real_typed_mapping():
    cases = evaluation.synthetic_cases()
    assert len(cases) == 12
    assert max(len(evaluation.canonical_option_keys(c["question"])) for c in cases) == 16
    for case in cases:
        evaluation.validate_case(case)


def test_serialization_preserves_choice_option_order():
    case = next(c for c in evaluation.synthetic_cases() if c["id"] == "synthetic-choice-16-reversed")
    restored = json.loads(evaluation.encoded(case))
    assert evaluation.canonical_option_keys(restored["question"]) == list(reversed([str(i) for i in range(16)]))
    assert evaluation.rendered(restored) == evaluation.rendered(case)


def test_private_storage_refuses_public_path_and_overwrite(tmp_path):
    with pytest.raises(evaluation.EvaluationError, match="private_location_invalid"):
        evaluation.secure_directory(ROOT / "docs/forbidden-private-test")
    path = tmp_path / "result.json"
    evaluation.private_json(path, {"count": 1})
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        evaluation.private_json(path, {"count": 2})
    path.chmod(0o644)
    with pytest.raises(evaluation.EvaluationError, match="private_file_invalid"):
        evaluation.read_private(path)


@pytest.fixture
def model_binding(tmp_path, monkeypatch):
    calibration = {"temperature": 1.22, "knockout_temperature": .93}
    model = tmp_path / "model"
    model.mkdir()
    files = {"config.json": b"{}", "tokenizer.json": b"synthetic-tokenizer",
             "model.safetensors": b"converted-synthetic-weights",
             "jevk5_config.json": evaluation.encoded(calibration)}
    for name, data in files.items():
        (model / name).write_bytes(data)
    gguf = tmp_path / "reference.gguf"
    gguf.write_bytes(b"pinned-synthetic-gguf")
    converter = tmp_path / "converter.py"
    converter.write_bytes(b"synthetic-converter")
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"synthetic-locked-dependencies")
    pinned = {"source": {"repository": "alibiserikbay/JevK5", "revision": "synthetic-revision",
        "files": [{"name": name, "bytes": len(data), "sha256": evaluation.sha(data)} for name, data in files.items()]},
        "llama_reference": {"bytes": gguf.stat().st_size, "sha256": evaluation.file_hash(gguf)},
        "calibration": calibration}
    monkeypatch.setattr(evaluation.model_acquirer, "provenance", lambda: json.loads(json.dumps(pinned)))
    manifest = {"schema_version": 1, **json.loads(json.dumps(pinned)), "conversion": {
        "status": "complete", "repository": "https://github.com/trymirai/lalamo",
        "revision": "831accb8252a6b6606c378048caeb44178f1a050", "license": "MIT",
        "dtype": "bfloat16", "quantization": "none", "script_sha256": evaluation.file_hash(converter),
        "uv_lock_sha256": evaluation.file_hash(lock), "source_weights_sha256": evaluation.sha(files["model.safetensors"]),
        "metadata_adaptation": {"model_type": "qwen3_5_text -> qwen3_5", "adapted_config_sha256": "a" * 64},
        "files": [{"name": name, "bytes": len(data), "sha256": evaluation.sha(data)} for name, data in files.items()]}}
    manifest_path = tmp_path / "provenance.json"
    def persist():
        manifest_path.write_bytes(evaluation.encoded(manifest))
    persist()
    return SimpleNamespace(model=model, gguf=gguf, converter=converter, lock=lock, manifest=manifest,
        manifest_path=manifest_path, persist=persist,
        validate=lambda: evaluation.validate_model_artifacts(model, gguf, manifest_path, converter, lock))


def test_model_binding_checks_pinned_manifest_and_every_export(model_binding):
    binding = model_binding.validate()
    assert binding["manifest_sha256"] == evaluation.file_hash(model_binding.manifest_path)
    assert set(binding["exported_files"]) == {"config.json", "tokenizer.json", "model.safetensors", "jevk5_config.json"}


@pytest.mark.parametrize("field,value", [("source", {"repository": "unrelated-model"}),
    ("calibration", {"temperature": 1.0, "knockout_temperature": .93}),
    ("llama_reference", {"sha256": "0" * 64})])
def test_model_binding_refuses_unrelated_or_recalibrated_manifest(model_binding, field, value):
    model_binding.manifest[field] = value
    model_binding.persist()
    with pytest.raises(evaluation.EvaluationError, match="model_manifest_identity_mismatch"):
        model_binding.validate()


@pytest.mark.parametrize("field,value", [("status", "pending"), ("revision", "unrelated-commit"),
    ("quantization", "different"), ("source_weights_sha256", "0" * 64)])
def test_model_binding_requires_completed_pinned_conversion(model_binding, field, value):
    model_binding.manifest["conversion"][field] = value
    model_binding.persist()
    with pytest.raises(evaluation.EvaluationError, match="model_conversion_identity_mismatch"):
        model_binding.validate()


def test_model_binding_refuses_modified_converter_or_lock(model_binding):
    model_binding.converter.write_bytes(b"modified-converter")
    with pytest.raises(evaluation.EvaluationError, match="model_converter_identity_mismatch"):
        model_binding.validate()


def test_model_binding_refuses_wrong_gguf(model_binding):
    model_binding.gguf.write_bytes(b"different-gguf")
    with pytest.raises(evaluation.EvaluationError, match="model_llama_artifact_mismatch"):
        model_binding.validate()


def test_model_binding_refuses_tampered_export_even_with_same_size(model_binding):
    weights = model_binding.model / "model.safetensors"
    weights.write_bytes(b"x" * weights.stat().st_size)
    with pytest.raises(evaluation.EvaluationError, match="model_export_artifact_mismatch"):
        model_binding.validate()


def test_model_binding_refuses_undeclared_export_and_unsafe_path(model_binding):
    extra = model_binding.model / "extra.safetensors"
    extra.write_bytes(b"undeclared-weights")
    with pytest.raises(evaluation.EvaluationError, match="model_export_manifest_coverage"):
        model_binding.validate()
    extra.unlink()
    model_binding.manifest["conversion"]["files"][0]["name"] = "../outside"
    model_binding.persist()
    with pytest.raises(evaluation.EvaluationError, match="model_export_manifest_invalid"):
        model_binding.validate()


def test_machine_information_captures_chip_and_physical_ram(monkeypatch):
    monkeypatch.setattr(evaluation.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout="Apple M5 Max\n137438953472\nMac17,7\n"))
    machine = evaluation.machine_information()
    assert machine["chip_model"] == "Apple M5 Max"
    assert machine["physical_ram_bytes"] == 128 * 1024**3
    assert machine["hardware_model"] == "Mac17,7"


def native_fixture(tmp_path):
    binary = tmp_path / "scorer"
    binary.write_bytes(b"synthetic-scoring-binary")
    patch = tmp_path / "scoring.patch"
    patch.write_bytes(b"synthetic-scoring-extension")
    lock = tmp_path / "scoring-Cargo.lock"
    lock.write_bytes(b"separate-patched-scoring-lock")
    manifest = tmp_path / "scoring-Cargo.toml"
    manifest.write_bytes(b"separate-scoring-manifest")
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(evaluation.encoded({"binary_sha256": evaluation.file_hash(binary),
        "scoring_patch_sha256": evaluation.file_hash(patch), "cargo_lock_sha256": evaluation.file_hash(lock),
        "cargo_manifest": "experiments/uzu-rust/scoring/Cargo.toml",
        "cargo_lock": "experiments/uzu-rust/scoring/Cargo.lock",
        "cargo_manifest_sha256": evaluation.file_hash(manifest),
        "upstream_revision": "8d01581474760bd1fc7f4a547ff1d02f7dada437"}))
    extension = tmp_path / "extension.json"
    extension.write_bytes(evaluation.encoded({"scoring_patch_sha256": evaluation.file_hash(patch),
        "upstream_revision": "8d01581474760bd1fc7f4a547ff1d02f7dada437", "readout_dtype": "BF16"}))
    return binary, receipt, extension, patch, lock, manifest


def test_scoring_build_binding_uses_its_separate_manifest_and_lock(tmp_path):
    fixture = native_fixture(tmp_path)
    binding = evaluation.native_build_identity(*fixture)
    assert evaluation.SCORING_CARGO_LOCK.relative_to(ROOT).as_posix() == "experiments/uzu-rust/scoring/Cargo.lock"
    assert binding["scoring_cargo_lock"] == {"path": "experiments/uzu-rust/scoring/Cargo.lock",
                                             "sha256": evaluation.file_hash(fixture[4])}
    assert binding["scoring_cargo_manifest"] == {"path": "experiments/uzu-rust/scoring/Cargo.toml",
                                                 "sha256": evaluation.file_hash(fixture[5])}
    assert binding["rust_main_source_sha256"] == evaluation.file_hash(ROOT / "experiments/uzu-rust/src/main.rs")


def test_scoring_build_binding_refuses_generation_lock_or_changed_manifest(tmp_path):
    fixture = list(native_fixture(tmp_path))
    original_lock = tmp_path / "generation-Cargo.lock"
    original_lock.write_bytes(b"unpatched-production-generation-lock")
    with pytest.raises(evaluation.EvaluationError, match="native_build_identity_mismatch"):
        evaluation.native_build_identity(*fixture[:4], original_lock, fixture[5])
    fixture[5].write_bytes(b"changed-scoring-manifest")
    with pytest.raises(evaluation.EvaluationError, match="native_build_identity_mismatch"):
        evaluation.native_build_identity(*fixture)


def test_scoring_build_binding_requires_manifest_hash(tmp_path):
    fixture = native_fixture(tmp_path)
    receipt = json.loads(fixture[1].read_bytes())
    del receipt["cargo_manifest_sha256"]
    fixture[1].write_bytes(evaluation.encoded(receipt))
    with pytest.raises(evaluation.EvaluationError, match="native_build_identity_mismatch"):
        evaluation.native_build_identity(*fixture)
