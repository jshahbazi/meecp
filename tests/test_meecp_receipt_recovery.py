"""Restart recovery uses native completion proof, never a recorded target PID."""
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

from meecp.contracts import validate_package
from meecp.ownership_receipts import prepare_receipt, verify_receipt
from meecp.private import read_private_json, write_private_json
from meecp.processes import OwnershipLost, ProcessError
from meecp.registry import RegistryError
from meecp.supervisor import HostError, Supervisor
from test_meecp_host import prepared  # Reuse the actual signed native fixture.
from test_meecp_packages import make_package


ROOT = Path(__file__).resolve().parents[1]

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


@pytest.mark.parametrize("failure", ["missing", "pending", "malformed", "wrong_nonce", "unsafe_file", "missing_metadata"])
async def test_unproven_receipt_keeps_ledger_generation_reservation_and_control_blocked(tmp_path, monkeypatch, failure):
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
    record = {"state": "ownership_lost"}
    if failure != "missing_metadata":
        record["completion"] = metadata
    host.ledger(slot.id)[metadata["instance_id"]] = record
    host.save_ledger(slot.id)
    write_private_json(host.slot_directory(slot.id) / "reservation.json", {"memory_mb": 32, "gpu": False})
    await host.close()
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


async def test_supervisor_receipt_capability_is_closed_before_target_exec(tmp_path, prepared):
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
        assert json.loads(await process.stdout.readline()) == []
        await host.reap(slot.id, process)
        assert not host.ledger(slot.id)
        assert not receipt_path(host.slot_directory(slot.id), completion).exists()
    finally:
        if process is not None:
            process.close_stdio()
        await host.close()


async def test_completed_identity_is_retired_while_unproven_identity_stays_blocked(tmp_path, prepared, monkeypatch):
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
    host.ledger(slot.id)[pending["instance_id"]] = {"state": "ownership_lost", "completion": pending}
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
            pending["instance_id"]: {"state": "ownership_lost", "completion": pending}}
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


async def test_actual_reaper_sigkill_never_clears_ownership_on_restart(tmp_path, prepared, monkeypatch):
    _, reaper = prepared
    state = tmp_path / "state"
    host = Supervisor(state, reaper)
    slot = blocked_slot(host, tmp_path / "source")
    host.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation)
    process = await host.launch(slot.id, executable=Path(sys.executable), args=["-c", "import time; time.sleep(100)"],
                                cwd=host.root, env={})
    completion = host.ledger(slot.id)[process.instance_id]["completion"]
    real_kill = os.kill
    real_kill(process.reaper_pid, signal.SIGKILL)
    try:
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(process.wait(), 5)
        assert not verify_receipt(host.slot_directory(slot.id), completion)
        assert host.ledger(slot.id)[process.instance_id]["state"] == "ownership_lost"
        await host.close()
        restarted = Supervisor(state, reaper)
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        try:
            await restarted.initialize()
            recovered = restarted.registry.get_slot(slot.id)
            assert recovered.generation == slot.generation
            assert recovered.error_code == "ownership_lost" and recovered.observed_state == "ownership_lost"
            assert process.instance_id in restarted.ledger(slot.id)
            with pytest.raises(HostError, match="^ownership_lost$"):
                await restarted.control(slot.id, "start")
            assert not signals
        finally:
            await restarted.close()
    finally:
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
