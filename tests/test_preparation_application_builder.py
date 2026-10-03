"""Actual mixed snapshot copies, mutation fences and explicit builder dispatch."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import shutil
import stat

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_application_builder import copy_application_snapshot
from mcpme import preparation_application_builder as adapter
from mcpme.preparation_application_source import read_application_snapshot
from mcpme.preparation_builder import prepare_bound
from mcpme.preparation_contracts import PreparationError
from test_preparation_builder import (
    acquire, draft_and_descriptor, stub_runner, synthetic_license_pins, toolchain,
)


@pytest.fixture
def mixed_draft(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "worker.py").write_text("raise RuntimeError('source executed while copying')\n")
    (source / "LICENSE").write_text("Synthetic retained source license\n")
    (source / "images").mkdir()
    (source / "images/asset.png").write_bytes(b"\x89PNG\0bounded synthetic image")
    (source / "unknown.payload").write_bytes(b"opaque\0source payload")
    (source / "unknown.payload").chmod(0o755)
    with ImportStore(tmp_path / "state") as store:
        draft = store.create(source, name="Mixed source", intent="Expose synthetic callable",
            source_mode="automatic")
        yield draft, source


def private_parent(tmp_path):
    parent = tmp_path / "builder"
    parent.mkdir(mode=0o700)
    return parent


def test_full_binary_and_unknown_closure_survives_original_removal(mixed_draft, tmp_path, monkeypatch):
    draft, source = mixed_draft
    expected = read_application_snapshot(draft)
    shutil.rmtree(source)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("copy executed source"))
    destination = private_parent(tmp_path) / "source"
    records = copy_application_snapshot(draft, destination)
    assert records == draft["source"]["files"]
    assert {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()} == set(expected)
    for item in expected.values():
        target = destination / item.path
        assert target.read_bytes() == item.data
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item.sha256
    assert expected["unknown.payload"].executable
    assert stat.S_IMODE((destination / "images").stat().st_mode) == 0o700


def test_invalid_v2_identity_is_rejected_before_destination_creation(mixed_draft, tmp_path):
    draft, _ = mixed_draft
    changed = copy.deepcopy(draft)
    changed["source"]["file_metadata"][0]["executable"] = not changed["source"]["file_metadata"][0]["executable"]
    destination = private_parent(tmp_path) / "source"
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        copy_application_snapshot(changed, destination)
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["bytes", "extra", "fifo", "source_parent"])
def test_copy_rechecks_snapshot_after_actual_writes(mixed_draft, tmp_path, monkeypatch, mutation):
    draft, _ = mixed_draft
    snapshot = Path(draft["source"]["snapshot_path"])
    original = adapter._write_file
    changed = False
    def write(*args):
        nonlocal changed
        original(*args)
        if changed:
            return
        changed = True
        if mutation == "bytes":
            (snapshot / "unknown.payload").write_bytes(b"changed")
        elif mutation == "extra":
            (snapshot / "extra").write_bytes(b"unbound")
            (snapshot / "extra").chmod(0o600)
        elif mutation == "fifo":
            (snapshot / "unknown.payload").unlink()
            os.mkfifo(snapshot / "unknown.payload", 0o600)
        else:
            moved = snapshot.with_name(snapshot.name + "-moved")
            snapshot.rename(moved)
            snapshot.symlink_to(moved, target_is_directory=True)
    monkeypatch.setattr(adapter, "_write_file", write)
    with pytest.raises(PreparationError, match="source_identity_mismatch|unsafe_source"):
        copy_application_snapshot(draft, private_parent(tmp_path) / "source")
    assert changed


def test_reader_and_copy_obey_per_file_cancellation(mixed_draft, tmp_path, monkeypatch):
    draft, _ = mixed_draft
    observed = 0
    def cancel_during_validation():
        nonlocal observed
        observed += 1
        if observed == 3:
            raise PreparationError("preparation_canceled")
    destination = private_parent(tmp_path) / "source"
    with pytest.raises(PreparationError, match="preparation_canceled"):
        copy_application_snapshot(draft, destination, cancel_during_validation)
    assert observed == 3 and not destination.exists()

    original = adapter._write_file
    copied = []
    def write(*args):
        original(*args)
        copied.append(args[1])
    monkeypatch.setattr(adapter, "_write_file", write)
    def cancel_after_first_copy():
        if copied:
            raise PreparationError("preparation_canceled")
    with pytest.raises(PreparationError, match="preparation_canceled"):
        copy_application_snapshot(draft, destination, cancel_after_first_copy)
    assert len(copied) == 1


@pytest.mark.parametrize("mutation", ["nested_directory", "bytes", "extra", "mode"])
def test_final_destination_membership_bytes_and_modes_are_rechecked(mixed_draft, tmp_path, monkeypatch, mutation):
    draft, _ = mixed_draft
    destination = private_parent(tmp_path) / "source"
    original = adapter._write_file
    changed = False
    def write(*args):
        nonlocal changed
        original(*args)
        if changed or args[1] != "images/asset.png":
            return
        changed = True
        if mutation == "nested_directory":
            (destination / "images").rename(destination.parent / "substituted")
            (destination / "images").mkdir(mode=0o700)
        elif mutation == "bytes":
            (destination / "images/asset.png").write_bytes(b"changed")
        elif mutation == "extra":
            (destination / "images/extra").write_bytes(b"unbound")
            (destination / "images/extra").chmod(0o600)
        else:
            (destination / "images/asset.png").chmod(0o700)
    monkeypatch.setattr(adapter, "_write_file", write)
    with pytest.raises(PreparationError, match="source_identity_mismatch|unsafe_source"):
        copy_application_snapshot(draft, destination)
    assert changed


def test_copy_refuses_existing_destination_and_symlink_ancestor(mixed_draft, tmp_path):
    draft, _ = mixed_draft
    parent = private_parent(tmp_path)
    destination = parent / "source"
    destination.mkdir(mode=0o700)
    with pytest.raises(PreparationError, match="unsafe_source_file"):
        copy_application_snapshot(draft, destination)
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    with pytest.raises(PreparationError, match="unsafe_source_file"):
        copy_application_snapshot(draft, alias / "other")


def test_prepare_bound_selects_verified_application_adapter_without_changing_template(tmp_path, monkeypatch):
    private = toolchain(tmp_path / "host")
    legacy, value = draft_and_descriptor(tmp_path)
    original = Path(legacy["source"]["snapshot_path"])
    (original / "asset.png").write_bytes(b"\x89PNG\0synthetic image")
    (original / "unknown.payload").write_bytes(b"bounded\0unknown closure")
    with ImportStore(tmp_path / "imports") as store:
        draft = store.create(original, name="Mixed", intent="Expose synthetic callable", source_mode="automatic")
        draft["revision"] = legacy["revision"]
        expected = read_application_snapshot(draft)
        before = (private.root / "templates/prepare.py").read_bytes()
        calls = []
        monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("builder executed source directly"))
        package = prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner(calls), lambda: None,
            acquire_wheels=acquire)
        assert (private.root / "templates/prepare.py").read_bytes() == before
        assert {item["path"] for item in draft["source"]["files"]} == set(expected)
        for item in expected.values():
            assert (package.root / "source" / item.path).read_bytes() == item.data
        assert len(calls) == 5


def test_legacy_builder_keeps_published_v1_snapshot_identity(tmp_path):
    private = toolchain(tmp_path / "host")
    draft, value = draft_and_descriptor(tmp_path)
    package = prepare_bound(value, draft, private, tmp_path / "candidate", stub_runner([]), lambda: None,
        acquire_wheels=acquire)
    import json
    receipt = json.loads((package.root / "preparation.json").read_bytes())
    assert receipt["source_revision"] == draft["source"]["revision"]
    assert receipt["files"] == draft["source"]["files"]
