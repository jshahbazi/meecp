"""Derived inputs retain original source identities and explicit authority."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import threading
import sys
from types import SimpleNamespace

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_contracts import PreparationError, contract_digest
from mcpme.preparation_inspection import inspect_source
from mcpme.preparation_resolution import ResolutionPolicyV1, ResolutionRequestV1, ResolutionStore, result_from_files
from mcpme.preparation_resolution_runner import OwnedResolutionRunner, dependency_project, discover_interface
from mcpme.preparation_components import ComponentReference
from mcpme.ownership_receipts import prepare_receipt
from mcpme.processes import OwnershipLost, ProcessOwner
from test_preparation_processes import group_reaper


def policy(discovery=False):
    return {"schema_version": 1, "operations": ["resolve_dependencies"] + (["discover_interface"] if discovery else []),
            "network": ["pypi.org", "files.pythonhosted.org"], "wall_seconds": 300,
            "disk_bytes": 2 * 1024**3, "memory_mb": 2048}


@pytest.fixture
def source(tmp_path):
    root = tmp_path.resolve()
    folder = root / "source"
    folder.mkdir()
    (folder / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n return {'value':n*2}\n")
    (folder / "LICENSE").write_text("Synthetic license.\n")
    (folder / "pyproject.toml").write_text('[project]\nname="synthetic"\nversion="1.0.0"\ndependencies=[]\n')
    with ImportStore(root / "state") as imports:
        draft = imports.create(folder, name="Synthetic")
        candidate = inspect_source(draft)["candidates"][0]
        store = ResolutionStore(imports)
        yield imports, store, draft, candidate, folder


def request(draft, candidate):
    return ResolutionRequestV1.model_validate({"schema_version": 1, "import_id": draft["draft_id"], "revision": draft["revision"],
        "source_revision": draft["source"]["revision"], "candidate_id": candidate["candidate_id"], "candidate_kind": candidate["kind"],
        "candidate_digest": contract_digest("preparation-resolution-candidate-v1", candidate), "policy": policy(),
        "runtime": {"name": "private-python-toolchain", "digest": "a" * 64},
        "producer": {"id": "python-resolution-v1", "version": "1.0.0", "digest": "b" * 64}})


def test_static_derived_project_does_not_mutate_original(source):
    imports, store, draft, candidate, folder = source
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    data = dependency_project(draft, candidate)
    assert b'mcp==1.30.0' in data
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
    assert imports.inspect(draft["draft_id"])["source"] == draft["source"]
    assert not list(store.list())


def test_authority_revision_idempotency_and_result_separation(source, tmp_path):
    imports, store, draft, candidate, folder = source
    req = request(draft, candidate)
    plan = store.plan(req, candidate, expires_in=60)
    key = str(uuid.uuid4())
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], key)
    assert store.authorize_start(plan["plan_id"], plan["plan_digest"], key) == job
    directory = tmp_path.resolve() / "result"
    directory.mkdir()
    derived = directory / "derived"
    derived.mkdir()
    (derived / "pyproject.toml").write_bytes(dependency_project(draft, candidate))
    (derived / "uv.lock").write_bytes(b'version=1\n')
    store.begin(job["job_id"], directory)
    result = result_from_files(req, derived, [], None, 1024)
    final = store.publish(job["job_id"], result)
    assert final["result_digest"] == result.digest
    read, path = store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], result.digest)
    assert read == result and path == derived
    assert not (folder / "uv.lock").exists()
    imports.revise(draft["draft_id"], 1, name="Changed")
    with pytest.raises(Exception, match="stale_import_revision"):
        store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], result.digest)
    with pytest.raises(PreparationError, match="resolution_binding_required"):
        store.resolve_result(draft["draft_id"], 2, candidate["candidate_id"], result.digest)
    # Acceptance rolls back with its caller's draft/selection transaction.
    proof = store.prepare_result_binding(draft["draft_id"], 2, candidate["candidate_id"], result.digest, prior_revision=1)
    with pytest.raises(RuntimeError):
        with imports._transaction():
            store.commit_result_binding(proof, 2)
            raise RuntimeError("synthetic rollback")
    with pytest.raises(PreparationError, match="resolution_binding_required"):
        store.resolve_result(draft["draft_id"], 2, candidate["candidate_id"], result.digest)
    accepted = store.bind_result(draft["draft_id"], 1, 2, candidate["candidate_id"], result.digest)
    assert accepted.original_request_digest == req.digest
    assert store.resolve_result(draft["draft_id"], 2, candidate["candidate_id"], result.digest)[0] == result
    assert store.get(job["job_id"])["result"] == final["result"]
    imports.revise(draft["draft_id"], 2, name="Later metadata")
    with pytest.raises(PreparationError, match="resolution_binding_required"):
        store.resolve_result(draft["draft_id"], 3, candidate["candidate_id"], result.digest)
    store.bind_result(draft["draft_id"], 2, 3, candidate["candidate_id"], result.digest)
    assert store.resolve_result(draft["draft_id"], 3, candidate["candidate_id"], result.digest)[0] == result


@pytest.mark.parametrize("damage", ["unknown", "version", "operation", "network", "bool"])
def test_resolution_policy_is_closed(damage):
    value = policy()
    if damage == "unknown": value["shell"] = True
    if damage == "version": value["schema_version"] = 2
    if damage == "operation": value["operations"].append("install")
    if damage == "network": value["network"].append("example.com")
    if damage == "bool": value["wall_seconds"] = True
    with pytest.raises(ValueError):
        ResolutionPolicyV1.model_validate(value)


@pytest.fixture
def controlled(source, monkeypatch, tmp_path, group_reaper):
    imports, store, draft, candidate, folder = source
    monkeypatch.setattr("mcpme.preparation_resolution_runner.load_private_toolchain", lambda *args: SimpleNamespace(identity="a" * 64))
    async def producer(*args):
        return ComponentReference(id="python-resolution-v1", version="1.0.0", digest="b" * 64)
    monkeypatch.setattr(OwnedResolutionRunner, "_producer", producer)
    resources = {}
    async def reserve(identity, policy): resources[identity] = policy
    async def release(identity, *, ownership_settled):
        assert ownership_settled
        resources.pop(identity, None)
    runner = OwnedResolutionRunner(store, tmp_path, group_reaper, enabled=True,
        reserve_resources=reserve, release_resources=release)
    async def resolve(attempt, toolchain, draft, candidate, executor):
        directory = attempt.workspace / "derived"
        directory.mkdir()
        (directory / "pyproject.toml").write_bytes(dependency_project(draft, candidate))
        (directory / "uv.lock").write_bytes(b'version=1\n')
        attempt.peak = 1024
        return directory
    monkeypatch.setattr(runner, "_resolve", resolve)
    return runner, draft, candidate, resources


async def start(controlled):
    runner, draft, candidate, _ = controlled
    plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
    return await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))


async def test_resolution_success_prunes_unpublished_runtime_and_rechecks_bytes(controlled):
    runner, draft, candidate, resources = controlled
    job = await start(controlled)
    await runner.tasks[job["job_id"]]
    final = runner.store.get(job["job_id"])
    assert final["state"] == "succeeded" and resources == {}
    root = Path(final["workspace"])
    assert {path.name for path in root.iterdir()} == {"derived"}
    await runner.close()
    assert runner.store.get(job["job_id"])["state"] == "succeeded"

    (root / "derived/uv.lock").chmod(0o600)
    (root / "derived/uv.lock").write_bytes(b'changed')
    with pytest.raises(PreparationError, match="resolution_result_changed"):
        runner.store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], final["result_digest"])


async def test_failed_recovery_does_not_poison_close_retry(controlled, monkeypatch):
    runner, _, _, _ = controlled
    recover, attempts = runner._recover, []
    async def flaky():
        attempts.append(True)
        if len(attempts) == 1:
            raise PreparationError("synthetic_recovery_failure")
        return await recover()
    monkeypatch.setattr(runner, "_recover", flaky)
    with pytest.raises(PreparationError, match="synthetic_recovery_failure"):
        await runner.recover()
    await runner.close()
    assert len(attempts) == 2


async def test_persistently_failed_recovery_retries_fresh_on_each_close(controlled, monkeypatch):
    runner, _, _, _ = controlled
    attempts = []
    async def fail():
        attempts.append(True)
        raise PreparationError("synthetic_recovery_failure")
    monkeypatch.setattr(runner, "_recover", fail)
    for _ in range(2):
        with pytest.raises(PreparationError, match="synthetic_recovery_failure"):
            await runner.close()
    assert len(attempts) == 2


async def test_status_recovery_cannot_interrupt_owned_authorization_before_registration(controlled, monkeypatch):
    runner, draft, candidate, resources = controlled
    plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
    entered, release = threading.Event(), threading.Event()
    original = runner.store.authorize_start
    def paused(*args):
        job = original(*args)
        entered.set()
        assert release.wait(5)
        return job
    monkeypatch.setattr(runner.store, "authorize_start", paused)
    starting = asyncio.create_task(runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4())))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        # Actual recovery/status surface remains responsive throughout the
        # committed-row / worker-registration window.
        status = await asyncio.wait_for(runner.recover(), .5)
        assert status[0]["state"] == "queued"
        assert runner.store.list()[0]["error_code"] is None
    finally:
        release.set()
    job = await starting
    await runner.tasks[job["job_id"]]
    assert runner.store.get(job["job_id"])["state"] == "succeeded"
    assert resources == {}
    await runner.close()


async def test_completed_worker_cleanup_failure_does_not_poison_recovered_close(controlled, monkeypatch):
    from mcpme import preparation_resolution_runner as module
    runner, draft, candidate, resources = controlled
    plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
    def runtime_failure(*args):
        raise PreparationError("synthetic_runtime_failure")
    monkeypatch.setattr(module, "load_private_toolchain", runtime_failure)
    original, attempts = module.shutil.rmtree, []
    def fail_once(path, *args, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError("synthetic cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(module.shutil, "rmtree", fail_once)
    job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    with pytest.raises(PermissionError):
        await runner.tasks[job["job_id"]]
    workspace = Path(runner.store.get(job["job_id"])["workspace"])
    assert workspace.exists()
    await runner.recover()
    assert not workspace.exists() and not resources
    assert runner.tasks[job["job_id"]].exception() is not None
    await runner.close()


async def test_stale_plan_cannot_start_owned_resolution(controlled):
    runner, draft, candidate, resources = controlled
    plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
    runner.store.imports.revise(draft["draft_id"], 1, name="Changed")
    with pytest.raises(Exception, match="stale_import_revision"):
        await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert runner.store.list() == [] and resources == {}
    await runner.close()


async def test_identical_results_from_independent_authorized_attempts_remain_readable(controlled):
    runner, draft, candidate, _ = controlled
    digests = []
    for _ in range(2):
        job = await start(controlled)
        await runner.tasks[job["job_id"]]
        digests.append(runner.store.get(job["job_id"])["result_digest"])
    assert digests[0] == digests[1]
    result, _ = runner.store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], digests[0])
    assert result.digest == digests[0]
    await runner.close()


async def test_cancel_joins_resolution_thread_and_publishes_nothing(controlled, monkeypatch):
    runner, _, _, resources = controlled
    entered, resume = threading.Event(), threading.Event()
    original = runner._resolve
    async def paused(attempt, *args):
        def work():
            entered.set()
            resume.wait(5)
        await runner._thread(attempt, work)
        return await original(attempt, *args)
    monkeypatch.setattr(runner, "_resolve", paused)
    job = await start(controlled)
    async with asyncio.timeout(5):
        while not entered.is_set(): await asyncio.sleep(.01)
    canceled = asyncio.create_task(runner.cancel(job["job_id"]))
    await asyncio.sleep(.02)
    canceled.cancel()
    await asyncio.sleep(.02)
    assert not canceled.done() and resources
    resume.set()
    final = await canceled
    assert final["state"] == "canceled" and final["result"] is None and resources == {}
    assert not Path(final["workspace"]).exists()
    await runner.close()
    assert runner.store.get(job["job_id"])["state"] == "canceled"


async def test_pending_native_receipt_retains_recovery_reservation(controlled):
    runner, draft, candidate, resources = controlled
    plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
    job = runner.store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    workspace = runner._workspace(job["job_id"])
    runner.store.begin(job["job_id"], workspace)
    workspace.mkdir(mode=0o700)
    descriptor, receipt = prepare_receipt(workspace, uuid.uuid4().hex)
    os.close(descriptor)
    runner.store.register_ownership(job["job_id"], receipt)
    await runner.recover()
    final = runner.store.get(job["job_id"])
    assert final["state"] == "interrupted" and final["reservation_retained"] and not final["ownership_settled"]
    assert "resolution:" + job["job_id"] in resources and workspace.exists()
    # Pending proof is intentionally retained; no guessed PID or replay.
    with pytest.raises(OwnershipLost):
        await runner.close()


async def test_real_owned_child_cancel_proves_reaping_before_release(controlled, monkeypatch):
    runner, _, _, resources = controlled
    original = runner._resolve
    async def owned_wait(attempt, toolchain, draft, candidate, executor):
        await runner._controlled(attempt, executor.execute(Path(sys.executable).resolve(),
            ["-I", "-S", "-B", "-c", "import time; time.sleep(60)"], attempt.workspace,
            {"PATH":"/usr/bin:/bin"}, timeout_seconds=60))
        return await original(attempt, toolchain, draft, candidate, executor)
    monkeypatch.setattr(runner, "_resolve", owned_wait)
    job = await start(controlled)
    async with asyncio.timeout(5):
        while not runner.attempts[job["job_id"]].owners or not runner.attempts[job["job_id"]].owners[0].instances:
            await asyncio.sleep(.01)
    owner = runner.attempts[job["job_id"]].owners[0]
    process = next(iter(owner.instances.values()))
    assert runner.store.ownership_receipts(job["job_id"]) and resources
    final = await runner.cancel(job["job_id"])
    assert process.returncode is not None and final["ownership_settled"] and not final["reservation_retained"]
    assert final["state"] == "canceled" and final["result"] is None and resources == {}
    assert runner.store.ownership_receipts(job["job_id"]) == []
    await runner.close()


@pytest.mark.parametrize("metadata", ['dependencies=["other @ https://example.com/other.whl"]',
    'dynamic=["dependencies"]', 'requires-python="<3.12"'])
def test_source_execution_or_incompatible_requirement_blocks_plan(source, metadata):
    imports, store, draft, candidate, folder = source
    (folder / "pyproject.toml").write_text('[project]\nname="synthetic"\nversion="1.0.0"\n' + metadata + '\n')
    changed = imports.create(folder, name="Unsupported")
    candidate = inspect_source(changed)["candidates"][0]
    with pytest.raises(PreparationError):
        dependency_project(changed, candidate)
    assert store.list() == []


@pytest.mark.skipif(not os.environ.get("MCPME_RESOLUTION_HOST"), reason="requires built private host and bounded public PyPI acquisition")
async def test_real_missing_lock_owned_private_resolution(source):
    imports, store, draft, candidate, folder = source
    host = Path(os.environ["MCPME_RESOLUTION_HOST"]).resolve()
    runner = OwnedResolutionRunner(store, host, host / "bin/mcpme-reap", enabled=True)
    try:
        plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        await runner.tasks[job["job_id"]]
        final = store.get(job["job_id"])
        assert final["state"] == "succeeded", final["error_code"]
        result, directory = store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], final["result_digest"])
        assert b'127.0.0.1' not in (directory / "uv.lock").read_bytes()
        assert b'https://pypi.org/simple' in (directory / "uv.lock").read_bytes()
        assert result.request.source_revision == draft["source"]["revision"]
        assert not (folder / "uv.lock").exists() and final["ownership_settled"]
    finally:
        await runner.close()


@pytest.mark.skipif(not os.environ.get("MCPME_RESOLUTION_HOST"), reason="requires built private host and bounded public PyPI acquisition")
async def test_real_foreground_sdk_dynamic_interface_resolution(source):
    imports, store, _, _, folder = source
    (folder / "pyproject.toml").write_text('[project]\nname="synthetic"\nversion="1.0.0"\ndependencies=["mcp==1.30.0"]\n'
        '[tool.mcpme]\nmcp_module="worker"\n')
    (folder / "worker.py").write_text('import asyncio\nfrom mcp.server import Server\nfrom mcp.server.models import InitializationOptions\n'
        'from mcp.server.stdio import stdio_server\nfrom mcp.types import Tool,ServerCapabilities,ToolsCapability\n'
        'server=Server("synthetic")\n@server.list_tools()\nasync def tools():\n'
        ' return [Tool(name="dynamic_echo",description="Synthetic dynamic echo",inputSchema={"type":"object","properties":{"value":{"type":"integer"}}},outputSchema={"type":"object"})]\n'
        'async def main():\n async with stdio_server() as (read,write):\n'
        '  await server.run(read,write,InitializationOptions(server_name="synthetic",server_version="1.0.0",capabilities=ServerCapabilities(tools=ToolsCapability())))\n'
        'if __name__=="__main__": asyncio.run(main())\n')
    draft = imports.create(folder, name="Dynamic MCP")
    candidate = next(item for item in inspect_source(draft)["candidates"] if item["kind"] == "python-mcp")
    host = Path(os.environ["MCPME_RESOLUTION_HOST"]).resolve()
    runner = OwnedResolutionRunner(store, host, host / "bin/mcpme-reap", enabled=True)
    try:
        plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy(True))
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        await runner.tasks[job["job_id"]]
        final = store.get(job["job_id"])
        assert final["state"] == "succeeded", final["error_code"]
        result, directory = store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], final["result_digest"])
        assert result.tools[0].name == "dynamic_echo" and result.protocol_version == "2025-11-25"
        assert {item.role for item in result.derived_inputs} == {"dependency-project", "dependency-lock", "interface"}
        assert not (folder / "uv.lock").exists()
    finally:
        await runner.close()


@pytest.mark.skipif(not os.environ.get("MCPME_RESOLUTION_HOST"), reason="requires built private host")
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
async def test_real_discovery_stop_reaps_owned_private_interpreter(tmp_path, stop):
    host = Path(os.environ["MCPME_RESOLUTION_HOST"]).resolve()
    workspace = tmp_path.resolve()
    for name in ("source", "home", "tmp"):
        (workspace / name).mkdir(mode=0o700)
    (workspace / "source/worker.py").write_text("import time\ntime.sleep(60)\n")
    owner = ProcessOwner(host / "bin/mcpme-reap")
    task = asyncio.create_task(discover_interface(owner, host / "python/bin/python3.12", host / "python",
        workspace / "source", workspace, {"declared_module":"worker", "declared_entrypoint":"worker"},
        timeout=.5 if stop == "deadline" else 60, memory_mb=512, check=lambda:None))
    try:
        if stop == "cancel":
            async with asyncio.timeout(5):
                while not owner.instances: await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            with pytest.raises(PreparationError, match="resolution_discovery_timeout"): await task
        assert all(item.returncode is not None for item in owner.instances.values())
    finally:
        await owner.close()
