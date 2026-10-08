"""Native upgrade consent over the real leased channel and scoped HTTP API."""
import asyncio
import json
from pathlib import Path
import threading
import time

import pytest

from mcpme.grants import GrantError
from mcpme.private import read_private_json, write_private_json
from test_native_authorization import access, api, decision, frame
from test_mcpme_upgrade_host import UpgradeFixture, settle


async def request_upgrade(reader, writer, plan):
    return await frame(reader, writer, {"type": "request_action", "kind": "upgrade",
        "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"]})


async def test_native_upgrade_review_ack_exact_apply_and_disconnect(tmp_path):
    fixture = UpgradeFixture(tmp_path.resolve(), configured=True)
    host = fixture.host
    try:
        plan = await fixture.plan()
        reader, writer, base = await access(host)
        pending = await request_upgrade(reader, writer, plan)
        assert pending["status"] == "pending" and pending["kind"] == "upgrade"
        assert "upgrade_effects" not in pending["review"]
        assert "idempotency_key" not in pending["review"]
        async with api(host) as owner:
            detail = await owner.get("/v1/authorization/requests/" + pending["request_id"])
            assert detail.status_code == 200
            review = detail.json()["request"]["review"]
            effects = review["upgrade_effects"]
            assert effects["slot_id"] == fixture.slot.id
            assert effects["current"]["version"] == "1.0.0"
            assert effects["candidate"]["version"] == "2.0.0"
            assert effects["current"]["generation"] == effects["candidate"]["expected_generation"] == fixture.slot.generation
            assert effects["current"]["public_port"] == effects["candidate"]["public_port"] == fixture.slot.public_port
            assert effects["current"]["package_digest"] == fixture.slot.package_digest
            assert effects["candidate"]["package_digest"] == plan["package_digest"]
            assert effects["candidate"]["configuration_fields"] == ["enabled"]
            assert "settings" not in json.dumps(effects) and str(tmp_path) not in json.dumps(effects)
            forbidden = await owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve",
                json=decision(pending, remember=True, consent=True))
            assert forbidden.status_code == 400
            approved = await owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve", json=decision(pending))
            assert approved.status_code == 200
            duplicate = await owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve", json=decision(pending))
            assert duplicate.status_code == 200 and host.grants.list()["total"] == 2
        assert not host.registry.list_operations()
        handoff = (await frame(reader, writer, {"type": "status"}))["handoff"]
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(handoff["token"])
        await frame(reader, writer, {"type": "ack", "request_id": handoff["request_id"], "grant_id": handoff["grant_id"]})
        assert not (host.grants.root / (handoff["grant_id"] + ".json")).exists()
        async with api(host, base["token"]) as inspection:
            assert (await inspection.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]})).status_code == 403
        async with api(host, handoff["token"]) as client:
            operation = await client.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]})
            assert operation.status_code == 200
            await settle(host, operation.json()["operation_id"])
            assert host.registry.get_operation(operation.json()["operation_id"]).status == "completed"
            retry = await client.post("/v1/upgrade/apply", json={"plan_id": plan["plan_id"]})
            assert retry.status_code == 200 and retry.json()["operation_id"] == operation.json()["operation_id"]
        lease = next(iter(host.authorization.leases.values()))
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(2):
            while not lease.closed:
                await asyncio.sleep(0)
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(handoff["token"])
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["plan", "generation", "startup", "package"])
async def test_native_upgrade_changed_after_review_cannot_be_approved(tmp_path, change):
    fixture = UpgradeFixture(tmp_path.resolve())
    host = fixture.host
    try:
        plan = await fixture.plan()
        reader, writer, _ = await access(host)
        pending = await request_upgrade(reader, writer, plan)
        assert pending["status"] == "pending"
        if change == "plan":
            path = host.root / "upgrade-plans" / (plan["plan_id"] + ".json")
            saved = read_private_json(path)
            write_private_json(path, {**saved, "start": not saved["start"]})
        elif change == "generation":
            host.registry.update_slot(fixture.slot.id, generation=fixture.slot.generation + 1)
        elif change == "startup":
            host.registry.update_slot(fixture.slot.id, desired_state="stopped")
        else:
            artifact = Path(plan["package_path"]) / "bin/service"
            artifact.chmod(0o700)
            artifact.write_bytes(artifact.read_bytes() + b"changed")
        async with api(host) as owner:
            assert (await owner.get("/v1/authorization/requests/" + pending["request_id"])).status_code == 409
            assert (await owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve", json=decision(pending))).status_code == 409
        assert host.grants.list()["total"] == 1 and not host.registry.list_operations()
        writer.close()
    finally:
        await host.close()


async def test_inspect_only_session_cannot_request_upgrade(tmp_path):
    fixture = UpgradeFixture(tmp_path.resolve())
    host = fixture.host
    try:
        plan = await fixture.plan()
        reader, writer, _ = await access(host, selected=["inspect"])
        assert (await request_upgrade(reader, writer, plan))["error"]["code"] == "grant_scope"
        writer.close()
    finally:
        await host.close()


@pytest.mark.parametrize("stage", ["request", "detail", "approve"])
async def test_upgrade_review_package_work_does_not_block_other_leases(tmp_path, monkeypatch, stage):
    fixture = UpgradeFixture(tmp_path.resolve())
    host = fixture.host
    entered, release = threading.Event(), threading.Event()
    try:
        plan = await fixture.plan()
        reader, writer, _ = await access(host)
        other_reader, other_writer, _ = await access(host)
        pending = None if stage == "request" else await request_upgrade(reader, writer, plan)
        original = host.authorization._upgrade_review_data

        def delayed(value):
            entered.set()
            assert release.wait(5)
            return original(value)

        monkeypatch.setattr(host.authorization, "_upgrade_review_data", delayed)
        async with api(host) as owner:
            if stage == "request":
                task = asyncio.create_task(request_upgrade(reader, writer, plan))
            elif stage == "detail":
                task = asyncio.create_task(owner.get("/v1/authorization/requests/" + pending["request_id"]))
            else:
                task = asyncio.create_task(owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve", json=decision(pending)))
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            started = time.monotonic()
            assert (await frame(other_reader, other_writer, {"type": "status"}))["status"] == "approved"
            assert time.monotonic() - started < 1
            release.set()
            result = await task
            assert result["status"] == "pending" if stage == "request" else result.status_code == 200
        writer.close(); other_writer.close()
    finally:
        release.set()
        await host.close()


async def test_upgrade_revoked_during_owner_review_creates_no_action_grant(tmp_path, monkeypatch):
    fixture = UpgradeFixture(tmp_path.resolve())
    host = fixture.host
    entered, release = threading.Event(), threading.Event()
    try:
        plan = await fixture.plan()
        reader, writer, base = await access(host)
        pending = await request_upgrade(reader, writer, plan)
        original = host.authorization._upgrade_review_data

        def delayed(value):
            entered.set()
            assert release.wait(5)
            return original(value)

        monkeypatch.setattr(host.authorization, "_upgrade_review_data", delayed)
        async with api(host) as owner:
            task = asyncio.create_task(owner.post("/v1/authorization/requests/" + pending["request_id"] + "/approve", json=decision(pending)))
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
            host.grants.revoke(base["grant_id"])
            release.set()
            assert (await task).status_code in {401, 403, 409}
        assert host.grants.list()["total"] == 1 and not host.registry.list_operations()
        writer.close()
    finally:
        release.set()
        await host.close()
