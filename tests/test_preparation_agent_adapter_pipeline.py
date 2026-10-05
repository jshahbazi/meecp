"""Opt-in ordinary confined pipeline for a required adapter; simulated provider."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

import pytest

from mcpme.imports import ImportStore
from mcpme.recipes import RecipeStore
from mcpme.preparations import PreparationStore
from mcpme.preparation_agent_adapter import AgentPythonAdapterFamily, component_catalog
from mcpme.preparation_agent_adapter_store import AgentAdapterDerivationStore, CODE_PATH
from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_contracts import canonical_bytes
from mcpme.preparation_forms import PreparationForms
from mcpme.preparation_inspection import inspect_source
from mcpme.preparation_inputs import parse_preparation_receipt
from mcpme.preparation_runner import PreparationRunner
from mcpme.preparation_verifier import verify_prepared_package
from mcpme.processes import ProcessOwner
from mcpme.contracts import validate_package
from test_preparation_agent_request_readiness import request_only_handler
from test_preparation_agent_host import provider, completed


@pytest.mark.skipif(not os.environ.get("MCPME_AGENT_ADAPTER_HOST"), reason="requires exact private host and common confinement source")
async def test_required_generated_adapter_real_confined_build_probe_attach_and_relocation(tmp_path, monkeypatch):
    assert "confine_private_python" in inspect.signature(verify_prepared_package).parameters
    from mcpme import recipes as recipe_module
    original_catalog = recipe_module.builtin_components
    monkeypatch.setattr(recipe_module, "builtin_components", lambda: {**original_catalog(), **component_catalog()})
    host_root = Path(os.environ["MCPME_AGENT_ADAPTER_HOST"]).resolve()
    helper_path = Path(__file__).resolve().parents[1] / "scripts/verify_phase3_native_onboarding.py"
    spec = importlib.util.spec_from_file_location("agent_adapter_payload", helper_path)
    helper = importlib.util.module_from_spec(spec); sys.modules[spec.name] = helper
    sys.path.insert(0, str(helper_path.parent))
    try:
        spec.loader.exec_module(helper)
    finally:
        sys.path.pop(0)
    before = helper.verify_host_payload(host_root)
    composition_path = os.environ.get("MCPME_AGENT_ADAPTER_COMPOSITION")
    assert composition_path
    from mcpme.private import read_private_json
    composition = read_private_json(Path(composition_path))
    source_root = Path(__file__).resolve().parents[1]
    assert Path(composition["overlay"]) == source_root
    source_hashes = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in composition["files"]}
    assert source_hashes == composition["files"]
    composition_digest = hashlib.sha256(Path(composition_path).read_bytes()).hexdigest()
    retained = Path(tempfile.mkdtemp(prefix="mcpme-agent-adapter-pipeline-")).resolve()
    retained.chmod(0o700)
    source = retained / "unfamiliar-source"; source.mkdir(mode=0o700)
    (source / "worker.py").write_text("def double(n):\n return n*2\n")
    (source / "LICENSE").write_text("Synthetic original-code permission.\n")
    # This first fixture is a normal already locked app. It does not stand in
    # for governed dependency derivation in an unfamiliar unlocked directory.
    for name in ("pyproject.toml", "uv.lock"):
        (source / name).write_bytes((host_root / "preparation-toolchain/templates/text-generate-v1" / name).read_bytes())
    (source / "unknown.payload").write_bytes(b"\x00\xffsynthetic\n")
    receipt_path = Path(os.environ.get("MCPME_AGENT_ADAPTER_RECEIPT", retained / "receipt.json"))
    started = time.monotonic()
    builder = agent = None
    @asynccontextmanager
    async def settled_imports(path):
        with ImportStore(path) as owned:
            try:
                yield owned
            finally:
                # The database stays open until its producers and owners join.
                if agent is not None:
                    await agent.close()
                if builder is not None:
                    await builder.close()
    phase = "intake"
    evidence = {"schema_version": 1, "provider_simulated": True, "live_provider_calls": 0,
        "source_only_integration": True, "locked_source": True, "unlocked_source_supported": False,
        "common_source_commit": composition["common_source_commit"], "composition_sha256": composition_digest,
        "source_artifacts_checked": len(source_hashes),
        "a5_scope_complete": False, "hard_aggregate_resource_enforced": False,
        "retained_directory": str(retained), "all_owners_joined": False}
    try:
        async with settled_imports(retained / "state") as imports:
            recipes = RecipeStore(imports)
            record = recipes.add(Path(__file__).resolve().parents[1] / "templates/builtin-recipes/agent-python-adapter-v1-0-0")
            record = recipes.enable(record["recipe_id"], record["version"])
            draft = imports.create(source, name="Unknown scalar app", intent="Wrap the selected scalar callable as a structured result")
            original_source = json.loads(json.dumps(draft["source"]))
            assert not inspect_source(draft)["candidates"]
            derivations = AgentAdapterDerivationStore(imports, recipes)
            family = AgentPythonAdapterFamily(derivations)
            preparations = PreparationStore(imports, recipes, families=(family,))
            builder = PreparationRunner(preparations, host_root, host_root / "bin/mcpme-reap", enabled=True,
                enabled_families=frozenset({family.component.reference.key}))
            agent = AgentPreparationHost(preparations, retained / "agent", providers=(provider(request_only_handler),), simulated_enabled=True)
            phase = "agent"
            plan = await agent.plan({"import_id": draft["draft_id"], "revision": 1,
                "recipe": {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]},
                "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["worker.py"],
                "selected_metadata_paths": ["LICENSE", "pyproject.toml", "uv.lock"], "limits": {}})
            evidence.update(request_only_provider_simulation=True,
                guide_digest=plan["guide_digest"], metadata_only_files=len(plan["selected_metadata"]),
                selected_content_files=len(plan["selected_files"]))
            job = await agent.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
                "idempotency_key": str(uuid.uuid4())})
            job = await completed(agent, job)
            assert job["state"] == "succeeded" and job["candidate"] and len(job["calls"]) == 4
            adopted = await agent.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
            draft = adopted["draft"]
            assert draft["revision"] == 2 and draft["source"] == original_source and not draft["attachment"]
            assert not (Path(original_source["snapshot_path"]) / CODE_PATH).exists() and not preparations.list()
            phase = "questions"
            forms = PreparationForms(preparations)
            context = await asyncio.to_thread(forms.context, draft["draft_id"], 2)
            assert not context.form.blockers and not context.form.resolution_required
            descriptor, _ = await asyncio.to_thread(forms.resolve, draft["draft_id"], 2, candidate_id=context.form.candidate_id,
                recipe=context.form.recipe.model_dump(), form_digest=context.form.form_digest, answers={})
            assert descriptor.digest == adopted["descriptor_digest"]
            ordinary = await asyncio.to_thread(preparations.select_descriptor, draft["draft_id"], 2, descriptor,
                candidate_id=context.form.candidate_id, form_digest=context.form.form_digest)
            assert ordinary["revision"] == 2
            shutil.rmtree(source)
            phase = "build"
            review = await builder.plan(draft["draft_id"], 2, descriptor.model_dump(mode="json", exclude_none=True))
            build = await builder.start(review["plan_id"], review["plan_digest"], str(uuid.uuid4()))
            await builder.tasks[build["job_id"]]
            built = preparations.get(build["job_id"])
            evidence.update(job_state=built["state"], error_code=built["error_code"], commands_finished=built["progress"]["commands_finished"],
                job_id=build["job_id"], import_id=draft["draft_id"])
            assert built["state"] == "succeeded" and built["ownership_settled"] and not built["reservation_retained"]
            attachment = imports.inspect(draft["draft_id"])["attachment"]
            package_root = Path(attachment["path"])
            package = validate_package(package_root)
            receipt = parse_preparation_receipt((package_root / "mcpme.preparation.json").read_bytes())
            assert receipt.binding.revision == 2 and receipt.binding.descriptor_digest == descriptor.digest
            assert receipt.inputs.source.revision == original_source["revision"] and len(receipt.inputs.derived_inputs) == 3
            generated = next(item for item in package.lock.artifacts if item.path == "source/" + CODE_PATH)
            assert generated.source.startswith("agent-derived:sha256:") and generated.license == "licenses/agent-generated-LICENSE.txt"
            phase = "settlement"
            await agent.close(); await builder.close()
            assert not agent.tasks and not agent.static_workers and not derivations.workers
            assert all(task.done() for task in builder.tasks.values()) and not preparations.pending_ownership(build["job_id"])
            moved = retained / "relocated-package"; shutil.copytree(package_root, moved)
            # Retain the candidate for independent review. Its original input
            # state can now be removed after every producer has joined.
            evidence.update(package_digest=package.digest, descriptor_digest=descriptor.digest,
                derivation_digest=receipt.inputs.derived_inputs[0].collection_digest,
                proposal_digest=job["proposal_digest"], original_source_revision=original_source["revision"],
                reviewed_attached_revision=2, actual_provider_transport_calls=4,
                generated_question_count=len(context.form.fields), recipe=descriptor.recipe.model_dump(),
                component=family.component.reference.model_dump(), confined_managed_probe_passed=True)
            (retained / "descriptor.json").write_bytes(canonical_bytes(descriptor.model_dump(mode="json", exclude_none=True)))
            (retained / "descriptor.json").chmod(0o600)
        shutil.rmtree(retained / "state")
        shutil.rmtree(retained / "agent")
        phase = "relocation"
        probe = retained / "probe"; probe.mkdir(mode=0o700)
        owner = ProcessOwner(host_root / "bin/mcpme-reap")
        report = await verify_prepared_package(moved, descriptor, host_root / "bin/mcpme-reap", probe,
            timeout_seconds=30, owner=owner, confine_private_python=True)
        assert report.tools_checked == report.fixtures_checked == 1 and report.package_digest == package.digest
        assert not owner.instances and not owner._launches and not owner._pending_ids
        assert helper.verify_host_payload(host_root) == before
        assert {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in composition["files"]} == source_hashes
        evidence.update(passed=True, stage="complete", all_owners_joined=True, source_removed=True,
            original_state_removed=True, relocation_confined_probe_passed=True, tools_checked=1, fixtures_checked=1,
            host_artifact_count=len(before["artifact_sha256"]), host_source_commit=before["source_commit"],
            host_build_sha256=hashlib.sha256((host_root / "build.json").read_bytes()).hexdigest(),
            elapsed_milliseconds=round((time.monotonic() - started)*1000))
    finally:
        if agent is not None:
            await agent.close()
        if builder is not None:
            await builder.close()
        evidence["all_owners_joined"] = (
            (agent is None or not agent.tasks and not agent.static_workers and not agent.children)
            and (builder is None or all(task.done() for task in builder.tasks.values())))
        evidence["frozen_host_unchanged"] = helper.verify_host_payload(host_root) == before
        if not evidence.get("passed"):
            evidence.update(passed=False, stage=phase, elapsed_milliseconds=round((time.monotonic()-started)*1000))
        payload = canonical_bytes(evidence)
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
