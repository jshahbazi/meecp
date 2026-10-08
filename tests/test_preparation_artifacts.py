from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import tracemalloc
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from meecp import preparation_artifacts as artifacts
from meecp.imports import ImportStore
from meecp.preparation_contracts import canonical_bytes, contract_digest


BUDGET = 8 * 1024 * 1024


@pytest.fixture
def directory(tmp_path):
    # macOS's tempfile default uses /var, a platform symlink to /private/var.
    # Selections must use an explicitly canonical, no-symlink ancestry.
    return tmp_path.resolve()


def selection(directory, content=b"\x08\x07synthetic ONNX selection", **overrides):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "selected.onnx"
    path.write_bytes(content)
    notice = directory / "NOTICE"
    notice.write_text("Synthetic license notice\n")
    entry = {"name": "weights", "role": "model-weights", "path": str(path), "format": "onnx", "licenses": ["notice"]}
    entry.update(overrides)
    return artifacts.AcquisitionRequestV1(entries=[entry, {"name": "notice", "role": "license", "path": str(notice), "format": "text"}])


def store(directory):
    return artifacts.ArtifactStore(directory / "private-artifacts")


def assert_clean(value):
    assert not list((value.root / "staging").iterdir())
    assert not list((value.root / "objects").iterdir())


def capture_error(value, request, **kwargs):
    with pytest.raises(artifacts.ArtifactError) as caught:
        value.capture(request, disk_ceiling_bytes=BUDGET, **kwargs)
    assert str(caught.value) == caught.value.code
    assert_clean(value)
    return caught.value.code


def test_capture_identity_portability_exact_licenses_and_independent_stream(directory, monkeypatch):
    request = selection(directory / "source")
    value = store(directory)
    def forbidden(*args, **kwargs):
        pytest.fail("artifact capture must not create a process")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    payload = collection.model_dump(mode="json")
    assert [entry["name"] for entry in payload["entries"]] == ["notice", "weights"]
    assert collection.digest == contract_digest("captured-artifact-collection-v1", payload)
    assert collection.digest != contract_digest("artifact-collection-v1", payload)
    notice, weights = collection.entries
    assert not notice.licenses
    assert [(ref.name, ref.sha256) for ref in weights.licenses] == [(notice.name, notice.sha256)]
    assert weights.origin.publisher_trust == "publisher_unverified"
    assert weights.origin.supplied_sha256 is None
    data = canonical_bytes(payload)
    assert str(directory).encode() not in data and b"selected.onnx" not in data
    object_dir = value.root / "objects" / collection.digest
    assert (object_dir / artifacts.MANIFEST_NAME).read_bytes() == data
    for path in [value.root, value.root / "objects", value.root / "staging", object_dir, object_dir / "files"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in [object_dir / artifacts.MANIFEST_NAME, *list((object_dir / "files").iterdir())]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    source = Path(request.entries[0].path)
    expected = source.read_bytes()
    source.unlink()
    Path(request.entries[1].path).unlink()
    assert value.verify(collection.digest) == collection
    with value.open_artifact(collection.digest, "weights") as fd:
        assert os.read(fd, 4096) == expected
        with pytest.raises(OSError):
            os.write(fd, b"forbidden")
    with pytest.raises(OSError):
        os.fstat(fd)


def test_content_identity_ignores_original_filename_and_selection_order(directory):
    first = selection(directory / "one")
    second = selection(directory / "two")
    second = artifacts.AcquisitionRequestV1(entries=list(reversed(second.entries)))
    value = store(directory)
    a = value.capture(first, disk_ceiling_bytes=BUDGET)
    b = value.capture(second, disk_ceiling_bytes=BUDGET)
    assert a == b and a.digest == b.digest
    assert len(list((value.root / "objects").iterdir())) == 1
    assert not list((value.root / "staging").iterdir())


def test_supplied_hash_checks_bytes_without_promoting_publisher(directory):
    content = b"\x08synthetic pinned weights"
    request = selection(directory / "source", content, expected_sha256=hashlib.sha256(content).hexdigest(), expected_size=len(content))
    value = store(directory)
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    assert collection.entries[-1].origin.supplied_sha256 == collection.entries[-1].sha256
    assert collection.entries[-1].origin.publisher_trust == "publisher_unverified"


@pytest.mark.parametrize("overrides,code", [({"expected_sha256": "0" * 64}, "artifact_hash_mismatch"),
                                            ({"expected_size": 1}, "artifact_size_mismatch")])
def test_hash_and_size_mismatches_clean_owned_stage(directory, overrides, code):
    assert capture_error(store(directory), selection(directory / "source", **overrides)) == code


@pytest.mark.parametrize("damage", ["duplicate", "missing_license", "wrong_role", "license_self_reference", "unknown", "boolean_version", "bad_format", "license_binary", "relative", "traversal", "url", "boolean_size"])
def test_closed_request_contract_rejects_bad_bindings(directory, damage):
    raw = selection(directory / "source").model_dump(mode="json")
    if damage == "duplicate":
        raw["entries"].append(raw["entries"][0])
    elif damage == "missing_license":
        raw["entries"][0]["licenses"] = []
    elif damage == "wrong_role":
        raw["entries"][0]["licenses"] = ["weights"]
    elif damage == "license_self_reference":
        raw["entries"][1]["licenses"] = ["notice"]
    elif damage == "unknown":
        raw["network"] = ["https://example.invalid"]
    elif damage == "boolean_version":
        raw["schema_version"] = True
    elif damage == "bad_format":
        raw["entries"][0]["format"] = "arbitrary-import-parser"
    elif damage == "license_binary":
        raw["entries"][1]["format"] = "binary"
    elif damage == "relative":
        raw["entries"][0]["path"] = "selected.onnx"
    elif damage == "traversal":
        raw["entries"][0]["path"] = str(directory) + "/../selected.onnx"
    elif damage == "url":
        raw["entries"][0]["path"] = "https://example.invalid/selected.onnx"
    else:
        raw["entries"][0]["expected_size"] = True
    with pytest.raises((ValidationError, artifacts.ArtifactError)):
        artifacts.AcquisitionRequestV1.model_validate(raw)


@pytest.mark.parametrize("damage", ["order", "duplicate", "license_hash", "absolute_path", "publisher", "unknown", "boolean_version"])
def test_captured_contract_rejects_ambiguous_or_unbound_metadata(directory, damage):
    value = store(directory)
    raw = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET).model_dump(mode="json")
    if damage == "order":
        raw["entries"].reverse()
    elif damage == "duplicate":
        raw["entries"].append(raw["entries"][-1])
    elif damage == "license_hash":
        raw["entries"][-1]["licenses"][0]["sha256"] = "0" * 64
    elif damage == "absolute_path":
        raw["entries"][-1]["path"] = str(directory / "selected.onnx")
    elif damage == "publisher":
        raw["entries"][-1]["origin"]["publisher_trust"] = "verified"
    elif damage == "unknown":
        raw["entries"][-1]["origin"]["source_path"] = str(directory)
    else:
        raw["schema_version"] = True
    with pytest.raises((ValidationError, artifacts.ArtifactError)):
        artifacts.CapturedArtifactCollectionV1.model_validate(raw)


