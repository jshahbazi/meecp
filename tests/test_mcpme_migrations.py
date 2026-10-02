import asyncio
import copy
import fcntl
import hashlib
import json
import os
import plistlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest

from mcpme import migrations
from mcpme.contracts import ModelArtifact, Tool
from mcpme.private import ensure_private_directory, read_private_json, write_private_json
from mcpme.supervisor import HostError, Supervisor


def record(host, tmp_path, *, stage="staged", status="planned"):
    legacy = ensure_private_directory(tmp_path / "legacy")
    descriptor = os.open(legacy / "backend.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    write_private_json(legacy / "connection.json", {"endpoint": "http://127.0.0.1:19385", "token": "x" * 48})
    value = {"transfer_id": str(uuid.uuid4()), "slot_id": str(uuid.uuid4()), "stage": stage,
             "status": status, "legacy_state": str(legacy), "legacy_app": str(tmp_path / "legacy.app"),
             "package_path": str(tmp_path / "package"), "package_digest": "a" * 64,
             "login_enabled": False, "legacy_running": True, "public_port": 19385, "start_policy": "host",
             "credential_digest": hashlib.sha256(b"x" * 48).hexdigest(), "error_code": None}
    migrations.save(host, value)
    if stage == "committed":
        host.registry.add_slot(slot_id=value["slot_id"], package_digest=value["package_digest"],
                               package_path=value["package_path"], name="JevK5 Judge", public_port=19385)
    return value


@pytest.mark.asyncio
async def test_transfer_apply_is_idempotent_before_yield(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path)
    calls = []
    release = asyncio.Event()
    async def execute(host, value):
        calls.append(value["transfer_id"])
        await release.wait()
    monkeypatch.setattr(migrations, "execute_transfer", execute)
    try:
        results = await asyncio.gather(*(migrations.apply_transfer(host, value["transfer_id"]) for _ in range(5)))
        await asyncio.sleep(0)
        assert calls == [value["transfer_id"]]
        assert {result["transfer_id"] for result in results} == {value["transfer_id"]}
        assert all("credential_digest" not in result for result in results)
    finally:
        release.set()
        await host.close()


@pytest.mark.asyncio
async def test_interrupted_transfer_cannot_reacquire_or_expose_without_recovery(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="endpoint_acquired", status="running")
    try:
        await migrations.initialize_transfers(host)
        assert value["slot_id"] in host.migration_blocked
        assert value["slot_id"] not in host.legacy_locks
        recovered = migrations.inspect_transfer(host, value["transfer_id"])
        assert recovered["status"] == "recovery_required"
        assert not host.listeners
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_committed_transfer_retains_exact_legacy_lock_before_any_listener(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    try:
        await migrations.initialize_transfers(host)
        assert value["slot_id"] in host.legacy_locks
        with pytest.raises(HostError, match="legacy_owner_conflict"):
            migrations.acquire_legacy_lock(value)
        assert not host.listeners
    finally:
        await host.close()
    descriptor = migrations.acquire_legacy_lock(value)
    os.close(descriptor)


@pytest.mark.asyncio
async def test_existing_legacy_owner_blocks_committed_transfer(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    descriptor = migrations.acquire_legacy_lock(value)
    try:
        await migrations.initialize_transfers(host)
        assert value["slot_id"] in host.migration_blocked
        assert not host.legacy_locks and not host.listeners
        assert migrations.inspect_transfer(host, value["transfer_id"])["error_code"] == "legacy_owner_conflict"
    finally:
        os.close(descriptor)
        await host.close()


@pytest.mark.asyncio
async def test_changed_legacy_credential_fails_before_stopping_old_app(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, status="running")
    package = SimpleNamespace(digest="a" * 64)
    monkeypatch.setattr(migrations, "validate_package", lambda _: package)
    native = AsyncMock()
    monkeypatch.setattr(migrations, "native_handoff", native)
    write_private_json(tmp_path / "legacy/connection.json", {"endpoint": "http://127.0.0.1:19385", "token": "y" * 48})
    try:
        await migrations.execute_transfer(host, value)
        native.assert_not_awaited()
        assert migrations.inspect_transfer(host, value["transfer_id"])["error_code"] == "legacy_connection_changed"
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_failed_endpoint_acquisition_releases_partial_legacy_lock(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, status="running")
    monkeypatch.setattr(migrations, "validate_package", lambda _: SimpleNamespace(digest="a" * 64))
    monkeypatch.setattr(migrations, "native_handoff", AsyncMock(return_value={"stopped": True, "login_disabled": True}))
    host.plan_install_async = AsyncMock(side_effect=HostError("port_conflict"))
    try:
        await migrations.execute_transfer(host, value)
        assert migrations.inspect_transfer(host, value["transfer_id"])["status"] == "recovery_required"
        assert not host.legacy_locks
        descriptor = migrations.acquire_legacy_lock(value)
        os.close(descriptor)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_post_exposure_failure_cannot_restore_old_data_or_binary(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="endpoint_acquired", status="recovery_required")
    write_private_json(host.slot_directory(value["slot_id"]) / "activation.json", {"exposure_permitted": True})
    native = AsyncMock()
    monkeypatch.setattr(migrations, "native_handoff", native)
    try:
        with pytest.raises(HostError, match="exposed_migration_requires_restore_plan"):
            await migrations.rollback_transfer(host, value["transfer_id"])
        native.assert_not_awaited()
        assert read_private_json(host.slot_directory(value["slot_id"]) / "activation.json")["exposure_permitted"]
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_unknown_owned_instance_blocks_legacy_relaunch(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="endpoint_acquired", status="recovery_required")
    host.ledger(value["slot_id"])["missing-instance"] = {"state": "ownership_lost"}
    native = AsyncMock()
    monkeypatch.setattr(migrations, "native_handoff", native)
    try:
        with pytest.raises(HostError, match="ownership_reconciliation_required"):
            await migrations.rollback_transfer(host, value["transfer_id"])
        native.assert_not_awaited()
    finally:
        await host.close()


def native_fixture(tmp_path, monkeypatch, login_status):
    root = tmp_path / "host-build"
    root.mkdir()
    (root / "bin").mkdir()
    (root / "bin/mcpme-handoff").write_bytes(b"helper")
    app = tmp_path / "legacy.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    executable = app / "Contents/MacOS/JevK5"
    executable.write_bytes(b"trusted-legacy")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "com.jshahbazi.jevk5", "CFBundleExecutable": "JevK5"}))
    (root / "legacy-handoff.json").write_text(json.dumps({
        "bundle_identifier": "com.jshahbazi.jevk5",
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}))
    monkeypatch.setattr(migrations, "__file__", str(root / "backend/mcpme/migrations.py"))
    native = AsyncMock(side_effect=[{"login_status": login_status}, {"running": []}, {"running": []}])
    monkeypatch.setattr(migrations, "_native", native)
    return app, native


