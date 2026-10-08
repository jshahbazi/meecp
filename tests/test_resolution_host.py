"""Owner resolution authority, shared admission and joined resource settlement."""
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest

from meecp.grants import read_credential
from meecp.preparation_contracts import contract_digest
from meecp.preparation_contracts import PreparationError
from meecp.preparation_inspection import inspect_source
from meecp.preparation_resolution import ResolutionPolicyV1, ResolutionRequestV1
from meecp.supervisor import HostError, Supervisor, management_app
from test_artifact_capture_api import body as capture_body
from test_preparation_resolution import policy


class MetadataResolutionRunner:
    """Exercise real persisted authority without enabling source execution."""
    def __init__(self, store, host_root, reaper, **options):
        self.store, self.options = store, options
        self.enabled = True
        self.calls = []

    async def plan(self, import_id, revision, candidate_id, raw_policy):
        self.calls.append("plan")
        draft, candidate = await asyncio.to_thread(self.store.candidate, import_id, revision, candidate_id)
        request = request_for(draft, candidate, raw_policy)
        return self.store.plan(request, candidate, expires_in=60)

    async def start(self, plan_id, digest, key):
        self.calls.append("start")
        return self.store.authorize_start(plan_id, digest, key)

    async def cancel(self, job_id):
        return self.store.cancel(job_id)

    async def recover(self):
        return self.store.list_summaries()

    async def close(self):
        self.calls.append("close")


def request_for(draft, candidate, raw_policy):
    return ResolutionRequestV1(schema_version=1, import_id=draft["draft_id"], revision=draft["revision"],
        source_revision=draft["source"]["revision"], candidate_id=candidate["candidate_id"],
        candidate_kind=candidate["kind"], candidate_digest=contract_digest("preparation-resolution-candidate-v1", candidate),
        policy=ResolutionPolicyV1.model_validate(raw_policy).model_dump(mode="json"), runtime={"name": "private-python-toolchain", "digest": "a" * 64},
        producer={"id": "python-resolution-v1", "version": "1.0.0", "digest": "b" * 64})


def selected(host, tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "worker.py").write_text("raise RuntimeError('source must not execute in metadata tests')\ndef double(n: int) -> dict:\n return {'value': n*2}\n")
    (folder / "LICENSE").write_text("Synthetic license\n")
    draft = host.imports.create(folder, name="Synthetic")
    return draft, inspect_source(draft)["candidates"][0]


def job_for(host, tmp_path, raw_policy):
    draft, candidate = selected(host, tmp_path)
    plan = host.resolutions.plan(request_for(draft, candidate, raw_policy), candidate, expires_in=60)
    job = host.resolutions.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    return job, draft


def client_for(host):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19886)),
        base_url="http://127.0.0.1:19886", headers={"Authorization": "Bearer owner"})


