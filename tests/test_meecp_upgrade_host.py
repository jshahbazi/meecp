"""Upgrade failure boundaries against real private files and SQLite transactions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest

import meecp.upgrade_host as upgrade_host
from meecp.contracts import validate_package
from meecp.grants import read_credential
from meecp.private import ensure_private_directory, read_private_json, snapshot_package, write_private_json
from meecp.processes import OwnershipLost
from meecp.registry import RegistryError
from meecp.settings import normalize_settings, settings_digest
from meecp.supervisor import HostError, Supervisor, bind_listener, management_app
from meecp.upgrades import UpgradeError, recovery_action
from test_meecp_packages import edit_manifest, make_package


def data_file(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(0o600)


class UpgradeFixture:
    def __init__(self, tmp_path: Path, *, running: bool = True, configured: bool = False):
        self.host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
        old_source = make_package(tmp_path / "old-source")
        next_source = make_package(tmp_path / "candidate-source")
        configuration = {"schema_version": 1, "fields": {"enabled": {"type": "boolean", "default": False}}}
        if configured:
            edit_manifest(old_source, configuration=configuration)
        edit_manifest(next_source, version="2.0.0", **({"configuration": configuration} if configured else {}))
        old = validate_package(old_source)
        self.candidate_source = next_source
        self.new_digest = validate_package(next_source).digest
        sealed = snapshot_package(old, self.host.root / "packages" / old.manifest.package_id / old.digest)
        listener = bind_listener()
        self.slot = self.host.registry.add_slot(package_digest=sealed.digest, package_path=sealed.root,
            name="Synthetic persistent service", public_port=listener.getsockname()[1],
            settings_digest=settings_digest(normalize_settings(old.manifest)))
        self.slot = self.host.registry.update_slot(self.slot.id, desired_state="running" if running else "stopped",
            observed_state="ready" if running else "stopped")
        self.host.listeners[self.slot.id] = listener
        self.directory = self.host.slot_directory(self.slot.id)
        self.old_data = ensure_private_directory(self.directory / "data")
        data_file(self.old_data / "state.bin", b"old generation data")
        write_private_json(self.directory / "settings.json", normalize_settings(old.manifest))
        self.connection = {"token": "synthetic-invocation-token", "port": self.slot.public_port}
        write_private_json(self.directory / "connection.json", self.connection)
        write_private_json(self.directory / "activation.json", {"generation": self.slot.generation,
            "verified": True, "exposure_permitted": running, "legacy_jev": False})
        self.gateways = []
        self.probes = []
        self.host.stop_gateway = AsyncMock()
        self.host.stop_residents = AsyncMock()

        async def gateway(slot, active: bool, **kwargs):
            self.gateways.append((slot.to_dict(), active, kwargs.get("inactive_code")))
        async def candidate(slot, **kwargs):
            self.probes.append(slot.to_dict())
            # This is a synthetic package-owned probe write; the host must give
            # it the inactive clone rather than the old generation's directory.
            if slot.package_digest == self.new_digest:
                data = self.host.data_directory(slot)
                data_file(data / "probe.bin", b"candidate probe data")
        self.host.start_gateway = gateway
        self.host.check_candidate = candidate

    async def plan(self, **changes):
        return await self.host.plan_upgrade({"slot_id": self.slot.id, "package": str(self.candidate_source), **changes})

    async def apply(self, plan: dict):
        operation = await self.host.apply_upgrade(plan["plan_id"])
        await settle(self.host, operation["operation_id"])
        return self.host.registry.get_operation(operation["operation_id"])


async def settle(host: Supervisor, operation_id: str) -> None:
    task = next((task for task in host.tasks if getattr(task, "operation_id", None) == operation_id), None)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 5)
    await asyncio.sleep(0)


async def test_successful_upgrade_keeps_identity_connection_and_old_data(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        operation = await fixture.apply(plan)
        selected = host.registry.get_slot(fixture.slot.id)
        assert operation.status == "completed"
        assert operation.details["selected"] and operation.details["exposed"]
        assert selected.generation == fixture.slot.generation + 1
        assert selected.package_digest == fixture.new_digest
        assert selected.public_port == fixture.slot.public_port
        assert selected.credential_ref == fixture.slot.credential_ref
        assert host.connection(selected.id) == fixture.connection
        assert Path(selected.data_path).joinpath("state.bin").read_bytes() == b"old generation data"
        assert Path(selected.data_path).joinpath("probe.bin").read_bytes() == b"candidate probe data"
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
        assert not (fixture.old_data / "probe.bin").exists()
        assert host.upgrades.get(operation.id)["status"] == "completed"
        assert fixture.gateways[0][1:] == (False, "updating")
        assert fixture.gateways[-1][1] is True
        assert (await host.apply_upgrade(plan["plan_id"]))["operation_id"] == operation.id
        assert len([item for item in host.registry.list_operations() if item.action == "upgrade"]) == 1
    finally:
        await host.close()


@pytest.mark.parametrize("stage", ["staging", "quiesced", "snapshot_ready", "candidate_verified", "selected"])
async def test_fault_at_each_preexposure_boundary_restores_original_generation_data(tmp_path, stage):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    async def checkpoint(operation_id, current):
        if current == stage:
            raise UpgradeError("synthetic_upgrade_failure")
        await asyncio.sleep(0)
    host.upgrade_checkpoint = checkpoint
    try:
        plan = await fixture.plan()
        operation = await fixture.apply(plan)
        restored = host.registry.get_slot(fixture.slot.id)
        assert operation.status == "failed"
        assert operation.error_code == "synthetic_upgrade_failure"
        assert restored.package_digest == fixture.slot.package_digest
        assert restored.package_path == fixture.slot.package_path
        assert restored.data_path == fixture.slot.data_path
        assert restored.settings_path == fixture.slot.settings_path
        assert restored.settings_digest == fixture.slot.settings_digest
        assert restored.public_port == fixture.slot.public_port
        assert restored.generation > fixture.slot.generation
        assert restored.observed_state == "ready"
        assert host.connection(restored.id) == fixture.connection
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
        assert not (fixture.old_data / "probe.bin").exists()
        record = host.upgrades.get(operation.id)
        assert record["status"] == "rolled_back" and not record["exposed"]
        assert recovery_action(record) == "restore_old"
        assert fixture.gateways[-1][0]["package_digest"] == fixture.slot.package_digest
        assert fixture.gateways[-1][1]
    finally:
        await host.close()


async def test_exposed_fault_preserves_candidate_and_authorized_recovery(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    async def checkpoint(operation_id, stage):
        if stage == "exposed":
            selected = host.registry.get_slot(fixture.slot.id)
            data_file(Path(selected.data_path) / "accepted.bin", b"accepted client write")
            raise UpgradeError("synthetic_routing_failure")
    host.upgrade_checkpoint = checkpoint
    try:
        plan = await fixture.plan()
        operation = await fixture.apply(plan)
        selected = host.registry.get_slot(fixture.slot.id)
        assert operation.status == "recovery_required"
        assert selected.package_digest == fixture.new_digest
        assert selected.error_code == "upgrade_recovery_required"
        assert Path(selected.data_path).joinpath("accepted.bin").read_bytes() == b"accepted client write"
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
        assert not any(slot["package_digest"] == fixture.slot.package_digest and active for slot, active, _ in fixture.gateways)
        assert recovery_action(host.upgrades.get(operation.id)) == "recover_candidate"
        with pytest.raises(UpgradeError, match="^upgrade_already_exposed$"):
            await host.cancel_operation(operation.id)
        await host.recover_operation(operation.id)
        await settle(host, operation.id)
        assert host.registry.get_operation(operation.id).status == "completed"
        recovered = host.registry.get_slot(fixture.slot.id)
        assert recovered.package_digest == fixture.new_digest
        assert recovered.data_path == selected.data_path
        assert Path(recovered.data_path).joinpath("accepted.bin").read_bytes() == b"accepted client write"
    finally:
        await host.close()


async def test_failure_after_routing_and_journal_completion_preserves_accepted_write(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    original_gateway = host.start_gateway
    async def gateway(slot, active, **kwargs):
        if active and slot.package_digest == fixture.new_digest:
            data_file(Path(slot.data_path) / "accepted.bin", b"write after routing")
        await original_gateway(slot, active, **kwargs)
    host.start_gateway = gateway
    update = host.registry.update_operation
    failed = False
    def fault(operation_id, status, **kwargs):
        nonlocal failed
        if status == "completed" and not failed:
            failed = True
            raise RegistryError("registry_unavailable")
        return update(operation_id, status, **kwargs)
    monkeypatch.setattr(host.registry, "update_operation", fault)
    try:
        operation = await fixture.apply(await fixture.plan())
        assert operation.status == "recovery_required"
        selected = host.registry.get_slot(fixture.slot.id)
        assert selected.package_digest == fixture.new_digest
        assert (Path(selected.data_path) / "accepted.bin").read_bytes() == b"write after routing"
        assert not any(slot["package_digest"] == fixture.slot.package_digest and active for slot, active, _ in fixture.gateways)
        await host.recover_operation(operation.id)
        await settle(host, operation.id)
        assert host.registry.get_operation(operation.id).status == "completed"
        assert (Path(selected.data_path) / "accepted.bin").read_bytes() == b"write after routing"
    finally:
        await host.close()


async def test_exposure_registry_marker_survives_secondary_journal_failure(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    original = host.upgrades.update
    failed = False
    def fault(operation_id, stage, **kwargs):
        nonlocal failed
        if stage == "exposed" and not failed:
            failed = True
            raise UpgradeError("upgrades_unavailable")
        return original(operation_id, stage, **kwargs)
    monkeypatch.setattr(host.upgrades, "update", fault)
    try:
        operation = await fixture.apply(await fixture.plan())
        assert operation.status == "recovery_required"
        assert operation.details["exposed"]
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.new_digest
        assert host.upgrades.get(operation.id)["exposed"]
        assert recovery_action(host.upgrades.get(operation.id)) == "recover_candidate"
    finally:
        await host.close()


async def test_stopped_configuration_upgrade_verifies_candidate_and_remains_stopped(tmp_path):
    fixture = UpgradeFixture(tmp_path, running=False, configured=True)
    host = fixture.host
    try:
        plan = await fixture.plan(settings={"configuration": {"enabled": True}, "secret_references": {}})
        operation = await fixture.apply(plan)
        selected = host.registry.get_slot(fixture.slot.id)
        assert operation.status == "completed"
        assert selected.desired_state == "stopped" and selected.observed_state == "stopped"
        assert host.package_settings(selected)["configuration"] == {"enabled": True}
        assert read_private_json(fixture.directory / "settings.json")["configuration"] == {"enabled": False}
        assert fixture.probes and fixture.probes[0]["package_digest"] == fixture.new_digest
        assert all(not active for _, active, _ in fixture.gateways)
    finally:
        await host.close()


@pytest.mark.parametrize("failure_stage", ["candidate_verified", "exposed"])
async def test_business_smoke_writes_only_inactive_candidate_and_never_replays_over_accepted_data(tmp_path, failure_stage):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    probes = []
    data_file(fixture.old_data / "accepted.bin", b"accepted old client write")
    async def business_probe(slot, *, business_smoke=True):
        probes.append((slot.package_digest, business_smoke))
        if business_smoke:
            data_file(host.data_directory(slot) / "accepted.bin", b"synthetic business smoke write")
    async def checkpoint(operation_id, stage):
        if stage == failure_stage:
            if stage == "exposed":
                selected = host.registry.get_slot(fixture.slot.id)
                data_file(host.data_directory(selected) / "accepted.bin", b"accepted new client write")
            raise UpgradeError("synthetic_routing_failure")
    host.check_candidate = business_probe
    host.upgrade_checkpoint = checkpoint
    try:
        operation = await fixture.apply(await fixture.plan())
        if failure_stage == "exposed":
            assert operation.status == "recovery_required"
            await host.recover_operation(operation.id)
            await settle(host, operation.id)
            assert host.registry.get_operation(operation.id).status == "completed"
            expected = b"accepted new client write"
        else:
            assert operation.status == "failed"
            candidate = host.upgrades.get(operation.id)["candidate"]
            assert Path(candidate["data_path"]).joinpath("accepted.bin").read_bytes() == b"synthetic business smoke write"
            expected = b"accepted old client write"
        selected = host.registry.get_slot(fixture.slot.id)
        selected_data = host.data_directory(selected)
        assert (selected_data / "accepted.bin").read_bytes() == expected
        assert (fixture.old_data / "accepted.bin").read_bytes() == b"accepted old client write"
        await host.control(selected.id, "stop")
        await host.control(selected.id, "start")
        await host.control(selected.id, "restart")
        assert (selected_data / "accepted.bin").read_bytes() == expected
        assert probes[0] == (fixture.new_digest, True)
        assert all(not business for _, business in probes[1:])
    finally:
        await host.close()


async def test_upgrade_plan_cannot_apply_after_slot_generation_changed(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        host.registry.update_slot(fixture.slot.id, generation=fixture.slot.generation + 1)
        with pytest.raises(UpgradeError, match="^stale_generation$"):
            await host.apply_upgrade(plan["plan_id"])
        assert not host.registry.list_operations()
        assert not host.upgrades.list_unfinished()
        assert fixture.gateways == []
    finally:
        await host.close()


async def test_upgrade_plan_cannot_apply_after_sealed_candidate_changed(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        # Simulate deliberate owner tampering past the accidental-write guard.
        for name in ("meecp.package.json", "meecp.lock.json"):
            (Path(plan["package_path"]) / name).chmod(0o600)
        edit_manifest(Path(plan["package_path"]), version="3.0.0")
        submitted = await host.apply_upgrade(plan["plan_id"])
        await settle(host, submitted["operation_id"])
        operation = host.registry.get_operation(submitted["operation_id"])
        assert operation.status == "failed" and operation.error_code == "package_digest_mismatch"
        assert not operation.details["selected"] and not operation.details["exposed"]
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.slot.package_digest
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
    finally:
        await host.close()


async def test_upgrade_apply_returns_durable_intent_before_slow_hashing_and_old_quiescence(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    plan = await fixture.plan()
    entered, release = threading.Event(), threading.Event()
    original = upgrade_host.validate_package
    def slow_validation(path):
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(upgrade_host, "validate_package", slow_validation)
    try:
        submitted = await asyncio.wait_for(host.apply_upgrade(plan["plan_id"]), 1)
        assert submitted["status"] == "pending"
        assert await asyncio.to_thread(entered.wait, 1)
        operation = host.registry.get_operation(submitted["operation_id"])
        assert operation.status == "running"
        assert host.upgrades.get(operation.id)["status"] == "staging"
        assert host.registry.get_slot(fixture.slot.id).to_dict() == fixture.slot.to_dict()
        host.stop_gateway.assert_not_awaited()
        host.stop_residents.assert_not_awaited()
        assert host.activation(fixture.slot.id)["exposure_permitted"]
        assert (await host.apply_upgrade(plan["plan_id"]))["operation_id"] == operation.id
        release.set()
        await settle(host, operation.id)
        assert host.registry.get_operation(operation.id).status == "completed"
    finally:
        release.set()
        await host.close()


async def test_unresolved_old_writer_blocks_upgrade_without_guessing_or_cloning(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        host.ledger(fixture.slot.id)["lost"] = {"state": "ownership_lost"}
        with pytest.raises(HostError, match="^ownership_lost$"):
            await fixture.plan()
        assert fixture.gateways == []
        assert not host.registry.list_operations()
    finally:
        host.ledger(fixture.slot.id).clear()
        await host.close()


async def test_snapshot_disk_failure_restores_old_and_retains_data(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    def unavailable(*args, **kwargs):
        raise UpgradeError("upgrade_disk_unavailable")
    monkeypatch.setattr(upgrade_host, "snapshot_data", unavailable)
    try:
        operation = await fixture.apply(await fixture.plan())
        assert operation.status == "failed" and operation.error_code == "upgrade_disk_unavailable"
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.slot.package_digest
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
        assert not (fixture.old_data / "probe.bin").exists()
    finally:
        await host.close()


@pytest.mark.parametrize("stage", ["candidate_verified", "exposed"])
async def test_real_ownership_lost_error_retains_ledger_and_reports_recovery(tmp_path, stage):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    lost = SimpleNamespace(instance_id="lost", ownership_lost=True, stop=AsyncMock(side_effect=OwnershipLost()))
    async def checkpoint(operation_id, current):
        if current == stage:
            host.owner(fixture.slot.id).instances[lost.instance_id] = lost
            host.ledger(fixture.slot.id)[lost.instance_id] = {"state": "ownership_lost"}
            host.registry.update_slot(fixture.slot.id, observed_state="ownership_lost", error_code="ownership_lost")
            raise OwnershipLost()
    host.upgrade_checkpoint = checkpoint
    try:
        operation = await fixture.apply(await fixture.plan())
        assert operation.status == "recovery_required" and operation.error_code == "ownership_lost"
        slot = host.registry.get_slot(fixture.slot.id)
        assert slot.observed_state == "ownership_lost" and slot.error_code == "ownership_lost"
        assert host.ledger(slot.id)[lost.instance_id]["state"] == "ownership_lost"
        assert host.owner(slot.id).instances[lost.instance_id] is lost
        assert not any(active for _, active, _ in fixture.gateways)
        if stage == "exposed":
            assert slot.package_digest == fixture.new_digest
            assert operation.details["exposed"]
        else:
            assert slot.package_digest == fixture.slot.package_digest
        with pytest.raises(UpgradeError, match="^ownership_lost$"):
            await host.recover_operation(operation.id)
    finally:
        await host.close()


async def test_cancel_waits_for_snapshot_writer_before_compensation(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    entered, released, settled = threading.Event(), threading.Event(), threading.Event()
    def blocking(source, destination, cancel):
        entered.set()
        try:
            assert released.wait(3)
            assert cancel.is_set()
            raise UpgradeError("upgrade_canceled")
        finally:
            settled.set()
    monkeypatch.setattr(upgrade_host, "snapshot_data", blocking)
    try:
        operation = await host.apply_upgrade((await fixture.plan())["plan_id"])
        task = next(task for task in host.tasks if getattr(task, "operation_id", None) == operation["operation_id"])
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done() and not settled.is_set()
        assert host.registry.get_operation(operation["operation_id"]).status == "running"
        assert not any(active for _, active, _ in fixture.gateways)
        released.set()
        await asyncio.wait_for(asyncio.shield(task), 3)
        assert settled.is_set()
        assert host.registry.get_operation(operation["operation_id"]).status == "canceled"
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.slot.package_digest
    finally:
        released.set()
        await host.close()


async def test_explicit_cancel_signals_writer_and_uses_same_operation(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    entered, released = threading.Event(), threading.Event()
    def blocking(source, destination, cancel):
        entered.set()
        assert released.wait(3)
        assert cancel.is_set()
        raise UpgradeError("upgrade_canceled")
    monkeypatch.setattr(upgrade_host, "snapshot_data", blocking)
    try:
        plan = await fixture.plan()
        operation = await host.apply_upgrade(plan["plan_id"])
        assert await asyncio.to_thread(entered.wait, 2)
        canceled = await host.cancel_operation(operation["operation_id"])
        assert canceled["details"]["cancel_requested"]
        assert host.upgrades.get(operation["operation_id"])["cancel_requested"]
        assert (await host.apply_upgrade(plan["plan_id"]))["operation_id"] == operation["operation_id"]
        released.set()
        await settle(host, operation["operation_id"])
        assert host.registry.get_operation(operation["operation_id"]).status == "canceled"
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.slot.package_digest
    finally:
        released.set()
        await host.close()


async def test_cancel_secondary_journal_failure_still_signals_owned_writer(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    entered, released = threading.Event(), threading.Event()
    def blocking(source, destination, cancel):
        entered.set()
        assert released.wait(3)
        assert cancel.is_set()
        raise UpgradeError("upgrade_canceled")
    def failure(operation_id):
        raise UpgradeError("upgrades_unavailable")
    monkeypatch.setattr(upgrade_host, "snapshot_data", blocking)
    monkeypatch.setattr(host.upgrades, "request_cancel", failure)
    try:
        operation = await host.apply_upgrade((await fixture.plan())["plan_id"])
        assert await asyncio.to_thread(entered.wait, 2)
        canceled = await host.cancel_operation(operation["operation_id"])
        assert canceled["details"]["cancel_requested"]
        released.set()
        await settle(host, operation["operation_id"])
        assert host.registry.get_operation(operation["operation_id"]).status == "canceled"
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.slot.package_digest
    finally:
        released.set()
        await host.close()


async def test_shutdown_retains_state_lock_until_snapshot_writer_settles(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    entered, released, settled = threading.Event(), threading.Event(), threading.Event()
    def blocking(source, destination, cancel):
        entered.set()
        try:
            assert released.wait(3)
            assert cancel.is_set()
            raise UpgradeError("upgrade_canceled")
        finally:
            settled.set()
    monkeypatch.setattr(upgrade_host, "snapshot_data", blocking)
    closed = False
    try:
        await host.apply_upgrade((await fixture.plan())["plan_id"])
        assert await asyncio.to_thread(entered.wait, 2)
        shutdown = asyncio.create_task(host.close())
        await asyncio.sleep(0.02)
        assert not shutdown.done()
        with pytest.raises(HostError, match="^host_already_running$"):
            Supervisor(host.root, tmp_path / "unused-reaper")
        shutdown.cancel()
        await asyncio.sleep(0.02)
        assert not settled.is_set()
        with pytest.raises(HostError, match="^host_already_running$"):
            Supervisor(host.root, tmp_path / "unused-reaper")
        released.set()
        try:
            await asyncio.wait_for(shutdown, 3)
            closed = True
        except asyncio.CancelledError:
            assert settled.is_set()
        if not closed:
            await host.close()
            closed = True
        restarted = Supervisor(host.root, tmp_path / "unused-reaper")
        try:
            assert settled.is_set()
        finally:
            await restarted.close()
    finally:
        released.set()
        if not closed:
            await host.close()


async def test_exposed_missing_data_directory_is_not_silently_recreated(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    async def checkpoint(operation_id, stage):
        if stage == "exposed":
            raise UpgradeError("synthetic_routing_failure")
    host.upgrade_checkpoint = checkpoint
    try:
        operation = await fixture.apply(await fixture.plan())
        selected = host.registry.get_slot(fixture.slot.id)
        shutil.rmtree(selected.data_path)
        await host.recover_operation(operation.id)
        await settle(host, operation.id)
        assert host.registry.get_operation(operation.id).status == "recovery_required"
        assert not Path(selected.data_path).exists()
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.new_digest
    finally:
        await host.close()


async def test_registry_selection_is_atomic_with_operation_marker(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        old = host.registry.get_slot(fixture.slot.id)
        operation = host.registry.create_operation("upgrade", old.id, generation=old.generation,
            details={"selected": False, "exposed": False, "cancel_requested": False})
        host.registry.update_operation(operation.id, "running")
        candidate = replace(old, generation=old.generation + 1, package_digest=fixture.new_digest)
        # A SQLite trigger fails the second half of the atomic selection.
        host.registry._connection.execute("CREATE TRIGGER fail_upgrade_marker BEFORE UPDATE OF details ON operations BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END")
        with pytest.raises(RegistryError, match="^registry_unavailable$"):
            host.registry.select_upgrade(operation.id, candidate, expected_generation=old.generation)
        assert host.registry.get_slot(old.id) == old
        assert not host.registry.get_operation(operation.id).details["selected"]
        host.registry._connection.execute("DROP TRIGGER fail_upgrade_marker")
        selected = host.registry.select_upgrade(operation.id, candidate, expected_generation=old.generation)
        assert selected == candidate
        assert host.registry.get_operation(operation.id).details["selected"]
        operation = host.registry.expose_upgrade(operation.id, expected_generation=candidate.generation)
        assert operation.details["exposed"]
        with pytest.raises(RegistryError, match="^operation_conflict$"):
            host.registry.select_upgrade(operation.id, replace(old, generation=candidate.generation + 1),
                expected_generation=candidate.generation, selected=False)
    finally:
        await host.close()


async def test_startup_does_not_reclassify_its_just_recovered_gateway_as_unknown_ownership(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    async def crash(operation_id, stage):
        if stage == "exposed":
            selected = host.registry.get_slot(fixture.slot.id)
            data_file(host.data_directory(selected) / "accepted.bin", b"accepted synthetic client write")
            raise UpgradeError("synthetic_routing_failure")
    host.upgrade_checkpoint = crash
    operation = await fixture.apply(await fixture.plan())
    assert operation.status == "recovery_required"
    await host.close()
    restarted = Supervisor(host.root, tmp_path / "unused-reaper")
    restarted.check_candidate = AsyncMock()
    restarted.stop_gateway = AsyncMock()
    restarted.stop_residents = AsyncMock()
    identity = uuid.uuid4().hex
    async def recovered_gateway(slot, active, **kwargs):
        assert active
        owner = restarted.owner(slot.id)
        loop = asyncio.get_running_loop()
        process = SimpleNamespace(owner=owner, instance_id=identity, pid=42424, ownership_lost=False,
            returncode=None, wrapper=SimpleNamespace(returncode=None), _done=loop.create_future(), _monitor=loop.create_future())
        async def stop_owned():
            owner.instances.pop(identity, None)
        process.stop = AsyncMock(side_effect=stop_owned)
        owner.instances[identity] = process
        restarted.ledger(slot.id)[identity] = {"state": "owned", "role": "gateway", "generation": slot.generation}
        restarted.save_ledger(slot.id)
    restarted.start_gateway = recovered_gateway
    try:
        await restarted.initialize()
        selected = restarted.registry.get_slot(fixture.slot.id)
        assert restarted.registry.get_operation(operation.id).status == "completed"
        assert selected.observed_state == "ready" and selected.error_code is None
        restarted.check_recovery_ownership(selected.id)
        assert set(restarted.ledger(selected.id)) == {identity}
        assert (restarted.data_directory(selected) / "accepted.bin").read_bytes() == b"accepted synthetic client write"
    finally:
        await restarted.close()


@pytest.mark.parametrize("stage", ["staging", "quiesced", "snapshot_ready", "candidate_verified", "selected", "exposed"])
async def test_restart_recovers_each_durable_boundary_from_actual_state(tmp_path, stage):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    async def crash(operation_id, current):
        if current == stage:
            if current == "exposed":
                selected = host.registry.get_slot(fixture.slot.id)
                data_file(Path(selected.data_path) / "accepted.bin", b"accepted before restart")
            raise UpgradeError("synthetic_host_exit")
    host.upgrade_checkpoint = crash
    # Simulate process disappearance, rather than ordinary failure compensation.
    host.fail_upgrade = AsyncMock()
    operation = await fixture.apply(await fixture.plan())
    assert operation.status == "running"
    selected_before = host.registry.get_slot(fixture.slot.id)
    await host.close()
    restarted = Supervisor(host.root, tmp_path / "unused-reaper")
    gateways = []
    restarted.stop_gateway = AsyncMock()
    restarted.stop_residents = AsyncMock()
    restarted.check_candidate = AsyncMock()
    async def gateway(slot, active, **kwargs):
        gateways.append((slot.to_dict(), active))
    restarted.start_gateway = gateway
    try:
        await restarted.initialize()
        recovered = restarted.registry.get_slot(fixture.slot.id)
        final = restarted.registry.get_operation(operation.id)
        assert recovered.public_port == fixture.slot.public_port
        assert restarted.connection(fixture.slot.id) == fixture.connection
        assert gateways and gateways[-1][1]
        if stage == "exposed":
            assert recovered.package_digest == fixture.new_digest
            assert recovered.data_path == selected_before.data_path
            assert Path(recovered.data_path).joinpath("accepted.bin").read_bytes() == b"accepted before restart"
            assert final.status == "completed"
            assert all(row["package_digest"] == fixture.new_digest for row, _ in gateways)
        else:
            assert recovered.package_digest == fixture.slot.package_digest
            assert recovered.data_path == fixture.slot.data_path
            assert final.status == "failed"
            assert restarted.upgrades.get(operation.id)["status"] == "rolled_back"
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
    finally:
        await restarted.close()


async def test_restart_exposed_marker_cannot_be_downgraded_when_journal_is_missing(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    async def crash(operation_id, stage):
        if stage == "exposed":
            selected = host.registry.get_slot(fixture.slot.id)
            data_file(Path(selected.data_path) / "accepted.bin", b"accepted before journal loss")
            raise UpgradeError("synthetic_host_exit")
    host.upgrade_checkpoint = crash
    host.fail_upgrade = AsyncMock()
    operation = await fixture.apply(await fixture.plan())
    selected = host.registry.get_slot(fixture.slot.id)
    (host.upgrades.records / (operation.id + ".json")).unlink()
    await host.close()
    restarted = Supervisor(host.root, tmp_path / "unused-reaper")
    restarted.start_gateway = AsyncMock()
    restarted.stop_gateway = AsyncMock()
    restarted.stop_residents = AsyncMock()
    restarted.check_candidate = AsyncMock()
    try:
        await restarted.initialize()
        assert restarted.registry.get_operation(operation.id).status == "recovery_required"
        assert restarted.registry.get_slot(fixture.slot.id).error_code == "upgrade_recovery_required"
        assert restarted.registry.get_slot(fixture.slot.id).package_digest == fixture.new_digest
        assert Path(selected.data_path).joinpath("accepted.bin").read_bytes() == b"accepted before journal loss"
        restarted.start_gateway.assert_not_awaited()
    finally:
        await restarted.close()


async def test_restart_intent_before_first_journal_write_restores_original_runtime(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    plan = await fixture.plan()
    operation = host.registry.create_operation("upgrade", fixture.slot.id, generation=fixture.slot.generation,
        idempotency_key=plan["plan_id"], details={"plan_id": plan["plan_id"], "package_digest": plan["package_digest"],
            "package_path": plan["package_path"], "settings_digest": plan["settings_digest"],
            "selected": False, "exposed": False, "cancel_requested": False})
    await host.close()
    restarted = Supervisor(host.root, tmp_path / "unused-reaper")
    restarted.start_gateway = AsyncMock()
    restarted.stop_gateway = AsyncMock()
    restarted.stop_residents = AsyncMock()
    restarted.check_candidate = AsyncMock()
    try:
        await restarted.initialize()
        assert restarted.registry.get_operation(operation.id).status == "failed"
        restored = restarted.registry.get_slot(fixture.slot.id)
        assert restored.package_digest == fixture.slot.package_digest
        assert restored.observed_state == "ready"
        assert fixture.old_data.joinpath("state.bin").read_bytes() == b"old generation data"
        assert restarted.start_gateway.await_count == 1
        assert restarted.start_gateway.await_args.kwargs["active"] is True
    finally:
        await restarted.close()


async def test_upgrade_api_owner_routes_grant_denial_and_transport_checks(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        # Planning/read grants can inspect reviewed upgrades. Execution still
        # needs the exact upgrade grant; install and lifecycle grants cannot apply.
        install = host.plan_install({"package": str(fixture.candidate_source)})
        reviewed = await fixture.plan()
        grants = [host.grants.issue({"mode": mode}) for mode in ("read", "prepare")]
        grants += [host.grants.issue({"mode": "lifecycle", "slot_ids": [fixture.slot.id], "actions": ["stop"]}),
                   host.grants.issue({"mode": "install", "plan_id": install["plan_id"]}, plan=install)]
        identity = str(uuid.uuid4())
        requests = [("POST", "/v1/upgrade/plan", {}), ("POST", "/v1/upgrade/apply", {}),
                    ("GET", "/v1/upgrade/plans/" + reviewed["plan_id"], None),
                    ("POST", "/v1/operations/" + identity + "/cancel", {}),
                    ("POST", "/v1/operations/" + identity + "/recover", {})]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                                     base_url="http://127.0.0.1:19384") as api:
            for grant in grants:
                token = read_credential(Path(grant["credential_file"]))["token"]
                for method, route, body in requests:
                    if grant["mode"] == "prepare" and route == "/v1/upgrade/plan":
                        response = await api.post(route, json={"slot_id": fixture.slot.id,
                            "package": str(fixture.candidate_source)}, headers={"Authorization": "Bearer " + token})
                        assert response.status_code == 200
                        continue
                    response = await api.request(method, route, json=body, headers={"Authorization": "Bearer " + token})
                    if grant["mode"] in {"read", "prepare"} and method == "GET":
                        assert response.status_code == 200
                        continue
                    assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            for method, route, body in requests:
                assert (await api.request(method, route, json=body)).status_code == 401
            for unsafe in ({"Host": "external.example"}, {"Origin": "https://external.example"}):
                response = await api.post("/v1/upgrade/plan", json={}, headers={"Authorization": "Bearer owner", **unsafe})
                assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
            assert host.registry.list_operations() == []
            owner = {"Authorization": "Bearer owner"}
            response = await api.post("/v1/upgrade/plan", json={"slot_id": fixture.slot.id,
                "package": str(fixture.candidate_source)}, headers=owner)
            assert response.status_code == 200
            plan = response.json()
            inspected = await api.get("/v1/upgrade/plans/" + plan["plan_id"], headers=owner)
            assert inspected.status_code == 200 and inspected.json()["plan_digest"] == plan["plan_digest"]
            applied = await api.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]}, headers=owner)
            assert applied.status_code == 200
            operation = applied.json()
            await settle(host, operation["operation_id"])
            observed = await api.get("/v1/operations/" + operation["operation_id"], headers=owner)
            assert observed.status_code == 200 and observed.json()["status"] == "completed"
            canceled = await api.post("/v1/operations/" + operation["operation_id"] + "/cancel", json={}, headers=owner)
            assert canceled.status_code == 400 and canceled.json()["error"]["code"] == "upgrade_already_exposed"
            malformed = await api.post("/v1/operations/" + operation["operation_id"] + "/recover", json={"force": True}, headers=owner)
            assert malformed.status_code == 400 and malformed.json()["error"]["code"] == "invalid_operation_request"
    finally:
        await host.close()
