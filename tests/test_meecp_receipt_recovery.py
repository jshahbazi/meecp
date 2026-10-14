"""Restart recovery uses exact native receipts without requiring a Mac reboot."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid

import pytest

from meecp.boot_session import BootSession
from meecp.contracts import validate_package
from meecp.ownership_receipts import prepare_receipt, verify_receipt
from meecp.private import read_private_json, write_private_json
from meecp.processes import OwnershipLost, ProcessError
from meecp.registry import RegistryError
from meecp.supervisor import HostError, Supervisor
from test_meecp_host import finish, prepared, tool_call  # Reuse the actual signed native fixture.
from test_meecp_packages import make_package


ROOT = Path(__file__).resolve().parents[1]
PRIOR_BOOT = "11111111-1111-4111-8111-111111111111"
CURRENT_BOOT = "22222222-2222-4222-8222-222222222222"

# The subprocess owns its Supervisor and every native process. SIGKILL prevents
# Python cleanup from running, leaving only the native completion capabilities.
HOST_CHILD = """
import asyncio, sys
from pathlib import Path
from meecp.private import write_private_json
from meecp.supervisor import Supervisor

async def main():
    state, reaper, package, candidate, ready, kind, stage, start = sys.argv[1:]
    host = Supervisor(Path(state), Path(reaper))
    start = start == 'True'
    async def snapshot(operation_id=None):
        slot = host.registry.list_slots()[0]
        data = host.data_directory(slot)
        (data / 'accepted.bin').write_bytes(b'accepted before supervisor exit')
        (data / 'accepted.bin').chmod(0o600)
        processes = list(host.owner(slot.id).instances.values())
        write_private_json(Path(ready), {
            'slot': slot.to_dict(), 'ledger': host.ledger(slot.id),
            'operation_id': operation_id,
            'processes': [{'instance_id': p.instance_id, 'pid': p.pid,
                           'reaper_pid': p.reaper_pid} for p in processes],
        })
        await asyncio.Event().wait()
    async def install_checkpoint(operation_id, current):
        if kind == 'install' and current == stage:
            await snapshot(operation_id)
    host.install_checkpoint = install_checkpoint
    plan = host.plan_install({'package': package, 'name': 'Receipt fixture',
                              'start': start, 'start_policy': 'manual'})
    operation = await host.apply_install(plan['plan_id'])
    while host.registry.get_operation(operation['operation_id']).status in {'pending', 'running'}:
        await asyncio.sleep(0.01)
    assert host.registry.get_operation(operation['operation_id']).status == 'completed'
    if kind == 'upgrade':
        async def upgrade_checkpoint(operation_id, current):
            if current == stage:
                await snapshot(operation_id)
        host.upgrade_checkpoint = upgrade_checkpoint
        slot = host.registry.list_slots()[0]
        plan = await host.plan_upgrade({'slot_id': slot.id, 'package': candidate})
        await host.apply_upgrade(plan['plan_id'])
        await asyncio.Event().wait()
    await snapshot()

