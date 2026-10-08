"""Resolution, form, revision carry and exact owned package consumption."""
import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

import pytest

from meecp.imports import ImportStore
from meecp.recipes import RecipeStore
from meecp.preparations import PreparationStore
from meecp.preparation_builtin_recipes import seed_builtin_recipes
from meecp.preparation_forms import PreparationForms
from meecp.preparation_resolution import ResolutionStore, result_from_files
from meecp.preparation_resolution_runner import OwnedResolutionRunner, dependency_project
from meecp.preparation_runner import PreparationRunner
from meecp.preparation_inputs import parse_preparation_receipt
from meecp.preparation_contracts import PreparationError, ExecutionPolicy
from meecp.preparation_inspection import inspect_source
from meecp.preparation_verifier import verify_prepared_package
from meecp.contracts import validate_package
from test_preparation_resolution import source, request, policy


def seeded(imports, resolutions):
    recipes = RecipeStore(imports)
    result = seed_builtin_recipes(recipes)
    # This fixture consumes only the resolved Python component. Frozen recipes
    # for unrelated families can correctly become unavailable while backend
    # changes await a new centrally reviewed producer version.
    required = next(item for item in result["recipes"] if item["recipe_id"] == "mcpme.resolved-python")
    assert required["ready"], required
    store = PreparationStore(imports, recipes, resolutions=resolutions)
    forms = PreparationForms(store, preferred_recipes=[required["recipe"]])
    return store, forms


def answers(form):
    values = {}
    for field in form.fields:
        if field.label.startswith("Test input"):
            values[field.id] = {"n": 3}
        elif field.label.startswith("Expected test result"):
            values[field.id] = {"value": 6}
    return values


def resolve_form(forms, draft, candidate, digest):
    context = forms.context(draft["draft_id"], draft["revision"], candidate_id=candidate["candidate_id"], resolution_digest=digest)
    assert not context.form.blockers
    return forms.resolve(draft["draft_id"], draft["revision"], candidate_id=candidate["candidate_id"],
        recipe=context.form.recipe.model_dump(), form_digest=context.form.form_digest,
        answers=answers(context.form), resolution_digest=digest)


def publish_fake(source, tmp_path):
    imports, resolutions, draft, candidate, folder = source
    req = request(draft, candidate)
    plan = resolutions.plan(req, candidate, expires_in=60)
    job = resolutions.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    workspace = tmp_path.resolve() / "result"
    derived = workspace / "derived"
    derived.mkdir(parents=True)
    (derived / "pyproject.toml").write_bytes(dependency_project(draft, candidate))
    (derived / "uv.lock").write_bytes(b"version=1\n")
    resolutions.begin(job["job_id"], workspace)
    result = result_from_files(req, derived, [], None, 1024)
    resolutions.publish(job["job_id"], result)
    return result, derived


def test_generated_resolved_descriptor_carries_exact_result_atomically(source, tmp_path):
    imports, resolutions, draft, candidate, folder = source
    result, derived = publish_fake(source, tmp_path)
    store, forms = seeded(imports, resolutions)
    pending = forms.context(draft["draft_id"], 1)
    assert pending.form.resolution_required and pending.form.resolution_operations == ["resolve_dependencies"]
    descriptor, context = resolve_form(forms, draft, candidate, result.digest)
    assert descriptor.schema_version == 2 and not hasattr(descriptor, "dependencies")
    before = copy.deepcopy(resolutions.list()[0]["result"])
    selected = store.select_descriptor(draft["draft_id"], 1, descriptor, candidate_id=candidate["candidate_id"],
        form_digest=context.form.form_digest, resolution_digest=result.digest)
    assert selected["revision"] == 2 and selected["source"] == draft["source"]
    assert resolutions.resolve_result(draft["draft_id"], 2, candidate["candidate_id"], result.digest)[0] == result
    assert resolutions.list()[0]["result"] == before
    restored = forms.context(draft["draft_id"], 2)
    assert restored.form.resolution_digest == result.digest
    assert next(field.value for field in restored.form.fields if field.label == "Expected test result") == {"value": 6}
    assert store.select_descriptor(draft["draft_id"], 2, descriptor, candidate_id=candidate["candidate_id"],
        form_digest=restored.form.form_digest)["revision"] == 2
    assert not (Path(draft["source"]["snapshot_path"]) / "uv.lock").exists()
    with pytest.raises(PreparationError, match="resolution_prevalidation_transaction"):
        with imports._transaction():
            resolutions.prepare_result_binding(draft["draft_id"], 2, candidate["candidate_id"], result.digest)
    with pytest.raises(PreparationError, match="resolution_checked_binding_required"):
        with imports._transaction():
            resolutions.commit_result_binding({"result_digest": result.digest}, 3)


