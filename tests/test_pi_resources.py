from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import platform
import threading
import tracemalloc

import pytest

from meecp import pi_resources as resources
from meecp import private
from meecp.contracts import PackageError
from test_meecp_packages import make_package


IMPORT = "11111111-2222-4333-8444-555555555555"
SOURCE = "sha256:" + "a" * 64
BUDGET = 64 * 1024**2


def source(tmp_path, *, license=True):
    root = tmp_path.resolve() / "selected"
    root.mkdir()
    path = root / "weights.bin"
    path.write_bytes(b"public synthetic model bytes")
    notice = root / "NOTICE"
    notice.write_text("Synthetic redistribution notice\n")
    return {"name": "weights", "path": str(path), "role": "model-weights",
            "licenses": [str(notice)] if license else []}


def capture(store, request, **kwargs):
    result = store.capture(IMPORT, 1, SOURCE, request, disk_ceiling_bytes=BUDGET, **kwargs)
    result.pop("reference_created")
    return result


def test_resource_restarts_rebinds_and_materializes_after_original_removed(tmp_path):
    request = source(tmp_path)
    root = tmp_path.resolve() / "private-resources"
    store = resources.PiResourceStore(root)
    item = capture(store, request)
    assert item["license_status"] == "declared" and item["file_count"] == 2
    assert not list(store.staging.iterdir())
    metadata = (store.objects / item["resource_id"] / "resource.json").read_text()
    assert str(tmp_path) not in metadata
    Path(request["path"]).unlink()
    Path(request["licenses"][0]).unlink()
    reopened = resources.PiResourceStore(root)
    with pytest.raises(resources.PiResourceError, match="^pi_resource_binding_changed$"):
        reopened.list(IMPORT, 2, SOURCE)
    with pytest.raises(resources.PiResourceError, match="^pi_resource_binding_changed$"):
        reopened.rebind(IMPORT, 1, 2, "sha256:" + "b" * 64)
    rebound = reopened.rebind(IMPORT, 1, 2, SOURCE)
    assert rebound["resources"] == [item]
    result = reopened.materialize(IMPORT, 2, SOURCE, tmp_path.resolve() / "work", disk_ceiling_bytes=BUDGET)
    mounted = result["resources"][0]
    assert (tmp_path.resolve() / "work" / mounted["file_path"]).read_bytes() == b"public synthetic model bytes"
    assert (tmp_path.resolve() / "work" / mounted["licenses_path"] / "notice-0").is_file()
    assert result["binding"]["revision"] == 2
    reopened.remove(IMPORT, 2, SOURCE, item["resource_id"])
    assert reopened.list(IMPORT, 2, SOURCE)["resources"] == []
    assert (reopened.objects / item["resource_id"]).is_dir()
    assert reopened.retained_bytes() == item["bytes"]


def test_runtime_tree_preserves_empty_files_and_executable_modes(tmp_path):
    root = tmp_path.resolve() / "runtime"
    (root / "lib").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "lib/__init__.py").write_bytes(b"")
    (root / "lib/module.py").write_text("public runtime fixture\n")
    (root / "bin/engine").write_bytes(b"public prepared engine fixture")
    (root / "bin/engine").chmod(0o755)
    notice = tmp_path.resolve() / "NOTICE"
    notice.write_text("Synthetic runtime notice")
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    item = capture(store, {"name": "runtime", "kind": "tree", "path": str(root), "licenses": [str(notice)]})
    assert item["file_count"] == 4
    work = tmp_path.resolve() / "work"
    mounted = store.materialize(IMPORT, 1, SOURCE, work, disk_ceiling_bytes=BUDGET)["resources"][0]
    runtime = work / mounted["path"]
    assert (runtime / "lib/__init__.py").stat().st_size == 0
    assert stat_mode(runtime / "bin/engine") == 0o700
    assert stat_mode(runtime / "lib/module.py") == 0o600
    (runtime / "bin/engine").write_bytes(b"changed attempt-local bytes")
    assert store.list(IMPORT, 1, SOURCE)["resources"] == [item]


