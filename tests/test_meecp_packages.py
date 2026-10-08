from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from meecp.contracts import Configuration, PackageError, PackageManifest, validate_package
from meecp.private import PrivateStateError, read_private_json, snapshot_package, write_private_json


def make_package(root: Path) -> Path:
    root.mkdir(mode=0o700)
    (root / "bin").mkdir()
    executable = root / "bin" / "service"
    executable.write_bytes(b"prepared executable fixture; validation never executes this")
    executable.chmod(0o700)
    (root / "LICENSE").write_text("MIT License\n")
    manifest = {
        "schema_version": 1, "host_api_version": 1, "package_id": "local.fixture", "version": "1.0.0",
        "source_revision": "sha256:" + "a" * 64, "name": "Fixture", "profile": "mcp-stdio",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "entrypoint": {"executable": "bin/service", "args": [], "cwd": "."},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "resources": {"memory_mb": 64, "cpu_only": True},
        "tools": [{"name": "validate_json", "description": "Validate synthetic JSON",
                   "input_schema": {"type": "object", "properties": {}}}],
        "licenses": ["LICENSE"],
    }
    (root / "meecp.package.json").write_text(json.dumps(manifest))
    relock(root)
    return root


def relock(root: Path) -> None:
    manifest = json.loads((root / "meecp.package.json").read_text())
    artifacts = []
    for name in ("LICENSE", "bin/service"):
        path = root / name
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        artifacts.append({"path": name, "sha256": digest, "size": len(content),
                          "executable": bool(path.stat().st_mode & 0o111),
                          "source": "prepared:sha256:" + digest, "provenance": "Fixture build v1",
                          "license": "LICENSE"})
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
            "manifest_sha256": hashlib.sha256((root / "meecp.package.json").read_bytes()).hexdigest(),
            "artifacts": artifacts, "build": {"recipe": "fixture-v1", "builder": "test-v1", "toolchain": "test-v1"}}
    (root / "meecp.lock.json").write_text(json.dumps(lock))


def edit_manifest(root: Path, **changes) -> None:
    path = root / "meecp.package.json"
    manifest = json.loads(path.read_text())
    manifest.update(changes)
    path.write_text(json.dumps(manifest))
    relock(root)


def test_validation_static_and_digest_bound(tmp_path, monkeypatch):
    root = make_package(tmp_path / "package")
    def forbid(*args, **kwargs):
        pytest.fail("validation must not execute package code")
    monkeypatch.setattr("subprocess.Popen", forbid)
    package = validate_package(root)
    assert package.manifest.entrypoint.executable == "bin/service"
    assert package.manifest.tools[0].mcp_schema()["inputSchema"] == {"type": "object", "properties": {}}
    digest = package.digest
    edit_manifest(root, name="Renamed")
    assert validate_package(root).digest != digest


def test_http_profile_requires_explicit_private_injection_contract(tmp_path):
    root = make_package(tmp_path / "package")
    edit_manifest(root, profile="mcp-http")
    with pytest.raises(PackageError, match="^http_configuration_required$"):
        validate_package(root)


@pytest.mark.parametrize("changes,code", [
    ({"schema_version": 2}, "unsupported_package_schema"),
    ({"schema_version": True}, "unsupported_package_schema"),
    ({"host_api_version": True}, "invalid_package_schema"),
    ({"source_revision": "main"}, "source_unresolved"),
    ({"install_hook": "private input"}, "invalid_package_schema"),
    ({"resources": {"memory_mb": 64, "cpu_only": True, "gpu": True}}, "invalid_resource_declaration"),
    ({"resources": {"memory_mb": 64, "cpu_only": 1}}, "invalid_package_schema"),
    ({"entrypoint": {"executable": "../private", "cwd": "."}}, "unsafe_package_path"),
    ({"entrypoint": {"executable": "LICENSE", "cwd": "."}}, "entrypoint_not_executable"),
    ({"protocol": {"version": "2025-11-25", "capabilities": ["resources"]}}, "invalid_package_schema"),
    ({"access": {"secrets": ["invalid-name"]}}, "invalid_package_schema"),
])
def test_rejected_schema_has_content_free_error(tmp_path, changes, code):
    root = make_package(tmp_path / "package")
    edit_manifest(root, **changes)
    with pytest.raises(PackageError) as caught:
        validate_package(root)
    assert caught.value.code == code
    assert str(caught.value) == code


