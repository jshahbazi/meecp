from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from pydantic import ValidationError

from meecp import preparation_artifacts as artifacts
from meecp import preparation_capture as capture
from meecp.imports import ImportStore


BUDGET = 8 * 1024 * 1024


def selection(directory, content=b"\x08synthetic model bytes"):
    directory.mkdir(parents=True, exist_ok=True)
    weights = directory / "selected.onnx"
    weights.write_bytes(content)
    notice = directory / "NOTICE"
    notice.write_text("Synthetic license notice\n")
    return artifacts.AcquisitionRequestV1(entries=[
        {"name": "weights", "role": "model-weights", "path": str(weights), "format": "onnx", "licenses": ["notice"]},
        {"name": "notice", "role": "license", "path": str(notice), "format": "text"}])


def setup(directory, **kwargs):
    imports = ImportStore(directory / "state")
    objects = artifacts.ArtifactStore(directory / "objects")
    store = capture.ArtifactCaptureStore(imports, objects, **kwargs)
    def admit(target, collection, capture_id):
        if isinstance(target, capture.CreateImportTargetV1):
            draft = imports._admit_captured(collection, choices=target.model_dump(mode="json", exclude={"kind"}))
        else:
            draft = imports._admit_captured(collection, draft_id=target.import_id, revision=target.revision)
        return {"import_id": draft["draft_id"], "revision": draft["revision"]}
    return imports, objects, store, admit


@pytest.fixture
def directory(tmp_path):
    return tmp_path.resolve()


def policy(**kwargs):
    return capture.CapturePolicyV1(disk_ceiling_bytes=BUDGET, **kwargs)


def target(name="Synthetic model"):
    return capture.CreateImportTargetV1(name=name)


async def finished(coordinator, capture_id):
    await asyncio.wait_for(asyncio.shield(coordinator.tasks[capture_id]), 10)
    return coordinator.store.get(capture_id)


async def event_set(event):
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("owned test boundary not reached")


@pytest.mark.asyncio
async def test_actual_capture_admission_reference_source_removal_and_restart(directory):
    request = selection(directory / "source")
    imports, objects, store, admit = setup(directory)
    resources = []
    async def reserve(capture_id, value):
        assert store.recovery_policy(capture_id) == value
        assert store.get(capture_id)["reservation_retained"]
        resources.append(("reserve", capture_id))
    async def release(capture_id, *, ownership_settled):
        assert ownership_settled
        resources.append(("release", capture_id))
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit, reserve_resources=reserve, release_resources=release)
    queued = await worker.start(request, policy(), target(), "actual")
    result = await finished(worker, queued["capture_id"])
    assert result["state"] == "succeeded" and result["ownership_settled"] and not result["reservation_retained"]
    assert resources == [("reserve", queued["capture_id"]), ("release", queued["capture_id"])]
    encoded = json.dumps(result)
    assert str(directory) not in encoded and "nonce" not in encoded and "request" not in encoded
    digest = result["collection_digest"]
    draft = imports._get(result["result"]["import_id"])
    assert draft["source"]["kind"] == "captured_artifact_collection"
    assert "snapshot_path" not in draft["source"]
    assert store.references(digest) == [{"kind": "draft", "owner_id": draft["draft_id"], "owner_revision": 1}]
    for entry in request.entries:
        Path(entry.path).unlink()
    with store.open_artifact(digest, "weights") as fd:
        assert os.read(fd, 100) == b"\x08synthetic model bytes"
        assert len(store.references(digest)) == 2
    assert len(store.references(digest)) == 1
    await worker.close(); imports.close()
    imports, objects, store, admit = setup(directory)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    await worker.recover()
    assert store.get(result["capture_id"])["state"] == "succeeded"
    assert objects.verify(digest).digest == digest
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_idempotency_concurrent_starts_have_one_owned_copy(directory, monkeypatch):
    request = selection(directory / "source")
    imports, objects, store, admit = setup(directory)
    calls = 0
    original = objects.capture
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(objects, "capture", counted)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    results = await asyncio.gather(*(worker.start(request, policy(), target(), "same") for _ in range(8)))
    assert len({row["capture_id"] for row in results}) == 1
    result = await finished(worker, results[0]["capture_id"])
    assert result["state"] == "succeeded" and calls == 1
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_idempotency_conflict"):
        await worker.start(request, policy(), target("changed"), "same")
    assert len(imports.list()) == 1
    await worker.close(); imports.close()


