import asyncio
import copy
import os
from pathlib import Path
import plistlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from meecp import migrations
from meecp.private import ensure_private_directory, read_private_json, write_private_json
from meecp.registry import RegistryError
from meecp.supervisor import HostError, Supervisor, bind_listener
from meecp_local_transport import bind_private_listener, socket_path
from test_meecp_migrations import record


def legacy_alias(value):
    listener = bind_private_listener(Path(value["legacy_state"]), "jevk5")
    listener.close()
    return socket_path(Path(value["legacy_state"]), "jevk5")


async def drain(host):
    while host.tasks:
        await asyncio.gather(*list(host.tasks))


def fresh_transfer_fixture(value, monkeypatch):
    app = Path(value["legacy_app"])
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.jshahbazi.jevk5"}))
    package = SimpleNamespace(digest="a" * 64, manifest=SimpleNamespace(package_id="local.jevk5", profile="model-service",
                                                                     model_dump=lambda: {"package_id": "local.jevk5"}))
    validate = Mock(return_value=package)
    native = AsyncMock(return_value={"running": False, "login_enabled": False})
    snapshot = Mock(side_effect=lambda _package, destination: SimpleNamespace(root=destination, digest="a" * 64))
    monkeypatch.setattr(migrations, "validate_package", validate)
    monkeypatch.setattr(migrations, "check_legacy_contract", lambda *_: None)
    monkeypatch.setattr(migrations, "native_handoff", native)
    monkeypatch.setattr(migrations, "snapshot_package", snapshot)
    payload = {"package": value["package_path"], "legacy_app": str(app), "legacy_state": value["legacy_state"]}
    return payload, validate, native, snapshot


@pytest.mark.parametrize("action", ["plan", "apply"])
@pytest.mark.parametrize("reservation", ["slot", "operation"])
async def test_fresh_transfer_requires_predecessor_removal_to_finish(tmp_path, monkeypatch, action, reservation):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    predecessor = record(host, tmp_path, stage="removed", status="completed")
    payload, validate, native, snapshot = fresh_transfer_fixture(predecessor, monkeypatch)
    host.registry.add_slot(slot_id=predecessor["slot_id"], package_digest=predecessor["package_digest"],
                           package_path=predecessor["package_path"], name="JevK5 Judge", public_port=19385)
    if reservation == "operation":
        removal = host.registry.create_operation("remove", slot_id=predecessor["slot_id"])
        host.registry.update_operation(removal.id, "recovery_required", error_code="registry_unavailable")
        host.registry.discard_slot(predecessor["slot_id"])
    execute = AsyncMock()
    monkeypatch.setattr(migrations, "execute_transfer", execute)
    if action == "apply":
        candidate = {**copy.deepcopy(predecessor), "transfer_id": str(uuid.uuid4()), "slot_id": str(uuid.uuid4()),
                     "stage": "staged", "status": "planned"}
        migrations.save(host, candidate)
    try:
        with pytest.raises(HostError, match="^removal_recovery_required$"):
            if action == "plan":
                await migrations.plan_transfer(host, payload)
            else:
                await migrations.apply_transfer(host, candidate["transfer_id"])
        native.assert_not_awaited()
        execute.assert_not_awaited()
        validate.assert_not_called()
        snapshot.assert_not_called()
        assert not host.tasks and not host.legacy_locks
        assert migrations.inspect_transfer(host, predecessor["transfer_id"])["stage"] == "removed"
        if action == "apply":
            assert migrations.inspect_transfer(host, candidate["transfer_id"])["status"] == "planned"
        if reservation == "slot":
            host.registry.discard_slot(predecessor["slot_id"])
        else:
            host.registry.update_operation(removal.id, "completed")
        if action == "plan":
            result = await migrations.plan_transfer(host, payload)
            assert result["stage"] == "staged" and result["slot_id"] != predecessor["slot_id"]
            native.assert_awaited_once()
        else:
            result = await migrations.apply_transfer(host, candidate["transfer_id"])
            await drain(host)
            assert result["status"] == "running"
            execute.assert_awaited_once()
    finally:
        await host.close()