@pytest.mark.parametrize("kind", ["unlocked", "empty_directory", "symlink", "hardlink", "fifo", "changed", "mode"])
def test_closed_artifact_tree(tmp_path, kind):
    root = make_package(tmp_path / "package")
    if kind == "unlocked":
        (root / "unexpected").write_text("private content")
    elif kind == "empty_directory":
        (root / "unused").mkdir()
    elif kind == "symlink":
        (root / "link").symlink_to(root / "LICENSE")
    elif kind == "hardlink":
        os.link(root / "LICENSE", tmp_path / "license-copy")
    elif kind == "fifo":
        os.mkfifo(root / "pipe")
    elif kind == "changed":
        (root / "LICENSE").write_text("XX License\n")
    else:
        (root / "bin/service").chmod(0o600)
    with pytest.raises(PackageError):
        validate_package(root)


def test_duplicate_json_and_lock_artifacts_rejected(tmp_path):
    root = make_package(tmp_path / "package")
    path = root / "meecp.lock.json"
    lock = json.loads(path.read_text())
    lock["artifacts"].append(lock["artifacts"][0])
    path.write_text(json.dumps(lock))
    with pytest.raises(PackageError, match="^duplicate_artifact$"):
        validate_package(root)
    path.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(PackageError, match="^invalid_package_json$"):
        validate_package(root)


@pytest.mark.parametrize("schema,code", [
    ({"type": "object", "properties": {"value": {"type": "nonexistent"}}}, "invalid_tool_schema"),
    ({"type": "object", "$ref": "https://example.invalid/schema"}, "external_schema_reference"),
    ({"type": "object", "$defs": {"nested": {"$dynamicRef": "file:///private"}}}, "external_schema_reference"),
])
def test_tool_schema_checks_never_fetch_references(tmp_path, schema, code):
    root = make_package(tmp_path / "package")
    edit_manifest(root, tools=[{"name": "fixture", "description": "Fixture", "input_schema": schema}])
    with pytest.raises(PackageError, match=f"^{code}$"):
        validate_package(root)


def test_mutation_during_verification_is_rejected(tmp_path, monkeypatch):
    import meecp.contracts as contracts
    root = make_package(tmp_path / "package")
    original = contracts._verify_artifact
    def mutate(path, artifact):
        original(path, artifact)
        if artifact.path == "bin/service":
            (root / "meecp.lock.json").write_text("{}")
    monkeypatch.setattr(contracts, "_verify_artifact", mutate)
    with pytest.raises(PackageError, match="^package_changed$"):
        validate_package(root)


def test_snapshot_is_independent_private_and_digest_checked(tmp_path):
    root = make_package(tmp_path / "source")
    source = validate_package(root)
    destination = tmp_path / "state" / source.digest
    snapshot = snapshot_package(source, destination)
    assert snapshot.digest == source.digest
    assert snapshot.root == destination
    assert (destination.stat().st_mode & 0o777) == 0o700
    assert ((destination / "LICENSE").stat().st_mode & 0o777) == 0o600
    assert ((destination / "bin/service").stat().st_mode & 0o777) == 0o700
    assert snapshot_package(source, destination).digest == snapshot.digest
    (root / "LICENSE").write_text("Changed user source")
    assert validate_package(destination).digest == snapshot.digest
    with pytest.raises(PackageError):
        snapshot_package(source, tmp_path / "state" / "different")


