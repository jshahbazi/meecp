"""Request-inspecting simulation; no live provider or hidden source facts."""
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from meecp.preparation_agent_contracts import AnalysisGuideV1
from meecp.preparation_agent_guides import AnalysisGuideRegistry, builtin_analysis_guides
from meecp.preparation_agent_host import AgentPreparationHost
from meecp.preparation_contracts import PreparationError
from test_preparation_agent import output, response
from test_preparation_agent_adapter import adapter_foundation
from test_preparation_agent_host import provider, completed


def request_only_handler(request):
    """Analyze only this dispatch. No closure, filesystem or source fixture input."""
    payload = json.loads(request.content)
    material = json.loads(payload["input"][0]["content"][0]["text"])
    role = material["role"]
    instructions = payload["instructions"]
    guide = json.loads(instructions[instructions.index('{"component"'):])
    contract = guide["role_contract"]
    assert contract["role"] == role
    assert [p["role"] for p in material["prior_proposals"]] == contract["depends_on"]
    records = {item["path"]: item for item in material["selected_files"] + material["selected_metadata"]}
    assert set(material["selected_source"]) == {item["path"] for item in material["selected_files"]}
    for path, content in material["selected_source"].items():
        assert hashlib.sha256(content.encode()).hexdigest() == records[path]["sha256"]
    path, source = next(iter(material["selected_source"].items()))
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef))
    arg = function.args.args[0].arg
    multiplication = function.body[0].value
    assert isinstance(multiplication, ast.BinOp) and isinstance(multiplication.op, ast.Mult)
    factor = multiplication.right.value
    tool = {"name": function.name, "description": "Synthetic scalar wrapper",
        "input_schema": {"type": "object", "properties": {arg: {"type": "integer"}}, "required": [arg], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}
    unresolved = []
    if role == "interface":
        files = [{"path": "proposed/interface/interface.json", "content": json.dumps({"tool": tool})}]
    elif role == "dependencies":
        if "uv.lock" not in records or "pyproject.toml" not in records:
            files = [{"path": "proposed/dependencies/dependencies.json", "content": "{}"}]
            unresolved = ["Original project and lock identity were not reviewed for disclosure."]
        else:
            files = [{"path": "proposed/dependencies/dependencies.json", "content": json.dumps({
                "project": "pyproject.toml", "lock": "uv.lock", "lock_sha256": records["uv.lock"]["sha256"]})}]
    elif role == "fixtures":
        agreed = json.loads(material["prior_proposals"][0]["files"][0]["content"])["tool"]
        assert agreed == tool
        files = [{"path": "proposed/fixtures/fixtures.json", "content": json.dumps({"arguments": {arg: 3}, "expected": {"value": 3 * factor}})}]
    else:
        if "LICENSE" not in records:
            unresolved = ["Original license identity was not reviewed for disclosure."]
        files = [
            {"path": "proposed/adapter/adapter.json", "content": json.dumps({"schema_version": 1,
                "module": "meecp_generated_agent_adapter", "function": "invoke", "source_license": "LICENSE"})},
            {"path": "proposed/adapter/wrapper.py", "content": f"from {Path(path).stem} import {function.name}\ndef invoke({arg}: int) -> dict[str, int]:\n return {{'value': {function.name}({arg})}}\n"},
            {"path": "proposed/adapter/LICENSE", "content": "Permission to use, copy and modify this synthetic generated wrapper is granted.\n"}]
    assert all(item["path"] in contract["instructions"] for item in files)
    return response(role, output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text",
        "text": json.dumps(output(role, files=files, unresolved=unresolved))}]}])


def selected(foundation, *, metadata=True):
    _, recipes, _, draft, *_ = foundation
    recipe = recipes.list()[0]
    return {"import_id": draft["draft_id"], "revision": 1,
        "recipe": {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
        "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["worker.py"],
        "selected_metadata_paths": ["LICENSE", "pyproject.toml", "uv.lock"] if metadata else []}


async def start(host, plan):
    return await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
        "idempotency_key": str(uuid.uuid4())})


