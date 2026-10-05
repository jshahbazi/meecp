"""Independent admission/cancellation races and opt-in real private-host runs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from mcpme.contracts import validate_package
from mcpme.imports import ImportStore
from mcpme.ownership_receipts import prepare_receipt, verify_receipt
from mcpme.preparation_contracts import ExecutionPolicy, PreparationError, parse_descriptor
from mcpme.preparation_processes import PreparationExecutor
from mcpme.preparation_runner import JournaledPreparationOwner, PreparationRunner, _workspace_size, implementation_digest
from mcpme.preparation_verifier import VerificationReport, verify_prepared_package
from mcpme.preparations import PreparationStore, RECEIPT_NAME
from mcpme.processes import OwnershipLost, ProcessOwner
from mcpme.recipe_sdk import scaffold_recipe
from mcpme.recipes import RecipeStore
from test_mcpme_packages import edit_manifest, make_package
from test_mcpme_preparations import foundation
from test_preparation_processes import group_reaper
from test_preparation_verifier import probe_fixture


async def finished(runner, job_id):
    async with asyncio.timeout(15):
        await runner.tasks[job_id]
    return runner.store.get(job_id)


@pytest.fixture
def controlled(foundation, monkeypatch, tmp_path, group_reaper):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    imports, recipes, store, draft, raw = foundation
    resources = {}
    released = []
    async def reserve(identifier, policy):
        resources[identifier] = policy
    async def release(identifier, *, ownership_settled):
        assert ownership_settled
        resources.pop(identifier, None)
        released.append(identifier)
    toolchain = SimpleNamespace(identity="d" * 64, uv=tmp_path / "uv")
    monkeypatch.setattr("mcpme.preparation_runner.load_private_toolchain", lambda *args: toolchain)
    def build(descriptor, draft, toolchain, output, run, check, *, acquire_wheels):
        check()
        package = make_package(output)
        edit_manifest(package, **store._candidate_contract(descriptor, draft["source"]["revision"]))
        return validate_package(package)
    monkeypatch.setattr("mcpme.preparation_runner.prepare_bound", build)
    async def verify(root, descriptor, reaper, workspace, **kwargs):
        package = validate_package(root)
        # Verification must see the locked receipt, never a later injected one.
        assert any(artifact.path == RECEIPT_NAME for artifact in package.lock.artifacts)
        return VerificationReport(package.digest, len(descriptor.interface.tools), len(descriptor.validation), "2025-11-25", 4096)
    monkeypatch.setattr("mcpme.preparation_runner.verify_prepared_package", verify)
    runner = PreparationRunner(store, tmp_path / "host", group_reaper, enabled=True,
                               reserve_resources=reserve, release_resources=release)
    return runner, imports, recipes, draft, raw, resources, released


async def planned(controlled, *, policy=None, expires_in=300):
    runner, _, _, draft, raw, *_ = controlled
    return await runner.plan(draft["draft_id"], draft["revision"], raw, policy, expires_in=expires_in)


async def started(controlled, **kwargs):
    plan = await planned(controlled, **kwargs)
    return await controlled[0].start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))


async def test_public_gate_never_starts_or_plans(foundation, tmp_path):
    _, _, store, draft, raw = foundation
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper")
    with pytest.raises(PreparationError, match="preparation_runner_unavailable"):
        await runner.plan(draft["draft_id"], 1, raw)
    with pytest.raises(PreparationError, match="preparation_runner_unavailable"):
        await runner.start(str(uuid.uuid4()), "a" * 64, str(uuid.uuid4()))
    assert store.list() == []
    await runner.close()


async def test_idempotency_real_atomic_attachment_and_postsuccess_cancel(controlled):
    runner, imports, _, draft, _, resources, released = controlled
    plan = await planned(controlled)
    key = str(uuid.uuid4())
    first = await runner.start(plan["plan_id"], plan["plan_digest"], key)
    second = await runner.start(plan["plan_id"], plan["plan_digest"], key)
    assert first["job_id"] == second["job_id"] and first["state"] == "queued" and len(runner.tasks) == 1
    job = await finished(runner, first["job_id"])
    assert job["state"] == "succeeded" and job["ownership_settled"] and not job["reservation_retained"]
    assert job["verification"]["fixtures_checked"] == 1
    assert job["progress"]["files_checked"] == 4 and job["progress"]["fixtures_checked"] == 1
    current = imports.inspect(draft["draft_id"])
    assert current["revision"] == 1 and current["attachment"]["digest"] == job["expected_package_digest"]
    assert (await runner.cancel(job["job_id"]))["state"] == "succeeded"
    assert not resources and released == [job["job_id"]]
    assert not Path(job["workspace"]).exists() and not [path for path in imports.attachment_staging.iterdir() if path.name != ".lock"]
    await runner.close()
    await runner.close()


async def test_independent_report_mismatch_never_attaches(controlled, monkeypatch):
    runner, imports, _, draft, _, resources, _ = controlled
    async def wrong(*args, **kwargs):
        return VerificationReport("e" * 64, 1, 1, "2025-11-25", 4096)
    monkeypatch.setattr("mcpme.preparation_runner.verify_prepared_package", wrong)
    job = await started(controlled)
    final = await finished(runner, job["job_id"])
    assert final["state"] == "failed" and final["error_code"] == "preparation_result_mismatch"
    assert imports.inspect(draft["draft_id"])["attachment"] is None and not resources
    await runner.close()


async def test_cancel_joins_copy_thread_and_cleans_eventual_stage(controlled, monkeypatch):
    runner, imports, _, draft, _, resources, _ = controlled
    entered, resume = threading.Event(), threading.Event()
    original = imports.stage_attachment
    def paused(*args, **kwargs):
        entered.set()
        resume.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(imports, "stage_attachment", paused)
    job = await started(controlled)
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    canceled = asyncio.create_task(runner.cancel(job["job_id"]))
    await asyncio.sleep(0.02)
    canceled.cancel()
    await asyncio.sleep(0.02)
    assert not canceled.done() and resources
    resume.set()
    result = await canceled
    assert result["state"] == "canceled" and not resources
    assert imports.inspect(draft["draft_id"])["attachment"] is None
    assert not [path for path in imports.attachment_staging.iterdir() if path.name != ".lock"] and not Path(result["workspace"]).exists()
    await runner.close()


@pytest.mark.parametrize("step", ["record_verified", "admit"])
async def test_cancel_during_publication_or_admission_hashes_retains_ownership(controlled, monkeypatch, step):
    runner, imports, _, draft, _, resources, _ = controlled
    entered, resume = threading.Event(), threading.Event()
    original = getattr(runner.store, step)
    def paused(*args, **kwargs):
        entered.set()
        resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(runner.store, step, paused)
    job = await started(controlled)
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    current = runner.store.get(job["job_id"])
    assert current["state"] == ("verifying" if step == "record_verified" else "attaching")
    assert not current["ownership_settled"] and current["reservation_retained"]
    # The event loop remains available while full package hashes are paused.
    canceled = asyncio.create_task(runner.cancel(job["job_id"]))
    await asyncio.sleep(0.03)
    assert not canceled.done() and resources and runner.store.get(job["job_id"])["state"] == "cancel_requested"
    resume.set()
    result = await canceled
    assert result["state"] == "canceled" and result["ownership_settled"] and not resources
    assert imports.inspect(draft["draft_id"])["attachment"] is None
    assert not [path for path in imports.attachment_staging.iterdir() if path.name != ".lock"]
    await runner.close()


async def test_cancel_builder_thread_checks_and_settles(controlled, monkeypatch):
    runner, _, _, _, _, resources, _ = controlled
    entered = threading.Event()
    def waiting(*args, **kwargs):
        check = args[5]
        entered.set()
        while True:
            check()
            time.sleep(0.005)
    monkeypatch.setattr("mcpme.preparation_runner.prepare_bound", waiting)
    job = await started(controlled)
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    result = await runner.cancel(job["job_id"])
    assert result["state"] == "canceled" and not resources
    assert not runner.attempts[job["job_id"]].threads and runner.tasks[job["job_id"]].done()
    await runner.close()


async def test_recovery_does_not_interrupt_current_worker(controlled, monkeypatch):
    runner, *_ = controlled
    entered, resume = threading.Event(), threading.Event()
    original = __import__("mcpme.preparation_runner", fromlist=["prepare_bound"]).prepare_bound
    def paused(*args, **kwargs):
        entered.set()
        resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr("mcpme.preparation_runner.prepare_bound", paused)
    job = await started(controlled)
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    await runner.recover()
    await runner.recover()
    assert runner.store.get(job["job_id"])["state"] == "preparing" and not runner.tasks[job["job_id"]].done()
    resume.set()
    assert (await finished(runner, job["job_id"]))["state"] == "succeeded"
    await runner.close()


async def test_request_cancel_during_admission_still_tracks_committed_job(controlled, monkeypatch):
    runner, *_ = controlled
    plan = await planned(controlled)
    entered, resume = threading.Event(), threading.Event()
    original = runner.store.authorize_start
    def paused(*args):
        entered.set()
        resume.wait(5)
        return original(*args)
    monkeypatch.setattr(runner.store, "authorize_start", paused)
    task = asyncio.create_task(runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4())))
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(runner.tasks) == len(runner.store.list()) == 1 and not runner._admissions
    await runner.close()
    assert runner.store.list()[0]["ownership_settled"] and not controlled[-2]


async def test_fast_builder_disk_usage_is_checked_before_probe(controlled, monkeypatch):
    runner, imports, _, draft, raw, *_ = controlled
    verified = False
    async def forbidden(*args, **kwargs):
        nonlocal verified
        verified = True
        pytest.fail("over-budget build was probed")
    monkeypatch.setattr("mcpme.preparation_runner.verify_prepared_package", forbidden)
    policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], wall_seconds=120,
                             disk_bytes=100, memory_mb=128)
    job = await started(controlled, policy=policy)
    result = await finished(runner, job["job_id"])
    assert result["state"] == "failed" and result["error_code"] == "preparation_disk_limit" and not verified
    assert imports.inspect(draft["draft_id"])["attachment"] is None and not controlled[-2]
    await runner.close()


@pytest.mark.parametrize("invalidation,code", [("revoke", "recipe_revoked"), ("revise", "stale_import_revision")])
async def test_monitor_cancels_running_probe_on_input_invalidation(controlled, monkeypatch, invalidation, code):
    runner, imports, recipes, draft, raw, resources, _ = controlled
    entered = asyncio.Event()
    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr("mcpme.preparation_runner.verify_prepared_package", waiting)
    job = await started(controlled)
    await entered.wait()
    if invalidation == "revoke":
        recipes.revoke(raw["recipe"]["id"], raw["recipe"]["version"])
    else:
        imports.revise(draft["draft_id"], 1, name="Changed synthetic task")
    result = await finished(runner, job["job_id"])
    assert result["state"] == "failed" and result["error_code"] == code
    assert result["ownership_settled"] and not result["reservation_retained"] and not resources
    assert imports.inspect(draft["draft_id"])["attachment"] is None
    await runner.close()


async def test_whole_job_deadline_stops_copy_worker(controlled, monkeypatch):
    runner, *_ = controlled
    def waiting(*args, **kwargs):
        while True:
            args[5]()
            time.sleep(0.005)
    monkeypatch.setattr("mcpme.preparation_runner.prepare_bound", waiting)
    policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], wall_seconds=1,
                             disk_bytes=1024**3, memory_mb=128)
    job = await started(controlled, policy=policy)
    result = await finished(runner, job["job_id"])
    assert result["state"] == "failed" and result["error_code"] == "preparation_wall_limit"
    assert not runner.attempts[job["job_id"]].threads
    await runner.close()


async def test_expiry_after_resource_wait_releases_reservation(controlled, monkeypatch):
    runner, *_ = controlled
    entered, resume = asyncio.Event(), asyncio.Event()
    reserve = runner.reserve_resources
    async def waiting(identifier, policy):
        await reserve(identifier, policy)
        entered.set()
        await resume.wait()
    runner.reserve_resources = waiting
    job = await started(controlled, expires_in=1)
    await entered.wait()
    monkeypatch.setattr("mcpme.preparation_authority.time.time", lambda: job["authority_expires_at"] + 1)
    resume.set()
    result = await finished(runner, job["job_id"])
    assert result["state"] == "failed" and result["error_code"] == "preparation_authority_expired"
    assert not controlled[-2]
    await runner.close()


async def test_actual_implementation_changed_after_review(controlled, monkeypatch):
    runner, *_ = controlled
    plan = await planned(controlled)
    monkeypatch.setattr("mcpme.preparation_runner.implementation_digest", lambda *args, **kwargs: "b" * 64)
    job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    result = await finished(runner, job["job_id"])
    assert result["state"] == "failed" and result["error_code"] == "preparation_implementation_changed"
    await runner.close()


def test_actual_native_reaper_bytes_change_both_implementation_identities(group_reaper, tmp_path):
    reaper = tmp_path / "reaper"
    shutil.copyfile(group_reaper, reaper)
    reaper.chmod(0o700)
    first = [implementation_digest(domain, ("preparation_runner.py",), reaper=reaper, dependencies="c" * 64)
             for domain in ("runner-v1", "verifier-v1")]
    with reaper.open("ab") as output:
        output.write(b"changed-native-proof")
    second = [implementation_digest(domain, ("preparation_runner.py",), reaper=reaper, dependencies="c" * 64)
              for domain in ("runner-v1", "verifier-v1")]
    assert all(before != after for before, after in zip(first, second))


async def test_source_snapshot_changed_after_review(controlled):
    runner, imports, _, draft, *_ = controlled
    plan = await planned(controlled)
    snapshot = Path(draft["source"]["snapshot_path"])
    (snapshot / "validator.py").write_text("def replacement(): pass\n")
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert runner.store.list() == [] and imports.inspect(draft["draft_id"])["attachment"] is None
    await runner.close()


def pending_job(controlled):
    runner, _, _, draft, raw, *_ = controlled
    policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], wall_seconds=120,
                             disk_bytes=1024**3, memory_mb=128)
    plan = runner.store.execution_plan(draft["draft_id"], 1, raw, policy, runner_digest="a" * 64,
                                      verifier_digest="b" * 64, runtime_digest="d" * 64)
    job = runner.store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    runner.store.transition(job["job_id"], "snapshotting")
    workspace = runner.store.imports.root / "preparations" / job["job_id"]
    workspace.mkdir(mode=0o700, parents=True)
    workspace.parent.chmod(0o700)
    runner.store.set_workspace(job["job_id"], workspace)
    runner.store.transition(job["job_id"], "preparing")
    return runner.store.get(job["job_id"]), workspace


async def test_restart_unknown_or_mismatched_receipt_keeps_reservation(controlled):
    runner, *_ = controlled
    job, workspace = pending_job(controlled)
    descriptor, metadata = prepare_receipt(workspace, uuid.uuid4().hex)
    runner.store.register_ownership(job["job_id"], metadata)
    # A complete-looking frame for a different nonce cannot settle this job.
    os.pwrite(descriptor, b"0" * 64 + b"C", 40)
    os.fsync(descriptor)
    os.close(descriptor)
    await runner.recover()
    result = runner.store.get(job["job_id"])
    assert result["state"] == "interrupted" and result["reservation_retained"] and not result["ownership_settled"]
    assert not runner.tasks and controlled[-2]
    with pytest.raises(OwnershipLost):
        await runner.close()
    with pytest.raises(OwnershipLost):
        await runner.close()
    assert workspace.exists()


async def test_restart_only_native_completed_receipt_releases(controlled, group_reaper):
    runner, *_ = controlled
    job, workspace = pending_job(controlled)
    owner = JournaledPreparationOwner(group_reaper, runner.store, job["job_id"], workspace)
    executor = PreparationExecutor(group_reaper, owner=owner)
    result = await executor.execute(Path(sys.executable), ["-c", "print('synthetic')"], workspace, {}, timeout_seconds=5)
    assert result.stdout == b"synthetic\n" and not owner.instances
    metadata = runner.store.ownership_receipts(job["job_id"])
    assert len(metadata) == 1 and verify_receipt(workspace, metadata[0])
    await executor.close()
    # Simulate host reconstruction without calling owner.confirm_completed.
    await runner.recover()
    result = runner.store.get(job["job_id"])
    assert result["state"] == "interrupted" and result["ownership_settled"] and not result["reservation_retained"]
    assert not runner.store.ownership_receipts(job["job_id"]) and not workspace.exists() and not runner.tasks
    await runner.close()


@pytest.mark.parametrize("reconciliation", ["recover", "close"])
async def test_late_native_completion_can_be_reconciled_without_restarting(controlled, group_reaper, reconciliation):
    runner, *_ = controlled
    job, workspace = pending_job(controlled)
    owner = JournaledPreparationOwner(group_reaper, runner.store, job["job_id"], workspace)
    process = await owner.launch(Path(sys.executable), ["-c", "import time; time.sleep(.2)"], workspace, {},
                                 owned_group=True, stop_grace=0.1)
    await runner.recover()
    assert runner.store.get(job["job_id"])["reservation_retained"]
    if reconciliation == "close":
        with pytest.raises(OwnershipLost):
            await runner.close()
    await process.wait()
    await owner.close()
    if reconciliation == "close":
        await runner.close()
    else:
        await runner.recover()
    result = runner.store.get(job["job_id"])
    assert result["state"] == "interrupted" and result["ownership_settled"] and not result["reservation_retained"]
    assert not owner.instances and not workspace.exists() and not controlled[-2]
    await runner.close()


async def test_proven_reaper_nonlaunch_receipt_releases_safely(controlled):
    runner, *_ = controlled
    job, workspace = pending_job(controlled)
    owner = JournaledPreparationOwner(workspace / "missing-reaper", runner.store, job["job_id"], workspace)
    executor = PreparationExecutor(workspace / "missing-reaper", owner=owner)
    with pytest.raises(PreparationError, match="preparation_step_launch_failed"):
        await executor.execute(Path(sys.executable), ["-c", "pass"], workspace, {}, timeout_seconds=5)
    metadata = runner.store.ownership_receipts(job["job_id"])
    assert len(metadata) == 1 and verify_receipt(workspace, metadata[0])
    assert (workspace / "ownership-receipts" / (metadata[0]["instance_id"] + ".receipt")).read_bytes()[-1:] == b"N"
    assert not owner.instances and not owner._launches and not owner._pending_ids
    await runner.recover()
    assert runner.store.get(job["job_id"])["ownership_settled"] and not runner.store.get(job["job_id"])["reservation_retained"]
    await runner.close()


def test_disk_scan_rejects_external_alias_and_actual_budget(tmp_path):
    (tmp_path / "data").write_bytes(b"a" * 9)
    with pytest.raises(PreparationError, match="preparation_disk_limit"):
        _workspace_size(tmp_path, 8, threading.Event())
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(PreparationError, match="unsafe_preparation_workspace"):
        _workspace_size(tmp_path, 1024, threading.Event())


@pytest.mark.parametrize("boundary", ["initial", "post_probe"])
async def test_verifier_cancellation_joins_hash_thread_before_return(group_reaper, tmp_path, monkeypatch, boundary):
    package, descriptor = probe_fixture(tmp_path)
    owner = ProcessOwner(group_reaper)
    entered, resume = threading.Event(), threading.Event()
    calls = 0
    def paused(root):
        nonlocal calls
        calls += 1
        if calls == (1 if boundary == "initial" else 2):
            entered.set()
            resume.wait(5)
        return validate_package(root)
    monkeypatch.setattr("mcpme.preparation_verifier.validate_package", paused)
    task = asyncio.create_task(verify_prepared_package(package, descriptor, group_reaper, tmp_path, owner=owner))
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done() and not owner.instances
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owner.instances and not owner._launches and not owner._pending_ids
    await owner.close()


@pytest.mark.parametrize("kind", ["python-callable", "python-mcp", "cancel-python-callable", "revoke-python-callable", "unsupported-fastmcp"])
async def test_actual_private_host_durable_attachment(tmp_path, kind, monkeypatch):
    """Development parent runs current code; all actual child tools come from host.

    Set MCPME_TEST_PREPARATION_HOST to an absolute complete built CLI host. The
    test intentionally opts into the internal runner while public defaults stay
    false. No global uv/Python fallback or developer subprocess callback exists.
    """
    selected = os.environ.get("MCPME_TEST_PREPARATION_HOST")
    if not selected:
        pytest.skip("opt-in complete built private host required")
    host = Path(selected)
    assert host.is_absolute() and host.resolve() == host
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    cancel_probe = kind == "cancel-python-callable"
    revoke_probe = kind == "revoke-python-callable"
    reject_capabilities = kind == "unsupported-fastmcp"
    kind = "python-callable" if cancel_probe or revoke_probe else "python-mcp" if reject_capabilities else kind
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(host / name, source / name)
    shutil.copyfile(host / "templates/LICENSE", source / "LICENSE")
    if kind == "python-callable":
        code = ("import time\ndef double(n: int) -> dict[str, int]:\n    time.sleep(100)\n    return {'value': n * 2}\n"
                if cancel_probe or revoke_probe else "def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n")
    elif reject_capabilities:
        code = ("from mcp.server.fastmcp import FastMCP\napp = FastMCP('Synthetic FastMCP')\n"
                "@app.tool(description='Double a synthetic integer')\n"
                "def double(n: int) -> dict[str, int]:\n    return {'value': n*2}\n"
                "if __name__ == '__main__':\n    app.run(transport='stdio')\n")
    else:
        code = ("import anyio\n"
                "from mcp.server import Server\nfrom mcp.server.stdio import stdio_server\n"
                "from mcp.types import Tool, CallToolResult, TextContent\n"
                "app = Server('Synthetic foreground MCP', version='1.0.0')\n"
                "@app.list_tools()\nasync def listing():\n"
                "    return [Tool(name='double', description='Double a synthetic integer', "
                "inputSchema={'type':'object','properties':{'n':{'type':'integer'}},'required':['n'],'additionalProperties':False}, "
                "outputSchema={'type':'object','properties':{'value':{'type':'integer'}},'required':['value'],'additionalProperties':False})]\n"
                "@app.call_tool(validate_input=False)\nasync def double(name, arguments):\n"
                "    return CallToolResult(content=[TextContent(type='text',text='synthetic')], structuredContent={'value':arguments['n']*2})\n"
                "async def serve():\n    async with stdio_server() as (incoming,outgoing):\n"
                "        options = app.create_initialization_options()\n"
                "        options.capabilities.experimental = None\n"
                "        await app.run(incoming,outgoing,options)\n"
                "if __name__ == '__main__':\n    anyio.run(serve)\n")
    (source / "tool.py").write_text(code)
    imports = ImportStore(tmp_path / "state")
    runner = None
    try:
        draft = imports.create(source, name="Synthetic durable preparation", intent="Expose a synthetic integer transformation")
        recipes = RecipeStore(imports)
        scaffold_recipe(tmp_path / "recipe", "test.durable", component="python-function-v1" if kind == "python-callable" else "existing-mcp-v1")
        recipe = recipes.add(tmp_path / "recipe")
        recipe = recipes.enable(recipe["recipe_id"], recipe["version"])
        tools = [{"name": "double", "description": "Double a synthetic integer",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}}]
        interface = {"kind": kind, "module": "tool", "tools": tools}
        if kind == "python-callable":
            interface["function"] = "double"
        raw = {"schema_version": 1, "recipe": {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
            "package": {"id": "test.durable-double", "version": "1.0.0", "name": "Synthetic Durable Double"},
            "interface": interface, "dependencies": {"project": ".", "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(),
                "python_version": "3.12.13", "mcp_version": "1.30.0"}, "source_license": "LICENSE",
            "inputs": {field["field"]: field["default"] for field in recipe["manifest"]["form"] if "default" in field},
            "resources": {"memory_mb": 128},
            "validation": [{"id": "synthetic", "tool": "double", "arguments": {"n": 3}, "expected": {"value": 6}, "synthetic": True}]}
        store = PreparationStore(imports, recipes)
        assert store.plan(draft["draft_id"], 1, raw)["execution_available"] is False
        runner = PreparationRunner(store, host, host / "bin/mcpme-reap", enabled=True)
        plan = await runner.plan(draft["draft_id"], 1, raw)
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        if cancel_probe or revoke_probe:
            async with asyncio.timeout(60):
                while (store.get(job["job_id"])["state"] != "verifying" or
                       len(runner.attempts[job["job_id"]].owners) < 2 or
                       not runner.attempts[job["job_id"]].owners[-1].instances):
                    assert not runner.tasks[job["job_id"]].done()
                    await asyncio.sleep(0.01)
            if revoke_probe:
                recipes.revoke(recipe["recipe_id"], recipe["version"])
                result = await finished(runner, job["job_id"])
                assert result["state"] == "failed" and result["error_code"] == "recipe_revoked"
            else:
                result = await runner.cancel(job["job_id"])
                assert result["state"] == "canceled"
            assert result["ownership_settled"] and not result["reservation_retained"]
            assert not store.ownership_receipts(job["job_id"]) and not imports.inspect(draft["draft_id"])["attachment"]
            assert all(not owner.instances and not owner._launches and not owner._pending_ids
                       for owner in runner.attempts[job["job_id"]].owners)
            assert not Path(result["workspace"]).exists()
            return
        async with asyncio.timeout(180):
            await runner.tasks[job["job_id"]]
        final = store.get(job["job_id"])
        if reject_capabilities:
            assert final["state"] == "failed" and final["error_code"] == "unsupported_preparation_probe_capabilities"
            assert final["ownership_settled"] and not final["reservation_retained"] and not store.ownership_receipts(job["job_id"])
            assert not imports.inspect(draft["draft_id"])["attachment"] and not Path(final["workspace"]).exists()
            assert all(not owner.instances and not owner._launches and not owner._pending_ids
                       for owner in runner.attempts[job["job_id"]].owners)
            return
        assert final["state"] == "succeeded", final["error_code"]
        assert final["progress"]["commands_finished"] == 5 and final["progress"]["wheels_acquired"] >= 1
        assert final["verification"]["fixtures_checked"] == 1 and not store.ownership_receipts(job["job_id"])
        assert all(not owner.instances and not owner._launches and not owner._pending_ids for owner in runner.attempts[job["job_id"]].owners)
        attached = imports.inspect(draft["draft_id"])["attachment"]
        shutil.rmtree(source)
        assert not Path(final["workspace"]).exists()
        assert validate_package(Path(attached["path"])).digest == final["expected_package_digest"]
    finally:
        if runner is not None:
            await runner.close()
        imports.close()