@pytest.mark.parametrize("model,value", [
    (capture.CapturePolicyV1, {"disk_ceiling_bytes": True}),
    (capture.CapturePolicyV1, {"disk_ceiling_bytes": BUDGET, "memory_mb": 65}),
    (capture.CapturePolicyV1, {"disk_ceiling_bytes": BUDGET, "memory_mb": 64.0}),
    (capture.CapturePolicyV1, {"disk_ceiling_bytes": BUDGET, "schema_version": True}),
    (capture.CapturePolicyV1, {"disk_ceiling_bytes": BUDGET, "network": []}),
    (capture.ArtifactStoragePolicyV1, {"retention_seconds": True}),
    (capture.CreateImportTargetV1, {"name": "x", "requirements": {"unknown": True}}),
    (capture.AttachImportTargetV1, {"import_id": str(uuid.uuid4()), "revision": True}),
    (capture.ArtifactReferenceV1, {"kind": "draft", "owner_id": str(uuid.uuid4()), "owner_revision": 0}),
])
def test_closed_bounded_policies_targets_and_references(model, value):
    with pytest.raises((ValidationError, ValueError)):
        model.model_validate(value)


@pytest.mark.asyncio
async def test_copy_cancellation_joins_thread_repeated_cancel_and_keeps_cost(directory, monkeypatch):
    imports, objects, store, admit = setup(directory)
    entered, leave = threading.Event(), threading.Event()
    original = artifacts._copy_source
    def blocked(*args, **kwargs):
        entered.set(); leave.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(artifacts, "_copy_source", blocked)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    queued = await worker.start(selection(directory / "source"), policy(), target(), "cancel")
    await event_set(entered)
    cancel = asyncio.create_task(worker.cancel(queued["capture_id"]))
    await asyncio.sleep(0.03)
    cancel.cancel(); cancel.cancel()
    assert not cancel.done()
    assert store.get(queued["capture_id"])["reservation_retained"]
    assert worker.attempts[queued["capture_id"]].threads
    leave.set()
    result = await asyncio.wait_for(cancel, 10)
    assert result["state"] == "canceled" and result["ownership_settled"]
    assert not worker.attempts[queued["capture_id"]].threads
    assert not list((objects.root / "staging").iterdir())
    assert not imports.list()
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_cancel_before_coroutine_runs_settles_without_owner(directory):
    imports, objects, store, admit = setup(directory)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    await worker._serial.acquire()
    row = await worker.start(selection(directory / "source"), policy(), target(), "queued-cancel")
    result = await worker.cancel(row["capture_id"])
    assert result["state"] == "canceled" and result["ownership_settled"]
    worker._serial.release()
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_admission_cancellation_rolls_back_draft_and_reference(directory, monkeypatch):
    imports, objects, store, admit = setup(directory)
    entered, leave = threading.Event(), threading.Event()
    def blocked(*args):
        result = admit(*args)
        entered.set(); leave.wait(10)
        return result
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=blocked)
    row = await worker.start(selection(directory / "source"), policy(), target(), "admission-cancel")
    await event_set(entered)
    # Set host cancellation immediately; DB cancellation waits behind the
    # existing admission winner, which must check the event before commit.
    worker.attempts[row["capture_id"]].stop.set()
    worker.tasks[row["capture_id"]].cancel()
    await asyncio.sleep(0.03)
    assert not worker.tasks[row["capture_id"]].done()
    leave.set()
    result = await finished(worker, row["capture_id"])
    assert result["state"] == "canceled" and not imports.list()
    assert not store.references(result["collection_digest"])
    assert objects.verify(result["collection_digest"])
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_draft_revision_invalidation_stops_actual_copy_and_preserves_source(directory, monkeypatch):
    imports, objects, store, admit = setup(directory)
    source = directory / "app"; source.mkdir(); (source / "app.py").write_text("def invoke(): return 1\n")
    draft = imports.create(source, name="app")
    entered, leave = threading.Event(), threading.Event()
    original = artifacts._copy_source
    def blocked(*args, **kwargs):
        entered.set(); leave.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(artifacts, "_copy_source", blocked)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    row = await worker.start(selection(directory / "source"), policy(), capture.AttachImportTargetV1(import_id=draft["draft_id"], revision=1), "invalidate")
    await event_set(entered)
    revised = imports.revise(draft["draft_id"], 1, name="new")
    for _ in range(100):
        if worker.attempts[row["capture_id"]].stop.is_set(): break
        await asyncio.sleep(0.01)
    assert worker.attempts[row["capture_id"]].stop.is_set()
    assert not worker.tasks[row["capture_id"]].done()
    leave.set()
    result = await finished(worker, row["capture_id"])
    assert result["ownership_settled"] and result["state"] != "succeeded"
    assert imports._get(draft["draft_id"])["source"] == revised["source"]
    assert "captured_artifacts" not in imports._get(draft["draft_id"])
    await worker.close(); imports.close()


