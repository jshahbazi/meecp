"""Independent review regressions for user-visible recovery boundaries."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from meecp.supervisor import Supervisor
from meecp.upgrades import UpgradeError
from test_meecp_packages import make_package
from test_meecp_upgrade_host import UpgradeFixture


async def settle_install(host, operation_id):
    task = next(task for task in host.tasks if getattr(task, "operation_id", None) == operation_id)
    await asyncio.wait_for(asyncio.shield(task), 5)
    await asyncio.sleep(0)


async def test_install_recovery_cannot_reopen_admission_while_stop_is_draining(tmp_path):
    """A recovery click must serialize behind an already accepted Stop."""
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    host.check_candidate = AsyncMock()
    host.stop_gateway = AsyncMock()
    host.stop_residents = AsyncMock()
    host.start_gateway = AsyncMock()
    stop_entered, release_stop = asyncio.Event(), asyncio.Event()
    stop_task = None

    async def interrupt(operation_id, stage):
        if stage == "completed":
            raise asyncio.CancelledError()

    host.install_checkpoint = interrupt
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "package")), "start": True})
        operation = await host.apply_install(plan["plan_id"])
        await settle_install(host, operation["operation_id"])
        assert host.registry.get_operation(operation["operation_id"]).status == "recovery_required"
        assert host.registry.get_slot(plan["slot_id"]).desired_state == "running"
        host.install_checkpoint = AsyncMock()
        opened = []

        async def gateway(slot, active):
            opened.append(active)

        calls = 0
        async def stop_gateway(slot_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                stop_entered.set()
                await release_stop.wait()

        host.stop_gateway = stop_gateway
        host.start_gateway = gateway
        stop_task = asyncio.create_task(host.control(plan["slot_id"], "stop"))
        await asyncio.wait_for(stop_entered.wait(), 2)
        assert host.locks[plan["slot_id"]].locked()
        await host.recover_operation(operation["operation_id"])
        # Give the scheduled recovery enough time to reach its publication gate.
        await asyncio.sleep(0.2)
        assert True not in opened, "Recovery opened ordinary traffic while Stop held the lifecycle lock"
    finally:
        release_stop.set()
        if stop_task is not None:
            await asyncio.gather(stop_task, return_exceptions=True)
        await host.close()


async def test_install_recovery_preserves_a_later_explicit_stop(tmp_path):
    """Recovery verifies retained data without replaying install-and-start intent."""
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    host.check_candidate = AsyncMock()
    host.stop_gateway = AsyncMock()
    host.stop_residents = AsyncMock()
    host.start_gateway = AsyncMock()

    async def interrupt(operation_id, stage):
        if stage == "completed":
            raise asyncio.CancelledError()

    host.install_checkpoint = interrupt
    try:
        plan = host.plan_install({"package": str(make_package(tmp_path / "package")), "start": True})
        operation = await host.apply_install(plan["plan_id"])
        await settle_install(host, operation["operation_id"])
        host.install_checkpoint = AsyncMock()
        await host.control(plan["slot_id"], "stop")
        assert host.registry.get_slot(plan["slot_id"]).desired_state == "stopped"
        host.start_gateway.reset_mock()
        await host.recover_operation(operation["operation_id"])
        await settle_install(host, operation["operation_id"])
        selected = host.registry.get_slot(plan["slot_id"])
        assert selected.desired_state == "stopped"
        assert selected.observed_state == "stopped"
        assert not any(call.kwargs.get("active", call.args[1] if len(call.args) > 1 else None)
                       for call in host.start_gateway.await_args_list)
    finally:
        await host.close()


async def test_upgrade_journal_read_failure_closes_already_exposed_admission(tmp_path, monkeypatch):
    """Unreadable recovery metadata cannot leave a failed mutation serving calls."""
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    admitting = False
    exposed_once = False

    async def gateway(slot, active, **kwargs):
        nonlocal admitting, exposed_once
        admitting = active
        exposed_once |= active and slot.package_digest == fixture.new_digest

    async def stop(slot_id, **kwargs):
        nonlocal admitting
        admitting = False

    original = host.upgrades.get
    def read(operation_id):
        if exposed_once:
            raise UpgradeError("upgrades_unavailable")
        return original(operation_id)

    host.start_gateway = gateway
    host.stop_gateway = stop
    monkeypatch.setattr(host.upgrades, "get", read)
    try:
        operation = await fixture.apply(await fixture.plan())
        assert exposed_once
        assert operation.status == "recovery_required"
        assert not admitting, "Journal failure left ordinary admission open"
        selected = host.registry.get_slot(fixture.slot.id)
        assert selected.package_digest == fixture.new_digest
        assert selected.observed_state == "failed"
    finally:
        await host.close()