async def test_paused_result_hash_releases_management_lock_and_stale_selection_fails(source, tmp_path, monkeypatch):
    imports, resolutions, draft, candidate, folder = source
    result, _ = publish_fake(source, tmp_path)
    store, forms = seeded(imports, resolutions)
    descriptor, context = resolve_form(forms, draft, candidate, result.digest)
    started, finish = threading.Event(), threading.Event()
    original = resolutions._read_result
    def paused(digest):
        started.set()
        assert finish.wait(5)
        return original(digest)
    monkeypatch.setattr(resolutions, "_read_result", paused)
    task = asyncio.create_task(asyncio.to_thread(store.select_descriptor, draft["draft_id"], 1, descriptor,
        candidate_id=candidate["candidate_id"], form_digest=context.form.form_digest))
    assert await asyncio.to_thread(started.wait, 5)
    try:
        # Actual collection/status methods and metadata mutation share the DB.
        assert (await asyncio.wait_for(asyncio.to_thread(resolutions.list_summaries), .5))[0]["state"] == "succeeded"
        changed = await asyncio.wait_for(asyncio.to_thread(imports.revise, draft["draft_id"], 1, name="New name"), .5)
        assert changed["revision"] == 2
    finally:
        finish.set()
    with pytest.raises(Exception, match="stale_import_revision"):
        await task
    assert store.selected_descriptor(draft["draft_id"]) is None


def test_checked_carry_rollback_stale_authority_and_changed_bytes(source, tmp_path):
    imports, resolutions, draft, candidate, folder = source
    result, derived = publish_fake(source, tmp_path)
    proof = resolutions.prepare_result_binding(draft["draft_id"], 1, candidate["candidate_id"], result.digest)
    with pytest.raises(RuntimeError):
        with imports._transaction():
            current = imports._get(draft["draft_id"], 1)
            current["revision"] = 2
            imports._save(current)
            resolutions.commit_result_binding(proof, 2)
            raise RuntimeError("rollback")
    assert imports.inspect(draft["draft_id"])["revision"] == 1
    assert imports._connection.execute("SELECT count(*) FROM accepted_resolutions").fetchone()[0] == 0
    (derived / "uv.lock").write_bytes(b"version=2\n")
    with pytest.raises(PreparationError, match="resolution_result_changed"):
        resolutions.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], result.digest)


def test_plan_and_receipt_bind_exact_derived_closure_and_runtime(source, tmp_path):
    imports, resolutions, draft, candidate, folder = source
    result, _ = publish_fake(source, tmp_path)
    store, forms = seeded(imports, resolutions)
    descriptor, context = resolve_form(forms, draft, candidate, result.digest)
    selected = store.select_descriptor(draft["draft_id"], 1, descriptor, candidate_id=candidate["candidate_id"],
        form_digest=context.form.form_digest)
    build_policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], network=[],
        wall_seconds=300, disk_bytes=2 * 1024**3, memory_mb=2048)
    plan = store.execution_plan(draft["draft_id"], 2, descriptor, build_policy,
        runner_digest="b"*64, verifier_digest="c"*64, runtime_digest="a"*64)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    from meecp.preparation_inputs import PreparationReceiptV2
    receipt = PreparationReceiptV2(schema_version=2, binding=job["binding"],
        inputs=job["bound_inputs"], fixture_digest=job["fixture_digest"])
    assert receipt.inputs.derived_inputs == result.derived_inputs
    assert receipt.inputs.runtimes == (result.request.runtime,)
    assert receipt.inputs.original_dependency_locks == ()
    assert {item.path for item in receipt.inputs.source_validations[0].files} == {item["path"] for item in draft["source"]["files"]}
    tampered = receipt.model_dump(mode="json")
    tampered["binding"]["descriptor_digest"] = "d"*64
    with pytest.raises(PreparationError, match="receipt_input_binding_mismatch"):
        parse_preparation_receipt(tampered)
    store.cancel(job["job_id"])
    wrong = store.execution_plan(draft["draft_id"], 2, descriptor, build_policy,
        runner_digest="b"*64, verifier_digest="c"*64, runtime_digest="d"*64)
    with pytest.raises(PreparationError, match="resolution_runtime_mismatch"):
        store.authorize_start(wrong["plan_id"], wrong["plan_digest"], str(uuid.uuid4()))


def test_canceled_import_cannot_consume_prechecked_carry(source, tmp_path):
    imports, resolutions, draft, candidate, folder = source
    result, _ = publish_fake(source, tmp_path)
    proof = resolutions.prepare_result_binding(draft["draft_id"], 1, candidate["candidate_id"], result.digest)
    imports.cancel(draft["draft_id"], 1)
    with pytest.raises(Exception):
        with imports._transaction():
            resolutions.commit_result_binding(proof, 2)
    assert imports._connection.execute("SELECT count(*) FROM accepted_resolutions").fetchone()[0] == 0


