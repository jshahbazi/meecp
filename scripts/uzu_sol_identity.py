"""Identity verifier for additive Sol scoring candidates. Historical validators stay immutable."""
from __future__ import annotations
import json
from pathlib import Path
import benchmark_uzu_scoring_deep as deep
base = deep.base
ROOT, REVISION, require = deep.ROOT, deep.REVISION, deep.require
ordinary_file, repository_file = deep.ordinary_file, deep.repository_file


def candidate_identity(binary, receipt_path, *, allow_baseline=False):
    binary, receipt_path = ordinary_file(binary), ordinary_file(receipt_path)
    value = json.loads(receipt_path.read_bytes())
    require(value.get("schema_version") == 3 and value.get("upstream_revision") == REVISION
            and value.get("binary_sha256") == base.file_hash(binary), "candidate_receipt_invalid")
    variants = {"rope": ["rope"], "gdn": ["gdn"], "attention": ["attention"],
        "rope-gdn": ["rope", "gdn"], "rope-attention": ["rope", "attention"],
        "gdn-attention": ["gdn", "attention"], "combined": ["rope", "gdn", "attention"]}
    variant = value.get("variant")
    require(variant in variants, "candidate_variant_invalid")
    feature_map = {"rope": "uzu-engine/sol-rope-cache", "gdn": "uzu-engine/compact-delta-conv",
        "attention": "uzu-engine/sol-attention-kv-only"}
    expected_features = ["probability-scoring", "uzu-engine/deferred-state-init",
        "uzu-engine/scoring-last-layer-pruning", "uzu-engine/direct-delta-conv"]
    expected_features += [feature_map[n] for n in variants[variant]]
    require(value.get("features") == expected_features and value.get("cache_policy") == "fresh-state", "candidate_features_invalid")
    chain = value.get("patch_chain")
    expected_names = ["experiments/uzu-rust/uzu-next-token-scoring.patch",
        "experiments/uzu-rust/uzu-scoring-topology.patch", "experiments/uzu-rust/uzu-scoring-state-init.patch",
        "experiments/uzu-rust/uzu-deep-submission.patch", "experiments/uzu-rust/uzu-deep-last-layer.patch",
        "experiments/uzu-rust/uzu-deep-metal.patch"]
    expected_names += [f"experiments/uzu-rust/patches/uzu-sol-{n}.patch" for n in variants[variant]]
    require(type(chain) is list and [r.get("path") for r in chain] == expected_names,
            "candidate_patch_chain_invalid")
    paths = []
    for record in chain:
        require(type(record) is dict and set(record) == {"path", "sha256"}, "candidate_patch_chain_invalid")
        path = repository_file(record["path"])
        require(base.file_hash(path) == record["sha256"], "candidate_patch_hash_invalid")
        paths.append(path)
    require(paths[0] == ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch"
            and len(set(paths)) == len(paths), "candidate_patch_chain_invalid")
    sources = value.get("worker_sources")
    require(type(sources) is dict and set(sources) == {"experiments/uzu-rust/src/main.rs",
            "experiments/uzu-rust/src/scoring.rs"}, "candidate_sources_invalid")
    for name, digest in sources.items():
        path = repository_file(name)
        require(base.file_hash(path) == digest, "candidate_source_hash_invalid")
        paths.append(path)
    probes = value.get("probe_sources")
    require(type(probes) is dict and set(probes) == {"experiments/uzu-rust/probes/score_digest.rs"},
            "probe_sources_invalid")
    for name, digest in probes.items():
        path = repository_file(name)
        require(base.file_hash(path) == digest, "probe_source_hash_invalid")
        paths.append(path)
    for key, path in {
        "build_recipe_sha256": ROOT / "experiments/uzu-rust/build_sol_scoring.py",
        "deep_build_recipe_sha256": ROOT / "experiments/uzu-rust/build_deep_scoring.py",
        "base_build_recipe_sha256": ROOT / "experiments/uzu-rust/build_scoring.py",
        "original_scoring_manifest_sha256": base.SCORING_CARGO_MANIFEST,
        "cargo_manifest_sha256": receipt_path.parent / "project/Cargo.toml",
        "cargo_lock_sha256": receipt_path.parent / "project/Cargo.lock",
    }.items():
        require(value.get(key) == base.file_hash(ordinary_file(path)), "build_recipe_mismatch")
        paths.append(path)
    require(value["cargo_lock_sha256"] == base.file_hash(base.SCORING_CARGO_LOCK), "build_lock_mismatch")
    control = ROOT / "runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-worker"
    control_receipt = control.parent.parent / "BUILD_RECEIPT.json"
    control_value = json.loads(control_receipt.read_bytes())
    require(value.get("control_binary_sha256") == base.file_hash(control)
            == "b2204b9a48fa3d78ab9de63cc58f59032c23578781cde0f09bbba960809edf4e"
            and value.get("control_receipt_sha256") == base.file_hash(control_receipt), "control_identity_invalid")
    require(all(value.get(k) == control_value[k] for k in
                ("upstream_archive_sha256", "upstream_license_sha256", "upstream_license"))
            and value.get("scoring_patch_sha256") == base.file_hash(ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch"),
            "upstream_source_identity_invalid")
    require(binary.parent.parent / "BUILD_RECEIPT.json" == receipt_path, "binary_receipt_location_invalid")
    return value, [binary, receipt_path, control, control_receipt, *paths]