@pytest.mark.asyncio
@pytest.mark.parametrize("login_status,confirmed,accepted", [
    ("not_found", False, False), ("not_found", True, True),
    ("requires_approval", True, False), ("unknown", True, False),
    ("not_registered", False, True), ("enabled", False, True)])
async def test_login_unknown_requires_exact_owner_confirmation(tmp_path, monkeypatch, login_status, confirmed, accepted):
    app, native = native_fixture(tmp_path, monkeypatch, login_status)
    if not accepted:
        with pytest.raises(HostError, match="legacy_login_state_unknown"):
            await migrations.native_handoff("status", app, login_disabled_confirmed=confirmed)
        assert native.await_count == 1
    else:
        result = await migrations.native_handoff("status", app, login_disabled_confirmed=confirmed)
        assert result["login_enabled"] == (login_status == "enabled")
        assert result["login_source"] == ("owner_confirmed_disabled" if login_status == "not_found" else "service_management")


async def test_confirmed_absent_login_skips_unregister(tmp_path, monkeypatch):
    app, native = native_fixture(tmp_path, monkeypatch, "not_found")
    result = await migrations.native_handoff("stop", app, login_disabled_confirmed=True)
    assert result == {"stopped": True, "login_disabled": True}
    assert not any("login-disable" in call.args[0] for call in native.await_args_list)


