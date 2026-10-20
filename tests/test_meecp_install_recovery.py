"""Failure and race checks at the installation publication/recovery boundary."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from meecp.contracts import validate_package
import meecp.install_recovery as install_recovery
from meecp.private import ensure_private_directory, read_private_json
from meecp.registry import RegistryError
from meecp.supervisor import HostError, Supervisor
from meecp.upgrades import UpgradeError
from test_meecp_packages import make_package
from test_meecp_upgrade_host import UpgradeFixture, data_file, settle


@pytest.mark.asyncio
async def test_post_exposure_persistence_failure_preserves_slot_and_client_write(tmp_path, monkeypatch):
    package = make_package(tmp_path / "source")
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    host.check_candidate = AsyncMock()
    client_write = None
    async def expose(slot, active):
        nonlocal client_write
        assert active
        data = host.slot_directory(slot.id) / "data"
        ensure_private_directory(data)
        client_write = data / "accepted-write.json"
        client_write.write_text('{"accepted":true}')
    host.start_gateway = expose
    original = host.registry.update_operation
    def fail_only_completed(operation_id, status, **kwargs):
        if status == "completed":
            raise RegistryError("registry_unavailable")
        return original(operation_id, status, **kwargs)
    monkeypatch.setattr(host.registry, "update_operation", fail_only_completed)
    try:
        plan = host.plan_install({"package": str(package), "start": True})
        result = await host.apply_install(plan["plan_id"])
        task = next(iter(host.tasks))
        await task
        operation = host.registry.get_operation(result["operation_id"])
        slot = host.registry.get_slot(plan["slot_id"])
        assert operation.status == "recovery_required"
        assert slot.error_code == "installation_recovery_required"
        assert slot.observed_state == "failed"
        assert slot.id in host.listeners
        assert client_write is not None and client_write.read_text() == '{"accepted":true}'
        assert read_private_json(host.slot_directory(slot.id) / "activation.json")["exposure_permitted"] is True
        assert (await host.apply_install(plan["plan_id"]))["operation_id"] == operation.id
        with pytest.raises(HostError, match="^installation_recovery_required$"):
            await host.control(slot.id, "start")
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_startup_recovers_install_older_than_operation_display_limit(tmp_path):
    package = validate_package(make_package(tmp_path / "source"))
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    gateway = host.start_gateway = AsyncMock()
    candidate = host.check_candidate = AsyncMock()
    try:
        slot_id = str(uuid.uuid4())
        install = host.registry.create_operation("install", slot_id=slot_id)
        host.registry.update_operation(install.id, "running")
        slot = host.registry.add_slot(slot_id=slot_id, package_digest=package.digest, package_path=package.root,
                                     name="Interrupted", public_port=19386, start_policy="host")
        for _ in range(105):
            later = host.registry.create_operation("install")
            host.registry.update_operation(later.id, "completed")
        await host.initialize()
        assert host.registry.get_operation(install.id).status == "recovery_required"
        assert host.registry.get_slot(slot.id).error_code == "installation_recovery_required"
        gateway.assert_not_awaited()
        candidate.assert_not_awaited()
        for action in ("start", "stop", "restart"):
            with pytest.raises(HostError, match="^installation_recovery_required$"):
                await host.control(slot.id, action)
        assert host.registry.get_slot(slot.id).generation == 1
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_live_candidate_rejects_controls_without_advancing_generation(tmp_path, action):
    package = make_package(tmp_path / "source")
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    entered, release = asyncio.Event(), asyncio.Event()
    async def check_candidate(slot, **kwargs):
        entered.set()
        await release.wait()
    host.check_candidate = check_candidate
    gateway = host.start_gateway = AsyncMock()
    try:
        plan = host.plan_install({"package": str(package), "start": True})
        operation = await host.apply_install(plan["plan_id"])
        task = next(iter(host.tasks))
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(HostError, match="^operation_in_progress$"):
            await asyncio.wait_for(host.control(plan["slot_id"], action), timeout=1)
        assert host.registry.get_slot(plan["slot_id"]).generation == 1
        gateway.assert_not_awaited()
        release.set()
        await task
        assert host.registry.get_operation(operation["operation_id"]).status == "completed"
        assert host.registry.get_slot(plan["slot_id"]).generation == 1
        gateway.assert_awaited_once()
    finally:
        release.set()
        await host.close()


def mock_install_runtime(host):
    host.check_candidate = AsyncMock()
    host.start_gateway = AsyncMock()
    host.stop_gateway = AsyncMock()
    host.stop_residents = AsyncMock()


async def install_task(host, operation_id):
    task = next((task for task in host.tasks if getattr(task, "operation_id", None) == operation_id), None)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 5)
    await asyncio.sleep(0)


@pytest.mark.parametrize("stage", ["staging", "files_ready", "registered", "candidate_verified", "exposed", "completed"])
async def test_install_interruption_at_each_stage_is_owned_and_explicitly_recoverable(tmp_path, stage):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    mock_install_runtime(host)
    async def interrupt(operation_id, current):
        if current == stage:
            raise asyncio.CancelledError()
    host.install_checkpoint = interrupt
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "source")), "start": True})
        operation = await host.apply_install(plan["plan_id"])
        await install_task(host, operation["operation_id"])
        result = host.registry.get_operation(operation["operation_id"])
        assert result.status == "recovery_required"
        if stage in {"registered", "candidate_verified", "exposed", "completed"}:
            slot = host.registry.get_slot(plan["slot_id"])
            assert slot.error_code == "installation_recovery_required"
            assert slot.observed_state == "failed"
        assert result.details.get("exposed", False) == (stage in {"exposed", "completed"})
        host.install_checkpoint = AsyncMock()
        await host.recover_operation(result.id)
        await install_task(host, result.id)
        assert host.registry.get_operation(result.id).status == "completed"
        assert host.registry.get_slot(plan["slot_id"]).observed_state == "ready"
        assert (await host.apply_install(plan["plan_id"]))["operation_id"] == result.id
        assert len(host.registry.list_slots()) == 1
    finally:
        await host.close()


@pytest.mark.parametrize("stage", ["staging", "files_ready", "registered", "candidate_verified"])
async def test_user_cancel_at_each_preexposure_stage_releases_candidate_and_endpoint(tmp_path, stage):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    mock_install_runtime(host)
    async def cancel(operation_id, current):
        if current == stage:
            await host.cancel_operation(operation_id)
    host.install_checkpoint = cancel
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "source")), "start": True})
        operation = await host.apply_install(plan["plan_id"])
        await install_task(host, operation["operation_id"])
        result = host.registry.get_operation(operation["operation_id"])
        assert result.status == "canceled" and result.details["cancel_requested"]
        assert not result.details.get("exposed") and not result.details.get("selected")
        assert host.registry.list_slots() == [] and plan["slot_id"] not in host.listeners
        assert (await host.cancel_operation(result.id))["operation_id"] == result.id
        assert (await host.apply_install(plan["plan_id"]))["operation_id"] == result.id
        host.start_gateway.assert_not_awaited()
        with pytest.raises(UpgradeError, match="^operation_recovery_unavailable$"):
            await host.recover_operation(result.id)
    finally:
        await host.close()


@pytest.mark.parametrize("stage", ["staging", "files_ready", "registered", "candidate_verified", "exposed", "completed"])
async def test_first_install_restart_recovers_every_actual_durable_stage(tmp_path, monkeypatch, stage):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    mock_install_runtime(host)
    async def crash(operation_id, current):
        if current == stage:
            if current in {"exposed", "completed"}:
                data = host.data_directory(host.registry.get_slot(host.registry.get_operation(operation_id).slot_id))
                (data / "accepted.bin").write_bytes(b"accepted installation write")
                (data / "accepted.bin").chmod(0o600)
            raise asyncio.CancelledError()
    host.install_checkpoint = crash
    compensation = install_recovery.fail_install
    disappeared = AsyncMock()
    monkeypatch.setattr(install_recovery, "fail_install", disappeared)
    plan = host.plan_install({"package": str(make_package(tmp_path / "source")), "start": True})
    operation = await host.apply_install(plan["plan_id"])
    await install_task(host, operation["operation_id"])
    assert host.registry.get_operation(operation["operation_id"]).status == "running"
    connection_path = host.slot_directory(plan["slot_id"]) / "connection.json"
    original_connection = read_private_json(connection_path) if connection_path.exists() else None
    # A real process exit closes even listeners held solely by stack frames.
    # The mocked compensation retains its arguments for inspection, so release
    # that process-local descriptor explicitly when simulating the disappearance.
    retained_listener = disappeared.await_args.kwargs.get("listener")
    if retained_listener is not None:
        retained_listener.close()
    await host.close()
    monkeypatch.setattr(install_recovery, "fail_install", compensation)
    restarted = Supervisor(host.root, tmp_path / "unused-reaper")
    mock_install_runtime(restarted)
    try:
        await restarted.initialize()
        result = restarted.registry.get_operation(operation["operation_id"])
        assert result.status == "recovery_required"
        restarted.start_gateway.assert_not_awaited()
        await restarted.recover_operation(result.id)
        await install_task(restarted, result.id)
        result = restarted.registry.get_operation(result.id)
        assert result.status == "completed"
        selected = restarted.registry.get_slot(plan["slot_id"])
        assert selected.public_port == plan["public_port"] and selected.observed_state == "ready"
        if original_connection is not None:
            assert restarted.connection(selected.id) == original_connection
        if stage in {"exposed", "completed"}:
            assert restarted.data_directory(selected).joinpath("accepted.bin").read_bytes() == b"accepted installation write"
        assert restarted.start_gateway.await_count == 1
        assert (await restarted.apply_install(plan["plan_id"]))["operation_id"] == result.id
    finally:
        await restarted.close()


async def test_queued_install_cancel_and_duplicate_apply_share_one_intent(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    mock_install_runtime(host)
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "source"))})
        operation = await host.apply_install(plan["plan_id"])
        # No event-loop yield occurs before cancellation: the queued task has
        # not yet executed its first instruction.
        assert host.registry.get_operation(operation["operation_id"]).status == "pending"
        await host.cancel_operation(operation["operation_id"])
        duplicate = await host.apply_install(plan["plan_id"])
        assert duplicate["operation_id"] == operation["operation_id"]
        await install_task(host, operation["operation_id"])
        assert host.registry.get_operation(operation["operation_id"]).status == "canceled"
        assert host.registry.list_slots() == []
        assert len(host.registry.list_operations()) == 1
        host.check_candidate.assert_not_awaited()
    finally:
        await host.close()


async def test_repeated_install_cancel_waits_for_static_writer_before_cleanup(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    mock_install_runtime(host)
    entered, released, settled = threading.Event(), threading.Event(), threading.Event()
    original = install_recovery.snapshot_package
    def blocking(*args, **kwargs):
        entered.set()
        try:
            assert released.wait(3)
            return original(*args, **kwargs)
        finally:
            settled.set()
    monkeypatch.setattr(install_recovery, "snapshot_package", blocking)
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "source"))})
        operation = await host.apply_install(plan["plan_id"])
        assert await asyncio.to_thread(entered.wait, 2)
        await host.cancel_operation(operation["operation_id"])
        await asyncio.sleep(0.01)
        await host.cancel_operation(operation["operation_id"])
        await asyncio.sleep(0.01)
        assert not settled.is_set()
        assert host.registry.get_operation(operation["operation_id"]).status == "running"
        with pytest.raises(HostError, match="^host_already_running$"):
            Supervisor(host.root, tmp_path / "unused-reaper")
        released.set()
        await install_task(host, operation["operation_id"])
        assert settled.is_set()
        assert host.registry.get_operation(operation["operation_id"]).status == "canceled"
        assert host.registry.list_slots() == []
        assert plan["slot_id"] not in host.listeners
    finally:
        released.set()
        await host.close()


async def test_install_registration_and_selected_marker_are_one_transaction(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    mock_install_runtime(host)
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "source"))})
        operation = host.registry.create_operation("install", slot_id=plan["slot_id"])
        host.registry.update_operation(operation.id, "running")
        host.registry._connection.execute("CREATE TRIGGER fail_install_marker BEFORE UPDATE OF details ON operations BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END")
        with pytest.raises(RegistryError, match="^registry_unavailable$"):
            host.registry.register_install(operation.id, slot_id=plan["slot_id"], package_digest=plan["package_digest"],
                package_path=plan["package_path"], name=plan["name"], public_port=plan["public_port"])
        assert host.registry.list_slots() == []
        assert not host.registry.get_operation(operation.id).details.get("selected")
    finally:
        await host.close()


@pytest.mark.parametrize("action", ["install", "upgrade"])
@pytest.mark.parametrize("ownership", ["held", "unknown", "lost", "mismatched_owner", "missing_ack",
                                        "wrapper_exited", "monitor_done", "pending_launch", "persistence_failure"])
async def test_exposed_recovery_accepts_held_stopped_gateway_and_rejects_unknown_ledger(tmp_path, action, ownership):
    """Stop retains a dormant gateway; its held capability can be quiesced.

    A journal entry without the matching live controller remains unknown and
    must never be signaled or adopted by recovery.
    """
    if action == "upgrade":
        fixture = UpgradeFixture(tmp_path)
        host = fixture.host
        async def fault(operation_id, stage):
            if stage == "exposed":
                raise UpgradeError("synthetic_routing_failure")
        host.upgrade_checkpoint = fault
        operation = await fixture.apply(await fixture.plan())
    else:
        host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
        mock_install_runtime(host)
        async def fault(operation_id, stage):
            if stage == "exposed":
                raise HostError("synthetic_routing_failure")
        host.install_checkpoint = fault
        plan = host.plan_install({"package": str(make_package(tmp_path / "source")), "start": True})
        submitted = await host.apply_install(plan["plan_id"])
        await install_task(host, submitted["operation_id"])
        operation = host.registry.get_operation(submitted["operation_id"])
    identity = uuid.uuid4().hex
    owner = host.owner(operation.slot_id)
    loop = asyncio.get_running_loop()
    process = SimpleNamespace(owner=owner, instance_id=identity, pid=42424, ownership_lost=False,
        returncode=None, wrapper=SimpleNamespace(returncode=None), _done=loop.create_future(), _monitor=loop.create_future())
    async def stop_owned():
        owner.instances.pop(identity, None)
    process.stop = AsyncMock(side_effect=stop_owned)
    try:
        assert operation.status == "recovery_required" and operation.details["exposed"]
        await host.control(operation.slot_id, "stop")
        host.install_checkpoint = AsyncMock()
        host.upgrade_checkpoint = AsyncMock()
        stopped = host.registry.get_slot(operation.slot_id)
        data = host.data_directory(stopped)
        data_file(data / "accepted.bin", b"accepted synthetic write")
        host.ledger(stopped.id)[identity] = {"state": "owned", "role": "gateway", "generation": stopped.generation}
        host.save_ledger(stopped.id)
        if ownership != "unknown":
            owner.instances[identity] = process
        if ownership == "lost":
            process.ownership_lost = True
        elif ownership == "mismatched_owner":
            process.owner = object()
        elif ownership == "missing_ack":
            process.pid = None
        elif ownership == "wrapper_exited":
            process.wrapper.returncode = 0
        elif ownership == "monitor_done":
            process._monitor.set_result(None)
        elif ownership == "pending_launch":
            owner._pending_ids.add(identity)
        elif ownership == "persistence_failure":
            owner.persistence_failed = True
        if ownership == "held":
            await host.recover_operation(operation.id)
            await settle(host, operation.id)
            restored = host.registry.get_slot(stopped.id)
            assert host.registry.get_operation(operation.id).status == "completed"
            assert restored.desired_state == "stopped" and restored.observed_state == "stopped"
            assert restored.data_path == stopped.data_path
            assert restored.generation == stopped.generation
            assert (data / "accepted.bin").read_bytes() == b"accepted synthetic write"
            process.stop.assert_awaited_once()
            assert host.ledger(stopped.id) == {}
        else:
            with pytest.raises(UpgradeError, match="^ownership_lost$"):
                await host.recover_operation(operation.id)
            process.stop.assert_not_awaited()
            assert host.registry.get_operation(operation.id).status == "recovery_required"
    finally:
        # Unknown simulated entries deliberately have no OS process to settle.
        host.ledger(operation.slot_id).pop(identity, None)
        owner.instances.pop(identity, None)
        owner._pending_ids.discard(identity)
        owner.persistence_failed = False
        host.save_ledger(operation.slot_id)
        await host.close()
