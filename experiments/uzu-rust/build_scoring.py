"""Prepare and build the pinned Uzu scoring extension without modifying Cargo's cache."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "experiments/uzu-rust"
RUNTIME = ROOT / "runtime/uzu-rust"
REVISION = "8d01581474760bd1fc7f4a547ff1d02f7dada437"
UPSTREAM_URL = "https://github.com/trymirai/uzu.git"
PATCH = EXPERIMENT / "uzu-next-token-scoring.patch"


def command(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, check=False)
    if result.returncode:
        # Only static build actions run here. Keep failures fixed and suppress
        # exception/command dumps, including local paths and environment values.
        raise RuntimeError("native_build_step_failed")
    return result.stdout


def pinned_source(explicit: Path | None) -> Path:
    if explicit:
        sources = [explicit.resolve()]
    else:
        cargo_home = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")))
        sources = sorted(cargo_home.glob("git/checkouts/uzu-*/8d01581*"))
    for source in sources:
        try:
            actual = command(["git", "rev-parse", f"{REVISION}^{{commit}}"], cwd=source).decode().strip()
        except (OSError, RuntimeError):
            continue
        if actual == REVISION:
            return source
    if explicit:
        raise RuntimeError("pinned_upstream_revision_unavailable")
    cache = RUNTIME / "upstream-cache"
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / ".git").exists():
        command(["git", "init", "--quiet"], cwd=cache)
    command(["git", "fetch", "--quiet", "--depth", "1", UPSTREAM_URL, REVISION], cwd=cache)
    return cache


def prepare(source: Path) -> dict[str, object]:
    archive = command(["git", "archive", REVISION], cwd=source)
    patch_bytes = PATCH.read_bytes()
    with tempfile.TemporaryDirectory(prefix="scoring-source-", dir=RUNTIME) as temporary:
        destination = Path(temporary) / "upstream"
        destination.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(destination, filter="data")
        patch_env = os.environ.copy()
        # The prepared source sits below Meecp's Git checkout. Prevent Git from
        # discovering that parent repository and silently skipping these paths
        # because they are outside its current repository-relative prefix.
        patch_env["GIT_CEILING_DIRECTORIES"] = str(RUNTIME)
        command(["git", "apply", "--check", str(PATCH)], cwd=destination, env=patch_env)
        command(["git", "apply", str(PATCH)], cwd=destination, env=patch_env)
        if not (destination / "crates/uzu-engine/src/engine/language_model/scoring.rs").is_file():
            raise RuntimeError("scoring_patch_not_applied")
        # Rebuild every time from an immutable Git object, so an edited local
        # upstream tree can never silently become part of the native artifact.
        target = RUNTIME / "upstream"
        if target.exists():
            shutil.rmtree(target)
        destination.rename(target)
    return {
        "schema_version": 1,
        "upstream_url": UPSTREAM_URL,
        "upstream_revision": REVISION,
        "upstream_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "scoring_patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "upstream_license": "MIT",
        "upstream_license_sha256": hashlib.sha256((RUNTIME / "upstream/LICENSE").read_bytes()).hexdigest(),
        "cache_policy": "fresh-state",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-source", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--test", action="store_true", help="Run the worker's Rust unit tests after building")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        receipt = prepare(pinned_source(args.upstream_source))
        print(json.dumps({"phase": "pinned_source_prepared", "scoring_patch_sha256": receipt["scoring_patch_sha256"]}), flush=True)
        if not args.prepare_only:
            cargo = shutil.which("cargo")
            if not cargo:
                candidate = Path.home() / ".cargo/bin/cargo"
                cargo = str(candidate) if candidate.exists() else None
            if not cargo:
                raise RuntimeError("cargo_unavailable")
            env = os.environ.copy()
            env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
            env["CARGO_TARGET_DIR"] = str(RUNTIME / "scoring-target")
            env["MEECP_UZU_SCORING_PATCH_SHA256"] = str(receipt["scoring_patch_sha256"])
            manifest = str(EXPERIMENT / "Cargo.toml")
            command([cargo, "build", "--release", "--locked", "--manifest-path", manifest], cwd=ROOT, env=env)
            binary = RUNTIME / "scoring-target/release/meecp-uzu-worker"
            receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
            receipt["cargo_lock_sha256"] = hashlib.sha256((EXPERIMENT / "Cargo.lock").read_bytes()).hexdigest()
            receipt["rustc_version"] = command([str(Path(cargo).parent / "rustc"), "--version"], env=env).decode().strip()
            if args.test:
                test_output = command([cargo, "test", "--release", "--locked", "--manifest-path", manifest], cwd=ROOT, env=env)
                # Test names and summary contain no request material.
                print(test_output.decode(), end="", flush=True)
                receipt["unit_tests_passed"] = True
            print(json.dumps({"phase": "native_build_completed", "binary_sha256": receipt["binary_sha256"]}), flush=True)
        (RUNTIME / "SCORING_BUILD_RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return 0
    except (OSError, RuntimeError, tarfile.TarError):
        print(json.dumps({"error": "scoring_build_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