@pytest.mark.parametrize("stage,status", [("staged", "running"), ("old_stopped", "recovery_required"),
    ("endpoint_acquired", "running"), ("committed", "running"), ("committed", "recovery_required")])
async def test_every_interrupted_stage_requires_explicit_recovery(tmp_path, stage, status):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage=stage, status=status)
    try:
        await migrations.initialize_transfers(host)
        assert value["slot_id"] in host.migration_blocked
        assert not host.legacy_locks and not host.listeners
        assert migrations.inspect_transfer(host, value["transfer_id"])["status"] == "recovery_required"
    finally:
        await host.close()


async def test_partial_lock_registration_failure_closes_acquisition(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="completed")
    def rejected(*_):
        raise HostError("registration_failed")
    monkeypatch.setattr(host, "retain_legacy_lock", rejected)
    try:
        await migrations.initialize_transfers(host)
        assert value["slot_id"] in host.migration_blocked
        descriptor = migrations.acquire_legacy_lock(value)
        os.close(descriptor)
    finally:
        await host.close()


def fake_http(monkeypatch, responder):
    transport = httpx.MockTransport(responder)
    # These lifecycle fixtures model a private transport; live ownership/socket
    # rejection and TCP impersonation checks are in test_local_transport.py.
    monkeypatch.setattr(migrations, "socket_path", lambda *_: None)
    monkeypatch.setattr(migrations, "private_transport", lambda _: transport)


@pytest.mark.parametrize("running", [False, True])
async def test_migration_planning_never_probes_unverified_legacy_tcp(tmp_path, monkeypatch, running):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    app = tmp_path / "legacy.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.jshahbazi.jevk5"}))
    legacy = ensure_private_directory(tmp_path / "legacy")
    write_private_json(legacy / "connection.json", {"endpoint": "http://127.0.0.1:19385", "token": "x" * 48})
    package = SimpleNamespace(digest="a" * 64, manifest=SimpleNamespace(package_id="local.jevk5", profile="model-service",
                                                                     model_dump=lambda: {"package_id": "local.jevk5"}))
    monkeypatch.setattr(migrations, "validate_package", lambda _: package)
    monkeypatch.setattr(migrations, "check_legacy_contract", lambda *_: None)
    monkeypatch.setattr(migrations, "native_handoff", AsyncMock(return_value={"running": running, "login_enabled": False}))
    monkeypatch.setattr(migrations, "snapshot_package", lambda _package, destination: SimpleNamespace(root=destination, digest="a" * 64))
    contacted = []
    def unverified_client(**kwargs):
        contacted.append(kwargs)
        raise AssertionError("unverified TCP client constructed")
    monkeypatch.setattr(migrations.httpx, "AsyncClient", unverified_client)
    try:
        payload = {"package": str(tmp_path / "package"), "legacy_app": str(app), "legacy_state": str(legacy)}
        if running:
            with pytest.raises(HostError, match="legacy_transport_upgrade_required"):
                await migrations.plan_transfer(host, payload)
        else:
            assert (await migrations.plan_transfer(host, payload))["legacy_paused"] is False
        assert contacted == []
    finally:
        await host.close()


async def test_migration_restore_and_smoke_do_not_fall_back_to_tcp(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="old_stopped", status="recovery_required")
    contacted = []
    def unverified_client(**kwargs):
        contacted.append(kwargs)
        raise AssertionError("unverified TCP client constructed")
    monkeypatch.setattr(migrations.httpx, "AsyncClient", unverified_client)
    try:
        with pytest.raises(HostError, match="legacy_restore_timeout"):
            await migrations.wait_legacy_restored(value, timeout=0.03)
        with pytest.raises(OSError):
            await migrations.verify_and_commit(host, value, "a" * 64)
        assert contacted == []
    finally:
        await host.close()