@pytest.mark.parametrize("kind", ["leaf_symlink", "ancestor_symlink", "hardlink", "fifo", "directory"])
def test_unsafe_local_nodes_never_publish(directory, kind):
    request = selection(directory / "source")
    path = Path(request.entries[0].path)
    raw = request.model_dump(mode="json")
    selected = directory / "unsafe.onnx"
    if kind == "leaf_symlink":
        selected.symlink_to(path)
    elif kind == "ancestor_symlink":
        linked = directory / "linked"
        linked.symlink_to(path.parent, target_is_directory=True)
        selected = linked / path.name
    elif kind == "hardlink":
        os.link(path, selected)
    elif kind == "fifo":
        os.mkfifo(selected)
    else:
        selected.mkdir()
    raw["entries"][0]["path"] = str(selected)
    capture_error(store(directory), artifacts.AcquisitionRequestV1.model_validate(raw))


@pytest.mark.parametrize("name", [".env", ".env.production", "credentials.json", "id_ed25519", "private.pem", "weights.zip", "weights.whl"])
def test_credential_and_archive_paths_rejected_before_open(directory, name):
    with pytest.raises((ValidationError, artifacts.ArtifactError)):
        selection(directory / "source", path=str(directory / name))


@pytest.mark.parametrize("content,code", [(b"PK\x03\x04synthetic archive", "artifact_archive_disallowed"),
                                          (b"\x1f\x8bsynthetic compressed stream", "artifact_archive_disallowed"),
                                          (b"\x00-----BEGIN " + b"PRIVATE KEY-----\x00", "artifact_credentials_disallowed")])
def test_content_markers_block_disguised_credentials_and_archives(directory, content, code):
    assert capture_error(store(directory), selection(directory / "source", content)) == code


def test_secret_marker_across_chunk_boundary_is_detected(directory, monkeypatch):
    monkeypatch.setattr(artifacts, "COPY_CHUNK_BYTES", 32)
    content = b"\x08" * 25 + b"-----BEGIN " + b"PRIVATE KEY-----" + b"\x00" * 64
    assert capture_error(store(directory), selection(directory / "source", content)) == "artifact_credentials_disallowed"


def credential_samples():
    # Construct synthetic pattern fixtures; these are not service credentials.
    return [b"-----BEGIN " + b"PRIVATE KEY-----",
            b"-----BEGIN RSA " + b"PRIVATE KEY-----",
            b"A" + b"KIA" + b"1" * 16,
            b"s" + b"k-" + b"A" * 24,
            b"s" + b"k-proj-" + b"A" * 24,
            *(b"gh" + kind + b"_" + b"A" * 24 for kind in (b"p", b"o", b"u", b"s", b"r"))]