@pytest.mark.parametrize("metadata", [False, True])
async def test_actual_encoded_request_supplies_admissible_proposal_only_with_reviewed_facts(adapter_foundation, tmp_path, metadata):
    imports, _, preparations, draft, *_ = adapter_foundation
    seen, active = [], set()
    peak = 0
    async def transport(request):
        nonlocal peak
        material = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])
        active.add(material["role"]); peak = max(peak, len(active))
        seen.append((material, len(request.content)))
        await asyncio.sleep(.01)
        try:
            return request_only_handler(request)
        finally:
            active.remove(material["role"])
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(transport),), simulated_enabled=True)
    try:
        plan = await host.plan(selected(adapter_foundation, metadata=metadata))
        assert plan["guide"] == builtin_analysis_guides()[0].model_dump()
        journal = host.db.execute("SELECT data FROM agent_host_plans").fetchone()[0]
        assert "instructions" not in journal and "Tool JSON schema" not in journal
        assert [item["path"] for item in plan["selected_metadata"]] == selected(adapter_foundation, metadata=metadata)["selected_metadata_paths"]
        job = await completed(host, await start(host, plan))
        assert job["state"] == "succeeded" and len(seen) == 4 and peak == 2
        assert sum(item[1] for item in seen) == sum(call["transfer_bytes"] for call in job["calls"])
        assert all(item[0]["selected_metadata"] == plan["selected_metadata"] for item in seen)
        assert all(item[0]["recipe"] == plan["recipe"] for item in seen)
        assert (job["candidate"] is not None) == metadata
        if metadata:
            adopted = await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
            assert adopted["draft"]["source"] == draft["source"] and adopted["draft"]["revision"] == 2
            assert preparations.list() == [] and adopted["draft"]["attachment"] is None
        else:
            assert imports.inspect(draft["draft_id"])["revision"] == 1
    finally:
        await host.close()


@pytest.mark.parametrize("mutation", ["metadata", "guide", "scope"])
async def test_reviewed_facts_guidance_and_disclosure_cannot_change_before_start(adapter_foundation, tmp_path, mutation):
    _, _, preparations, draft, *_ = adapter_foundation
    seen = []
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(lambda req: seen.append(req)),), simulated_enabled=True)
    try:
        plan = await host.plan(selected(adapter_foundation))
        if mutation == "metadata":
            (Path(draft["source"]["snapshot_path"]) / "uv.lock").write_text("version=2\n")
        elif mutation == "guide":
            guide = builtin_analysis_guides()[0].model_dump()
            guide["roles"][0]["instructions"] += " changed"
            host.guides = AnalysisGuideRegistry((AnalysisGuideV1.model_validate(guide),))
        else:
            saved = host._plan(plan["plan_id"]); saved["selected_metadata"].pop()
            host.db.execute("UPDATE agent_host_plans SET data=? WHERE id=?", (json.dumps(saved), plan["plan_id"]))
        with pytest.raises(PreparationError):
            await start(host, plan)
        assert not seen
    finally:
        await host.close()


async def test_metadata_mutation_after_joined_byte_validation_stops_first_dispatch(adapter_foundation, tmp_path, monkeypatch):
    _, _, preparations, draft, *_ = adapter_foundation
    seen = []
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(lambda req: seen.append(req)),), simulated_enabled=True)
    try:
        plan = await host.plan(selected(adapter_foundation))
        original = host._selected_bytes
        def mutate(*args):
            result = original(*args)
            (Path(draft["source"]["snapshot_path"]) / "uv.lock").write_text("version=2\n")
            return result
        monkeypatch.setattr(host, "_selected_bytes", mutate)
        job = await completed(host, await start(host, plan))
        assert job["state"] == "failed" and not seen
    finally:
        await host.close()


async def test_guidance_bytes_are_in_actual_transfer_budget(adapter_foundation, tmp_path):
    _, _, preparations, *_ = adapter_foundation
    seen = []
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(lambda req: seen.append(req)),), simulated_enabled=True)
    try:
        scope = selected(adapter_foundation); scope["limits"] = {"max_transfer_bytes": 1}
        plan = await host.plan(scope)
        job = await completed(host, await start(host, plan))
        assert job["state"] == "failed" and not seen and job["calls"] == []
    finally:
        await host.close()


async def test_large_original_lock_discloses_only_reviewed_identity(adapter_foundation, tmp_path):
    imports, _, preparations, _, *_ = adapter_foundation
    source = tmp_path / "large-lock"; source.mkdir()
    for name in ("worker.py", "LICENSE", "pyproject.toml", "uv.lock"):
        (source / name).write_bytes((tmp_path / "source" / name).read_bytes())
    (source / "uv.lock").write_bytes(b"version=1\n" + b"# private lock contents omitted\n" * 1000)
    draft = imports.create(source, name="Large original lock", intent="Wrap the selected scalar")
    foundation = (*adapter_foundation[:3], draft, *adapter_foundation[4:])
    calls = []
    def transport(request):
        assert b"private lock contents omitted" not in request.content
        calls.append(len(request.content))
        return request_only_handler(request)
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(transport),), simulated_enabled=True)
    try:
        plan = await host.plan(selected(foundation))
        assert next(row for row in plan["selected_metadata"] if row["path"] == "uv.lock")["bytes"] > 16384
        job = await completed(host, await start(host, plan))
        assert job["candidate"] and len(calls) == 4
    finally:
        await host.close()


