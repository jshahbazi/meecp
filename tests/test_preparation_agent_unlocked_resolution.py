"""Real source/store authority and held worker controls; no dependency run."""
import asyncio
import os
from pathlib import Path
import threading
import uuid

import pytest

from meecp.preparation_agent_unlocked_resolution import (AgentUnlockedResolutionStore,
    OwnedAgentUnlockedResolutionRunner)
from meecp.preparation_agent_unlocked_store import read_resolution_inputs
from meecp.preparation_contracts import PreparationError, contract_digest
from meecp.preparation_resolution import ResolutionRequestV1
from test_preparation_agent_unlocked_store import foundation, staged, synthetic_resolution


def request_for(foundation):
    imports, recipes, draft, inputs, job, scope, proposal = foundation
    stage, _ = staged(foundation)
    store = AgentUnlockedResolutionStore(imports, inputs)
    observed, candidate = store.candidate(draft["draft_id"], 1, stage.digest)
    request = ResolutionRequestV1(schema_version=1, import_id=draft["draft_id"], revision=1,
        source_revision=draft["source"]["revision"], candidate_id=stage.digest, candidate_kind="python-callable",
        candidate_digest=contract_digest("preparation-resolution-candidate-v1", candidate),
        policy={"schema_version": 1, "operations": ["resolve_dependencies"],
            "network": ["pypi.org", "files.pythonhosted.org"], "wall_seconds": 30, "disk_bytes": 1024**2, "memory_mb": 128},
        runtime={"name": "private-python-toolchain", "digest": "c"*64},
        producer={"id": "python-agent-resolution-v1", "version": "1.0.0", "digest": "f"*64})
    return store, request, candidate


def test_plan_start_metadata_checks_do_not_read_proposal_or_source_bytes(foundation, monkeypatch):
    store, request, candidate = request_for(foundation)
    def forbidden(*args, **kwargs):
        raise AssertionError("full_byte_validation_on_admission")
    monkeypatch.setattr("meecp.preparation_agent_unlocked_store.original_snapshot", forbidden)
    plan = store.plan(request, candidate, expires_in=300)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert job["state"] == "queued" and job["ownership_settled"] and not job["reservation_retained"]
    assert store.check_authority(job["job_id"])["revision"] == 1


@pytest.mark.parametrize("field", ["producer", "candidate_kind", "operations", "network", "candidate_digest"])
def test_agent_plan_cannot_select_other_resolution_authority(foundation, field):
    store, request, candidate = request_for(foundation)
    raw = request.model_dump(mode="json")
    if field == "producer":
        raw[field]["id"] = "python-resolution-v1"
    elif field == "candidate_kind":
        raw[field] = "python-mcp"
    elif field == "operations":
        raw["policy"][field].append("discover_interface")
    elif field == "network":
        raw["policy"][field] = ["pypi.org"]
    else:
        raw[field] = "0"*64
    with pytest.raises(PreparationError, match="agent_unlocked_resolution_mismatch"):
        store.plan(ResolutionRequestV1.model_validate(raw), candidate, expires_in=300)
    assert not store.list()


@pytest.mark.parametrize("change", ["source", "recipe", "proposal"])
def test_start_rechecks_exact_current_authority(foundation, change):
    imports, recipes, draft, inputs, original_job, scope, proposal = foundation
    store, request, candidate = request_for(foundation)
    plan = store.plan(request, candidate, expires_in=300)
    if change == "source":
        (Path(draft["source"]["snapshot_path"])/"worker.py").write_bytes(b"# changed\n")
    elif change == "recipe":
        recipes.disable(scope["recipe"]["id"], scope["recipe"]["version"])
    else:
        inputs.cancel_job(original_job["job_id"])
    with pytest.raises((PreparationError, ValueError)):
        store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert not store.list()


@pytest.mark.parametrize("change", ["fifo", "inode", "mode", "membership", "bytes"])
def test_frozen_resolution_reader_rejects_actual_file_mutations(foundation, tmp_path, change):
    item, job, result, directory = synthetic_resolution(foundation, tmp_path)
    path = directory/"uv.lock"
    if change == "fifo":
        path.unlink(); os.mkfifo(path, 0o400)
    elif change == "inode":
        # Substitute after the root inventory is captured and before opening.
        original = path.read_bytes()
        def check():
            if not getattr(check, "done", False):
                check.done = True; path.rename(tmp_path/"held"); path.write_bytes(original); path.chmod(0o400)
        with pytest.raises(PreparationError, match="agent_unlocked_resolution_mismatch"):
            read_resolution_inputs(directory, result, check)
        return
    elif change == "mode":
        path.chmod(0o600)
    elif change == "membership":
        (directory/"foreign").write_bytes(b"bounded synthetic file")
    else:
        path.chmod(0o600); path.write_bytes(b"version=2\nrevision=3\n"); path.chmod(0o400)
    with pytest.raises(PreparationError, match="agent_unlocked_resolution_mismatch"):
        read_resolution_inputs(directory, result, lambda: None)


async def test_cancel_and_close_join_held_static_worker_before_return(foundation, tmp_path, monkeypatch):
    store, request, candidate = request_for(foundation)
    runner = OwnedAgentUnlockedResolutionRunner(store, tmp_path/"absent", tmp_path/"absent-reaper", enabled=True)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = store.candidate
    def held(*args):
        result = original(*args); entered.set()
        try:
            assert release.wait(3)
            return result
        finally:
            exited.set()
    monkeypatch.setattr(store, "candidate", held)
    planning = asyncio.create_task(runner.plan(request.import_id, 1, request.candidate_id, request.policy.model_dump()))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(runner.close())
        await asyncio.sleep(.02)
        closing.cancel(); planning.cancel(); planning.cancel()
        await asyncio.sleep(.02)
        assert not planning.done() and not closing.done() and not exited.is_set()
    finally:
        release.set()
        await asyncio.gather(planning, closing, return_exceptions=True)
        await runner.close()
    assert exited.is_set() and not runner._planning and not runner.tasks and not store.list()
