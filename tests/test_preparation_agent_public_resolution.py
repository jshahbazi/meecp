"""Public proposal authority and ordinary authority share one resolution owner.

Synthetic proposal/lock fixtures are used only for authority and lifecycle
controls. These checks never launch uv, a provider, or source code.
"""
import asyncio
from pathlib import Path
import threading
import uuid

import pytest

from mcpme.preparation_admission import PublicPreparationAdmission
from mcpme.preparation_agent_public_resolution import PublicAgentResolutionStore, PublicAgentResolutionRunner
from mcpme.preparation_agent_unlocked_resolution import _AGENT_RESOLUTION
from mcpme.preparation_components import ComponentReference
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_release_policy import AgentExecutionAdmission
from mcpme.preparation_resolution import ResolutionRequestV1, result_from_files
from mcpme.preparation_inspection import inspect_source
from test_preparation_agent_unlocked_store import foundation, REFERENCE
from test_preparation_agent_unlocked_resolution import request_for


def public_request(foundation):
    _, request, candidate = request_for(foundation)
    public = PublicPreparationAdmission(frozenset({REFERENCE.key}), frozenset({frozenset({REFERENCE.key})}),
        frozenset({"python-callable", "python-mcp"}))
    admission = AgentExecutionAdmission(frozenset({REFERENCE.key}), frozenset(), True)
    raw = request.model_dump(mode="json"); raw["producer"]["version"] = "1.0.1"
    request = ResolutionRequestV1.model_validate(raw)
    current = [request.producer]
    store = PublicAgentResolutionStore(foundation[0], foundation[3], public_admission=public,
        agent_admission=admission, current_producer=lambda request: current[0])
    return store, request, candidate, current


def test_real_proposal_plans_and_starts_under_separate_public_authority(foundation):
    store, request, candidate, current = public_request(foundation)
    with pytest.raises(PreparationError): store.public_admission.require_candidate(candidate)
    plan = store.plan(request, candidate, expires_in=300)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert job["state"] == "queued" and job["ownership_settled"] and not job["reservation_retained"]
    assert store.check_authority(job["job_id"])["revision"] == 1


def test_same_store_preserves_ordinary_original_candidate_authority(foundation, tmp_path):
    store, agent_request, candidate, current = public_request(foundation)
    source = tmp_path / "ordinary"; source.mkdir()
    (source/"worker.py").write_text("def add(n: int) -> dict[str, int]:\n return {'value': n+1}\n")
    (source/"LICENSE").write_text("Synthetic permission.\n")
    (source/"pyproject.toml").write_text('[project]\nname="synthetic"\nversion="1.0.0"\ndependencies=[]\n')
    draft = foundation[0].create(source, name="Ordinary", source_mode="automatic")
    ordinary = next(value for value in inspect_source(draft)["candidates"] if value["kind"] == "python-callable")
    from test_preparation_resolution import request as ordinary_request
    raw = ordinary_request(draft, ordinary).model_dump(mode="json"); raw["producer"]["version"] = "1.0.1"
    request = ResolutionRequestV1.model_validate(raw); current[0] = request.producer
    plan = store.plan(request, ordinary, expires_in=300)
    assert plan["request"]["producer"]["id"] == "python-resolution-v1"
    with pytest.raises(PreparationError): store.plan(request, {**ordinary, "_agent_proposal_id": candidate["_agent_proposal_id"]}, expires_in=300)
    current[0] = agent_request.producer
    agent_plan = store.plan(agent_request, candidate, expires_in=300)
    assert agent_plan["request"]["producer"]["id"] == "python-agent-resolution-v1"


@pytest.mark.parametrize("change", ["producer", "version", "kind", "network", "operations", "candidate_digest", "candidate_marker"])
def test_public_proposal_cannot_select_other_authority(foundation, change):
    store, request, candidate, current = public_request(foundation)
    raw = request.model_dump(mode="json")
    if change == "producer": raw["producer"]["id"] = "python-resolution-v1"
    elif change == "version": raw["producer"]["version"] = "1.0.0"
    elif change == "kind": raw["candidate_kind"] = "python-mcp"
    elif change == "network": raw["policy"]["network"] = ["pypi.org"]
    elif change == "operations": raw["policy"]["operations"].append("discover_interface")
    elif change == "candidate_digest": raw["candidate_digest"] = "0"*64
    else: candidate = {**candidate, "_agent_proposal_id": str(uuid.uuid4())}
    with pytest.raises(PreparationError): store.plan(ResolutionRequestV1.model_validate(raw), candidate, expires_in=300)
    assert not store.list()


