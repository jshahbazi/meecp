"""Real capture admission, owner authority and shared supervisor budgets."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest

from meecp.grants import read_credential
from meecp.preparation_capture import CapturePolicyV1
from meecp.supervisor import HostError, Supervisor, capabilities, management_app
from test_meecp_imports import source_folder


def body(root, *, target=None, key="synthetic-capture"):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "weights.onnx").write_bytes(b"\x08\x09Synthetic captured model")
    (root / "LICENSE").write_text("Synthetic artifact license\n")
    return {"request": {"entries": [
        {"name": "weights", "role": "model-weights", "path": str(root / "weights.onnx"), "format": "onnx", "licenses": ["notice"]},
        {"name": "notice", "role": "license", "path": str(root / "LICENSE"), "format": "text"},
    ]}, "policy": {"disk_ceiling_bytes": 1024**2, "wall_seconds": 30},
        "target": target or {"kind": "create-import", "name": "Synthetic detector", "intent": "Classify selected images"},
        "idempotency_key": key}


async def settled(host, capture_id):
    async with asyncio.timeout(10):
        while True:
            current = host.captures.get(capture_id)
            if current["ownership_settled"] and capture_id not in host.capture_reservations:
                return current
            await asyncio.sleep(0.01)


def client_for(host):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19886)),
        base_url="http://127.0.0.1:19886", headers={"Authorization": "Bearer owner"})


async def test_owner_capture_admission_idempotency_and_restart_without_originals(tmp_path):
    state = tmp_path.resolve() / "state"
    host = Supervisor(state, tmp_path / "unused-reaper")
    try:
        payload = body(tmp_path / "selected")
        async with client_for(host) as client:
            rejected = await client.post("/v1/artifact-captures", json={**payload, "unknown": True})
            assert rejected.status_code == 400
            started = await client.post("/v1/artifact-captures", json=payload)
            assert started.status_code == 200
            capture_id = started.json()["capture_id"]
            result = await settled(host, capture_id)
            assert result["state"] == "succeeded" and result["reservation_retained"] is False
            same = (await client.post("/v1/artifact-captures", json=payload)).json()
            assert same["capture_id"] == capture_id
            conflict = await client.post("/v1/artifact-captures", json={**payload, "target": {**payload["target"], "name": "Different"}})
            assert conflict.json()["error"]["code"] == "artifact_capture_idempotency_conflict"
            admitted = result["result"]
            draft = (await client.get("/v1/imports/" + admitted["import_id"])).json()
            assert draft["source"]["kind"] == "captured_artifact_collection"
            assert "snapshot_path" not in draft["source"] and "locator" not in draft["source"]
            summaries = (await client.get("/v1/artifact-captures")).json()["captures"]
            assert "request" not in summaries[0] and summaries[0]["target"] == {"kind": "create-import"}
            assert host.captures.references(result["collection_digest"])[0]["owner_id"] == admitted["import_id"]
            assert not host.capture_reservations and not host.registry.list_slots()
            assert capabilities()["automatic_preparation"] is False
        for entry in payload["request"]["entries"]:
            Path(entry["path"]).unlink()
    finally:
        await host.close()
    restarted = Supervisor(state, tmp_path / "unused-reaper")
    try:
        await restarted.initialize()
        assert restarted.captures.get(capture_id)["result"] == admitted
        assert restarted.imports.inspect(admitted["import_id"])["source"] == draft["source"]
        with restarted.captures.open_artifact(result["collection_digest"], "weights") as descriptor:
            import os
            assert os.read(descriptor, 1024).startswith(b"\x08\x09")
        assert not restarted.capture_reservations
    finally:
        await restarted.close()


async def test_capture_routes_require_owner_and_preserve_origin_checks(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    try:
        grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
        token = read_credential(Path(grant["credential_file"]))["token"]
        async with client_for(host) as client:
            for path, method, value in (("/v1/artifact-captures", "GET", None),
                    ("/v1/artifact-captures", "POST", body(tmp_path / "selected")),
                    ("/v1/artifact-captures/" + str(uuid.uuid4()) + "/cancel", "POST", {}),
                    ("/v1/artifacts/" + "a" * 64 + "/collect", "POST", {})):
                response = await client.request(method, path, json=value, headers={"Authorization": "Bearer " + token})
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            response = await client.post("/v1/artifact-captures", json=body(tmp_path / "other"), headers={"Origin": "https://example.com"})
            assert response.status_code == 403
            assert host.captures.list_summaries() == []
    finally:
        await host.close()


async def test_revision_race_rejects_capture_and_keeps_management_responsive(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    entered, release = threading.Event(), threading.Event()
    original = host.artifacts.capture
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(host.artifacts, "capture", paused)
    try:
        draft = host.imports.create(source_folder(tmp_path), name="Adapter", intent="Classify selected images")
        payload = body(tmp_path / "selected", target={"kind": "attach-to-import", "import_id": draft["draft_id"], "revision": 1})
        async with client_for(host) as client:
            started = (await client.post("/v1/artifact-captures", json=payload)).json()
            assert await asyncio.to_thread(entered.wait, 3)
            assert host.capture_reservations
            async with asyncio.timeout(1):
                assert (await client.get("/v1/status")).status_code == 200
                revised = await client.post("/v1/imports/" + draft["draft_id"] + "/revise", json={"revision": 1, "name": "Changed"})
                assert revised.status_code == 200 and revised.json()["revision"] == 2
            release.set()
            result = await settled(host, started["capture_id"])
            assert result["state"] in {"failed", "canceled"} and result["result"] is None
            assert "captured_artifacts" not in host.imports.inspect(draft["draft_id"])
            assert not host.capture_reservations
    finally:
        release.set()
        await host.close()


async def test_queued_capture_cost_blocks_model_and_disk_admission(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    try:
        host.resource_policy = {"memory_budget_mb": 100, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
        payload = body(tmp_path / "selected")
        queued = host.captures.begin(payload["request"], payload["policy"], payload["target"], payload["idempotency_key"], host.validate_capture_target)
        slot = SimpleNamespace(id=str(uuid.uuid4()))
        package = SimpleNamespace(manifest=SimpleNamespace(resources=SimpleNamespace(memory_mb=64, gpu=False)))
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_model(slot, package)
        assert host.capture_reservations[queued["capture_id"]]["memory_mb"] == 64
        monkeypatch.setattr("meecp.supervisor.shutil.disk_usage", lambda _: SimpleNamespace(free=256 * 1024**2))
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_capture(queued["capture_id"], CapturePolicyV1.model_validate(payload["policy"]))
        await host.capture_runner.recover()
        assert host.captures.get(queued["capture_id"])["ownership_settled"] is True
        assert not host.capture_reservations
    finally:
        await host.close()


async def test_capture_policy_refresh_stays_responsive_and_joins_repeated_cancellation(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    entered, release = threading.Event(), threading.Event()
    pending = None
    original = host.captures.recovery_policy
    def paused(identity):
        entered.set()
        assert release.wait(10)
        return original(identity)
    try:
        payload = body(tmp_path / "selected")
        host.captures.begin(payload["request"], payload["policy"], payload["target"], payload["idempotency_key"], host.validate_capture_target)
        monkeypatch.setattr(host.captures, "recovery_policy", paused)
        slot = SimpleNamespace(id=str(uuid.uuid4()))
        package = SimpleNamespace(manifest=SimpleNamespace(resources=SimpleNamespace(memory_mb=64, gpu=False)))
        pending = asyncio.create_task(host.reserve_model(slot, package))
        assert await asyncio.to_thread(entered.wait, 2)
        async with client_for(host) as client, asyncio.timeout(1):
            assert (await client.get("/v1/status")).status_code == 200
        pending.cancel()
        await asyncio.sleep(0.02)
        pending.cancel()
        await asyncio.sleep(0.02)
        assert not pending.done() and host.resource_lock.locked() and host.static_tasks
        assert slot.id not in host.reservations
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not host.resource_lock.locked() and not host.static_tasks
        assert not host.capture_reservations
    finally:
        release.set()
        if pending is not None and not pending.done():
            await asyncio.gather(pending, return_exceptions=True)
        await host.close()


async def test_capture_settlement_survives_saturated_static_capacity(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    copy_entered, copy_release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()
    static_release = threading.Event()
    static_entered = [threading.Event() for _ in range(4)]
    pending = []
    original_capture = host.artifacts.capture
    original_cleanup = host.imports.discard_previous_capture_attachment

    def paused_capture(*args, **kwargs):
        copy_entered.set()
        assert copy_release.wait(10)
        return original_capture(*args, **kwargs)

    def paused_cleanup(*args):
        cleanup_entered.set()
        assert cleanup_release.wait(10)
        return original_cleanup(*args)

    def paused_static(entered):
        entered.set()
        assert static_release.wait(10)

    monkeypatch.setattr(host.artifacts, "capture", paused_capture)
    monkeypatch.setattr(host.imports, "discard_previous_capture_attachment", paused_cleanup)
    try:
        async with client_for(host) as client:
            started = (await client.post("/v1/artifact-captures", json=body(tmp_path / "selected"))).json()
            capture_id = started["capture_id"]
            assert await asyncio.to_thread(copy_entered.wait, 3)
            pending = [asyncio.create_task(host.static_work(paused_static, entered)) for entered in static_entered]
            for entered in static_entered:
                assert await asyncio.to_thread(entered.wait, 3)
            assert len(host.static_tasks) == 4
            copy_release.set()
            assert await asyncio.to_thread(cleanup_entered.wait, 3)
            assert capture_id in host.capture_reservations
            async with asyncio.timeout(1):
                assert (await client.get("/v1/status")).status_code == 200
            cleanup_release.set()
            result = await settled(host, capture_id)
            assert result["state"] == "succeeded" and not host.capture_reservations
            assert len(host.static_tasks) == 4 and all(not task.done() for task in pending)
    finally:
        copy_release.set()
        cleanup_release.set()
        static_release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await host.close()