@pytest.mark.parametrize("case", range(10))
def test_all_credential_patterns_rejected_across_every_prefix_boundary(directory, monkeypatch, case):
    monkeypatch.setattr(artifacts, "COPY_CHUNK_BYTES", 64)
    credential = credential_samples()[case]
    value = store(directory)
    for split in range(1, len(credential)):
        content = b"\x00" * (64 - split) + credential + b"\x00" * 64
        request = selection(directory / ("source-" + str(split)), content)
        assert capture_error(value, request) == "artifact_credentials_disallowed"


@pytest.mark.parametrize("case", range(10))
def test_reverification_preserves_credential_rejection(directory, monkeypatch, case):
    monkeypatch.setattr(artifacts, "COPY_CHUNK_BYTES", 64)
    credential = credential_samples()[case]
    value = store(directory)
    content = b"\x00" * 61 + credential + b"\x00" * 64
    request = selection(directory / "source", b"\x08" * len(content))
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    captured = value.root / "objects" / collection.digest / "files" / "weights"
    captured.chmod(0o600)
    captured.write_bytes(content)
    captured.chmod(0o400)
    with pytest.raises(artifacts.ArtifactError) as caught:
        value.verify(collection.digest)
    assert caught.value.code == "artifact_credentials_disallowed"


def test_prefix_near_misses_keep_original_word_boundaries_and_minimum_lengths(directory):
    samples = credential_samples()
    content = b"\x00".join([b"-----BEGIN malformed header", b"AKIA-short", b"sk-short", b"ghp_short",
                              b"x" + samples[2], samples[2] + b"x", b"x" + samples[3], b"x" + samples[5]])
    value = store(directory)
    collection = value.capture(selection(directory / "source", content), disk_ceiling_bytes=BUDGET)
    assert value.verify(collection.digest) == collection


def test_license_bytes_must_be_utf8(directory):
    request = selection(directory / "source")
    Path(request.entries[1].path).write_bytes(b"\xff\xfe")
    assert capture_error(store(directory), request) == "artifact_license_format_required"


@pytest.mark.parametrize("kind", ["file", "aggregate", "count", "zero"])
def test_size_and_count_bounds_without_large_allocation(directory, monkeypatch, kind):
    request = selection(directory / "source")
    value = store(directory)
    if kind == "file":
        with Path(request.entries[0].path).open("r+b") as handle:
            handle.truncate(artifacts.MAX_ARTIFACT_FILE_BYTES + 1)
        assert capture_error(value, request) == "artifact_size_limit"
    elif kind == "aggregate":
        monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 32)
        with pytest.raises(artifacts.ArtifactError, match="artifact_size_limit"):
            value.capture(request, disk_ceiling_bytes=32 + artifacts.MAX_CONTRACT_BYTES)
        assert_clean(value)


    elif kind == "zero":
        Path(request.entries[0].path).write_bytes(b"")
        assert capture_error(value, request) == "artifact_size_limit"
    else:
        raw = request.model_dump(mode="json")
        raw["entries"] = [dict(raw["entries"][1], name="notice" + str(i)) for i in range(65)]
        with pytest.raises(ValidationError):
            artifacts.AcquisitionRequestV1.model_validate(raw)
        assert_clean(value)


def test_exact_maximum_collection_captures_and_verifies_bounded_file_closure(directory):
    base = selection(directory / "source")
    raw = base.model_dump(mode="json")
    entry = {**raw["entries"][0], "role": "runtime-file", "format": "binary"}
    raw["entries"] = [raw["entries"][1], *[dict(entry, name="runtime" + str(i)) for i in range(63)]]
    value = store(directory)
    collection = value.capture(artifacts.AcquisitionRequestV1.model_validate(raw), disk_ceiling_bytes=BUDGET)
    assert len(collection.entries) == artifacts.MAX_ARTIFACT_FILES
    assert value.verify(collection.digest) == collection


def test_disk_ceiling_and_actual_free_space_checked_before_copy(directory, monkeypatch):
    request = selection(directory / "source")
    value = store(directory)
    with pytest.raises(artifacts.ArtifactError, match="artifact_disk_limit"):
        value.capture(request, disk_ceiling_bytes=128)
    assert_clean(value)
    monkeypatch.setattr(artifacts.os, "fstatvfs", lambda fd: SimpleNamespace(f_bavail=0, f_frsize=4096))
    assert capture_error(value, request) == "artifact_disk_unavailable"


def test_free_disk_drop_during_stream_cleans_owned_copy(directory, monkeypatch):
    request = selection(directory / "source", b"\x08" * (artifacts.COPY_CHUNK_BYTES + 64))
    value = store(directory)
    original = artifacts._write
    exhausted = False
    def write(fd, data):
        nonlocal exhausted
        original(fd, data)
        if len(data) > 512:
            exhausted = True
    def available(fd):
        return SimpleNamespace(f_bavail=0 if exhausted else 1024 * 1024, f_frsize=4096)
    monkeypatch.setattr(artifacts, "_write", write)
    monkeypatch.setattr(artifacts.os, "fstatvfs", available)
    assert capture_error(value, request) == "artifact_disk_unavailable"


