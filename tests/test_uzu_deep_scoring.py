"""Focused identity and isolation checks for the deep scoring experiment."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/uzu-rust"))
import benchmark_uzu_scoring_deep as deep
import build_deep_scoring as builder


def write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def receipt_tree(tmp_path, monkeypatch):
    root = tmp_path / "root"
    experiment = root / "experiments/uzu-rust"
    scoring = experiment / "scoring"
    upstream = root / "runtime/uzu-rust/upstream"
    monkeypatch.setattr(deep, "ROOT", root)
    monkeypatch.setattr(deep.base, "ROOT", root)
    monkeypatch.setattr(deep.base, "EXPERIMENT", experiment)
    monkeypatch.setattr(deep.base, "SCORING_CARGO_MANIFEST", scoring / "Cargo.toml")
    monkeypatch.setattr(deep.base, "SCORING_CARGO_LOCK", scoring / "Cargo.lock")
    for name in ("topology", "state-init", "submission", "last-layer", "metal"):
        prefix = "uzu-scoring-" if name in ("topology", "state-init") else "uzu-deep-"
        write(experiment / (prefix + name + ".patch"), name.encode())
    for name in ("main.rs", "scoring.rs"):
        write(experiment / "src" / name)
    write(experiment / "probes/score_digest.rs")
    write(experiment / "build_deep_scoring.py")
    write(experiment / "build_scoring.py")
    write(scoring / "Cargo.toml", (
        b'[package]\nname = "synthetic"\n'
        b'[[bin]]\nname = "worker"\npath = "../src/main.rs"\n'
        b'uzu-engine = { path = "../../../runtime/uzu-rust/upstream/crates/uzu-engine" }\n'
        b'hanashi = { path = "../../../runtime/uzu-rust/upstream/crates/legacy/hanashi" }\n'
        b'shoji = { path = "../../../runtime/uzu-rust/upstream/crates/legacy/shoji" }\n'))
    write(scoring / "Cargo.lock", b"locked")
    for name in ("crates/uzu-engine", "crates/legacy/hanashi", "crates/legacy/shoji"):
        (upstream / name).mkdir(parents=True)
    binary = write(root / "candidate", b"candidate binary")
    project = root / "receipt/project"
    manifest = write(project / "Cargo.toml")
    lock = write(project / "Cargo.lock", b"locked")
    chain_names = ["topology", "state-init", "submission"]
    chain = [{"path": "experiments/uzu-rust/uzu-next-token-scoring.patch", "sha256": ""}]
    write(experiment / "uzu-next-token-scoring.patch")
    chain[0]["sha256"] = deep.base.file_hash(experiment / "uzu-next-token-scoring.patch")
    for name in chain_names:
        prefix = "uzu-scoring-" if name in ("topology", "state-init") else "uzu-deep-"
        path = experiment / (prefix + name + ".patch")
        relative = path.relative_to(root).as_posix()
        chain.append({"path": relative, "sha256": deep.base.file_hash(path)})
    sources = {f"experiments/uzu-rust/src/{name}": deep.base.file_hash(experiment / "src" / name)
               for name in ("main.rs", "scoring.rs")}
    probes = {"experiments/uzu-rust/probes/score_digest.rs": deep.base.file_hash(experiment / "probes/score_digest.rs")}
    receipt = {"schema_version": 2, "variant": "submission", "upstream_revision": deep.REVISION,
        "binary_sha256": deep.base.file_hash(binary), "features": ["probability-scoring", "uzu-engine/deferred-state-init"],
        "patch_chain": chain, "worker_sources": sources, "probe_sources": probes,
        "build_recipe_sha256": deep.base.file_hash(experiment / "build_deep_scoring.py"),
        "base_build_recipe_sha256": deep.base.file_hash(experiment / "build_scoring.py"),
        "original_scoring_manifest_sha256": deep.base.file_hash(scoring / "Cargo.toml"),
        "cargo_manifest_sha256": deep.base.file_hash(manifest),
        "cargo_lock_sha256": deep.base.file_hash(lock)}
    receipt_path = root / "receipt/BUILD_RECEIPT.json"
    receipt_path.write_text(json.dumps(receipt))
    return root, receipt_path, binary, scoring, upstream, manifest, lock


def test_candidate_receipt_accepts_ordered_chain_and_features(receipt_tree):
    _, receipt, binary, *_ = receipt_tree
    value, _ = deep.candidate_identity(binary, receipt)
    assert value["variant"] == "submission"
    assert [item["path"] for item in value["patch_chain"]][1:] == [
        "experiments/uzu-rust/uzu-scoring-topology.patch",
        "experiments/uzu-rust/uzu-scoring-state-init.patch",
        "experiments/uzu-rust/uzu-deep-submission.patch"]


@pytest.mark.parametrize("change, message", [
    (lambda r: r.update(schema_version=1), "candidate_receipt_invalid"),
    (lambda r: r.update(features=["probability-scoring", "wrong-feature"]), "candidate_features_invalid"),
    (lambda r: r["patch_chain"].reverse(), "candidate_patch_chain_invalid"),
])
def test_candidate_receipt_rejects_identity_changes(receipt_tree, change, message):
    _, receipt, binary, *_ = receipt_tree
    value = json.loads(receipt.read_text())
    change(value)
    receipt.write_text(json.dumps(value))
    with pytest.raises(deep.base.EvaluationError, match=message):
        deep.candidate_identity(binary, receipt)


def test_prepare_project_preserves_manifest_and_lock(receipt_tree, monkeypatch, tmp_path):
    _, _, _, scoring, upstream, original_manifest, original_lock = receipt_tree
    monkeypatch.setattr(builder.base, "SCORING", scoring)
    monkeypatch.setattr(builder, "EXPERIMENT", scoring.parent)
    before_manifest, before_lock = original_manifest.read_bytes(), original_lock.read_bytes()
    before_scoring_manifest = (scoring / "Cargo.toml").read_bytes()
    before_scoring_lock = (scoring / "Cargo.lock").read_bytes()
    destination = tmp_path / "project"
    builder.prepare_project(destination, upstream)
    generated = (destination / "Cargo.toml").read_text()
    assert original_manifest.read_bytes() == before_manifest
    assert original_lock.read_bytes() == before_lock
    assert (scoring / "Cargo.toml").read_bytes() == before_scoring_manifest
    assert (scoring / "Cargo.lock").read_bytes() == before_scoring_lock
    assert (destination / "Cargo.lock").read_bytes() == before_lock
    assert str(builder.EXPERIMENT / "src/main.rs") in generated
    assert str(upstream / "crates/uzu-engine") in generated


@pytest.mark.parametrize("relative, message", [
    ("candidate", "candidate_receipt_invalid"),
    ("experiments/uzu-rust/src/main.rs", "candidate_source_hash_invalid"),
    ("experiments/uzu-rust/probes/score_digest.rs", "probe_source_hash_invalid"),
    ("experiments/uzu-rust/build_deep_scoring.py", "build_recipe_mismatch"),
    ("receipt/project/Cargo.toml", "build_recipe_mismatch"),
    ("receipt/project/Cargo.lock", "build_recipe_mismatch"),
])
def test_candidate_identity_rejects_bound_file_mutation(receipt_tree, relative, message):
    root, receipt, binary, *_ = receipt_tree
    target = root / relative
    target.write_bytes(target.read_bytes() + b"mutation")
    with pytest.raises(deep.base.EvaluationError, match=message):
        deep.candidate_identity(binary, receipt)


def test_candidate_identity_rejects_extra_source_and_patch_records(receipt_tree):
    _, receipt, binary, *_ = receipt_tree
    value = json.loads(receipt.read_text())
    value["worker_sources"]["experiments/uzu-rust/src/extra.rs"] = "0" * 64
    receipt.write_text(json.dumps(value))
    with pytest.raises(deep.base.EvaluationError, match="candidate_sources_invalid"):
        deep.candidate_identity(binary, receipt)

    value = json.loads(receipt.read_text())
    value["worker_sources"].pop("experiments/uzu-rust/src/extra.rs")
    value["patch_chain"].append({"path": value["patch_chain"][-1]["path"], "sha256": value["patch_chain"][-1]["sha256"]})
    receipt.write_text(json.dumps(value))
    with pytest.raises(deep.base.EvaluationError, match="candidate_patch_chain_invalid"):
        deep.candidate_identity(binary, receipt)


@pytest.mark.parametrize("field", ["input_token_ids", "candidate_token_ids", "logits", "log_probabilities", "probabilities", "decision"])
def test_equal_rows_rejects_identity_changes(field):
    left = {key: [1] if "ids" in key else [0.5] if key in {"logits", "log_probabilities", "probabilities"} else "a"
            for key in deep.IDENTITY}
    right = dict(left)
    right[field] = ([2] if "ids" in field else [0.6] if field in {"logits", "log_probabilities", "probabilities"} else "b")
    with pytest.raises(deep.base.EvaluationError, match="output_equality_failed"):
        deep.equal_rows(left, right)
