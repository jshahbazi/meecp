"""The generic Pi lock helper seals custom artifacts for the real host contract."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mcpme.contracts import PackageError, validate_package

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_package_helper", ROOT / "templates/pi_package.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def package(tmp_path):
    root = tmp_path / "package"
    root.mkdir(mode=0o700)
    (root / "bin").mkdir(mode=0o700)
    (root / "bin/service").write_bytes(b"synthetic executable; never run\n")
    (root / "bin/service").chmod(0o700)
    (root / "LICENSE").write_text("Synthetic source license\n")
    (root / "DEPENDENCIES.md").write_text("Synthetic dependency notices\n")
    (root / "verification.json").write_text("[]\n")
    manifest = {"schema_version": 1, "host_api_version": 1, "package_id": "local.pi-test", "version": "1.0.0",
        "source_revision": "sha256:" + "a" * 64, "name": "Pi fixture", "profile": "mcp-stdio",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "entrypoint": {"executable": "bin/service", "args": [], "cwd": "."},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "resources": {"memory_mb": 64, "cpu_only": True},
        "tools": [{"name": "synthetic", "description": "Synthetic fixture tool",
            "input_schema": {"type": "object", "properties": {}}}],
        "licenses": ["LICENSE", "DEPENDENCIES.md"]}
    (root / "mcpme.package.json").write_text(json.dumps(manifest) + "\n")
    return root


def test_seals_complete_tree_with_actual_host_schema(tmp_path, monkeypatch):
    root = package(tmp_path)
    def forbid(*_, **__):
        pytest.fail("lock sealing must not execute package code")
    monkeypatch.setattr(subprocess, "Popen", forbid)
    metadata = tmp_path / "dependency-metadata.json"
    metadata.write_text(json.dumps({"schema_version": 1, "ecosystem": "python", "versions": []}))
    lock = helper.seal(root, primary_license="DEPENDENCIES.md", toolchain_metadata=metadata)
    actual = validate_package(root)
    assert actual.lock.build.recipe == "pi-setup-v1"
    assert actual.lock.build.builder == "pi-sdk"
    assert actual.lock.build.toolchain == "recorded-metadata:sha256:" + hashlib.sha256(metadata.read_bytes()).hexdigest()
    assert {entry["path"] for entry in lock["artifacts"]} == {"LICENSE", "DEPENDENCIES.md", "verification.json", "bin/service"}
    assert all(entry["license"] == "DEPENDENCIES.md" for entry in lock["artifacts"])
    assert all(entry["source"] == "prepared:sha256:" + entry["sha256"] for entry in lock["artifacts"])
    assert next(entry for entry in lock["artifacts"] if entry["path"] == "bin/service")["executable"] is True
    assert root.joinpath("mcpme.lock.json").stat().st_mode & 0o777 == 0o600


def test_relocking_is_deterministic_and_binds_changed_bytes_modes_and_manifest(tmp_path):
    root = package(tmp_path)
    helper.seal(root)
    original = (root / "mcpme.lock.json").read_bytes()
    original_digest = validate_package(root).digest
    helper.seal(root)
    assert (root / "mcpme.lock.json").read_bytes() == original
    (root / "verification.json").write_text("[{}]\n")
    with pytest.raises(PackageError, match="artifact_hash_mismatch|artifact_size_mismatch|package_changed"):
        validate_package(root)
    manifest = json.loads((root / "mcpme.package.json").read_text())
    manifest["name"] = "Updated fixture"
    (root / "mcpme.package.json").write_text(json.dumps(manifest))
    (root / "DEPENDENCIES.md").chmod(0o700)
    helper.seal(root)
    assert validate_package(root).digest != original_digest
    lock = json.loads((root / "mcpme.lock.json").read_text())
    assert next(entry for entry in lock["artifacts"] if entry["path"] == "DEPENDENCIES.md")["executable"] is True
    assert not list(root.glob(".pi-lock-*"))


def test_detects_artifact_mutation_before_publishing_lock(tmp_path, monkeypatch):
    root = package(tmp_path)
    original = helper._read
    def mutate(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == "verification.json":
            path.write_text("private mutation marker\n")
        return result
    monkeypatch.setattr(helper, "_read", mutate)
    with pytest.raises(helper.LockError, match="^pi_lock_changed$"):
        helper.seal(root)
    assert not (root / "mcpme.lock.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "writable", "special_mode", "empty_directory", "directory_writable", "lock_symlink"])
def test_rejects_unsafe_tree_without_changing_existing_lock(tmp_path, kind):
    root = package(tmp_path)
    helper.seal(root)
    old = (root / "mcpme.lock.json").read_bytes()
    expected = "pi_lock_unsafe_file"
    if kind == "symlink":
        (root / "link").symlink_to(root / "LICENSE")
    elif kind == "hardlink":
        os.link(root / "LICENSE", tmp_path / "outside-license")
    elif kind == "fifo":
        os.mkfifo(root / "pipe")
    elif kind == "writable":
        (root / "LICENSE").chmod(0o666)
    elif kind == "special_mode":
        (root / "bin/service").chmod(0o4700)
    elif kind == "directory_writable":
        (root / "bin").chmod(0o777)
    elif kind == "lock_symlink":
        (root / "mcpme.lock.json").unlink()
        target = tmp_path / "external-lock.json"
        target.write_bytes(old)
        (root / "mcpme.lock.json").symlink_to(target)
    else:
        (root / "unused").mkdir()
        expected = "pi_lock_empty_directory"
    with pytest.raises(helper.LockError, match="^" + expected + "$"):
        helper.seal(root)
    assert (root / "mcpme.lock.json").read_bytes() == old


@pytest.mark.parametrize("change", ["missing", "empty", "undeclared", "duplicate_json", "traversal"])
def test_requires_declared_nonempty_licenses_and_closed_metadata(tmp_path, change):
    root = package(tmp_path)
    primary = None
    if change == "missing":
        (root / "LICENSE").unlink()
    elif change == "empty":
        (root / "LICENSE").write_text("")
    elif change == "undeclared":
        primary = "verification.json"
    elif change == "duplicate_json":
        file = root / "mcpme.package.json"
        file.write_text(file.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
    else:
        primary = "../LICENSE"
    with pytest.raises(helper.LockError):
        helper.seal(root, primary_license=primary)
    assert not (root / "mcpme.lock.json").exists()


def test_cli_uses_stdlib_and_emits_fixed_codes_without_input_or_exception_text(tmp_path):
    root = package(tmp_path)
    command = [sys.executable, "-I", "-S", str(ROOT / "templates/pi_package.py")]
    success = subprocess.run(command + [str(root)], capture_output=True, text=True)
    assert success.returncode == 0
    assert json.loads(success.stdout) == {"success": True, "code": "pi_lock_ready"}
    assert success.stderr == ""
    invalid = subprocess.run(command + [str(root), "--private-option", "private-marker"], capture_output=True, text=True)
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout) == {"success": False, "code": "pi_lock_arguments_invalid"}
    assert invalid.stderr == ""
    (root / "mcpme.package.json").write_text("private-invalid-json-marker")
    failed = subprocess.run(command + [str(root)], capture_output=True, text=True)
    assert failed.returncode == 1
    assert json.loads(failed.stdout) == {"success": False, "code": "pi_lock_failed"}
    assert failed.stderr == ""
