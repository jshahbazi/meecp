"""Exact generation-upgrade admission, retries, and capability boundaries."""
import asyncio
from pathlib import Path
import uuid

import pytest

from meecp import __main__ as cli
from meecp.grants import GrantError
from meecp.private import read_private_json, write_private_json
from meecp.supervisor import Supervisor
from meecp.upgrades import UpgradeError
from test_meecp_grants import client, credential
from test_meecp_upgrade_host import UpgradeFixture, settle


def upgrade_grant(host, plan):
    return host.grants.issue({"mode": "upgrade", "plan_id": plan["plan_id"]},
                             plan=host.read_upgrade_plan(plan["plan_id"]))


async def test_exact_upgrade_grant_retries_one_operation_and_preserves_entry(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan, alternate = await fixture.plan(), await fixture.plan()
        grant = upgrade_grant(host, plan)
        token = credential(grant)
        assert grant["expires_at"] - grant["created_at"] == 900
        assert grant["expected_generation"] == fixture.slot.generation
        async with client(host, token) as api:
            assert (await api.get("/v1/upgrade/plans/" + plan["plan_id"])).status_code == 200
            assert (await api.get("/v1/upgrade/plans/" + alternate["plan_id"])).status_code == 403
            assert (await api.post("/v1/upgrade/apply", json={"plan_id": alternate["plan_id"]})).status_code == 403
            first, duplicate = await asyncio.gather(*(api.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]}) for _ in range(2)))
            assert first.status_code == duplicate.status_code == 200
            assert first.json()["operation_id"] == duplicate.json()["operation_id"]
            operation_id = first.json()["operation_id"]
            await settle(host, operation_id)
            operation = host.registry.get_operation(operation_id)
            assert operation.status == "completed"
            current = host.registry.get_slot(fixture.slot.id)
            assert current.id == fixture.slot.id and current.public_port == fixture.slot.public_port
            assert current.credential_ref == fixture.slot.credential_ref and current.package_digest == fixture.new_digest
            assert host.connection(current.id) == fixture.connection
            assert Path(current.data_path).joinpath("state.bin").read_bytes() == b"old generation data"
            assert host.grants.inspect(grant["grant_id"])["operation_id"] == operation_id
            assert (await api.get("/v1/operations/" + operation_id)).status_code == 200
            assert [row["slot_id"] for row in (await api.get("/v1/slots")).json()["slots"]] == [current.id]
            retry = await api.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]})
            assert retry.status_code == 200 and retry.json()["operation_id"] == operation_id
            for route, body in (("/v1/install/apply", {"plan_id": plan["plan_id"]}),
                                ("/v1/slots/" + current.id + "/start", {}),
                                ("/v1/slots/" + current.id + "/remove", {}),
                                ("/v1/operations/" + operation_id + "/cancel", {}),
                                ("/v1/operations/" + operation_id + "/recover", {})):
                assert (await api.post(route, json=body)).status_code == 403
        assert len(host.registry.list_operations()) == 1
        await host.close()
        host = Supervisor(host.root, tmp_path / "unused-reaper")
        async with client(host, token) as api:
            retry = await api.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]})
            assert retry.status_code == 200 and retry.json()["operation_id"] == operation_id
    finally:
        await host.close()


async def test_owner_issues_exact_upgrade_grant_only_for_current_valid_review(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        async with client(host) as owner:
            response = await owner.post("/v1/grants", json={"mode": "upgrade", "plan_id": plan["plan_id"]})
            assert response.status_code == 200
            grant = response.json()
            assert grant["mode"] == "upgrade" and grant["plan_digest"] == plan["plan_digest"]
            assert "token" not in grant
            async with client(host, credential(grant)) as agent:
                assert (await agent.post("/v1/grants", json={"mode": "upgrade", "plan_id": plan["plan_id"]})).status_code == 403
        host.registry.update_slot(fixture.slot.id, generation=fixture.slot.generation + 1)
        with pytest.raises(UpgradeError, match="^stale_generation$"):
            host.validate_upgrade_review(plan["plan_id"])
        with pytest.raises(GrantError, match="^grant_plan_changed$"):
            upgrade_grant(host, plan)
        assert not host.registry.list_operations()
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["plan", "generation", "startup", "settings", "package"])
async def test_upgrade_changed_review_or_installed_entry_creates_no_intent(tmp_path, change):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        grant = upgrade_grant(host, plan)
        if change == "plan":
            path = host.root / "upgrade-plans" / (plan["plan_id"] + ".json")
            saved = read_private_json(path)
            write_private_json(path, {**saved, "start": not saved["start"]})
        elif change == "generation":
            host.registry.update_slot(fixture.slot.id, generation=fixture.slot.generation + 1)
        elif change == "startup":
            host.registry.update_slot(fixture.slot.id, desired_state="stopped")
        elif change == "settings":
            host.registry.update_slot(fixture.slot.id, generation=fixture.slot.generation + 1, settings_digest="f" * 64)
        else:
            sealed = Path(plan["package_path"]) / "bin" / "service"
            sealed.chmod(0o700)
            sealed.write_bytes(b"changed synthetic sealed executable")
            sealed.chmod(0o500)
        async with client(host, credential(grant)) as api:
            response = await api.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]})
            assert response.status_code in {400, 409}
            assert "synthetic sealed executable" not in response.text
        assert not host.registry.list_operations() and not host.tasks
        assert host.grants.inspect(grant["grant_id"])["operation_id"] is None
        assert host.registry.get_slot(fixture.slot.id).package_digest == fixture.slot.package_digest
        assert not fixture.probes
    finally:
        await host.close()