asyncio.run(main())
"""


@pytest.fixture(scope="module")
def receipt_packages(prepared, tmp_path_factory):
    package, reaper = prepared
    candidate = tmp_path_factory.mktemp("receipt-candidate").resolve() / "package"
    shutil.copytree(package, candidate)
    manifest_path = candidate / "meecp.package.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest))
    lock_path = candidate / "meecp.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["version"] = manifest["version"]
    lock["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock))
    validate_package(candidate)
    return package, candidate, reaper


def receipt_path(slot_directory: Path, metadata: dict) -> Path:
    return slot_directory / "ownership-receipts" / (metadata["instance_id"] + ".receipt")


async def killed_host(tmp_path: Path, receipt_packages, *, start=True, kind="normal", stage=""):
    package, candidate, reaper = receipt_packages
    state = tmp_path.resolve() / "state"
    ready = tmp_path / "ready.json"
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "backend"), PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.Popen([sys.executable, "-c", HOST_CHILD, str(state), str(reaper), str(package),
        str(candidate), str(ready), kind, stage, str(start)], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                assert child.poll() is None, "synthetic host exited before its durable checkpoint"
                await asyncio.sleep(0.01)
        snapshot = read_private_json(ready)
        assert snapshot["ledger"] and snapshot["processes"]
        slot_directory = state / "slots" / snapshot["slot"]["slot_id"]
        assert all(not verify_receipt(slot_directory, record["completion"])
                   for record in snapshot["ledger"].values())
        child.kill()
        await asyncio.to_thread(child.wait, timeout=5)
        async with asyncio.timeout(10):
            while not all(verify_receipt(slot_directory, record["completion"])
                          for record in snapshot["ledger"].values()):
                await asyncio.sleep(0.01)
        return state, reaper, snapshot
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)


@pytest.mark.parametrize("start", [False, True])
async def test_sigkill_receipts_reconcile_running_and_stopped_manual_slots(tmp_path, receipt_packages, monkeypatch, start):
    state, reaper, before = await killed_host(tmp_path, receipt_packages, start=start)
    host = Supervisor(state, reaper)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        slot_id = before["slot"]["slot_id"]
        assert all(record["completion"]["schema_version"] == 2
                   and record["boot_id"] == host.boot_session.identity
                   for record in before["ledger"].values())
        await host.initialize()
        slot = host.registry.get_slot(slot_id)
        assert slot.generation == before["slot"]["generation"]
        assert slot.observed_state == "stopped" and slot.error_code is None
        assert slot.desired_state == ("running" if start else "stopped")
        assert all(identity not in host.ledger(slot_id) for identity in before["ledger"])
        assert all(not receipt_path(host.slot_directory(slot_id), value["completion"]).exists()
                   for value in before["ledger"].values())
        assert host.data_directory(slot).joinpath("accepted.bin").read_bytes() == b"accepted before supervisor exit"
        assert not signals
        assert host.workers[slot_id].process.instance_id in host.ledger(slot_id)
        await host.control(slot_id, "start")
        assert host.registry.get_slot(slot_id).generation == slot.generation + 1
        assert host.registry.get_slot(slot_id).observed_state == "ready"
        assert not signals
    finally:
        await host.close()
    assert not list((state / "slots" / slot_id / "ownership-receipts").glob("*.receipt"))


@pytest.mark.parametrize("start", [False, True])
async def test_actual_install_interruption_requires_recovery_after_exit_proof(tmp_path, receipt_packages, monkeypatch, start):
    state, reaper, before = await killed_host(tmp_path, receipt_packages, start=start, kind="install", stage="completed")
    host = Supervisor(state, reaper)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        await host.initialize()
        slot_id = before["slot"]["slot_id"]
        slot = host.registry.get_slot(slot_id)
        operation = host.registry.get_operation(before["operation_id"])
        assert not host.ledger(slot_id) and slot_id not in host.workers
        assert slot.generation == before["slot"]["generation"]
        assert operation.status == "recovery_required"
        assert slot.error_code == "installation_recovery_required"
        with pytest.raises(HostError, match="^installation_recovery_required$"):
            await host.control(slot_id, "start")
        await host.recover_operation(operation.id)
        async with asyncio.timeout(20):
            while host.registry.get_operation(operation.id).status in {"pending", "running", "recovery_required"}:
                task = next((t for t in host.tasks if getattr(t, "operation_id", None) == operation.id), None)
                if task is None:
                    break
                await asyncio.sleep(0.01)
        assert host.registry.get_operation(operation.id).status == "completed"
        slot = host.registry.get_slot(slot_id)
        assert slot.generation == before["slot"]["generation"]
        assert slot.observed_state == ("ready" if start else "stopped")
        assert host.data_directory(slot).joinpath("accepted.bin").read_bytes() == b"accepted before supervisor exit"
        assert not signals
    finally:
        await host.close()


@pytest.mark.parametrize("stage", ["selected", "exposed"])
async def test_actual_upgrade_sigkill_recovery_respects_exposure_boundary(tmp_path, receipt_packages, monkeypatch, stage):
    state, reaper, before = await killed_host(tmp_path, receipt_packages, kind="upgrade", stage=stage)
    host = Supervisor(state, reaper)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        await host.initialize()
        slot_id = before["slot"]["slot_id"]
        operation = host.registry.get_operation(before["operation_id"])
        selected = host.registry.get_slot(slot_id)
        journal = host.upgrades.get(operation.id)
        if stage == "selected":
            assert selected.package_digest == journal["old"]["package_digest"]
            assert selected.data_path == journal["old"]["data_path"]
            assert selected.generation > before["slot"]["generation"]
            assert operation.status == "failed"
        else:
            assert selected.package_digest == before["slot"]["package_digest"]
            assert selected.data_path == before["slot"]["data_path"]
            assert selected.generation == before["slot"]["generation"]
            assert operation.status == "completed"
            assert host.data_directory(selected).joinpath("accepted.bin").read_bytes() == b"accepted before supervisor exit"
        assert all(identity not in host.ledger(slot_id) for identity in before["ledger"])
        assert not signals
    finally:
        await host.close()


def blocked_slot(host: Supervisor, source: Path):
    package = validate_package(make_package(source))
    slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root,
                                 name="Unresolved owned identity", public_port=19385)
    return host.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")


@pytest.mark.parametrize("failure,boot_state", [
    ("missing", "different"), ("pending", "same"), ("pending", "unknown"),
    ("malformed", "different"), ("wrong_nonce", "different"),
    ("unsafe_file", "different"), ("missing_metadata", "different"),
])
async def test_unproven_receipt_keeps_ledger_generation_reservation_and_control_blocked(
        tmp_path, monkeypatch, failure, boot_state):
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(PRIOR_BOOT))
    state = tmp_path / "state"
    host = Supervisor(state, tmp_path / "unused-reaper")
    slot = blocked_slot(host, tmp_path / "source")
    descriptor, metadata = prepare_receipt(host.slot_directory(slot.id), uuid.uuid4().hex)
    os.close(descriptor)
    path = receipt_path(host.slot_directory(slot.id), metadata)
    if failure == "missing":
        path.unlink()
    elif failure == "malformed":
        path.write_bytes(b"invalid completion")
    elif failure == "wrong_nonce":
        metadata = {**metadata, "nonce": "0" * 64}
    elif failure == "unsafe_file":
        path.chmod(0o644)
    elif failure == "pending":
        os.utime(path, ns=(1, 1))
    record = {"state": "ownership_lost", "boot_id": PRIOR_BOOT}
    if failure != "missing_metadata":
        record["completion"] = metadata
    host.ledger(slot.id)[metadata["instance_id"]] = record
    host.save_ledger(slot.id)
    write_private_json(host.slot_directory(slot.id) / "reservation.json", {"memory_mb": 32, "gpu": False})
    await host.close()
    boot = (None if boot_state == "unknown" else
            BootSession(PRIOR_BOOT if boot_state == "same" else CURRENT_BOOT))
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: boot)
    restarted = Supervisor(state, tmp_path / "unused-reaper")
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        await restarted.initialize()
        recovered = restarted.registry.get_slot(slot.id)
        assert recovered.generation == slot.generation
        assert recovered.observed_state == "ownership_lost" and recovered.error_code == "ownership_lost"
        assert restarted.ledger(slot.id) == {metadata["instance_id"]: record}
        assert restarted.reservations[slot.id] == {"memory_mb": 32, "gpu": False}
        assert not restarted.owners and not restarted.workers and not restarted.listeners
        for action in ("start", "stop", "restart"):
            with pytest.raises(HostError, match="^ownership_lost$"):
                await restarted.control(slot.id, action)
        assert not signals
    finally:
        await restarted.close()


@pytest.mark.parametrize("start_policy,wake_suppressed,proof", [
    ("manual", False, "uuid"), ("manual", True, "uuid"),
    ("host", True, "uuid"), ("manual", False, "legacy"),
])
async def test_previous_boot_pending_receipt_recovers_slot_and_explicit_start(
        tmp_path, prepared, monkeypatch, start_policy, wake_suppressed, proof):
    package, reaper = prepared
    state = tmp_path / "state"
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(PRIOR_BOOT))
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package), "start": False, "start_policy": start_policy})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        await host.stop_gateway(slot.id)
        host.data_directory(slot).joinpath("accepted.bin").write_bytes(b"accepted before reboot")
        before = host.registry.update_slot(slot.id, desired_state="running", wake_suppressed=wake_suppressed,
                                           observed_state="ownership_lost", error_code="ownership_lost")
        connection = host.connection(slot.id)
        descriptor, metadata = prepare_receipt(host.slot_directory(slot.id), uuid.uuid4().hex)
        os.close(descriptor)
        record = {"state": "ownership_lost", "completion": metadata}
        if proof == "uuid":
            record["boot_id"] = PRIOR_BOOT
        host.ledger(slot.id)[metadata["instance_id"]] = record
        host.save_ledger(slot.id)
        reservation_path = host.slot_directory(slot.id) / "reservation.json"
        write_private_json(reservation_path, {"memory_mb": 32, "gpu": False})
        os.utime(receipt_path(host.slot_directory(slot.id), metadata), ns=(1, 1))
        next_boot = BootSession(CURRENT_BOOT)
    finally:
        await host.close()

    if proof == "legacy":
        monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(PRIOR_BOOT))
        observed = Supervisor(state, reaper)
        try:
            await observed.initialize()
            unchanged = observed.registry.get_slot(slot.id)
            assert unchanged == before
            assert observed.ledger(slot.id) == {metadata["instance_id"]: {**record, "boot_id": PRIOR_BOOT}}
            assert read_private_json(observed.slot_directory(slot.id) / "ownership.json") == observed.ledger(slot.id)
            assert receipt_path(observed.slot_directory(slot.id), metadata).exists()
            assert observed.reservations[slot.id] == {"memory_mb": 32, "gpu": False}
            assert not observed.workers and not observed.listeners
            with pytest.raises(HostError, match="^ownership_lost$"):
                await observed.control(slot.id, "start")
        finally:
            await observed.close()

    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: next_boot)
    restarted = Supervisor(state, reaper)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        await restarted.initialize()
        recovered = restarted.registry.get_slot(slot.id)
        assert recovered.observed_state == "stopped" and recovered.error_code is None
        for field in ("generation", "package_digest", "package_path", "data_path", "credential_ref",
                      "public_port", "start_policy", "wake_suppressed", "desired_state"):
            assert getattr(recovered, field) == getattr(before, field)
        assert restarted.connection(slot.id) == connection
        assert restarted.data_directory(recovered).joinpath("accepted.bin").read_bytes() == b"accepted before reboot"
        assert metadata["instance_id"] not in restarted.ledger(slot.id)
        assert not receipt_path(restarted.slot_directory(slot.id), metadata).exists()
        assert not reservation_path.exists() and slot.id not in restarted.reservations
        assert restarted.workers[slot.id].process.instance_id in restarted.ledger(slot.id)
        assert not signals
        await restarted.control(slot.id, "start")
        started = restarted.registry.get_slot(slot.id)
        assert started.observed_state == "ready" and started.generation == before.generation + 1
        assert not started.wake_suppressed
        assert await tool_call(restarted, slot.id) == {"valid": True, "kind": "object"}
        assert not signals
    finally:
        await restarted.close()


async def test_last_receipt_survives_interrupted_ledger_removal_and_replays_on_restart(
        tmp_path, prepared, monkeypatch):
    package, reaper = prepared
    state = tmp_path / "state"
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(PRIOR_BOOT))
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package), "start": False})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        await host.stop_gateway(slot.id)
        host.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
        descriptor, metadata = prepare_receipt(host.slot_directory(slot.id), uuid.uuid4().hex)
        os.close(descriptor)
        record = {"state": "ownership_lost", "completion": metadata, "boot_id": PRIOR_BOOT}
        host.ledger(slot.id)[metadata["instance_id"]] = record
        host.save_ledger(slot.id)
    finally:
        await host.close()

    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
    interrupted = Supervisor(state, reaper)
    def fail_save(slot_id):
        raise OSError("synthetic ledger persistence failure")
    monkeypatch.setattr(interrupted, "save_ledger", fail_save)
    try:
        with pytest.raises(OSError, match="^synthetic ledger persistence failure$"):
            await interrupted.initialize()
        assert read_private_json(interrupted.slot_directory(slot.id) / "ownership.json") == {metadata["instance_id"]: record}
        assert receipt_path(interrupted.slot_directory(slot.id), metadata).exists()
        classified = interrupted.registry.get_slot(slot.id)
        assert classified.observed_state == "failed" and classified.error_code == "operation_interrupted"
        assert classified.generation == slot.generation
        assert not interrupted.workers and not interrupted.listeners
    finally:
        await interrupted.close()

    restarted = Supervisor(state, reaper)
    try:
        await restarted.initialize()
        recovered = restarted.registry.get_slot(slot.id)
        assert recovered.generation == slot.generation
        assert recovered.observed_state == "stopped" and recovered.error_code is None
        assert metadata["instance_id"] not in restarted.ledger(slot.id)
        assert not receipt_path(restarted.slot_directory(slot.id), metadata).exists()
        await restarted.control(slot.id, "start")
        assert restarted.registry.get_slot(slot.id).observed_state == "ready"
        assert await tool_call(restarted, slot.id) == {"valid": True, "kind": "object"}
    finally:
        await restarted.close()


async def test_supervisor_receipt_capability_is_closed_before_target_exec(tmp_path, prepared, monkeypatch):
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
    _, reaper = prepared
    host = Supervisor(tmp_path / "state", reaper)
    slot = blocked_slot(host, tmp_path / "source")
    # This test uses launch directly so the synthetic child has no permitted
    # extra descriptors; the receipt capability must remain solely in its reaper.
    host.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation)
    process = None
    try:
        process = await host.launch(slot.id, executable=Path(sys.executable), args=["-c",
            "import os,json; f=[]\nfor n in range(3,256):\n try: os.fstat(n); f.append(n)\n except OSError: pass\nprint(json.dumps(f),flush=True)"],
            cwd=host.root, env={})
        completion = host.ledger(slot.id)[process.instance_id]["completion"]
        assert read_private_json(host.slot_directory(slot.id) / "ownership.json")[process.instance_id]["boot_id"] == CURRENT_BOOT
        assert json.loads(await process.stdout.readline()) == []
        await host.reap(slot.id, process)
        assert not host.ledger(slot.id)
        assert not receipt_path(host.slot_directory(slot.id), completion).exists()
    finally:
        if process is not None:
            process.close_stdio()
        await host.close()


async def test_completed_identity_is_retired_while_unproven_identity_stays_blocked(tmp_path, prepared, monkeypatch):
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
    _, reaper = prepared
    state = tmp_path / "state"
    host = Supervisor(state, reaper)
    slot = blocked_slot(host, tmp_path / "source")
    host.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation)
    process = await host.launch(slot.id, executable=Path(sys.executable), args=["-c", "pass"],
                                cwd=host.root, env={})
    completed = host.ledger(slot.id)[process.instance_id]["completion"]
    assert await process.wait() == 0
    process.close_stdio()
    assert verify_receipt(host.slot_directory(slot.id), completed)
    descriptor, pending = prepare_receipt(host.slot_directory(slot.id), uuid.uuid4().hex)
    os.close(descriptor)
    host.ledger(slot.id)[pending["instance_id"]] = {"state": "ownership_lost", "completion": pending, "boot_id": CURRENT_BOOT}
    host.save_ledger(slot.id)
    host.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
    await host.close()
    restarted = Supervisor(state, reaper)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        await restarted.initialize()
        assert process.instance_id not in restarted.ledger(slot.id)
        assert not receipt_path(restarted.slot_directory(slot.id), completed).exists()
        assert restarted.ledger(slot.id) == {
            pending["instance_id"]: {"state": "ownership_lost", "completion": pending, "boot_id": CURRENT_BOOT}}
        assert receipt_path(restarted.slot_directory(slot.id), pending).exists()
        recovered = restarted.registry.get_slot(slot.id)
        assert recovered.generation == slot.generation
        assert recovered.observed_state == "ownership_lost" and recovered.error_code == "ownership_lost"
        assert not signals
    finally:
        await restarted.close()


async def test_failed_exec_retires_completed_receipt_after_ledger_removal(tmp_path, prepared):
    _, reaper = prepared
    host = Supervisor(tmp_path / "state", reaper)
    slot = blocked_slot(host, tmp_path / "source")
    host.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation)
    try:
        with pytest.raises(ProcessError, match="^launch_failed$"):
            await host.launch(slot.id, executable=tmp_path / "missing", args=[], cwd=host.root, env={})
        assert not host.ledger(slot.id) and not host.owner(slot.id).instances
        assert not list((host.slot_directory(slot.id) / "ownership-receipts").glob("*.receipt"))
    finally:
        await host.close()


@pytest.mark.parametrize("start_policy,wake_suppressed", [
    ("manual", False), ("manual", True), ("host", True),
])
async def test_actual_reaper_sigkill_recovers_on_same_boot_and_allows_explicit_start(
        tmp_path, prepared, monkeypatch, start_policy, wake_suppressed):
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
    package, reaper = prepared
    state = tmp_path / "state"
    host = Supervisor(state, reaper)
    real_kill = os.kill
    process = None
    host_closed = False
    try:
        plan = host.plan_install({"package": str(package), "start": False, "start_policy": start_policy})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        await host.stop_gateway(slot.id)
        accepted = host.data_directory(slot) / "accepted.bin"
        accepted.write_bytes(b"accepted before relaunch")
        connection = host.connection(slot.id)
        process = await host.launch(slot.id, executable=Path(sys.executable),
            args=["-c", "import time; time.sleep(100)"], cwd=host.root, env={})
        completion = host.ledger(slot.id)[process.instance_id]["completion"]
        assert completion["schema_version"] == 2
        real_kill(process.reaper_pid, signal.SIGKILL)
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(process.wait(), 5)
        assert not verify_receipt(host.slot_directory(slot.id), completion)
        assert host.ledger(slot.id)[process.instance_id]["state"] == "ownership_lost"
        before = host.registry.update_slot(slot.id, desired_state="running",
            wake_suppressed=wake_suppressed, observed_state="ownership_lost", error_code="ownership_lost")
        reservation = host.slot_directory(slot.id) / "reservation.json"
        write_private_json(reservation, {"memory_mb": 32, "gpu": False})
        await host.close()
        host_closed = True
        restarted = Supervisor(state, reaper)
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        try:
            await restarted.initialize()
            recovered = restarted.registry.get_slot(slot.id)
            assert recovered.observed_state == "stopped" and recovered.error_code is None
            for field in ("generation", "package_digest", "package_path", "data_path", "credential_ref",
                          "public_port", "start_policy", "wake_suppressed", "desired_state"):
                assert getattr(recovered, field) == getattr(before, field)
            assert restarted.boot_session.identity == CURRENT_BOOT
            connection_unchanged = restarted.connection(slot.id) == connection
            assert connection_unchanged
            assert restarted.data_directory(recovered).joinpath("accepted.bin").read_bytes() == b"accepted before relaunch"
            assert process.instance_id not in restarted.ledger(slot.id)
            assert not receipt_path(restarted.slot_directory(slot.id), completion).exists()
            assert not reservation.exists() and slot.id not in restarted.reservations
            with pytest.raises(ProcessLookupError):
                real_kill(process.pid, 0)
            assert restarted.workers[slot.id].process.instance_id in restarted.ledger(slot.id)
            await restarted.control(slot.id, "start")
            started = restarted.registry.get_slot(slot.id)
            assert started.observed_state == "ready" and started.generation == before.generation + 1
            assert not started.wake_suppressed
            assert await tool_call(restarted, slot.id) == {"valid": True, "kind": "object"}
            await restarted.reserve_model(started, restarted.package(started))
            assert slot.id in restarted.reservations and reservation.exists()
            await restarted.release_model(slot.id)
            assert slot.id not in restarted.reservations and not reservation.exists()
            assert not signals
        finally:
            await restarted.close()
    finally:
        if not host_closed:
            await host.close()
        if process is not None:
            process.close_stdio()
            with contextlib.suppress(ProcessLookupError):
                real_kill(process.pid, signal.SIGKILL)


async def test_registry_receipt_reconciliation_preserves_generation_and_checks_expected_value(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    slot = blocked_slot(host, tmp_path / "source")
    try:
        with pytest.raises(RegistryError, match="^stale_generation$"):
            host.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation + 1)
        assert host.registry.get_slot(slot.id) == slot
        reconciled = host.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation)
        assert reconciled.generation == slot.generation
        assert reconciled.observed_state == "failed" and reconciled.error_code == "operation_interrupted"
    finally:
        await host.close()
