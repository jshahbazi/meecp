"""Real leased consent, exact workflow review, and persistent policy boundaries."""
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import struct
import sys
import time
import uuid

import pytest

from meecp import authorization
from meecp.grants import GrantError, read_credential
from meecp.preparation_contracts import PreparationError
from meecp.preparation_inspection import inspect_source
from meecp.private import read_private_json, write_private_json
from meecp.registry import Registry, RegistryError
from meecp.supervisor import Supervisor
from meecp_local_transport import socket_path
from test_meecp_preparations import descriptor, policy, recipe_bundle
from test_preparation_resolution import request as resolution_request
from test_meecp_host import prepared
from test_native_authorization import access, api, channel, decision, frame, host, pi_routes


def workflow_plan(manager, folder, kind):
    import hashlib
    folder.mkdir()
    (folder / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n return {'value':n*2}\n")
    (folder / "validator.py").write_text("def validate_json(document): return {'valid':True}\n")
    (folder / "LICENSE").write_text("Synthetic permission notice.\n")
    (folder / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    lock = b"version=1\nrevision=3\n"
    (folder / "uv.lock").write_bytes(lock)
    draft = manager.imports.create(folder, name="Synthetic authorization")
    if kind == "resolution":
        candidate = inspect_source(draft)["candidates"][0]
        return manager.resolutions.plan(resolution_request(draft, candidate), candidate, expires_in=300), draft
    recipe = manager.recipes.add(recipe_bundle(folder.parent / "recipe"))
    recipe = manager.recipes.enable(recipe["recipe_id"], recipe["version"])
    raw = descriptor(recipe, hashlib.sha256(lock).hexdigest())
    return manager.preparations.execution_plan(draft["draft_id"], draft["revision"], raw, policy(),
        runner_digest="a" * 64, verifier_digest="b" * 64, runtime_digest="c" * 64), draft


async def action(manager, reader, writer, kind, plan):
    proposal = {"type": "request_action", "kind": kind, "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"]}
    if kind != "install":
        proposal["idempotency_key"] = str(uuid.uuid4())
    result = await frame(reader, writer, proposal)
    assert result["status"] == "pending"
    return result, proposal


async def native_preparation(manager, folder):
    plan, _ = workflow_plan(manager, folder, "preparation")
    profile = manager.authorization.create_profile({})["profile"]["profile_id"]
    reader, writer, base = await access(manager, profile=profile, remember=True)
    pending, proposal = await action(manager, reader, writer, "preparation", plan)
    manager.authorization.approve(pending["request_id"], decision(pending))
    grant = (await frame(reader, writer, {"type": "status"}))["handoff"]
    await frame(reader, writer, {"type": "ack", "request_id": grant["request_id"], "grant_id": grant["grant_id"]})
    return reader, writer, base, grant, plan, proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive", ["base_revoked", "base_expired", "action_revoked", "action_expired", "profile_revoked", "disconnect"])
async def test_native_preparation_worker_admission_and_monitor_keep_live_authority(host, tmp_path, monkeypatch, inactive):
    try:
        _, writer, base, grant, plan, proposal = await native_preparation(host, tmp_path / "source")
        # These are the worker calls used by PreparationRunner._start and its
        # authority monitor, including the imports lock held during admission.
        job = await asyncio.to_thread(host.preparations.authorize_start, plan["plan_id"], plan["plan_digest"],
                                      proposal["idempotency_key"], grant_id=grant["grant_id"])
        assert job["management_authority"]["grant_id"] == grant["grant_id"]
        assert (await asyncio.to_thread(host.preparations.check_authority, job["job_id"]))["job_id"] == job["job_id"]
        if inactive == "base_revoked":
            host.grants.revoke(base["grant_id"])
        elif inactive == "base_expired":
            host.registry._connection.execute("UPDATE management_grants SET expires_at=? WHERE grant_id=?",
                                              (time.time() - 1, base["grant_id"]))
        elif inactive == "action_revoked":
            host.workflow_grants.revoke(grant["grant_id"])
        elif inactive == "action_expired":
            expired_at = host.workflow_grants._row(grant["grant_id"])["expires_at"] + 1
            monkeypatch.setattr(time, "time", lambda: expired_at)
        elif inactive == "profile_revoked":
            profile_id = host.authorization.credentials[base["grant_id"]]["profile_id"]
            host.authorization.owner_route("POST", "/v1/authorization/profiles/" + profile_id + "/revoke", {})
        else:
            host.authorization.disconnect(host.authorization.credentials[base["grant_id"]]["lease_id"])
        with pytest.raises(PreparationError, match="management_execution_(?:revoked|expired)"):
            await asyncio.to_thread(host.preparations.check_authority, job["job_id"])
        with pytest.raises(GrantError, match="grant_(?:revoked|expired)"):
            await asyncio.to_thread(host.workflow_grants.authenticate, grant["token"])
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["permissions", "missing", "busy", "replaced"])
async def test_native_worker_authority_denies_unavailable_private_registry(host, tmp_path, unavailable):
    saved_registry = None
    try:
        _, writer, base, grant, plan, proposal = await native_preparation(host, tmp_path / "source")
        job = await asyncio.to_thread(host.preparations.authorize_start, plan["plan_id"], plan["plan_digest"],
                                      proposal["idempotency_key"], grant_id=grant["grant_id"])
        if unavailable == "permissions":
            host.registry.path.chmod(0o644)
        elif unavailable == "missing":
            host.registry._connection.execute("DELETE FROM management_grants WHERE grant_id=?", (base["grant_id"],))
        elif unavailable == "replaced":
            saved_registry = host.registry.path.with_suffix(".original")
            host.registry.path.rename(saved_registry)
            shutil.copy2(saved_registry, host.registry.path)
        else:
            # A worker must settle independently even while the owner holds an
            # exclusive registry transaction; it cannot wait on the owner loop.
            host.registry._connection.execute("BEGIN EXCLUSIVE")
        with pytest.raises(PreparationError, match="management_execution_revoked"):
            await asyncio.wait_for(asyncio.to_thread(host.preparations.check_authority, job["job_id"]), 2)
        writer.close()
    finally:
        if saved_registry is not None:
            host.registry.path.unlink()
            saved_registry.rename(host.registry.path)
        if host.registry._connection.in_transaction:
            host.registry._connection.execute("ROLLBACK")
        host.registry.path.chmod(0o600)
        await host.close()