@pytest.mark.parametrize("options", [{"disk_ceiling_bytes": True}, {"disk_ceiling_bytes": 0},
                                     {"deadline": float("nan")}, {"deadline": float("inf")},
                                     {"deadline": True}, {"cancel_event": "invalid"}])
def test_budget_is_strict_and_finite(directory, options):
    value = store(directory)
    request = selection(directory / "source")
    arguments = {"disk_ceiling_bytes": BUDGET, **options}
    with pytest.raises(artifacts.ArtifactError, match="artifact_budget_invalid"):
        value.capture(request, **arguments)
    assert_clean(value)


@pytest.mark.parametrize("kind", ["cancel", "deadline"])
def test_preexisting_stop_never_creates_work(directory, kind):
    event = threading.Event()
    event.set()
    options = {"cancel_event": event} if kind == "cancel" else {"deadline": time.monotonic() - 1}
    assert capture_error(store(directory), selection(directory / "source"), **options) == ("artifact_capture_canceled" if kind == "cancel" else "artifact_deadline_exceeded")


def test_cancel_during_owned_copy_joins_and_cleans(directory, monkeypatch):
    request = selection(directory / "source", b"\x08" * (2 * artifacts.COPY_CHUNK_BYTES))
    value = store(directory)
    event, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = artifacts._write
    def paused(fd, data):
        original(fd, data)
        if len(data) > 512:
            entered.set()
            assert release.wait(10)
    monkeypatch.setattr(artifacts, "_write", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(value.capture, request, disk_ceiling_bytes=BUDGET, cancel_event=event)
        assert entered.wait(10)
        assert list((value.root / "staging").iterdir()) and not pending.done()
        event.set()
        release.set()
        with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
            pending.result(timeout=10)
    assert_clean(value)


def test_deadline_during_stream_cleans_private_copy(directory, monkeypatch):
    request = selection(directory / "source", b"\x08" * (artifacts.COPY_CHUNK_BYTES + 64))
    value = store(directory)
    original = artifacts._write
    deadline = time.monotonic() + 0.1
    def write(fd, data):
        original(fd, data)
        if len(data) > 512:
            while time.monotonic() < deadline:
                time.sleep(0.005)
    monkeypatch.setattr(artifacts, "_write", write)
    assert capture_error(value, request, deadline=deadline) == "artifact_deadline_exceeded"


@pytest.mark.parametrize("kind", ["leaf_replace", "ancestor_replace", "content_change", "store_replace", "objects_replace"])
def test_real_copy_races_reject_and_cleanup_owned_staging(directory, monkeypatch, kind):
    request = selection(directory / "source", b"\x08" * (artifacts.COPY_CHUNK_BYTES + 64))
    value = store(directory)
    selected = Path(request.entries[0].path)
    original = artifacts._write
    changed = False
    def raced(fd, data):
        nonlocal changed
        original(fd, data)
        if len(data) < 512 or changed:
            return
        changed = True
        if kind == "leaf_replace":
            selected.rename(selected.with_suffix(".old"))
            selected.write_bytes(b"\x08" * (artifacts.COPY_CHUNK_BYTES + 64))
        elif kind == "ancestor_replace":
            selected.parent.rename(directory / "moved-source")
            selected.parent.mkdir()
        elif kind == "content_change":
            with selected.open("r+b") as handle:
                handle.seek(-1, os.SEEK_END)
                handle.write(b"\x09")
        elif kind == "store_replace":
            value.root.rename(directory / "moved-store")
            value.root.mkdir(mode=0o700)
        else:
            (value.root / "objects").rename(value.root / "moved-objects")
            (value.root / "objects").mkdir(mode=0o700)
    monkeypatch.setattr(artifacts, "_write", raced)
    with pytest.raises(artifacts.ArtifactError):
        value.capture(request, disk_ceiling_bytes=BUDGET)
    cleanup_root = directory / "moved-store" if kind == "store_replace" else value.root
    assert not list((cleanup_root / "staging").iterdir())
    assert not list((cleanup_root / "objects").iterdir())
    if kind == "objects_replace":
        assert not list((cleanup_root / "moved-objects").iterdir())


def test_independent_source_rehash_catches_change_after_initial_identity_check(directory, monkeypatch):
    request = selection(directory / "source")
    selected = Path(request.entries[0].path)
    inode = selected.stat().st_ino
    original = artifacts._hash_fd
    changed = False
    def raced(fd, size, budget, **kwargs):
        nonlocal changed
        if os.fstat(fd).st_ino == inode and not changed:
            changed = True
            selected.write_bytes(b"\x09" * size)
        return original(fd, size, budget, **kwargs)
    monkeypatch.setattr(artifacts, "_hash_fd", raced)
    assert capture_error(store(directory), request) == "artifact_file_changed"


def test_uncertain_staging_location_retains_bytes_without_deleting_foreign_tree(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source", b"\x08" * (artifacts.COPY_CHUNK_BYTES + 64))
    original = artifacts._write
    moved = False
    foreign = None
    retained = value.root / "retained-unknown-stage"
    def raced(fd, data):
        nonlocal moved, foreign
        original(fd, data)
        if len(data) > 512 and not moved:
            moved = True
            active = next((value.root / "staging").iterdir())
            active.rename(retained)
            active.mkdir(mode=0o700)
            foreign = active / "foreign"
            foreign.write_bytes(b"keep foreign bytes")
    monkeypatch.setattr(artifacts, "_write", raced)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_reconciliation_required"):
        value.capture(request, disk_ceiling_bytes=BUDGET)
    assert foreign.read_bytes() == b"keep foreign bytes"
    assert (retained / "files" / "weights").is_file()
    assert not list((value.root / "objects").iterdir())


def test_cancel_during_final_verification_retains_complete_committed_cache(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    event = threading.Event()
    original = value._verify_fd
    calls = 0
    def stop(directory_fd, digest, budget):
        nonlocal calls
        calls += 1
        if calls == 2:
            event.set()
        return original(directory_fd, digest, budget)
    monkeypatch.setattr(value, "_verify_fd", stop)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
        value.capture(request, disk_ceiling_bytes=BUDGET, cancel_event=event)
    assert not list((value.root / "staging").iterdir())
    objects = list((value.root / "objects").iterdir())
    assert len(objects) == 1
    monkeypatch.setattr(value, "_verify_fd", original)
    value.verify(objects[0].name)


def test_failed_atomic_publication_cleans_owned_staging(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    def publish(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic")
    monkeypatch.setattr(artifacts, "_publish_directory", publish)
    assert capture_error(value, request) == "artifact_capture_failed"


def test_stage_open_failure_removes_owned_empty_directory_and_retry_succeeds(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    original = artifacts._open_directory
    def exhausted(parent, name, **kwargs):
        if len(name) == 36:
            raise OSError(errno.EMFILE, "synthetic")
        return original(parent, name, **kwargs)
    monkeypatch.setattr(artifacts, "_open_directory", exhausted)
    assert capture_error(value, request) == "artifact_capture_failed"
    monkeypatch.setattr(artifacts, "_open_directory", original)
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    assert value.verify(collection.digest) == collection


def test_publication_reuses_stage_fd_without_opening_a_digest_claim(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    original = artifacts._open_directory
    def exhausted(parent, name, **kwargs):
        if len(name) == 64:
            raise OSError(errno.EMFILE, "synthetic")
        return original(parent, name, **kwargs)
    monkeypatch.setattr(artifacts, "_open_directory", exhausted)
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    monkeypatch.setattr(artifacts, "_open_directory", original)
    assert value.verify(collection.digest) == collection
    assert not list((value.root / "staging").iterdir())


@pytest.mark.parametrize("reuse", [False, True])
def test_stop_immediately_after_final_successful_hash_cannot_return_success(directory, monkeypatch, reuse):
    value = store(directory)
    request = selection(directory / "source")
    existing = value.capture(request, disk_ceiling_bytes=BUDGET) if reuse else None
    event = threading.Event()
    original = value._verify_fd
    calls = 0
    def stop(directory_fd, digest, budget):
        nonlocal calls
        result = original(directory_fd, digest, budget)
        calls += 1
        if calls == 2:
            event.set()
        return result
    monkeypatch.setattr(value, "_verify_fd", stop)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
        value.capture(request, disk_ceiling_bytes=BUDGET, cancel_event=event)
    assert not list((value.root / "staging").iterdir())
    assert len(list((value.root / "objects").iterdir())) == 1
    if existing:
        monkeypatch.setattr(value, "_verify_fd", original)
        assert value.verify(existing.digest) == existing


def test_native_move_error_after_commit_cannot_delete_visible_object(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    original = artifacts._publish_directory
    def uncertain(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError(errno.EIO, "synthetic")
    monkeypatch.setattr(artifacts, "_publish_directory", uncertain)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_failed"):
        value.capture(request, disk_ceiling_bytes=BUDGET)
    assert not list((value.root / "staging").iterdir())
    objects = list((value.root / "objects").iterdir())
    assert len(objects) == 1
    value.verify(objects[0].name)


def test_another_reader_keeps_published_identity_after_late_cancellation(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    event = threading.Event()
    original = value._verify_fd
    adopted = []
    calls = 0
    def inspect_then_cancel(directory_fd, digest, budget):
        nonlocal calls
        result = original(directory_fd, digest, budget)
        calls += 1
        if calls == 2:
            # A separate store sees the globally visible complete object
            # while the publishing caller still owns its final check.
            independent = artifacts.ArtifactStore(value.root)
            adopted.append(independent.verify(digest))
            event.set()
        return result
    monkeypatch.setattr(value, "_verify_fd", inspect_then_cancel)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
        value.capture(request, disk_ceiling_bytes=BUDGET, cancel_event=event)
    assert len(adopted) == 1
    independent = artifacts.ArtifactStore(value.root)
    assert independent.verify(adopted[0].digest) == adopted[0]
    with independent.open_artifact(adopted[0].digest, "weights") as fd:
        assert os.read(fd, 8)
    assert not list((value.root / "staging").iterdir())


def test_concurrent_identical_capture_adopts_complete_cache_after_publisher_cancel(directory, monkeypatch):
    first = store(directory)
    second = artifacts.ArtifactStore(first.root)
    request = selection(directory / "source")
    event, published, release, waiting = (threading.Event() for _ in range(4))
    original_verify = first._verify_fd
    calls = 0
    def paused(directory_fd, digest, budget):
        nonlocal calls
        result = original_verify(directory_fd, digest, budget)
        calls += 1
        if calls == 2:
            published.set()
            assert release.wait(10)
        return result
    monkeypatch.setattr(first, "_verify_fd", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publisher = pool.submit(first.capture, request, disk_ceiling_bytes=BUDGET, cancel_event=event)
        assert published.wait(10)
        original_lock = artifacts._lock_publication
        def second_waiting(fd, budget):
            waiting.set()
            return original_lock(fd, budget)
        monkeypatch.setattr(artifacts, "_lock_publication", second_waiting)
        adopter = pool.submit(second.capture, request, disk_ceiling_bytes=BUDGET)
        assert waiting.wait(10)
        assert not adopter.done()
        event.set()
        release.set()
        with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
            publisher.result(timeout=10)
        result = adopter.result(timeout=10)
    assert second.verify(result.digest) == result
    assert len(list((first.root / "objects").iterdir())) == 1
    assert not list((first.root / "staging").iterdir())


def test_late_cancel_cannot_crash_while_deleting_visible_collection(directory):
    value = store(directory)
    request = selection(directory / "source")
    child = os.fork()
    if child == 0:
        event = threading.Event()
        original = value._verify_fd
        calls = 0
        def canceled(directory_fd, digest, budget):
            nonlocal calls
            result = original(directory_fd, digest, budget)
            calls += 1
            if calls == 2:
                event.set()
            return result
        def forbidden_unlink(*args, **kwargs):
            os._exit(23)
        value._verify_fd = canceled
        artifacts.os.unlink = forbidden_unlink
        try:
            value.capture(request, disk_ceiling_bytes=BUDGET, cancel_event=event)
        except artifacts.ArtifactError as error:
            os._exit(17 if error.code == "artifact_capture_canceled" else 18)
        os._exit(19)
    until = time.monotonic() + 10
    while True:
        completed, status = os.waitpid(child, os.WNOHANG)
        if completed:
            break
        if time.monotonic() >= until:
            os.kill(child, 9)
            os.waitpid(child, 0)
            pytest.fail("owned crash fixture exceeded deadline")
        time.sleep(0.01)
    assert os.waitstatus_to_exitcode(status) == 17
    restored = artifacts.ArtifactStore(value.root)
    objects = list((value.root / "objects").iterdir())
    assert len(objects) == 1
    restored.verify(objects[0].name)
    result = restored.capture(request, disk_ceiling_bytes=BUDGET)
    assert restored.verify(result.digest) == result
    assert not list((value.root / "staging").iterdir())


@pytest.mark.parametrize("after_commit", [False, True])
def test_abrupt_stop_around_atomic_publish_never_poisoned_digest_retry(directory, monkeypatch, after_commit):
    value = store(directory)
    request = selection(directory / "source")
    original = artifacts._publish_directory
    def crash(*args, **kwargs):
        if after_commit:
            original(*args, **kwargs)
        os._exit(17)
    child = os.fork()
    if child == 0:
        try:
            artifacts._publish_directory = crash
            value.capture(request, disk_ceiling_bytes=BUDGET)
        finally:
            os._exit(18)
    # This test owns the returned child; it never discovers/adopts a PID.
    until = time.monotonic() + 10
    try:
        while True:
            completed, status = os.waitpid(child, os.WNOHANG)
            if completed:
                break
            if time.monotonic() >= until:
                os.kill(child, 9)
                os.waitpid(child, 0)
                pytest.fail("owned crash fixture exceeded deadline")
            time.sleep(0.01)
    except BaseException:
        try:
            os.kill(child, 9)
            os.waitpid(child, 0)
        except ProcessLookupError:
            pass
        raise
    assert os.waitstatus_to_exitcode(status) == 17
    restored = artifacts.ArtifactStore(value.root)
    objects = list((value.root / "objects").iterdir())
    assert len(objects) == int(after_commit)
    if objects:
        restored.verify(objects[0].name)
    result = restored.capture(request, disk_ceiling_bytes=BUDGET)
    assert restored.verify(result.digest) == result
    assert len(list((value.root / "objects").iterdir())) == 1
    # A precommit crash leaves an unpublished private stage for the owning
    # coordinator's retention/recovery; the store does not adopt it by scan.
    assert len(list((value.root / "staging").iterdir())) == int(not after_commit)


def test_cancellation_while_waiting_for_publication_lock_cleans_stage(directory, monkeypatch):
    value = store(directory)
    request = selection(directory / "source")
    event, waiting = threading.Event(), threading.Event()
    fd = os.open(value.root / "objects", os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    original = artifacts._lock_publication
    def locked(directory_fd, budget):
        waiting.set()
        return original(directory_fd, budget)
    monkeypatch.setattr(artifacts, "_lock_publication", locked)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(value.capture, request, disk_ceiling_bytes=BUDGET, cancel_event=event)
            assert waiting.wait(10)
            event.set()
            with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
                pending.result(timeout=10)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert_clean(value)


def test_concurrent_identical_captures_reuse_only_complete_verified_object(directory):
    request = selection(directory / "source", b"\x08" * 128 * 1024)
    value = store(directory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(value.capture, request, disk_ceiling_bytes=BUDGET) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert len(list((value.root / "objects").iterdir())) == 1
    assert not list((value.root / "staging").iterdir())


@pytest.mark.parametrize("damage", ["bytes", "extra_file", "symlink", "hardlink", "writable", "manifest_unknown", "manifest_duplicates", "noncanonical", "ancestor"])
def test_reverification_rejects_tampered_closed_objects(directory, damage):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    object_dir = value.root / "objects" / collection.digest
    selected = object_dir / "files" / "weights"
    manifest = object_dir / artifacts.MANIFEST_NAME
    if damage == "bytes":
        selected.chmod(0o600)
        selected.write_bytes(b"\x09" * selected.stat().st_size)
        selected.chmod(0o400)
    elif damage == "extra_file":
        (object_dir / "extra").write_bytes(b"unexpected")
    elif damage == "symlink":
        selected.unlink()
        selected.symlink_to(Path(directory / "source" / "selected.onnx"))
    elif damage == "hardlink":
        os.link(selected, directory / "alias")
    elif damage == "writable":
        selected.chmod(0o600)
    elif damage == "ancestor":
        object_dir.rename(value.root / "moved-object")
        object_dir.symlink_to(value.root / "moved-object", target_is_directory=True)
    else:
        data = manifest.read_bytes()
        manifest.chmod(0o600)
        if damage == "manifest_unknown":
            raw = json.loads(data)
            raw["origin_path"] = str(directory)
            manifest.write_bytes(canonical_bytes(raw))
        elif damage == "manifest_duplicates":
            manifest.write_bytes(data[:-1] + b',"schema_version":1}')
        else:
            manifest.write_bytes(data + b"\n")
        manifest.chmod(0o400)
    with pytest.raises(artifacts.ArtifactError):
        value.verify(collection.digest)
    with pytest.raises(artifacts.ArtifactError):
        with value.open_artifact(collection.digest, "weights"):
            pytest.fail("tampered bytes must not become worker inputs")


def test_corrupt_immutable_collision_is_preserved_and_never_overwritten(directory):
    request = selection(directory / "source")
    value = store(directory)
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    selected = value.root / "objects" / collection.digest / "files" / "weights"
    selected.chmod(0o600)
    selected.write_bytes(b"corrupt existing object")
    selected.chmod(0o400)
    before = selected.read_bytes()
    with pytest.raises(artifacts.ArtifactError):
        value.capture(request, disk_ceiling_bytes=BUDGET)
    assert selected.read_bytes() == before
    assert not list((value.root / "staging").iterdir())


def test_native_exclusive_publish_never_overwrites_even_an_empty_existing_object(directory):
    request = selection(directory / "source")
    value = store(directory)
    collection = value.capture(request, disk_ceiling_bytes=BUDGET)
    object_dir = value.root / "objects" / collection.digest
    for path in (object_dir / "files").iterdir():
        path.unlink()
    (object_dir / "files").rmdir()
    (object_dir / artifacts.MANIFEST_NAME).unlink()
    inode = object_dir.stat().st_ino
    with pytest.raises(artifacts.ArtifactError, match="artifact_tree_invalid"):
        value.capture(request, disk_ceiling_bytes=BUDGET)
    assert object_dir.stat().st_ino == inode
    assert not list(object_dir.iterdir())
    assert not list((value.root / "staging").iterdir())


@pytest.mark.parametrize("damage", ["earlier_file", "manifest", "files_ancestor"])
def test_late_mutation_during_collection_verification_is_rejected(directory, monkeypatch, damage):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    object_dir = value.root / "objects" / collection.digest
    weight_inode = (object_dir / "files" / "weights").stat().st_ino
    original = artifacts._hash_fd
    changed = False
    def raced(fd, size, budget, **kwargs):
        nonlocal changed
        result = original(fd, size, budget, **kwargs)
        if os.fstat(fd).st_ino == weight_inode and not changed:
            changed = True
            if damage == "files_ancestor":
                (object_dir / "files").rename(object_dir / "moved-files")
                (object_dir / "files").mkdir(mode=0o700)
            else:
                path = object_dir / ("files/notice" if damage == "earlier_file" else artifacts.MANIFEST_NAME)
                path.chmod(0o600)
                path.write_bytes(b"x" * path.stat().st_size)
                path.chmod(0o400)
        return result
    monkeypatch.setattr(artifacts, "_hash_fd", raced)
    with pytest.raises(artifacts.ArtifactError):
        value.verify(collection.digest)


def test_stream_consumer_must_discard_when_bytes_change_during_use(directory):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    selected = value.root / "objects" / collection.digest / "files" / "weights"
    with pytest.raises(artifacts.ArtifactError):
        with value.open_artifact(collection.digest, "weights") as fd:
            assert os.read(fd, 8)
            selected.chmod(0o600)
            selected.write_bytes(b"\x09" * selected.stat().st_size)
            selected.chmod(0o400)


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_verification_rejects_stop_after_final_hash_before_return(directory, monkeypatch, stop):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    canceled = threading.Event()
    clock = [100.0]
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: clock[0])
    original = value._verify_fd
    def stop_after_hash(*args):
        result = original(*args)
        canceled.set()
        clock[0] = 101.0
        return result
    monkeypatch.setattr(value, "_verify_fd", stop_after_hash)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled" if stop == "cancel" else "artifact_deadline_exceeded"):
        value.verify(collection.digest, cancel_event=canceled if stop == "cancel" else None, deadline=100.5)


def test_verification_rejects_cancellation_at_final_identity_completion(directory, monkeypatch):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    canceled = threading.Event()
    original = artifacts._same_directory
    checks = 0
    def stop_after_exit(*args):
        nonlocal checks
        result = original(*args)
        if args[1] == collection.digest:
            checks += 1
            if checks == 2:
                canceled.set()
        return result
    monkeypatch.setattr(artifacts, "_same_directory", stop_after_exit)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled"):
        value.verify(collection.digest, cancel_event=canceled)


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_artifact_descriptor_is_not_yielded_after_late_hash_stop(directory, monkeypatch, stop):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    inode = (value.root / "objects" / collection.digest / "files" / "weights").stat().st_ino
    canceled = threading.Event()
    clock = [100.0]
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: clock[0])
    original = artifacts._hash_fd
    checks, yielded = 0, False
    def stop_before_handoff(fd, *args, **kwargs):
        nonlocal checks
        result = original(fd, *args, **kwargs)
        if os.fstat(fd).st_ino == inode:
            checks += 1
            if checks == 2:
                canceled.set()
                clock[0] = 101.0
        return result
    monkeypatch.setattr(artifacts, "_hash_fd", stop_before_handoff)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_canceled" if stop == "cancel" else "artifact_deadline_exceeded"):
        with value.open_artifact(collection.digest, "weights", cancel_event=canceled if stop == "cancel" else None, deadline=100.5):
            yielded = True
    assert yielded is False


def test_stream_consumer_rejects_replaced_captured_ancestor(directory):
    value = store(directory)
    collection = value.capture(selection(directory / "source"), disk_ceiling_bytes=BUDGET)
    object_dir = value.root / "objects" / collection.digest
    with pytest.raises(artifacts.ArtifactError):
        with value.open_artifact(collection.digest, "weights") as fd:
            assert os.read(fd, 8)
            (object_dir / "files").rename(object_dir / "moved-files")
            (object_dir / "files").mkdir(mode=0o700)


def test_stream_copy_reads_are_bounded_independently_of_artifact_size(directory, monkeypatch):
    request = selection(directory / "source", b"\x08" * (4 * artifacts.COPY_CHUNK_BYTES + 1024))
    value = store(directory)
    original = artifacts.os.read
    reads = []
    def bounded(fd, count):
        assert 0 < count <= artifacts.COPY_CHUNK_BYTES
        reads.append(count)
        return original(fd, count)
    monkeypatch.setattr(artifacts.os, "read", bounded)
    result = value.capture(request, disk_ceiling_bytes=BUDGET)
    assert result.entries[-1].size > artifacts.COPY_CHUNK_BYTES
    assert len(reads) >= 20


def test_large_selection_does_not_scale_capture_memory_to_content_size(directory):
    request = selection(directory / "source")
    size = 16 * 1024 * 1024
    with Path(request.entries[0].path).open("wb") as handle:
        for _ in range(32):
            handle.write(b"\x08" * (512 * 1024))
    value = store(directory)
    tracemalloc.start()
    try:
        collection = value.capture(request, disk_ceiling_bytes=size + 2 * artifacts.MAX_CONTRACT_BYTES)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert collection.entries[-1].size == size
    assert peak < 6 * 1024 * 1024


def test_legacy_folder_source_identity_and_snapshot_are_unchanged(directory):
    source = directory / "text-source"
    source.mkdir()
    content = b"def tool():\n    return 1\n"
    (source / "tool.py").write_bytes(content)
    records = [{"path": "tool.py", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}]
    legacy_digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with ImportStore(directory / "legacy-state") as imports:
        first = imports.create(source, name="Legacy app")
        saved = first["source"]
        assert saved["files"] == records and saved["digest"] == legacy_digest
        assert saved["revision"] == "sha256:" + legacy_digest and "contract_version" not in saved
        artifacts_store = store(directory)
        artifacts_store.capture(selection(directory / "binary-selection"), disk_ceiling_bytes=BUDGET)
        assert imports.inspect(first["draft_id"])["source"] == saved
        second = imports.create(source, name="Still legacy")
        assert second["source"]["files"] == records and second["source"]["digest"] == legacy_digest
