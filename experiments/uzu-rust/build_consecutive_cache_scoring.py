"""Build a fresh versioned Sol distribution-cache worker; never load a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import build_deep_scoring as deep
import build_scoring as base
import build_sol_scoring as sol

ROOT = base.ROOT
WORKER = base.EXPERIMENT / "consecutive-cache"
OUTPUT = ROOT / "runtime/uzu-rust/consecutive-cache/builds"
SOL_CONTROL = ROOT / "runtime/uzu-rust/sol-optimization/builds/combined"
CACHE_POLICY = "consecutive-full-vocabulary-logits-v1"
BINARIES = ("meecp-uzu-cache-worker", "meecp-uzu-cache-score-digest")


def require(value):
    if not value:
        raise ValueError("cache_build_identity_invalid")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(path):
    return Path(path).relative_to(ROOT).as_posix()


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def patch_chain():
    return [base.PATCH, *(deep.PATCHES[name] for name in deep.VARIANTS["all"]),
            *(sol.PATCHES[name] for name in sol.VARIANTS["combined"])]


def source_bindings():
    paths = [Path(__file__), Path(base.__file__), Path(deep.__file__), Path(sol.__file__),
             base.SCORING / "Cargo.toml", base.SCORING / "Cargo.lock", *patch_chain()]
    paths += sorted((base.EXPERIMENT / "src").glob("*.rs"))
    paths += sorted((base.EXPERIMENT / "probes").glob("*.rs"))
    paths += sorted(WORKER.rglob("*.rs"))
    paths += [WORKER / "Cargo.toml", WORKER / "Cargo.lock"]
    return {relative(path): digest(path) for path in paths}


def verify_sol_control():
    receipt_path = SOL_CONTROL / "BUILD_RECEIPT.json"
    receipt = json.loads(receipt_path.read_bytes())
    require(receipt["variant"] == "combined" and receipt["cache_policy"] == "fresh-state"
            and receipt["upstream_revision"] == base.REVISION)
    require(digest(SOL_CONTROL / "bin/meecp-uzu-worker") == receipt["binary_sha256"]
            == "384310c592ef1f217e148dea51f8182cc95d5c14e724cb38afde2bc22b0840d1")
    require(digest(SOL_CONTROL / "bin/meecp-uzu-score-digest") == receipt["digest_binary_sha256"])
    expected_chain = [{"path": relative(path), "sha256": digest(path)} for path in patch_chain()]
    require(receipt["patch_chain"] == expected_chain)
    for group in ("worker_sources", "probe_sources"):
        require(all(digest(ROOT / path) == value for path, value in receipt[group].items()))
    require(digest(Path(sol.__file__)) == receipt["build_recipe_sha256"]
            and digest(Path(deep.__file__)) == receipt["deep_build_recipe_sha256"]
            and digest(Path(base.__file__)) == receipt["base_build_recipe_sha256"])
    return {"binary_sha256": receipt["binary_sha256"],
            "digest_binary_sha256": receipt["digest_binary_sha256"],
            "receipt_sha256": digest(receipt_path), "path": relative(SOL_CONTROL)}


def prepare_project(destination, upstream, bindings):
    # Build the exact source copies bound here, never live worker source files.
    copied = destination / "source/worker"
    shutil.copytree(WORKER, copied)
    require(all(digest(copied / Path(path).relative_to(WORKER)) == bindings[relative(path)]
                for path in WORKER.rglob("*") if path.is_file()))
    project = destination / "project"
    project.mkdir()
    manifest = (copied / "Cargo.toml").read_text()
    for old, new in (("src/main.rs", copied / "src/main.rs"),
                     ("probes/score_digest.rs", copied / "probes/score_digest.rs")):
        require(manifest.count(json.dumps(old)) == 1)
        manifest = manifest.replace(json.dumps(old), json.dumps(str(new)))
    for name, source in {"uzu-engine": upstream / "crates/uzu-engine",
                         "hanashi": upstream / "crates/legacy/hanashi",
                         "shoji": upstream / "crates/legacy/shoji"}.items():
        old = "../../../runtime/uzu-rust/upstream/" + str(source.relative_to(upstream))
        require(manifest.count(json.dumps(old)) == 1)
        manifest = manifest.replace(json.dumps(old), json.dumps(str(source)))
    (project / "Cargo.toml").write_text(manifest)
    shutil.copyfile(copied / "Cargo.lock", project / "Cargo.lock")
    return project / "Cargo.toml"


def command(args, environment):
    result = subprocess.run(args, cwd=ROOT, env=environment, capture_output=True)
    raw = result.stdout + result.stderr
    require(result.returncode == 0)
    return raw


def candidate_identity(directory):
    """Verify completed native build/source/test bindings; never load the model."""
    directory = Path(directory).absolute()
    require(directory.is_relative_to(OUTPUT) and not any(p.is_symlink() for p in (directory, *directory.parents)))
    receipt_path = directory / "BUILD_RECEIPT.json"
    receipt = json.loads(receipt_path.read_bytes())
    require(receipt["schema_version"] == 4 and receipt["variant"] == "sol-consecutive-cache-v1"
            and receipt["upstream_revision"] == base.REVISION
            and receipt["cache_policy"] == CACHE_POLICY and receipt["cache_enabled_by_default"] is False
            and receipt["cache_flag"] == "--consecutive-cache" and receipt["unit_tests_passed"] is True)
    require(receipt["source_bindings"] == source_bindings())
    require(receipt["sol_control"] == verify_sol_control())
    require(receipt["patch_chain"] == [{"path": relative(path), "sha256": digest(path)} for path in patch_chain()])
    for group in ("source_bindings", "prepared_worker_sources", "prepared_upstream_sources"):
        require(all(digest(ROOT / path) == value for path, value in receipt[group].items()))
    for binary in BINARIES:
        require(digest(directory / "bin" / binary) == receipt["binaries"][binary])
    require(receipt["binary_sha256"] == receipt["binaries"][BINARIES[0]]
            and receipt["digest_binary_sha256"] == receipt["binaries"][BINARIES[1]])
    require(digest(directory / "project/Cargo.toml") == receipt["cargo_manifest_sha256"]
            and digest(directory / "project/Cargo.lock") == receipt["cargo_lock_sha256"])
    tests_path = directory / "UNIT_TESTS.json"
    require(digest(tests_path) == receipt["unit_test_receipt_sha256"])
    tests = json.loads(tests_path.read_bytes())
    require(tests["status"] == "complete" and tests["source_bindings"] == receipt["source_bindings"]
            and tests["build_recipe_sha256"] == receipt["build_recipe_sha256"]
            and [row["binary"] for row in tests["results"]] == list(BINARIES)
            and all(type(row["passed"]) is int and row["passed"] >= 17 for row in tests["results"])
            and tests["tests_passed"] == sum(row["passed"] for row in tests["results"]))
    return {"build_path": relative(directory), "build_receipt_sha256": digest(receipt_path),
        "binary_sha256": receipt["binary_sha256"], "digest_binary_sha256": receipt["digest_binary_sha256"],
        "unit_test_receipt_sha256": receipt["unit_test_receipt_sha256"], "tests_passed": tests["tests_passed"],
        "source_bindings": receipt["source_bindings"], "prepared_worker_sources": receipt["prepared_worker_sources"],
        "prepared_upstream_sources": receipt["prepared_upstream_sources"],
        "cache_policy": CACHE_POLICY, "cache_flag": receipt["cache_flag"],
        "sol_control": receipt["sol_control"], "patch_chain": receipt["patch_chain"],
        "cargo_manifest_sha256": receipt["cargo_manifest_sha256"], "cargo_lock_sha256": receipt["cargo_lock_sha256"]}


def build(name, upstream_source, run_tests):
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) is not None)
    destination = OUTPUT / name
    require(not destination.exists() and not any(p.is_symlink() for p in (destination, *destination.parents)))
    control = verify_sol_control()
    bindings = source_bindings()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    original_runtime = base.RUNTIME
    try:
        base.RUNTIME = destination / "source"
        base.RUNTIME.mkdir()
        prepared = base.prepare(base.pinned_source(upstream_source))
    finally:
        base.RUNTIME = original_runtime
    upstream = destination / "source/upstream"
    environment = os.environ.copy()
    environment.pop("RUSTC_BOOTSTRAP", None)
    environment["GIT_CEILING_DIRECTORIES"] = str(destination / "source")
    for patch in patch_chain()[1:]:
        base.command(["git", "apply", "--check", str(patch)], cwd=upstream, env=environment)
        base.command(["git", "apply", str(patch)], cwd=upstream, env=environment)
    manifest = prepare_project(destination, upstream, bindings)
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    require(Path(cargo).is_file())
    environment["PATH"] = str(Path(cargo).parent) + os.pathsep + environment.get("PATH", "")
    environment["CARGO_TARGET_DIR"] = str(destination / "target")
    environment["MEECP_UZU_SCORING_PATCH_SHA256"] = digest(base.PATCH)
    features = ["probability-scoring"] + [deep.FEATURES[name] for name in deep.VARIANTS["all"] if name in deep.FEATURES]
    features += [sol.FEATURES[name] for name in sol.VARIANTS["combined"]]
    common = ["--release", "--locked", "--manifest-path", str(manifest), "--features", ",".join(features)]
    print(json.dumps({"stage": "cache_build_started", "name": name}), flush=True)
    for binary in BINARIES:
        command([cargo, "build", *common, "--bin", binary], environment)
    bins = destination / "bin"
    bins.mkdir()
    for binary in BINARIES:
        shutil.copyfile(destination / "target/release" / binary, bins / binary)
        (bins / binary).chmod(0o755)
    tests = []
    if run_tests:
        for binary in BINARIES:
            raw = command([cargo, "test", *common, "--bin", binary, "--", "--test-threads=1"], environment)
            counts = re.findall(rb"test result: ok\. (\d+) passed; 0 failed;", raw)
            require(len(counts) == 1 and int(counts[0]) >= 17)
            tests.append({"binary": binary, "passed": int(counts[0]), "output_sha256": hashlib.sha256(raw).hexdigest()})
        private_json(destination / "UNIT_TESTS.json", {"schema_version": 1, "status": "complete",
            "backend": "fake scoring backend; no model loading", "results": tests,
            "tests_passed": sum(row["passed"] for row in tests),
            "source_bindings": bindings, "build_recipe_sha256": digest(Path(__file__))})
    require(source_bindings() == bindings and verify_sol_control() == control)
    receipt = {**prepared, "schema_version": 4, "variant": "sol-consecutive-cache-v1",
        "binary_sha256": digest(bins / BINARIES[0]), "digest_binary_sha256": digest(bins / BINARIES[1]),
        "binaries": {binary: digest(bins / binary) for binary in BINARIES},
        "patch_chain": [{"path": relative(path), "sha256": digest(path)} for path in patch_chain()],
        "features": features, "source_bindings": bindings, "sol_control": control,
        "prepared_worker_sources": {relative(path): digest(path) for path in sorted((destination / "source/worker").rglob("*")) if path.is_file()},
        "prepared_upstream_sources": {relative(path): digest(path) for path in sorted(upstream.rglob("*")) if path.is_file()},
        "build_recipe_sha256": digest(Path(__file__)),
        "cargo_manifest_sha256": digest(manifest), "cargo_lock_sha256": digest(destination / "project/Cargo.lock"),
        "rustc_version": base.command([str(Path(cargo).parent / "rustc"), "--version"], env=environment).decode().strip(),
        "unit_tests_passed": run_tests, "unit_test_receipt_sha256": digest(destination / "UNIT_TESTS.json") if run_tests else None,
        "cache_policy": CACHE_POLICY, "cache_enabled_by_default": False, "cache_flag": "--consecutive-cache",
        "scope": "experimental Sol exact-token full-vocabulary distribution reuse; no production integration"}
    private_json(destination / "BUILD_RECEIPT.json", receipt)
    print(json.dumps({"stage": "cache_build_complete", "name": name,
        "binary_sha256": receipt["binary_sha256"], "tests_passed": sum(row["passed"] for row in tests)}), flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="sol-cache-v1")
    parser.add_argument("--upstream-source", type=Path)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    try:
        build(args.name, args.upstream_source, args.test)
        return 0
    except Exception:
        print(json.dumps({"stage": "failed", "error": "consecutive_cache_build_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
