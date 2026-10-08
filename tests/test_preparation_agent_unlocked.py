"""Actual agent/store integration with simulated transport; no execution."""
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from meecp.imports import ImportStore
from meecp.recipes import RecipeStore
from meecp.preparations import PreparationStore
from meecp.preparation_agent_host import AgentPreparationHost
from meecp.preparation_agent_unlocked import AgentUnlockedPythonFamily, component_catalog
from meecp.preparation_agent_unlocked_store import AgentUnlockedInputStore
from meecp.preparation_agent_unlocked_resolution import AgentUnlockedResolutionStore, OwnedAgentUnlockedResolutionRunner
from meecp.preparation_contracts import PreparationError
from meecp.preparation_forms import PreparationForms
from test_preparation_agent_adapter import handler
from test_preparation_agent_host import provider, completed
from test_preparation_agent_unlocked_contracts import captured
from test_preparation_agent_unlocked_store import bundle, COMPONENT


@pytest.fixture
def foundation(tmp_path, monkeypatch):
    from meecp import recipes as module
    original = module.builtin_components
    monkeypatch.setattr(module, "builtin_components", lambda: {**original(), **component_catalog()})
    source = captured(tmp_path)
    with ImportStore(tmp_path/"state") as imports:
        recipes = RecipeStore(imports)
        inputs = AgentUnlockedInputStore(imports, recipes)
        resolutions = AgentUnlockedResolutionStore(imports, inputs)
        family = AgentUnlockedPythonFamily(inputs, resolutions)
        record = recipes.add(bundle(tmp_path/"bundle", family.component.reference))
        record = recipes.enable(record["recipe_id"], record["version"])
        draft = imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic")
        preparations = PreparationStore(imports, recipes, families=(family,), resolutions=resolutions)
        yield imports, recipes, draft, inputs, resolutions, family, preparations, record


async def analyzed(foundation, tmp_path):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    def changes(proposals):
        proposals["dependencies"][0]["content"] = json.dumps({"requirements": ["humanize==4.13.0"]})
    host = AgentPreparationHost(preparations, tmp_path/"agent", providers=(provider(handler("a"*64, changes=changes)),), simulated_enabled=True)
    plan = await host.plan({"import_id": draft["draft_id"], "revision": 1,
        "recipe": {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]},
        "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["worker.py"], "limits": {}})
    job = await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
    return host, await completed(host, job)


async def test_analysis_stages_only_and_adoption_stays_blocked(foundation, tmp_path):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    host, job = await analyzed(foundation, tmp_path)
    try:
        assert job["state"] == "succeeded" and len(job["calls"]) == 4 and job["candidate"] is None
        item = inputs.for_job(job["job_id"], "proposal")
        assert item["state"] == "ready" and item["ownership_settled"]
        assert resolutions.list() == preparations.list() == [] and not imports.inspect(draft["draft_id"])["attachment"]
        with pytest.raises(PreparationError, match="agent_candidate_rejected"):
            await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert imports.inspect(draft["draft_id"])["revision"] == 1 and preparations.selected_descriptor(draft["draft_id"]) is None
    finally:
        await host.close()


async def test_pending_form_uses_ordinary_explicit_resolution_scope(foundation, tmp_path):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    host, job = await analyzed(foundation, tmp_path)
    try:
        context = PreparationForms(preparations).context(draft["draft_id"],1)
        assert context.form.resolution_required and context.form.resolution_operations == ["resolve_dependencies"]
        assert context.form.resolution_policy["network"] == ["pypi.org", "files.pythonhosted.org"]
        assert not context.form.fields and context.form.blockers == ["dependency_resolution_required"]
        assert not context.form.execution_available and not context.form.resolution_available
        candidate = resolutions.candidate(draft["draft_id"],1,context.form.candidate_id)[1]
        assert candidate["kind"] == "python-callable" and "_agent_proposal_id" in candidate
        assert resolutions.list() == []
    finally:
        await host.close()


async def test_public_resolution_default_denies_before_runtime_access(foundation, tmp_path):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    host, job = await analyzed(foundation, tmp_path)
    runner = OwnedAgentUnlockedResolutionRunner(resolutions,tmp_path/"absent-host",tmp_path/"absent-reaper")
    try:
        item = inputs.for_job(job["job_id"], "proposal")
        with pytest.raises(PreparationError, match="resolution_runner_unavailable"):
            await runner.plan(draft["draft_id"],1,item["result_digest"],{})
        assert not resolutions.list() and not runner.tasks
    finally:
        await runner.close(); await host.close()


async def test_analysis_cancel_preserves_settled_proposal_without_late_adoption(foundation, tmp_path):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    host, job = await analyzed(foundation, tmp_path)
    try:
        # Completed analysis is a retained proposal; cancellation of its later
        # dependency job belongs to that separately reviewed resolution job.
        assert (await host.cancel(job["job_id"]))["state"] == "succeeded"
        assert not resolutions.list() and imports.inspect(draft["draft_id"])["revision"] == 1
    finally:
        await host.close()
