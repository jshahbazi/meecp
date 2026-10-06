"""Opt-in unlocked directory, explicit resolution and actual confined SDK."""
import asyncio
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
import tomllib
import uuid

import pytest

from mcpme.preparation_agent_unlocked import COMPONENT_ID, component_catalog
from mcpme.preparation_agent_unlocked_contracts import DEPENDENCY_NAMESPACE
from mcpme.preparation_agent_unlocked_resolution import OwnedAgentUnlockedResolutionRunner
from mcpme.preparation_agent_adapter_store import CODE_PATH
from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_contracts import PreparationError, canonical_bytes
from mcpme.preparation_inputs import parse_preparation_receipt
from mcpme.preparation_runner import PreparationRunner
from mcpme.preparation_verifier import verify_prepared_package
from mcpme.processes import ProcessOwner
from mcpme.supervisor import Supervisor
from mcpme.contracts import validate_package
from test_preparation_agent_unlocked_request_readiness import request_only_unlocked_handler
from test_preparation_agent_host import provider, completed


@pytest.mark.skipif(not os.environ.get("MCPME_AGENT_UNLOCKED_HOST"), reason="requires exact private host and composed trusted source")
async def test_unlocked_adapter_explicit_resolution_confined_build_attach_and_relocation():
    assert "confine_private_python" in inspect.signature(verify_prepared_package).parameters
    host_root = Path(os.environ["MCPME_AGENT_UNLOCKED_HOST"]).resolve()
    composition_path = Path(os.environ["MCPME_AGENT_UNLOCKED_COMPOSITION"])
    composition = json.loads(composition_path.read_bytes())
    source_root = Path(__file__).resolve().parents[1]
    assert Path(composition["overlay"]) == source_root
    source_hashes = {name: hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in composition["files"]}
    assert source_hashes == composition["files"]
    helper_path = source_root/"scripts/verify_phase3_native_onboarding.py"
    spec = importlib.util.spec_from_file_location("unlocked_payload", helper_path)
    helper = importlib.util.module_from_spec(spec); sys.modules[spec.name] = helper
    sys.path.insert(0, str(helper_path.parent))
    try:
        spec.loader.exec_module(helper)
    finally:
        sys.path.pop(0)
    before = helper.verify_host_payload(host_root)
    retained = Path(tempfile.mkdtemp(prefix="mcpme-agent-unlocked-pipeline-")).resolve(); retained.chmod(0o700)
    source = retained/"unfamiliar-source"; source.mkdir(mode=0o700)
    (source/"worker.py").write_text("import humanize\ndef double(n):\n return int(humanize.intcomma(n*2).replace(',', ''))\n")
    (source/"LICENSE").write_text("Synthetic original-code permission.\n")
    (source/"README.md").write_text("Unfamiliar scalar application using humanize.\n")
    (source/"unknown.payload").write_bytes(b"\x00\xffsynthetic\n")
    configured = provider(request_only_unlocked_handler)
    def preparation_factory(*args, **kwargs):
        selected = next(value for value in args[0].families.values() if value.component.reference.id == COMPONENT_ID)
        kwargs.update(enabled=True, enabled_families=frozenset({selected.component.reference.key}))
        return PreparationRunner(*args, **kwargs)
    def resolution_factory(*args, **kwargs):
        kwargs["enabled"] = True
        return OwnedAgentUnlockedResolutionRunner(*args, **kwargs)
    def agent_factory(*args, **kwargs):
        return AgentPreparationHost(*args, **kwargs, providers=(configured,), simulated_enabled=True)
    host = None; phase = "intake"; started = time.monotonic()
    evidence = {"schema_version": 1, "provider_simulated": True, "live_provider_calls": 0,
        "source_only_integration": True, "original_project_present": False, "original_lock_present": False,
        "common_source_commit": composition["common_source_commit"], "composition_sha256": hashlib.sha256(composition_path.read_bytes()).hexdigest(),
        "source_artifacts_checked": len(source_hashes), "retained_directory": str(retained),
        "a5_scope_complete": False, "hard_aggregate_resource_enforced": False, "all_owners_joined": False}
    try:
        phase = "public_denials"
        public = Supervisor(retained/"public-state",host_root/"bin/mcpme-reap")
        try:
            assert public.preparation_runner.enabled is False and public.resolution_runner.enabled is False
            assert not public.agent_preparations.providers
            with pytest.raises(PreparationError,match="resolution_runner_unavailable"):
                await public.resolution_runner.plan(str(uuid.uuid4()),1,"0"*64,{})
            assert not public.resolutions.list() and not public.preparations.list()
        finally:
            await public.close()
        evidence["public_execution_defaults_closed"] = True
        phase = "intake"
        host = Supervisor(retained/"state", host_root/"bin/mcpme-reap", preparation_runner_factory=preparation_factory,
            resolution_runner_factory=resolution_factory, agent_preparation_factory=agent_factory)
        family = next(value for value in host.preparations.families.values() if value.component.reference.id == COMPONENT_ID)
        assert family.component.reference.digest == component_catalog()[COMPONENT_ID+"@1.0.1"]["digest"]
        record = next(value for value in host.recipes.list() if value["recipe_id"] == "mcpme.agent-unlocked-python-adapter")
        assert record["state"] == "enabled"
        draft = host.imports.create(source, name="Unlocked scalar app", intent="Wrap the selected scalar as a structured result", source_mode="automatic")
        original_source = json.loads(json.dumps(draft["source"]))
        assert not host.preparations.inspect_source(draft)["candidates"]
        phase = "agent"
        plan = await host.agent_preparations.plan({"import_id": draft["draft_id"], "revision": 1,
            "recipe": {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]},
            "provider_id": configured.provider_id, "model": "explicit-test-model", "selected_paths": ["worker.py"],
            "selected_metadata_paths": ["LICENSE"], "limits": {}})
        evidence.update(request_only_provider_simulation=True, guide_digest=plan["guide_digest"],
            metadata_only_files=len(plan["selected_metadata"]), selected_content_files=len(plan["selected_files"]))
        job = await host.agent_preparations.start({"action": "analyze", "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        job = await completed(host.agent_preparations, job)
        assert job["state"] == "succeeded" and job["candidate"] is None and len(job["calls"]) == 4
        assert not host.resolutions.list() and not host.preparations.list()
        pending = await asyncio.to_thread(host.preparation_forms.context, draft["draft_id"], 1,
            execution_available=True, resolution_available=True)
        assert pending.form.resolution_required and not pending.form.fields
        assert pending.form.blockers == ["dependency_resolution_required"]
        assert len(pending.form.recipe_choices) == 1 and pending.form.recipe.id == record["recipe_id"]
        phase = "resolution"
        resolution_plan = await host.resolution_runner.plan(draft["draft_id"],1,pending.form.candidate_id,
            pending.form.resolution_policy)
        assert resolution_plan["request"]["producer"]["id"] == "python-agent-resolution-v1"
        resolving = await host.resolution_runner.start(resolution_plan["plan_id"],resolution_plan["plan_digest"],str(uuid.uuid4()))
        await host.resolution_runner.tasks[resolving["job_id"]]
        resolved = host.resolutions.get(resolving["job_id"])
        evidence.update(resolution_state=resolved["state"], resolution_error_code=resolved["error_code"],
            resolution_commands_finished=resolved["progress"]["commands_finished"])
        assert resolved["state"] == "succeeded" and resolved["ownership_settled"] and not resolved["reservation_retained"]
        assert not host.resolution_reservations and not host.preparations.list()
        assert host.imports.inspect(draft["draft_id"])["revision"] == 1 and not host.imports.inspect(draft["draft_id"])["attachment"]
        assert not (Path(original_source["snapshot_path"])/"pyproject.toml").exists()
        assert not (Path(original_source["snapshot_path"])/"uv.lock").exists()
        phase = "questions"
        context = await asyncio.to_thread(host.preparation_forms.context,draft["draft_id"],1,
            resolution_digest=resolved["result_digest"], execution_available=True, resolution_available=True)
        assert not context.form.blockers and not context.form.resolution_required and len(context.form.fields) == 4
        descriptor, _ = await asyncio.to_thread(host.preparation_forms.resolve,draft["draft_id"],1,
            candidate_id=context.form.candidate_id,recipe=context.form.recipe.model_dump(),form_digest=context.form.form_digest,
            resolution_digest=resolved["result_digest"],execution_available=True,resolution_available=True,answers={})
        draft = await asyncio.to_thread(host.preparations.select_descriptor,draft["draft_id"],1,descriptor,
            candidate_id=context.form.candidate_id, form_digest=context.form.form_digest, resolution_digest=resolved["result_digest"])
        assert draft["revision"] == 2 and draft["source"] == original_source and not draft["attachment"]
        assert not (Path(original_source["snapshot_path"])/CODE_PATH).exists()
        shutil.rmtree(source)
        evidence["source_removed"] = True
        phase = "build"
        review = await host.preparation_runner.plan(draft["draft_id"],2,descriptor.model_dump(mode="json",exclude_none=True))
        building = await host.preparation_runner.start(review["plan_id"],review["plan_digest"],str(uuid.uuid4()))
        await host.preparation_runner.tasks[building["job_id"]]
        built = host.preparations.get(building["job_id"])
        evidence.update(job_state=built["state"],error_code=built["error_code"],commands_finished=built["progress"]["commands_finished"],
            job_id=building["job_id"],import_id=draft["draft_id"])
        assert built["state"] == "succeeded" and built["ownership_settled"] and not built["reservation_retained"]
        assert not host.preparation_reservations and not host.resolution_reservations
        attachment = host.imports.inspect(draft["draft_id"])["attachment"]
        package_root = Path(attachment["path"]); package = validate_package(package_root)
        receipt = parse_preparation_receipt((package_root/"mcpme.preparation.json").read_bytes())
        dependency_root = package_root/"source"/DEPENDENCY_NAMESPACE
        dependency_project = tomllib.loads((dependency_root/"pyproject.toml").read_text())
        assert "humanize" in dependency_project["project"]["dependencies"]
        locked_packages = tomllib.loads((dependency_root/"uv.lock").read_text())["package"]
        resolved_humanize = [item for item in locked_packages if item["name"] == "humanize"]
        assert len(resolved_humanize) == 1 and isinstance(resolved_humanize[0]["version"], str)
        evidence.update(dependency_requirement_from_request="humanize",
            dependency_version_selected_by_ordinary_resolution=resolved_humanize[0]["version"])
        assert receipt.binding.revision == 2 and receipt.binding.descriptor_digest == descriptor.digest
        assert receipt.inputs.source.revision == original_source["revision"] and len(receipt.inputs.derived_inputs) == 5
        assert (package_root/"source/unknown.payload").read_bytes() == b"\x00\xffsynthetic\n"
        for name in (CODE_PATH,DEPENDENCY_NAMESPACE+"/pyproject.toml",DEPENDENCY_NAMESPACE+"/uv.lock"):
            artifact = next(item for item in package.lock.artifacts if item.path == "source/"+name)
            assert artifact.source.startswith("agent-derived:sha256:")
        phase = "settlement"
        moved = retained/"relocated-package"; shutil.copytree(package_root,moved)
        (retained/"descriptor.json").write_bytes(canonical_bytes(descriptor.model_dump(mode="json",exclude_none=True)))
        (retained/"descriptor.json").chmod(0o600)
        evidence.update(package_digest=package.digest,descriptor_digest=descriptor.digest,proposal_digest=job["proposal_digest"],
            resolution_result_digest=resolved["result_digest"],resolver=resolved["request"]["producer"],
            original_source_revision=original_source["revision"],reviewed_attached_revision=2,
            actual_provider_transport_calls=4,generated_question_count=4,recipe=descriptor.recipe.model_dump(),
            component=family.component.reference.model_dump(),confined_managed_probe_passed=True)
        await host.agent_preparations.close()
        await host.preparation_runner.close()
        await host.resolution_runner.close()
        assert not host.agent_preparations.tasks and not host.agent_preparations.static_workers
        assert not host.agent_unlocked_inputs.workers and not host.preparations.pending_ownership(building["job_id"])
        assert not host.reservations and not host.preparation_reservations and not host.resolution_reservations
        await host.close()
        host = None
        shutil.rmtree(retained/"state")
        phase = "relocation"
        probe = retained/"probe"; probe.mkdir(mode=0o700)
        owner = ProcessOwner(host_root/"bin/mcpme-reap")
        report = await verify_prepared_package(moved,descriptor,host_root/"bin/mcpme-reap",probe,
            timeout_seconds=30,owner=owner,confine_private_python=True)
        assert report.tools_checked == report.fixtures_checked == 1 and report.package_digest == package.digest
        assert not owner.instances and not owner._launches and not owner._pending_ids
        assert helper.verify_host_payload(host_root) == before
        assert {name:hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in composition["files"]} == source_hashes
        evidence.update(passed=True,stage="complete",all_owners_joined=True,source_removed=True,original_state_removed=True,
            relocation_confined_probe_passed=True,tools_checked=1,fixtures_checked=1,host_artifact_count=len(before["artifact_sha256"]),
            host_source_commit=before["source_commit"],host_build_sha256=hashlib.sha256((host_root/"build.json").read_bytes()).hexdigest())
    finally:
        shutdown_error = None
        if host is not None:
            try:
                await host.close()
                evidence["owned_shutdown_completed"] = True
            except BaseException as error:
                shutdown_error = error
                evidence.update(shutdown_error_type=type(error).__name__,shutdown_error_code=getattr(error,"code",None))
        evidence.update(elapsed_milliseconds=round((time.monotonic()-started)*1000))
        if not evidence.get("passed"):
            evidence.update(passed=False,stage=phase)
        receipt_path = Path(os.environ.get("MCPME_AGENT_UNLOCKED_RECEIPT",retained/"receipt.json"))
        fd = os.open(receipt_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"wb") as stream:
            stream.write(canonical_bytes(evidence)); stream.flush(); os.fsync(stream.fileno())
        if shutdown_error is not None:
            raise shutdown_error
