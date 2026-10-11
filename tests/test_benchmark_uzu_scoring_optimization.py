"""Optimization receipt, equality, privacy and completeness rejection tests."""
import copy
import json
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_uzu_scoring_optimization as opt


def response(tokens, candidates=(32, 33)):
    logits = [2.0, -1.0]
    partition = math.log(sum(math.exp(v) for v in logits))
    return {"input_tokens": len(tokens), "prompt_tokens_sha256": opt.base.token_digest(tokens),
        "candidate_token_ids": list(candidates), "logits": logits,
        "log_probabilities": [v - partition for v in logits],
        "candidate_probabilities": opt.base.calibrated(logits), "temperature": 1.22,
        "cache_hit": False, "truncated": False, "metrics": {k: .01 for k in opt.METRICS}}


def make_row(case, condition):
    tokens = [10, 11]
    value = response(tokens)
    return {"case_id": case["id"], "condition": condition, "input_tokens": len(tokens),
        "input_token_ids": tokens, "candidate_token_ids": value["candidate_token_ids"],
        "logits": value["logits"], "log_probabilities": value["log_probabilities"],
        "probabilities": value["candidate_probabilities"], "decision": opt.base.typed_decision(
            case["question"], value["candidate_probabilities"], len(tokens)),
        "cache_hit": False, "truncated": False, "engine_metrics": value["metrics"],
        "tokenization_seconds": .001, "score_request_seconds": .02, "wall_seconds": .021}


@pytest.fixture
def sealed(tmp_path):
    case = opt.base.synthetic_cases()[0]
    config = {"rounds": 1, "case_ids": [case["id"]]}
    digest = opt.base.sha(opt.base.encoded(config))
    opt.base.private_json(tmp_path / "dataset.json", {"cases": [case]})
    boundaries = []
    for n in opt.LENGTHS:
        for kind in ("fresh", "repeat", "changed", "return"):
            tokens = [10] * n
            if kind == "changed":
                tokens[-1] = 11
            boundaries.append({"length": n, "kind": kind, "tokens": tokens, "response": response(tokens)})
    for engine in ("baseline", "candidate"):
        opt.base.private_json(tmp_path / f"round-1-{engine}.json", {"round": 1, "engine": engine,
            "complete": True, "configuration_sha256": digest, "ownership_settled": True,
            "warmup_count": 3, "rows": [make_row(case, c) for c in ("fresh", "identical_repeat")],
            "boundaries": boundaries})
    pre = {"configuration_sha256": digest, "dataset_sha256": opt.base.file_hash(tmp_path / "dataset.json")}
    opt.base.private_json(tmp_path / "pre-run-seal.json", pre)
    opt.base.private_json(tmp_path / "post-run-seal.json", {**pre, "ownership_settled": True,
        "blocks": {p.name: opt.base.file_hash(p) for p in tmp_path.glob("round-*.json")}})
    return tmp_path, config, [case]


def edit_block(directory, change, reseal=True):
    path = directory / "round-1-candidate.json"
    block = opt.base.read_private(path)
    change(block)
    path.write_bytes(opt.base.encoded(block))
    if reseal:
        seal_path = directory / "post-run-seal.json"
        seal = opt.base.read_private(seal_path)
        seal["blocks"][path.name] = opt.base.file_hash(path)
        seal_path.write_bytes(opt.base.encoded(seal))


def test_complete_blocks_and_fresh_repeat_equality(sealed):
    assert len(opt.load_blocks(*sealed)) == 2


def test_missing_block_fails(sealed):
    (sealed[0] / "round-1-candidate.json").unlink()
    with pytest.raises(opt.base.EvaluationError, match="blocks_incomplete"):
        opt.load_blocks(*sealed)


def test_modified_block_without_seal_fails(sealed):
    edit_block(sealed[0], lambda b: b.update(warmup_count=4), reseal=False)
    with pytest.raises(opt.base.EvaluationError, match="block_seal_mismatch"):
        opt.load_blocks(*sealed)


def test_changed_tokens_with_valid_seal_fail_equality(sealed):
    def change(block):
        block["rows"][0]["input_token_ids"] = [11, 10]
    edit_block(sealed[0], change)
    with pytest.raises(opt.base.EvaluationError, match="output_equality_failed"):
        opt.load_blocks(*sealed)


def test_changed_normalizer_with_valid_seal_fails_equality(sealed):
    def change(block):
        block["rows"][0]["log_probabilities"] = [v - .1 for v in block["rows"][0]["log_probabilities"]]
    edit_block(sealed[0], change)
    with pytest.raises(opt.base.EvaluationError, match="output_equality_failed"):
        opt.load_blocks(*sealed)