def test_private_json_rejects_unsafe_existing_files(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "connection.json"
    write_private_json(path, {"token": "synthetic"})
    assert read_private_json(path) == {"token": "synthetic"}
    path.chmod(0o644)
    with pytest.raises(PrivateStateError):
        read_private_json(path)
    with pytest.raises(PrivateStateError):
        write_private_json(path, {"token": "replacement"})
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    path.symlink_to(outside)
    with pytest.raises(PrivateStateError):
        write_private_json(path, {})
    assert outside.read_text() == "{}"


def test_new_private_ancestors_and_duplicate_json(tmp_path):
    from meecp.private import ensure_private_directory
    root = ensure_private_directory(tmp_path / "outer" / "inner")
    assert (root.parent.stat().st_mode & 0o777) == 0o700
    path = root / "state.json"
    path.write_text('{"token":"first","token":"second"}')
    path.chmod(0o600)
    with pytest.raises(PrivateStateError):
        read_private_json(path)


def test_optional_configuration_contract_and_logical_secrets(tmp_path):
    root = make_package(tmp_path / "package")
    old = validate_package(root)
    assert old.manifest.configuration.fields == {}
    assert old.manifest.access.secrets == []
    edit_manifest(root, configuration={"schema_version": 1, "fields": {
        "label": {"type": "string", "default": "fixture", "min_length": 1, "max_length": 20},
        "limit": {"type": "integer", "default": 3, "minimum": 1, "maximum": 8},
        "enabled": {"type": "boolean", "required": True, "enum": [True, False]},
    }}, access={"secrets": ["API_KEY", "secondary"]})
    prepared = validate_package(root)
    assert prepared.digest != old.digest
    assert prepared.manifest.configuration.fields["label"].default == "fixture"
    assert prepared.manifest.access.secrets == ["API_KEY", "secondary"]
    serialized = prepared.manifest.model_dump()
    assert "default" not in serialized["configuration"]["fields"]["enabled"]
    reparsed = PackageManifest.model_validate(serialized)
    assert reparsed.configuration == prepared.manifest.configuration
    assert Configuration.model_validate(reparsed.configuration.model_dump()) == reparsed.configuration


@pytest.mark.parametrize("declaration", [
    {"type": "object"}, {"type": "array"}, {"type": "number"}, {"type": "string", "$ref": "private"},
    {"type": "integer", "default": True}, {"type": "integer", "default": 1.0},
    {"type": "boolean", "default": 1}, {"type": "string", "default": None},
    {"type": "string", "default": "\x00"}, {"type": "string", "default": "\ud800"},
    {"type": "integer", "default": 2 ** 53 + 1}, {"type": "integer", "minimum": -(2 ** 53) - 1},
    {"type": "integer", "maximum": True}, {"type": "integer", "minimum": None},
    {"type": "string", "minimum": 1}, {"type": "boolean", "max_length": 2},
    {"type": "string", "min_length": -1}, {"type": "string", "max_length": 4097},
    {"type": "string", "min_length": 4, "max_length": 3},
    {"type": "integer", "minimum": 4, "maximum": 3},
    {"type": "integer", "default": 2, "minimum": 3},
    {"type": "string", "default": "x", "min_length": 2},
    {"type": "string", "enum": []}, {"type": "string", "enum": None},
    {"type": "string", "enum": ["x", "x"]}, {"type": "string", "enum": ["x", 1]},
    {"type": "integer", "enum": [True]}, {"type": "boolean", "enum": [1]},
    {"type": "integer", "enum": [1, 2], "maximum": 1},
    {"type": "string", "enum": ["x"], "default": "y"},
    {"type": "string", "enum": [str(index) for index in range(33)]},
    {"type": "boolean", "required": 1},
])
def test_configuration_declarations_reject_invalid_constraints(tmp_path, declaration):
    root = make_package(tmp_path / "package")
    edit_manifest(root, configuration={"schema_version": 1, "fields": {"setting": declaration}})
    with pytest.raises(PackageError, match="^invalid_package_schema$"):
        validate_package(root)


@pytest.mark.parametrize("configuration", [
    {"schema_version": True, "fields": {}}, {"schema_version": 2, "fields": {}},
    {"schema_version": 1, "fields": {}, "additionalProperties": True},
    {"fields": {"bad-name": {"type": "string"}}}, {"fields": {"_bad": {"type": "string"}}},
    {"fields": {"a" * 65: {"type": "string"}}},
    {"fields": {"name" + str(index): {"type": "string"} for index in range(33)}},
])
def test_configuration_schema_and_names_are_closed(tmp_path, configuration):
    root = make_package(tmp_path / "package")
    edit_manifest(root, configuration=configuration)
    with pytest.raises(PackageError, match="^invalid_package_schema$"):
        validate_package(root)


@pytest.mark.parametrize("names", [["x", "x"], ["bad-name"], ["x\x00"], [""], [1],
                                  ["name" + str(index) for index in range(17)]])
def test_secret_declarations_are_unique_logical_names(tmp_path, names):
    root = make_package(tmp_path / "package")
    edit_manifest(root, access={"secrets": names})
    with pytest.raises(PackageError, match="^invalid_package_schema$"):
        validate_package(root)