def test_queued_recovery_no_replay_and_unknown_stage_retains_cost(directory):
    imports, objects, store, admit = setup(directory)
    row = store.begin(selection(directory / "source"), policy(), target(), "orphan")
    stage = objects.root / "staging" / row["capture_id"]
    stage.mkdir(mode=0o700)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    asyncio.run(worker.recover())
    result = store.get(row["capture_id"])
    assert not result["ownership_settled"] and result["reservation_retained"]
    assert stage.exists() and not imports.list()
    assert store.unsettled_reservations()[0]["policy_digest"] == policy().digest
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_reconciliation_required"):
        asyncio.run(worker.close())
    imports.close()


@pytest.mark.asyncio
async def test_live_lease_recovery_does_not_touch_writer(directory):
    imports, objects, store, admit = setup(directory)
    row = store.begin(selection(directory / "source"), policy(), target(), "live")
    lease = store.create_owner(row["capture_id"])
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    await worker.recover()
    assert store.get(row["capture_id"])["reservation_retained"]
    assert store.get_record(row["capture_id"])["error_code"] == "artifact_capture_reconciliation_required"
    os.close(lease.fd)
    await worker.recover()
    assert store.get(row["capture_id"])["ownership_settled"]
    await worker.close(); imports.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash proof")
@pytest.mark.parametrize("boundary", ["before_stage", "copy", "candidate", "published"])
def test_actual_crash_recovery_exact_journal_no_replay(directory, boundary):
    request = selection(directory / "source")
    pid = os.fork()
    if pid == 0:
        try:
            imports, objects, store, admit = setup(directory)
            worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
            if boundary == "before_stage":
                original = store.record_owner
                def stop(*args): original(*args); os._exit(31)
                store.record_owner = stop
            elif boundary == "copy":
                original = artifacts._copy_source
                def stop(*args): original(*args); os._exit(32)
                artifacts._copy_source = stop
            elif boundary == "candidate":
                original = store.record_candidate
                def stop(*args): original(*args); os._exit(33)
                store.record_candidate = stop
            else:
                original = artifacts._publish_directory
                def stop(*args): original(*args); os._exit(34)
                artifacts._publish_directory = stop
            async def run():
                row = await worker.start(request, policy(), target(), "crash")
                await worker.tasks[row["capture_id"]]
            asyncio.run(run())
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) in {31, 32, 33, 34}
    imports, objects, store, admit = setup(directory)
    before = store.list_summaries()[0]
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    asyncio.run(worker.recover())
    result = store.get(before["capture_id"])
    assert result["state"] == "interrupted" and result["ownership_settled"]
    assert not list((objects.root / "staging").iterdir()) and not imports.list()
    assert not [path for path in store.owners.iterdir() if path.name.endswith(".owner")]
    if boundary == "published":
        assert objects.verify(result["collection_digest"])
        assert not store.references(result["collection_digest"])
    else:
        assert not list((objects.root / "objects").iterdir())
    asyncio.run(worker.close()); imports.close()


