"""Actual immutable FD artifacts and simulated provider; no execution claims."""
import asyncio
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading
import uuid

import pytest

from mcpme.imports import ImportStore, ImportError as SourceError
from mcpme.recipes import RecipeStore
from mcpme.preparations import PreparationStore
from mcpme.preparation_agent_adapter import (AgentPythonAdapterFamily, component_catalog, descriptor_for, VERSION)
from mcpme.preparation_agent_adapter_store import (AgentAdapterDerivationStore, CODE_PATH, MODULE)
from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_forms import PreparationForms
from mcpme.preparation_inspection import inspect_source
from test_preparation_agent import output, response, role_of
from test_preparation_agent_host import provider, completed


TOOL = {"name": "double", "description": "Synthetic scalar wrapper",
    "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False},
    "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}}
CODE = "from worker import double\ndef invoke(n: int) -> dict[str, int]:\n return {'value': double(n)}\n"


def bundle(path, catalog):
    path.mkdir()
    (path / "LICENSE").write_text("Synthetic recipe permission.\n")
    ref = catalog["agent-python-adapter-v1@" + VERSION]
    manifest = {"schema_version": 1, "id": "mcpme.agent-python-adapter", "version": VERSION, "kind": "declarative",
        "worker_api_version": 1, "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}, "form": [],
        "steps": [{"component": "agent-python-adapter-v1", "version": VERSION, "digest": ref["digest"], "config": {}}],
        "licenses": ["LICENSE"], "provenance": {"origin": "synthetic", "publisher": "test"},
        "requirements": {"host_api_version": 1, "package_schema": 1, "protocol": "2025-11-25", "os": "macos",
            "architecture": "arm64", "capabilities": ["declarative-composition"]}}
    data = json.dumps(manifest).encode(); (path / "mcpme.recipe.json").write_bytes(data)
    license_data = (path / "LICENSE").read_bytes()
    (path / "mcpme.recipe.lock.json").write_text(json.dumps({"schema_version": 1,
        "manifest_sha256": hashlib.sha256(data).hexdigest(), "artifacts": [{"path": "LICENSE",
            "sha256": hashlib.sha256(license_data).hexdigest(), "size": len(license_data), "license": "LICENSE", "origin": "synthetic"}]}))
    return path


def handler(lock_hash, *, changes=None):
    proposals = {
        "interface": [{"path": "proposed/interface/interface.json", "content": json.dumps({"tool": TOOL})}],
        "dependencies": [{"path": "proposed/dependencies/dependencies.json", "content": json.dumps({
            "project": "pyproject.toml", "lock": "uv.lock", "lock_sha256": lock_hash})}],
        "fixtures": [{"path": "proposed/fixtures/fixtures.json", "content": json.dumps({"arguments": {"n": 3}, "expected": {"value": 6}})}],
        "adapter": [{"path": "proposed/adapter/adapter.json", "content": json.dumps({"schema_version": 1,
            "module": MODULE, "function": "invoke", "source_license": "LICENSE"})},
            {"path": "proposed/adapter/wrapper.py", "content": CODE},
            {"path": "proposed/adapter/LICENSE", "content": "Synthetic generated-code permission.\n"}]}
    if changes is not None:
        changes(proposals)
    def transport(request):
        role = role_of(request)
        return response(role, output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text",
            "text": json.dumps(output(role, files=proposals[role]))}]}])
    return transport


@pytest.fixture
def adapter_foundation(tmp_path, monkeypatch):
    from mcpme import recipes as recipe_module
    original = recipe_module.builtin_components
    monkeypatch.setattr(recipe_module, "builtin_components", lambda: {**original(), **component_catalog()})
    source = tmp_path / "source"; source.mkdir()
    (source / "worker.py").write_text("def double(n):\n return n*2\n")
    (source / "LICENSE").write_text("Synthetic original permission.\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\ndependencies=[]\n")
    lock = b"version=1\nrevision=3\n"; (source / "uv.lock").write_bytes(lock)
    with ImportStore(tmp_path / "state") as imports:
        recipes = RecipeStore(imports)
        registered = recipes.add(bundle(tmp_path / "bundle", component_catalog()))
        registered = recipes.enable(registered["recipe_id"], registered["version"])
        draft = imports.create(source, name="Unknown scalar app", intent="Wrap the selected scalar callable as a structured result")
        derivations = AgentAdapterDerivationStore(imports, recipes)
        family = AgentPythonAdapterFamily(derivations)
        preparations = PreparationStore(imports, recipes, families=(family,))
        yield imports, recipes, preparations, draft, derivations, family, hashlib.sha256(lock).hexdigest()