async def test_failed_removal_finalization_blocks_new_migration_until_retry_completes(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    payload, validate, native, snapshot = fresh_transfer_fixture(value, monkeypatch)
    await migrations.initialize_transfers(host)
    complete_removal = host.registry.complete_removal
    def unavailable(*args, **kwargs):
        raise RegistryError("registry_unavailable")
    monkeypatch.setattr(host.registry, "complete_removal", unavailable)
    try:
        removal = host.begin_control(value["slot_id"], "remove")
        await drain(host)
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == "removed"
        assert host.registry.get_operation(removal["operation_id"]).status == "recovery_required"
        assert host.registry.get_slot(value["slot_id"]).public_port == 19385
        with pytest.raises(HostError, match="^removal_recovery_required$"):
            await migrations.plan_transfer(host, payload)
        native.assert_not_awaited()
        validate.assert_not_called()
        snapshot.assert_not_called()
        monkeypatch.setattr(host.registry, "complete_removal", complete_removal)
        retry = host.begin_control(value["slot_id"], "remove")
        assert retry["operation_id"] == removal["operation_id"]
        await drain(host)
        assert host.registry.get_operation(removal["operation_id"]).status == "completed"
        assert not host.registry.list_slots()
        result = await migrations.plan_transfer(host, payload)
        assert result["stage"] == "staged" and result["slot_id"] != value["slot_id"]
        native.assert_awaited_once()
    finally:
        await host.close()


async def test_planning_rechecks_predecessor_after_package_validation_yields(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    payload, validate, native, snapshot = fresh_transfer_fixture(value, monkeypatch)
    await migrations.initialize_transfers(host)
    entered, release = threading.Event(), threading.Event()
    def validate_after_release(path):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("fixture-validation-wait")
        return validate(path)
    monkeypatch.setattr(migrations, "validate_package", validate_after_release)
    def unavailable(*args, **kwargs):
        raise RegistryError("registry_unavailable")
    monkeypatch.setattr(host.registry, "complete_removal", unavailable)
    planning = asyncio.create_task(migrations.plan_transfer(host, payload))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        removal = host.begin_control(value["slot_id"], "remove")
        await drain(host)
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == "removed"
        assert host.registry.get_operation(removal["operation_id"]).status == "recovery_required"
        release.set()
        with pytest.raises(HostError, match="^removal_recovery_required$"):
            await planning
        native.assert_not_awaited()
        snapshot.assert_not_called()
        assert len(list((host.root / "transfers").glob("*.json"))) == 1
    finally:
        release.set()
        await asyncio.gather(planning, return_exceptions=True)
        await host.close()


async def test_remove_migrated_app_retires_alias_and_preserves_recovery_artifacts(tmp_path, monkeypatch):
    state = tmp_path / "state"
    host = Supervisor(state, tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    slot_id = value["slot_id"]
    directory = host.slot_directory(slot_id)
    write_private_json(directory / "connection.json", {"token": "x" * 48, "endpoint": "http://127.0.0.1:19385"})
    write_private_json(directory / "activation.json", {"exposure_permitted": True, "legacy_jev": True})
    data = ensure_private_directory(directory / "data") / "retained.bin"
    data.write_bytes(b"retained-generation-data")
    package = Path(value["package_path"])
    package.mkdir()
    source = package / "source.txt"
    source.write_bytes(b"preserved-package-source")
    app = Path(value["legacy_app"])
    app.mkdir()
    legacy_binary = app / "legacy-binary"
    legacy_binary.write_bytes(b"preserved-legacy-bundle")
    connection = Path(value["legacy_state"]) / "connection.json"
    connection_before = connection.read_bytes()
    native = AsyncMock()
    monkeypatch.setattr(migrations, "native_handoff", native)
    await migrations.initialize_transfers(host)
    alias = legacy_alias(value)
    private = bind_private_listener(host.root, slot_id)
    private.close()
    private_path = socket_path(host.root, slot_id)
    host.listeners[slot_id] = bind_listener(0)
    listener = host.listeners[slot_id]
    try:
        operation = host.begin_control(slot_id, "remove")
        await drain(host)
        assert host.registry.get_operation(operation["operation_id"]).status == "completed"
        with pytest.raises(RegistryError, match="slot_not_found"):
            host.registry.get_slot(slot_id)
        retired = migrations.inspect_transfer(host, value["transfer_id"])
        assert retired["stage"] == "removed" and retired["status"] == "completed"
        assert retired["removal_operation_id"] == operation["operation_id"]
        assert retired["login_enabled"] == value["login_enabled"]
        assert not alias.exists() and not private_path.exists() and listener.fileno() == -1
        assert not (directory / "connection.json").exists()
        assert read_private_json(directory / "activation.json")["exposure_permitted"] is False
        assert slot_id not in host.legacy_locks and slot_id not in host.migration_blocked
        assert connection.read_bytes() == connection_before
        assert data.read_bytes() == b"retained-generation-data"
        assert source.read_bytes() == b"preserved-package-source"
        assert legacy_binary.read_bytes() == b"preserved-legacy-bundle"
        native.assert_not_awaited()
        descriptor = migrations.acquire_legacy_lock(value)
        os.close(descriptor)
    finally:
        await host.close()
    # A new legacy owner can hold its original lock after explicit removal;
    # neither transfer initialization nor normal startup reacquires it.
    descriptor = migrations.acquire_legacy_lock(value)
    restarted = Supervisor(state, tmp_path / "unused")
    try:
        await restarted.initialize()
        assert not restarted.legacy_locks and not restarted.migration_blocked
        assert not restarted.registry.list_slots() and not restarted.listeners
        assert migrations.inspect_transfer(restarted, value["transfer_id"])["stage"] == "removed"
    finally:
        await restarted.close()
        os.close(descriptor)


@pytest.mark.parametrize("action", ["apply_transfer", "recover_transfer", "rollback_transfer"])
async def test_removed_transfer_cannot_be_replayed(tmp_path, monkeypatch, action):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="removed", status="completed")
    native = AsyncMock()
    monkeypatch.setattr(migrations, "native_handoff", native)
    try:
        with pytest.raises(HostError, match="migration_removed"):
            await getattr(migrations, action)(host, value["transfer_id"])
        native.assert_not_awaited()
        assert not host.tasks and not host.legacy_locks and not host.registry.list_slots()
    finally:
        await host.close()


async def test_removed_transfer_allows_a_fresh_explicit_migration_plan(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="removed", status="completed")
    app = Path(value["legacy_app"])
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.jshahbazi.jevk5"}))
    package = SimpleNamespace(digest="a" * 64, manifest=SimpleNamespace(package_id="local.jevk5", profile="model-service",
                                                                     model_dump=lambda: {"package_id": "local.jevk5"}))
    monkeypatch.setattr(migrations, "validate_package", lambda _: package)
    monkeypatch.setattr(migrations, "check_legacy_contract", lambda *_: None)
    native = AsyncMock(return_value={"running": False, "login_enabled": False})
    monkeypatch.setattr(migrations, "native_handoff", native)
    monkeypatch.setattr(migrations, "snapshot_package", lambda _package, destination:
                        SimpleNamespace(root=destination, digest="a" * 64))
    try:
        result = await migrations.plan_transfer(host, {"package": value["package_path"], "legacy_app": str(app),
                                                       "legacy_state": value["legacy_state"]})
        assert result["stage"] == "staged" and result["status"] == "planned"
        assert result["transfer_id"] != value["transfer_id"] and result["slot_id"] != value["slot_id"]
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == "removed"
        assert native.await_count == 1
        assert not host.legacy_locks and not host.registry.list_slots()
    finally:
        await host.close()


async def test_retirement_is_idempotent_and_does_not_reacquire_released_lock(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    await migrations.initialize_transfers(host)
    alias = legacy_alias(value)
    operation_id = str(uuid.uuid4())
    descriptor = None
    try:
        migrations.retire_transfer(host, value["slot_id"], operation_id)
        descriptor = migrations.acquire_legacy_lock(value)
        migrations.retire_transfer(host, value["slot_id"], operation_id)
        retired = migrations.inspect_transfer(host, value["transfer_id"])
        assert retired["removal_operation_id"] == operation_id and retired["stage"] == "removed"
        assert not host.legacy_locks and not alias.exists()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        await host.close()


async def test_failed_tombstone_write_keeps_lock_alias_and_removal_reservation(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    await migrations.initialize_transfers(host)
    alias = legacy_alias(value)
    saved = migrations.save
    failures = []
    def fail_once(_host, _value):
        if not failures:
            failures.append(True)
            raise OSError("fixture-write-failure")
        saved(_host, _value)
    monkeypatch.setattr(migrations, "save", fail_once)
    try:
        operation = host.begin_control(value["slot_id"], "remove")
        await drain(host)
        assert host.registry.get_operation(operation["operation_id"]).status == "recovery_required"
        assert host.registry.get_slot(value["slot_id"]).wake_suppressed
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == "committed"
        assert alias.exists() and value["slot_id"] in host.legacy_locks
        with pytest.raises(HostError, match="legacy_owner_conflict"):
            migrations.acquire_legacy_lock(value)
        retry = host.begin_control(value["slot_id"], "remove")
        assert retry["operation_id"] == operation["operation_id"]
        await drain(host)
        assert host.registry.get_operation(operation["operation_id"]).status == "completed"
        assert not alias.exists() and not host.legacy_locks
    finally:
        await host.close()


async def test_partial_retirement_write_keeps_lock_until_all_records_are_durable(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    duplicate = {**copy.deepcopy(value), "transfer_id": str(uuid.uuid4())}
    migrations.save(host, duplicate)
    host.retain_legacy_lock(value["slot_id"], migrations.acquire_legacy_lock(value))
    alias = legacy_alias(value)
    saved = migrations.save
    writes = []
    def fail_second(_host, changed):
        writes.append(changed["transfer_id"])
        if len(writes) == 2:
            raise OSError("fixture-write-failure")
        saved(_host, changed)
    monkeypatch.setattr(migrations, "save", fail_second)
    operation_id = str(uuid.uuid4())
    try:
        with pytest.raises(OSError):
            migrations.retire_transfer(host, value["slot_id"], operation_id)
        stages = {migrations.inspect_transfer(host, identity)["stage"] for identity in writes}
        assert stages == {"committed", "removed"}
        assert alias.exists() and value["slot_id"] in host.legacy_locks
        with pytest.raises(HostError, match="legacy_owner_conflict"):
            migrations.acquire_legacy_lock(value)
        migrations.retire_transfer(host, value["slot_id"], operation_id)
        assert all(migrations.inspect_transfer(host, identity)["stage"] == "removed" for identity in writes)
        assert not alias.exists() and not host.legacy_locks
    finally:
        await host.close()


async def test_crash_after_tombstone_rechecks_alias_ownership_before_removal(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="removed", status="completed")
    alias = legacy_alias(value)
    descriptor = migrations.acquire_legacy_lock(value)
    try:
        await migrations.initialize_transfers(host)
        assert not host.legacy_locks and not host.migration_blocked
        with pytest.raises(HostError, match="legacy_owner_conflict"):
            migrations.retire_transfer(host, value["slot_id"], str(uuid.uuid4()))
        assert alias.exists() and not host.legacy_locks
        os.close(descriptor)
        descriptor = None
        migrations.retire_transfer(host, value["slot_id"], str(uuid.uuid4()))
        assert not alias.exists() and not host.legacy_locks
    finally:
        if descriptor is not None:
            os.close(descriptor)
        alias.unlink(missing_ok=True)
        await host.close()


async def test_restart_replays_tombstoned_removal_without_resurrecting_gateway(tmp_path, monkeypatch):
    state = tmp_path / "state"
    host = Supervisor(state, tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    await migrations.initialize_transfers(host)
    alias = legacy_alias(value)
    real_unlink = Path.unlink
    def interrupted_unlink(path, *args, **kwargs):
        if path == alias:
            raise OSError("fixture-interrupted-alias-removal")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", interrupted_unlink)
    try:
        operation = host.begin_control(value["slot_id"], "remove")
        await drain(host)
        assert host.registry.get_operation(operation["operation_id"]).status == "recovery_required"
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == "removed"
        assert value["slot_id"] in host.legacy_locks and alias.exists()
    finally:
        await host.close()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    restarted = Supervisor(state, tmp_path / "unused")
    gateway = AsyncMock(side_effect=AssertionError("removed_gateway_must_not_start"))
    monkeypatch.setattr(restarted, "start_gateway", gateway)
    try:
        await restarted.initialize()
        gateway.assert_not_awaited()
        assert not restarted.registry.list_slots() and not restarted.listeners
        assert not restarted.legacy_locks and not restarted.migration_blocked and not alias.exists()
        assert restarted.registry.get_operation(operation["operation_id"]).status == "completed"
        assert migrations.inspect_transfer(restarted, value["transfer_id"])["stage"] == "removed"
    finally:
        alias.unlink(missing_ok=True)
        await restarted.close()


@pytest.mark.parametrize("stage,status", [("staged", "planned"), ("old_stopped", "recovery_required"),
    ("endpoint_acquired", "recovery_required"), ("committed", "running")])
async def test_retirement_rejects_noncommitted_or_running_transfer(tmp_path, stage, status):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage=stage, status=status)
    try:
        with pytest.raises(HostError, match="migration_removal_requires_committed_transfer"):
            migrations.retire_transfer(host, value["slot_id"], str(uuid.uuid4()))
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == stage
    finally:
        await host.close()


async def test_retirement_rejects_unresolved_owned_process(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    host.ledger(value["slot_id"])["unresolved"] = {"state": "ownership_lost"}
    try:
        with pytest.raises(HostError, match="ownership_reconciliation_required"):
            migrations.retire_transfer(host, value["slot_id"], str(uuid.uuid4()))
        assert migrations.inspect_transfer(host, value["transfer_id"])["stage"] == "committed"
    finally:
        await host.close()


async def test_retirement_rejects_unsafe_alias_without_removing_it_or_releasing_lock(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    await migrations.initialize_transfers(host)
    alias = legacy_alias(value)
    alias.unlink()
    alias.write_bytes(b"unsafe-regular-file")
    alias.chmod(0o600)
    try:
        with pytest.raises(ValueError, match="unsafe_private_transport"):
            migrations.retire_transfer(host, value["slot_id"], str(uuid.uuid4()))
        assert alias.read_bytes() == b"unsafe-regular-file"
        assert value["slot_id"] in host.legacy_locks
        with pytest.raises(HostError, match="legacy_owner_conflict"):
            migrations.acquire_legacy_lock(value)
    finally:
        alias.unlink(missing_ok=True)
        await host.close()
