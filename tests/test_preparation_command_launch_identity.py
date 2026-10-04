"""Deterministic candidate/authority changes at the actual native launch fence."""
from __future__ import annotations

import asyncio
import inspect
import os
import threading

import pytest

from mcpme.preparation_contracts import PreparationError
from test_host_command import native, pytestmark
from test_preparation_command_managed import intake
from test_preparation_command_measured import measured_admission, measured_owner


def owner_at(tmp_path, native, measured_admission, measured):
    if measured:
        return measured_owner(tmp_path, native, measured_admission, "echo")
    return intake(tmp_path, native, "echo")


def executable(owner):
    return owner.package.root / owner.package.manifest.command.commands["run"].executable


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [False, True])
@pytest.mark.parametrize("mutation", ["executable", "restore_bytes_mtime", "notice", "membership"])
async def test_mutation_after_final_full_hash_creates_no_native_process(tmp_path, native, measured_admission, monkeypatch, measured, mutation):
    imports, store, owner = owner_at(tmp_path, native, measured_admission, measured)
    ready, release = threading.Event(), threading.Event()
    original = owner._verify_package
    calls, created = 0, []
    def checked():
        nonlocal calls
        valid = original()
        calls += 1
        if calls == 3:
            assert valid is True
            ready.set()
            assert release.wait(3)
        return valid
    monkeypatch.setattr(owner, "_verify_package", checked)
    def forbidden(*args, **kwargs):
        created.append(True)
        raise AssertionError("unexpected_native_creation")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    task = None
    try:
        handle = await owner.session(owner.package)
        task = asyncio.create_task(owner.invoke(handle, "run", {"ok": True}, 1))
        async with asyncio.timeout(3):
            while not ready.is_set():
                await asyncio.sleep(.001)
        target = executable(owner)
        if mutation == "notice":
            target = owner.package.root / owner.package.manifest.licenses[0]
        if mutation == "membership":
            (target.parent / "unexpected").write_bytes(b"bounded fixture")
        else:
            original_bytes, original_stat = target.read_bytes(), target.stat()
            target.write_bytes(original_bytes + b"\0")
            if mutation == "restore_bytes_mtime":
                target.write_bytes(original_bytes)
                os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, Exception)
        assert not created and not owner.executor.journal["entries"]
        await owner.close()
        assert owner.confirm_completed()
        assert not store.pending_ownership(owner.job_id)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await owner.close()
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [False, True])
@pytest.mark.parametrize("mutation", ["candidate", "helper", "cancel", "fixture", "attempt", "session", "deadline"])
async def test_final_native_fence_composes_original_bytes_and_current_authority(tmp_path, native, measured_admission, monkeypatch, measured, mutation):
    imports, store, owner = owner_at(tmp_path, native, measured_admission, measured)
    created, native_checks = [], []
    helper_bytes = None
    original = owner.executor.before_native_create
    def changed(spec):
        nonlocal helper_bytes
        native_checks.append(True)
        # All full-byte reads have already returned. The native callback must
        # use its original baseline, never recapture or hash selected bytes.
        monkeypatch.setattr("mcpme.preparation_command_owner.validate_package",
            lambda *args: pytest.fail("full_byte_hash_at_native_fence"))
        if mutation == "candidate":
            target = executable(owner)
            target.write_bytes(target.read_bytes() + b"\0")
        elif mutation == "helper":
            helper_bytes = owner.executor.helper.read_bytes()
            owner.executor.helper.write_bytes(helper_bytes + b"\0")
        elif mutation == "cancel":
            store.cancel(owner.job_id)
        elif mutation == "fixture":
            owner.descriptor.validation[0].expected["ok"] = False
        elif mutation == "attempt":
            owner.metadata["attempt_id"] = "f" * 32
        elif mutation == "session":
            owner.live[spec.authority_digest][1].closed = True
        else:
            owner.deadline = 0
        return original(spec)
    owner.executor.before_native_create = changed
    def forbidden(*args, **kwargs):
        created.append(True)
        raise AssertionError("unexpected_native_creation")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    try:
        handle = await owner.session(owner.package)
        with pytest.raises(Exception):
            await owner.invoke(handle, "run", {"ok": True}, 1)
        assert native_checks == [True] and not created
        entry, = owner.executor.journal["entries"]
        assert entry["state"] == "settled"
        receipt = owner.executor._receipt_path(entry).read_bytes()
        offset = 360 if measured else 296
        assert receipt[offset:offset + 1] == b"N"
        assert owner.executor.reconcile()["pending"] == 0
        if mutation == "attempt":
            owner.metadata["attempt_id"] = owner._metadata_baseline["attempt_id"]
        await owner.close()
        assert owner.confirm_completed()
        if mutation == "cancel":
            assert store.settle(owner.job_id)["state"] == "canceled"
    finally:
        if helper_bytes is not None:
            owner.executor.helper.write_bytes(helper_bytes)
        await owner.close()
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [False, True])
@pytest.mark.parametrize("unsupported", ["truthy", "coroutine", "task"])
async def test_native_callback_requires_sync_literal_true_and_joins_rejected_work(tmp_path, native, measured_admission, monkeypatch, measured, unsupported):
    imports, store, owner = owner_at(tmp_path, native, measured_admission, measured)
    returned = []
    async def async_value():
        return True
    def invalid(spec):
        value = 1 if unsupported == "truthy" else async_value()
        if unsupported == "task":
            value = asyncio.create_task(value)
        returned.append(value)
        return value
    owner.executor.before_native_create = invalid
    created = []
    def forbidden(*args, **kwargs):
        created.append(True)
        raise AssertionError("unexpected_native_creation")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    try:
        handle = await owner.session(owner.package)
        with pytest.raises(Exception):
            await owner.invoke(handle, "run", {"ok": True}, 1)
        assert len(returned) == 1 and not created
        if unsupported == "coroutine":
            assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED
        elif unsupported == "task":
            assert returned[0].done() and returned[0].cancelled()
        assert owner.executor.reconcile()["pending"] == 0
        await owner.close()
        assert owner.confirm_completed()
    finally:
        await owner.close()
        imports.close()