@pytest.mark.asyncio
async def test_native_worker_authority_read_encodes_private_registry_uri(tmp_path, prepared):
    _, reaper = prepared
    manager = Supervisor(tmp_path.resolve() / "host ? # %", reaper)
    try:
        _, writer, _, grant, plan, proposal = await native_preparation(manager, tmp_path / "source")
        job = await asyncio.to_thread(manager.preparations.authorize_start, plan["plan_id"], plan["plan_digest"],
                                      proposal["idempotency_key"], grant_id=grant["grant_id"])
        assert (await asyncio.to_thread(manager.preparations.check_authority, job["job_id"]))["job_id"] == job["job_id"]
        assert await asyncio.to_thread(manager.workflow_grants.authenticate, grant["token"]) == {"grant_id": grant["grant_id"], "workflow_grant": True}
        writer.close()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["preparation", "resolution"])
async def test_workflow_review_exact_attempt_no_file_and_pi_fence(host, tmp_path, kind):
    try:
        plan, draft = workflow_plan(host, tmp_path / "source", kind)
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        reader, writer, base = await access(host, profile=profile, remember=True)
        pending, proposal = await action(host, reader, writer, kind, plan)
        assert "effects" not in pending["review"]
        async with api(host) as owner:
            inspected = await owner.get("/v1/authorization/requests/" + pending["request_id"])
            assert inspected.status_code == 200
            details = inspected.json()["request"]["review"]
            effects = details["effects"]
            assert effects["import_id"] == draft["draft_id"] and effects["revision"] == 1
            assert effects["source_revision"] == draft["source"]["revision"]
            assert set(effects["execution_policy"]) >= {"operations", "network", "memory_mb", "disk_bytes", "wall_seconds"}
            if kind == "preparation":
                assert effects["recipe"]["digest"] == plan["descriptor"]["recipe"]["digest"]
                assert effects["resources"]["memory_mb"] == 128
            else:
                assert effects["candidate_id"] == plan["request"]["candidate_id"]
            assert "arguments" not in json.dumps(details) and str(tmp_path) not in json.dumps(details)
        host.authorization.approve(pending["request_id"], decision(pending))
        handoff = (await frame(reader, writer, {"type": "status"}))["handoff"]
        await frame(reader, writer, {"type": "ack", "request_id": handoff["request_id"], "grant_id": handoff["grant_id"]})
        store = host.preparations if kind == "preparation" else host.resolutions
        with pytest.raises(GrantError, match="grant_scope"):
            store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()), grant_id=handoff["grant_id"])
        job = store.authorize_start(plan["plan_id"], plan["plan_digest"], proposal["idempotency_key"], grant_id=handoff["grant_id"])
        assert job["management_authority"]["grant_id"] == handoff["grant_id"]
        assert not (host.workflow_grants.root / (handoff["grant_id"] + ".json")).exists()
        assert handoff["token"].encode() not in host.imports.path.read_bytes()
        before = host.workflow_grants.inspect(handoff["grant_id"])
        renewed = (await frame(reader, writer, {"type": "renew"}))["handoff"]
        await frame(reader, writer, {"type": "ack", "request_id": renewed["request_id"], "grant_id": renewed["grant_id"]})
        assert host.workflow_grants.inspect(handoff["grant_id"]) == before
        assert host.workflow_grants.authenticate(handoff["token"])["grant_id"] == handoff["grant_id"]
        async with api(host, handoff["token"]) as client:
            for method, route in pi_routes():
                result = await client.request(method, route, **({"json": {}} if method != "GET" else {}))
                assert result.status_code == 403 and result.json()["error"]["code"] == "grant_scope"
        writer.close();await writer.wait_closed();await asyncio.sleep(0)
        with pytest.raises(GrantError, match="grant_revoked"):
            host.workflow_grants.authenticate(handoff["token"])
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["preparation", "resolution"])
@pytest.mark.parametrize("changed", ["revision", "plan"])
async def test_workflow_changed_after_review_cannot_issue_action(host, tmp_path, kind, changed):
    try:
        plan, draft = workflow_plan(host, tmp_path / "source", kind)
        reader, writer, _ = await access(host)
        pending, _ = await action(host, reader, writer, kind, plan)
        if changed == "revision":
            host.imports.revise(draft["draft_id"], 1, name="Changed")
        else:
            table = "preparation_execution_plans" if kind == "preparation" else "resolution_plans"
            value = json.loads(host.imports._connection.execute("SELECT data FROM " + table + " WHERE plan_id=?", (plan["plan_id"],)).fetchone()[0])
            selected = value["execution_policy"] if kind == "preparation" else value["request"]["policy"]
            selected["memory_mb"] += 1
            host.imports._connection.execute("UPDATE " + table + " SET data=? WHERE plan_id=?", (json.dumps(value), plan["plan_id"]))
        async with api(host) as owner:
            result = await owner.get("/v1/authorization/requests/" + pending["request_id"])
            assert result.status_code == 409
            result = await owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve", json=decision(pending))
            assert result.status_code == 409
        assert host.workflow_grants.list()["total"] == 0
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_install_review_exposes_effects_only_to_owner_and_plan_change_fails(host, prepared):
    package, _ = prepared
    try:
        plan = host.plan_install({"package": str(package), "name": "Reviewed app", "start": True, "start_policy": "host"})
        reader, writer, _ = await access(host)
        pending, _ = await action(host, reader, writer, "install", plan)
        assert "install_effects" not in pending["review"]
        async with api(host) as owner:
            details = (await owner.get("/v1/authorization/requests/" + pending["request_id"])).json()["request"]["review"]
        effects = details["install_effects"]
        assert effects["name"] == "Reviewed app" and effects["start"] is True and effects["start_policy"] == "host"
        assert set(effects["resources"]) == {"memory_mb", "cpu_only", "gpu"}
        assert set(effects["declared_access"]) == {"filesystem", "network", "writes", "secrets"}
        assert "configuration_fields" in effects and "secret_reference_names" in effects
        path = host.root / "plans" / (plan["plan_id"] + ".json")
        saved = read_private_json(path)
        write_private_json(path, {**saved, "start": False})
        with pytest.raises(GrantError, match="grant_plan_changed"):
            host.authorization.approve(pending["request_id"], decision(pending))
        assert host.grants.list()["total"] == 1
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_all_legacy_grant_modes_keep_pi_owner_only(host, tmp_path, prepared):
    package, _ = prepared
    try:
        plan = host.plan_install({"package": str(package)})
        saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        slot = host.registry.add_slot(package_digest=saved["package_digest"], package_path=package, name="Fixture", public_port=19389)
        grants = [host.grants.issue({"mode": mode}) for mode in ("read", "prepare")]
        grants.append(host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=saved))
        grants.append(host.grants.issue({"mode": "lifecycle", "slot_ids": [slot.id], "actions": ["stop"]}))
        prep, _ = workflow_plan(host, tmp_path / "prep", "preparation")
        resolution, _ = workflow_plan(host, tmp_path / "resolve", "resolution")
        for kind, reviewed in (("preparation", prep), ("resolution", resolution)):
            grants.append(host.workflow_grants.issue({"mode": kind + "-execution", "plan_id": reviewed["plan_id"],
                "plan_digest": reviewed["plan_digest"], "idempotency_key": str(uuid.uuid4())}))
        recipe = prep["descriptor"]["recipe"]
        grants.append(host.workflow_grants.issue({"mode": "recipe-management", "recipe_id": recipe["id"],
            "version": recipe["version"], "digest": recipe["digest"], "actions": ["disable"]}))
        for grant in grants:
            token = read_credential(grant["credential_file"])["token"]
            async with api(host, token) as client:
                for method, route in pi_routes():
                    response = await client.request(method, route, **({"json": {}} if method != "GET" else {}))
                    assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
                assert (await client.post("/v1/authorization/profiles", json={})).status_code == 403
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_broader_base_scope_waits_for_owner_then_replaces_previous_base(host):
    try:
        reader, writer, first = await access(host, selected=["inspect"])
        pending = await frame(reader, writer, {"type": "request_access", "profile_id": None,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect", "plan"]})
        assert pending["status"] == "pending" and pending["base_status"] == "approved" and "handoff" not in pending
        assert host.grants.authenticate(first["token"])
        host.authorization.approve(pending["request_id"], decision(pending))
        second = (await frame(reader, writer, {"type": "status"}))["handoff"]
        await frame(reader, writer, {"type": "ack", "request_id": second["request_id"], "grant_id": second["grant_id"]})
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(first["token"])
        host.grants.authorize_route(second["grant_id"], "POST", "/v1/imports", {})
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_exact_denial_and_deadline_keep_base_but_pending_blocks_renewal(host, prepared):
    package, _ = prepared
    try:
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        reader, writer, base = await access(host, profile=profile, remember=True)
        plan = host.plan_install({"package": str(package)})
        pending, _ = await action(host, reader, writer, "install", plan)
        blocked = await frame(reader, writer, {"type": "renew"})
        assert blocked["error"]["code"] == "authorization_request_pending"
        host.authorization.owner_route("POST", "/v1/authorization/requests/" + pending["request_id"] + "/deny", {})
        denied = await frame(reader, writer, {"type": "status"})
        assert denied["status"] == "denied" and denied["base_status"] == "approved"
        pending, _ = await action(host, reader, writer, "install", plan)
        host.authorization.requests[pending["request_id"]]["expires_at"] = time.time() - 1
        expired = await frame(reader, writer, {"type": "status"})
        assert expired["status"] == "expired" and expired["base_status"] == "approved"
        assert host.grants.authenticate(base["token"])
        assert "handoff" in await frame(reader, writer, {"type": "renew"})
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_owner_revoke_between_handoff_and_ack_never_activates(host):
    try:
        reader, writer = await channel(host)
        pending = await frame(reader, writer, {"type": "request_access", "profile_id": None,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"]})
        host.authorization.approve(pending["request_id"], decision(pending))
        handoff = (await frame(reader, writer, {"type": "status"}))["handoff"]
        host.grants.revoke(handoff["grant_id"])
        result = await frame(reader, writer, {"type": "ack", "request_id": handoff["request_id"], "grant_id": handoff["grant_id"]})
        assert result["error"]["code"] == "authorization_request_stale"
        assert not host.authorization.credentials[handoff["grant_id"]]["active"]
        writer.close()
    finally:
        await host.close()


def test_peer_uid_mismatch_rejected_and_real_start_identity_observed(monkeypatch):
    assert authorization.process_start(os.getpid())
    class WrongUID:
        def getsockopt(self, *args):
            return struct.pack("3i", os.getpid(), os.getuid() + 1, 0)
    monkeypatch.setattr(authorization.platform, "system", lambda: "Linux")
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    with pytest.raises(GrantError, match="authorization_peer_unavailable"):
        authorization.peer_identity(WrongUID())


@pytest.mark.asyncio
async def test_live_peer_process_is_bound_to_os_start_not_reported_client(host):
    child = None
    try:
        await host.authorization.start("http://127.0.0.1:19384")
        program = "import socket,sys,time;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);s.sendall(b'{\"version\":1,\"type\":\"status\"}\\n');s.recv(8192);time.sleep(60)"
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", program, str(socket_path(host.root, "authorization")),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        async with asyncio.timeout(3):
            while not host.authorization.leases:
                await asyncio.sleep(.01)
        lease = next(iter(host.authorization.leases.values()))
        assert lease.pid == child.pid and lease.start == authorization.process_start(child.pid)
        child.kill();await child.wait()
        async with asyncio.timeout(3):
            while host.authorization.leases:
                await asyncio.sleep(.01)
        assert not lease.live()
    finally:
        if child is not None and child.returncode is None:
            child.kill();await child.wait()
        await host.close()


@pytest.mark.asyncio
async def test_duplicate_json_and_oversized_channel_frames_cannot_authorize(host):
    try:
        reader, writer = await channel(host)
        writer.write(b'{"version":1,"type":"status","type":"approve"}\n');await writer.drain()
        result = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert result["error"]["code"] == "authorization_invalid_request"
        writer.write(b"x" * (authorization.FRAME_BYTES + 1) + b"\n");await writer.drain()
        assert await asyncio.wait_for(reader.readline(), 2) == b""
        assert host.grants.list()["total"] == 0
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_session_expiry_denies_current_calls_without_replaying_owner(host):
    try:
        reader, writer, grant = await access(host)
        host.authorization.credentials[grant["grant_id"]]["expires_at"] = time.time() - 1
        assert (await frame(reader, writer, {"type": "status"}))["status"] == "expired"
        async with api(host, grant["token"]) as client:
            assert (await client.get("/v1/capabilities")).status_code == 401
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_copied_profile_cannot_approve_another_installation(tmp_path, prepared):
    _, reaper = prepared
    first = Supervisor(tmp_path / "first", reaper)
    profile = first.authorization.create_profile({})["profile"]["profile_id"]
    _, writer, _ = await access(first, profile=profile, remember=True)
    saved = first.authorization.profiles[profile]
    second = Supervisor(tmp_path / "second", reaper)
    second.registry._connection.execute("INSERT INTO authorization_profiles VALUES (?,?)", (profile, json.dumps(saved)))
    await second.close()
    second = Supervisor(tmp_path / "second", reaper)
    try:
        assert profile not in second.authorization.profiles
        reader, other = await channel(second)
        result = await frame(reader, other, {"type": "request_access", "profile_id": profile,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"]})
        assert result["error"]["code"] == "authorization_profile_unavailable"
        writer.close();other.close()
    finally:
        await first.close();await second.close()


def test_registry_v4_upgrade_preserves_existing_state_and_marks_guard_requirement(tmp_path):
    state = tmp_path / "state"
    with Registry(state) as registry:
        slot = registry.add_slot(package_digest="a" * 64, package_path="/synthetic/package", name="Fixture", public_port=19389)
        registry._connection.execute("PRAGMA user_version=4")
    with Registry(state) as registry:
        assert registry._connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert registry.get_slot(slot.id) == slot
        registry._connection.execute("PRAGMA user_version=6")
    with pytest.raises(RegistryError, match="unsupported_registry_schema"):
        Registry(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["base", "preparation", "resolution"])
async def test_absent_native_association_or_session_guard_never_becomes_file_authority(host, tmp_path, kind):
    try:
        reader, writer, base = await access(host)
        grant, ledger = base, host.grants
        if kind != "base":
            plan, _ = workflow_plan(host, tmp_path / "source", kind)
            pending, _ = await action(host, reader, writer, kind, plan)
            host.authorization.approve(pending["request_id"], decision(pending))
            grant = (await frame(reader, writer, {"type": "status"}))["handoff"]
            await frame(reader, writer, {"type": "ack", "request_id": grant["request_id"], "grant_id": grant["grant_id"]})
            ledger = host.workflow_grants
        assert "credential_file" not in ledger.inspect(grant["grant_id"])
        association = host.authorization.credentials.pop(grant["grant_id"])
        with pytest.raises(GrantError, match="grant_revoked"):
            ledger.authenticate(grant["token"])
        host.authorization.credentials[grant["grant_id"]] = association
        guard = ledger.session_guard
        del ledger.session_guard
        with pytest.raises(GrantError, match="grant_revoked"):
            ledger.authenticate(grant["token"])
        ledger.session_guard = guard
        assert ledger.authenticate(grant["token"])["grant_id"] == grant["grant_id"]
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_unsettled_crash_record_cannot_restore_session_on_restart(tmp_path, prepared, missing):
    _, reaper = prepared
    root = tmp_path / "state"
    first = Supervisor(root, reaper)
    _, writer, grant = await access(first)
    await first.close()
    # Reconstruct an interrupted host's still-active durable hash and session
    # record. This fixture never persists the raw bearer.
    with sqlite3.connect(root / "registry.sqlite") as db:
        db.execute("UPDATE management_grants SET revoked=0 WHERE grant_id=?", (grant["grant_id"],))
        db.execute("UPDATE authorization_credentials SET active=1 WHERE grant_id=?", (grant["grant_id"],))
        if missing:
            db.execute("DELETE FROM authorization_credentials WHERE grant_id=?", (grant["grant_id"],))
    second = Supervisor(root, reaper)
    try:
        with pytest.raises(GrantError, match="grant_revoked"):
            second.grants.authenticate(grant["token"])
        writer.close()
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_credential_association_publication_failure_reclaims_prepared_authority(host):
    try:
        reader, writer = await channel(host)
        pending = await frame(reader, writer, {"type": "request_access", "profile_id": None,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"]})
        host.registry._connection.execute("""CREATE TRIGGER reject_native_publication BEFORE INSERT ON authorization_credentials
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        with pytest.raises(RegistryError, match="registry_unavailable"):
            host.authorization.approve(pending["request_id"], decision(pending))
        assert host.grants.list()["total"] == 1 and host.grants.list()["grants"][0]["revoked"]
        assert list(host.grants.root.iterdir()) == []
        assert not host.authorization.credentials
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_generic_profile_uses_reported_name_for_display_while_custom_label_is_preserved(host):
    try:
        for initial, expected in (({}, "Synthetic client"), ({"label": "Owner's integration"}, "Owner's integration")):
            profile = host.authorization.create_profile(initial)["profile"]["profile_id"]
            _, writer, grant = await access(host, profile=profile, remember=True)
            assert host.authorization.profiles[profile]["label"] == expected
            session = next(item for item in host.authorization.list_sessions()["sessions"] if item["session_id"] == grant["grant_id"])
            assert session["label"] == expected and session["client_name"] == "Synthetic client"
            assert "token" not in json.dumps(session) and "credential_file" not in session
            writer.close()
    finally:
        await host.close()
