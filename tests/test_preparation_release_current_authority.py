"""Held signed-shape proof mutations; mock transport only, no live calls."""
from dataclasses import replace
import uuid

import httpx
import pytest

from mcpme.preparation_agent_backend import OpenAIResponsesBackend
from mcpme.preparation_agent_host import AgentPreparationHost, _AdmittedBackend
from mcpme.preparation_agent_providers import AgentProviderRegistry, OpenAIResponsesAdapter
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_release_artifact import verify_release_artifact
from mcpme.preparation_release_policy import AgentExecutionAdmission, POLICY_PATH
from mcpme.preparation_resolution import result_from_files
from test_preparation_release_artifact import artifact
from test_preparation_agent_adapter import adapter_foundation
from test_preparation_agent_host import completed
from test_preparation_agent_providers import choices
from test_preparation_agent_request_readiness import request_only_handler, selected, start
from test_preparation_agent_unlocked_store import foundation
from test_preparation_agent_public_resolution import public_request


def mutate(artifact, damage):
    app, host, policy, *_ = artifact
    path = policy if damage == "policy" else app/"Contents/_CodeSignature/CodeResources" if damage == "seal" else host/"backend/mcpme/preparation_release_artifact.py"
    path.write_bytes(b"changed synthetic artifact")


@pytest.mark.parametrize("damage", ["policy", "seal", "member"])
def test_held_public_resolution_start_fails_before_job_creation(foundation, artifact, damage):
    store, request, candidate, _ = public_request(foundation)
    proof = verify_release_artifact(artifact[1], POLICY_PATH, b"{}")
    store.public_admission = replace(store.public_admission, artifact=proof)
    store.agent_admission = replace(store.agent_admission, artifact=proof)
    plan = store.plan(request, candidate, expires_in=300)
    mutate(artifact, damage)
    with pytest.raises(PreparationError): store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert not store.list()


@pytest.mark.parametrize("phase", ["publish", "consume"])
@pytest.mark.parametrize("damage", ["policy", "seal", "member"])
def test_resolution_publication_and_consumption_require_original_artifact(foundation, artifact, tmp_path, damage, phase):
    store, request, candidate, _ = public_request(foundation)
    proof = verify_release_artifact(artifact[1], POLICY_PATH, b"{}")
    store.public_admission = replace(store.public_admission, artifact=proof)
    store.agent_admission = replace(store.agent_admission, artifact=proof)
    plan = store.plan(request, candidate, expires_in=300)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    workspace = tmp_path/"resolution-work"
    store.begin(job["job_id"], workspace)
    directory = workspace/"derived"; directory.mkdir(parents=True, mode=0o700)
    (directory/"pyproject.toml").write_bytes(store.project(foundation[2], candidate))
    (directory/"uv.lock").write_bytes(b"version=1\nrevision=3\n")
    for path in directory.iterdir(): path.chmod(0o400)
    result = result_from_files(request, directory, (), None, 1)
    if phase == "consume":
        assert store.publish(job["job_id"], result)["state"] == "succeeded"
    mutate(artifact, damage)
    with pytest.raises(PreparationError):
        if phase == "publish": store.publish(job["job_id"], result)
        else: store.resolve_result(request.import_id, 1, request.candidate_id, result.digest)
    if phase == "publish": assert foundation[3].for_job(foundation[4]["job_id"], "completed") is None
    assert foundation[0].inspect(request.import_id)["revision"] == 1


@pytest.mark.parametrize("damage", ["policy", "seal", "member"])
async def test_each_transport_checks_current_artifact_before_sending(artifact, damage):
    proof = verify_release_artifact(artifact[1], POLICY_PATH, b"{}")
    reference = ("agent-python-adapter-v1", "1.0.0", "28c1d862b947f294dcd0a7f038c32f3d9cf4e31ea20f81d91ab800b13d0b8274")
    admission = AgentExecutionAdmission(frozenset({reference}), frozenset(), artifact=proof)
    calls = []
    class Backend:
        backend_id = "openai-responses-v1"
        async def complete(self, *args): calls.append(True)
    backend = _AdmittedBackend(Backend(), admission, lambda request: None)
    mutate(artifact, damage)
    with pytest.raises(PreparationError): await backend.complete(None, None, b"")
    assert not calls


@pytest.mark.parametrize("damage", [None, "policy", "seal", "member"])
async def test_actual_public_review_mock_proposal_adoption_current_proof(adapter_foundation, artifact, tmp_path, monkeypatch, damage):
    imports, recipes, preparations, draft, derivations, family, _ = adapter_foundation
    calls = []
    def transport(request):
        calls.append(True)
        return request_only_handler(request)
    # Transport replacement belongs only to this test. User source/config does
    # not supply a backend, endpoint, factory or a live credential.
    monkeypatch.setattr(OpenAIResponsesAdapter, "backend", lambda self, resolve, configuration:
        OpenAIResponsesBackend(lambda generation: resolve(generation)["api_key"], transport=httpx.MockTransport(transport)))
    proof = verify_release_artifact(artifact[1], POLICY_PATH, b"{}")
    adapter = OpenAIResponsesAdapter()
    admission = AgentExecutionAdmission(frozenset({family.component.reference.key}),
        frozenset({(adapter.adapter_id, adapter.digest, "explicit-test-model")}), artifact=proof)
    registry = AgentProviderRegistry(tmp_path/"providers", execution_admission=admission)
    row = registry.create(choices())["provider"]
    host = AgentPreparationHost(preparations, tmp_path/"agent", provider_registry=registry, execution_admission=admission)
    try:
        plan = await host.plan({**selected(adapter_foundation), "provider_id": row["provider_id"]})
        job = await completed(host, await start(host, plan))
        assert job["state"] == "succeeded" and job["candidate"] and len(calls) == 4
        payload = {"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]}
        if damage:
            mutate(artifact, damage)
            with pytest.raises(PreparationError): await host.adopt(payload)
            assert imports.inspect(draft["draft_id"])["revision"] == 1
        else:
            adopted = await host.adopt(payload)
            assert adopted["draft"]["revision"] == 2 and adopted["draft"]["source"] == draft["source"]
        assert len(calls) == 4 and not preparations.list()
    finally: await host.close()
