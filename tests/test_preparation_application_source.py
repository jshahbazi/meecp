"""Actual bounded intake and snapshot mutation checks; selected code never runs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import pytest

from mcpme.imports import ImportError, ImportStore
from mcpme import preparation_application_source as intake
from mcpme.preparation_components import original_file_records, read_snapshot


def source_tree(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    (root / "worker").write_bytes(bytes.fromhex("cffaedfe") + b"\0bounded synthetic binary")
    (root / "worker").chmod(0o755)
    (root / "worker.input.schema.json").write_text('{"type":"object"}')
    (root / "LICENSE").write_text("Synthetic test license")
    return root


def test_automatic_binary_capture_retains_modes_and_survives_source_removal(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("intake invoked selected code"))
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(root, name="Example", intent="Expose synthetic JSON command", source_mode="automatic")
        assert draft["source"]["kind"] == "application_directory"
        assert draft["source"]["contract_version"] == 2
        worker = next(item for item in draft["source"]["file_metadata"] if item["path"] == "worker")
        assert worker == {"path": "worker", "kind": "binary", "executable": True}
        snapshot = Path(draft["source"]["snapshot_path"])
        assert stat.S_IMODE((snapshot / "worker").stat().st_mode) == 0o600
        expected = (root / "worker").read_bytes()
        shutil.rmtree(root)
        files = intake.read_application_snapshot(draft)
        assert files["worker"].data == expected and files["worker"].executable
        assert "bounded synthetic binary" not in repr(files["worker"])
        assert original_file_records(draft) == tuple(draft["source"]["files"])
        assert read_snapshot(draft).by_path()["worker"].data == expected
    with ImportStore(tmp_path / "state") as store:
        assert intake.read_application_snapshot(store.inspect(draft["draft_id"]))["worker"].data == expected


def test_automatic_text_directory_and_document_keep_exact_legacy_identity(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "worker.py").write_text("def calculate(value): return value\n")
    (root / "README.md").write_text("Synthetic readme")
    with ImportStore(tmp_path / "state") as store:
        for source in (root, root / "README.md"):
            legacy = store.create(source, name="Old")
            automatic = store.create(source, name="New", source_mode="automatic")
            for key in ("kind", "files", "digest", "revision", "bytes"):
                assert legacy["source"][key] == automatic["source"][key]
            assert "contract_version" not in automatic["source"]
        expected = hashlib.sha256(json.dumps(legacy["source"]["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert legacy["source"]["digest"] == expected


def test_default_intake_does_not_silently_change_legacy_binary_selection(tmp_path):
    root = source_tree(tmp_path)
    with ImportStore(tmp_path / "state") as store:
        legacy = store.create(root, name="Legacy")
        assert legacy["source"]["kind"] == "local_folder"
        assert "worker" not in [item["path"] for item in legacy["source"]["files"]]


def test_unknown_regular_payload_is_bound_in_automatic_mode(tmp_path):
    root = source_tree(tmp_path)
    (root / "unrecognized.payload").write_text("Bounded unknown source")
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(root, name="Unknown", source_mode="automatic")
        assert "unrecognized.payload" in intake.read_application_snapshot(draft)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "ancestor"])
def test_automatic_rejects_unsafe_source_entries(tmp_path, unsafe):
    root = source_tree(tmp_path)
    if unsafe == "symlink":
        (root / "alias").symlink_to(root / "worker")
    elif unsafe == "hardlink":
        os.link(root / "worker", root / "alias")
    elif unsafe == "fifo":
        os.mkfifo(root / "pipe")
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="unsafe_source"):
            store.create(root, name="Unsafe", source_mode="automatic")
        assert store.list() == []


@pytest.mark.parametrize("mutation", ["bytes", "extra", "mode", "metadata"])
def test_reader_rejects_snapshot_or_identity_mutation(tmp_path, mutation):
    root = source_tree(tmp_path)
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(root, name="Mutation", source_mode="automatic")
        snapshot = Path(draft["source"]["snapshot_path"])
        if mutation == "bytes":
            (snapshot / "worker").write_bytes(b"changed")
        elif mutation == "extra":
            (snapshot / "extra").write_bytes(b"unexpected")
        elif mutation == "mode":
            (snapshot / "worker").chmod(0o700)
        else:
            draft = copy.deepcopy(draft)
            draft["source"]["file_metadata"][0]["executable"] = True
        with pytest.raises(ImportError, match="source_identity_mismatch|unsafe_source_file"):
            intake.read_application_snapshot(draft)


def test_source_mode_change_during_copy_rejects_publication(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    original = intake._read_file
    reads = 0
    def changing(path, **kwargs):
        nonlocal reads
        result = original(path, **kwargs)
        if Path(path) == root / "worker":
            reads += 1
            if reads == 1:
                (root / "worker").chmod(0o644)
        return result
    monkeypatch.setattr(intake, "_read_file", changing)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="source_changed"):
            store.create(root, name="Changed", source_mode="automatic")
        assert store.list() == []


def test_binary_credential_material_is_rejected_without_persisting_it(tmp_path):
    root = source_tree(tmp_path)
    (root / "worker").write_bytes(b"\0" + b"sk-" + b"x" * 32)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="source_credentials_detected"):
            store.create(root, name="Credentials", source_mode="automatic")
        assert store.list() == []


def test_binary_limits_apply_before_snapshot_publication(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    monkeypatch.setattr(intake, "MAX_SOURCE_FILE_BYTES", 8)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="source_size_exceeded"):
            store.create(root, name="Large", source_mode="automatic")
        assert store.list() == []


def test_explicit_binary_selection_binds_only_selected_records(tmp_path):
    root = source_tree(tmp_path)
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(root, name="Selected", files=["worker"], source_mode="automatic")
        assert list(intake.read_application_snapshot(draft)) == ["worker"]


def test_entry_limit_stops_streaming_enumeration_before_full_listing(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    for index in range(12):
        (root / ("entry" + str(index))).write_bytes(b"bounded")
    monkeypatch.setattr(intake, "MAX_SCANNED_ENTRIES", 8)
    monkeypatch.setattr(intake.os, "listdir", lambda *args: pytest.fail("unbounded directory listing"))
    with pytest.raises(ImportError, match="source_size_exceeded"):
        intake._scan(root)


def test_same_bytes_replacement_between_capture_reads_is_rejected(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    original = intake._read_file
    replaced = False
    def changing(path, **kwargs):
        nonlocal replaced
        result = original(path, **kwargs)
        if Path(path) == root / "worker" and not replaced:
            replaced = True
            replacement = root / "replacement"
            replacement.write_bytes(result[0])
            replacement.chmod(0o755)
            os.replace(replacement, root / "worker")
        return result
    monkeypatch.setattr(intake, "_read_file", changing)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="source_changed"):
            store.create(root, name="Replaced", source_mode="automatic")
        assert store.list() == []


def test_nested_directory_limit_is_a_fixed_error(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    nested = root
    for _ in range(12):
        nested = nested / "nested"
        nested.mkdir()
    monkeypatch.setattr(intake, "MAX_SOURCE_DEPTH", 8)
    with pytest.raises(ImportError, match="source_size_exceeded"):
        intake._scan(root)
    with ImportStore(tmp_path / "state") as store:
        with pytest.raises(ImportError, match="source_size_exceeded"):
            store.create(root, name="Deep", source_mode="automatic")
        assert store.list() == []


def test_automatic_copy_does_not_hold_sqlite_transaction(tmp_path, monkeypatch):
    root = source_tree(tmp_path)
    with ImportStore(tmp_path / "state") as store:
        original = intake._read_file
        reads = []
        def observing(path, **kwargs):
            reads.append(Path(path).name)
            assert not store._connection.in_transaction
            return original(path, **kwargs)
        monkeypatch.setattr(intake, "_read_file", observing)
        draft = store.create(root, name="Off transaction", source_mode="automatic")
        assert reads and draft["source"]["kind"] == "application_directory"


def test_python_inspection_keeps_binary_assets_and_unknown_payload_bound(tmp_path, monkeypatch):
    from mcpme.preparation_inspection import inspect_source
    root = tmp_path / "source"
    root.mkdir()
    (root / "worker.py").write_text("def calculate(label: str) -> dict[str, bool]:\n    return {'ok': True}\n")
    (root / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\n\0synthetic asset")
    (root / "source.metadata").write_text("Unknown regular payload")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("inspection executed source"))
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(root, name="Mixed Python", source_mode="automatic")
        assert draft["source"]["kind"] == "application_directory"
        inspection = inspect_source(draft)
        candidate, = inspection["candidates"]
        assert candidate["kind"] == "python-callable" and candidate["function"] == "calculate"
        assert inspection["source_revision"] == draft["source"]["revision"]
        assert {item.path for item in read_snapshot(draft).files} == {"worker.py", "asset.png", "source.metadata"}
        shutil.rmtree(root)
        assert inspect_source(draft) == inspection
