"""Development barriers must retain exact descriptor/child ownership."""
import asyncio
import importlib
import os
from pathlib import Path
import json
import sqlite3

import pytest


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_recipe_lifecycle")


def test_barrier_closes_only_its_descriptors_and_releases_once(lifecycle):
    barrier = lifecycle.Barrier()
    unrelated_read, unrelated_write = os.pipe()
    try:
        barrier.release()
        barrier.release()
        assert os.read(barrier.control_read, 1) == b"G"
        barrier.close()
        barrier.close()
        assert all(getattr(barrier, name) is None for name in
            ("control_read", "control_write", "status_read", "status_write"))
        os.write(unrelated_write, b"X")
        assert os.read(unrelated_read, 1) == b"X"
    finally:
        barrier.close()
        os.close(unrelated_read)
        os.close(unrelated_write)


@pytest.mark.asyncio
async def test_native_completion_before_barrier_has_bounded_diagnostic(lifecycle, tmp_path):
    receipt = tmp_path / "receipt.json"
    lifecycle.write_private_json(receipt, {"stage": "preparation_review", "error_code": "source_changed",
        "untrusted_payload": "do not retain this field"})
    child = asyncio.create_task(asyncio.sleep(0))
    await child
    barrier = lifecycle.Barrier()
    try:
        with pytest.raises(lifecycle.onboarding.AcceptanceError) as captured:
            await barrier.await_hit(child, receipt)
        assert captured.value.code == "native_before_barrier_failed"
        assert captured.value.observation == {"native_stage": "preparation_review",
            "native_error_code": "source_changed", "native_receipt_sha256": lifecycle.digest(receipt)}
    finally:
        barrier.close()


@pytest.mark.asyncio
async def test_barrier_hit_precedes_native_completion(lifecycle, tmp_path):
    barrier = lifecycle.Barrier()
    child = asyncio.create_task(asyncio.sleep(30))
    try:
        os.write(barrier.status_write, b"H")
        await barrier.await_hit(child, tmp_path / "unused.json", timeout=0.1)
        assert not child.done()
    finally:
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        barrier.close()


@pytest.mark.asyncio
async def test_canceling_joiner_still_joins_owned_request(lifecycle):
    released = asyncio.Event()
    completed = asyncio.Event()
    async def request():
        await released.wait()
        completed.set()
    owned = asyncio.create_task(request())
    joiner = asyncio.create_task(lifecycle.join_owned(owned))
    await asyncio.sleep(0)
    joiner.cancel()
    await asyncio.sleep(0)
    assert not owned.done() and not joiner.done()
    released.set()
    await joiner
    assert completed.is_set() and owned.done()


@pytest.mark.asyncio
async def test_terminal_row_waits_for_physical_resource_settlement(lifecycle, tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    workspace = state / "workspace"
    workspace.mkdir(mode=0o700)
    reservations = state / "preparation-reservations"
    reservations.mkdir(mode=0o700)
    reservation = reservations / "synthetic.json"
    lifecycle.write_private_json(reservation, {"synthetic": True})
    connection = sqlite3.connect(state / "imports.sqlite")
    connection.execute("CREATE TABLE preparation_ownership(job_id TEXT)")
    connection.close()
    (state / "imports.sqlite").chmod(0o600)
    job = {"job_id": "synthetic", "state": "succeeded", "ownership_settled": True,
        "reservation_retained": False, "workspace": str(workspace)}
    observed = asyncio.Event()
    harness = lifecycle.LifecycleHarness(tmp_path, tmp_path, tmp_path, {"safe_to_remove": True})
    async def jobs(*_):
        return [job]
    async def request(*_):
        observed.set()
        return job
    monkeypatch.setattr(harness, "jobs", jobs)
    monkeypatch.setattr(harness, "request", request)
    task = asyncio.create_task(harness.settled_job(state, "synthetic"))
    await observed.wait()
    assert not task.done()
    reservation.unlink()
    workspace.rmdir()
    assert await task is job


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_receipt_hold_preserves_inode_and_refuses_replacement(lifecycle, tmp_path, replacement):
    from mcpme.ownership_receipts import prepare_receipt, acknowledge_nonlaunch
    state = tmp_path / "state"
    workspace = state / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    descriptor, metadata = prepare_receipt(workspace, "a" * 32)
    # Unit-only owned nonlaunch proof; installed cases require real native C
    # completion from their actual helper. No process is claimed by this test.
    try:
        acknowledge_nonlaunch(descriptor, metadata["instance_id"], metadata["nonce"])
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(state / "imports.sqlite")
    try:
        connection.execute("CREATE TABLE preparation_ownership(job_id TEXT,data TEXT)")
        connection.execute("INSERT INTO preparation_ownership VALUES (?,?)", ("synthetic", json.dumps(metadata)))
        connection.commit()
    finally:
        connection.close()
    source = workspace / "ownership-receipts" / (metadata["instance_id"] + ".receipt")
    original = source.stat().st_dev, source.stat().st_ino
    hold = lifecycle.ReceiptHold(state, {"job_id": "synthetic", "workspace": str(workspace)})
    try:
        assert not source.exists()
        await hold.completed()
        if replacement:
            source.write_bytes(b"synthetic replacement")
            source.chmod(0o600)
            with pytest.raises(FileExistsError):
                hold.restore()
            assert source.read_bytes() == b"synthetic replacement" and hold.withheld
        else:
            hold.restore()
            assert (source.stat().st_dev, source.stat().st_ino) == original
            assert source.stat().st_nlink == 1 and not hold.withheld
    finally:
        hold.close()
