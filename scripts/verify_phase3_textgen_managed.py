"""Development acceptance for one fresh built host's actual captured LLM lane.

The Python 3.12 parent is a development verifier. Product code and all launched
commands come from the selected host and its verified private toolchain. Public
preparation admission is independently gated; this harness never enables it.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import uuid

from verify_phase3_primitives import VerificationError, _check_module_origins, _host_imports, _host_paths

STAGE = "bootstrap"
WALL_SECONDS = 300
DISK_BYTES = 4 * 1024**3
SAFE_STAGES = frozenset({"bootstrap", "host_hashes", "capture", "forms", "planning", "building", "relocation",
                        "removing_inputs", "relocation_probe", "installed_lifecycle", "closing"})


def stage(value):
    global STAGE
    STAGE = value if value in SAFE_STAGES else "bootstrap"


def check_origins(host):
    _check_module_origins(host)
    for name, module in tuple(sys.modules.items()):
        if name in {"mcpme_textgen", "jevk5_app"} or name.startswith(("mcpme_textgen.", "jevk5_app.")):
            path = getattr(module, "__file__", None)
            if not path or not Path(path).resolve().is_relative_to(host / "backend"):
                raise VerificationError("phase3_textgen_bundled_import_required")


def host_hashes(host):
    stage("host_hashes")
    data = json.loads((host / "build.json").read_bytes())
    hashes = data["artifact_sha256"]
    if data["source_tree_dirty"] is not False or not isinstance(hashes, dict) or not 1 <= len(hashes) <= 30000:
        raise VerificationError("phase3_textgen_frozen_host_required")
    for relative, expected in hashes.items():
        path = host / relative
        if (not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.resolve() != path or not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise VerificationError("phase3_textgen_host_identity_mismatch")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise VerificationError("phase3_textgen_host_identity_mismatch")
    return {"source_commit": data["source_commit"], "host_version": data["host_version"], "host_hashes_checked": len(hashes)}


def copy_selected_model(source, target, size, digest):
    from mcpme.preparation_builder import _opened
    from mcpme.preparation_contracts import PreparationError
    total, value = 0, hashlib.sha256()
    with _opened(source) as original, target.open("xb") as copied:
        while chunk := original.read(1024*1024):
            total += len(chunk)
            if total > size:
                raise PreparationError("phase3_textgen_model_identity_mismatch")
            copied.write(chunk); value.update(chunk)
    if total != size or value.hexdigest() != digest:
        raise PreparationError("phase3_textgen_model_identity_mismatch")


async def verify(host, model, base, *, source_mode="captured", install_lifecycle=False):
    _host_paths(host)
    receipt = await asyncio.to_thread(host_hashes,host)
    _host_imports(host)
    from mcpme.contracts import validate_package
    from mcpme.imports import ImportStore
    from mcpme.preparation_artifacts import AcquisitionRequestV1, ArtifactStore
    from mcpme.preparation_capture import (ArtifactCaptureCoordinator, ArtifactCaptureStore,
        CapturePolicyV1, CreateImportTargetV1, AttachImportTargetV1)
    from mcpme.preparation_contracts import ExecutionPolicy, PreparationError
    from mcpme.preparation_forms import PreparationForms
    from mcpme.preparation_textgen import (MODEL_BYTES, MODEL_SHA256, TextGenerationFamily, builtin_textgen_components,
                                     write_textgen_recipe)
    from mcpme.preparation_runner import PreparationRunner
    from mcpme.preparation_textgen_verifier import verify_textgen_package as verify_prepared_package
    from mcpme.preparations import PreparationStore
    from mcpme.preparations import RECEIPT_NAME
    from mcpme.preparation_inputs import parse_preparation_receipt
    from mcpme.processes import _await_ownership
    from mcpme.recipes import RecipeStore, builtin_components
    check_origins(host)
    imports = ImportStore(base / "state")
    artifacts = ArtifactStore(base / "state/captured-artifacts")
    capture_store = ArtifactCaptureStore(imports,artifacts)
    family = TextGenerationFamily(capture_store)
    recipes = RecipeStore(imports,component_catalog={**builtin_components(),**builtin_textgen_components()})
    store = PreparationStore(imports,recipes,families=(family,))
    reserved = set()
    runner = None
    async def files(function,*args,**kwargs):
        task = asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
        try:
            return await asyncio.shield(task)
        finally:
            await _await_ownership(asyncio.gather(task,return_exceptions=True))
    def admit(target,collection,capture_id):
        if isinstance(target, AttachImportTargetV1):
            draft = imports._admit_captured(collection, draft_id=target.import_id, revision=target.revision)
        else:
            draft = imports._admit_captured(collection,choices=target.model_dump(mode="json",exclude={"kind"}))
        capture_store._reconcile_draft_references()
        return {"import_id":draft["draft_id"],"revision":draft["revision"]}
    coordinator = ArtifactCaptureCoordinator(capture_store,admit_target=admit)
    async def reserve(job_id,policy):
        if policy.memory_mb != 2048 or policy.disk_bytes != DISK_BYTES:
            raise PreparationError("phase3_textgen_reservation_mismatch")
        reserved.add(job_id)
    async def release(job_id,*,ownership_settled):
        job = store.get(job_id)
        if not ownership_settled or not job["ownership_settled"] or job["reservation_retained"]:
            raise PreparationError("phase3_textgen_premature_release")
        reserved.remove(job_id)
    started = time.monotonic()
    try:
        async with asyncio.timeout(WALL_SECONDS):
            stage("capture")
            selected = base / "selected"; selected.mkdir(mode=0o700)
            await files(copy_selected_model,model,selected / "model.gguf",MODEL_BYTES,MODEL_SHA256)
            await files(shutil.copyfile,host / "templates/text-generate-v1/LICENSE-MODEL",selected / "LICENSE")
            request = AcquisitionRequestV1(entries=[
                {"name":"weights","role":"model-weights","path":str(selected / "model.gguf"),"format":"gguf",
                 "expected_sha256":MODEL_SHA256,"expected_size":MODEL_BYTES,"licenses":["license"]},
                {"name":"license","role":"license","path":str(selected / "LICENSE"),"format":"text"}])
            if source_mode == "folder":
                original = base / "instructions"
                original.mkdir(mode=0o700)
                (original / "README.md").write_text("Prepare bounded local CPU text generation from the selected model.\n")
                previous = await files(imports.create, original, name="Local text generation")
                original_source = previous["source"]
                target = AttachImportTargetV1(import_id=previous["draft_id"], revision=previous["revision"])
            else:
                original_source = None
                target = CreateImportTargetV1(name="Local text generation",requirements={
                    "tools":["text_generate","text_status"], "memory_mb":1024, "profile":"model-service"})
            captured = await coordinator.start(request,CapturePolicyV1(disk_ceiling_bytes=1024**3), target,"actual-textgen")
            await asyncio.shield(coordinator.tasks[captured["capture_id"]])
            completed = capture_store.get(captured["capture_id"])
            if completed["state"] != "succeeded":
                raise PreparationError(completed["error_code"] or "phase3_textgen_capture_failed")
            draft = await files(imports._get,completed["result"]["import_id"])
            if original_source is not None:
                if draft["source"] != original_source or draft["revision"] != previous["revision"] + 1:
                    raise PreparationError("phase3_textgen_source_snapshot_changed")
                await files(shutil.rmtree, original)
            await files(shutil.rmtree,selected)
            bundle = base / "recipe"
            await files(write_textgen_recipe,bundle)
            recipe = await files(recipes.add,bundle)
            recipe = await files(recipes.enable,recipe["recipe_id"],recipe["version"])
            stage("forms")
            forms = PreparationForms(store,providers=(family.form_provider,))
            form = await files(forms.form,draft["draft_id"],draft["revision"])
            if form["blockers"] or form["execution_available"] is not False:
                raise PreparationError("phase3_textgen_form_failed")
            descriptor,context = await files(forms.resolve,draft["draft_id"],draft["revision"],
                candidate_id=form["candidate_id"],recipe=form["recipe"],form_digest=form["form_digest"],answers={})
            draft = await files(store.select_descriptor,draft["draft_id"],draft["revision"],descriptor,
                                candidate_id=form["candidate_id"],form_digest=form["form_digest"])
            stage("planning")
            runner = PreparationRunner(store,host,host / "bin/mcpme-reap",enabled=True,
                                       reserve_resources=reserve,release_resources=release)
            policy = ExecutionPolicy(schema_version=1,operations=["build","probe"],network=["files.pythonhosted.org"],
                                     wall_seconds=240,disk_bytes=DISK_BYTES,memory_mb=2048)
            plan = await runner.plan(draft["draft_id"],draft["revision"],descriptor.model_dump(exclude_none=True),policy)
            stage("building")
            job = await runner.start(plan["plan_id"],plan["plan_digest"],str(uuid.uuid4()))
            await asyncio.shield(runner.tasks[job["job_id"]])
            final = store.get(job["job_id"])
            if final["state"] != "succeeded":
                raise PreparationError(final["error_code"] or "phase3_textgen_build_failed")
            attached = await files(imports.inspect,draft["draft_id"])
            if (attached["revision"] != draft["revision"] or not attached["attachment"]
                    or attached["attachment"]["digest"] != final["expected_package_digest"]
                    or not final["ownership_settled"] or final["reservation_retained"] or reserved
                    or store.ownership_receipts(job["job_id"]) or Path(final["workspace"]).exists()
                    or any(item["kind"]=="job" for item in capture_store.references(completed["collection_digest"]))):
                raise PreparationError("phase3_textgen_settlement_failed")
            package = await files(validate_package,Path(attached["attachment"]["path"]))
            sealed = parse_preparation_receipt((package.root / RECEIPT_NAME).read_bytes())
            expected_files = draft["source"]["files"] if source_mode == "folder" else [
                {"path": entry["path"], "sha256": entry["sha256"], "size": entry["size"]}
                for entry in draft["source"]["collection"]["entries"]]
            if (sealed.schema_version != 2 or sealed.inputs.model_dump(mode="json") != final["bound_inputs"]
                    or sealed.inputs.digest != final["bound_inputs_digest"]
                    or sealed.inputs.descriptor_digest != descriptor.digest
                    or sealed.inputs.recipe != descriptor.recipe
                    or sealed.inputs.source.revision != draft["source"]["revision"]
                    or sealed.inputs.captured_collection.digest != completed["collection_digest"]
                    or any([record.model_dump(mode="json") for record in binding.files] != expected_files
                           for binding in sealed.inputs.source_validations)):
                raise PreparationError("phase3_textgen_receipt_binding_mismatch")
            await runner.close(); runner=None
            await coordinator.close(); coordinator=None
            imports.close(); imports=None
            installed = None
            if install_lifecycle:
                stage("installed_lifecycle")
                module_path = Path(__file__).with_name("verify_phase3_llm_installed.py")
                lifecycle_hash = hashlib.sha256(module_path.read_bytes()).hexdigest()
                spec = importlib.util.spec_from_file_location("textgen_installed_acceptance", module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                installed = {"checks": {}, "stage": "start"}
                options = argparse.Namespace(host=host, package=package.root, prepared_state=base / "state",
                                             import_id=draft["draft_id"], revision=draft["revision"])
                try:
                    await module.verify(options, installed)
                finally:
                    if not installed.get("ownership_settled"):
                        (base / "ownership-unsettled").touch(mode=0o600)
                if not installed.get("ownership_settled"):
                    raise PreparationError("phase3_textgen_install_ownership_unsettled")
                if hashlib.sha256(module_path.read_bytes()).hexdigest() != lifecycle_hash:
                    raise PreparationError("phase3_textgen_harness_changed")
                installed["harness_sha256"] = lifecycle_hash
            stage("relocation")
            relocated = base / "relocated-package"
            await files(package.root.rename,relocated)
            digest = package.digest
            stage("removing_inputs")
            for path in (base / "state",bundle):
                await files(shutil.rmtree,path)
            package = await files(validate_package,relocated)
            if package.digest != digest:
                raise PreparationError("phase3_textgen_relocation_identity_mismatch")
            probe = base / "probe"; probe.mkdir(mode=0o700)
            stage("relocation_probe")
            report = await verify_prepared_package(relocated,descriptor,host / "bin/mcpme-reap",probe,timeout_seconds=60)
            after = await files(validate_package,relocated)
            if after.digest != digest:
                raise PreparationError("phase3_textgen_probe_mutated_package")
            check_origins(host)
            stage("closing")
            return {**receipt,"verified":True,"public_execution_available":False,"slot_installed":install_lifecycle,
                "source_mode":source_mode, "original_source_records_preserved":True,
                "capture_descriptor_recipe_receipt_bound":True, "installed_lifecycle":installed,
                "model_sha256":MODEL_SHA256,"model_bytes":MODEL_BYTES,"package_digest":digest,
                "descriptor_version":descriptor.schema_version,"same_revision_attachment":True,
                "selected_sources_removed":True,"captured_state_removed_before_relocation_probe":True,
                "commands":final["progress"]["commands_finished"],"wheels":final["progress"]["wheels_acquired"],
                "dependency_bytes":final["progress"]["bytes_acquired"],"fixtures":report.fixtures_checked,
                "relocation_probe_peak_memory_bytes":report.peak_memory_bytes,"declared_memory_mb":descriptor.resources.memory_mb,
                "owned_work_remaining":0,"pending_receipts":0,"job_references_remaining":0,
                "a4_managed_native_gate_passed":False,"wall_seconds":round(time.monotonic()-started,6)}
    finally:
        try:
            if runner is not None:
                await _await_ownership(runner.close())
            if coordinator is not None:
                await _await_ownership(coordinator.close())
            if imports is not None:
                imports.close()
            if reserved:
                (base / "ownership-unsettled").touch(mode=0o600)
        except BaseException:
            (base / "ownership-unsettled").touch(mode=0o600)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",type=Path,required=True,help="Absolute fresh complete built CLI host.")
    parser.add_argument("--model",type=Path,required=True,help="Absolute exact pinned LLM file; never removed or modified.")
    parser.add_argument("--keep-artifacts",action="store_true")
    parser.add_argument("--source-mode", choices=("captured", "folder"), default="captured")
    parser.add_argument("--install-lifecycle", action="store_true")
    options = parser.parse_args()
    os.umask(0o077)
    base = Path(tempfile.mkdtemp(prefix="mcpme-phase3-textgen-",dir="/private/tmp")).resolve()
    result,failed = None,False
    harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        if not options.model.is_absolute() or options.model.resolve() != options.model:
            raise VerificationError("phase3_textgen_absolute_model_required")
        result = asyncio.run(verify(options.host,options.model,base, source_mode=options.source_mode,
                                   install_lifecycle=options.install_lifecycle))
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != harness_hash:
            raise VerificationError("phase3_textgen_harness_changed")
    except (Exception,KeyboardInterrupt) as error:
        code = getattr(error,"code","phase3_textgen_verification_failed")
        if not isinstance(code,str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,127}",code):
            code = "phase3_textgen_verification_failed"
        error_type = type(error).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}",error_type):
            error_type = "UnknownError"
        result = {"verified":False,"error":code,"stage":STAGE,"exception_type":error_type}
        failed = True
    result["harness_sha256"] = harness_hash
    if options.keep_artifacts or (base / "ownership-unsettled").exists():
        result["private_artifact_directory"] = str(base)
        if (base / "ownership-unsettled").exists():
            result["ownership_retained"] = True
    else:
        shutil.rmtree(base)
    print(json.dumps(result,sort_keys=True))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