@pytest.mark.asyncio
async def test_reference_transfer_revision_expiry_and_job_pin_gc(directory):
    imports, objects, store, admit = setup(directory, storage_policy=capture.ArtifactStoragePolicyV1(retention_seconds=0))
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    row = await worker.start(selection(directory / "source"), policy(), target(), "references")
    result = await finished(worker, row["capture_id"])
    digest, result_id = result["collection_digest"], result["result"]["import_id"]
    reference = capture.ArtifactReferenceV1(kind="job", owner_id=str(uuid.uuid4()))
    store.acquire_reference(digest, reference)
    revised = imports.revise(result_id, 1, name="new name")
    store.reconcile_draft_references()
    assert {ref["owner_revision"] for ref in store.references(digest) if ref["kind"] == "draft"} == {revised["revision"]}
    imports.cancel(result_id, revised["revision"])
    store.reconcile_draft_references()
    assert store.references(digest) == [reference.model_dump()]
    with pytest.raises(artifacts.ArtifactError, match="artifact_collection_retained"):
        store.collect(digest)
    store.release_reference(digest, reference)
    store.collect(digest)
    assert not list((objects.root / "objects").iterdir())
    with pytest.raises(artifacts.ArtifactError, match="artifact_object_unavailable"):
        store.acquire_reference(digest, reference)
    await worker.close(); imports.close()


def test_storage_limit_and_forged_recovery_policy_fail_before_copy(directory):
    imports, objects, store, admit = setup(directory, storage_policy=capture.ArtifactStoragePolicyV1(max_retained_bytes=BUDGET))
    row = store.begin(selection(directory / "source"), policy(), target(), "one")
    with pytest.raises(artifacts.ArtifactError, match="artifact_storage_limit"):
        store.begin(selection(directory / "other"), policy(), target(), "two")
    with imports._transaction():
        record = store._get(row["capture_id"])
        record["policy"]["disk_ceiling_bytes"] -= 1
        store._save(record)
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_scope_mismatch"):
        store.recovery_policy(row["capture_id"])
    assert not list((objects.root / "staging").iterdir())
    imports.close()


@pytest.mark.asyncio
async def test_status_list_omits_target_intent_and_private_requests(directory):
    imports, objects, store, admit = setup(directory)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    row = await worker.start(selection(directory / "source"), policy(),
                             capture.CreateImportTargetV1(name="x", intent="private synthetic intent"), "summary")
    await finished(worker, row["capture_id"])
    assert store.list_summaries()[0]["target"] == {"kind": "create-import"}
    assert "request" not in store.recovery_records()[0]
    assert store.get(row["capture_id"])["target"]["intent"] == "private synthetic intent"
    # Completed metadata no longer causes a private request decode on recover.
    original = store.get_record
    def forbidden(capture_id):
        pytest.fail("settled historical private request was loaded")
    store.get_record = forbidden
    await worker.recover()
    store.get_record = original
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_storage_counts_committed_bytes_once_after_duplicate_capture(directory):
    imports, objects, store, admit = setup(directory)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    request = selection(directory / "source")
    first = await worker.start(request, policy(), target(), "first")
    first = await finished(worker, first["capture_id"])
    second = await worker.start(request, policy(), target(), "second")
    second = await finished(worker, second["capture_id"])
    assert first["collection_digest"] == second["collection_digest"]
    assert store.db.execute("SELECT count(*) FROM captured_artifact_objects").fetchone()[0] == 1
    assert len(store.references(first["collection_digest"])) == 2
    assert not store.unsettled_reservations()
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_close_repeated_cancellation_joins_owned_copy_and_blocks_new_start(directory, monkeypatch):
    imports, objects, store, admit = setup(directory)
    entered, leave = threading.Event(), threading.Event()
    original = artifacts._copy_source
    def blocked(*args, **kwargs):
        entered.set(); leave.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(artifacts, "_copy_source", blocked)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    request = selection(directory / "source")
    row = await worker.start(request, policy(), target(), "closing")
    await event_set(entered)
    closing = asyncio.create_task(worker.close())
    await asyncio.sleep(0.03); closing.cancel(); closing.cancel()
    assert not closing.done() and store.get(row["capture_id"])["reservation_retained"]
    with pytest.raises(artifacts.ArtifactError, match="artifact_capture_closing"):
        await worker.start(request, policy(), target(), "after-close")
    leave.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await worker.close()
    assert store.get(row["capture_id"])["ownership_settled"]
    imports.close()