@pytest.mark.parametrize("paused", [False, True])
async def test_explicit_forward_recovery_preserves_exposed_data_and_saved_pause(tmp_path, monkeypatch, paused):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="recovery_required")
    value["legacy_paused"] = paused
    migrations.save(host, value)
    data = ensure_private_directory(host.slot_directory(value["slot_id"]) / "data") / "state.bin"
    data.write_bytes(b"post-exposure-write")
    write_private_json(host.slot_directory(value["slot_id"]) / "activation.json", {"exposure_permitted": True})
    host.migration_blocked.add(value["slot_id"])
    host.control = AsyncMock()
    monkeypatch.setattr(host, "package", lambda _: SimpleNamespace(manifest=SimpleNamespace(model=SimpleNamespace(sha256="a" * 64))))
    native = AsyncMock()
    monkeypatch.setattr(migrations, "native_handoff", native)
    def respond(request):
        assert request.url.path == "/api/decide"
        assert request.headers["Authorization"] == "Bearer " + "x" * 48
        return httpx.Response(200, json={"model": {"sha256": "a" * 64}})
    fake_http(monkeypatch, respond)
    try:
        result = await migrations.recover_transfer(host, value["transfer_id"])
        assert result["status"] == "running"
        await asyncio.gather(*list(host.tasks))
        result = migrations.inspect_transfer(host, value["transfer_id"])
        assert result["stage"] == "committed" and result["status"] == "completed"
        assert data.read_bytes() == b"post-exposure-write"
        assert value["slot_id"] in host.legacy_locks and value["slot_id"] not in host.migration_blocked
        assert [call.args[1] for call in host.control.await_args_list] == (["start", "stop"] if paused else ["start"])
        assert all(call.kwargs == {"migration_internal": True} for call in host.control.await_args_list)
        native.assert_not_awaited()
        assert "credential_digest" not in result and "x" * 48 not in json.dumps(result)
    finally:
        await host.close()


async def test_recovery_submission_cannot_race_apply_or_rollback(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="recovery_required")
    write_private_json(host.slot_directory(value["slot_id"]) / "activation.json", {"exposure_permitted": True})
    release = asyncio.Event()
    host.control = AsyncMock(side_effect=lambda *_args, **_kwargs: release.wait())
    async def control(*args, **kwargs):
        await release.wait()
    host.control = AsyncMock(side_effect=control)
    monkeypatch.setattr(host, "package", lambda _: SimpleNamespace(manifest=SimpleNamespace(model=SimpleNamespace(sha256="a" * 64))))
    monkeypatch.setattr(migrations, "verify_and_commit", AsyncMock())
    try:
        results = await asyncio.gather(*(migrations.recover_transfer(host, value["transfer_id"]) for _ in range(4)))
        assert all(result["status"] == "running" for result in results)
        assert (await migrations.apply_transfer(host, value["transfer_id"]))["status"] == "running"
        with pytest.raises(HostError, match="committed_migration_requires_restore_plan"):
            await migrations.rollback_transfer(host, value["transfer_id"])
        release.set()
        await asyncio.gather(*list(host.tasks))
        assert host.control.await_count == 1
    finally:
        release.set()
        await host.close()


