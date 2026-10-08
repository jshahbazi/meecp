"""Actual durable private artifacts; resolver bytes here are synthetic only."""
import hashlib
import asyncio
import json
import os
from pathlib import Path
import time
import threading
import uuid

import pytest

from meecp.imports import ImportStore
from meecp.recipes import RecipeStore
from meecp.preparation_agent_adapter_store import CODE_PATH, original_snapshot
from meecp.preparation_agent_unlocked_contracts import canonical_dependency_project
from meecp.preparation_agent_unlocked_store import AgentUnlockedInputStore, UnlockedCompletionRequestV1
from meecp.preparation_components import ComponentReference
from meecp.preparation_contracts import PreparationError, contract_digest
from meecp.preparation_resolution import ResolutionRequestV1, result_from_files
from test_preparation_agent_unlocked_contracts import captured, proposal


COMPONENT = "agent-unlocked-python-adapter-v1"
REFERENCE = ComponentReference(id=COMPONENT, version="1.0.0", digest="d"*64)


def bundle(path, reference=REFERENCE):
    path.mkdir()
    license_data = b"Synthetic recipe permission.\n"; (path/"LICENSE").write_bytes(license_data)
    manifest = {"schema_version": 1, "id": "mcpme.agent-unlocked-python-adapter", "version": "1.0.0", "kind": "declarative",
        "worker_api_version": 1, "input_schema": {"type": "object", "additionalProperties": False}, "form": [],
        "steps": [{"component": COMPONENT, "version": "1.0.0", "digest": reference.digest, "config": {}}],
        "licenses": ["LICENSE"], "provenance": {"origin": "synthetic", "publisher": "test"},
        "requirements": {"host_api_version": 1, "package_schema": 1, "protocol": "2025-11-25", "os": "macos",
            "architecture": "arm64", "capabilities": ["declarative-composition"]}}
    data = json.dumps(manifest).encode(); (path/"meecp.recipe.json").write_bytes(data)
    (path/"meecp.recipe.lock.json").write_text(json.dumps({"schema_version": 1,
        "manifest_sha256": hashlib.sha256(data).hexdigest(), "artifacts": [{"path": "LICENSE", "size": len(license_data),
            "sha256": hashlib.sha256(license_data).hexdigest(), "license": "LICENSE", "origin": "synthetic"}]}))
    return path