@pytest.mark.asyncio
async def test_http_start_cancellation_schedules_durable_queued_work(directory, monkeypatch):
    imports, objects, store, admit = setup(directory)
    entered, leave = threading.Event(), threading.Event()
    original = store.begin
    def blocked(*args):
        result = original(*args)
        entered.set(); leave.wait(10)
        return result
    monkeypatch.setattr(store, "begin", blocked)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    started = asyncio.create_task(worker.start(selection(directory / "source"), policy(), target(), "http-cancel"))
    await event_set(entered)
    started.cancel(); started.cancel()
    assert not started.done()
    leave.set()
    with pytest.raises(asyncio.CancelledError):
        await started
    row = store.list_summaries()[0]
    result = await finished(worker, row["capture_id"])
    assert result["state"] == "succeeded" and result["ownership_settled"]
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_deadline_in_queue_does_not_create_stage(directory):
    clock = [time.time()]
    imports, objects, store, admit = setup(directory, clock=lambda: clock[0])
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    await worker._serial.acquire()
    row = await worker.start(selection(directory / "source"), policy(wall_seconds=1), target(), "deadline")
    clock[0] += 2
    worker._serial.release()
    result = await finished(worker, row["capture_id"])
    assert result["state"] == "failed" and result["error_code"] == "artifact_deadline_exceeded"
    assert result["ownership_settled"] and not list((objects.root / "staging").iterdir())
    await worker.close(); imports.close()


@pytest.mark.asyncio
async def test_resource_failure_is_sanitized_and_creates_no_owner(directory):
    imports, objects, store, admit = setup(directory)
    async def reserve(*args):
        raise RuntimeError("synthetic exception payload must not be persisted")
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit, reserve_resources=reserve)
    row = await worker.start(selection(directory / "source"), policy(), target(), "resources")
    result = await finished(worker, row["capture_id"])
    assert result["error_code"] == "artifact_capture_failed" and result["ownership_settled"]
    assert not list(store.owners.iterdir()) and not imports.list()
    await worker.close(); imports.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash proof")
def test_crash_inside_actual_admission_rolls_back_draft_and_reference(directory):
    request = selection(directory / "source")
    pid = os.fork()
    if pid == 0:
        try:
            imports, objects, store, admit = setup(directory)
            def crash(*args):
                admit(*args)
                os._exit(41)
            worker = capture.ArtifactCaptureCoordinator(store, admit_target=crash)
            async def run():
                row = await worker.start(request, policy(), target(), "admission-crash")
                await worker.tasks[row["capture_id"]]
            asyncio.run(run())
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 41
    imports, objects, store, admit = setup(directory)
    row = store.list_summaries()[0]
    assert not imports.list() and not store.references(row["collection_digest"])
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    asyncio.run(worker.recover())
    assert store.get(row["capture_id"])["ownership_settled"]
    assert objects.verify(row["collection_digest"])
    asyncio.run(worker.close()); imports.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash proof")
def test_actual_reader_crash_reclaims_only_lease_proven_reference(directory):
    imports, objects, store, admit = setup(directory)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    async def build():
        row = await worker.start(selection(directory / "source"), policy(), target(), "reader-crash")
        return await finished(worker, row["capture_id"])
    result = asyncio.run(build())
    digest = result["collection_digest"]
    pid = os.fork()
    if pid == 0:
        try:
            child_imports, _, child_store, _ = setup(directory)
            with child_store.open_artifact(digest, "weights") as descriptor:
                assert os.read(descriptor, 1) == b"\x08"
                os._exit(42)
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert {row["kind"] for row in store.references(digest)} == {"draft", "read"}
    store.recover_readers()
    assert {row["kind"] for row in store.references(digest)} == {"draft"}
    assert not store.db.execute("SELECT * FROM captured_artifact_readers").fetchall()
    asyncio.run(worker.close()); imports.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX crash proof")
