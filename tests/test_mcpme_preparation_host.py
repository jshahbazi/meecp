"""Owner dispatch, durable admission and host ownership cleanup boundaries."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest
import anyio

from mcpme.grants import read_credential
from mcpme.preparation_contracts import ExecutionPolicy
from mcpme.processes import OwnershipLost
from mcpme.preparation_runner import JournaledPreparationOwner
from mcpme.supervisor import HostError, Supervisor, capabilities, management_app
from test_mcpme_preparations import foundation, policy
from test_preparation_processes import group_reaper


class RecordingRunner:
    def __init__(self, store, host_root, reaper, **options):
        self.store, self.host_root, self.reaper, self.options = store, host_root, reaper, options
        self.calls = []
        self.plan_entered = self.plan_release = self.close_entered = self.close_release = None
        self.close_error = None

    async def recover(self):
        self.calls.append(("recover",))

    async def plan(self, import_id, revision, descriptor, execution_policy=None):
        self.calls.append(("plan", import_id, revision, descriptor, execution_policy))
        if self.plan_entered is not None:
            self.plan_entered.set()
            await self.plan_release.wait()
        return self.store.execution_plan(import_id, revision, descriptor, ExecutionPolicy.model_validate(execution_policy),
                                         runner_digest="a" * 64, verifier_digest="b" * 64, runtime_digest="c" * 64)

    async def start(self, plan_id, plan_digest, idempotency_key):
        self.calls.append(("start", plan_id, plan_digest, idempotency_key))
        return self.store.authorize_start(plan_id, plan_digest, idempotency_key)

    async def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return self.store.cancel(job_id)

    async def close(self):
        self.calls.append(("close",))
        if self.close_entered is not None:
            self.close_entered.set()
            await self.close_release.wait()
        if self.close_error is not None:
            raise self.close_error


def host_for(tmp_path, state=None):
    return Supervisor(state or tmp_path / "state", tmp_path / "host/bin/mcpme-reap",
                      preparation_runner_factory=RecordingRunner)


def managed_job(host, draft, value, execution_policy=None):
    reviewed = host.preparations.execution_plan(draft["draft_id"], draft["revision"], value, execution_policy or policy(),
                                               runner_digest="a" * 64, verifier_digest="b" * 64, runtime_digest="c" * 64)
    return host.preparations.authorize_start(reviewed["plan_id"], reviewed["plan_digest"], str(uuid.uuid4()))


async def test_owner_execution_dispatch_keeps_exact_plan_identity_and_scoped_boundary(tmp_path, foundation):
    imports, _, _, draft, value = foundation
    host = host_for(tmp_path, imports.root)
    runner = host.preparation_runner
    assert runner.host_root == (tmp_path / "host").absolute()
    assert runner.options["enabled"] is False
    try:
        owner = "synthetic-owner"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, 19886)),
                                    base_url="http://127.0.0.1:19886") as client:
            headers = {"Authorization": "Bearer " + owner}
            plan_body = {"import_id": draft["draft_id"], "revision": 1, "descriptor": value,
                         "execution_policy": policy().model_dump()}
            for mode in ("read", "prepare"):
                grant = host.grants.issue({"mode": mode, "ttl_seconds": 60})
                token = read_credential(Path(grant["credential_file"]))["token"]
                for route, body in (("inspect-source", {"import_id": draft["draft_id"], "revision": 1}),
                                    ("plan", plan_body), ("start", {}), (str(uuid.uuid4()) + "/cancel", {}),
                                    (str(uuid.uuid4()) + "/recover", {})):
                    assert (await client.post("/v1/preparations/" + route, json=body)).status_code == 401
                    rejected = await client.post("/v1/preparations/" + route, json=body,
                                                 headers={"Authorization": "Bearer " + token})
                    assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "grant_scope"
            assert runner.calls == []
            for route, body in (("start", {}), ("inspect-source", {"import_id": draft["draft_id"]}),
                                ("plan", {**plan_body, "extra": True})):
                rejected = await client.post("/v1/preparations/" + route, json=body, headers=headers)
                assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "invalid_preparation_request"
            runner.plan_entered, runner.plan_release = asyncio.Event(), asyncio.Event()
            pending = asyncio.create_task(client.post("/v1/preparations/plan", json=plan_body, headers=headers))
            await asyncio.wait_for(runner.plan_entered.wait(), 1)
            assert (await client.get("/v1/status", headers=headers)).status_code == 200
            runner.plan_release.set()
            reviewed = (await pending).json()
            assert reviewed["source_revision"] == draft["source"]["revision"]
            body = {"plan_id": reviewed["plan_id"], "plan_digest": reviewed["plan_digest"],
                    "idempotency_key": str(uuid.uuid4())}
            job = (await client.post("/v1/preparations/start", json=body, headers=headers)).json()
            retry = (await client.post("/v1/preparations/start", json=body, headers=headers)).json()
            assert job["job_id"] == retry["job_id"] and job["state"] == "queued"
            host.preparations.record_progress(job["job_id"], "snapshotting", files_checked=1)
            inspected = (await client.get("/v1/imports/" + draft["draft_id"], headers=headers)).json()
            listed = (await client.get("/v1/imports", headers=headers)).json()["imports"][0]
            assert listed["preparation"] == inspected["preparation"]
            assert listed["preparation"]["job_id"] == job["job_id"]
            assert listed["preparation"]["progress"]["files_checked"] == 1
            assert listed["operation_id"] is None and inspected["operation_id"] is None
            assert "preparation" not in host.imports.inspect(draft["draft_id"])
            canceled = await client.post("/v1/preparations/" + job["job_id"] + "/cancel", json={}, headers=headers)
            assert canceled.json()["state"] == "canceled"
            assert sum(call[0] == "recover" for call in runner.calls) >= 1
            assert ("start", body["plan_id"], body["plan_digest"], body["idempotency_key"]) in runner.calls
            assert not host.registry.list_slots() and not host.registry.list_operations()
        assert capabilities()["automatic_preparation"] is False and capabilities()["recipe_execution"] is False
    finally:
        await host.close()


async def test_inspection_does_not_execute_source_and_rejects_revision_race(tmp_path, monkeypatch):
    host = host_for(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "unexpected-execution"
    (source / "worker.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                                    "def calculate(value: str) -> dict:\n    return {}\n")
    draft = host.imports.create(source, name="Synthetic")
    entered, release = threading.Event(), threading.Event()
    original = host.preparations.inspect_source
    def slow(value):
        result = original(value)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(host.preparations, "inspect_source", slow)
    pending = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19886)),
                                    base_url="http://127.0.0.1:19886", headers={"Authorization": "Bearer owner"}) as client:
            pending = asyncio.create_task(client.post("/v1/preparations/inspect-source",
                                                     json={"import_id": draft["draft_id"], "revision": 1}))
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await client.get("/v1/status")).status_code == 200
            host.imports.revise(draft["draft_id"], 1, name="Revised")
            release.set()
            rejected = await pending
            assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "stale_import_revision"
            assert not marker.exists() and host.preparation_runner.calls == [("recover",)]
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await host.close()


async def test_preparation_memory_and_disk_admission_and_settlement(tmp_path, foundation, monkeypatch):
    imports, _, _, draft, value = foundation
    host = host_for(tmp_path, imports.root)
    host.resource_policy = {"memory_budget_mb": 256, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
    try:
        first = managed_job(host, draft, value)
        await host.reserve_preparation(first["job_id"], policy())
        slot = SimpleNamespace(id=str(uuid.uuid4()))
        package = SimpleNamespace(manifest=SimpleNamespace(resources=SimpleNamespace(memory_mb=129, gpu=False)))
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_model(slot, package)
        other = host.imports.create(tmp_path / "source", name="Second")
        second = managed_job(host, other, value)
        monkeypatch.setattr("mcpme.supervisor.shutil.disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_preparation(second["job_id"], policy())
        host.preparations.cancel(first["job_id"])
        await host.release_preparation(first["job_id"], ownership_settled=False)
        assert first["job_id"] in host.preparation_reservations
        await host.release_preparation(first["job_id"])
        assert first["job_id"] not in host.preparation_reservations
        await host.reserve_model(slot, package)
        with pytest.raises(HostError, match="resource_blocked"):
            await host.reserve_preparation(second["job_id"], policy())
        host.reservations.pop(slot.id)
        await host.reserve_preparation(second["job_id"], policy())
        host.preparations.transition(second["job_id"], "snapshotting", ownership_settled=False)
        host.preparations.cancel(second["job_id"])
        await host.release_preparation(second["job_id"])
        assert second["job_id"] in host.preparation_reservations
    finally:
        await host.close()


@pytest.mark.parametrize("missing_reservation_file", [False, True])
async def test_unsettled_preparation_costs_survive_restart_without_authority_replay(tmp_path, foundation, missing_reservation_file):
    imports, _, _, draft, value = foundation
    host = host_for(tmp_path, imports.root)
    job = managed_job(host, draft, value)
    await host.reserve_preparation(job["job_id"], policy())
    host.preparations.transition(job["job_id"], "snapshotting", ownership_settled=False)
    host.imports.revise(draft["draft_id"], 1, name="Changed after admission")
    reservation = host.preparation_reservations[job["job_id"]]
    await host.close()
    if missing_reservation_file:
        (imports.root / "preparation-reservations" / (job["job_id"] + ".json")).unlink()
    restarted = host_for(tmp_path, imports.root)
    try:
        assert restarted.preparation_reservations[job["job_id"]] == reservation
        assert restarted.preparations.get(job["job_id"])["state"] == "interrupted"
        await restarted.recover_preparations()
        assert restarted.preparation_runner.calls == [("recover",)]
        await restarted.release_preparation(job["job_id"])
        assert job["job_id"] in restarted.preparation_reservations
    finally:
        await restarted.close()


async def test_repeated_shutdown_cancellation_settles_runner_and_retains_host_lock(tmp_path):
    host = host_for(tmp_path)
    runner = host.preparation_runner
    runner.close_entered, runner.close_release = asyncio.Event(), asyncio.Event()
    shutdown = asyncio.create_task(host.close())
    try:
        await asyncio.wait_for(runner.close_entered.wait(), 1)
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done() and host.imports.list() == []
        os.fstat(host.lock_fd)
        with pytest.raises(HostError, match="host_already_running"):
            host_for(tmp_path)
        runner.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        os.fstat(host.lock_fd)
        await host.close()
        assert runner.calls == [("close",)]
    finally:
        runner.close_release.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        if shutdown.cancelled():
            # The successful retry above may already have closed descriptors.
            try:
                os.fstat(host.lock_fd)
            except OSError:
                pass
            else:
                await host.close()


async def test_uncertain_runner_cleanup_retains_database_and_host_lock(tmp_path):
    host = host_for(tmp_path)
    host.preparation_runner.close_error = OwnershipLost()
    try:
        for _ in range(2):
            with pytest.raises(OwnershipLost):
                await host.close()
            assert host.imports.list() == []
            os.fstat(host.lock_fd)
            with pytest.raises(HostError, match="host_already_running"):
                host_for(tmp_path)
        assert host.preparation_runner.calls == [("close",), ("close",)]
        host.preparation_runner.close_error = None
        await host.close()
        assert host.preparation_runner.calls == [("close",), ("close",), ("close",)]
        with pytest.raises(OSError):
            os.fstat(host.lock_fd)
        replacement = host_for(tmp_path)
        await replacement.close()
    finally:
        # This fake has no process to settle; release test-owned state directly.
        try:
            os.fstat(host.lock_fd)
        except OSError:
            pass
        else:
            host.imports.close()
            host.registry.close()
            os.close(host.lock_fd)


async def test_anyio_shutdown_cancellation_settles_runner_before_retry(tmp_path):
    host = host_for(tmp_path)
    runner = host.preparation_runner
    runner.close_entered, runner.close_release = asyncio.Event(), asyncio.Event()
    scope = anyio.CancelScope()
    async def shutdown():
        with scope:
            await host.close()
    task = asyncio.create_task(shutdown())
    try:
        await asyncio.wait_for(runner.close_entered.wait(), 1)
        scope.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        runner.close_release.set()
        await task
        assert host.imports.list() == []
        os.fstat(host.lock_fd)
        await host.close()
    finally:
        runner.close_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_production_execution_gate_is_explicit_and_static_inspection_remains_available(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("public gate launched a process"))
    host = Supervisor(tmp_path / "state", tmp_path / "host/bin/mcpme-reap")
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("raise RuntimeError('executed source')\ndef calculate(value: str) -> dict:\n    return {}\n")
    draft = host.imports.create(source, name="Synthetic")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19886)),
                                    base_url="http://127.0.0.1:19886", headers={"Authorization": "Bearer owner"}) as client:
            rejected = await client.post("/v1/preparations/start", json={"plan_id": str(uuid.uuid4()),
                                        "plan_digest": "a" * 64, "idempotency_key": str(uuid.uuid4())})
            assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "preparation_runner_unavailable"
            inspected = await client.post("/v1/preparations/inspect-source", json={"import_id": draft["draft_id"], "revision": 1})
            assert inspected.status_code == 200
            from mcpme.preparation_inspection import inspect_source
            assert inspected.json() == {"api_version": 1, "import_id": draft["draft_id"], "revision": 1, **inspect_source(draft)}
            assert inspected.json()["execution_available"] is False
            assert inspected.json()["candidates"][0]["verified"] is False
            assert not host.preparations.list() and not host.preparation_reservations
    finally:
        await host.close()


@pytest.mark.parametrize("explicit_recovery", [False, True])
async def test_late_native_completion_reconciles_through_disabled_owner_api(tmp_path, foundation, group_reaper, explicit_recovery):
    imports, _, _, draft, value = foundation
    host = Supervisor(imports.root, group_reaper)
    job = managed_job(host, draft, value)
    await host.reserve_preparation(job["job_id"], policy())
    host.preparations.transition(job["job_id"], "snapshotting")
    workspace = imports.root / "preparations" / job["job_id"]
    workspace.mkdir(mode=0o700, parents=True)
    workspace.parent.chmod(0o700)
    host.preparations.set_workspace(job["job_id"], workspace)
    host.preparations.transition(job["job_id"], "preparing")
    owner = JournaledPreparationOwner(group_reaper, host.preparations, job["job_id"], workspace)
    process = await owner.launch(Path(sys.executable), ["-I", "-S", "-c", "import time; time.sleep(30)"],
                                 workspace, {}, owned_group=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19886)),
                                    base_url="http://127.0.0.1:19886", headers={"Authorization": "Bearer owner"}) as client:
            route = "/v1/preparations/" + job["job_id"]
            pending = (await client.get(route)).json()
            assert pending["state"] == "interrupted" and pending["reservation_retained"] and not pending["ownership_settled"]
            assert job["job_id"] in host.preparation_reservations and workspace.exists()
            if explicit_recovery:
                invalid = await client.post(route + "/recover", json={"replay": True})
                assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "invalid_preparation_request"
            await process.stop()
            assert host.preparations.ownership_receipts(job["job_id"])
            response = await client.post(route + "/recover", json={}) if explicit_recovery else await client.get(route)
            final = response.json()
            assert response.status_code == 200 and final["state"] == "interrupted"
            assert final["ownership_settled"] and not final["reservation_retained"]
            assert not host.preparations.ownership_receipts(job["job_id"])
            assert not host.preparation_reservations and not workspace.exists()
            assert not host.preparation_runner.tasks and not host.registry.list_slots() and not host.registry.list_operations()
    finally:
        await owner.close()
        await host.recover_preparations()
        await host.close()


async def test_concurrent_recovery_requests_share_one_owned_reconciliation(tmp_path):
    host = host_for(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def recover():
        calls.append(True)
        entered.set()
        await release.wait()
    host.preparation_runner.recover = recover
    first = asyncio.create_task(host.recover_preparations())
    await entered.wait()
    second = asyncio.create_task(host.recover_preparations())
    try:
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert calls == [True] and not second.done()
        release.set()
        await second
        await host.recover_preparations()
        assert calls == [True, True]
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await host.close()
