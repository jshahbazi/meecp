"""Management responsiveness and existing scoped-authority boundaries."""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import httpx
import pytest

from mcpme.grants import read_credential
from mcpme.supervisor import Supervisor, capabilities, management_app
from test_mcpme_imports import prepared_for, source_folder


async def test_foundation_metadata_allows_prepare_and_execution_stays_unavailable(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    port, owner = 19886, "synthetic-owner-authorization"
    try:
        grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
        scoped = read_credential(Path(grant["credential_file"]))["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                                     base_url=f"http://127.0.0.1:{port}") as client:
            for route in ("/v1/recipes", "/v1/preparations"):
                assert (await client.get(route)).status_code == 401
                metadata = await client.get(route, headers={"Authorization": "Bearer " + scoped})
                assert metadata.status_code == 200
                assert (await client.get(route, headers={"Authorization": "Bearer " + owner})).status_code == 200
            denied = await client.post("/v1/preparations/start", json={}, headers={"Authorization": "Bearer " + scoped})
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "grant_scope"
            denied = await client.post("/v1/recipes/enable", json={}, headers={"Authorization": "Bearer " + scoped})
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "grant_scope"
            unavailable = await client.post("/v1/preparations/start", json={}, headers={"Authorization": "Bearer " + owner})
            assert unavailable.status_code == 400 and unavailable.json()["error"]["code"] == "invalid_preparation_request"
            assert not host.registry.list_slots() and not host.registry.list_operations()
        assert capabilities()["automatic_preparation"] is False
        assert capabilities()["recipe_execution"] is False
    finally:
        await host.close()


async def test_owner_recipe_scaffold_is_authenticated_and_collision_safe(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    owner, port = "synthetic-owner-authorization", 19886
    destination = tmp_path / "created-recipe"
    try:
        grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
        scoped = read_credential(Path(grant["credential_file"]))["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                base_url=f"http://127.0.0.1:{port}") as client:
            payload = {"path": str(destination), "recipe_id": "local.created", "component": "python-function-v1"}
            assert (await client.post("/v1/recipes/scaffold", json=payload)).status_code == 401
            denied = await client.post("/v1/recipes/scaffold", json=payload,
                headers={"Authorization": "Bearer " + scoped})
            assert denied.status_code == 403 and not destination.exists()
            malformed = await client.post("/v1/recipes/scaffold", json={"path": str(destination)},
                headers={"Authorization": "Bearer " + owner})
            assert malformed.status_code == 400
            created = await client.post("/v1/recipes/scaffold", json=payload,
                headers={"Authorization": "Bearer " + owner})
            assert created.status_code == 200 and created.json()["recipe_id"] == "local.created"
            collision = await client.post("/v1/recipes/scaffold", json=payload,
                headers={"Authorization": "Bearer " + owner})
            assert collision.status_code == 400
    finally:
        await host.close()


async def test_integrated_agent_routes_preserve_owner_policy_and_closed_defaults(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    port, owner = 19886, "synthetic-owner-authorization"
    try:
        draft = host.imports.create(source_folder(tmp_path), name="Synthetic", intent="Expose validator",
                                    source_mode="automatic")
        grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
        scoped = read_credential(Path(grant["credential_file"]))["token"]
        payload = {"import_id": draft["draft_id"], "revision": draft["revision"]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                base_url=f"http://127.0.0.1:{port}") as client:
            route = "/v1/agent-preparations/plans"
            assert (await client.post(route, json=payload)).status_code == 401
            denied = await client.post(route, json=payload, headers={"Authorization": "Bearer " + scoped})
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "grant_scope"
            job_route = "/v1/agent-preparations/jobs/10000000-0000-4000-8000-000000000001"
            for method, path in (("POST", "/v1/agent-preparations/start"),
                                 ("GET", job_route), ("POST", job_route + "/cancel")):
                denied = await client.request(method, path, headers={"Authorization": "Bearer " + scoped})
                assert denied.status_code == 403 and denied.json()["error"]["code"] == "grant_scope"
            headers = {"Authorization": "Bearer " + owner}
            for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
                assert (await client.post(route, json=payload, headers=headers | extra)).status_code == 403
            options = await client.post(route, json=payload, headers=headers)
            assert options.status_code == 200
            assert options.json()["providers"] == [] and options.json()["latest_job"] is None
            rejected = await client.post("/v1/agent-preparations/start", json={}, headers=headers)
            assert rejected.status_code == 400
        assert not host.agent_preparations.tasks and not host.agent_preparations.static_workers
        assert not host.registry.list_slots() and not host.registry.list_operations()
    finally:
        await host.close()


async def test_slow_attachment_keeps_management_responsive_and_cancel_wins(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    port, owner = 19886, "synthetic-owner-authorization"
    started, release = threading.Event(), threading.Event()
    from mcpme import imports as module
    original = module.snapshot_package
    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5), "synthetic copy barrier timed out"
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "snapshot_package", slow)
    pending = None
    try:
        draft = host.imports.create(source_folder(tmp_path), name="Synthetic", intent="Expose validator")
        package = prepared_for(tmp_path, draft)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + owner}) as client:
            route = "/v1/imports/" + draft["draft_id"]
            pending = asyncio.create_task(client.post(route + "/attach", json={"revision": 1, "package": str(package)}))
            assert await asyncio.to_thread(started.wait, 2)
            async with asyncio.timeout(1):
                assert (await client.get("/v1/status")).status_code == 200
                assert (await client.get(route)).status_code == 200
                result = await client.post(route + "/cancel", json={"revision": 1})
                assert result.status_code == 200 and result.json()["state"] == "canceled"
            release.set()
            rejected = await pending
            assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "draft_inactive"
            assert host.imports.inspect(draft["draft_id"])["attachment"] is None
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await host.close()


async def test_caller_cancellation_does_not_release_owned_thread_or_database(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    started, release = threading.Event(), threading.Event()
    def work():
        started.set()
        assert release.wait(5)
        return host.imports.list()
    caller = asyncio.create_task(host.static_work(work))
    shutdown = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        assert host.static_tasks
        shutdown = asyncio.create_task(host.close())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await shutdown
        assert not host.static_tasks
    finally:
        release.set()
        if shutdown is None:
            await host.close()
        else:
            await asyncio.gather(shutdown, return_exceptions=True)


async def test_shutdown_cancellation_retains_thread_database_and_owner_lock(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def work():
        started.set()
        assert release.wait(5)
        try:
            return host.imports.list()
        finally:
            finished.set()
    caller = asyncio.create_task(host.static_work(work))
    shutdown = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        for _ in range(2):
            shutdown = asyncio.create_task(host.close())
            await asyncio.sleep(0)
            assert not shutdown.done()
            shutdown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await shutdown
            assert host.static_tasks
            assert not finished.is_set() and not caller.done()
            assert host.imports.list() == []
            os.fstat(host.lock_fd)
        shutdown = asyncio.create_task(host.close())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        assert await caller == []
        await shutdown
        assert finished.is_set() and not host.static_tasks
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        if shutdown is not None:
            await asyncio.gather(shutdown, return_exceptions=True)
        if shutdown is None or shutdown.cancelled():
            await host.close()
