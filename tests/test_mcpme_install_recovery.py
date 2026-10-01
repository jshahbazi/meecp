"""Failure and race checks at the installation publication/recovery boundary."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import pytest

from mcpme.contracts import validate_package
from mcpme.private import read_private_json
from mcpme.registry import RegistryError
from mcpme.supervisor import HostError, Supervisor
from test_mcpme_packages import make_package


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
        data.mkdir(mode=0o700)
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
    async def check_candidate(slot):
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
