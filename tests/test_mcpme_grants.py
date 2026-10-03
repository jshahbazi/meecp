"""Durable management authority, scoped routes, and exact-plan apply regressions."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import uuid

import httpx
import pytest

from mcpme.grants import GrantError, read_credential
from mcpme.imports import ImportError as ImportDraftError
from mcpme.private import read_private_json, write_private_json
from mcpme.registry import Registry
from mcpme.supervisor import Supervisor, management_app
from test_mcpme_host import prepared, finish, install


def client(host, token="owner"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                             base_url="http://127.0.0.1:19384", headers={"Authorization": "Bearer " + token})


def credential(grant):
    return read_credential(Path(grant["credential_file"]))["token"]


@pytest.mark.asyncio
async def test_grant_metadata_private_files_and_hash_only_database(tmp_path, prepared):
    _, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        async with client(host) as api:
            result = await api.post("/v1/grants", json={"mode": "read"})
            assert result.status_code == 200
            grant = result.json()
            token = credential(grant)
            assert len(token) == 43 and token not in result.text and "token_hash" not in result.text
            assert (Path(grant["credential_file"]).stat().st_mode & 0o777) == 0o600
            assert (host.root / "grants").stat().st_mode & 0o777 == 0o700
            row = host.registry._connection.execute("SELECT token_hash FROM management_grants").fetchone()
            assert row[0] == hashlib.sha256(token.encode()).hexdigest()
            assert token.encode() not in host.registry.path.read_bytes()
            for route in ("/v1/grants", "/v1/grants/" + grant["grant_id"]):
                assert token not in (await api.get(route)).text
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_read_and_prepare_grants_cannot_escalate(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        plan = host.plan_install({"package": str(package)})
        for mode in ("read", "prepare"):
            grant = host.grants.issue({"mode": mode})
            async with client(host, credential(grant)) as api:
                assert (await api.get("/v1/capabilities")).status_code == 200
                assert (await api.get("/v1/plans/" + plan["plan_id"])).status_code == 200
                for route, body in (("/v1/grants", {"mode": "read"}),
                                    ("/v1/grants/" + grant["grant_id"] + "/revoke", {}),
                                    ("/v1/install/apply", {"plan_id": plan["plan_id"]}),
                                    ("/v1/slots/" + plan["slot_id"] + "/start", {}),
                                    ("/v1/migrations/jevk5/plan", {})):
                    response = await api.post(route, json=body)
                    assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
                created = await api.post("/v1/imports", json={"source": str(package), "name": "Synthetic"})
                assert created.status_code == (200 if mode == "prepare" else 403)
                if mode == "prepare":
                    draft = created.json()
                    exported = await api.post("/v1/imports/" + draft["draft_id"] + "/export", json={"revision": 1})
                    assert exported.status_code == 200
                    planned = await api.post("/v1/install/plan", json={"import_id": draft["draft_id"], "revision": 1})
                    assert planned.status_code == 200
        assert not host.registry.list_operations()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_lifecycle_grant_filters_slots_actions_and_operations(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        first, second = await install(host, package, "First"), await install(host, package, "Second")
        host.activity(first.id).record("received")
        host.activity(first.id).record("succeeded", 2)
        host.activity(second.id).record("received")
        host.activity(second.id).record("failed", 4)
        grant = host.grants.issue({"mode": "lifecycle", "slot_ids": [first.id], "actions": ["stop"]})
        token = credential(grant)
        async with client(host, token) as api:
            listed = await api.get("/v1/slots")
            assert listed.status_code == 200
            slots = listed.json()["slots"]
            assert [slot["slot_id"] for slot in slots] == [first.id]
            assert slots[0]["activity"]["received"] == slots[0]["activity"]["succeeded"] == 1
            assert slots[0]["activity"]["failed"] == slots[0]["activity"]["in_flight"] == 0
            inspected = await api.get("/v1/slots/" + first.id)
            assert inspected.status_code == 200
            assert inspected.json()["activity"] == slots[0]["activity"]
            for route in ("/v1/status", "/v1/imports", "/v1/slots/" + second.id, "/v1/grants"):
                assert (await api.get(route)).status_code == 403
            for target, action in ((first, "start"), (first, "restart"), (second, "stop")):
                assert (await api.post(f"/v1/slots/{target.id}/{action}", json={})).status_code == 403
            stopped = await api.post(f"/v1/slots/{first.id}/stop", json={})
            assert stopped.status_code == 200
            operation = await finish(host, stopped.json())
            assert operation.status == "completed"
            assert (await api.get("/v1/operations/" + operation.id)).status_code == 200
            # Normal stop/restart generations do not widen the UUID/action scope.
            assert (await api.post(f"/v1/slots/{first.id}/stop", json={})).status_code == 200
            other = next(value for value in host.registry.list_operations() if value.slot_id == second.id)
            assert (await api.get("/v1/operations/" + other.id)).status_code == 403
            host.registry.update_slot(first.id, generation=host.registry.get_slot(first.id).generation + 1,
                                      package_digest="f" * 64)
            assert (await api.get("/v1/slots/" + first.id)).status_code == 403
            assert (await api.get("/v1/operations/" + operation.id)).status_code == 403
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_exact_plan_duplicate_apply_and_bound_operation_survive_restart(tmp_path, prepared):
    package, reaper = prepared
    state = tmp_path / "host"
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package), "start": False})
        other = host.plan_install({"package": str(package)})
        saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        grant = host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=saved)
        token = credential(grant)
        assert grant["expires_at"] - grant["created_at"] == 900
        async with client(host, token) as api:
            assert (await api.get("/v1/plans/" + other["plan_id"])).status_code == 403
            assert (await api.post("/v1/install/apply", json={"plan_id": other["plan_id"]})).status_code == 403
            first, duplicate = await asyncio.gather(*(api.post("/v1/install/apply", json={"plan_id": plan["plan_id"]}) for _ in range(2)))
            assert first.status_code == duplicate.status_code == 200
            assert first.json()["operation_id"] == duplicate.json()["operation_id"]
            op = await finish(host, first.json())
            assert op.status == "completed"
            assert host.grants.inspect(grant["grant_id"])["operation_id"] == op.id
            assert (await api.get("/v1/operations/" + op.id)).status_code == 200
            assert [slot["slot_id"] for slot in (await api.get("/v1/slots")).json()["slots"]] == [op.slot_id]
            assert (await api.post(f"/v1/slots/{op.slot_id}/start", json={})).status_code == 403
        await host.close()
        host = Supervisor(state, reaper)
        await host.initialize()
        async with client(host, token) as api:
            retry = await api.post("/v1/install/apply", json={"plan_id": plan["plan_id"]})
            assert retry.status_code == 200 and retry.json()["operation_id"] == op.id
        assert len(host.registry.list_operations()) == 1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_modified_plan_is_rejected_before_any_operation(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        plan = host.plan_install({"package": str(package)})
        path = host.root / "plans" / (plan["plan_id"] + ".json")
        saved = read_private_json(path)
        grant = host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=saved)
        write_private_json(path, {**saved, "start": not saved["start"]})
        async with client(host, credential(grant)) as api:
            result = await api.post("/v1/install/apply", json={"plan_id": plan["plan_id"]})
            assert result.status_code == 409 and result.json()["error"]["code"] == "grant_plan_changed"
        assert not host.registry.list_operations()
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("inactive", ["revoke", "expire"])
async def test_grant_rechecked_after_waiting_for_apply_lock(tmp_path, prepared, inactive):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        plan = host.plan_install({"package": str(package)})
        saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        grant = host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=saved)
        token = credential(grant)
        await host.install_lock.acquire()
        async with client(host, token) as api:
            pending = asyncio.create_task(api.post("/v1/install/apply", json={"plan_id": plan["plan_id"]}))
            await asyncio.sleep(0.05)
            assert not pending.done()
            if inactive == "revoke":
                host.grants.revoke(grant["grant_id"])
            else:
                host.registry._connection.execute("UPDATE management_grants SET expires_at=0 WHERE grant_id=?", (grant["grant_id"],))
            host.install_lock.release()
            result = await pending
            assert result.status_code == 401 and result.json()["error"]["code"] == ("grant_revoked" if inactive == "revoke" else "grant_expired")
        assert not host.registry.list_operations() and not host.registry.list_slots()
    finally:
        if host.install_lock.locked():
            host.install_lock.release()
        await host.close()


@pytest.mark.asyncio
async def test_operation_and_grant_binding_roll_back_together(tmp_path, prepared, monkeypatch):
    package, reaper = prepared
    state = tmp_path / "host"
    host = Supervisor(state, reaper)
    try:
        plan = host.plan_install({"package": str(package)})
        saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        grant = host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=saved)
        bind = host.grants.bind_operation
        def fail_after_binding(*args):
            bind(*args)
            raise RuntimeError("synthetic_failure")
        monkeypatch.setattr(host.grants, "bind_operation", fail_after_binding)
        with pytest.raises(RuntimeError, match="synthetic_failure"):
            await host.apply_install(plan["plan_id"], grant_id=grant["grant_id"])
        assert not host.registry.list_operations() and host.grants.inspect(grant["grant_id"])["operation_id"] is None
        await host.close()
        host = Supervisor(state, reaper)
        assert not host.registry.list_operations() and host.grants.inspect(grant["grant_id"])["operation_id"] is None
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_revoked_copied_token_and_expired_grant_stay_inactive_after_restart(tmp_path, prepared):
    _, reaper = prepared
    state = tmp_path / "host"
    host = Supervisor(state, reaper)
    try:
        grants = [host.grants.issue({"mode": "read"}) for _ in range(2)]
        tokens = [credential(value) for value in grants]
        host.grants.revoke(grants[0]["grant_id"])
        host.registry._connection.execute("UPDATE management_grants SET expires_at=0 WHERE grant_id=?", (grants[1]["grant_id"],))
        await host.close()
        host = Supervisor(state, reaper)
        for token, code in zip(tokens, ["grant_revoked", "grant_expired"]):
            async with client(host, token) as api:
                result = await api.get("/v1/capabilities")
                assert result.status_code == 401 and result.json()["error"]["code"] == code
                assert token not in result.text
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_grants_preserve_host_origin_and_strict_json_checks(tmp_path, prepared):
    _, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        grant = host.grants.issue({"mode": "prepare"})
        async with client(host, credential(grant)) as api:
            assert (await api.get("/v1/capabilities", headers={"Origin": "https://invalid.example"})).status_code == 403
            assert (await api.get("/v1/capabilities", headers={"Host": "invalid.example"})).status_code == 403
            response = await api.post("/v1/imports", content='{"name":"first","name":"second"}')
            assert response.status_code == 400
        with pytest.raises(GrantError, match="invalid_grant"):
            host.grants.issue({"mode": "read", "approved": True})
        with pytest.raises(GrantError, match="invalid_grant"):
            host.grants.issue({"mode": "read", "ttl_seconds": True})
    finally:
        await host.close()


@pytest.mark.parametrize("unsafe", ["mode", "link", "parent", "hardlink"])
def test_credential_files_reject_unsafe_paths(tmp_path, unsafe):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "credential.json"
    write_private_json(path, {"schema_version": 1, "grant_id": str(uuid.uuid4()), "token": "a" * 43})
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "link":
        link = root / "link.json"
        link.symlink_to(path)
        path = link
    elif unsafe == "parent":
        root.chmod(0o755)
    else:
        os.link(path, root / "copy.json")
    with pytest.raises(GrantError, match="grant_credential_unavailable"):
        read_credential(path)


def test_registry_version_one_migration_preserves_records(tmp_path):
    state = tmp_path / "host"
    with Registry(state) as registry:
        operation = registry.create_operation("install", idempotency_key=str(uuid.uuid4()))
        registry._connection.execute("DROP TABLE management_grants")
        registry._connection.execute("PRAGMA user_version=1")
    with Registry(state) as migrated:
        assert migrated.get_operation(operation.id) == operation
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert migrated._connection.execute("SELECT count(*) FROM management_grants").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_kind", ["granted", "alternate_owner"])
async def test_pending_intent_resumes_after_transient_draft_submission_failure(tmp_path, prepared, monkeypatch, retry_kind):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        draft = host.imports.create(package, name="Synthetic", start=False)
        plan, alternate = [await host.plan_install_async({"import_id": draft["draft_id"], "revision": 1}) for _ in range(2)]
        saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        grant = host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=saved)
        submit = host.imports.submit
        def fail_submission(*args):
            raise ImportDraftError("imports_unavailable")
        monkeypatch.setattr(host.imports, "submit", fail_submission)
        with pytest.raises(ImportDraftError, match="imports_unavailable"):
            await host.apply_install(plan["plan_id"], grant_id=grant["grant_id"])
        assert len(host.registry.list_operations()) == 1 and not host.tasks
        monkeypatch.setattr(host.imports, "submit", submit)
        retry = await host.apply_install(plan["plan_id"], grant_id=grant["grant_id"]) if retry_kind == "granted" else await host.apply_install(alternate["plan_id"])
        operation = await finish(host, retry)
        assert operation.status == "completed" and operation.slot_id == plan["slot_id"]
        assert len(host.registry.list_slots()) == len(host.registry.list_operations()) == 1
        assert host.grants.inspect(grant["grant_id"])["operation_id"] == operation.id
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_capacity_retires_only_inactive_grants(tmp_path, prepared, monkeypatch):
    import mcpme.grants as grants
    _, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    monkeypatch.setattr(grants, "MAX_GRANTS", 2)
    try:
        active = host.grants.issue({"mode": "read"})
        inactive = host.grants.issue({"mode": "read"})
        active_token, inactive_token = credential(active), credential(inactive)
        with pytest.raises(GrantError, match="grant_limit"):
            host.grants.issue({"mode": "read"})
        host.registry._connection.execute("UPDATE management_grants SET expires_at=0 WHERE grant_id=?", (inactive["grant_id"],))
        replacement = host.grants.issue({"mode": "read"})
        assert host.grants.list()["total"] == 2
        assert host.grants.authenticate(active_token)["grant_id"] == active["grant_id"]
        assert host.grants.authenticate(credential(replacement))["grant_id"] == replacement["grant_id"]
        assert not Path(inactive["credential_file"]).exists()
        with pytest.raises(GrantError, match="unauthorized"):
            host.grants.authenticate(inactive_token)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_second_granted_import_plan_cannot_capture_first_operation(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        draft = host.imports.create(package, name="Synthetic", start=False)
        plans = [await host.plan_install_async({"import_id": draft["draft_id"], "revision": 1}) for _ in range(2)]
        grants = [host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]},
            plan=read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))) for plan in plans]
        applied = await host.apply_install(plans[0]["plan_id"], grant_id=grants[0]["grant_id"])
        with pytest.raises(GrantError, match="^grant_scope$"):
            await host.apply_install(plans[1]["plan_id"], grant_id=grants[1]["grant_id"])
        assert host.grants.inspect(grants[1]["grant_id"])["operation_id"] is None
        operation = await finish(host, applied)
        assert operation.status == "completed"
        assert operation.slot_id == plans[0]["slot_id"]
        assert len(host.registry.list_operations()) == len(host.registry.list_slots()) == 1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_revocation_preserves_durably_authorized_install_operation(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        plan = host.plan_install({"package": str(package), "start": False})
        grant = host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]},
            plan=read_private_json(host.root / "plans" / (plan["plan_id"] + ".json")))
        applied = await host.apply_install(plan["plan_id"], grant_id=grant["grant_id"])
        host.grants.revoke(grant["grant_id"])
        operation = await finish(host, applied)
        assert operation.status == "completed"
        with pytest.raises(GrantError, match="^grant_revoked$"):
            await host.apply_install(plan["plan_id"], grant_id=grant["grant_id"])
        assert len(host.registry.list_operations()) == len(host.registry.list_slots()) == 1
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["read", "prepare", "lifecycle", "install"])
async def test_scoped_grants_cannot_configure_or_remove_installed_slots(tmp_path, prepared, mode):
    package, reaper = prepared
    host = Supervisor(tmp_path / "host", reaper)
    try:
        plan = host.plan_install({"package": str(package), "start": False})
        installed = await finish(host, await host.apply_install(plan["plan_id"]))
        assert installed.status == "completed"
        slot = host.registry.get_slot(installed.slot_id)
        payload = {"mode": mode}
        saved = None
        if mode == "lifecycle":
            payload.update(slot_ids=[slot.id], actions=["start", "stop", "restart"])
        elif mode == "install":
            payload["plan_id"] = plan["plan_id"]
            saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        grant = host.grants.issue(payload, plan=saved)
        if mode == "install":
            retry = await host.apply_install(plan["plan_id"], grant_id=grant["grant_id"])
            assert retry["operation_id"] == installed.id
        async with client(host, credential(grant)) as api:
            assert (await api.get("/v1/slots/" + slot.id)).status_code == 200
            before_operations = host.registry.list_operations()
            for method, route, body in (("PATCH", "/v1/slots/" + slot.id, {"start_policy": "host"}),
                                        ("POST", "/v1/slots/" + slot.id + "/remove", {})):
                response = await api.request(method, route, json=body)
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
                assert host.registry.get_slot(slot.id) == slot
                assert host.registry.list_operations() == before_operations
        async with client(host) as owner:
            updated = await owner.patch("/v1/slots/" + slot.id, json={"start_policy": "host"})
            assert updated.status_code == 200 and updated.json()["slot"]["start_policy"] == "host"
            removed = await owner.post("/v1/slots/" + slot.id + "/remove", json={})
            assert removed.status_code == 200
            assert (await finish(host, removed.json())).status == "completed"
        assert not host.registry.list_slots()
        if mode in {"lifecycle", "install"}:
            async with client(host, credential(grant)) as api:
                assert (await api.get("/v1/slots")).json()["slots"] == []
                assert (await api.get("/v1/slots/" + slot.id)).status_code == 403
                if mode == "install":
                    retry = await api.post("/v1/install/apply", json={"plan_id": plan["plan_id"]})
                    assert retry.status_code == 200 and retry.json()["operation_id"] == installed.id
                    assert not host.registry.list_slots()
    finally:
        await host.close()