def stat_mode(path):
    return path.stat().st_mode & 0o777


def test_acquired_archive_is_retained_without_extraction_or_false_license(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "download.tar.xz"
    path.write_bytes(b"\xfd7zXZ\x00synthetic compressed artifact")
    monkeypatch.setattr("tarfile.open", lambda *args, **kwargs: pytest.fail("capture must not extract archives"))
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    request = {"name": "download", "kind": "archive", "path": str(path), "format": "archive",
               "origin": {"kind": "download", "url": "https://github.com/example/public/releases/download/v1/runtime.tar.xz"},
               "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "expected_size": path.stat().st_size}
    item = capture(store, request)
    assert item["license_status"] == "missing" and item["origin"]["publisher_trust"] == "publisher_unverified"
    assert item["file_count"] == 1
    assert item["file_size"] == path.stat().st_size and item["file_sha256"] == request["expected_sha256"]
    assert store.source_file(item["resource_id"]).read_bytes() == path.read_bytes()
    work = tmp_path.resolve() / "work"
    mounted = store.materialize(IMPORT, 1, SOURCE, work, disk_ceiling_bytes=BUDGET)["resources"][0]
    assert (work / mounted["file_path"]).read_bytes() == path.read_bytes()
    with pytest.raises(resources.PiResourceError, match="^pi_resource_origin_invalid$"):
        capture(store, {**request, "origin": {"kind": "download", "url": "https://example.com/asset?token=transient"}})


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "credential", "tree-link"])
def test_resources_reject_unsafe_inputs_without_catalog_publication(tmp_path, unsafe):
    request = source(tmp_path)
    path = Path(request["path"])
    if unsafe == "symlink":
        target = path.with_name("real.bin")
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, path.with_name("another.bin"))
    elif unsafe == "credential":
        path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nsynthetic-disallowed\n")
    else:
        path.with_name("link.bin").symlink_to(path)
        request = {**request, "kind": "tree", "path": str(path.parent)}
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    with pytest.raises(resources.PiResourceError):
        capture(store, request)
    assert not list(store.staging.iterdir())
    assert store.list(IMPORT, 1, SOURCE)["resources"] == []


def test_wrong_hash_disk_limit_stale_commit_and_tampered_resource_fail_closed(tmp_path):
    request = source(tmp_path)
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    with pytest.raises(resources.PiResourceError, match="^artifact_hash_mismatch$"):
        capture(store, {**request, "expected_sha256": "0" * 64})
    with pytest.raises(resources.PiResourceError, match="^pi_resource_disk_limit$"):
        store.capture(IMPORT, 1, SOURCE, request, disk_ceiling_bytes=16)
    calls = []
    def binding_check(binding):
        calls.append(binding)
        if len(calls) == 2:
            raise resources.PiResourceError("pi_resource_binding_changed")
    with pytest.raises(resources.PiResourceError, match="^pi_resource_binding_changed$"):
        capture(store, request, validate_binding=binding_check)
    assert store.list(IMPORT, 1, SOURCE)["resources"] == []
    item = capture(store, request)
    content = store.objects / item["resource_id"] / "content/weights.bin"
    content.write_bytes(b"tampered")
    with pytest.raises(resources.PiResourceError, match="^pi_resource_changed$"):
        store.list(IMPORT, 1, SOURCE)
    with pytest.raises(resources.PiResourceError, match="^pi_resource_changed$"):
        store.materialize(IMPORT, 1, SOURCE, tmp_path.resolve() / "work", disk_ceiling_bytes=BUDGET)
    assert not (tmp_path.resolve() / "work/resources").exists()


def test_large_resource_streams_with_separate_text_intake_limits(tmp_path):
    request = source(tmp_path)
    path = Path(request["path"])
    with path.open("wb") as output:
        output.truncate(17 * 1024**2)
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    tracemalloc.start()
    try:
        item = capture(store, request)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert item["bytes"] > 16 * 1024**2
    assert peak < 8 * 1024**2
    assert store.estimate_capture(request)["peak_disk_bytes"] <= BUDGET


