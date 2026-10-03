from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import pytest

from mcpme.contracts import validate_package
from mcpme.settings import (MAX_CONFIGURATION_BYTES, SecretStore, SettingsError,
                            normalize_settings, settings_digest, validate_settings_choices)
from test_mcpme_packages import edit_manifest, make_package


def envelope(configuration=None, secret_references=None):
    return {"schema_version": 1, "configuration": configuration or {}, "secret_references": secret_references or {}}


def configured_manifest(tmp_path, fields=None, secrets=None):
    root = make_package(tmp_path / "package")
    edit_manifest(root, configuration={"schema_version": 1, "fields": fields or {}},
                  access={"secrets": secrets or []})
    return validate_package(root).manifest


def test_old_package_normalizes_to_empty_settings(tmp_path):
    manifest = validate_package(make_package(tmp_path / "package")).manifest
    assert normalize_settings(manifest) == envelope()
    assert settings_digest(envelope()) == settings_digest({"secret_references": {}, "configuration": {}})


def test_effective_defaults_required_values_and_digest(tmp_path):
    manifest = configured_manifest(tmp_path, {
        "title": {"type": "string", "default": "fixture", "max_length": 8},
        "count": {"type": "integer", "required": True, "minimum": 1, "maximum": 3},
        "enabled": {"type": "boolean", "default": False},
        "optional": {"type": "string"},
    }, ["API_KEY"])
    reference = str(uuid.uuid4())
    selected = envelope({"count": 2}, {"API_KEY": reference})
    normalized = normalize_settings(manifest, selected)
    assert normalized == envelope({"title": "fixture", "count": 2, "enabled": False}, {"API_KEY": reference})
    assert selected["configuration"] == {"count": 2}
    assert settings_digest(normalized) != settings_digest(envelope({"title": "fixture", "count": 3,
                                                                   "enabled": False}, {"API_KEY": reference}))
    with pytest.raises(SettingsError, match="^configuration_required$"):
        normalize_settings(manifest, envelope(secret_references={"API_KEY": reference}))


@pytest.mark.parametrize("declaration,value", [
    ({"type": "integer"}, True), ({"type": "integer"}, 1.0),
    ({"type": "boolean"}, 1), ({"type": "string"}, 1),
    ({"type": "integer", "minimum": 2}, 1), ({"type": "integer", "maximum": 2}, 3),
    ({"type": "string", "min_length": 2}, "x"), ({"type": "string", "max_length": 2}, "xxx"),
    ({"type": "string", "enum": ["x", "y"]}, "z"),
    ({"type": "integer", "enum": [1, 2]}, 3), ({"type": "boolean", "enum": [True]}, False),
])
def test_selected_values_follow_declared_constraints(tmp_path, declaration, value):
    manifest = configured_manifest(tmp_path, {"setting": declaration})
    with pytest.raises(SettingsError, match="^invalid_settings$"):
        normalize_settings(manifest, envelope({"setting": value}))


def test_unknown_settings_and_secret_names_rejected(tmp_path):
    manifest = configured_manifest(tmp_path, {"known": {"type": "boolean"}}, ["API_KEY"])
    reference = str(uuid.uuid4())
    with pytest.raises(SettingsError, match="^invalid_settings$"):
        normalize_settings(manifest, envelope({"unknown": True}, {"API_KEY": reference}))
    for refs in ({}, {"other": reference}, {"API_KEY": reference, "other": reference}):
        with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
            normalize_settings(manifest, envelope(secret_references=refs))


@pytest.mark.parametrize("payload", [
    {}, [], {"configuration": {}}, {"secret_references": {}},
    {"configuration": {}, "secret_references": {}, "value": "private"},
    {"schema_version": True, "configuration": {}, "secret_references": {}},
    {"schema_version": 2, "configuration": {}, "secret_references": {}},
    {"configuration": [], "secret_references": {}},
    {"configuration": {"x": None}, "secret_references": {}},
    {"configuration": {"x": []}, "secret_references": {}},
    {"configuration": {"x": {}}, "secret_references": {}},
    {"configuration": {"x": 1.0}, "secret_references": {}},
    {"configuration": {"x": float("inf")}, "secret_references": {}},
    {"configuration": {"x": 2 ** 53 + 1}, "secret_references": {}},
    {"configuration": {"x": -(2 ** 53) - 1}, "secret_references": {}},
    {"configuration": {"x": "\x00"}, "secret_references": {}},
    {"configuration": {"x": "\ud800"}, "secret_references": {}},
    {"configuration": {"x": "x" * 4097}, "secret_references": {}},
    {"configuration": {"bad-name": True}, "secret_references": {}},
    {"configuration": {"n" + str(index): True for index in range(33)}, "secret_references": {}},
    {"configuration": {}, "secret_references": []},
    {"configuration": {}, "secret_references": {"bad-name": str(uuid.uuid4())}},
    {"configuration": {}, "secret_references": {"API_KEY": "secret material"}},
    {"configuration": {}, "secret_references": {"API_KEY": str(uuid.uuid4()).upper()}},
    {"configuration": {}, "secret_references": {"n" + str(index): str(uuid.uuid4()) for index in range(17)}},
])
def test_unbound_settings_are_closed_bounded_and_strict(payload):
    with pytest.raises(SettingsError) as caught:
        validate_settings_choices(payload)
    assert caught.value.code == str(caught.value) == "invalid_settings"


