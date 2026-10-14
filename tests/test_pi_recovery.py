"""Bounded checkpoints copy generated files without replaying setup state."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import stat
import threading

import pytest

from meecp import pi_recovery as recovery
from meecp import private
from meecp.pi_setup import _joined


def workspaces(tmp_path):
    root = tmp_path.resolve()
    source, destination = root / "old", root / "new"
    source.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    return source, destination


def restore(source, destination, **kwargs):
    return recovery.restore_workspace(source, destination, disk_ceiling_bytes=8 * 1024**2, **kwargs)


def test_estimate_and_restore_exclude_protected_roots_and_skip_unsafe_entries(tmp_path):
    source, destination = workspaces(tmp_path)
    for name in recovery.EXCLUDED:
        if name.endswith(".json"):
            (source / name).write_bytes(b"synthetic protected metadata")
        else:
            (source / name).mkdir()
            (source / name / "protected.bin").write_bytes(b"synthetic protected state")
    (source / "build").mkdir()
    # Protected names only apply to the roots the host rematerializes.
    (source / "build/source").write_bytes(b"retained")
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "external.bin").write_bytes(b"must stay outside")
    (source / "linked-tree").symlink_to(outside, target_is_directory=True)
    (source / "linked-file").symlink_to(outside / "external.bin")
    (source / "hardlink-one").write_bytes(b"shared inode")
    os.link(source / "hardlink-one", source / "hardlink-two")
    os.mkfifo(source / "pipe")
    (source / "bad\nname").write_bytes(b"unsafe name")
    assert recovery.estimate_workspace_bytes(source) == 8
    assert restore(source, destination) == {"files": 1, "bytes": 8, "skipped_unsafe": 6}
    assert (destination / "build/source").read_bytes() == b"retained"
    assert list(destination.iterdir()) == [destination / "build"]
    assert (outside / "external.bin").read_bytes() == b"must stay outside"


@pytest.mark.parametrize("fallback", [False, True])
def test_restore_preserves_regular_empty_executable_files_with_independent_inodes(tmp_path, monkeypatch, fallback):
    source, destination = workspaces(tmp_path)
    (source / "bin").mkdir()
    (source / "bin/engine").write_bytes(b"synthetic engine")
    (source / "bin/engine").chmod(0o755)
    (source / "empty.py").write_bytes(b"")
    (source / "data.bin").write_bytes(b"synthetic bytes")
    clone = private._clone_file
    clone_calls = []

    def attempt_clone(descriptor, target, **kwargs):
        clone_calls.append(True)
        return False if fallback else clone(descriptor, target, **kwargs)

    monkeypatch.setattr(private, "_clone_file", attempt_clone)
    size = recovery.estimate_workspace_bytes(source)
    result = recovery.restore_workspace(source, destination, disk_ceiling_bytes=size)
    assert result == {"files": 3, "bytes": size, "skipped_unsafe": 0}
    assert len(clone_calls) == 3
    for name, mode in (("bin/engine", 0o700), ("empty.py", 0o600), ("data.bin", 0o600)):
        old, new = source / name, destination / name
        assert new.read_bytes() == old.read_bytes()
        assert stat.S_IMODE(new.stat().st_mode) == mode
        assert new.stat().st_nlink == 1 and new.stat().st_ino != old.stat().st_ino
    assert stat.S_IMODE((destination / "bin").stat().st_mode) == 0o700
    (destination / "data.bin").write_bytes(b"attempt-local changes")
    assert (source / "data.bin").read_bytes() == b"synthetic bytes"


async def test_cancellation_checks_fallback_chunks_and_caller_joins_the_thread(tmp_path, monkeypatch):
    source, destination = workspaces(tmp_path)
    selected = source / "large.bin"
    selected.write_bytes(b"a" * (3 * 1024**2))
    identity = (selected.stat().st_dev, selected.stat().st_ino)
    entered, release, canceled = threading.Event(), threading.Event(), threading.Event()
    read = os.read
    reads = []
    monkeypatch.setattr(private, "_clone_file", lambda *_, **kwargs: False)

    def paused_read(descriptor, count):
        data = read(descriptor, count)
        details = os.fstat(descriptor)
        if (details.st_dev, details.st_ino) == identity:
            reads.append(len(data))
            entered.set()
            assert release.wait(5), "test did not release owned copy thread"
        return data

    monkeypatch.setattr(private.os, "read", paused_read)
    task = asyncio.create_task(_joined(
        recovery.restore_workspace, source, destination,
        cancel_event=canceled, disk_ceiling_bytes=selected.stat().st_size, _on_cancel=canceled.set,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert canceled.is_set() and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reads == [1024**2]
    assert not (destination / "large.bin").exists()
    assert selected.stat().st_size == 3 * 1024**2


@pytest.mark.parametrize("replacement", ["leaf", "directory", "root", "ancestor"])
def test_source_replacement_is_detected_before_any_redirected_read(tmp_path, monkeypatch, replacement):
    root = tmp_path.resolve()
    owner = root / "owner"
    owner.mkdir(mode=0o700)
    source = owner / "old"
    source.mkdir(mode=0o700)
    (source / "build").mkdir()
    selected = source / "build/artifact.bin"
    selected.write_bytes(b"retained generated file")
    destination = root / "new"
    destination.mkdir(mode=0o700)
    outside = root / "outside"
    (outside / "old/build").mkdir(parents=True)
    (outside / "build").mkdir()
    for path in (outside / "artifact.bin", outside / "build/artifact.bin", outside / "old/build/artifact.bin"):
        path.write_bytes(b"external sentinel")
    copy = recovery.copy_regular_file

    def replace_then_copy(*args, **kwargs):
        if replacement == "leaf":
            selected.rename(selected.with_name("held.bin"))
            selected.symlink_to(outside / "artifact.bin")
        elif replacement == "directory":
            (source / "build").rename(source / "held-build")
            (source / "build").symlink_to(outside / "build", target_is_directory=True)
        elif replacement == "root":
            source.rename(owner / "held-old")
            source.symlink_to(outside, target_is_directory=True)
        else:
            owner.rename(root / "held-owner")
            owner.symlink_to(outside, target_is_directory=True)
        return copy(*args, **kwargs)

    monkeypatch.setattr(recovery, "copy_regular_file", replace_then_copy)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_source_changed$"):
        restore(source, destination)
    assert not (destination / "build/artifact.bin").exists()
    assert all(path.read_bytes() == b"external sentinel" for path in outside.rglob("*.bin"))


def test_destination_swap_during_clone_attempt_cannot_redirect_writes_or_cleanup(tmp_path, monkeypatch):
    source, destination = workspaces(tmp_path)
    (source / "build").mkdir()
    (source / "build/artifact.bin").write_bytes(b"retained generated file")
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "artifact.bin").write_bytes(b"external sentinel")
    held = destination / "held-build"
    clone = private._clone_file

    def replace_then_clone(descriptor, target, **kwargs):
        (destination / "build").rename(held)
        (destination / "build").symlink_to(outside, target_is_directory=True)
        return clone(descriptor, target, **kwargs)

    monkeypatch.setattr(private, "_clone_file", replace_then_clone)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_source_changed$"):
        restore(source, destination)
    assert (outside / "artifact.bin").read_bytes() == b"external sentinel"
    assert not (held / "artifact.bin").exists()


def test_estimate_detects_root_replacement_during_enumeration(tmp_path, monkeypatch):
    source, _ = workspaces(tmp_path)
    (source / "artifact.bin").write_bytes(b"generated file")
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    scan = os.scandir
    changed = False

    @contextmanager
    def replaced_scan(descriptor):
        def entries():
            nonlocal changed
            with scan(descriptor) as listing:
                for entry in listing:
                    if not changed:
                        changed = True
                        source.rename(source.with_name("held"))
                        source.symlink_to(outside, target_is_directory=True)
                    yield entry
        yield entries()

    monkeypatch.setattr(recovery.os, "scandir", replaced_scan)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_source_changed$"):
        recovery.estimate_workspace_bytes(source)


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_BYTES", "MAX_FILES", "MAX_ENTRIES", "MAX_DEPTH"])
def test_scan_limits_are_enforced_before_copying(tmp_path, monkeypatch, limit):
    source, destination = workspaces(tmp_path)
    if limit == "MAX_DEPTH":
        (source / "one/two").mkdir(parents=True)
        (source / "one/two/file").write_bytes(b"fixture")
    elif limit == "MAX_ENTRIES":
        os.mkfifo(source / "pipe-one")
        os.mkfifo(source / "pipe-two")
    else:
        (source / "one").write_bytes(b"four")
        (source / "two").write_bytes(b"four")
    monkeypatch.setattr(recovery, limit, 1)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_resume_limit$"):
        recovery.estimate_workspace_bytes(source)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_resume_limit$"):
        restore(source, destination)
    assert not list(destination.iterdir())


def test_explicit_disk_budget_and_free_space_bound_copy_before_mutation(tmp_path, monkeypatch):
    source, destination = workspaces(tmp_path)
    (source / "data.bin").write_bytes(b"generated data")
    size = recovery.estimate_workspace_bytes(source)
    with pytest.raises(TypeError):
        recovery.restore_workspace(source, destination)
    for ceiling in (-1, True, None):
        with pytest.raises(recovery.PiRecoveryError, match="^pi_resume_unavailable$"):
            recovery.restore_workspace(source, destination, disk_ceiling_bytes=ceiling)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_resume_limit$"):
        recovery.restore_workspace(source, destination, disk_ceiling_bytes=size - 1)
    monkeypatch.setattr(recovery, "_free_bytes", lambda *_: size - 1)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_resume_limit$"):
        recovery.restore_workspace(source, destination, disk_ceiling_bytes=size)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("unsafe", ["root-link", "nested-link", "public-mode", "existing-file", "overlap"])
def test_destination_requires_owned_private_paths_and_never_overwrites(tmp_path, unsafe):
    source, destination = workspaces(tmp_path)
    (source / "build").mkdir()
    (source / "build/file.bin").write_bytes(b"generated data")
    outside = tmp_path.resolve() / "outside"
    outside.mkdir(mode=0o700)
    if unsafe == "root-link":
        destination.rmdir()
        destination.symlink_to(outside, target_is_directory=True)
    elif unsafe == "nested-link":
        (destination / "build").symlink_to(outside, target_is_directory=True)
    elif unsafe == "public-mode":
        destination.chmod(0o755)
    elif unsafe == "existing-file":
        (destination / "build").mkdir(mode=0o700)
        (destination / "build/file.bin").write_bytes(b"existing sentinel")
    else:
        destination = source
    with pytest.raises(recovery.PiRecoveryError):
        restore(source, destination)
    assert not list(outside.iterdir())
    if unsafe == "existing-file":
        assert (destination / "build/file.bin").read_bytes() == b"existing sentinel"


def test_source_root_symlink_is_rejected_and_empty_checkpoint_accepts_zero_budget(tmp_path):
    source, destination = workspaces(tmp_path)
    link = source.with_name("linked")
    link.symlink_to(source, target_is_directory=True)
    with pytest.raises(recovery.PiRecoveryError, match="^pi_resume_unavailable$"):
        recovery.estimate_workspace_bytes(link)
    assert recovery.estimate_workspace_bytes(source) == 0
    assert recovery.restore_workspace(source, destination, disk_ceiling_bytes=0) == {
        "files": 0, "bytes": 0, "skipped_unsafe": 0,
    }
