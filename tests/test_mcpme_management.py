"""App settings and removal checks at lifecycle and durable recovery boundaries."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcpme_local_transport import socket_path

from mcpme.private import read_private_json, write_private_json
from mcpme.registry import Registry, RegistryError
from mcpme.supervisor import HostError, Supervisor, bind_listener, management_app
from test_mcpme_host import finish, install, prepared, tool_call


def registered(host, *, policy="manual"):
    """Register metadata without launching package code for failure-injection checks."""
    listener = bind_listener()
    try:
        return host.registry.add_slot(package_digest="a" * 64, package_path="/prepared/package",
                                      name="Managed", public_port=listener.getsockname()[1],
                                      start_policy=policy)
    finally:
        listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_remove_revokes_endpoint_and_credential_retains_data(tmp_path, prepared, running):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        slot = await install(host, package, "Removed", start=running)
        other = await install(host, package, "Retained", start=True)
        directory = host.slot_directory(slot.id)
        data = directory / "data"
        data.mkdir(mode=0o700, exist_ok=True)
        saved = data / "saved-state.json"
        saved.write_text('{"saved":true}')
        connection = host.connection(slot.id)
        private_socket = socket_path(host.root, slot.id)
        assert private_socket.exists()
        owner = host.owner(slot.id)
        gateway = host.workers[slot.id].process
        result = await finish(host, host.begin_control(slot.id, "remove"))
        assert result.status == "completed", result.error_code
        with pytest.raises(RegistryError, match="^slot_not_found$"):
            host.registry.get_slot(slot.id)
        assert gateway.returncode is not None and not owner.instances
        assert not read_private_json(directory / "ownership.json")
        assert read_private_json(directory / "activation.json")["exposure_permitted"] is False
        assert slot.id not in host.listeners and slot.id not in host.workers and slot.id not in host.owners
        assert not (directory / "connection.json").exists()
        assert not private_socket.exists()
        assert saved.read_text() == '{"saved":true}'
        assert Path(slot.package_path).is_dir()
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(connection["endpoint"], headers={"Authorization": "Bearer " + connection["token"]})
        released = bind_listener(slot.public_port)
        released.close()
        assert await tool_call(host, other.id) == {"valid": True, "kind": "object"}
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_remove_reaps_open_sdk_session_and_its_process(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        slot = await install(host, package, "Open session", start=True)
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
            async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    await client.call_tool("validate_json", {"document": "null"})
                    owned = list(host.owner(slot.id).instances.values())
                    assert len(owned) == 2  # Gateway and the open session's executable.
                    operation = await finish(host, host.begin_control(slot.id, "remove"))
                    assert operation.status == "completed", operation.error_code
                    assert all(process.returncode is not None for process in owned)
                    assert not read_private_json(host.slot_directory(slot.id) / "ownership.json")
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,running,selected,reopened_state", [
    ("manual", False, "host", "ready"),
    ("host", True, "manual", "stopped"),
])
async def test_settings_persist_without_restarting_current_runtime(tmp_path, prepared, initial, running, selected, reopened_state):
    package, reaper = prepared
    state = tmp_path.resolve() / "host"
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package), "start_policy": initial, "start": running})
        installed = await finish(host, await host.apply_install(plan["plan_id"]))
        assert installed.status == "completed", installed.error_code
        slot = host.registry.get_slot(installed.slot_id)
        worker = host.workers[slot.id]
        credentials = host.connection(slot.id)
        operations = host.registry.list_operations()
        updated = await host.update_settings(slot.id, {"start_policy": selected})
        assert updated["slot"] == replace(slot, start_policy=selected).to_dict()
        assert host.workers[slot.id] is worker
        assert host.connection(slot.id) == credentials
        assert host.registry.list_operations() == operations
    finally:
        await host.close()
    reopened = Supervisor(state, reaper)
    try:
        await reopened.initialize()
        persisted = reopened.registry.get_slot(slot.id)
        assert persisted.start_policy == selected
        assert persisted.observed_state == reopened_state
        assert persisted.public_port == slot.public_port
        assert reopened.connection(slot.id) == credentials
        if reopened_state == "ready":
            assert await tool_call(reopened, slot.id) == {"valid": True, "kind": "object"}
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_settings_do_not_clear_explicit_stop_suppression(tmp_path, prepared):
    package, reaper = prepared
    state = tmp_path.resolve() / "host"
    host = Supervisor(state, reaper)
    try:
        slot = await install(host, package, "Stopped", start=True)
        await host.control(slot.id, "stop")
        stopped = host.registry.get_slot(slot.id)
        for policy in ("host", "manual", "host"):
            await host.update_settings(slot.id, {"start_policy": policy})
            assert host.registry.get_slot(slot.id) == replace(stopped, start_policy=policy)
    finally:
        await host.close()
    reopened = Supervisor(state, reaper)
    try:
        await reopened.initialize()
        same = reopened.registry.get_slot(slot.id)
        assert same.start_policy == "host" and same.wake_suppressed
        assert same.observed_state == same.desired_state == "stopped"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_management_writes_require_owner_auth_and_strict_payloads(tmp_path):
    host = Supervisor(tmp_path / "host", tmp_path / "unused-reaper")
    slot = registered(host)
    app = management_app(host, "manager-credential", 19499)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19499") as client:
            settings, removal = f"/v1/slots/{slot.id}", f"/v1/slots/{slot.id}/remove"
            for method, path, body in [("PATCH", settings, {"start_policy": "host"}), ("POST", removal, {})]:
                assert (await client.request(method, path, json=body)).status_code == 401
                assert (await client.request(method, path, headers={"Authorization": "Bearer invocation-credential"}, json=body)).status_code == 401
                headers = {"Authorization": "Bearer manager-credential", "Origin": "https://example.com"}
                assert (await client.request(method, path, headers=headers, json=body)).status_code == 403
                headers = {"Authorization": "Bearer manager-credential", "Host": "example.com"}
                assert (await client.request(method, path, headers=headers, json=body)).status_code == 403
            client.headers["Authorization"] = "Bearer manager-credential"
            for payload in [{}, {"start_policy": None}, {"start_policy": True}, {"start_policy": 1},
                            {"start_policy": []}, {"start_policy": {}}, {"start_policy": "first_call"},
                            {"start_policy": "host", "unexpected": "never-echo"}, [], None]:
                response = await client.patch(settings, json=payload)
                assert response.status_code == 400
                assert "never-echo" not in response.text
            assert (await client.post(removal, json={"unexpected": "never-echo"})).status_code == 400
            response = await client.patch(settings, json={"start_policy": "host"})
            assert response.status_code == 200 and response.json()["slot"]["start_policy"] == "host"
            response = await client.post(removal, json={})
            assert response.status_code == 200
            assert (await finish(host, response.json())).status == "completed"
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["settings", "remove"])
@pytest.mark.parametrize("block", ["pending", "running", "ownership_lost", "ledger", "locked"])
async def test_management_rejects_active_operations_or_lost_ownership(tmp_path, action, block):
    host = Supervisor(tmp_path / "host", tmp_path / "unused-reaper")
    slot = registered(host)
    lock = host.locks.setdefault(slot.id, asyncio.Lock())
    try:
        if block in {"pending", "running"}:
            operation = host.registry.create_operation("start", slot_id=slot.id)
            if block == "running":
                host.registry.update_operation(operation.id, "running")
        elif block == "ownership_lost":
            host.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
        elif block == "ledger":
            host.ledger(slot.id)[uuid.uuid4().hex] = {"state": "ownership_lost"}
        else:
            await lock.acquire()
        before = host.registry.get_slot(slot.id)
        count = len(host.registry.list_operations())
        code = "ownership_lost" if block in {"ownership_lost", "ledger"} else "operation_in_progress"
        with pytest.raises(HostError, match=f"^{code}$"):
            if action == "settings":
                await host.update_settings(slot.id, {"start_policy": "host"})
            else:
                host.begin_control(slot.id, "remove")
        assert host.registry.get_slot(slot.id) == before
        assert len(host.registry.list_operations()) == count
    finally:
        if lock.locked():
            lock.release()
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", [False, True])
async def test_interrupted_removal_replays_only_when_ownership_is_resolved(tmp_path, ledger):
    state = tmp_path / "host"
    host = Supervisor(state, tmp_path / "unused-reaper")
    slot = registered(host, policy="host")
    operation = host.registry.create_operation("remove", slot_id=slot.id)
    host.registry.update_operation(operation.id, "running")
    directory = host.slot_directory(slot.id)
    write_private_json(directory / "connection.json", {"token": "old-credential"})
    if ledger:
        host.ledger(slot.id)[uuid.uuid4().hex] = {"state": "owned"}
        host.save_ledger(slot.id)
    await host.close()
    reopened = Supervisor(state, tmp_path / "unused-reaper")
    gateway = reopened.start_gateway = AsyncMock()
    try:
        await reopened.initialize()
        result = reopened.registry.get_operation(operation.id)
        gateway.assert_not_awaited()
        assert not reopened.listeners and not reopened.workers
        if ledger:
            assert result.status == "recovery_required" and result.error_code == "ownership_lost"
            assert reopened.registry.get_slot(slot.id).observed_state == "ownership_lost"
            assert (directory / "connection.json").exists()
        else:
            assert result.status == "completed"
            assert not reopened.registry.list_slots()
            assert not (directory / "connection.json").exists()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_finalization_never_republishes_and_retry_reuses_operation(tmp_path, monkeypatch):
    state = tmp_path / "host"
    host = Supervisor(state, tmp_path / "unused-reaper")
    slot = registered(host, policy="host")
    directory = host.slot_directory(slot.id)
    write_private_json(directory / "connection.json", {"token": "old-credential"})
    def unavailable(*args, **kwargs):
        raise RegistryError("registry_unavailable")
    monkeypatch.setattr(host.registry, "complete_removal", unavailable)
    try:
        result = await finish(host, host.begin_control(slot.id, "remove"))
        assert result.status == "recovery_required" and result.error_code == "registry_unavailable"
        assert host.registry.get_slot(slot.id).wake_suppressed
        assert not (directory / "connection.json").exists()
    finally:
        await host.close()
    reopened = Supervisor(state, tmp_path / "unused-reaper")
    original = reopened.registry.complete_removal
    monkeypatch.setattr(reopened.registry, "complete_removal", unavailable)
    gateway = reopened.start_gateway = AsyncMock()
    try:
        await reopened.initialize()
        gateway.assert_not_awaited()
        assert not reopened.listeners and not reopened.workers
        for action in ("start", "stop", "restart"):
            with pytest.raises(HostError, match="^removal_recovery_required$"):
                reopened.begin_control(slot.id, action)
        with pytest.raises(HostError, match="^removal_recovery_required$"):
            await reopened.update_settings(slot.id, {"start_policy": "manual"})
        monkeypatch.setattr(reopened.registry, "complete_removal", original)
        retry = reopened.begin_control(slot.id, "remove")
        assert retry["operation_id"] == result.id
        assert (await finish(reopened, retry)).status == "completed"
        assert len(reopened.registry.list_operations()) == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_post_commit_owner_cleanup_failure_preserves_completed_result(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "host", tmp_path / "unused-reaper")
    slot = registered(host)
    owner = host.owner(slot.id)
    original = owner.close
    async def fail():
        raise RuntimeError("synthetic cleanup failure")
    monkeypatch.setattr(owner, "close", fail)
    try:
        operation = await finish(host, host.begin_control(slot.id, "remove"))
        assert operation.status == "completed" and operation.error_code is None
        assert not host.registry.list_slots()
        with pytest.raises(RegistryError, match="^operation_finalized$"):
            host.registry.update_operation(operation.id, "recovery_required", error_code="removal_failed")
    finally:
        monkeypatch.setattr(owner, "close", original)
        await host.close()


def test_registry_removal_rolls_back_slot_deletion_if_operation_completion_fails(tmp_path):
    state = tmp_path / "host"
    with Registry(state) as registry:
        slot = registry.add_slot(package_digest="a" * 64, package_path="/prepared/package", name="Managed", public_port=19386)
        operation = registry.create_operation("remove", slot_id=slot.id)
        registry.update_operation(operation.id, "running")
        registry._connection.execute("CREATE TRIGGER block_completion BEFORE UPDATE OF status ON operations WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with pytest.raises(RegistryError, match="^registry_unavailable$"):
            registry.complete_removal(slot.id, operation.id, expected_generation=slot.generation)
        assert registry.get_slot(slot.id) == slot
        assert registry.get_operation(operation.id).status == "running"
        registry._connection.execute("DROP TRIGGER block_completion")
        registry.complete_removal(slot.id, operation.id, expected_generation=slot.generation)
    with Registry(state) as reopened:
        assert not reopened.list_slots()
        assert reopened.get_operation(operation.id).status == "completed"


@pytest.mark.parametrize("conflict,code", [
    ("generation", "stale_generation"),
    ("action", "operation_conflict"),
    ("pending", "operation_conflict"),
    ("running_slot", "slot_not_stopped"),
])
def test_registry_removal_requires_matching_inactive_generation(tmp_path, conflict, code):
    with Registry(tmp_path / "host") as registry:
        slot = registry.add_slot(package_digest="a" * 64, package_path="/prepared/package", name="Managed", public_port=19386)
        operation = registry.create_operation("stop" if conflict == "action" else "remove", slot_id=slot.id)
        if conflict != "pending":
            registry.update_operation(operation.id, "running")
        if conflict == "running_slot":
            registry.update_slot(slot.id, desired_state="running", observed_state="ready")
        before = registry.get_slot(slot.id)
        status = registry.get_operation(operation.id).status
        with pytest.raises(RegistryError, match=f"^{code}$"):
            registry.complete_removal(slot.id, operation.id, expected_generation=2 if conflict == "generation" else slot.generation)
        assert registry.get_slot(slot.id) == before
        assert registry.get_operation(operation.id).status == status
