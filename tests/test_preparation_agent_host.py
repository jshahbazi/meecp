"""Real stores/FD snapshots with simulated Responses transport; no live A5 proof."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import threading
import uuid

import httpx
import pytest

from mcpme.preparation_agent_backend import OpenAIResponsesBackend
from mcpme.preparation_agent_host import AgentPreparationHost, AgentProviderConfiguration, agent_preparation_routes
from mcpme.preparation_contracts import PreparationError
from mcpme.imports import ImportStore
from mcpme.recipes import RecipeStore
from mcpme.preparations import PreparationStore
from test_mcpme_preparations import recipe_bundle, descriptor as synthetic_descriptor
from test_preparation_agent import response, role_of, output


@pytest.fixture
def foundation(tmp_path):
    import hashlib
    source = tmp_path / "source"; source.mkdir()
    (source / "validator.py").write_text("raise RuntimeError('inspection executed source')\ndef validate_json(document: str) -> dict[str, bool]: return {'valid': True}\n")
    (source / "LICENSE").write_text("Synthetic permission notice.\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    lock = b"version=1\nrevision=3\n"; (source / "uv.lock").write_bytes(lock)
    with ImportStore(tmp_path / "state") as imports:
        recipes = RecipeStore(imports)
        record = recipes.add(recipe_bundle(tmp_path / "bundle"))
        record = recipes.enable(record["recipe_id"], record["version"])
        draft = imports.create(source, name="Unfamiliar validator", intent="Expose the selected validator")
        yield imports, recipes, PreparationStore(imports, recipes), draft, synthetic_descriptor(record, hashlib.sha256(lock).hexdigest())


def provider(handler, *, simulated=True):
    return AgentProviderConfiguration("configured-openai", OpenAIResponsesBackend(
        lambda ref: "synthetic-credential" if ref == "synthetic-reference" else "",
        transport=httpx.MockTransport(handler)), ("explicit-test-model",), "synthetic-reference", simulated)


def selection(draft, descriptor):
    return {"import_id": draft["draft_id"], "revision": draft["revision"], "recipe": descriptor["recipe"],
        "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["validator.py"], "limits": {}}


def handler_for(descriptor):
    def handler(request):
        role = role_of(request)
        proposed = output(role)
        if role == "adapter":
            proposed = output(role, files=[{"path": "proposed/adapter/preparation.json", "content": json.dumps(descriptor)}])
        return response(role, output=[{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": json.dumps(proposed)}]}])
    return handler


async def completed(host, job):
    task = host.tasks[job["job_id"]]
    await asyncio.wait_for(asyncio.shield(task), 3)
    assert not host.tasks and not host.children and not host.stops
    return host.get(job["job_id"])


async def planned_start(host, draft, descriptor):
    plan = await host.plan(selection(draft, descriptor))
    start = {"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
             "idempotency_key": str(uuid.uuid4())}
    return plan, start, await host.start(start)


async def test_review_start_candidate_adoption_preserves_normal_preparation_authority(foundation, tmp_path):
    imports, recipes, preparations, draft, descriptor = foundation
    seen = []
    def handler(request):
        seen.append(request)
        return handler_for(descriptor)(request)
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(handler),), simulated_enabled=True)
    try:
        options = host.options(draft["draft_id"], 1)
        assert options["providers"][0]["simulated"] and not seen
        plan, start, job = await planned_start(host, draft, descriptor)
        assert plan["selected_files"][0]["path"] == "validator.py"
        assert plan["destination"] == "https://api.openai.com/v1/responses"
        assert plan["maximum_reserved_output_tokens"] == 16384 and plan["monetary_ceiling_enforced"] is False
        assert (await host.start(start))["job_id"] == job["job_id"]
        job = await completed(host, job)
        assert job["state"] == "succeeded" and job["local_io_settled"]
        assert job["candidate"] is not None and len(job["calls"]) == len(seen) == 4
        assert job["verification"] is None and job["installed"] is False
        assert preparations.list() == [] and imports.inspect(draft["draft_id"])["attachment"] is None
        adopted = await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert adopted["draft"]["revision"] == 2 and adopted["draft"]["attachment"] is None
        assert adopted["requires_preparation_review"] is True and preparations.list() == []
        selected = preparations.selected_descriptor(draft["draft_id"])
        assert selected["descriptor_digest"] == adopted["descriptor_digest"]
        assert selected["candidate_id"] != job["proposal_digest"]
        assert preparations.plan(draft["draft_id"], 2, selected["descriptor"])["execution_available"] is False
        replay = await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert replay["draft"]["revision"] == 2 and preparations.list() == []
        # The status/plan metadata journal does not retain source, intent, raw
        # provider bodies or credential references. Typed artifacts are separate.
        metadata = " ".join(row[0] for table in ("agent_host_plans", "agent_host_jobs", "attempts")
                            for row in host.db.execute("SELECT data FROM " + table))
        assert draft["intent"] not in metadata and "inspection executed source" not in metadata
        assert "synthetic-credential" not in metadata and "synthetic-reference" not in metadata
    finally:
        await host.close()


async def test_authenticated_routes_preserve_host_origin_and_old_grant_denial(tmp_path):
    from mcpme.supervisor import Supervisor, management_app
    from mcpme.grants import read_credential
    host = Supervisor(tmp_path / "actual-host", tmp_path / "unused-reaper")
    agent = AgentPreparationHost(host.preparations, tmp_path / "agent")
    owner, port = "synthetic-owner", 19886
    app = agent_preparation_routes(management_app(host, owner, port), agent)
    try:
        source = tmp_path / "source"; source.mkdir()
        (source / "worker.py").write_text("def double(n: int) -> dict[str, int]: return {'value': n*2}\n")
        draft = host.imports.create(source, name="Unknown source", intent="Expose the callable")
        grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
        scoped = read_credential(Path(grant["credential_file"]))["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{port}") as client:
            query = {"import_id": draft["draft_id"], "revision": 1}
            response = await client.post("/v1/agent-preparations/plans", json=query, headers={"Authorization": "Bearer " + owner})
            assert response.status_code == 200 and response.json()["providers"] == []
            for route, method in (("plans", "POST"), ("start", "POST"), ("jobs/" + str(uuid.uuid4()), "GET"), ("jobs/" + str(uuid.uuid4()) + "/cancel", "POST")):
                response = await client.request(method, "/v1/agent-preparations/" + route,
                    json=None if method == "GET" else query, headers={"Authorization": "Bearer " + scoped})
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            for headers in ({}, {"Authorization": "Bearer " + owner, "Host": "elsewhere.invalid"},
                            {"Authorization": "Bearer " + owner, "Origin": "https://elsewhere.invalid"}):
                response = await client.post("/v1/agent-preparations/plans", json=query, headers=headers)
                assert response.status_code in {401, 403}
    finally:
        await agent.close(); await host.close()


@pytest.mark.parametrize("changes", [
    {"endpoint": "https://elsewhere.invalid"}, {"credential_ref": "arbitrary"},
    {"provider_id": "unconfigured"}, {"model": "unconfigured"}, {"revision": True},
    {"selected_paths": ["../outside"]}, {"selected_paths": ["validator.py", "validator.py"]},
    {"limits": {"max_calls": 5}}, {"limits": {"max_parallel": 4}}, {"limits": {"monetary_ceiling": 1}},
])
async def test_request_cannot_expand_host_authority(foundation, tmp_path, changes):
    _, _, preparations, draft, descriptor = foundation
    calls = []
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(lambda request: calls.append(request)),), simulated_enabled=True)
    try:
        with pytest.raises((PreparationError, ValueError)):
            await host.plan({**selection(draft, descriptor), **changes})
        assert calls == [] and not host.tasks
    finally:
        await host.close()


@pytest.mark.parametrize("simulated", [True, False])
async def test_public_defaults_and_old_grants_cannot_dispatch(foundation, tmp_path, simulated):
    _, _, preparations, draft, descriptor = foundation
    calls = []
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(lambda request: calls.append(request), simulated=simulated),))
    try:
        plan = await host.plan(selection(draft, descriptor))
        assert not plan["execution_available"]
        start = {"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())}
        with pytest.raises(PreparationError, match="execution_disabled"):
            await host.start(start)
        for method, route, body in (("POST", "plans", selection(draft, descriptor)), ("POST", "start", start),
                                   ("GET", "jobs/" + str(uuid.uuid4()), None), ("POST", "jobs/" + str(uuid.uuid4()) + "/cancel", {})):
            with pytest.raises(PreparationError, match="owner_required"):
                await host.dispatch(method, "/v1/agent-preparations/" + route, body)
        assert calls == []
    finally:
        await host.close()


@pytest.mark.parametrize("mutation", ["revision", "recipe", "bytes", "provider"])
async def test_reviewed_scope_rechecked_before_dispatch(foundation, tmp_path, mutation):
    imports, recipes, preparations, draft, descriptor = foundation
    calls = []
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(lambda request: calls.append(request)),), simulated_enabled=True)
    try:
        plan = await host.plan(selection(draft, descriptor))
        if mutation == "revision":
            imports.revise(draft["draft_id"], 1, intent="Changed intent")
        elif mutation == "recipe":
            recipes.disable(descriptor["recipe"]["id"], descriptor["recipe"]["version"])
        elif mutation == "bytes":
            Path(draft["source"]["snapshot_path"]).joinpath("validator.py").write_text("changed")
        else:
            host.providers.clear()
        start = {"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())}
        with pytest.raises(ValueError):
            await host.start(start)
        assert calls == [] and preparations.list() == []
    finally:
        await host.close()


async def test_repeated_cancel_joins_cancellation_resistant_provider_io(foundation, tmp_path):
    _, _, preparations, draft, descriptor = foundation
    entered, release = asyncio.Event(), asyncio.Event()
    active, settled = 0, 0
    async def handler(request):
        nonlocal active, settled
        active += 1
        if active == 3:
            entered.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return handler_for(descriptor)(request)
        finally:
            settled += 1
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(handler),), simulated_enabled=True)
    try:
        _, _, job = await planned_start(host, draft, descriptor)
        await asyncio.wait_for(entered.wait(), 2)
        cancel = asyncio.create_task(host.cancel(job["job_id"]))
        await asyncio.sleep(0.02)
        cancel.cancel(); cancel.cancel()
        await asyncio.sleep(0.02)
        assert not cancel.done() and not host.get(job["job_id"])["local_io_settled"]
        release.set()
        result = await asyncio.wait_for(cancel, 2)
        assert result["state"] == "canceled" and result["local_io_settled"] and settled == active == 3
        assert len(result["calls"]) == 3 and all(call["usage"] is None for call in result["calls"])
        assert result["candidate"] is None and preparations.list() == []
    finally:
        release.set()
        await host.close()


async def test_cancel_joins_owned_snapshot_thread_before_settlement(foundation, tmp_path, monkeypatch):
    _, _, preparations, draft, descriptor = foundation
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(handler_for(descriptor)),), simulated_enabled=True)
    entered, release = threading.Event(), threading.Event()
    original = host._selected_bytes
    try:
        plan = await host.plan(selection(draft, descriptor))
        def held(*args):
            entered.set(); release.wait(3)
            return original(*args)
        monkeypatch.setattr(host, "_selected_bytes", held)
        job = await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        assert await asyncio.to_thread(entered.wait, 2)
        cancel = asyncio.create_task(host.cancel(job["job_id"]))
        await asyncio.sleep(0.02); cancel.cancel(); await asyncio.sleep(0.02)
        assert not cancel.done() and not host.get(job["job_id"])["local_io_settled"]
        release.set()
        result = await asyncio.wait_for(cancel, 2)
        assert result["state"] == "canceled" and result["local_io_settled"] and result["calls"] == []
        assert not host.tasks and not host.static_workers
    finally:
        release.set(); await host.close()


async def test_restart_does_not_replay_consumed_review_or_stale_proposal(foundation, tmp_path):
    imports, _, preparations, draft, descriptor = foundation
    calls = []
    def handler(request):
        calls.append(request)
        return handler_for(descriptor)(request)
    root = tmp_path / "agent"
    host = AgentPreparationHost(preparations, root, providers=(provider(handler),), simulated_enabled=True)
    plan, start, job = await planned_start(host, draft, descriptor)
    job = await completed(host, job)
    await host.close()
    host = AgentPreparationHost(preparations, root, providers=(provider(handler),), simulated_enabled=True)
    try:
        assert host.get(job["job_id"])["candidate"] is not None
        assert (await host.start(start))["job_id"] == job["job_id"] and len(calls) == 4
        with pytest.raises(PreparationError, match="authority_consumed"):
            await host.start({**start, "idempotency_key": str(uuid.uuid4())})
        imports.revise(draft["draft_id"], 1, intent="Changed intent")
        assert host.get(job["job_id"])["candidate"] is None
        with pytest.raises(ValueError):
            await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert preparations.list() == [] and len(calls) == 4
    finally:
        await host.close()


@pytest.mark.parametrize("malformation", ["recipe", "code", "unresolved"])
async def test_untrusted_artifacts_never_become_execution_or_attachment(foundation, tmp_path, malformation):
    _, _, preparations, draft, descriptor = foundation
    altered = copy.deepcopy(descriptor)
    if malformation == "recipe":
        altered["recipe"]["digest"] = "a" * 64
    base = handler_for(altered)
    def handler(request):
        if role_of(request) == "adapter" and malformation != "recipe":
            task = output("adapter", files=[{"path": "proposed/adapter/run.py", "content": "raise RuntimeError('must not execute')"}])
            if malformation == "unresolved":
                task["unresolved"] = ["Unresolved dependency"]
            return response("adapter", output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(task)}]}])
        return base(request)
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(handler),), simulated_enabled=True)
    try:
        _, _, job = await planned_start(host, draft, descriptor)
        job = await completed(host, job)
        assert job["state"] == "succeeded" and job["candidate"] is None
        assert job["blockers"] == ["agent_candidate_unresolved" if malformation == "unresolved" else "agent_candidate_requires_review"]
        with pytest.raises(ValueError):
            await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert preparations.list() == [] and preparations.imports.inspect(draft["draft_id"])["attachment"] is None
    finally:
        await host.close()
