"""Opt-in real Python pipeline with simulated provider; no live A5 claim."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import httpx
import pytest

from meecp.imports import ImportStore
from meecp.preparation_agent_host import AgentPreparationHost
from meecp.preparation_contracts import canonical_bytes
from meecp.preparation_inspection import inspect_source
from meecp.preparation_inputs import parse_preparation_receipt
from meecp.preparation_resolution import ResolutionStore
from meecp.preparation_resolution_runner import OwnedResolutionRunner
from meecp.preparation_resolved_python import ResolvedPythonComponent
from meecp.preparation_runner import PreparationRunner
from meecp.preparation_verifier import verify_prepared_package
from meecp.contracts import validate_package
from test_preparation_resolution import policy
from test_preparation_derived_build import resolve_form
from test_preparation_agent_host import provider, handler_for, completed


@pytest.mark.skipif(not os.environ.get("MCPME_AGENT_PIPELINE_HOST"), reason="requires exact private host; simulated provider only")
async def test_real_questions_build_sdk_probe_attach_after_simulated_agent_adoption(tmp_path):
    private_host = Path(os.environ["MCPME_AGENT_PIPELINE_HOST"]).resolve()
    # Verify the supplied trusted runtime payload externally before target code
    # launches. Integration modules run from this source checkpoint, not claimed
    # to be present in the supplied older host payload.
    import importlib.util
    helper_path = Path(__file__).resolve().parents[1] / "scripts/verify_phase3_native_onboarding.py"
    spec = importlib.util.spec_from_file_location("agent_private_payload_check", helper_path)
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    sys.path.insert(0, str(helper_path.parent))
    try:
        spec.loader.exec_module(helper)
    finally:
        sys.path.pop(0)
    before = helper.verify_host_payload(private_host)
    root = tmp_path.resolve()
    source = root / "unfamiliar-source"; source.mkdir()
    (source / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n return {'value': n*2}\n")
    (source / "LICENSE").write_text("Synthetic permission notice.\n")
    assert not (source / "pyproject.toml").exists() and not (source / "uv.lock").exists()
    receipt_path = Path(os.environ.get("MCPME_AGENT_PIPELINE_RECEIPT", root / "agent-pipeline-receipt.json"))
    with ImportStore(root / "state") as imports:
        draft = imports.create(source, name="Unfamiliar synthetic directory", intent="Expose the selected double callable and a synthetic behavior fixture")
        source_identity = json.loads(json.dumps(draft["source"]))
        candidate = next(item for item in inspect_source(draft)["candidates"] if item["kind"] == "python-callable")
        resolutions = ResolutionStore(imports)
        resolver = OwnedResolutionRunner(resolutions, private_host, private_host / "bin/meecp-reap", enabled=True)
        from meecp.recipes import RecipeStore
        from meecp.preparations import PreparationStore
        from meecp.preparation_forms import PreparationForms
        from meecp.preparation_builtin_recipes import _catalog, CATALOG_ROOT
        recipes = RecipeStore(imports)
        entry = next(item for item in _catalog().recipes if item.recipe_id == "mcpme.resolved-python")
        registered = recipes.add(CATALOG_ROOT / entry.directory)
        assert registered["digest"] == entry.digest
        registered = recipes.enable(registered["recipe_id"], registered["version"])
        store = PreparationStore(imports, recipes, resolutions=resolutions)
        forms = PreparationForms(store, preferred_recipes=[{"id": registered["recipe_id"], "version": registered["version"], "digest": registered["digest"]}])
        builder = PreparationRunner(store, private_host, private_host / "bin/meecp-reap", enabled=True,
            enabled_families=frozenset({ResolvedPythonComponent().reference.key}))
        agent = None
        try:
            resolution_plan = await resolver.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
            resolution_job = await resolver.start(resolution_plan["plan_id"], resolution_plan["plan_digest"], str(uuid.uuid4()))
            await resolver.tasks[resolution_job["job_id"]]
            resolved = resolutions.get(resolution_job["job_id"])
            assert resolved["state"] == "succeeded" and resolved["ownership_settled"] and not resolved["reservation_retained"]
            descriptor, _ = await asyncio.to_thread(resolve_form, forms, draft, candidate, resolved["result_digest"])
            raw = descriptor.model_dump(mode="json", exclude_none=True)
            agent = AgentPreparationHost(store, root / "agent-state", providers=(provider(handler_for(raw)),), simulated_enabled=True)
            scope = await agent.plan({"import_id": draft["draft_id"], "revision": draft["revision"], "recipe": raw["recipe"],
                "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["worker.py"], "limits": {}})
            job = await agent.start({"action": "analyze", "plan_id": scope["plan_id"], "plan_digest": scope["plan_digest"],
                "idempotency_key": str(uuid.uuid4())})
            job = await completed(agent, job)
            assert job["state"] == "succeeded" and job["candidate"] is not None and len(job["calls"]) == 4
            adopted = await agent.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
            draft = adopted["draft"]
            assert draft["revision"] == 2 and draft["attachment"] is None
            assert draft["source"] == source_identity and store.list() == []
            # Actual host-generated questions restore the admitted descriptor.
            context = await asyncio.to_thread(lambda: forms.context(draft["draft_id"], 2,
                candidate_id=candidate["candidate_id"], recipe=raw["recipe"]))
            assert not context.form.blockers and context.form.resolution_digest == resolved["result_digest"]
            value, _ = await asyncio.to_thread(lambda: forms.resolve(draft["draft_id"], 2,
                candidate_id=context.form.candidate_id, recipe=context.form.recipe.model_dump(), form_digest=context.form.form_digest,
                answers={}, resolution_digest=context.form.resolution_digest))
            assert value.digest == descriptor.digest
            ordinary = await asyncio.to_thread(store.select_descriptor, draft["draft_id"], 2,
                value.model_dump(mode="json", exclude_none=True), candidate_id=candidate["candidate_id"],
                form_digest=context.form.form_digest, resolution_digest=context.form.resolution_digest)
            assert ordinary["revision"] == 2
            shutil.rmtree(source)
            plan = await builder.plan(draft["draft_id"], 2, value.model_dump(mode="json", exclude_none=True))
            build_job = await builder.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
            await builder.tasks[build_job["job_id"]]
            built = store.get(build_job["job_id"])
            assert built["state"] == "succeeded" and built["ownership_settled"] and not built["reservation_retained"]
            attachment = imports.inspect(draft["draft_id"])["attachment"]
            package_root = Path(attachment["path"])
            actual = parse_preparation_receipt((package_root / "meecp.preparation.json").read_bytes())
            assert actual.binding.revision == 2 and actual.binding.descriptor_digest == descriptor.digest
            moved = root / "relocated-package"; shutil.copytree(package_root, moved)
            shutil.rmtree(package_root)
            package = validate_package(moved)
            probe = root / "probe"; probe.mkdir()
            from meecp.processes import ProcessOwner
            probe_owner = ProcessOwner(private_host / "bin/meecp-reap")
            report = await verify_prepared_package(moved, value,
                private_host / "bin/meecp-reap", probe, timeout_seconds=30, owner=probe_owner)
            assert report.package_digest == package.digest and report.fixtures_checked == report.tools_checked == 1
            # This legacy report has no owner-count fields. Its verifier returns
            # only after exact ProcessOwner cleanup; inspect the actual ledger.
            assert not probe_owner.instances
            assert not probe_owner._launches and not probe_owner._pending_ids
            assert not agent.tasks and not agent.children and not agent.static_workers
            # These runners retain completed task handles for exact lifecycle
            # inspection; settlement requires every owned task joined, not an
            # empty historical task map.
            await builder.close(); await resolver.close()
            assert all(task.done() for task in (*builder.tasks.values(), *resolver.tasks.values()))
            assert not store.pending_ownership(build_job["job_id"])
            after = helper.verify_host_payload(private_host)
            assert before == after
            evidence = {"schema_version": 1, "provider_simulated": True, "live_provider_calls": 0,
                "source_only_integration": True, "scope_digest": scope["plan_digest"], "proposal_digest": job["proposal_digest"],
                "descriptor_digest": descriptor.digest, "recipe": raw["recipe"], "source_revision": source_identity["revision"],
                "reviewed_attached_revision": actual.binding.revision, "actual_provider_transport_calls": len(job["calls"]),
                "generated_question_count": len(context.form.fields), "package_digest": package.digest,
                "tools_checked": report.tools_checked, "fixtures_checked": report.fixtures_checked,
                "commands_finished": built["progress"]["commands_finished"], "source_removed": not source.exists(),
                "original_attachment_removed": not package_root.exists(), "relocation_probe_passed": True,
                "all_owners_joined": True, "host_source_commit": before["source_commit"],
                "host_artifact_count": len(before["artifact_sha256"]),
                "host_build_sha256": hashlib.sha256((private_host / "build.json").read_bytes()).hexdigest(),
                "a5_scope_complete": False}
            payload = canonical_bytes(evidence)
            fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(payload); output.flush(); os.fsync(output.fileno())
        finally:
            if agent is not None:
                await agent.close()
            await builder.close(); await resolver.close()