def test_encoded_settings_size_bound_applies_after_defaults(tmp_path):
    values = {"field" + str(index): "x" * 4096 for index in range(5)}
    assert len(json.dumps(values).encode()) > MAX_CONFIGURATION_BYTES
    with pytest.raises(SettingsError, match="^invalid_settings$"):
        validate_settings_choices(envelope(values))
    manifest = configured_manifest(tmp_path, {name: {"type": "string", "default": value}
                                               for name, value in values.items()})
    with pytest.raises(SettingsError, match="^invalid_settings$"):
        normalize_settings(manifest)


def test_secret_metadata_resolution_revocation_and_restart(tmp_path, caplog, capsys):
    root = tmp_path / "state"
    value = "synthetic-secret-" + str(uuid.uuid4())
    store = SecretStore(root)
    first = store.create("Fixture key", value)
    second = store.create("Fixture key", "replacement-fixture")
    assert first["secret_id"] != second["secret_id"]
    assert set(first) == {"secret_id", "name", "status", "created_at"}
    assert first["status"] == "active"
    assert store.get(first["secret_id"]) == first
    assert store.count() == 2
    assert store.resolve({"API_KEY": first["secret_id"]}) == {"API_KEY": value}
    assert value not in json.dumps(store.list())
    for directory in (root, root / "secrets", store.records):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert (store.records / (first["secret_id"] + ".json")).stat().st_mode & 0o777 == 0o600
    recovered = SecretStore(root)
    assert recovered.resolve({"API_KEY": first["secret_id"]}) == {"API_KEY": value}
    revoked = recovered.revoke(first["secret_id"])
    assert revoked["status"] == "revoked"
    assert recovered.revoke(first["secret_id"]) == revoked
    persisted = json.loads((store.records / (first["secret_id"] + ".json")).read_text())
    assert "value" not in persisted and value not in json.dumps(persisted)
    final = SecretStore(root)
    assert final.get(first["secret_id"])["status"] == "revoked"
    assert final.count() == 2
    assert final.resolve({"OTHER": second["secret_id"]}) == {"OTHER": "replacement-fixture"}
    with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
        final.resolve({"API_KEY": first["secret_id"]})
    assert value not in caplog.text
    captured = capsys.readouterr()
    assert value not in captured.out + captured.err


@pytest.mark.parametrize("name,value", [
    ("", "x"), ("x" * 65, "x"), ("x\n", "x"), ("x\x7f", "x"), ("x\u0080", "x"),
    ("x\ud800", "x"), (1, "x"), ("x", ""), ("x", "\x00"), ("x", "\ud800"),
    ("x", "x" * 4097), ("x", "\U0001f600" * 1025), ("x", 1),
])
def test_secret_input_errors_never_echo_material(tmp_path, name, value):
    store = SecretStore(tmp_path / "state")
    with pytest.raises(SettingsError) as caught:
        store.create(name, value)
    assert caught.value.code == str(caught.value) == "invalid_secret"
    assert store.count() == 0


def test_unicode_secret_byte_boundary_is_retained(tmp_path):
    store = SecretStore(tmp_path / "state")
    value = "\U0001f600" * 1024
    metadata = store.create("Unicode fixture", value)
    assert SecretStore(tmp_path / "state").resolve({"KEY": metadata["secret_id"]}) == {"KEY": value}


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink", "fifo", "directory", "invalid_json", "duplicate"])
def test_unsafe_secret_record_fails_closed(tmp_path, kind):
    root = tmp_path / "state"
    store = SecretStore(root)
    item = store.create("fixture", "private-fixture")
    path = store.records / (item["secret_id"] + ".json")
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        content = path.read_bytes()
        path.unlink()
        outside = tmp_path / "outside.json"
        outside.write_bytes(content)
        outside.chmod(0o600)
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(path, tmp_path / "duplicate.json")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif kind == "invalid_json":
        path.write_text("{private-fixture")
    elif kind == "duplicate":
        path.write_text('{"value":"private-fixture","value":"other"}')
    with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
        store.resolve({"KEY": item["secret_id"]})
    with pytest.raises(SettingsError, match="^secrets_unavailable$"):
        SecretStore(root)


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"secret_id": str(uuid.uuid4())},
    {"name": "x\n"}, {"status": "unknown"}, {"status": []}, {"status": "revoked"},
    {"created_at": True}, {"created_at": -1}, {"created_at": float("nan")},
    {"created_at": 10 ** 400}, {"value": None}, {"value": "x\x00"}, {"extra": "private-fixture"},
])
def test_persisted_secret_schema_is_strict(tmp_path, changes):
    root = tmp_path / "state"
    store = SecretStore(root)
    item = store.create("fixture", "private-fixture")
    path = store.records / (item["secret_id"] + ".json")
    record = json.loads(path.read_text())
    record.update(changes)
    path.write_text(json.dumps(record))
    with pytest.raises(SettingsError, match="^secrets_unavailable$"):
        SecretStore(root)
    with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
        store.resolve({"KEY": item["secret_id"]})


