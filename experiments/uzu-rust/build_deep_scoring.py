"""Build isolated deeper Uzu scoring experiments with immutable additive patch chains."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import build_scoring as base

ROOT = base.ROOT
EXPERIMENT = base.EXPERIMENT
OUTPUT = ROOT / "runtime/uzu-rust/deep-optimization"
PATCHES = {
    "topology": EXPERIMENT / "uzu-scoring-topology.patch",
    "state-init": EXPERIMENT / "uzu-scoring-state-init.patch",
    "submission": EXPERIMENT / "uzu-deep-submission.patch",
    "last-layer": EXPERIMENT / "uzu-deep-last-layer.patch",
    "metal": EXPERIMENT / "uzu-deep-metal.patch",
}
BASE = ("topology", "state-init")
VARIANTS = {
    "submission": (*BASE, "submission"),
    "last-layer": (*BASE, "last-layer"),
    "metal": (*BASE, "metal"),
    "submission-last-layer": (*BASE, "submission", "last-layer"),
    "all": (*BASE, "submission", "last-layer", "metal"),
}
FEATURES = {"state-init": "uzu-engine/deferred-state-init",
    "last-layer": "uzu-engine/scoring-last-layer-pruning",
    "metal": "uzu-engine/direct-delta-conv"}



def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition: bool) -> None:
    if not condition:
        raise RuntimeError("deep_build_identity_invalid")


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def prepare_project(destination: Path, upstream: Path) -> Path:
    """Use the same locked dependency graph with isolated local path overrides."""
    destination.mkdir()
    manifest = (base.SCORING / "Cargo.toml").read_text()
    manifest = manifest.replace('path = "../src/main.rs"',
                                "path = " + json.dumps(str(EXPERIMENT / "src/main.rs")))
    for name, source in {
        "uzu-engine": upstream / "crates/uzu-engine",
        "hanashi": upstream / "crates/legacy/hanashi",
        "shoji": upstream / "crates/legacy/shoji",
    }.items():
        original = "../../../runtime/uzu-rust/upstream/" + str(source.relative_to(upstream))
        require(original in manifest)
        manifest = manifest.replace(json.dumps(original), json.dumps(str(source)))
    probe = EXPERIMENT / "probes/score_digest.rs"
    require(probe.is_file())
    manifest += '\n[[bin]]\nname = "meecp-uzu-score-digest"\npath = ' + json.dumps(str(probe)) + "\n"
    (destination / "Cargo.toml").write_text(manifest)
    shutil.copyfile(base.SCORING / "Cargo.lock", destination / "Cargo.lock")
    return destination / "Cargo.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=tuple(VARIANTS))
    parser.add_argument("--upstream-source", type=Path)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    phase = "prepare"
    try:
        destination = OUTPUT / args.variant
        require(not destination.exists())
        destination.mkdir(parents=True)
        canonical_binary = ROOT / "runtime/uzu-rust/scoring-target/release/meecp-uzu-worker"
        historical = json.loads((EXPERIMENT / "jev-scoring/RESULTS_BF16.json").read_bytes())
        require(digest(canonical_binary) == historical["configuration"]["artifacts"]["uzu_binary_sha256"])
        # The original builder prepares an immutable Git archive plus the original
        # patch in this isolated destination. Its defaults and canonical tree stay
        # unchanged; no Cargo cache source is ever edited.
        base.RUNTIME = destination / "source"
        base.RUNTIME.mkdir()
        receipt = base.prepare(base.pinned_source(args.upstream_source))
        upstream = base.RUNTIME / "upstream"
        chain = [{"path": relative(base.PATCH), "sha256": digest(base.PATCH)}]
        environment = os.environ.copy()
        environment["GIT_CEILING_DIRECTORIES"] = str(base.RUNTIME)
        for name in VARIANTS[args.variant]:
            patch = PATCHES[name]
            base.command(["git", "apply", "--check", str(patch)], cwd=upstream, env=environment)
            base.command(["git", "apply", str(patch)], cwd=upstream, env=environment)
            chain.append({"path": relative(patch), "sha256": digest(patch)})
        manifest = prepare_project(destination / "project", upstream)
        cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
        require(Path(cargo).is_file())
        environment["PATH"] = str(Path(cargo).parent) + os.pathsep + environment.get("PATH", "")
        environment["CARGO_TARGET_DIR"] = str(OUTPUT / "target")
        environment["MEECP_UZU_SCORING_PATCH_SHA256"] = digest(base.PATCH)
        features = ["probability-scoring"] + [FEATURES[name] for name in VARIANTS[args.variant] if name in FEATURES]
        common = ["--release", "--locked", "--manifest-path", str(manifest),
                  "--features", ",".join(features)]
        phase = "build"
        print(json.dumps({"phase": "deep_build_started", "variant": args.variant}), flush=True)
        binaries = ["meecp-uzu-score-digest"]
        if args.variant != "baseline":
            binaries.append("meecp-uzu-worker")
        for binary in binaries:
            base.command([cargo, "build", *common, "--bin", binary], cwd=ROOT, env=environment)
        bin_dir = destination / "bin"
        bin_dir.mkdir()
        for binary in binaries:
            shutil.copyfile(OUTPUT / "target/release" / binary, bin_dir / binary)
            (bin_dir / binary).chmod(0o755)
        if args.variant == "baseline":
            shutil.copyfile(canonical_binary, bin_dir / "meecp-uzu-worker")
            (bin_dir / "meecp-uzu-worker").chmod(0o755)
        require(digest(canonical_binary) == historical["configuration"]["artifacts"]["uzu_binary_sha256"])
        if args.test:
            phase = "test"
            base.command([cargo, "test", *common, "--bin", "meecp-uzu-worker"], cwd=ROOT, env=environment)
        receipt.update({
            "schema_version": 2,
            "variant": args.variant,
            "upstream_revision": base.REVISION,
            "binary_sha256": digest(bin_dir / "meecp-uzu-worker"),
            "digest_binary_sha256": digest(bin_dir / "meecp-uzu-score-digest"),
            "patch_chain": chain,
            "features": features,
            "worker_sources": {relative(p): digest(p) for p in sorted((EXPERIMENT / "src").glob("*.rs"))},
            "probe_sources": {relative(p): digest(p) for p in sorted((EXPERIMENT / "probes").glob("*.rs"))},
            "build_recipe_sha256": digest(Path(__file__)),
            "base_build_recipe_sha256": digest(Path(base.__file__)),
            "original_scoring_manifest_sha256": digest(base.SCORING / "Cargo.toml"),
            "cargo_manifest_sha256": digest(manifest),
            "cargo_lock_sha256": digest(destination / "project/Cargo.lock"),
            "rustc_version": base.command([str(Path(cargo).parent / "rustc"), "--version"],
                                          env=environment).decode().strip(),
            "unit_tests_passed": bool(args.test),
            "cache_policy": "fresh-state",
            "scope": "experimental exact-token scoring; generation not accepted",
        })
        (destination / "BUILD_RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"phase": "deep_build_complete", "variant": args.variant,
                          "binary_sha256": receipt["binary_sha256"],
                          "unit_tests_passed": receipt["unit_tests_passed"]}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"phase": phase, "state": "failed", "error_type": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