async def test_generated_form_exposes_owned_resolution_without_executing_source(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused", resolution_runner_factory=MetadataResolutionRunner)
    try:
        draft, candidate = selected(host, tmp_path)
        request = {"import_id": draft["draft_id"], "revision": 1, "candidate_id": candidate["candidate_id"]}
        async with client_for(host) as client:
            response = await client.post("/v1/preparations/form", json=request)
            assert response.status_code == 200
            form = response.json()
            assert form["resolution_required"] is True and form["resolution_available"] is True
            assert form["resolution_operations"] == ["resolve_dependencies"]
            assert form["resolution_policy"] == policy()
            assert form["resolution_digest"] is None and form["execution_available"] is False
            assert form["recipe"]["id"] == "mcpme.resolved-python"
            stale = await client.post("/v1/preparations/form", json={**request, "resolution_digest": "a" * 64})
            assert stale.status_code == 400
            assert host.resolutions.list() == [] and not host.registry.list_slots()
            assert not (tmp_path / "source/pyproject.toml").exists()
            assert not (tmp_path / "source/uv.lock").exists()
    finally:
        await host.close()


async def test_resolution_routes_preserve_owner_origin_and_exact_idempotency(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused", resolution_runner_factory=MetadataResolutionRunner)
    try:
        draft, candidate = selected(host, tmp_path)
        payload = {"import_id": draft["draft_id"], "revision": 1, "candidate_id": candidate["candidate_id"], "policy": policy()}
        assert host.resolution_runner.options["enabled"] is False
        async with client_for(host) as client:
            grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
            credential = read_credential(Path(grant["credential_file"]))
            headers = {"Authorization": "Bearer " + credential["token"]}
            metadata = await client.get("/v1/resolutions", headers=headers)
            assert metadata.status_code == 200 and metadata.json()["resolutions"] == []
            for method, path, value in (("POST", "/v1/resolutions/start", {}),
                    ("POST", "/v1/resolutions/" + str(uuid.uuid4()) + "/cancel", {}),
                    ("POST", "/v1/resolutions/" + str(uuid.uuid4()) + "/recover", {})):
                response = await client.request(method, path, json=value, headers=headers)
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            assert (await client.post("/v1/resolutions/plan", json=payload,
                headers={"Origin": "https://example.com"})).status_code == 403
            assert host.resolution_runner.calls == []
            scoped_review = await client.post("/v1/resolutions/plan", json=payload, headers=headers)
            assert scoped_review.status_code == 200 and scoped_review.json()["execution_available"] is True
            assert host.resolution_runner.calls == ["plan"]
            assert host.resolutions.list_summaries() == [] and not host.resolution_reservations
            assert (await client.post("/v1/resolutions/plan", json={**payload, "unknown": True})).status_code == 400
            reviewed = (await client.post("/v1/resolutions/plan", json=payload)).json()
            assert reviewed["execution_available"] is True and reviewed["request"]["source_revision"] == draft["source"]["revision"]
            start = {"plan_id": reviewed["plan_id"], "plan_digest": reviewed["plan_digest"], "idempotency_key": str(uuid.uuid4())}
            admitted = (await client.post("/v1/resolutions/start", json=start)).json()
            assert (await client.post("/v1/resolutions/start", json=start)).json()["job_id"] == admitted["job_id"]
            conflict = await client.post("/v1/resolutions/start", json={**start, "plan_digest": "c" * 64})
            assert conflict.json()["error"]["code"] == "resolution_idempotency_conflict"
            inspected = (await client.get("/v1/imports/" + draft["draft_id"])).json()
            assert inspected["resolution"]["job_id"] == admitted["job_id"]
            assert "resolution" not in host.imports.inspect(draft["draft_id"])
            recovered = await client.post("/v1/resolutions/" + admitted["job_id"] + "/recover", json={})
            assert recovered.json()["job_id"] == admitted["job_id"]
            assert not host.registry.list_slots() and not host.registry.list_operations()
    finally:
        await host.close()


async def test_default_resolution_runner_stays_disabled(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    try:
        draft, candidate = selected(host, tmp_path)
        async with client_for(host) as client:
            response = await client.post("/v1/resolutions/plan", json={"import_id": draft["draft_id"],
                "revision": 1, "candidate_id": candidate["candidate_id"], "policy": policy()})
            assert response.status_code == 409 and response.json()["error"]["code"] == "resolution_runner_unavailable"
            assert host.resolutions.list() == [] and not host.resolution_reservations
    finally:
        await host.close()


async def test_resolution_cost_blocks_model_and_capture_and_survives_restart(tmp_path):
    state = tmp_path / "state"
    host = Supervisor(state, tmp_path / "unused", resolution_runner_factory=MetadataResolutionRunner)
    raw_policy = {**policy(), "memory_mb": 128, "disk_bytes": 1024**2}
    try:
        host.resource_policy = {"memory_budget_mb": 160, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
        job, _ = job_for(host, tmp_path, raw_policy)
        resource_id = "resolution:" + job["job_id"]
        await host.reserve_resolution(resource_id, ResolutionPolicyV1.model_validate(raw_policy))
        assert host.resolution_reservations[job["job_id"]]["memory_mb"] == 128
        host.resource_policy["memory_budget_mb"] = 100
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_resolution(resource_id, ResolutionPolicyV1.model_validate(raw_policy))
        host.resource_policy["memory_budget_mb"] = 160
        package = SimpleNamespace(manifest=SimpleNamespace(resources=SimpleNamespace(memory_mb=64, gpu=False)))
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_model(SimpleNamespace(id=str(uuid.uuid4())), package)
        capture = capture_body(tmp_path / "selected")
        queued = host.captures.begin(capture["request"], capture["policy"], capture["target"], capture["idempotency_key"], host.validate_capture_target)
        from meecp.preparation_capture import CapturePolicyV1
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_capture(queued["capture_id"], CapturePolicyV1.model_validate(capture["policy"]))
        await host.capture_runner.recover()
    finally:
        await host.close()
    restarted = Supervisor(state, tmp_path / "unused")
    try:
        assert job["job_id"] in restarted.resolution_reservations
        await restarted.initialize()
        assert restarted.resolutions.get(job["job_id"])["state"] == "interrupted"
        assert not restarted.resolution_reservations and not list(restarted.resolution_reservation_root.iterdir())
        assert not restarted.registry.list_slots()
    finally:
        await restarted.close()


async def test_resolution_charge_is_joined_under_repeated_cancel_and_static_saturation(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused")
    entered, release = threading.Event(), threading.Event()
    static_release = threading.Event()
    pending = None
    unrelated = []
    try:
        raw_policy = {**policy(), "memory_mb": 128, "disk_bytes": 1024**2}
        job, _ = job_for(host, tmp_path, raw_policy)
        resource_id = "resolution:" + job["job_id"]
        from meecp import supervisor as module
        original = module.write_private_json
        def paused(path, value):
            if path.parent == host.resolution_reservation_root:
                entered.set()
                assert release.wait(10)
            return original(path, value)
        monkeypatch.setattr(module, "write_private_json", paused)
        pending = asyncio.create_task(host.reserve_resolution(resource_id, ResolutionPolicyV1.model_validate(raw_policy)))
        assert await asyncio.to_thread(entered.wait, 3)
        async with client_for(host) as client, asyncio.timeout(1):
            assert (await client.get("/v1/status")).status_code == 200
        pending.cancel()
        await asyncio.sleep(0.02)
        pending.cancel()
        await asyncio.sleep(0.02)
        assert host.resource_lock.locked() and not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with host.resource_lock:
            await host.refresh_resolution_reservations()
        assert job["job_id"] in host.resolution_reservations
        markers = [threading.Event() for _ in range(4)]
        def static(ready):
            ready.set()
            assert static_release.wait(10)
        unrelated = [asyncio.create_task(host.static_work(static, marker)) for marker in markers]
        for marker in markers:
            assert await asyncio.to_thread(marker.wait, 3)
        host.resolutions.finish(job["job_id"], interrupted=True)
        await host.release_resolution(resource_id)
        assert not host.resolution_reservations and len(host.static_tasks) == 4
    finally:
        release.set()
        static_release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await asyncio.gather(*unrelated, return_exceptions=True)
        await host.close()


async def test_failed_resolution_recovery_does_not_poison_shutdown_retry(tmp_path, monkeypatch):
    state = tmp_path / "state"
    host = Supervisor(state, tmp_path / "unused")
    original = host.resolution_runner._recover
    attempts = 0
    closed = False

    async def transient_failure():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PreparationError("resolution_interrupted")
        return await original()

    monkeypatch.setattr(host.resolution_runner, "_recover", transient_failure)
    try:
        with pytest.raises(PreparationError, match="resolution_interrupted"):
            await host.recover_resolutions()
        await host.close()
        closed = True
        assert attempts >= 2
        replacement = Supervisor(state, tmp_path / "unused")
        await replacement.close()
    finally:
        if not closed:
            monkeypatch.setattr(host.resolution_runner, "_recover", original)
            # Recovery also replaces a terminal failed coordinator task.
            await host.resolution_runner.recover()
            await host.close()
