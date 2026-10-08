"""Typed source policy and synthetic owner credentials; no provider calls."""
import uuid

import pytest

from meecp.preparation_admission import PublicPreparationAdmission
from meecp.preparation_agent_host import AgentPreparationHost
from meecp.preparation_agent_providers import AgentProviderRegistry, OpenAIResponsesAdapter
from meecp.preparation_agent_public_resolution import PublicAgentResolutionStore, PublicAgentResolutionRunner
from meecp.preparation_contracts import PreparationError
from meecp.preparation_release_policy import AgentExecutionAdmission
from meecp.supervisor import Supervisor
from test_preparation_agent_unlocked import foundation
from test_preparation_agent_providers import choices, update


def admission(component, model="accepted-model"):
    adapter = OpenAIResponsesAdapter()
    return AgentExecutionAdmission(frozenset({component.key}), frozenset({(adapter.adapter_id, adapter.digest, model)}), True)


@pytest.mark.parametrize("change", ["policy", "model", "rotation", "remove"])
async def test_review_binds_exact_policy_provider_and_model_before_dispatch(foundation, tmp_path, change):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    accepted = admission(family.component.reference)
    registry = AgentProviderRegistry(tmp_path/"providers", execution_admission=accepted)
    row = registry.create(choices(models=["accepted-model", "other-model"]))["provider"]
    host = AgentPreparationHost(preparations, tmp_path/"agent", provider_registry=registry, execution_admission=accepted)
    scope = {"import_id": draft["draft_id"], "revision": 1, "recipe": {
        "id": record["recipe_id"], "version": record["version"], "digest": record["digest"]},
        "provider_id": row["provider_id"], "model": "accepted-model", "selected_paths": ["worker.py"],
        "selected_metadata_paths": ["LICENSE"], "limits": {}}
    try:
        plan = await host.plan(scope)
        assert plan["execution_available"] and not plan["simulated"] and not plan["blockers"]
        blocked = await host.plan({**scope, "model": "other-model"})
        assert not blocked["execution_available"] and blocked["blockers"] == ["agent_execution_disabled"]
        if change == "policy": host.execution_admission = admission(family.component.reference, "other-model")
        elif change == "model": registry.update(row["provider_id"], update(row, models=["other-model"]))
        elif change == "rotation": registry.update(row["provider_id"], update(row, credentials={"api_key": "synthetic-new-credential"}))
        else: registry.remove(row["provider_id"], {"schema_version": 1, "expected_generation": row["generation"]})
        with pytest.raises(PreparationError):
            await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        assert not host.tasks and not host.static_workers and not host.children and not preparations.list() and not resolutions.list()
    finally: await host.close()


async def test_supervisor_has_one_resolution_owner_and_retains_private_gate(foundation, tmp_path):
    family = foundation[5]
    accepted = admission(family.component.reference)
    public = PublicPreparationAdmission(accepted.components, frozenset({accepted.components}), frozenset({"python-callable"}))
    host = Supervisor(tmp_path/"host", tmp_path/"reaper", public_admission=public, agent_execution_admission=accepted)
    try:
        assert type(host.resolutions) is PublicAgentResolutionStore
        assert type(host.resolution_runner) is PublicAgentResolutionRunner
        assert host.resolution_runner.store is host.resolutions and host.resolution_runner.enabled
        assert host.agent_preparations.execution_admission is host.agent_provider_registry.execution_admission is accepted
        assert not host.agent_preparations.live_enabled and not host.agent_preparations.simulated_enabled
        assert not host.resolution_runner.tasks
    finally: await host.close()


@pytest.mark.parametrize("damage", ["untyped", "private_factory", "missing_public", "wrong_public"])
def test_supervisor_rejects_mixed_authority_before_opening_state(foundation, tmp_path, damage):
    accepted = admission(foundation[5].component.reference)
    public = PublicPreparationAdmission(accepted.components, frozenset({accepted.components}))
    kwargs = {"public_admission": public, "agent_execution_admission": accepted}
    if damage == "untyped": kwargs["agent_execution_admission"] = {"unlocked_resolution": True}
    elif damage == "private_factory": kwargs["resolution_runner_factory"] = lambda *args, **kwargs: None
    elif damage == "missing_public": kwargs["public_admission"] = None
    else: kwargs["public_admission"] = {}
    with pytest.raises(PreparationError): Supervisor(tmp_path/"denied", tmp_path/"reaper", **kwargs)
    assert not (tmp_path/"denied").exists()