@pytest.mark.parametrize("mutation", ["missing", "nan", "negative"])
def test_invalid_native_metrics_fail(mutation):
    value = response([10])
    if mutation == "missing":
        del value["metrics"]["state_creation_seconds"]
    else:
        value["metrics"]["state_creation_seconds"] = float("nan") if mutation == "nan" else -1
    with pytest.raises(opt.base.EvaluationError):
        opt.validate_response(value, [10], [32, 33])


def test_private_file_permissions_required(sealed):
    path = sealed[0] / "round-1-candidate.json"
    path.chmod(0o644)
    with pytest.raises(opt.base.EvaluationError, match="private_file_invalid"):
        opt.load_blocks(*sealed)


def test_preseal_configuration_tamper_fails(sealed):
    sealed[1]["extra"] = True
    with pytest.raises(opt.base.EvaluationError, match="seal_mismatch"):
        opt.load_blocks(*sealed)


def test_changed_boundary_must_actually_change_input(sealed):
    def change(block):
        block["boundaries"][2] = {**copy.deepcopy(block["boundaries"][0]), "kind": "changed"}
    edit_block(sealed[0], change)
    with pytest.raises(opt.base.EvaluationError, match="boundary_change_missing"):
        opt.load_blocks(*sealed)


def test_model_identity_rejects_wrong_source(tmp_path, monkeypatch):
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    (experiment / "MODEL_PROVENANCE.json").write_text(json.dumps({"schema_version": 1, "source": {}}))
    monkeypatch.setattr(opt.base, "EXPERIMENT", experiment)
    with pytest.raises(opt.base.EvaluationError, match="model_identity_invalid"):
        opt.model_identity(tmp_path)


