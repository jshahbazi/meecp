"""Development barriers must retain exact descriptor/child ownership."""
import asyncio
import importlib
import os
from pathlib import Path
import json
import sqlite3
import socket
import logging
import sys
import threading
import tempfile
from types import SimpleNamespace

import pytest


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_recipe_lifecycle")


@pytest.mark.parametrize("registry", ["current", "old", "duplicate"])
def test_barrier_factory_admits_only_registered_current_python_reference(lifecycle, monkeypatch, tmp_path, registry):
    from meecp.preparation_resolved_python import ResolvedPythonComponent
    from meecp.supervisor import Supervisor

    reference = ResolvedPythonComponent().reference.key
    assert reference[0] == "resolved-python-v1" and reference[1] != "1.0.0"
    observed, threads = {}, []
    thread_type = threading.Thread

    def thread(*args, **kwargs):
        owned = thread_type(*args, **kwargs)
        threads.append(owned)
        return owned

    async def serve(state, reaper, parent, **factories):
        if registry == "current":
            host = Supervisor(state, reaper, **factories)
            try:
                observed["enabled"] = host.preparation_runner.enabled
                observed["admitted"] = host.preparation_runner.enabled_families
                observed["registered"] = frozenset(host.preparations.families)
            finally:
                await host.close()
        else:
            keys = [reference, reference] if registry == "duplicate" else [
                (reference[0], "1.0.0", reference[2])]
            factories["preparation_runner_factory"](SimpleNamespace(families=keys))

    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    try:
        os.write(control_write, b"G")
        monkeypatch.setattr("meecp.supervisor.serve", serve)
        monkeypatch.setattr(threading, "Thread", thread)
        monkeypatch.setattr(logging.root.manager, "disable", logging.root.manager.disable)
        backend = str(Path(__file__).resolve().parents[1] / "backend")
        monkeypatch.setattr(sys, "argv", ["acceptance", backend, backend, str(tmp_path / "state"),
            str(tmp_path / "unused-reaper"), "0", "factory_contract", str(control_read), str(status_write)])
        monkeypatch.setattr(sys, "path", list(sys.path))
        if registry == "current":
            exec(lifecycle.BARRIER_HOST, {})
            assert observed["enabled"] is True
            assert observed["admitted"] == frozenset({reference})
            assert reference in observed["registered"] and observed["registered"] - {reference}
        else:
            with pytest.raises(ValueError, match="reviewed_python_family_required"):
                exec(lifecycle.BARRIER_HOST, {})
    finally:
        for owned in threads:
            owned.join(3)
            assert not owned.is_alive()
        # The listener owns and closes control_read after consuming its byte.
        if not threads:
            os.close(control_read)
        for descriptor in (control_write, status_read, status_write):
            os.close(descriptor)


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
    from meecp.ownership_receipts import prepare_receipt, acknowledge_nonlaunch
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


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "mode"])
def test_native_manager_metadata_rejects_unsafe_connection_records(lifecycle, tmp_path, mutation):
    connection = tmp_path / "manager-connection.json"
    lifecycle.write_private_json(connection, {"synthetic": True})
    if mutation == "symlink":
        target = tmp_path / "original.json"
        connection.rename(target)
        connection.symlink_to(target)
    elif mutation == "hardlink":
        os.link(connection, tmp_path / "second.json")
    else:
        connection.chmod(0o644)
    with pytest.raises((lifecycle.onboarding.AcceptanceError, OSError)):
        lifecycle.manager_file_identity(connection)


def test_native_fixture_binds_fresh_nonce_and_manager_generation(lifecycle, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    connection = state / "manager-connection.json"
    lifecycle.write_private_json(connection, {"synthetic": True})
    first_identity = lifecycle.manager_file_identity(connection)
    fixture, receipt = lifecycle.native_management_fixture(tmp_path, state, [{"synthetic": True}])
    data = lifecycle.private_json(fixture)
    marker = lifecycle.private_json(state / ("native-recipes-" + data["fixture_nonce"] + ".json"))
    assert marker["manager_file"] == first_identity and marker["fixture_nonce"] == data["fixture_nonce"]
    assert data["output_path"] == str(receipt) and not receipt.exists()
    second, _ = lifecycle.native_management_fixture(tmp_path, state, [{"synthetic": True}])
    assert lifecycle.private_json(second)["fixture_nonce"] != data["fixture_nonce"]
    replacement = state / "replacement.json"
    lifecycle.write_private_json(replacement, {"synthetic": True})
    replacement.replace(connection)
    assert lifecycle.manager_file_identity(connection) != first_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["extra", "changed", "fifo", "socket", "declared_fifo"])
async def test_payload_rejection_precedes_target_imports(lifecycle, tmp_path, monkeypatch, mutation):
    host = tmp_path / "sealed-host"
    host.mkdir(mode=0o700)
    payload = host / "payload.py"
    payload.write_bytes(b"synthetic inert bytes")
    artifacts = {"payload.py": lifecycle.digest(payload)}
    if mutation == "declared_fifo":
        artifacts["unlisted-pipe"] = "a" * 64
    lifecycle.write_private_json(host / "build.json", {"source_tree_dirty": False,
        "source_commit": "a" * 40, "artifact_sha256": artifacts})
    listener = None
    socket_root = None
    if mutation == "extra":
        (host / "extra.py").write_bytes(b"synthetic unlisted bytes")
    elif mutation == "changed":
        payload.write_bytes(b"synthetic substituted bytes")
    elif mutation in {"fifo", "declared_fifo"}:
        os.mkfifo(host / "unlisted-pipe", 0o600)
    else:
        listener = socket.socket(socket.AF_UNIX)
        # macOS Unix socket names are shorter than pytest's descriptive paths.
        socket_root = Path(tempfile.mkdtemp(prefix="meecp-a0r-sock-", dir="/private/tmp"))
        listener.bind(str(socket_root / "socket"))
        (socket_root / "socket").rename(host / "socket")
    imported = []
    monkeypatch.setattr(lifecycle, "_host_imports", lambda *_: imported.append(True))
    try:
        with pytest.raises(lifecycle.onboarding.AcceptanceError) as captured:
            await lifecycle.verify(host, tmp_path / "unused-native", {})
        assert captured.value.code == ({"extra": "relocated_host_membership_mismatch",
            "changed": "relocated_host_artifact_mismatch"}.get(mutation, "relocated_host_unsafe_member"))
        assert imported == []
    finally:
        if listener is not None:
            listener.close()
        if socket_root is not None:
            socket_root.rmdir()