@pytest.mark.parametrize("paused", [False, True])
async def test_rollback_authenticates_readiness_and_restores_saved_pause(tmp_path, monkeypatch, paused):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="old_stopped", status="recovery_required")
    value.update(legacy_paused=paused, login_source="owner_confirmed_disabled")
    migrations.save(host, value)
    original = tmp_path / "legacy/user-state.bin"
    original.write_bytes(b"legacy-user-state")
    snapshot = ensure_private_directory(host.slot_directory(value["slot_id"]) / "data") / "state.bin"
    snapshot.write_bytes(b"candidate-data")
    native = AsyncMock(return_value={"restored": True})
    monkeypatch.setattr(migrations, "native_handoff", native)
    monkeypatch.setattr("mcpme.supervisor.bind_listener", lambda _: SimpleNamespace(close=lambda: None))
    observed = {"paused": False, "gets": 0, "controls": 0}
    def respond(request):
        assert request.headers["Authorization"] == "Bearer " + "x" * 48
        if request.method == "POST":
            assert json.loads(request.content) == {"action": "pause"}
            observed["paused"] = True
            observed["controls"] += 1
        else:
            observed["gets"] += 1
        return httpx.Response(200, json={"paused": observed["paused"], "ready": not observed["paused"]})
    fake_http(monkeypatch, respond)
    try:
        result = await migrations.rollback_transfer(host, value["transfer_id"])
        assert result["stage"] == "rolled_back" and result["status"] == "completed"
        assert observed["controls"] == int(paused)
        assert observed["gets"] >= (2 if paused else 1)
        assert original.read_bytes() == b"legacy-user-state" and snapshot.read_bytes() == b"candidate-data"
        assert native.await_args.kwargs == {"login_enabled": False, "login_disabled_confirmed": True}
    finally:
        await host.close()


@pytest.mark.parametrize("opened", [False, True])
async def test_failed_legacy_restore_never_marks_rollback_completed(tmp_path, monkeypatch, opened):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="old_stopped", status="recovery_required")
    monkeypatch.setattr(migrations, "native_handoff", AsyncMock(return_value={"restored": opened}))
    monkeypatch.setattr("mcpme.supervisor.bind_listener", lambda _: SimpleNamespace(close=lambda: None))
    original_wait = migrations.wait_legacy_restored
    async def short_wait(record):
        await original_wait(record, timeout=0.03)
    wait = AsyncMock(side_effect=short_wait)
    monkeypatch.setattr(migrations, "wait_legacy_restored", wait)
    fake_http(monkeypatch, lambda _: httpx.Response(401))
    try:
        with pytest.raises(HostError, match="legacy_restore_timeout" if opened else "legacy_restore_failed"):
            await migrations.rollback_transfer(host, value["transfer_id"])
        assert wait.await_count == int(opened)
        result = migrations.inspect_transfer(host, value["transfer_id"])
        assert result["status"] == "recovery_required" and result["stage"] == "old_stopped"
        assert value["slot_id"] in host.migration_blocked
    finally:
        await host.close()


async def test_frozen_legacy_contract_matches_original_tool_registration():
    from jevk5_app.server import make_app
    from pathlib import Path
    contract = json.loads(Path(migrations.__file__).with_name("legacy_jevk5_contract.json").read_bytes())
    _, app = make_app(None, "schema-only")
    tools = await app.state.mcp.list_tools()
    assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools] == contract["tools"]
    model = json.loads(Path(__file__).resolve().parents[1].joinpath("model-manifest.json").read_bytes())
    assert {key: model[key] for key in contract["model"]} == contract["model"]
    assert contract["parameters"] == {"temperature": 1.22, "knockout_temperature": 0.93}


@pytest.mark.parametrize("changed", [None, "model_id", "sha256", "profile", "context_tokens", "parameters", "tools", "old_model"])
def test_migration_requires_exact_original_contract(tmp_path, changed):
    from pathlib import Path
    contract = json.loads(Path(migrations.__file__).with_name("legacy_jevk5_contract.json").read_bytes())
    app = tmp_path / "legacy.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    old = dict(contract["model"])
    model = {**contract["model"], "artifact": "model.gguf", "engine_identity": {},
             "parameters": dict(contract["parameters"])}
    tool_values = copy.deepcopy(contract["tools"])
    if changed in {"model_id", "profile"}:
        model[changed] = "changed-model"
    elif changed == "sha256":
        model[changed] = "b" * 64
    elif changed == "context_tokens":
        model[changed] = 4096
    elif changed == "parameters":
        model[changed]["temperature"] = 1.0
    elif changed == "tools":
        tool_values[0]["inputSchema"] = {"type": "object"}
    elif changed == "old_model":
        old["sha256"] = "c" * 64
    (resources / "model-manifest.json").write_text(json.dumps(old))
    tools = [Tool(name=value["name"], description=value["description"], input_schema=value["inputSchema"],
                  output_schema=value.get("outputSchema"), annotations=value.get("annotations")) for value in tool_values]
    package = SimpleNamespace(manifest=SimpleNamespace(package_id="local.jevk5", profile="model-service",
        model=ModelArtifact(**model), tools=tools))
    if changed is None:
        migrations.check_legacy_contract(package, app)
    else:
        with pytest.raises(HostError, match="legacy_model_identity_mismatch" if changed == "old_model" else "legacy_package_contract_mismatch"):
            migrations.check_legacy_contract(package, app)