def test_model_file_hash_must_match(tmp_path, monkeypatch):
    pinned = opt.base.model_acquirer.provenance()
    weights = next(r["sha256"] for r in pinned["source"]["files"] if r["name"] == "model.safetensors")
    manifest = {"schema_version": 1, "source": pinned["source"], "calibration": pinned["calibration"],
        "conversion": {"status": "complete", "dtype": "bfloat16", "quantization": "none",
            "revision": "831accb8252a6b6606c378048caeb44178f1a050", "source_weights_sha256": weights,
            "files": [{"name": "model.safetensors", "bytes": 1, "sha256": "0" * 64}]}}
    (tmp_path / "MODEL_PROVENANCE.json").write_text(json.dumps(manifest))
    (tmp_path / "model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(opt.base, "EXPERIMENT", tmp_path)
    with pytest.raises(opt.base.EvaluationError, match="model_file_mismatch"):
        opt.model_identity(tmp_path)


@pytest.fixture
def candidate_receipt(tmp_path, monkeypatch):
    value = {"schema_version": 1, "upstream_revision": opt.REVISION, "variant": "combined",
        "binary_sha256": "h", "features": ["probability-scoring", "uzu-engine/deferred-state-init"],
        "patch_chain": [{"path": "experiments/uzu-rust/" + n, "sha256": "h"} for n in
            ("uzu-next-token-scoring.patch", "uzu-scoring-topology.patch", "uzu-scoring-state-init.patch")],
        "worker_sources": {"experiments/uzu-rust/src/" + n: "h" for n in ("main.rs", "scoring.rs")},
        "probe_sources": {"experiments/uzu-rust/probes/score_digest.rs": "h"},
        **{k: "h" for k in ("build_recipe_sha256", "base_build_recipe_sha256",
            "original_scoring_manifest_sha256", "cargo_manifest_sha256", "cargo_lock_sha256")}}
    receipt = tmp_path / "BUILD_RECEIPT.json"
    receipt.write_text(json.dumps(value))
    monkeypatch.setattr(opt, "ordinary_file", lambda p: Path(p).absolute())
    monkeypatch.setattr(opt, "repository_file", lambda p: opt.ROOT / p)
    monkeypatch.setattr(opt.base, "file_hash", lambda p: "h")
    return tmp_path / "worker", receipt, value


def test_valid_candidate_receipt(candidate_receipt):
    binary, path, value = candidate_receipt
    assert opt.candidate_identity(binary, path)[0] == value


@pytest.mark.parametrize("field,value", [("binary_sha256", "wrong"), ("features", ["probability-scoring"]),
    ("variant", "baseline"), ("build_recipe_sha256", "wrong"), ("cargo_lock_sha256", "wrong")])
def test_candidate_receipt_rejects_identity_drift(candidate_receipt, field, value):
    binary, path, receipt = candidate_receipt
    receipt[field] = value
    path.write_text(json.dumps(receipt))
    with pytest.raises(opt.base.EvaluationError):
        opt.candidate_identity(binary, path)


def test_candidate_patch_order_is_semantic(candidate_receipt):
    binary, path, receipt = candidate_receipt
    receipt["patch_chain"][1:] = reversed(receipt["patch_chain"][1:])
    path.write_text(json.dumps(receipt))
    with pytest.raises(opt.base.EvaluationError, match="candidate_patch_chain_invalid"):
        opt.candidate_identity(binary, path)


def test_digest_requires_exact_input_binding():
    value = {"input_tokens": 1, "prompt_tokens_sha256": opt.base.token_digest([10]), "cache_hit": False,
        "truncated": False, "vocab_size": 100, "vocab_logits_sha256": "a" * 64}
    opt.validate_digest(value, [10], 100)
    with pytest.raises(opt.base.EvaluationError, match="digest_response_invalid"):
        opt.validate_digest(value, [11], 100)


def test_artifact_postseal_drift_rejected(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"before")
    config = {"files": {str(path): opt.base.file_hash(path)}}
    opt.verify_files(config)
    path.write_bytes(b"after")
    with pytest.raises(opt.base.EvaluationError, match="artifact_changed"):
        opt.verify_files(config)


@pytest.mark.parametrize("alter_hash", [False, True])
def test_complete_digest_blocks_compare_every_vocab_hash(sealed, alter_hash):
    directory, config, _ = sealed
    config["digest_binaries"] = {"baseline": "baseline", "candidate": "candidate"}
    reference = opt.base.read_private(directory / "round-1-baseline.json")
    reference["readiness"] = {"vocab_size": 100}
    requests = [r["input_token_ids"] for r in reference["rows"]] + [r["tokens"] for r in reference["boundaries"]]
    for engine in ("baseline", "candidate"):
        outputs = [{"input_tokens": len(tokens), "prompt_tokens_sha256": opt.base.token_digest(tokens),
            "cache_hit": False, "truncated": False, "vocab_size": 100, "vocab_logits_sha256": "a" * 64}
            for tokens in requests]
        if alter_hash and engine == "candidate":
            outputs[-2]["vocab_logits_sha256"] = "b" * 64
        opt.base.private_json(directory / f"digest-{engine}.json", {"complete": True,
            "ownership_settled": True, "configuration_sha256": opt.base.sha(opt.base.encoded(config)),
            "readiness": {"vocab_size": 100}, "outputs": outputs})
    post = opt.base.read_private(directory / "post-run-seal.json")
    post["digests"] = {p.name: opt.base.file_hash(p) for p in directory.glob("digest-*.json")}
    (directory / "post-run-seal.json").write_bytes(opt.base.encoded(post))
    if alter_hash:
        with pytest.raises(opt.base.EvaluationError, match="vocabulary_equality_failed"):
            opt.verify_digests(directory, config, reference)
    else:
        assert opt.verify_digests(directory, config, reference)


def test_exported_configuration_omits_host_paths_and_case_ids():
    config = {"schema_version": 1, "smoke": False, "rounds": 2,
        "case_ids": ["private-case-identity"], "warmups": 3, "boundary_lengths": list(opt.LENGTHS),
        "temperature": 1.22, "context_tokens": 8192,
        "files": {str(opt.ROOT / "experiments/uzu-rust/src/scoring.rs"): "a" * 64,
                  "/Users/private-person/outside/binary": "b" * 64},
        "candidate_receipt": {"variant": "combined", "upstream_revision": opt.REVISION,
            "features": ["probability-scoring", "uzu-engine/deferred-state-init"],
            "unexpected_private_path": "/Users/private-person/secret"},
        "model": str(opt.ROOT / "models/uzu-jevk5/bf16"),
        "candidate_binary": "/Users/private-person/outside/binary",
        "digest_binaries": {"baseline": "/Users/private-person/probe"},
        "machine": {"unexpected_host_field": "/Users/private-person"}}
    original = copy.deepcopy(config)
    exported = opt.exported_configuration(config)
    serialized = json.dumps(exported)
    assert "/Users/" not in serialized
    assert "private-person" not in serialized
    assert "private-case-identity" not in serialized
    assert str(opt.ROOT) not in serialized
    assert exported["case_count"] == 1
    assert exported["ordered_case_ids_sha256"] == opt.base.sha(opt.base.encoded(config["case_ids"]))
    assert exported["artifacts"][0]["path"] == "experiments/uzu-rust/src/scoring.rs"
    assert exported["artifacts"][1] == {"sha256": "b" * 64, "external_artifact": True}
    assert config == original