@pytest.mark.skipif(not os.environ.get("MCPME_RESOLUTION_HOST"), reason="requires private host and bounded public PyPI")
@pytest.mark.parametrize("kind", ["callable", "dynamic-mcp"])
async def test_real_resolution_form_build_attach_and_relocation(tmp_path, kind):
    host = Path(os.environ["MCPME_RESOLUTION_HOST"]).resolve()
    source = tmp_path.resolve() / "ordinary-source"
    source.mkdir()
    (source / "LICENSE").write_text("Synthetic test permission notice.\n")
    if kind == "callable":
        (source / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n return {'value': n*2}\n")
    else:
        (source / "pyproject.toml").write_text('[project]\nname="dynamic"\nversion="1.0.0"\ndependencies=["mcp==1.30.0"]\n'
            '[tool.meecp]\nmcp_module="worker:main"\n')
        (source / "worker.py").write_text('import asyncio\nfrom mcp.server import Server\nfrom mcp.server.models import InitializationOptions\n'
            'from mcp.server.stdio import stdio_server\nfrom mcp.types import Tool,ServerCapabilities,ToolsCapability,CallToolResult,TextContent\n'
            'server=Server("synthetic")\n@server.list_tools()\nasync def listing():\n'
            ' return [Tool(name="dynamic_"+suffix,description="Synthetic dynamic tool",inputSchema={"type":"object","properties":{"n":{"type":"integer"}},"required":["n"],"additionalProperties":False},outputSchema={"type":"object","properties":{"value":{"type":"integer"}},"required":["value"],"additionalProperties":False}) for suffix in ["one","two"]]\n'
            '@server.call_tool(validate_input=False)\nasync def call(name,args):\n return CallToolResult(content=[TextContent(type="text",text="synthetic")],structuredContent={"value":args["n"]*2})\n'
            'async def serve():\n async with stdio_server() as (read,write):\n'
            '  await server.run(read,write,InitializationOptions(server_name="synthetic",server_version="1.0.0",capabilities=ServerCapabilities(tools=ToolsCapability())))\n'
            'def main(): asyncio.run(serve())\n')
    with ImportStore(tmp_path.resolve() / "state") as imports:
        draft = imports.create(source, name="Ordinary derived app")
        candidate = next(item for item in inspect_source(draft)["candidates"]
            if item["kind"] == ("python-callable" if kind == "callable" else "python-mcp"))
        resolutions = ResolutionStore(imports)
        resolver = OwnedResolutionRunner(resolutions, host, host / "bin/meecp-reap", enabled=True)
        store, forms = seeded(imports, resolutions)
        builder = PreparationRunner(store, host, host / "bin/meecp-reap", enabled=True,
                                    enabled_families=tuple(store.families))
        try:
            plan = await resolver.plan(draft["draft_id"], 1, candidate["candidate_id"], policy(kind != "callable"))
            job = await resolver.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
            await resolver.tasks[job["job_id"]]
            resolved = resolutions.get(job["job_id"])
            assert resolved["state"] == "succeeded", resolved["error_code"]
            descriptor, context = await asyncio.to_thread(resolve_form, forms, draft, candidate, resolved["result_digest"])
            selected = await asyncio.to_thread(store.select_descriptor, draft["draft_id"], 1, descriptor,
                candidate_id=candidate["candidate_id"], form_digest=context.form.form_digest)
            plan = await builder.plan(draft["draft_id"], selected["revision"], descriptor)
            job = await builder.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
            await builder.tasks[job["job_id"]]
            final = store.get(job["job_id"])
            assert final["state"] == "succeeded", final["error_code"]
            attached = Path(imports.inspect(draft["draft_id"])["attachment"]["path"])
            receipt = parse_preparation_receipt((attached / "meecp.preparation.json").read_bytes())
            assert receipt.schema_version == 2 and receipt.binding.revision == selected["revision"]
            assert not receipt.inputs.original_dependency_locks and receipt.inputs.captured_collection is None
            assert receipt.inputs.derived_inputs[0].request_digest == plan_request_digest(resolved)
            assert not (attached / "source/uv.lock").exists()
            moved = tmp_path.resolve() / "relocated"
            shutil.copytree(attached, moved)
            shutil.rmtree(source)
            shutil.rmtree(attached)
            package = validate_package(moved)
            probe = tmp_path.resolve() / "relocation-probe"
            probe.mkdir()
            report = await verify_prepared_package(moved, descriptor, host / "bin/meecp-reap", probe, timeout_seconds=30)
            assert report.package_digest == package.digest and report.tools_checked == (1 if kind == "callable" else 2)
            assert report.fixtures_checked == report.tools_checked
            assert all(not owner.instances and not owner._pending_ids for owner in builder.attempts[job["job_id"]].owners)
        finally:
            await resolver.close()
            await builder.close()


def plan_request_digest(job):
    from meecp.preparation_resolution import ResolutionRequestV1
    return ResolutionRequestV1.model_validate(job["request"]).digest
