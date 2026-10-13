"""Exercise Stark dependency selection and provenance without a model or compiler.

The fixture is a real local Git repository. Small compiler stand-ins make build
failures, environment contamination, and artifact changes deterministic.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stark_dependency as dependency


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def commit(repository: Path, message: str) -> str:
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "upstream"
    source.mkdir()
    git(source, "init", "--quiet")
    git(source, "config", "user.name", "Dependency Test")
    git(source, "config", "user.email", "dependency-test@example.invalid")
    write(source / "Cargo.toml", '[workspace]\nmembers = ["crates/stark-score"]\nresolver = "3"\n')
    write(source / "Cargo.lock", '# Synthetic locked dependency graph\nversion = 4\n')
    write(source / "rust-toolchain.toml", '[toolchain]\nchannel = "1.98.1"\nprofile = "minimal"\n')
    write(source / ".cargo/config.toml", "# Synthetic repository Cargo configuration\n")
    write(source / "LICENSE", "Synthetic upstream license fixture\n")
    write(source / "crates/stark-score/Cargo.toml", '''[package]
name = "stark-score"
version = "0.1.0"
edition = "2024"
rust-version = "1.98"
[[bin]]
name = "stark-score"
path = "src/main.rs"
[[bin]]
name = "stark-score-digest"
path = "probes/score_digest.rs"
[features]
default = ["probability-scoring"]
probability-scoring = []
optimized = []
''')
    for name in ("main.rs", "protocol.rs", "cache.rs", "scoring.rs"):
        write(source / "crates/stark-score/src" / name, "// synthetic " + name + "\n")
    write(source / "crates/stark-score/src/scoring.rs", '"upstream_revision": "' + "1" * 40 + '"\n')
    write(source / "crates/stark-score/build.rs", "// STARK_SCORING_PATCH_SHA256=" + "2" * 64 + "\n")
    write(source / "crates/stark-score/probes/score_digest.rs", "// synthetic digest worker\n")
    for name in (
        "export_scoring_reference.py", "scoring_fixture.py", "scoring_process.py",
        "stark-validation-reap.c", "test_scoring_process.py", "validate_scoring_rename.py",
    ):
        write(source / "scripts" / name, "// synthetic validation tool\n")
    revision = commit(source, "Initial synthetic engine")

    tools = tmp_path / "tools"
    tools.mkdir()
    state = tools / "state.json"
    state.write_text(json.dumps({"fail": None, "version": "1.98.1"}))
    fake = f"#!{sys.executable}\n" + '''import hashlib, json, os
from pathlib import Path
import sys
root = Path(__file__).parent
state = json.loads((root / "state.json").read_text())
name = Path(__file__).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"name": name, "args": args, "env": dict(os.environ), "cwd": os.getcwd()}) + "\\n")
if name == "rustc":
    version = "rustc " + state["version"] + " (1234567890a 2026-10-11)"
    if "-vV" in args or "--verbose" in args:
        version += "\\nbinary: rustc\\ncommit-hash: synthetic\\ncommit-date: 2026-10-11\\nhost: aarch64-apple-darwin\\nrelease: " + state["version"] + "\\nLLVM version: 22.0.0"
    print(version)
elif "--version" in args or "-V" in args:
    print("cargo " + state["version"] + " (synthetic 2026-10-11)")
elif "build" in args:
    if state["fail"] == "build":
        raise SystemExit(9)
    target = Path(os.environ.get("CARGO_TARGET_DIR", "target"))
    if "--target-dir" in args:
        target = Path(args[args.index("--target-dir") + 1])
    if "--target" in args:
        target /= args[args.index("--target") + 1]
    target /= "release"
    target.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256()
    source = Path(os.getcwd())
    if "--manifest-path" in args:
        source = Path(args[args.index("--manifest-path") + 1]).parent
    for path in sorted(source.rglob("*.rs")):
        fingerprint.update(path.read_bytes())
    for binary in ("stark-score", "stark-score-digest"):
        path = target / binary
        path.write_bytes(b"fake compiled " + binary.encode() + fingerprint.digest())
        path.chmod(0o755)
elif "test" in args:
    if state["fail"] == "test":
        print("test result: FAILED. 18 passed; 1 failed; 0 ignored")
        raise SystemExit(9)
    print("test result: ok. 19 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s")
    print("test result: ok. 19 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s")
else:
    raise SystemExit(8)
'''
    for name in ("cargo", "rustc"):
        executable = tools / name
        executable.write_text(fake)
        executable.chmod(0o755)
    monkeypatch.setattr(dependency, "REPOSITORY", str(source))
    monkeypatch.setattr(dependency, "CARGO", tools / "cargo")
    monkeypatch.setattr(dependency, "RUSTC", tools / "rustc")
    monkeypatch.setattr(dependency, "RUNTIME_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(dependency.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dependency.platform, "machine", lambda: "arm64")
    lock = dependency.derive_lock(source, revision)
    lock_path = tmp_path / "stark.lock.json"
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")
    monkeypatch.setattr(dependency, "DEFAULT_LOCK", lock_path)
    return {
        "source": source, "revision": revision, "lock": lock, "lock_path": lock_path,
        "tools": tools, "state": state, "root": tmp_path,
    }


def set_state(project, **changes):
    state = json.loads(project["state"].read_text())
    state.update(changes)
    project["state"].write_text(json.dumps(state))


def build(project, name="official", **kwargs):
    return dependency.build_dependency(
        project["lock"], output=project["root"] / name, **kwargs,
    )


def calls(project):
    path = project["tools"] / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_lock_derivation_round_trips_exact_commit_and_build_contract(project):
    lock = dependency.load_lock(project["lock_path"])
    assert lock["revision"] == project["revision"]
    assert lock["tree"] == git(project["source"], "rev-parse", "HEAD^{tree}")
    assert lock["build"]["features"] == ["optimized"]
    assert lock["protocol"]["cache_default_enabled"] is False


@pytest.mark.parametrize("change", [
    {"revision": "main"}, {"revision": "1234567"}, {"repository": "https://example.invalid/other.git"},
    {"schema_version": 2}, {"unknown": "unexpected"},
])
def test_lock_rejects_floating_revision_foreign_source_or_unknown_schema(project, change):
    lock = json.loads(project["lock_path"].read_text())
    lock.update(change)
    project["lock_path"].write_text(json.dumps(lock))
    with pytest.raises(dependency.DependencyError):
        dependency.load_lock(project["lock_path"])
    assert not calls(project)


@pytest.mark.parametrize("field,value", [
    ("features", ["optimized", "unexpected"]), ("target", "x86_64-apple-darwin"),
    ("default_features", False), ("binaries", ["other-worker"]),
])
def test_lock_rejects_unreviewed_build_contract(project, field, value):
    lock = json.loads(project["lock_path"].read_text())
    lock["build"][field] = value
    project["lock_path"].write_text(json.dumps(lock))
    with pytest.raises(dependency.DependencyError):
        dependency.load_lock(project["lock_path"])


@pytest.mark.parametrize("group,field,value", [
    (None, "schema_version", True), (None, "schema_version", 1.0),
    ("build", "default_features", 1), ("protocol", "version", True),
    ("protocol", "cache_default_enabled", 0),
])
def test_lock_json_types_are_exact_not_python_boolean_numeric_equivalents(project, group, field, value):
    lock = json.loads(project["lock_path"].read_text())
    selected = lock[group] if group else lock
    selected[field] = value
    project["lock_path"].write_text(json.dumps(lock))
    with pytest.raises(dependency.DependencyError):
        dependency.load_lock(project["lock_path"])


def test_official_build_uses_commit_snapshot_even_when_checkout_dirty(project, monkeypatch):
    original = (project["source"] / "crates/stark-score/src/cache.rs").read_bytes()
    (project["source"] / "crates/stark-score/src/cache.rs").write_text("// different dirty cache\n")
    monkeypatch.setenv("MEECP_STARK_CHECKOUT", str(project["source"]))
    artifact = build(project)
    receipt = dependency.verify_build(artifact, project["lock"])
    assert receipt["source"] == {"revision": project["revision"], "tree": project["lock"]["tree"],
                                  "dirty": False, "development_only": False}
    assert (artifact / "source/crates/stark-score/src/cache.rs").read_bytes() == original
    assert receipt["unit_tests"]["passed"] == 38
    assert receipt["native_acceptance_required"] is True


def test_explicit_dirty_override_is_isolated_and_never_changes_official_pin(project):
    lock_before = project["lock_path"].read_bytes()
    dirty = project["source"] / "crates/stark-score/src/cache.rs"
    dirty.write_text("// first local engine revision\n")
    added = project["source"] / "crates/stark-score/src/local.rs"
    added.write_text("// untracked local engine module\n")
    artifact = build(project, "development", checkout=project["source"])
    receipt = dependency.verify_build(artifact, project["lock"])
    assert receipt["source"]["development_only"] is True
    assert receipt["source"]["dirty"] is True
    assert (artifact / "source/crates/stark-score/src/cache.rs").read_bytes() == dirty.read_bytes()
    assert (artifact / "source/crates/stark-score/src/local.rs").read_bytes() == added.read_bytes()
    dirty.write_text("// a later local engine revision\n")
    added.unlink()
    assert dependency.verify_build(artifact, project["lock"]) == receipt
    assert project["lock_path"].read_bytes() == lock_before
    official = build(project, "official-after-development")
    assert dependency.verify_build(official, project["lock"])["source"]["development_only"] is False
    assert (official / "source/crates/stark-score/src/cache.rs").read_bytes() != dirty.read_bytes()
    assert not (official / "source/crates/stark-score/src/local.rs").exists()


def test_changed_override_dependency_graph_is_rejected_before_compiler(project):
    (project["source"] / "Cargo.lock").write_text("changed dependency graph\n")
    with pytest.raises(dependency.DependencyError, match="source_dependency_mismatch"):
        build(project, checkout=project["source"])
    assert not calls(project)


def test_changed_override_cargo_config_is_rejected_before_compiler(project):
    (project["source"] / ".cargo/config.toml").write_text('[build]\nrustflags = ["--cfg=unreviewed"]\n')
    with pytest.raises(dependency.DependencyError):
        build(project, checkout=project["source"])
    assert not calls(project)


@pytest.mark.parametrize("location", ["crates/stark-score/src/cache.rs", "crates/stark-score/src/extra.rs"])
def test_override_cannot_snapshot_external_symlink(project, location):
    outside = project["root"] / "external.rs"
    outside.write_text("// outside repository\n")
    link = project["source"] / location
    if link.exists():
        link.unlink()
    link.symlink_to(outside)
    with pytest.raises(dependency.DependencyError, match="unsafe_source"):
        build(project, checkout=project["source"])
    assert not calls(project)


def test_committed_symlink_cannot_enter_official_source_archive(project):
    (project["source"] / "external.rs").symlink_to(project["root"] / "external.rs")
    revision = commit(project["source"], "Add unsafe synthetic symlink")
    with pytest.raises(dependency.DependencyError, match="unsafe_source"):
        dependency.derive_lock(project["source"], revision)


@pytest.mark.parametrize("phase", ["build", "test"])
def test_failed_explicit_update_preserves_previous_lock(project, phase):
    write(project["source"] / "crates/stark-score/src/cache.rs", "// next pinned cache revision\n")
    revision = commit(project["source"], "Update synthetic engine")
    before = project["lock_path"].read_bytes()
    set_state(project, fail=phase)
    output = project["root"] / "failed-update"
    with pytest.raises(dependency.DependencyError):
        dependency.update_dependency(project["lock_path"], revision, output=output)
    assert project["lock_path"].read_bytes() == before
    assert not (output / "BUILD_RECEIPT.json").exists()


def test_successful_explicit_update_publishes_new_pin_only_after_build_and_tests(project):
    write(project["source"] / "crates/stark-score/src/cache.rs", "// next pinned cache revision\n")
    revision = commit(project["source"], "Update synthetic engine")
    artifact = dependency.update_dependency(project["lock_path"], revision,
                                            output=project["root"] / "updated")
    selected = dependency.load_lock(project["lock_path"])
    assert selected["revision"] == revision
    assert selected["revision"] != project["revision"]
    receipt = dependency.verify_build(artifact, selected)
    assert receipt["source"]["revision"] == revision
    assert receipt["unit_tests"]["passed"] == 38
    assert receipt["native_acceptance_required"] is True
    with pytest.raises(dependency.DependencyError, match="build_lock_mismatch"):
        dependency.verify_build(artifact, project["lock"])


def test_update_cannot_overwrite_a_lock_changed_during_build(project, monkeypatch):
    write(project["source"] / "crates/stark-score/src/cache.rs", "// next pinned cache revision\n")
    revision = commit(project["source"], "Update synthetic engine")
    concurrent_bytes = project["lock_path"].read_bytes() + b"\n"
    actual_build = dependency.build_dependency

    def build_and_concurrently_change_lock(*args, **kwargs):
        result = actual_build(*args, **kwargs)
        project["lock_path"].write_bytes(concurrent_bytes)
        return result

    monkeypatch.setattr(dependency, "build_dependency", build_and_concurrently_change_lock)
    with pytest.raises(dependency.DependencyError, match="dependency_lock_changed_concurrently"):
        dependency.update_dependency(project["lock_path"], revision,
                                     output=project["root"] / "concurrent-update")
    assert project["lock_path"].read_bytes() == concurrent_bytes


def test_update_requires_explicit_full_revision_and_preserves_lock(project):
    before = project["lock_path"].read_bytes()
    with pytest.raises(dependency.DependencyError, match="revision_must_be_full_sha"):
        dependency.update_dependency(project["lock_path"], "main")
    assert project["lock_path"].read_bytes() == before
    assert not calls(project)


def test_source_package_version_cannot_silently_change_fixed_worker_protocol(project):
    manifest = project["source"] / "crates/stark-score/Cargo.toml"
    manifest.write_text(manifest.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
    revision = commit(project["source"], "Change synthetic protocol package version")
    with pytest.raises(dependency.DependencyError, match="unsupported_build_contract"):
        dependency.derive_lock(project["source"], revision)


def test_offline_build_uses_existing_verified_git_cache(project):
    build(project)
    artifact = build(project, "offline", offline=True)
    receipt = dependency.verify_build(artifact, project["lock"])
    assert receipt["source"]["revision"] == project["revision"]
    offline_calls = [call for call in calls(project)
                     if str(artifact / "source/Cargo.toml") in call["args"]]
    assert offline_calls
    assert all("--offline" in call["args"] for call in offline_calls)


@pytest.mark.parametrize("path", ["bin/stark-score", "bin/stark-score-digest",
                                  "source/crates/stark-score/src/cache.rs", "DEPENDENCY_LOCK.json"])
def test_modified_build_artifact_cannot_verify(project, path):
    artifact = build(project)
    modified = artifact / path
    modified.write_bytes(modified.read_bytes() + b" changed")
    with pytest.raises(dependency.DependencyError):
        dependency.verify_build(artifact, project["lock"])


def test_changed_compiler_cannot_verify_previous_build(project):
    artifact = build(project)
    set_state(project, version="1.98.2")
    with pytest.raises(dependency.DependencyError):
        dependency.verify_build(artifact, project["lock"])


@pytest.mark.parametrize("directory", ["source", "bin"])
def test_build_source_and_binary_directories_cannot_be_replaced_with_symlinks(project, directory):
    artifact = build(project)
    target = artifact.with_name("external-" + directory)
    (artifact / directory).rename(target)
    (artifact / directory).symlink_to(target, target_is_directory=True)
    with pytest.raises(dependency.DependencyError):
        dependency.verify_build(artifact, project["lock"])


def test_completed_build_is_not_overwritten(project):
    artifact = build(project)
    original = (artifact / "BUILD_RECEIPT.json").read_bytes()
    with pytest.raises(dependency.DependencyError, match="build_output_already_exists"):
        build(project)
    assert (artifact / "BUILD_RECEIPT.json").read_bytes() == original


def test_unsupported_host_is_rejected_before_fetch_or_compiler(project, monkeypatch):
    monkeypatch.setattr(dependency.platform, "system", lambda: "Linux")
    with pytest.raises(dependency.DependencyError, match="unsupported_build_host"):
        build(project)
    assert not (project["root"] / "official").exists()
    assert not calls(project)


def test_build_sanitizes_flags_wrappers_overrides_and_target_directory(project, monkeypatch):
    forbidden = {
        "RUSTFLAGS": "--cfg=injected", "CARGO_ENCODED_RUSTFLAGS": "--cfg=injected",
        "RUSTC": "/unreviewed/rustc",
        "RUSTC_WRAPPER": "/unreviewed/compiler-wrapper", "RUSTC_WORKSPACE_WRAPPER": "/unreviewed/workspace-wrapper",
        "CARGO_BUILD_RUSTFLAGS": "--cfg=injected", "CARGO_TARGET_DIR": str(project["root"] / "foreign-target"),
        "CARGO_HOME": str(project["root"] / "foreign-cargo"), "RUSTUP_TOOLCHAIN": "unreviewed",
        "GIT_CONFIG_GLOBAL": str(project["root"] / "foreign-git-config"), "DYLD_INSERT_LIBRARIES": "/unreviewed/library",
        "MEECP_STARK_CHECKOUT": str(project["root"] / "foreign-source"),
    }
    for name, value in forbidden.items():
        monkeypatch.setenv(name, value)
    artifact = build(project)
    compiler_calls = calls(project)
    assert any("build" in call["args"] for call in compiler_calls)
    for call in compiler_calls:
        for name, value in forbidden.items():
            assert call["env"].get(name) != value
    cargo_calls = [call for call in compiler_calls if call["name"] == "cargo"]
    for call in cargo_calls:
        assert Path(call["env"]["CARGO_HOME"]).is_relative_to(artifact)
        assert Path(call["env"]["CARGO_TARGET_DIR"]).is_relative_to(dependency.RUNTIME_ROOT)
        assert call["env"]["RUSTC"] == str(project["tools"] / "rustc")
    assert not (project["root"] / "foreign-target").exists()
    assert not (project["root"] / "foreign-cargo").exists()