async def test_active_rollback_cannot_race_apply_or_second_rollback(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="old_stopped", status="recovery_required")
    release = asyncio.Event()
    started = asyncio.Event()
    async def stop(*_):
        started.set()
        await release.wait()
    host.stop_gateway = AsyncMock(side_effect=stop)
    monkeypatch.setattr(migrations, "native_handoff", AsyncMock(return_value={"restored": True}))
    monkeypatch.setattr(migrations, "wait_legacy_restored", AsyncMock())
    monkeypatch.setattr("mcpme.supervisor.bind_listener", lambda _: SimpleNamespace(close=lambda: None))
    task = asyncio.create_task(migrations.rollback_transfer(host, value["transfer_id"]))
    try:
        await started.wait()
        assert (await migrations.apply_transfer(host, value["transfer_id"]))["status"] == "running"
        assert (await migrations.recover_transfer(host, value["transfer_id"]))["status"] == "running"
        with pytest.raises(HostError, match="operation_in_progress"):
            await migrations.rollback_transfer(host, value["transfer_id"])
        release.set()
        assert (await task)["stage"] == "rolled_back"
        assert host.stop_gateway.await_count == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


@pytest.mark.parametrize("reason", ["ownership_lost", "unfinished_install"])
async def test_forward_recovery_cannot_bypass_unknown_ownership_or_install(tmp_path, reason):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, stage="committed", status="recovery_required")
    write_private_json(host.slot_directory(value["slot_id"]) / "activation.json", {"exposure_permitted": True})
    if reason == "ownership_lost":
        host.ledger(value["slot_id"])["unknown"] = {"state": "ownership_lost"}
    else:
        host.registry.create_operation("install", value["slot_id"])
    try:
        with pytest.raises(HostError, match="ownership_reconciliation_required" if reason == "ownership_lost" else "installation_recovery_required"):
            await migrations.recover_transfer(host, value["transfer_id"])
        assert not host.tasks and not host.listeners
        assert migrations.inspect_transfer(host, value["transfer_id"])["status"] == "recovery_required"
    finally:
        await host.close()


