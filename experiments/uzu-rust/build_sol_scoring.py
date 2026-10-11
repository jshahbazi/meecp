"""Build additive Sol candidates without modifying accepted source or artifacts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

import build_deep_scoring as deep
import build_scoring as base

ROOT = base.ROOT
OUTPUT = ROOT / "runtime/uzu-rust/sol-optimization/builds"
PATCHES = {name: base.EXPERIMENT / "patches" / f"uzu-sol-{name}.patch"
           for name in ("rope", "gdn", "attention")}
VARIANTS = {"rope": ("rope",), "gdn": ("gdn",), "attention": ("attention",),
            "rope-gdn": ("rope", "gdn"), "rope-attention": ("rope", "attention"),
            "gdn-attention": ("gdn", "attention"),
            "combined": ("rope", "gdn", "attention")}
FEATURES = {"rope": "uzu-engine/sol-rope-cache", "gdn": "uzu-engine/compact-delta-conv",
            "attention": "uzu-engine/sol-attention-kv-only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=tuple(VARIANTS), required=True)
    parser.add_argument("--upstream-source", type=Path)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    phase = "prepare"
    try:
        destination = OUTPUT / args.variant
        deep.require(not destination.exists())
        destination.mkdir(parents=True)
        control = ROOT / "runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-worker"
        control_receipt = control.parent.parent / "BUILD_RECEIPT.json"
        receipt_control = json.loads(control_receipt.read_bytes())
        deep.require(deep.digest(control) == receipt_control["binary_sha256"]
                     == "b2204b9a48fa3d78ab9de63cc58f59032c23578781cde0f09bbba960809edf4e")
        base.RUNTIME = destination / "source"
        base.RUNTIME.mkdir()
        receipt = base.prepare(base.pinned_source(args.upstream_source))
        upstream = base.RUNTIME / "upstream"
        environment = os.environ.copy()
        environment["GIT_CEILING_DIRECTORIES"] = str(base.RUNTIME)
        patches = [deep.PATCHES[name] for name in deep.VARIANTS["all"]]
        patches += [PATCHES[name] for name in VARIANTS[args.variant]]
        chain = [{"path": deep.relative(base.PATCH), "sha256": deep.digest(base.PATCH)}]
        for patch in patches:
            base.command(["git", "apply", "--check", str(patch)], cwd=upstream, env=environment)
            base.command(["git", "apply", str(patch)], cwd=upstream, env=environment)
            chain.append({"path": deep.relative(patch), "sha256": deep.digest(patch)})
        manifest = deep.prepare_project(destination / "project", upstream)
        cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
        deep.require(Path(cargo).is_file())
        environment["PATH"] = str(Path(cargo).parent) + os.pathsep + environment.get("PATH", "")
        environment["CARGO_TARGET_DIR"] = str(OUTPUT / "target")
        environment["MEECP_UZU_SCORING_PATCH_SHA256"] = deep.digest(base.PATCH)
        features = ["probability-scoring"] + [deep.FEATURES[name] for name in deep.VARIANTS["all"] if name in deep.FEATURES]
        features += [FEATURES[name] for name in VARIANTS[args.variant]]
        common = ["--release", "--locked", "--manifest-path", str(manifest), "--features", ",".join(features)]
        phase = "build"
        print(json.dumps({"stage": "sol_build_started", "variant": args.variant}), flush=True)
        for binary in ("meecp-uzu-score-digest", "meecp-uzu-worker"):
            base.command([cargo, "build", *common, "--bin", binary], cwd=ROOT, env=environment)
        bin_dir = destination / "bin"
        bin_dir.mkdir()
        for binary in ("meecp-uzu-score-digest", "meecp-uzu-worker"):
            shutil.copyfile(OUTPUT / "target/release" / binary, bin_dir / binary)
            (bin_dir / binary).chmod(0o755)
        if args.test:
            phase = "test"
            base.command([cargo, "test", *common, "--bin", "meecp-uzu-worker"], cwd=ROOT, env=environment)
        deep.require(deep.digest(control) == receipt_control["binary_sha256"])
        receipt.update({"schema_version": 3, "variant": args.variant,
            "binary_sha256": deep.digest(bin_dir / "meecp-uzu-worker"),
            "digest_binary_sha256": deep.digest(bin_dir / "meecp-uzu-score-digest"),
            "patch_chain": chain, "features": features,
            "worker_sources": {deep.relative(p): deep.digest(p) for p in sorted((base.EXPERIMENT / "src").glob("*.rs"))},
            "probe_sources": {deep.relative(p): deep.digest(p) for p in sorted((base.EXPERIMENT / "probes").glob("*.rs"))},
            "build_recipe_sha256": deep.digest(Path(__file__)),
            "deep_build_recipe_sha256": deep.digest(Path(deep.__file__)),
            "base_build_recipe_sha256": deep.digest(Path(base.__file__)),
            "original_scoring_manifest_sha256": deep.digest(base.SCORING / "Cargo.toml"),
            "cargo_manifest_sha256": deep.digest(manifest),
            "cargo_lock_sha256": deep.digest(destination / "project/Cargo.lock"),
            "rustc_version": base.command([str(Path(cargo).parent / "rustc"), "--version"], env=environment).decode().strip(),
            "control_binary_sha256": deep.digest(control), "control_receipt_sha256": deep.digest(control_receipt),
            "unit_tests_passed": bool(args.test), "cache_policy": "fresh-state",
            "scope": "experimental exact-token scoring; generation not accepted"})
        (destination / "BUILD_RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"stage": "sol_build_complete", "variant": args.variant,
                          "binary_sha256": receipt["binary_sha256"], "unit_tests_passed": receipt["unit_tests_passed"]}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({"stage": phase, "error": "sol_build_failed", "error_type": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