@pytest.fixture
def foundation(tmp_path, monkeypatch, request):
    from meecp import recipes as module
    original = module.builtin_components
    monkeypatch.setattr(module, "builtin_components", lambda: {**original(), COMPONENT+"@1.0.0": {
        "digest": REFERENCE.digest, "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}})
    source = captured(tmp_path,files=getattr(request,"param",None))
    with ImportStore(tmp_path/"state") as imports:
        recipes = RecipeStore(imports); record = recipes.add(bundle(tmp_path/"bundle"))
        record = recipes.enable(record["recipe_id"], record["version"])
        draft = imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic")
        store = AgentUnlockedInputStore(imports, recipes)
        proposed = proposal(requirements=["humanize==4.13.0"])
        job = {"import_id": draft["draft_id"], "revision": 1, "source_revision": draft["source"]["revision"],
            "job_id": str(uuid.uuid4()), "attempt_id": proposed.attempt_id, "proposal_digest": proposed.digest}
        plan = {"source_identity_digest": contract_digest("agent-host-source-v1", draft["source"]),
            "plan_digest": "b"*64, "request_digest": proposed.request_digest, "recipe": {"id": record["recipe_id"],
                "version": record["version"], "digest": record["digest"]}, "recipe_epoch": record["epoch"], "limits": {},
            "expires_at": int(time.time())+300}
        yield imports, recipes, draft, store, job, plan, proposed


def staged(foundation):
    imports, recipes, draft, store, job, plan, proposed = foundation
    result = store.stage(job, plan, proposed, REFERENCE, draft)
    item = store.for_job(job["job_id"], "proposal")
    return result, item


def synthetic_resolution(foundation, tmp_path):
    imports, recipes, draft, store, job, plan, proposed = foundation
    stage, item = staged(foundation)
    directory = tmp_path/"synthetic-resolved"; directory.mkdir(mode=0o700)
    (directory/"pyproject.toml").write_bytes(canonical_dependency_project(stage.specification, original_snapshot(draft)[0]))
    (directory/"uv.lock").write_bytes(b"version=1\nrevision=3\n")
    for entry in directory.iterdir():
        entry.chmod(0o400)
    request = ResolutionRequestV1(schema_version=1, import_id=draft["draft_id"], revision=1,
        source_revision=draft["source"]["revision"], candidate_id=stage.digest, candidate_kind="python-callable",
        candidate_digest="e"*64, policy={"schema_version": 1, "operations": ["resolve_dependencies"],
            "network": ["pypi.org", "files.pythonhosted.org"], "wall_seconds": 30, "disk_bytes": 1024**2, "memory_mb": 128},
        runtime={"name": "private-python-toolchain", "digest": "c"*64},
        producer={"id": "python-agent-resolution-v1", "version": "1.0.0", "digest": "f"*64})
    result = result_from_files(request, directory, (), None, 1)
    resolution_job = {"job_id": str(uuid.uuid4()), "plan_digest": "9"*64, "state": "running", "ownership": [],
        "request": request.model_dump(mode="json")}
    return item, resolution_job, result, directory


def test_stage_has_no_lock_and_journal_contains_no_proposed_source(foundation):
    imports, recipes, draft, store, job, plan, proposed = foundation
    result, item = staged(foundation)
    assert result.resolution is None and len(result.derived_inputs) == 3 and item["ownership_settled"]
    assert not (store.root/item["id"]/"uv.lock").exists()
    assert set(os.listdir(store.root/item["id"])) == {"LICENSE", "adapter.json", CODE_PATH}
    raw = store.db.execute("SELECT data FROM agent_unlocked_inputs").fetchone()[0]
    assert "humanize==4.13.0" not in raw and "from worker import" not in raw and "Synthetic wrapper permission" not in raw
    assert not imports.inspect(draft["draft_id"])["attachment"] and not store.workers


def test_cold_reopen_resolves_same_attempt_without_adopting_new_baseline(foundation):
    imports, recipes, draft, store, job, plan, proposed = foundation
    result, item = staged(foundation)
    reopened = AgentUnlockedInputStore(imports, recipes)
    assert reopened.resolve(item["id"], result.digest, draft)[0] == result
    path = store.root/item["id"]/CODE_PATH
    path.write_bytes(path.read_bytes()+b"# changed\n")
    with pytest.raises(PreparationError, match="input_changed"):
        reopened.resolve(item["id"], result.digest, draft)


def test_canceled_stage_deletes_only_owned_artifacts(foundation):
    imports, recipes, draft, store, job, plan, proposed = foundation
    result, item = staged(foundation)
    store.cancel_job(job["job_id"])
    with pytest.raises(PreparationError, match="unavailable"):
        store.resolve(item["id"], result.digest, draft)
    store.delete(item["id"])
    assert store.get(item["id"])["state"] == "deleted" and not (store.root/item["id"]).exists()


def test_replaced_inode_is_retained_on_delete(foundation, tmp_path):
    imports, recipes, draft, store, job, plan, proposed = foundation
    result, item = staged(foundation)
    path = store.root/item["id"]/CODE_PATH
    path.rename(tmp_path/"held-original"); path.write_bytes(b"# foreign replacement\n"); path.chmod(0o600)
    store.cancel_job(job["job_id"])
    with pytest.raises(PreparationError, match="ownership_unsettled"):
        store.delete(item["id"])
    assert path.exists() and store.get(item["id"])["state"] == "interrupted"


def test_synthetic_resolution_materializes_separate_bound_inputs(foundation, tmp_path):
    imports, recipes, draft, store, job, plan, proposed = foundation
    item, resolution_job, result, directory = synthetic_resolution(foundation, tmp_path)
    completed = store.materialize(item, resolution_job, result, directory, draft, lambda: None)
    finished, path = store.resolve(completed["id"], completed["result_digest"], draft)
    assert isinstance(finished.request, UnlockedCompletionRequestV1)
    assert finished.request.resolution_job_id == resolution_job["job_id"] and finished.resolution == result
    assert [ref.name for ref in finished.derived_inputs[:2]] == ["dependency-project", "dependency-lock"]
    assert len(finished.derived_inputs) == 5 and not imports.inspect(draft["draft_id"])["attachment"]
    assert item["id"] != completed["id"] and not store.workers
    assert not (Path(draft["source"]["snapshot_path"])/"uv.lock").exists()


@pytest.mark.parametrize("change", ["ownership", "cancel", "source", "project"])
def test_resolution_mismatch_never_creates_completed_input(foundation, tmp_path, change):
    imports, recipes, draft, store, job, plan, proposed = foundation
    item, resolution_job, result, directory = synthetic_resolution(foundation, tmp_path)
    if change == "ownership":
        resolution_job["ownership"] = [{"synthetic_pending": True}]
    elif change == "cancel":
        resolution_job["state"] = "cancel_requested"
    elif change == "source":
        (Path(draft["source"]["snapshot_path"])/"worker.py").write_bytes(b"# changed source\n")
    else:
        (directory/"pyproject.toml").chmod(0o600)
        (directory/"pyproject.toml").write_bytes(b"# changed project\n")
        (directory/"pyproject.toml").chmod(0o400)
    with pytest.raises(PreparationError):
        store.materialize(item, resolution_job, result, directory, draft, lambda: None)
    assert store.for_job(job["job_id"], "completed") is None and not store.workers


async def test_completed_input_writer_repeated_cancel_joins_before_recovery(foundation, tmp_path, monkeypatch):
    from meecp.preparation_agent_unlocked_resolution import AgentUnlockedResolutionStore, OwnedAgentUnlockedResolutionRunner
    from meecp.preparation_runner import _Attempt
    from meecp import preparation_agent_unlocked_store as module
    imports, recipes, draft, store, job, plan, proposed = foundation
    item, resolution_job, result, directory = synthetic_resolution(foundation, tmp_path)
    runner = OwnedAgentUnlockedResolutionRunner(AgentUnlockedResolutionStore(imports, store),tmp_path/"absent",tmp_path/"absent-reaper")
    attempt = _Attempt(resolution_job["job_id"])
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = module.original_snapshot
    def held(value, check=lambda: None):
        path = Path(value["source"]["snapshot_path"])
        if path.parent == store.root and path.name != item["id"]:
            entered.set()
            try:
                assert release.wait(3)
            finally:
                exited.set()
        return original(value, check)
    monkeypatch.setattr(module,"original_snapshot",held)
    worker = asyncio.create_task(runner._thread(attempt,store.materialize,item,resolution_job,result,directory,draft,attempt.check))
    try:
        assert await asyncio.to_thread(entered.wait,2)
        store.cancel_job(job["job_id"])
        worker.cancel(); worker.cancel()
        await asyncio.sleep(.02)
        completed = store.for_job(job["job_id"],"completed")
        assert not worker.done() and not exited.is_set() and not completed["ownership_settled"]
        with pytest.raises(PreparationError,match="ownership_unsettled"):
            store.recover(completed["id"])
    finally:
        release.set()
        await asyncio.gather(worker,return_exceptions=True)
        await runner.close()
    completed = store.for_job(job["job_id"],"completed")
    assert exited.is_set() and not attempt.threads and not attempt.callbacks and not store.workers
    assert completed["state"] == "interrupted" and not completed["ownership_settled"]
    store.recover(completed["id"]); store.delete(completed["id"])
    assert store.get(completed["id"])["state"] == "deleted" and not (store.root/completed["id"]).exists()