@pytest.mark.parametrize("change", ["source", "recipe", "proposal", "producer", "component"])
def test_public_start_revalidates_original_proof_before_job_creation(foundation, change):
    store, request, candidate, current = public_request(foundation)
    plan = store.plan(request, candidate, expires_in=300)
    if change == "source": (Path(foundation[2]["source"]["snapshot_path"])/"worker.py").write_bytes(b"# changed\n")
    elif change == "recipe": foundation[1].disable(foundation[5]["recipe"]["id"], foundation[5]["recipe"]["version"])
    elif change == "proposal": foundation[3].cancel_job(foundation[4]["job_id"])
    elif change == "producer": current[0] = ComponentReference(id=request.producer.id, version="1.0.1", digest="0"*64)
    else: store.agent_admission = AgentExecutionAdmission(frozenset({(REFERENCE.id, REFERENCE.version, "0"*64)}), frozenset(), True)
    with pytest.raises(PreparationError): store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert not store.list()


def test_runner_producer_domains_are_exact_and_context_resets(foundation, tmp_path):
    store, request, candidate, current = public_request(foundation)
    (tmp_path/"reaper").write_bytes(b"synthetic identity only; never executed")
    (tmp_path/"reaper").chmod(0o700)
    runner = PublicAgentResolutionRunner(store, tmp_path, tmp_path/"reaper", enabled=True,
        public_admission=store.public_admission, agent_admission=store.agent_admission)
    original = runner._producer_sync()
    agent = runner.producer_for(request)
    assert original.id == "python-resolution-v1" and original.version == "1.0.1"
    assert agent.id == "python-agent-resolution-v1" and agent.version == "1.0.1"
    assert agent.digest != original.digest and _AGENT_RESOLUTION.get() is False
    assert runner._producer_sync() == original


async def test_cancel_close_join_public_static_worker_without_second_owner(foundation, tmp_path, monkeypatch):
    store, request, candidate, current = public_request(foundation)
    runner = PublicAgentResolutionRunner(store, tmp_path, tmp_path/"reaper", enabled=True,
        public_admission=store.public_admission, agent_admission=store.agent_admission)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = store.candidate
    def held(*args):
        value = original(*args); entered.set()
        try:
            assert release.wait(3)
            return value
        finally: exited.set()
    monkeypatch.setattr(store, "candidate", held)
    planning = asyncio.create_task(runner.plan(request.import_id, 1, request.candidate_id, request.policy.model_dump(mode="json")))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(runner.close())
        planning.cancel(); closing.cancel()
        await asyncio.sleep(.02)
        assert not planning.done() and not exited.is_set()
    finally:
        release.set()
        await asyncio.gather(*[task for task in (planning, closing) if task], return_exceptions=True)
        await runner.close()
    assert exited.is_set() and not runner._planning and not runner.tasks and not store.list()


async def test_recovery_settles_one_queued_public_proposal_job(foundation, tmp_path):
    store, request, candidate, current = public_request(foundation)
    plan = store.plan(request, candidate, expires_in=300)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    runner = PublicAgentResolutionRunner(store, tmp_path, tmp_path/"reaper", enabled=True,
        public_admission=store.public_admission, agent_admission=store.agent_admission)
    try:
        await runner.recover()
        settled = store.get(job["job_id"])
        assert settled["state"] == "interrupted" and settled["ownership_settled"] and not settled["reservation_retained"]
        assert len(store.list()) == 1 and not runner.tasks
    finally: await runner.close()


@pytest.mark.parametrize("stale", [False, True])
def test_completed_collection_publication_rechecks_producer(foundation, tmp_path, stale):
    store, request, candidate, current = public_request(foundation)
    plan = store.plan(request, candidate, expires_in=300)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    workspace = tmp_path / "synthetic-resolution-work"
    store.begin(job["job_id"], workspace)
    directory = workspace / "derived"; directory.mkdir(parents=True, mode=0o700)
    (directory/"pyproject.toml").write_bytes(store.project(foundation[2], candidate))
    (directory/"uv.lock").write_bytes(b"version=1\nrevision=3\n")
    for path in directory.iterdir(): path.chmod(0o400)
    result = result_from_files(request, directory, (), None, 1)
    if stale:
        current[0] = ComponentReference(id=request.producer.id, version="1.0.1", digest="0"*64)
        with pytest.raises(PreparationError, match="resolution_implementation_changed"):
            store.publish(job["job_id"], result)
        assert foundation[3].for_job(foundation[4]["job_id"], "completed") is None
    else:
        published = store.publish(job["job_id"], result)
        completed = foundation[3].for_job(foundation[4]["job_id"], "completed")
        assert published["state"] == "succeeded" and published["ownership_settled"]
        assert completed["state"] == "ready" and completed["ownership_settled"]
        assert foundation[0].inspect(request.import_id)["revision"] == 1