async def analyze(foundation, tmp_path, *, changes=None):
    imports, recipes, preparations, draft, derivations, family, lock_hash = foundation
    registered = recipes.list()[0]
    host = AgentPreparationHost(preparations, tmp_path / ("agent-" + uuid.uuid4().hex),
        providers=(provider(handler(lock_hash, changes=changes)),), simulated_enabled=True)
    plan = await host.plan({"import_id": draft["draft_id"], "revision": draft["revision"], "recipe": {
        "id": registered["recipe_id"], "version": registered["version"], "digest": registered["digest"]},
        "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["worker.py"], "limits": {}})
    job = await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
    job = await completed(host, job)
    if changes is None and job["candidate"] is None:
        host._candidate(host._job(job["job_id"]))
    return host, plan, job


async def test_untyped_scalar_requires_adapter_then_actual_generated_forms_adoption(adapter_foundation, tmp_path):
    imports, recipes, preparations, draft, derivations, family, _ = adapter_foundation
    original = copy.deepcopy(draft["source"])
    assert not inspect_source(draft)["candidates"]
    host, plan, job = await analyze(adapter_foundation, tmp_path)
    try:
        assert job["state"] == "succeeded" and job["candidate"] and len(job["calls"]) == 4
        selected = await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        adopted = selected["draft"]
        assert adopted["revision"] == 2 and adopted["source"] == original and adopted["attachment"] is None
        assert not (Path(original["snapshot_path"]) / CODE_PATH).exists() and preparations.list() == []
        forms = PreparationForms(preparations)
        context = forms.context(draft["draft_id"], 2)
        assert not context.form.blockers and not context.form.resolution_required
        value, _ = forms.resolve(draft["draft_id"], 2, candidate_id=context.form.candidate_id,
            recipe=context.form.recipe.model_dump(), form_digest=context.form.form_digest, answers={})
        assert value.digest == selected["descriptor_digest"]
        bound = family.bound_inputs(value, adopted, recipes.resolve(value.recipe.id, value.recipe.version, value.recipe.digest),
            preparations.components, "a"*64)
        assert len(bound.derived_inputs) == 3 and len(bound.original_dependency_locks) == 1
        assert bound.source.revision == original["revision"]
        assert [item.model_dump() for item in bound.source_validations[0].files] == original["files"]
        assert all(item.path != CODE_PATH for item in bound.source_validations[0].files)
        assert not preparations.plan(draft["draft_id"], 2, value)["execution_available"]
    finally:
        await host.close()


async def test_changed_reviewed_questions_carry_same_derived_input_but_intent_edit_does_not(adapter_foundation, tmp_path):
    imports, _, preparations, draft, derivations, family, _ = adapter_foundation
    host, _, job = await analyze(adapter_foundation, tmp_path)
    try:
        await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        forms = PreparationForms(preparations)
        context = forms.context(draft["draft_id"], 2)
        name = next(item.id for item in context.form.fields if item.label == "App name")
        descriptor, _ = forms.resolve(draft["draft_id"], 2, candidate_id=context.form.candidate_id,
            recipe=context.form.recipe.model_dump(), form_digest=context.form.form_digest, answers={name: "Reviewed new name"})
        changed = preparations.select_descriptor(draft["draft_id"], 2, descriptor,
            candidate_id=context.form.candidate_id, form_digest=context.form.form_digest)
        assert changed["revision"] == 3
        assert not forms.context(draft["draft_id"], 3).form.blockers
        unrelated = imports.revise(draft["draft_id"], 3, intent="Different task")
        assert not family.inspect_source(unrelated)["candidates"]
    finally:
        await host.close()


async def test_canceled_held_derivation_worker_joins_before_recovery_and_delete(adapter_foundation, tmp_path, monkeypatch):
    from mcpme import preparation_agent_adapter_store as module
    imports, recipes, preparations, draft, derivations, family, lock_hash = adapter_foundation
    entered, release = threading.Event(), threading.Event()
    original = module.original_snapshot
    def held(value, check=lambda: None):
        result = original(value, check)
        if str(value["source"]["snapshot_path"]).startswith(str(derivations.root)):
            entered.set(); assert release.wait(5)
        return result
    monkeypatch.setattr(module, "original_snapshot", held)
    record = recipes.list()[0]
    host = AgentPreparationHost(preparations, tmp_path / "held-agent", providers=(provider(handler(lock_hash)),), simulated_enabled=True)
    cancel = None
    try:
        plan = await host.plan({"import_id": draft["draft_id"], "revision": 1,
            "recipe": {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]},
            "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["worker.py"], "limits": {}})
        job = await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        assert await asyncio.to_thread(entered.wait, 5)
        item = json.loads(derivations.db.execute("SELECT data FROM agent_adapter_derivations").fetchone()[0])
        derivations.cancel(item["id"])
        with pytest.raises(PreparationError, match="ownership_unsettled"):
            derivations.recover(item["id"])
        cancel = asyncio.create_task(host.cancel(job["job_id"]))
        await asyncio.sleep(.05)
        assert not cancel.done() and item["id"] in derivations.workers
        release.set()
        ended = await cancel
        assert ended["state"] == "canceled" and ended["local_io_settled"] and not derivations.workers
        assert family.inspect_source(draft)["candidates"] == []
        derivations.recover(item["id"]); derivations.delete(item["id"])
        assert not (derivations.root / item["id"]).exists()
    finally:
        release.set()
        if cancel is not None:
            await cancel
        await host.close()


async def test_replaced_derived_file_cannot_be_deleted_as_owned(adapter_foundation, tmp_path):
    _, _, _, _, derivations, _, _ = adapter_foundation
    host, _, _ = await analyze(adapter_foundation, tmp_path)
    await host.close()
    item = json.loads(derivations.db.execute("SELECT data FROM agent_adapter_derivations").fetchone()[0])
    original = derivations.root / item["id"] / CODE_PATH
    original.rename(tmp_path / "retained-owned-code")
    original.write_text("# replacement\n"); original.chmod(0o600)
    derivations.cancel(item["id"])
    with pytest.raises(PreparationError, match="ownership_unsettled"):
        derivations.delete(item["id"])
    assert original.exists() and derivations.get(item["id"])["state"] == "interrupted"


async def test_current_metadata_check_reads_no_source_bytes_and_rejects_changed_file(adapter_foundation, tmp_path, monkeypatch):
    from mcpme import preparation_agent_adapter_store as module
    imports, _, preparations, draft, _, family, _ = adapter_foundation
    host, _, job = await analyze(adapter_foundation, tmp_path)
    try:
        adopted = await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        descriptor = preparations._bound_descriptor(draft["draft_id"], 2, preparations.selected_descriptor(draft["draft_id"])["descriptor"])[0]
        def forbidden(*args, **kwargs):
            raise AssertionError("metadata fence read source bytes")
        monkeypatch.setattr(module, "read_snapshot", forbidden)
        with imports._transaction():
            assert family.check_current(descriptor, adopted["draft"]) is True
        (Path(draft["source"]["snapshot_path"]) / "worker.py").write_bytes(b"# changed\n")
        with imports._transaction(), pytest.raises(PreparationError, match="input_changed"):
            family.check_current(descriptor, adopted["draft"])
    finally:
        await host.close()


@pytest.mark.skipif("preparation_family_current_inputs_denied" not in inspect.getsource(PreparationStore._current_inputs),
    reason="returned shared hook must be applied for this integration control")
@pytest.mark.parametrize("result", [False, None, 1, "true", "awaitable"])
async def test_shared_authority_hook_requires_synchronous_exact_true(adapter_foundation, tmp_path, monkeypatch, result):
    from mcpme.preparation_contracts import ExecutionPolicy
    imports, _, preparations, draft, _, family, _ = adapter_foundation
    host, _, job = await analyze(adapter_foundation, tmp_path)
    try:
        await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        descriptor = preparations.selected_descriptor(draft["draft_id"])["descriptor"]
        policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], network=[], wall_seconds=30,
            disk_bytes=512*1024**2, memory_mb=128)
        plan = preparations.execution_plan(draft["draft_id"], 2, descriptor, policy,
            runner_digest="a"*64, verifier_digest="b"*64, runtime_digest="c"*64)
        build = preparations.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        assert preparations.check_authority(build["job_id"])["state"] == "queued"
        async def later():
            return True
        monkeypatch.setattr(family, "check_current", lambda *args: later() if result == "awaitable" else result)
        with pytest.raises(PreparationError, match="family_current_inputs_denied"):
            preparations.check_authority(build["job_id"])
    finally:
        await host.close()


async def test_static_adoption_does_not_import_original_module(adapter_foundation, tmp_path):
    imports, _, preparations, draft, _, _, _ = adapter_foundation
    source = tmp_path / "never-import"; source.mkdir()
    for name in ("LICENSE", "pyproject.toml", "uv.lock"):
        (source / name).write_bytes((tmp_path / "source" / name).read_bytes())
    (source / "worker.py").write_text("raise RuntimeError('static inspection imported source')\ndef double(n): return n*2\n")
    captured = imports.create(source, name="Static only", intent="Wrap the scalar")
    updated = (*adapter_foundation[:3], captured, *adapter_foundation[4:])
    host, _, job = await analyze(updated, tmp_path)
    try:
        assert job["candidate"] is not None
        adopted = await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert adopted["draft"]["revision"] == 2 and preparations.list() == []
    finally:
        await host.close()


async def test_locked_copy_preserves_binary_model_and_rechecks_full_tree(adapter_foundation, tmp_path, monkeypatch):
    from mcpme.preparation_agent_adapter_builder import copy_adapter_snapshot
    from mcpme import preparation_agent_adapter_builder as module
    imports, _, _, _, _, family, _ = adapter_foundation
    source = tmp_path / "binary-app"; source.mkdir()
    for name in ("worker.py", "LICENSE", "pyproject.toml", "uv.lock"):
        (source / name).write_bytes((tmp_path / "source" / name).read_bytes())
    (source / "models").mkdir()
    asset = b"\0\xffsynthetic model bytes\x80"
    (source / "models/model.onnx").write_bytes(asset)
    draft = imports.create(source, name="Binary app", intent="Wrap the scalar", source_mode="automatic")
    updated = (*adapter_foundation[:3], draft, *adapter_foundation[4:])
    host, _, job = await analyze(updated, tmp_path)
    try:
        descriptor, observed = host._candidate(host._job(job["job_id"]))
        result, derived = family.component.result(descriptor, observed)
        destination = tmp_path / "copied-source"
        records = copy_adapter_snapshot(draft, destination, result, derived, lambda: None)
        assert (destination / "models/model.onnx").read_bytes() == asset
        assert (destination / CODE_PATH).read_text() == CODE
        assert next(item for item in records if item["path"] == "models/model.onnx")["sha256"] == hashlib.sha256(asset).hexdigest()
        original = module._write_file
        def corrupted(root, name, data):
            original(root, name, data + b"changed" if name == "models/model.onnx" else data)
        monkeypatch.setattr(module, "_write_file", corrupted)
        with pytest.raises(PreparationError, match="source_identity_mismatch"):
            copy_adapter_snapshot(draft, tmp_path / "corrupt-copy", result, derived, lambda: None)
    finally:
        await host.close()


def test_new_bundle_static_registration_preserves_nonexecution_boundary(adapter_foundation, tmp_path):
    _, recipes, _, _, _, family, _ = adapter_foundation
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(PreparationError, match="recipe_version_collision"):
        recipes.add(root / "templates/builtin-recipes/agent-python-adapter-v1-0-2")
    with ImportStore(tmp_path / "new-state") as imports:
        actual = RecipeStore(imports)
        registered = actual.add(root / "templates/builtin-recipes/agent-python-adapter-v1-0-2")
        enabled = actual.enable(registered["recipe_id"], registered["version"])
        assert enabled["state"] == "enabled" and not enabled["runnable"]
        assert enabled["manifest"]["steps"] == [{"component": family.component.reference.id,
            "version": family.component.reference.version, "digest": family.component.reference.digest, "config": {}}]


@pytest.mark.parametrize("mutation", ["code", "source", "recipe", "revision", "derivation-state"])
async def test_stale_or_changed_inputs_cannot_adopt(adapter_foundation, tmp_path, mutation):
    imports, recipes, preparations, draft, derivations, family, _ = adapter_foundation
    host, _, job = await analyze(adapter_foundation, tmp_path)
    try:
        item = json.loads(derivations.db.execute("SELECT data FROM agent_adapter_derivations").fetchone()[0])
        if mutation == "code":
            (derivations.root / item["id"] / CODE_PATH).write_text(CODE + "\n")
        elif mutation == "source":
            (Path(draft["source"]["snapshot_path"]) / "worker.py").write_text("def double(n): return n*3\n")
        elif mutation == "recipe":
            recipes.disable("mcpme.agent-python-adapter", VERSION)
        elif mutation == "revision":
            imports.revise(draft["draft_id"], 1, name="Changed")
        else:
            derivations.cancel(item["id"])
        with pytest.raises((PreparationError, SourceError)):
            await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert preparations.selected_descriptor(draft["draft_id"]) is None
    finally:
        await host.close()


@pytest.mark.parametrize("mutation", ["extra", "wrong-lock", "untyped", "top-level", "namespace"])
async def test_closed_proposal_roles_static_eligibility_and_collision_blockers(adapter_foundation, tmp_path, mutation):
    def change(proposals):
        if mutation == "extra":
            proposals["interface"].append({"path": "proposed/interface/ignored.py", "content": "pass"})
        elif mutation == "wrong-lock":
            value = json.loads(proposals["dependencies"][0]["content"]); value["lock_sha256"] = "f"*64
            proposals["dependencies"][0]["content"] = json.dumps(value)
        elif mutation == "untyped":
            proposals["adapter"][1]["content"] = "def invoke(n): return {'value': n}\n"
        elif mutation == "top-level":
            proposals["adapter"][1]["content"] = "raise RuntimeError('never execute')\n" + CODE
    if mutation == "namespace":
        imports, _, _, draft, _, _, _ = adapter_foundation
        # Exact original capture collision, rather than tampering after capture.
        source = tmp_path / "collision"; source.mkdir(); (source / CODE_PATH).write_text("pass\n")
        for name in ("worker.py", "LICENSE", "pyproject.toml", "uv.lock"):
            (source / name).write_bytes((tmp_path / "source" / name).read_bytes())
        draft = imports.create(source, name="Collision", intent="Wrap the scalar")
        adapter_foundation = (*adapter_foundation[:3], draft, *adapter_foundation[4:])
    host, _, job = await analyze(adapter_foundation, tmp_path, changes=change)
    try:
        assert job["state"] == "succeeded" and job["candidate"] is None
        expected = {"extra": "agent_adapter_proposal_invalid", "wrong-lock": "locked_dependency_project_required",
            "top-level": "agent_adapter_static_eligibility_required"}.get(mutation, "agent_candidate_requires_review")
        assert job["blockers"] == [expected]
        assert adapter_foundation[2].list() == []
    finally:
        await host.close()


async def test_durable_restart_and_explicit_cancel_delete_exact_artifacts(adapter_foundation, tmp_path):
    imports, recipes, preparations, draft, derivations, family, _ = adapter_foundation
    host, _, job = await analyze(adapter_foundation, tmp_path)
    await host.close()
    item = json.loads(derivations.db.execute("SELECT data FROM agent_adapter_derivations").fetchone()[0])
    reopened = AgentAdapterDerivationStore(imports, recipes)
    result, path = reopened.resolve(item["id"], item["result_digest"], draft)
    assert result.request.attempt_id == job["attempt_id"] and result.request.proposal_digest == job["proposal_digest"]
    assert all(os.stat(path / name).st_mode & 0o777 == 0o600 for name in ("LICENSE", "adapter.json", CODE_PATH))
    reopened.cancel(item["id"]); reopened.delete(item["id"])
    assert not path.exists() and reopened.get(item["id"])["state"] == "deleted"
