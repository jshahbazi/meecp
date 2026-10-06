"""Actual proposal/form stores with simulated provider and confined-runner seam.

These unit checks assert orchestration and authority, not a real build receipt.
The opt-in app-setup pipeline test separately exercises the signed runtime.
"""
import asyncio
import copy
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest

from mcpme.app_setup import AppSetupHost, LIMITS
from mcpme.app_setup_contracts import RepairAnalysisRequestV1
from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_forms import PreparationForms
from test_preparation_agent_adapter import adapter_foundation, handler
from test_preparation_agent_host import provider


class BuildSeam:
    enabled = True

    def __init__(self, outcomes):
        self.outcomes, self.plans, self.jobs, self.tasks = list(outcomes), [], {}, {}

    async def plan(self, import_id, revision, descriptor, policy):
        self.plans.append(copy.deepcopy(descriptor))
        return {"plan_id": str(uuid.uuid4()), "plan_digest": "a" * 64}

    async def start(self, plan_id, plan_digest, idempotency_key):
        code = self.outcomes.pop(0)
        value = {"job_id": str(uuid.uuid4()), "state": "failed" if code else "succeeded",
            "ownership_settled": True, "reservation_retained": False, "error_code": code}
        self.jobs[value["job_id"]] = value
        return value

    async def cancel(self, identity):
        self.jobs[identity].update(state="canceled", ownership_settled=True)
        return self.jobs[identity]


def workflow(foundation, tmp_path, monkeypatch, *, outcomes=(None,), transport=None):
    imports, recipes, preparations, draft, _, _, lock_hash = foundation
    agent = AgentPreparationHost(preparations, tmp_path / ("agent-" + uuid.uuid4().hex),
        providers=(provider(transport or handler(lock_hash)),), simulated_enabled=True)
    runner = BuildSeam(outcomes)
    real_get, real_inspect = preparations.get, imports.inspect
    monkeypatch.setattr(preparations, "get", lambda identity: runner.jobs[identity] if identity in runner.jobs else real_get(identity))
    def inspect(identity):
        value = real_inspect(identity)
        if runner.jobs and list(runner.jobs.values())[-1]["state"] == "succeeded":
            value["attachment"] = {"test_only": True}
        return value
    monkeypatch.setattr(imports, "inspect", inspect)
    async def static(function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)
    host = SimpleNamespace(agent_preparations=agent, imports=imports, preparations=preparations,
        preparation_forms=PreparationForms(preparations), preparation_runner=runner,
        resolution_runner=SimpleNamespace(enabled=False, tasks={}), static_work=static)
    return AppSetupHost(host), agent, runner, draft