@pytest.mark.asyncio
async def test_duplicate_capture_reuses_exact_object_and_cancel_joins_writer(tmp_path, monkeypatch):
    request = source(tmp_path)
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    results = await asyncio.gather(*(store.capture_async(IMPORT, 1, SOURCE, request, disk_ceiling_bytes=BUDGET) for _ in range(2)))
    assert sorted(result.pop("reference_created") for result in results) == [False, True]
    assert results[0] == results[1]
    assert len(list(store.objects.iterdir())) == 1
    assert len(store.list(IMPORT, 1, SOURCE)["resources"]) == 1
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    original = resources.copy_regular_file
    def paused(*args, **kwargs):
        started.set()
        released.wait(5)
        try:
            original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(resources, "copy_regular_file", paused)
    different = {**request, "name": "other"}
    task = asyncio.create_task(store.capture_async(IMPORT, 1, SOURCE, different, disk_ceiling_bytes=BUDGET))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done() and not finished.is_set()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and not list(store.staging.iterdir())
    assert len(store.list(IMPORT, 1, SOURCE)["resources"]) == 1


def test_named_rollback_preserves_preexisting_aliases_and_tracks_reused_references(tmp_path):
    request = source(tmp_path)
    store = resources.PiResourceStore(tmp_path.resolve() / "store")
    first = store.capture(IMPORT, 1, SOURCE, request, disk_ceiling_bytes=BUDGET)
    duplicate = store.capture(IMPORT, 1, SOURCE, request, disk_ceiling_bytes=BUDGET)
    alias = store.capture(IMPORT, 1, SOURCE, {**request, "name": "alias"}, disk_ceiling_bytes=BUDGET)
    assert first["reference_created"] and not duplicate["reference_created"] and alias["reference_created"]
    assert alias["resource_id"] == first["resource_id"]
    store.remove_name(IMPORT, 1, SOURCE, "alias", resource_id=alias["resource_id"])
    assert [item["name"] for item in store.list(IMPORT, 1, SOURCE)["resources"]] == ["weights"]
    assert store.source_file(first["resource_id"]).is_file()
    with pytest.raises(resources.PiResourceError, match="^pi_resource_binding_changed$"):
        store.remove_name(IMPORT, 1, SOURCE, "weights", resource_id="f" * 64)
    assert len(store.list(IMPORT, 1, SOURCE)["resources"]) == 1


def test_clone_and_fallback_preserve_package_independence_and_validation(tmp_path, monkeypatch):
    package = make_package(tmp_path.resolve() / "package")
    calls = []
    original = private._clone_file
    def observed(fd, target):
        result = original(fd, target)
        calls.append(result)
        return result
    monkeypatch.setattr(private, "_clone_file", observed)
    copied = private.snapshot_package(package, tmp_path.resolve() / "clone/package")
    assert len(calls) == 4
    if platform.system() == "Darwin":
        assert any(calls), "native APFS cloning must be exercised on this fixture filesystem"
    assert (copied.root / "LICENSE").stat().st_ino != (package / "LICENSE").stat().st_ino
    assert (copied.root / "LICENSE").stat().st_nlink == 1
    monkeypatch.setattr(private, "_clone_file", lambda *args: False)
    fallback = private.snapshot_package(package, tmp_path.resolve() / "fallback/package")
    (package / "LICENSE").write_text("changed source")
    assert (copied.root / "LICENSE").read_text() == (fallback.root / "LICENSE").read_text() == "MIT License\n"
    with pytest.raises(PackageError):
        private.snapshot_package(package, tmp_path.resolve() / "damaged/package")


def test_clone_source_change_is_rejected_and_empty_runtime_file_is_supported(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source_path = root / "source"
    source_path.write_bytes(b"before")
    destination = root / "copy"
    original = private._clone_file
    def changed(fd, target):
        result = original(fd, target)
        source_path.write_bytes(b"after!")
        return result
    monkeypatch.setattr(private, "_clone_file", changed)
    with source_path.open("rb") as source_file, pytest.raises(PackageError, match="^package_changed$"):
        private.copy_regular_file(source_file.fileno(), destination, size=6)
