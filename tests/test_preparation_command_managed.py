"""Real intake/forms/SQLite/native probe; missing resource evidence blocks attach.

These tests require the shared application-directory contract. They deliberately
do not substitute a positive memory measurement or a fake installed slot.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import time
import threading
from functools import partial
import uuid

import pytest

from mcpme.host_command import HostCommandAdmission
from mcpme.imports import ImportStore
from mcpme.preparation_command import CommandFamily, component_catalog, write_command_recipe
from mcpme.preparation_command_owner import CommandPreparationOwner
from mcpme.preparation_command_verifier import verify_command_package
from mcpme.preparation_contracts import ExecutionPolicy, PreparationError, canonical_bytes
from mcpme.preparation_forms import PreparationForms
from mcpme.preparation_runner import seal_receipt
from mcpme.preparations import PreparationStore
from mcpme.recipes import RecipeStore
from test_host_command import native, digest, pytestmark
from test_preparation_command import SCHEMA


def intake(tmp_path, native, mode="echo", *, create_owner=True, prepare_job=True):
    original = tmp_path / "original"
    original.mkdir()
    shutil.copyfile(native[2], original / "command")
    (original / "command").chmod(0o700)
    for name in ("command.input.schema.json", "command.output.schema.json"):
        (original / name).write_bytes(canonical_bytes(SCHEMA))
    (original / "LICENSE").write_text("Synthetic repository fixture notice.\n")
    imports = ImportStore(tmp_path / "state")
    family = CommandFamily(digest(native[1]))
    recipes = RecipeStore(imports, component_catalog=component_catalog())
    write_command_recipe(tmp_path / "recipe")
    record = recipes.add(tmp_path / "recipe")
    recipes.enable(record["recipe_id"], record["version"])
    store = PreparationStore(imports, recipes, families=(family,))
    draft = imports.create(original, name="Native JSON command", intent="Expose the supplied JSON operation", source_mode="automatic")
    assert draft["source"]["kind"] == "application_directory"
    assert not (Path(draft["source"]["snapshot_path"]) / "command").stat().st_mode & 0o111
    shutil.rmtree(original)
    forms = PreparationForms(store)
    form = forms.form(draft["draft_id"], draft["revision"], execution_available=True)
    assert not form["blockers"]
    answers = {}
    for field in form["fields"]:
        if field["kind"] == "schema-preview":
            continue
        answer = field["value"]
        if field["choices"]:
            answer = field["choices"][0]["id"]
        if field["label"] in {"Synthetic test input", "Expected synthetic result"}:
            answer = {"ok": True}
        if field["label"] == "Fixed command arguments":
            answer = {"args": [mode]}
        answers[field["id"]] = answer
    descriptor, _ = forms.resolve(draft["draft_id"], draft["revision"], candidate_id=form["candidate_id"],
        recipe=form["recipe"], form_digest=form["form_digest"], answers=answers, execution_available=True)
    draft = store.select_descriptor(draft["draft_id"], draft["revision"], descriptor.model_dump(exclude_none=True),
        candidate_id=form["candidate_id"], form_digest=form["form_digest"])
    if not prepare_job:
        return imports, store, draft, descriptor
    policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], wall_seconds=30,
        disk_bytes=128 * 1024**2, memory_mb=256)
    plan = store.execution_plan(draft["draft_id"], draft["revision"], descriptor.model_dump(exclude_none=True), policy,
        runner_digest="a" * 64, verifier_digest="b" * 64, runtime_digest="c" * 64)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    identifier = job["job_id"]
    store.transition(identifier, "snapshotting")
    workspace = imports.root / "preparations" / identifier
    workspace.mkdir(mode=0o700, parents=True)
    workspace.parent.chmod(0o700)
    store.set_workspace(identifier, workspace)
    store.transition(identifier, "preparing")
    package = family.prepare(descriptor, draft, None, workspace / "candidate", None, lambda: None, acquire_wheels=None, job_id=identifier)
    package = seal_receipt(package, store.get(identifier))
    store.transition(identifier, "verifying")
    factory = partial(CommandPreparationOwner, HostCommandAdmission(native[1], digest(native[1])), store, identifier, workspace,
        package, descriptor, attempt_id=uuid.uuid4().hex, check=lambda: None, deadline=time.monotonic() + 10)
    return imports, store, factory() if create_owner else factory


@pytest.mark.asyncio
async def test_actual_directory_questions_copy_sdk_probe_stays_blocked_without_memory_evidence(tmp_path, native):
    imports, store, owner = intake(tmp_path, native, "tree")
    try:
        report = await verify_command_package(owner.package.root, owner.descriptor, None, owner.workspace,
            timeout_seconds=5, owner=owner)
        assert report.fixtures_checked == 1 and report.peak_memory_bytes == 0
        assert not store.pending_ownership(owner.job_id)
        store.settle(owner.job_id)
        store.begin_verification_publication(owner.job_id)
        with pytest.raises(PreparationError, match="independent_verification_required"):
            store.record_verified(owner.job_id, owner.package, verification=report)
        job = store.get(owner.job_id)
        assert job["verification"] is None and job["state"] == "verifying"
        assert imports.inspect(job["binding"]["import_id"])["attachment"] is None
        store.settle(owner.job_id)
        store.transition(owner.job_id, "failed", ownership_settled=True, error_code="independent_verification_required")
    finally:
        await owner.close()
        imports.close()


async def wait_entered(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["cancel", "revision"])
async def test_registration_hash_worker_holds_no_sql_lock_and_rechecks_winner(tmp_path, native, monkeypatch, winner):
    from mcpme import contracts
    imports, store, factory = intake(tmp_path, native, create_owner=False)
    job_id = factory.args[2]
    entered, release = threading.Event(), threading.Event()
    original = contracts.validate_package
    observations = []

    def paused(*args, **kwargs):
        observations.append((threading.current_thread() is not threading.main_thread(), not store.db.in_transaction))
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(contracts, "validate_package", paused)
    task = asyncio.create_task(asyncio.to_thread(factory))
    try:
        await wait_entered(entered)
        # A different worker must acquire the SQLite lock while hashing waits;
        # the manager loop also keeps ticking for a concrete 200 ms interval.
        job = await asyncio.wait_for(asyncio.to_thread(store.get, job_id), .2)
        ticks = 0
        stop = time.monotonic() + .2
        while time.monotonic() < stop:
            await asyncio.sleep(.01)
            ticks += 1
        assert ticks >= 10
        if winner == "cancel":
            store.cancel(job_id)
        else:
            imports.revise(job["binding"]["import_id"], job["binding"]["revision"], name="Revised")
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, Exception)
        assert observations == [(True, True)]
        assert not store.command_ownership(job_id)
        from mcpme.preparation_command_owner import recover_command_ownership
        from mcpme.private import read_private_json
        workspace = Path(job["workspace"])
        metadata = read_private_json(workspace / "command-probe-v3/owner.json")
        assert recover_command_ownership(workspace, metadata)  # constructor released its held lock
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["session", "invoke", "response"])
@pytest.mark.parametrize("winner", ["cancel", "revision"])
async def test_publication_hash_worker_rechecks_authority_before_launch_or_write(tmp_path, native, monkeypatch, boundary, winner):
    from mcpme import preparation_command_owner as module
    imports, store, owner = intake(tmp_path, native)
    handle = await owner.session(owner.package) if boundary != "session" else None
    entered, release = threading.Event(), threading.Event()
    original = module.validate_package

    def paused(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "validate_package", paused)
    operation = (owner.session(owner.package) if boundary == "session" else
        owner.invoke(handle, "run", {"ok": True}, 1) if boundary == "invoke" else
        handle.send({"jsonrpc": "2.0", "id": 1, "result": {}}))
    task = asyncio.create_task(operation)
    try:
        await wait_entered(entered)
        job = await asyncio.wait_for(asyncio.to_thread(store.get, owner.job_id), .2)
        await asyncio.sleep(.2)
        assert not task.done()
        if winner == "cancel":
            store.cancel(owner.job_id)
        else:
            imports.revise(job["binding"]["import_id"], job["binding"]["revision"], name="Revised")
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, Exception)
        assert not owner.executor.journal["entries"]
        if handle is not None:
            import os
            os.set_blocking(handle.stdout_fd, False)
            with pytest.raises(BlockingIOError):
                os.read(handle.stdout_fd, 1)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await owner.close()
        assert owner.confirm_completed()
        imports.close()


@pytest.mark.asyncio
async def test_actual_managed_command_cancel_requires_native_settlement(tmp_path, native):
    imports, store, owner = intake(tmp_path, native, "hang")
    try:
        handle = await owner.session(owner.package)
        task = asyncio.create_task(owner.invoke(handle, "run", {"ok": True}, 1))
        while not owner.executor._native:
            await asyncio.sleep(.001)
        store.cancel(owner.job_id)
        assert store.get(owner.job_id)["reservation_retained"]
        with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
            store.settle(owner.job_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await owner.close()
        assert owner.confirm_completed()
        job = store.settle(owner.job_id)
        assert job["state"] == "canceled" and not job["reservation_retained"]
    finally:
        await owner.close()
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_construction", [False, True])
async def test_runner_owns_real_command_constructor_and_settles_without_attachment(tmp_path, native, monkeypatch, cancel_during_construction):
    from types import SimpleNamespace
    from mcpme.preparation_runner import PreparationRunner
    imports, store, draft, descriptor = intake(tmp_path, native, "tree", prepare_job=False)
    # Only distribution discovery is controlled here; intake, forms, bindings,
    # builder, native executor, SDK verifier, SQLite and cleanup are actual.
    monkeypatch.setattr("mcpme.preparation_runner.load_private_toolchain", lambda *args: SimpleNamespace(identity="d" * 64))
    monkeypatch.setattr("mcpme.preparation_runner.dependency_identity", lambda *args: "e" * 64)
    runner = PreparationRunner(store, tmp_path / "host", native[0], enabled=True,
        enabled_families=tuple(store.families), command_admission=HostCommandAdmission(native[1], digest(native[1])))
    entered, release = threading.Event(), threading.Event()
    original = store.register_command_ownership

    def registered(*args):
        original(*args)
        if cancel_during_construction:
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(store, "register_command_ownership", registered)
    canceled = None
    try:
        plan = await runner.plan(draft["draft_id"], draft["revision"], descriptor.model_dump(exclude_none=True))
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        identifier = job["job_id"]
        if cancel_during_construction:
            await wait_entered(entered)
            assert store.command_ownership(identifier)
            canceled = asyncio.create_task(runner.cancel(identifier))
            await asyncio.sleep(.2)
            assert not canceled.done() and store.get(identifier)["reservation_retained"]
            release.set()
            await canceled
        await asyncio.wait_for(asyncio.shield(runner.tasks[identifier]), 10)
        final = store.get(identifier)
        assert final["state"] == ("canceled" if cancel_during_construction else "failed")
        if not cancel_during_construction:
            assert final["error_code"] == "independent_verification_required"
        assert final["ownership_settled"] and not final["reservation_retained"]
        assert not store.pending_ownership(identifier)
        assert imports.inspect(draft["draft_id"])["attachment"] is None
    finally:
        release.set()
        if canceled is not None:
            await asyncio.gather(canceled, return_exceptions=True)
        await runner.close()
        imports.close()


@pytest.mark.asyncio
async def test_canceled_publication_joins_hash_worker_before_settlement(tmp_path, native, monkeypatch):
    from mcpme import preparation_command_owner as module
    imports, store, owner = intake(tmp_path, native)
    entered, release = threading.Event(), threading.Event()
    original = module.validate_package

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "validate_package", paused)
    task = asyncio.create_task(owner.session(owner.package))
    try:
        await wait_entered(entered)
        task.cancel()
        await asyncio.sleep(.05)
        task.cancel()
        await asyncio.sleep(.15)
        assert not task.done() and store.pending_ownership(owner.job_id)
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert not owner.sessions and not owner.executor.journal["entries"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await owner.close()
        assert owner.confirm_completed() and owner.confirm_completed()
        imports.close()