async def start(setup, draft):
    plan = await setup.plan({"import_id": draft["draft_id"], "revision": draft["revision"],
        "provider_id": "configured-openai", "model": "explicit-test-model"})
    payload = {"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())}
    job = await setup.start(payload)
    return plan, payload, job


async def finish(setup, job):
    task = setup.tasks[job["job_id"]]
    await asyncio.wait_for(asyncio.shield(task), 5)
    return setup.get(job["job_id"])


async def test_folder_prepare_drives_forms_build_test_without_user_questions(adapter_foundation, tmp_path, monkeypatch):
    setup, agent, runner, draft = workflow(adapter_foundation, tmp_path, monkeypatch)
    try:
        source = copy.deepcopy(draft["source"])
        plan, payload, job = await start(setup, draft)
        assert plan["limits"] == LIMITS and plan["execution_available"]
        assert plan["selected_file_count"] == 4 and plan["selected_metadata_count"] == 0
        job = await finish(setup, job)
        assert job["state"] == "ready", job.get("error")
        assert job["local_io_settled"] and job["calls_used"] == 4 and not job["installed"]
        assert runner.plans[0]["resources"]["memory_mb"] == 2048
        assert job["draft"]["source"] == source and job["revision"] == 2
        assert (await setup.start(payload))["job_id"] == job["job_id"]
        assert agent.options(draft["draft_id"], 2)["latest_setup_job"]["job_id"] == job["job_id"]
    finally:
        await setup.close(); await agent.close()


async def test_real_proposals_failing_test_get_one_adapter_repair_and_unchanged_expectations(adapter_foundation, tmp_path, monkeypatch):
    seen = []
    base = handler(adapter_foundation[-1])
    def transport(request):
        seen.append(json.loads(request.content))
        return base(request)
    setup, agent, runner, draft = workflow(adapter_foundation, tmp_path, monkeypatch,
        outcomes=("preparation_fixture_failed", None), transport=transport)
    try:
        _, _, job = await start(setup, draft)
        job = await finish(setup, job)
        assert job["state"] == "ready", job.get("error")
        assert job["calls_used"] == 5 and job["attempt"] == 2 and len(runner.plans) == 2
        assert runner.plans[0]["validation"] == runner.plans[1]["validation"]
        assert runner.plans[0]["tools"] == runner.plans[1]["tools"]
        assert "preparation_fixture_failed" in seen[-1]["instructions"]
        assert "repair-adapter" in seen[-1]["instructions"]
        assert json.loads(seen[-1]["input"][0]["content"][0]["text"])["role"] == "adapter"
        inner = agent._plan(agent._job(job["agent_job_id"])["plan_id"])
        assert isinstance(agent._request(inner, agent._draft(draft["draft_id"], job["revision"])), RepairAnalysisRequestV1)
    finally:
        await setup.close(); await agent.close()


async def test_incomplete_proposal_is_corrected_before_build(adapter_foundation, tmp_path, monkeypatch):
    base = handler(adapter_foundation[-1]); seen = []
    def transport(request):
        payload = json.loads(request.content)
        role = json.loads(payload["input"][0]["content"][0]["text"])["role"]
        seen.append(role)
        result = base(request)
        if len(seen) <= 4 and role in {"fixtures", "adapter"}:
            value = json.loads(result.content)
            task = json.loads(value["output"][0]["content"][0]["text"])
            task["unresolved"] = ["Synthetic incomplete generation; must never enter status or logs"]
            value["output"][0]["content"][0]["text"] = json.dumps(task)
            return httpx.Response(200, json=value)
        return result
    setup, agent, runner, draft = workflow(adapter_foundation, tmp_path, monkeypatch, transport=transport)
    try:
        _, _, job = await start(setup, draft)
        job = await finish(setup, job)
        assert job["state"] == "ready", job.get("error")
        assert seen[-2:] == ["fixtures", "adapter"] and job["calls_used"] == 6
        assert len(runner.plans) == 1 and "Synthetic incomplete" not in json.dumps(job)
    finally:
        await setup.close(); await agent.close()


async def test_exhausted_repair_reports_fixed_reason_without_raw_error(adapter_foundation, tmp_path, monkeypatch):
    setup, agent, runner, draft = workflow(adapter_foundation, tmp_path, monkeypatch,
        outcomes=("preparation_fixture_failed", "preparation_fixture_failed"))
    try:
        _, _, job = await start(setup, draft)
        job = await finish(setup, job)
        assert job["state"] == "failed" and job["local_io_settled"] and job["calls_used"] == 5
        assert job["error"]["code"] == "preparation_fixture_failed"
        assert "original synthetic behavior test" in job["error"]["message"]
        assert "preserving" in job["error"]["action"]
        assert len(runner.plans) == 2
    finally:
        await setup.close(); await agent.close()


async def test_cancel_waits_for_provider_children_and_never_builds(adapter_foundation, tmp_path, monkeypatch):
    entered, settled = asyncio.Event(), asyncio.Event()
    async def transport(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()
    setup, agent, runner, draft = workflow(adapter_foundation, tmp_path, monkeypatch, transport=transport)
    try:
        _, _, job = await start(setup, draft)
        await asyncio.wait_for(entered.wait(), 3)
        ended = await setup.cancel(job["job_id"])
        assert ended["state"] == "canceled" and ended["local_io_settled"] and settled.is_set()
        assert not agent.tasks and not agent.children and not setup.tasks and not runner.plans
    finally:
        await setup.close(); await agent.close()


async def test_changed_draft_refuses_dispatch_and_empty_intent_preview_cannot_start(adapter_foundation, tmp_path, monkeypatch):
    setup, agent, _, draft = workflow(adapter_foundation, tmp_path, monkeypatch)
    try:
        selection = {"import_id": draft["draft_id"], "revision": 1,
            "provider_id": "configured-openai", "model": "explicit-test-model"}
        plan = await setup.plan(selection)
        changed = agent.imports.revise(draft["draft_id"], 1, intent="")
        with pytest.raises(ValueError):
            await setup.start({"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        preview = await setup.plan({**selection, "revision": changed["revision"]})
        assert preview["blockers"] == ["agent_intent_required"] and not preview["execution_available"]
        with pytest.raises(PreparationError):
            await setup.start({"plan_id": preview["plan_id"], "plan_digest": preview["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        assert not agent.tasks
    finally:
        await setup.close(); await agent.close()


async def test_full_generation_correction_cannot_spend_an_extra_repair_call(adapter_foundation, tmp_path, monkeypatch):
    base = handler(adapter_foundation[-1]); count = 0
    def transport(request):
        nonlocal count
        count += 1
        result = base(request)
        if count <= 4:
            value = json.loads(result.content)
            task = json.loads(value["output"][0]["content"][0]["text"])
            task["unresolved"] = ["Synthetic initial missing requirement"]
            value["output"][0]["content"][0]["text"] = json.dumps(task)
            return httpx.Response(200, json=value)
        return result
    setup, agent, runner, draft = workflow(adapter_foundation, tmp_path, monkeypatch,
        outcomes=("preparation_fixture_failed",), transport=transport)
    try:
        _, _, job = await start(setup, draft)
        job = await finish(setup, job)
        assert job["state"] == "failed" and job["local_io_settled"]
        assert job["error"]["code"] == "app_setup_budget_exhausted"
        assert count == job["calls_used"] == 8 and len(runner.plans) == 1
        assert job["transfer_bytes_used"] <= LIMITS["max_transfer_bytes"]
    finally:
        await setup.close(); await agent.close()


async def test_all_new_routes_require_owner_and_preserve_host_origin_checks(tmp_path):
    from pathlib import Path
    from mcpme.grants import read_credential
    from mcpme.supervisor import Supervisor, management_app
    host = Supervisor(tmp_path / "host", tmp_path / "unused-reaper")
    port, token = 19889, "synthetic-owner"
    grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
    delegated = read_credential(Path(grant["credential_file"]))["token"]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, token, port)),
                base_url=f"http://127.0.0.1:{port}") as client:
            for path, method in (("plans", "POST"), ("start", "POST"),
                    ("jobs/" + str(uuid.uuid4()), "GET"), ("jobs/" + str(uuid.uuid4()) + "/cancel", "POST")):
                result = await client.request(method, "/v1/app-setups/" + path, json={} if method == "POST" else None,
                    headers={"Authorization": "Bearer " + delegated})
                assert result.status_code == 403 and result.json()["error"]["code"] == "grant_scope"
            for headers in ({}, {"Authorization": "Bearer " + token, "Host": "outside.invalid"},
                    {"Authorization": "Bearer " + token, "Origin": "https://outside.invalid"}):
                result = await client.post("/v1/app-setups/plans", json={}, headers=headers)
                assert result.status_code in {401, 403}
    finally:
        await host.close()