async def test_cancel_dependency_graph_joins_only_dispatched_calls(adapter_foundation, tmp_path):
    _, _, preparations, *_ = adapter_foundation
    fixture_started = asyncio.Event()
    held, settled = set(), set()
    async def transport(request):
        role = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])["role"]
        if role == "interface":
            return request_only_handler(request)
        held.add(role)
        if role == "fixtures":
            fixture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.add(role)
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(transport),), simulated_enabled=True)
    try:
        plan = await host.plan(selected(adapter_foundation))
        job = await start(host, plan)
        await asyncio.wait_for(fixture_started.wait(), 2)
        ended = await host.cancel(job["job_id"])
        assert ended["state"] == "canceled" and ended["local_io_settled"]
        assert held == settled == {"dependencies", "fixtures"}
        assert [call["role"] for call in ended["calls"]] == ["interface", "dependencies", "fixtures"]
        assert ended["candidate"] is None and not host.tasks
    finally:
        await host.close()


def test_unregistered_component_cannot_inherit_published_guide():
    guide = builtin_analysis_guides()[0]
    recipe = {"manifest": {"steps": [{"component": guide.component.id, "version": guide.component.version,
        "digest": "f" * 64}]}}
    assert AnalysisGuideRegistry((guide,)).resolve(recipe) is None


@pytest.mark.parametrize("payload", [b"binary_identity_content_marker\0asset", b"\xff\xfe\x81"])
@pytest.mark.parametrize("disclosure", ["identity", "content", "changed-identity"])
async def test_binary_identity_is_proved_without_content_disclosure(adapter_foundation, tmp_path, payload, disclosure):
    imports, _, preparations, _, *_ = adapter_foundation
    source = tmp_path / "binary-identity"; source.mkdir()
    for name in ("worker.py", "LICENSE", "pyproject.toml", "uv.lock"):
        (source / name).write_bytes((tmp_path / "source" / name).read_bytes())
    (source / "unknown.asset").write_bytes(payload)
    draft = imports.create(source, name="Binary identity", intent="Review scalar behavior", source_mode="automatic")
    foundation = (*adapter_foundation[:3], draft, *adapter_foundation[4:])
    seen = []
    def transport(request):
        assert b"binary_identity_content_marker" not in request.content
        material = json.loads(json.loads(request.content)["input"][0]["content"][0]["text"])
        assert set(material["selected_source"]) == {"worker.py"}
        assert next(row for row in material["selected_metadata"] if row["path"] == "unknown.asset") == {
            "path": "unknown.asset", "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        seen.append(material["role"])
        return request_only_handler(request)
    host = AgentPreparationHost(preparations, tmp_path / "agent", providers=(provider(transport),), simulated_enabled=True)
    try:
        options = host.options(draft["draft_id"], 1)
        assert "unknown.asset" in {row["path"] for row in options["metadata_files"]}
        scope = selected(foundation)
        if disclosure == "content":
            scope["selected_paths"] = ["unknown.asset", "worker.py"]
            with pytest.raises(PreparationError, match="agent_source_identity_mismatch"):
                await host.plan(scope)
        else:
            scope["selected_metadata_paths"] = sorted(scope["selected_metadata_paths"] + ["unknown.asset"])
            plan = await host.plan(scope)
            if disclosure == "changed-identity":
                (Path(draft["source"]["snapshot_path"]) / "unknown.asset").write_bytes(payload + b"changed")
                with pytest.raises(PreparationError, match="agent_source_identity_mismatch"):
                    await start(host, plan)
            else:
                job = await completed(host, await start(host, plan))
                assert job["state"] == "succeeded" and len(seen) == 4 and job["candidate"] is not None
        assert (len(seen) == 4) == (disclosure == "identity")
        assert imports.inspect(draft["draft_id"])["revision"] == 1 and preparations.list() == []
    finally:
        await host.close()


@pytest.mark.parametrize("field,value", [("selected_metadata_paths", ["worker.py"]),
    ("selected_metadata_paths", ["uv.lock", "LICENSE"]), ("selected_metadata_paths", ["missing"]),
    ("guide", {})])
async def test_metadata_selection_is_closed(adapter_foundation, tmp_path, field, value):
    _, _, preparations, *_ = adapter_foundation
    host = AgentPreparationHost(preparations, tmp_path / "agent")
    # A provider configuration is necessary to reach source validation.
    host.providers = {"configured-openai": provider(request_only_handler)}
    try:
        scope = selected(adapter_foundation); scope[field] = value
        with pytest.raises(PreparationError):
            await host.plan(scope)
    finally:
        await host.close()