@pytest.mark.parametrize("boundary", ["intent", "quarantine", "partial_delete"])
def test_actual_gc_crash_resumes_only_recorded_quarantine(directory, boundary):
    imports, objects, store, admit = setup(directory, storage_policy=capture.ArtifactStoragePolicyV1(retention_seconds=0))
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    async def build():
        row = await worker.start(selection(directory / "source"), policy(), target(), "gc-crash")
        return await finished(worker, row["capture_id"])
    result = asyncio.run(build())
    digest = result["collection_digest"]
    store.release_reference(digest, capture.ArtifactReferenceV1(kind="draft", owner_id=result["result"]["import_id"], owner_revision=1))
    pid = os.fork()
    if pid == 0:
        try:
            child_imports, _, child_store, _ = setup(directory, storage_policy=capture.ArtifactStoragePolicyV1(retention_seconds=0))
            if boundary == "intent":
                child_store._finish_gc = lambda *args: os._exit(51)
            elif boundary == "quarantine":
                original = capture._publish_directory
                def crash(*args): original(*args); os._exit(52)
                capture._publish_directory = crash
            else:
                original = os.unlink
                def crash(*args, **kwargs): original(*args, **kwargs); os._exit(53)
                capture.os.unlink = crash
            child_store.collect(digest)
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) in {51, 52, 53}
    store.recover_gc()
    assert not store.db.execute("SELECT * FROM captured_artifact_gc").fetchall()
    assert not store.db.execute("SELECT * FROM captured_artifact_objects").fetchall()
    assert not list((objects.root / "objects").iterdir())
    assert not list((objects.root / "quarantine").iterdir())
    asyncio.run(worker.close()); imports.close()


@pytest.mark.asyncio
async def test_live_reader_keeps_database_owned_until_context_exits(directory):
    imports, objects, store, admit = setup(directory)
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    row = await worker.start(selection(directory / "source"), policy(), target(), "live-reader")
    result = await finished(worker, row["capture_id"])
    with store.open_artifact(result["collection_digest"], "weights") as fd:
        assert os.read(fd, 1) == b"\x08"
        with pytest.raises(artifacts.ArtifactError, match="artifact_capture_reconciliation_required"):
            await worker.close()
        assert store.reader_recovery_records()
        assert store.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    await worker.close()
    assert not store.reader_recovery_records()
    imports.close()


@pytest.mark.asyncio
async def test_old_gc_finisher_cannot_remove_new_identical_publication(directory, monkeypatch):
    imports, objects, store, admit = setup(directory, storage_policy=capture.ArtifactStoragePolicyV1(retention_seconds=0))
    worker = capture.ArtifactCaptureCoordinator(store, admit_target=admit)
    request = selection(directory / "source")
    row = await worker.start(request, policy(), target(), "old-object")
    result = await finished(worker, row["capture_id"])
    digest = result["collection_digest"]
    store.release_reference(digest, capture.ArtifactReferenceV1(kind="draft", owner_id=result["result"]["import_id"], owner_revision=1))
    intents = []
    original = store._finish_gc
    monkeypatch.setattr(store, "_finish_gc", lambda intent: intents.append(intent))
    store.collect(digest)
    monkeypatch.setattr(store, "_finish_gc", original)
    original(intents[0])
    newer = await worker.start(request, policy(), target(), "new-object")
    newer = await finished(worker, newer["capture_id"])
    assert newer["state"] == "succeeded"
    original(intents[0])
    assert objects.verify(digest)
    assert len(store.references(digest)) == 1
    assert store.db.execute("SELECT count(*) FROM captured_artifact_objects").fetchone()[0] == 1
    await worker.close(); imports.close()