def test_missing_reference_errors_and_record_identity_changes(tmp_path, monkeypatch):
    store = SecretStore(tmp_path / "state")
    missing = str(uuid.uuid4())
    for method in (store.get, store.revoke):
        with pytest.raises(SettingsError, match="^secret_not_found$"):
            method(missing)
    with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
        store.resolve({"KEY": missing})
    item = store.create("fixture", "private-fixture")
    path = store.records / (item["secret_id"] + ".json")
    original = Path.lstat
    changed = False
    def replace_during_read(candidate, *args, **kwargs):
        nonlocal changed
        if candidate == path and not changed:
            changed = True
            content = candidate.read_bytes()
            candidate.unlink()
            candidate.write_bytes(content)
            candidate.chmod(0o600)
        return original(candidate, *args, **kwargs)
    # The first leaf check is deliberately replaced; the descriptor then safely
    # reads the new leaf. A second replacement during the post-read identity
    # check must reject the changed file.
    checks = 0
    def replace_after_open(candidate, *args, **kwargs):
        nonlocal checks
        if candidate == path:
            checks += 1
            if checks == 2:
                return replace_during_read(candidate, *args, **kwargs)
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", replace_after_open)
    with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
        store.resolve({"KEY": item["secret_id"]})


def test_safe_atomic_write_leftovers_are_reclaimed_but_unknown_files_retained(tmp_path):
    root = tmp_path / "state"
    store = SecretStore(root)
    temporary = store.records / (str(uuid.uuid4()) + ".json." + "a" * 24 + ".tmp")
    temporary.write_text("unpublished synthetic secret")
    temporary.chmod(0o600)
    assert SecretStore(root).count() == 0
    assert not temporary.exists()
    unknown = store.records / "unknown"
    unknown.write_text("unrecognized synthetic secret")
    unknown.chmod(0o600)
    with pytest.raises(SettingsError, match="^secrets_unavailable$"):
        SecretStore(root)
    assert unknown.read_text() == "unrecognized synthetic secret"


def test_secret_capacity_metadata_page_and_immutable_values(tmp_path, monkeypatch):
    import mcpme.settings as settings
    store = SecretStore(tmp_path / "state")
    items = [store.create("fixture", "synthetic") for _ in range(102)]
    assert store.count() == 102
    assert len(store.list()) == 100
    monkeypatch.setattr(settings, "MAX_SECRETS", 102)
    with pytest.raises(SettingsError, match="^secret_limit$"):
        store.create("fixture", "new")
    store.revoke(items[0]["secret_id"])
    with pytest.raises(SettingsError, match="^secret_limit$"):
        store.create("fixture", "new")
    monkeypatch.setattr(settings, "MAX_SECRETS", 4096)
    monkeypatch.setattr(settings.uuid, "uuid4", lambda: uuid.UUID(items[1]["secret_id"]))
    with pytest.raises(SettingsError, match="^secrets_unavailable$"):
        store.create("fixture", "replacement")
    assert store.resolve({"KEY": items[1]["secret_id"]}) == {"KEY": "synthetic"}


def test_secret_record_write_failure_and_revocation_failure_are_content_free(tmp_path, monkeypatch):
    import mcpme.settings as settings
    from mcpme.private import PrivateStateError
    store = SecretStore(tmp_path / "state")
    item = store.create("fixture", "private-fixture")
    def failed(*args, **kwargs):
        raise PrivateStateError()
    monkeypatch.setattr(settings, "write_private_json", failed)
    for call in (lambda: store.create("new", "private-fixture"), lambda: store.revoke(item["secret_id"])):
        with pytest.raises(SettingsError) as caught:
            call()
        assert caught.value.code == str(caught.value) == "secrets_unavailable"
    assert store.resolve({"KEY": item["secret_id"]}) == {"KEY": "private-fixture"}


def test_directory_safety_is_rechecked_without_recreating_missing_state(tmp_path):
    root = tmp_path / "state"
    store = SecretStore(root)
    item = store.create("fixture", "private-fixture")
    store.records.chmod(0o755)
    with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
        store.resolve({"KEY": item["secret_id"]})
    store.records.chmod(0o700)
    (store.records / (item["secret_id"] + ".json")).unlink()
    store.records.rmdir()
    with pytest.raises(SettingsError, match="^secrets_unavailable$"):
        store.get(item["secret_id"])
    assert not store.records.exists()
