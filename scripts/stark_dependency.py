#!/usr/bin/env python3
"""Build a pinned Stark dependency or an explicitly selected development snapshot.

No model is loaded here. Native numerical acceptance is a separate, source-bound
step. Subprocess output is private and errors contain only fixed authored codes.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "engines/stark.lock.json"
RUNTIME_ROOT = ROOT / "runtime/stark"
REPOSITORY = "https://github.com/jshahbazi/stark.git"
CARGO = Path.home() / ".cargo/bin/cargo"
RUSTC = Path.home() / ".cargo/bin/rustc"
TOOLCHAIN = "1.98.1"
TARGET = "aarch64-apple-darwin"
BINARIES = ("stark-score", "stark-score-digest")
PROTOCOL_FILES = (
    "crates/stark-score/Cargo.toml", "crates/stark-score/build.rs",
    "crates/stark-score/src/main.rs", "crates/stark-score/src/protocol.rs",
    "crates/stark-score/src/cache.rs", "crates/stark-score/src/scoring.rs",
    "crates/stark-score/probes/score_digest.rs",
)
VALIDATION_FILES = (
    "scripts/validate_scoring_rename.py", "scripts/scoring_fixture.py",
    "scripts/scoring_process.py", "scripts/export_scoring_reference.py",
    "scripts/test_scoring_process.py", "scripts/stark-validation-reap.c",
)
BUILD_CONFIG_FILES = ("Cargo.toml", ".cargo/config.toml", "crates/stark-score/Cargo.toml")
REQUIRED_FILES = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", ".cargo/config.toml", *PROTOCOL_FILES, *VALIDATION_FILES)
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024


class DependencyError(Exception):
    """Fixed, content-free failure code suitable for the command line."""


def _fail(code: str) -> None:
    raise DependencyError(code)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        _fail("unsafe_file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _valid_hash(value: object, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in ("", ".", "..", ".git") for part in path.parts) or "\\" in name or str(path) != name:
        _fail("unsafe_source_path")
    return path


def _no_symlink_ancestors(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            _fail("unsafe_path_symlink")


def _write_new(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _no_symlink_ancestors(path.parent.absolute())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _base_env() -> dict[str, str]:
    # Do not inherit compiler wrappers, flags, Git rewrites, loader injection,
    # Cargo configuration overrides, or an implicit checkout override.
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(Path.home()), "LANG": "C", "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
    }


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, code: str = "command_failed") -> bytes:
    try:
        result = subprocess.run(args, cwd=cwd, env=env or _base_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except (OSError, ValueError):
        _fail(code)
    if result.returncode:
        _fail(code)
    return result.stdout


def _git(repo: Path, *args: str) -> bytes:
    return _run(["/usr/bin/git", "-c", "maintenance.auto=false", "-C", str(repo), *args], code="git_failed")


@contextlib.contextmanager
def _exclusive(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _no_symlink_ancestors(path.absolute())
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _source_hashes(source: Path) -> dict[str, str]:
    _no_symlink_ancestors(source.absolute())
    if not source.is_dir():
        _fail("invalid_source_directory")
    result: dict[str, str] = {}
    total = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            if (parent / name).is_symlink():
                _fail("unsafe_source_symlink")
        for name in files:
            path = parent / name
            relative = path.relative_to(source).as_posix()
            _relative(relative)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                _fail("unsafe_source_file")
            total += info.st_size
            if total > MAX_SOURCE_BYTES:
                _fail("source_too_large")
            result[relative] = _sha(path)
    return dict(sorted(result.items()))


def _archive_snapshot(repo: Path, revision: str, destination: Path) -> dict[str, str]:
    archive = _git(repo, "archive", "--format=tar", revision)
    destination.mkdir(mode=0o700)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        for member in stream:
            name = member.name.rstrip("/") if member.isdir() else member.name
            relative = _relative(name)
            path = destination.joinpath(*relative.parts)
            if member.isdir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not member.isfile() or member.size > MAX_FILE_BYTES:
                _fail("unsafe_source_file")
            total += member.size
            if total > MAX_SOURCE_BYTES:
                _fail("source_too_large")
            data = stream.extractfile(member)
            if data is None:
                _fail("invalid_source_archive")
            _write_new(path, data.read(), 0o700 if member.mode & 0o111 else 0o600)
    return _source_hashes(destination)


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        _fail("invalid_source_manifest")


def _make_lock(source: Path, revision: str, tree: str) -> dict:
    hashes = _source_hashes(source)
    if any(name not in hashes for name in REQUIRED_FILES):
        _fail("incomplete_source_contract")
    toolchain = _read_toml(source / "rust-toolchain.toml")
    manifest = _read_toml(source / "crates/stark-score/Cargo.toml")
    if toolchain.get("toolchain", {}).get("channel") != TOOLCHAIN or manifest.get("package", {}).get("name") != "stark-score" or manifest.get("package", {}).get("version") != "0.1.0":
        _fail("unsupported_build_contract")
    if "optimized" not in manifest.get("features", {}) or {item.get("name") for item in manifest.get("bin", [])} != set(BINARIES):
        _fail("unsupported_build_contract")
    scoring = (source / "crates/stark-score/src/scoring.rs").read_text()
    build = (source / "crates/stark-score/build.rs").read_text()
    upstream = re.search(r'"upstream_revision"\s*:\s*"([0-9a-f]{40})"', scoring)
    patch = re.search(r"STARK_SCORING_PATCH_SHA256=([0-9a-f]{64})", build)
    if not upstream or not patch:
        _fail("unsupported_protocol_identity")
    lock = {
        "schema_version": 1, "engine": "stark", "repository": REPOSITORY,
        "revision": revision, "tree": tree, "cargo_lock_sha256": hashes["Cargo.lock"],
        "toolchain": {"channel": TOOLCHAIN, "manifest_sha256": hashes["rust-toolchain.toml"]},
        "build": {"package": "stark-score", "features": ["optimized"], "default_features": True,
                  "target": TARGET, "binaries": list(BINARIES)},
        "protocol": {
            "version": 1, "worker_mode": "exact-token-scoring-cache-v1", "probe_mode": "exact-token-digest-cache-v1",
            "cache_default_enabled": False, "cache_policy_enabled": "consecutive-full-vocabulary-logits-v1",
            "cache_policy_disabled": "fresh-state", "cache_key": "exact-token-ids+context-tokens+model-instance",
            "ready_engine": "stark", "ready_version": "0.1.0", "upstream_revision": upstream.group(1),
            "scoring_patch_sha256": patch.group(1), "source_sha256": {name: hashes[name] for name in PROTOCOL_FILES},
        },
        "validation_tools_sha256": {name: hashes[name] for name in VALIDATION_FILES},
        "build_config_sha256": {name: hashes[name] for name in BUILD_CONFIG_FILES},
    }
    _validate_lock(lock)
    return lock


def derive_lock(source_git_dir: Path, revision: str) -> dict:
    """Derive an exact candidate lock from a locally available Git commit."""
    if not _valid_hash(revision, 40):
        _fail("revision_must_be_full_sha")
    repo = Path(source_git_dir).resolve()
    actual = _git(repo, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    if actual != revision:
        _fail("source_revision_mismatch")
    tree = _git(repo, "rev-parse", revision + "^{tree}").decode().strip()
    with tempfile.TemporaryDirectory(prefix="stark-lock-") as directory:
        source = Path(directory).resolve() / "source"
        _archive_snapshot(repo, revision, source)
        return _make_lock(source, revision, tree)


def _validate_lock(lock: object) -> None:
    if not isinstance(lock, dict) or set(lock) != {"schema_version", "engine", "repository", "revision", "tree", "cargo_lock_sha256", "toolchain", "build", "protocol", "validation_tools_sha256", "build_config_sha256"}:
        _fail("invalid_dependency_lock")
    expected_build = {"package": "stark-score", "features": ["optimized"], "default_features": True, "target": TARGET, "binaries": list(BINARIES)}
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1 or lock["engine"] != "stark" or lock["repository"] != REPOSITORY or lock["build"] != expected_build or type(lock["build"].get("default_features")) is not bool:
        _fail("unsupported_dependency_lock")
    for field, length in (("revision", 40), ("tree", 40), ("cargo_lock_sha256", 64)):
        if not _valid_hash(lock[field], length):
            _fail("invalid_dependency_lock")
    toolchain = lock["toolchain"]
    if not isinstance(toolchain, dict) or set(toolchain) != {"channel", "manifest_sha256"} or toolchain["channel"] != TOOLCHAIN or not _valid_hash(toolchain["manifest_sha256"]):
        _fail("unsupported_toolchain")
    protocol = lock["protocol"]
    constants = {
        "version": 1, "worker_mode": "exact-token-scoring-cache-v1", "probe_mode": "exact-token-digest-cache-v1",
        "cache_default_enabled": False, "cache_policy_enabled": "consecutive-full-vocabulary-logits-v1",
        "cache_policy_disabled": "fresh-state", "cache_key": "exact-token-ids+context-tokens+model-instance",
        "ready_engine": "stark", "ready_version": "0.1.0",
    }
    if not isinstance(protocol, dict) or set(protocol) != {*constants, "upstream_revision", "scoring_patch_sha256", "source_sha256"} or any(type(protocol.get(key)) is not type(value) or protocol.get(key) != value for key, value in constants.items()):
        _fail("unsupported_protocol_contract")
    if not _valid_hash(protocol["upstream_revision"], 40) or not _valid_hash(protocol["scoring_patch_sha256"]):
        _fail("invalid_protocol_identity")
    for mapping, paths in ((protocol["source_sha256"], PROTOCOL_FILES), (lock["validation_tools_sha256"], VALIDATION_FILES), (lock["build_config_sha256"], BUILD_CONFIG_FILES)):
        if not isinstance(mapping, dict) or set(mapping) != set(paths) or any(not _valid_hash(value) for value in mapping.values()):
            _fail("invalid_source_hashes")


def _read_json(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("invalid_json_record")
            result[key] = value
        return result

    try:
        if path.is_symlink():
            _fail("unsafe_file")
        result = json.loads(path.read_bytes(), object_pairs_hook=pairs,
                            parse_constant=lambda value: _fail("invalid_json_record"))
        if not isinstance(result, dict):
            _fail("invalid_json_record")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("invalid_json_record")


def load_lock(path: Path = DEFAULT_LOCK) -> dict:
    lock = _read_json(Path(path))
    _validate_lock(lock)
    return lock


def _repository() -> Path:
    _no_symlink_ancestors(RUNTIME_ROOT.absolute())
    RUNTIME_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    repo = RUNTIME_ROOT / "repository.git"
    if repo.is_symlink():
        _fail("unsafe_repository_cache")
    if not repo.exists():
        _run(["/usr/bin/git", "init", "--bare", str(repo)], code="repository_init_failed")
    return repo


def _fetch_revision(revision: str, *, offline: bool) -> Path:
    if not _valid_hash(revision, 40):
        _fail("revision_must_be_full_sha")
    repo = _repository()
    if not offline:
        _git(repo, "fetch", "--no-tags", REPOSITORY, revision)
    actual = _git(repo, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    if actual != revision:
        _fail("source_revision_mismatch")
    return repo


def _checkout_snapshot(checkout: Path, destination: Path) -> dict:
    checkout = checkout.resolve()
    actual_root = Path(_git(checkout, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual_root != checkout or destination.is_relative_to(checkout):
        _fail("invalid_checkout_override")
    head_before = _git(checkout, "rev-parse", "HEAD").decode().strip()
    tree = _git(checkout, "rev-parse", "HEAD^{tree}").decode().strip()
    status_before = _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths = _git(checkout, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    destination.mkdir(mode=0o700)
    copied: dict[str, str] = {}
    total = 0
    for encoded in sorted(set(paths)):
        if not encoded:
            continue
        name = encoded.decode("utf-8")
        relative = _relative(name)
        path = checkout.joinpath(*relative.parts)
        for parent in (path, *path.parents):
            if parent == checkout:
                break
            if parent.is_symlink():
                _fail("unsafe_source_symlink")
        if not path.exists():
            continue  # An explicitly selected dirty checkout can delete files.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            _fail("unsafe_source_file")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            _fail("source_too_large")
        _write_new(destination.joinpath(*relative.parts), data, 0o700 if info.st_mode & 0o111 else 0o600)
        copied[name] = _digest(data)
    if _git(checkout, "rev-parse", "HEAD").decode().strip() != head_before or _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all") != status_before:
        _fail("checkout_changed_during_snapshot")
    for name, digest in copied.items():
        if _sha(checkout / name) != digest:
            _fail("checkout_changed_during_snapshot")
    return {"revision": head_before, "tree": tree, "dirty": bool(status_before), "development_only": True}


def _verify_contract(source: Path, lock: dict, *, development: bool) -> None:
    hashes = _source_hashes(source)
    if any(name not in hashes for name in REQUIRED_FILES):
        _fail("incomplete_source_contract")
    if not development:
        expected = {**lock["protocol"]["source_sha256"], **lock["validation_tools_sha256"], "Cargo.lock": lock["cargo_lock_sha256"], "rust-toolchain.toml": lock["toolchain"]["manifest_sha256"]}
        if any(hashes.get(name) != digest for name, digest in expected.items()):
            _fail("source_contract_mismatch")
    # Local development can change engine source, but not silently switch the
    # dependency graph or compiler. Updates to these need a reviewed lock.
    if hashes["Cargo.lock"] != lock["cargo_lock_sha256"] or hashes["rust-toolchain.toml"] != lock["toolchain"]["manifest_sha256"]:
        _fail("source_dependency_mismatch")
    if any(hashes.get(name) != digest for name, digest in lock["build_config_sha256"].items()):
        _fail("source_build_configuration_mismatch")
    cargo_configs = {name for name in hashes if ".cargo" in PurePosixPath(name).parts}
    if cargo_configs != {".cargo/config.toml"}:
        _fail("unlocked_cargo_configuration")


def _cargo_environment(output: Path, target: Path) -> dict[str, str]:
    cargo_home = output / "cargo-home"
    cargo_home.mkdir(mode=0o700)
    # Reuse dependency downloads only. Never load the user's Cargo config.
    original = Path.home() / ".cargo"
    for name in ("registry", "git"):
        cache = original / name
        if cache.is_dir():
            (cargo_home / name).symlink_to(cache.resolve(), target_is_directory=True)
    env = _base_env()
    env.update({"CARGO_HOME": str(cargo_home), "CARGO_TARGET_DIR": str(target),
                "RUSTUP_HOME": str(Path.home() / ".rustup"), "RUST_TEST_THREADS": "1",
                "RUSTC": str(RUSTC)})
    return env


def _compiler() -> str:
    compiler = _run([str(RUSTC), "+" + TOOLCHAIN, "--version"], code="compiler_unavailable").decode().strip()
    if not re.fullmatch(r"rustc 1\.98\.1 \([0-9a-f]+ [0-9-]+\)", compiler):
        _fail("compiler_version_mismatch")
    return compiler


def _unit_summary(stdout: bytes) -> dict:
    results = re.findall(rb"test result: ok\. (\d+) passed; 0 failed;", stdout)
    if len(results) != len(BINARIES) or any(int(count) == 0 for count in results):
        _fail("invalid_unit_test_summary")
    return {"passed": sum(map(int, results)), "failures": 0, "binary_suites": len(results)}


def build_dependency(lock: dict, *, output: Path | None = None, checkout: Path | None = None, offline: bool = False) -> Path:
    """Create a new build and receipt; never reuse or mutate a completed build."""
    _validate_lock(lock)
    if platform.system() != "Darwin" or platform.machine() not in ("arm64", "aarch64"):
        _fail("unsupported_build_host")
    output = Path(output) if output else RUNTIME_ROOT / "builds" / (lock["revision"][:12] + "-" + uuid.uuid4().hex[:12])
    if ".." in output.parts:
        _fail("unsafe_build_output")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        _fail("build_output_already_exists")
    for ancestor in output.parents:
        if ancestor.is_symlink():
            _fail("unsafe_build_output")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.mkdir(mode=0o700)
    _write_new(output / "DEPENDENCY_LOCK.json", _json_bytes(lock))
    source = output / "source"
    development = checkout is not None
    if development:
        identity = _checkout_snapshot(Path(checkout), source)
    else:
        with _exclusive(RUNTIME_ROOT / "repository.lock"):
            repo = _fetch_revision(lock["revision"], offline=offline)
            tree = _git(repo, "rev-parse", lock["revision"] + "^{tree}").decode().strip()
            if tree != lock["tree"]:
                _fail("source_tree_mismatch")
            _archive_snapshot(repo, lock["revision"], source)
        identity = {"revision": lock["revision"], "tree": tree, "dirty": False, "development_only": False}
    _verify_contract(source, lock, development=development)
    before = _source_hashes(source)
    compiler = _compiler()
    cache_key = _digest(_json_bytes({"source_files": before, "compiler": compiler, "build": lock["build"]}))
    target = RUNTIME_ROOT / "targets" / cache_key
    _no_symlink_ancestors(target.absolute())
    env = _cargo_environment(output, target)
    common = [str(CARGO), "+" + TOOLCHAIN, "--config", str(source / ".cargo/config.toml")]
    flags = ["--manifest-path", str(source / "Cargo.toml"), "--release", "--locked", "--target", TARGET, "-p", lock["build"]["package"], "--features", ",".join(lock["build"]["features"])]
    if offline:
        flags.append("--offline")
    with _exclusive(RUNTIME_ROOT / "target-locks" / (cache_key + ".lock")):
        # Cargo discovers configuration from its working directory, not from
        # --manifest-path. A neutral directory and explicit pinned config keep
        # both the user's home config and the host repository config out.
        with tempfile.TemporaryDirectory(prefix="stark-cargo-") as directory:
            work = Path(directory).resolve()
            for ancestor in (work, *work.parents):
                if any((ancestor / ".cargo" / name).exists() for name in ("config", "config.toml")):
                    _fail("ambient_cargo_configuration")
            _run([*common, "build", *flags], cwd=work, env=env, code="cargo_build_failed")
            tests = _unit_summary(_run([*common, "test", *flags, "--", "--test-threads=1"], cwd=work, env=env, code="cargo_tests_failed"))
        if _source_hashes(source) != before or _compiler() != compiler:
            _fail("build_inputs_changed")
        binaries: dict[str, str] = {}
        for name in BINARIES:
            binary = target / TARGET / "release" / name
            digest = _sha(binary)
            _write_new(output / "bin" / name, binary.read_bytes(), 0o700)
            if _sha(output / "bin" / name) != digest:
                _fail("binary_copy_mismatch")
            binaries[name] = digest
    receipt = {
        "schema_version": 1, "engine": "stark", "source": identity,
        "dependency_lock_sha256": _digest(_json_bytes(lock)), "source_files": before,
        "compiler": compiler, "target": TARGET, "build": lock["build"], "binaries": binaries,
        "unit_tests": tests, "cache_default_enabled": False,
        "native_acceptance_required": True,
        "validation_tools_match_lock": all(before.get(name) == digest for name, digest in lock["validation_tools_sha256"].items()),
    }
    _write_new(output / "BUILD_RECEIPT.json", _json_bytes(receipt), 0o400)
    verify_build(output, lock)
    return output


def verify_build(build: Path, lock: dict | Path | None = None) -> dict:
    """Reject modified source, binaries, lock bindings, and compiler identity."""
    build = Path(build).absolute()
    _no_symlink_ancestors(build)
    _no_symlink_ancestors(build / "source")
    _no_symlink_ancestors(build / "bin")
    if build.is_symlink() or not build.is_dir():
        _fail("invalid_build_directory")
    copied = load_lock(build / "DEPENDENCY_LOCK.json")
    selected = load_lock(lock) if isinstance(lock, (str, Path)) else lock
    if selected is not None:
        _validate_lock(selected)
        if selected != copied:
            _fail("build_lock_mismatch")
    receipt = _read_json(build / "BUILD_RECEIPT.json")
    try:
        source = receipt["source"]
        if receipt["schema_version"] != 1 or receipt["engine"] != "stark" or receipt["dependency_lock_sha256"] != _digest(_json_bytes(copied)) or receipt["build"] != copied["build"] or receipt["target"] != TARGET:
            _fail("invalid_build_receipt")
        if type(source["development_only"]) is not bool or type(source["dirty"]) is not bool or not _valid_hash(source["revision"], 40) or not _valid_hash(source["tree"], 40):
            _fail("invalid_build_receipt")
        if not source["development_only"] and (source != {"revision": copied["revision"], "tree": copied["tree"], "dirty": False, "development_only": False}):
            _fail("build_source_identity_mismatch")
        actual = _source_hashes(build / "source")
        if actual != receipt["source_files"]:
            _fail("build_source_modified")
        _verify_contract(build / "source", copied, development=source["development_only"])
        if set(receipt["binaries"]) != set(BINARIES) or any(_sha(build / "bin" / name) != receipt["binaries"][name] for name in BINARIES):
            _fail("build_binary_modified")
        if receipt["compiler"] != _compiler():
            _fail("build_compiler_changed")
        tests = receipt["unit_tests"]
        if tests["failures"] != 0 or tests["binary_suites"] != 2 or type(tests["passed"]) is not int or tests["passed"] <= 0 or receipt["cache_default_enabled"] is not False or receipt["native_acceptance_required"] is not True:
            _fail("invalid_build_acceptance")
        if receipt["validation_tools_match_lock"] != all(actual.get(name) == digest for name, digest in copied["validation_tools_sha256"].items()):
            _fail("validation_tools_identity_mismatch")
    except (KeyError, TypeError):
        _fail("invalid_build_receipt")
    return receipt


def update_dependency(lock_path: Path, revision: str, *, output: Path | None = None, offline: bool = False) -> Path:
    """Build/test an explicit remote commit before atomically replacing the pin."""
    if not _valid_hash(revision, 40):
        _fail("revision_must_be_full_sha")
    path = Path(lock_path).absolute()
    previous = path.read_bytes()
    load_lock(path)
    with _exclusive(RUNTIME_ROOT / "repository.lock"):
        repo = _fetch_revision(revision, offline=offline)
        candidate = derive_lock(repo, revision)
    build = build_dependency(candidate, output=output, offline=offline)
    with _exclusive(RUNTIME_ROOT / ("update-" + _digest(str(path).encode()) + ".lock")):
        if path.is_symlink() or path.read_bytes() != previous:
            _fail("dependency_lock_changed_concurrently")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        _write_new(temporary, _json_bytes(candidate), 0o644)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return build


def main() -> int:
    class SafeParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            _fail("invalid_command_arguments")

    parser = SafeParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "update"):
        command = subcommands.add_parser(name)
        command.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
        command.add_argument("--output", type=Path)
        command.add_argument("--offline", action="store_true")
        if name == "build":
            command.add_argument("--checkout", type=Path)
        else:
            command.add_argument("--revision", required=True)
    command = subcommands.add_parser("verify")
    command.add_argument("--build", type=Path, required=True)
    command.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    try:
        args = parser.parse_args()
        if args.command == "build":
            build = build_dependency(load_lock(args.lock), output=args.output, checkout=args.checkout, offline=args.offline)
            receipt = verify_build(build, load_lock(args.lock))
        elif args.command == "update":
            build = update_dependency(args.lock, args.revision, output=args.output, offline=args.offline)
            receipt = verify_build(build, load_lock(args.lock))
        else:
            build = args.build
            receipt = verify_build(build, load_lock(args.lock))
        print(json.dumps({"ok": True, "build": str(build), "revision": receipt["source"]["revision"],
                          "development_only": receipt["source"]["development_only"],
                          "dirty": receipt["source"]["dirty"], "unit_tests_passed": receipt["unit_tests"]["passed"],
                          "native_acceptance_required": receipt["native_acceptance_required"]}, sort_keys=True))
        return 0
    except DependencyError as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "dependency_operation_interrupted"}, sort_keys=True))
        return 1
    except (OSError, ValueError, UnicodeError, tarfile.TarError):
        print(json.dumps({"ok": False, "error": "dependency_operation_failed"}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "dependency_operation_failed"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