@pytest.mark.parametrize("inactive", ["revoke", "expire"])
async def test_upgrade_grant_rechecked_after_waiting_for_admission_lock(tmp_path, inactive):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        grant = upgrade_grant(host, plan)
        await host.install_lock.acquire()
        async with client(host, credential(grant)) as api:
            pending = asyncio.create_task(api.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]}))
            await asyncio.sleep(0.02)
            assert not pending.done()
            if inactive == "revoke":
                host.grants.revoke(grant["grant_id"])
            else:
                host.registry._connection.execute("UPDATE management_grants SET expires_at=0 WHERE grant_id=?", (grant["grant_id"],))
            host.install_lock.release()
            response = await pending
            assert response.status_code == 401
            assert response.json()["error"]["code"] == ("grant_revoked" if inactive == "revoke" else "grant_expired")
        assert not host.registry.list_operations()
    finally:
        if host.install_lock.locked():
            host.install_lock.release()
        await host.close()


async def test_upgrade_operation_and_grant_binding_roll_back_together(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        grant = upgrade_grant(host, plan)
        bind = host.grants.bind_upgrade_operation
        def fail(*args):
            bind(*args)
            raise RuntimeError("synthetic_binding_failure")
        monkeypatch.setattr(host.grants, "bind_upgrade_operation", fail)
        with pytest.raises(RuntimeError, match="^synthetic_binding_failure$"):
            await host.apply_upgrade(plan["plan_id"], grant_id=grant["grant_id"])
        assert not host.registry.list_operations() and not host.tasks
        assert host.grants.inspect(grant["grant_id"])["operation_id"] is None
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["revoke", "expire", "generation"])
async def test_upgrade_rechecks_admission_after_package_verification_yields(tmp_path, monkeypatch, change):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    inspected, released = asyncio.Event(), asyncio.Event()
    work = host.static_work
    async def delay(function, *args):
        result = await work(function, *args)
        if function == host.inspect_upgrade_plan:
            inspected.set()
            await released.wait()
        return result
    try:
        plan = await fixture.plan()
        grant = upgrade_grant(host, plan)
        monkeypatch.setattr(host, "static_work", delay)
        pending = asyncio.create_task(host.apply_upgrade(plan["plan_id"], grant_id=grant["grant_id"]))
        await asyncio.wait_for(inspected.wait(), 2)
        if change == "revoke":
            host.grants.revoke(grant["grant_id"])
        elif change == "expire":
            host.registry._connection.execute("UPDATE management_grants SET expires_at=0 WHERE grant_id=?", (grant["grant_id"],))
        else:
            host.registry.update_slot(fixture.slot.id, generation=fixture.slot.generation + 1)
        released.set()
        error = UpgradeError if change == "generation" else GrantError
        code = "stale_generation" if change == "generation" else "grant_revoked" if change == "revoke" else "grant_expired"
        with pytest.raises(error, match="^" + code + "$"):
            await pending
        assert not host.registry.list_operations() and not fixture.probes
        assert host.grants.inspect(grant["grant_id"])["operation_id"] is None
    finally:
        released.set()
        await host.close()


async def test_install_grant_cannot_be_reused_for_upgrade(tmp_path):
    fixture = UpgradeFixture(tmp_path)
    host = fixture.host
    try:
        plan = await fixture.plan()
        installation = host.plan_install({"package": str(fixture.candidate_source)})
        grant = host.grants.issue({"mode": "install", "plan_id": installation["plan_id"]}, plan=installation)
        with pytest.raises(GrantError, match="^grant_scope$"):
            await host.apply_upgrade(plan["plan_id"], grant_id=grant["grant_id"])
        assert not host.registry.list_operations()
    finally:
        await host.close()


@pytest.mark.parametrize("extras", [[], ["--ttl-seconds", "900"], ["--ttl-seconds", "3600"], ["--action", "start"]])
async def test_owner_upgrade_authorization_cli_is_exact_and_finite(monkeypatch, tmp_path, extras):
    identity = str(uuid.uuid4())
    calls = []
    async def request(state, method, route, body):
        calls.append((state, method, route, body))
        return {"grant_id": str(uuid.uuid4())}
    monkeypatch.setattr(cli, "call", request)
    args = cli.parser().parse_args(["--state-dir", str(tmp_path), "authorize", "--upgrade-plan", identity, *extras])
    response = await cli.execute(args)
    if extras in ([], ["--ttl-seconds", "900"]):
        assert "grant_id" in response
        assert calls == [(tmp_path, "POST", "/v1/grants", {"mode": "upgrade", "plan_id": identity, "ttl_seconds": 900})]
    else:
        assert response == {"error": {"code": "invalid_grant_request"}}
        assert not calls