@pytest.mark.parametrize("smoke_success", [False, True])
async def test_transfer_after_endpoint_exposure_retains_lock_and_candidate_data(tmp_path, monkeypatch, smoke_success):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    value = record(host, tmp_path, status="running")
    value["login_source"] = "owner_confirmed_disabled"
    migrations.save(host, value)
    model = SimpleNamespace(sha256="a" * 64)
    package = SimpleNamespace(digest="a" * 64, manifest=SimpleNamespace(model=model))
    monkeypatch.setattr(migrations, "validate_package", lambda _: package)
    native = AsyncMock(return_value={"stopped": True, "login_disabled": True})
    monkeypatch.setattr(migrations, "native_handoff", native)
    plan_id = str(uuid.uuid4())
    host.plan_install_async = AsyncMock(return_value={"plan_id": plan_id})
    data = ensure_private_directory(host.slot_directory(value["slot_id"]) / "data") / "state.bin"
    data.write_bytes(b"candidate-state")
    async def expose(plan):
        assert plan == plan_id
        slot = host.registry.add_slot(slot_id=value["slot_id"], package_digest=value["package_digest"],
            package_path=value["package_path"], name="JevK5 Judge", public_port=19385)
        operation = host.registry.create_operation("install", slot.id)
        operation = host.registry.update_operation(operation.id, status="completed")
        write_private_json(host.slot_directory(slot.id) / "activation.json", {"exposure_permitted": True})
        return {"operation_id": operation.id}
    host.apply_install = AsyncMock(side_effect=expose)
    fake_http(monkeypatch, lambda _: httpx.Response(200 if smoke_success else 503,
                                                  json={"model": {"sha256": "a" * 64}}))
    try:
        await migrations.execute_transfer(host, value)
        result = migrations.inspect_transfer(host, value["transfer_id"])
        assert result["stage"] == ("committed" if smoke_success else "endpoint_acquired")
        assert result["status"] == ("completed" if smoke_success else "recovery_required")
        assert data.read_bytes() == b"candidate-state"
        assert value["slot_id"] in host.legacy_locks
        assert (value["slot_id"] in host.migration_blocked) == (not smoke_success)
        assert native.await_count == 1
        assert native.await_args.kwargs == {"login_disabled_confirmed": True}
        if not smoke_success:
            with pytest.raises(HostError, match="exposed_migration_requires_restore_plan"):
                await migrations.rollback_transfer(host, value["transfer_id"])
            assert host.registry.get_slot(value["slot_id"]).error_code == "migration_recovery_required"
    finally:
        await host.close()


@pytest.mark.parametrize("declaration", [False, 0, 1, "true", None])
async def test_plan_rejects_ambiguous_login_disabled_declarations(tmp_path, declaration):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    try:
        with pytest.raises(HostError, match="invalid_migration_plan"):
            await migrations.plan_transfer(host, {"package": str(tmp_path / "package"),
                "legacy_app": str(tmp_path / "legacy.app"), "legacy_state": str(tmp_path / "legacy"),
                "legacy_login_disabled": declaration})
    finally:
        await host.close()


async def test_confirmed_disabled_plan_is_durable_and_duplicate_planners_share_identity(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    app = tmp_path / "legacy.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.jshahbazi.jevk5"}))
    legacy = ensure_private_directory(tmp_path / "legacy")
    write_private_json(legacy / "connection.json", {"endpoint": "http://127.0.0.1:19385", "token": "x" * 48})
    package = SimpleNamespace(digest="a" * 64, manifest=SimpleNamespace(package_id="local.jevk5", profile="model-service",
                                                                     model_dump=lambda: {"package_id": "local.jevk5"}))
    monkeypatch.setattr(migrations, "validate_package", lambda _: package)
    monkeypatch.setattr(migrations, "check_legacy_contract", lambda *_: None)
    native = AsyncMock(return_value={"running": False, "login_enabled": False, "login_source": "owner_confirmed_disabled"})
    monkeypatch.setattr(migrations, "native_handoff", native)
    snapshots = []
    def snapshot(_package, destination):
        snapshots.append(destination)
        return SimpleNamespace(root=destination, digest="a" * 64)
    monkeypatch.setattr(migrations, "snapshot_package", snapshot)
    fake_http(monkeypatch, lambda _: httpx.Response(503, json={}))
    payload = {"package": str(tmp_path / "package"), "legacy_app": str(app), "legacy_state": str(legacy),
               "legacy_login_disabled": True}
    try:
        results = await asyncio.gather(*(migrations.plan_transfer(host, payload) for _ in range(4)))
        assert len({result["transfer_id"] for result in results}) == 1
        assert len(snapshots) == 1 and native.await_count == 1
        assert native.await_args.kwargs == {"login_disabled_confirmed": True}
        saved = read_private_json(migrations.record_path(host, results[0]["transfer_id"]))
        assert saved["login_source"] == "owner_confirmed_disabled" and saved["login_enabled"] is False
        assert all("credential_digest" not in result and "x" * 48 not in json.dumps(result) for result in results)
    finally:
        await host.close()
