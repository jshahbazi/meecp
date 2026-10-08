"""Verify real native model capture, preparation and installed ONNX invocation.

Only this development harness admits the selected ONNX family. All product,
SDK, resolver and compiler bytes come from a hash-checked relocated host. The
native driver invokes ArtifactCaptureView's actual handlers and production
authenticated Unix-socket routes. No source descriptor or manifest is authored.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time

import verify_phase3_native_onboarding as common
import verify_phase3_onnx as onnx
from verify_phase3_primitives import _check_module_origins, _host_imports, _host_paths

CASES = ("create", "supplement")
NATIVE_STAGES = common.NATIVE_STAGES | {"capture_selection", "capture_start", "capture_poll"}
ENABLED_HOST = (
    "import asyncio,logging,sys\nfrom pathlib import Path\n"
    "sys.path[:0]=[sys.argv[1],sys.argv[2]]\nlogging.disable(logging.CRITICAL)\n"
    "from meecp.supervisor import serve\nfrom meecp.preparation_runner import PreparationRunner\n"
    "from meecp.preparation_onnx import component_reference\n"
    "def preparation(*args,**kwargs):\n kwargs['enabled']=True\n runner=PreparationRunner(*args,**kwargs)\n"
    " runner.enabled_families=frozenset({component_reference().key})\n return runner\n"
    "asyncio.run(serve(Path(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]),preparation_runner_factory=preparation))\n"
)


@contextmanager
def isolated_workspace(report):
    temporary = tempfile.TemporaryDirectory(prefix="meecp-native-onnx-", delete=False)
    root, lease = Path(temporary.name).resolve(), {"safe_to_remove": True}
    try:
        yield root, lease
    finally:
        if lease["safe_to_remove"]:
            try:
                temporary.cleanup()
            except OSError:
                lease["safe_to_remove"] = False
                report["isolated_state_retained"] = True
                report["retained_state_directory"] = str(root)
                raise common.AcceptanceError("acceptance_workspace_cleanup_failed") from None
        else:
            report["isolated_state_retained"] = True
            report["retained_state_directory"] = str(root)


def capture_ownership(state):
    """Read only bounded ownership counts; never acquisition paths or bytes."""
    path = state / "imports.sqlite"
    details = path.lstat()
    common.require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
        and stat.S_IMODE(details.st_mode) == 0o600 and path.resolve() == path, "unsafe_capture_ledger")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        return {
            "unsettled_jobs": db.execute("SELECT count(*) FROM artifact_captures WHERE json_extract(data,'$.ownership_settled')!=1 OR json_extract(data,'$.reservation_retained')!=0").fetchone()[0],
            "readers": db.execute("SELECT count(*) FROM captured_artifact_readers").fetchone()[0],
            "job_references": db.execute("SELECT count(*) FROM captured_artifact_references WHERE kind='job'").fetchone()[0],
            "gc_journals": db.execute("SELECT count(*) FROM captured_artifact_gc").fetchone()[0],
        }
    finally:
        db.close()


def selected_descriptor(state, import_id):
    # Read the actual host-generated selection, without printing or replacing it.
    path = state / "imports.sqlite"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        row = db.execute("SELECT data FROM preparation_selections WHERE import_id=?", (import_id,)).fetchone()
        common.require(row is not None and len(row[0].encode()) <= common.MAX_JSON_BYTES, "onnx_selected_descriptor_missing")
        return json.loads(row[0])["descriptor"]
    finally:
        db.close()


class Harness(common.Harness):
    def __init__(self, root, host, native, lease, model):
        super().__init__(root, host, native, lease)
        self.model = model
        self.retired_states = set()

    def retire_socket(self, state):
        from meecp_local_transport import socket_path
        directory = socket_path(state, "manager").parent
        known = {"manager.sock", *(path.name + ".sock" for path in (state / "slots").glob("*"))}
        for path in directory.iterdir():
            details = path.lstat()
            common.require(path.name in known and stat.S_ISSOCK(details.st_mode) and details.st_uid == os.getuid(),
                "unexpected_acceptance_socket")
            path.unlink()
        directory.rmdir()
        self.retired_states.add(state)

    async def cleanup(self):
        states = self.states
        self.states = [state for state in states if state not in self.retired_states]
        try:
            await super().cleanup()
        finally:
            self.states = states

    async def start(self, state, environment, home, *, enabled):
        # An explicitly owned factory is the only admission path. No CLI or
        # environment switch expands public execution or enables resolution.
        if not enabled:
            return await super().start(state, environment, home, enabled=False)
        argv = [str(self.host / "python/bin/python3.12"), "-I", "-S", "-B", "-c", ENABLED_HOST,
            str(self.host / "backend"), str(self.host / "python/site-packages"), str(state),
            str(self.reaper), str(os.getpid())]
        child = self.spawn(argv, environment, home)
        try:
            async with asyncio.timeout(30):
                while child.poll() is None:
                    if (state / "manager-connection.json").is_file():
                        try:
                            await self.cli(state, environment, home, "capabilities", "--json")
                            return child
                        except (common.AcceptanceError, OSError):
                            pass
                    await asyncio.sleep(.1)
            raise common.AcceptanceError("acceptance_host_exited")
        except BaseException:
            await self.shutdown(child, state)
            raise

    async def manager_get(self, state, route):
        import httpx
        from meecp_local_transport import private_transport, socket_path
        connection = common.private_json(state / "manager-connection.json")
        async with httpx.AsyncClient(base_url=connection["endpoint"], trust_env=False, timeout=30,
                follow_redirects=False, transport=private_transport(socket_path(state, "manager")),
                headers={"Authorization": "Bearer " + connection["token"]}) as client:
            response = await client.get(route)
            common.require(len(response.content) <= common.MAX_JSON_BYTES, "manager_response_limit")
            value = response.json()
            common.require(response.status_code == 200 and isinstance(value, dict) and "error" not in value,
                "manager_request_failed")
            return value

    async def shutdown(self, child, state):
        try:
            await super().shutdown(child, state)
            if (state / "imports.sqlite").exists():
                common.require(not any(capture_ownership(state).values()), "capture_ownership_retained")
                common.require(not list((state / "artifact-capture-owners").glob("*.json")), "capture_owner_retained")
        except BaseException:
            self.lease["safe_to_remove"] = False
            raise

    def selected(self, base):
        from meecp.preparation_onnx import MODEL_BYTES, MODEL_SHA256
        selected = base / "selected"
        selected.mkdir(mode=0o700)
        model, license = selected / "model.onnx", selected / "LICENSE"
        onnx.copy_selected_model(self.model, model, MODEL_BYTES, MODEL_SHA256)
        shutil.copyfile(self.host / "templates/onnx-hotdog-v1/LICENSE-MODEL", license)
        common.require(common.digest(license) == "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0",
            "onnx_license_identity_mismatch")
        return selected, model, license

    async def native_capture(self, base, state, home, environment, model, license, draft=None):
        fixture, output = base / "native-case.json", base / "native-receipt.json"
        case = {"state_dir": str(state), "output_path": str(output), "capture_model_path": str(model),
            "capture_license_path": str(license), "capture_format": "onnx", "answers_by_label": {},
            "recipe_id": "mcpme.onnx-hotdog"}
        if draft is not None:
            case["draft_id"] = draft["draft_id"]
        common.write_private_json(fixture, case)
        common.stage("native")
        code, _ = await self.run([str(self.native), "--preparation-native-acceptance", str(fixture)],
            environment, home, timeout=1200)
        common.require(output.is_file(), "native_receipt_missing")
        receipt = common.private_json(output, 256 * 1024)
        return code, receipt, output

    async def production_capture_denial(self):
        common.stage("production_defaults")
        base, state, home, environment = self.new_case("public-capture")
        selected, model, license = self.selected(base)
        child = await self.start(state, environment, home, enabled=False)
        try:
            caps = await self.cli(state, environment, home, "capabilities", "--json")
            common.require(caps["automatic_preparation"] is False and caps["recipe_execution"] is False
                and caps["preparation_resolution"]["execution"] is False, "production_execution_enabled")
            code, receipt, output = await self.native_capture(base, state, home, environment, model, license)
            if not (code != 0 and receipt.get("passed") is False and receipt.get("stage") == "preparation_review"
                    and isinstance(receipt.get("capture_job_id"), str)):
                failure = common.AcceptanceError("public_capture_denial_mismatch")
                failure.native_stage = receipt.get("stage") if receipt.get("stage") in NATIVE_STAGES else "connection"
                failure.native_observation = {"native_exit_code": code, "capture_completed": "capture_job_id" in receipt,
                    "receipt_sha256": common.digest(output), "error_code": receipt.get("error_code")}
                raise failure
            captured = await self.manager_get(state, "/v1/artifact-captures/" + receipt["capture_job_id"])
            common.require(captured["state"] == "succeeded" and captured["ownership_settled"] is True
                and captured["reservation_retained"] is False, "public_capture_unsettled")
            common.require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == []
                and (await self.cli(state, environment, home, "resolutions", "list", "--json"))["resolutions"] == [],
                "public_preparation_created")
            common.require(not any(capture_ownership(state).values()), "public_capture_ownership_retained")
            return {"capture_available": True, "native_capture_adopted": True, "execution_denied": True,
                "preparation_jobs": 0, "resolution_jobs": 0, "native_stage": receipt["stage"],
                "native_receipt_sha256": common.digest(output)}
        finally:
            await self.shutdown(child, state)

    async def wrong_weights_denial(self):
        common.stage("source_intake")
        base, state, home, environment = self.new_case("wrong-weights")
        _, model, license = self.selected(base)
        # Mutate only the isolated selection. Same size and format cannot admit
        # bytes outside the exact model identity reviewed by this recipe.
        with model.open("r+b") as stream:
            first = stream.read(1); stream.seek(0); stream.write(bytes([first[0] ^ 1]))
        child = await self.start(state, environment, home, enabled=True)
        try:
            code, receipt, output = await self.native_capture(base, state, home, environment, model, license)
            common.require(code != 0 and receipt.get("passed") is False
                and receipt.get("stage") == "initial_form" and "capture_job_id" in receipt, "wrong_weights_native_admission_mismatch")
            captured = await self.manager_get(state, "/v1/artifact-captures/" + receipt["capture_job_id"])
            common.require(captured["state"] == "succeeded" and captured["ownership_settled"] is True
                and captured["reservation_retained"] is False, "wrong_weights_capture_unsettled")
            draft = await self.cli(state, environment, home, "imports", "inspect", receipt["draft_id"], "--json")
            inspected = await self.manager(state, "/v1/preparations/form", {"import_id": receipt["draft_id"], "revision": draft["revision"]})
            from meecp.preparation_onnx import OnnxPreparationFamily
            # The frozen host's generic form exposes a selection blocker when
            # no candidate exists. Verify its production family's precise static
            # rejection independently; never parse or execute the wrong model.
            reason = OnnxPreparationFamily(None).inspect_source(draft)
            common.require(reason["blockers"] == ["onnx_model_identity_unsupported"]
                and inspected["blockers"] and inspected["candidate_choices"] == []
                and draft["attachment"] is None
                and (await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == [],
                "wrong_weights_preparation_admitted")
            return {"capture_adopted": True, "preparation_denied": True, "error_code": "onnx_model_identity_unsupported",
                "preparation_jobs": 0, "same_byte_length": True, "native_receipt_sha256": common.digest(output)}
        finally:
            await self.shutdown(child, state)

    async def onnx_case(self, label):
        from meecp.contracts import Tool, validate_package
        from meecp.preparation_inputs import parse_preparation_receipt, parse_preparation_descriptor
        from meecp.preparation_onnx import MODEL_BYTES, MODEL_SHA256, onnx_hotdog_tool, onnx_hotdog_fixtures
        common.stage("source_intake")
        started = time.monotonic()
        base, state, home, environment = self.new_case(label)
        selected, model, license = self.selected(base)
        source = None
        child = await self.start(state, environment, home, enabled=True)
        try:
            draft, original = None, None
            if label == "supplement":
                source = base / "ordinary folder"
                source.mkdir(mode=0o700)
                (source / "README.md").write_text("Ordinary project notes for the model app.\n")
                draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                    "--name", "Native supplemented detector", "--intent", "Detect hot dogs in supplied synthetic images.",
                    "--start", "--start-policy", "host", "--json")
                original = draft["source"]
            recipe = await self.cli(state, environment, home, "recipes", "inspect", "--id", "mcpme.onnx-hotdog",
                "--version", "1.0.0", "--json")
            common.require(recipe["state"] == "enabled", "onnx_default_recipe_missing")
            code, receipt, output = await self.native_capture(base, state, home, environment, model, license, draft)
            if code or receipt.get("passed") is not True:
                failure = common.AcceptanceError(receipt.get("error_code", "native_acceptance_failed"))
                failure.native_stage = receipt.get("stage") if receipt.get("stage") in NATIVE_STAGES else "connection"
                failure.native_observation = {"case": label, "native_exit_code": code,
                    "receipt_sha256": common.digest(output), "capture_completed": "capture_job_id" in receipt,
                    "reviewed_revision": receipt.get("reviewed_revision")}
                raise failure
            common.require(receipt["stage"] == "attached" and receipt["capture_target"] == ("create-import" if label == "create" else "attach-to-import")
                and receipt["recipe_id"] == recipe["recipe_id"] and receipt["recipe_digest"] == recipe["digest"],
                "native_onnx_binding_mismatch")
            capture_id, import_id = receipt["capture_job_id"], receipt["draft_id"]
            routes = set(receipt["routes"])
            common.require({"POST v1/artifact-captures", "POST v1/preparations/form", "POST v1/preparations/plan-form",
                "POST v1/preparations/start", "GET v1/artifact-captures/" + capture_id} <= routes,
                "native_capture_routes_missing")
            common.require(not any("v1/resolutions/" in route for route in routes), "onnx_resolution_unexpected")
            captured = await self.manager_get(state, "/v1/artifact-captures/" + capture_id)
            common.require(captured["state"] == "succeeded" and captured["ownership_settled"] is True
                and captured["reservation_retained"] is False and captured["collection_digest"] == receipt["capture_collection_digest"]
                and captured["result"] == {"import_id": import_id, "revision": receipt["captured_revision"]},
                "native_capture_receipt_mismatch")
            prepared = await self.cli(state, environment, home, "preparations", "get", receipt["preparation_job_id"], "--json")
            attached = await self.cli(state, environment, home, "imports", "inspect", import_id, "--json")
            common.require(prepared["state"] == "succeeded" and prepared["ownership_settled"] is True
                and prepared["reservation_retained"] is False and common.preparation_ownership_count(state) == 0
                and not any(capture_ownership(state).values()), "native_onnx_ownership_retained")
            if not (attached["revision"] == receipt["reviewed_revision"] == receipt["attached_revision"]
                and attached["revision"] >= receipt["captured_revision"]
                and attached["source"]["revision"] == receipt["source_revision"] and attached["state"] == "ready_to_plan"):
                failure = common.AcceptanceError("native_onnx_revision_mismatch")
                failure.native_observation = {"case": label, "captured_revision": receipt["captured_revision"],
                    "reviewed_revision": receipt["reviewed_revision"], "attached_revision": receipt["attached_revision"],
                    "current_revision": attached["revision"], "same_source_revision": attached["source"]["revision"] == receipt["source_revision"],
                    "ready_to_plan": attached["state"] == "ready_to_plan", "attachment_present": attached["attachment"] is not None}
                raise failure
            common.require(attached["start"] is True and attached["start_policy"] == "host", "native_start_choice_changed")
            if original is not None:
                common.require(attached["source"] == original and attached["captured_artifacts"]["digest"] == captured["collection_digest"],
                    "supplement_original_namespace_changed")
            else:
                common.require(attached["source"]["kind"] == "captured_artifact_collection"
                    and attached["source"]["revision"] == "sha256:" + captured["collection_digest"], "captured_source_identity_mismatch")
            package = validate_package(Path(attached["attachment"]["path"]))
            common.require(package.digest == attached["attachment"]["digest"] == prepared["expected_package_digest"]
                == receipt["attached_package_digest"], "native_onnx_package_identity_mismatch")
            proof = parse_preparation_receipt((package.root / "mcpme.preparation.json").read_bytes())
            component_ids = {item.id for item in proof.inputs.components}
            common.require(proof.schema_version == 2 and proof.inputs.descriptor_version == 2
                and component_ids == {"onnxruntime-cpu-v1", "yolo11-coco-detect-v1"}
                and proof.inputs.captured_collection.digest == captured["collection_digest"]
                and proof.binding.revision == attached["revision"] and proof.binding.source_revision == attached["source"]["revision"]
                and not proof.inputs.original_dependency_locks, "native_onnx_input_closure_mismatch")
            common.require(common.digest(model) == MODEL_SHA256 and model.stat().st_size == MODEL_BYTES,
                "selected_model_mutated")
            common.require(prepared["verification"]["tools_checked"] == 1 and prepared["verification"]["fixtures_checked"] == 3
                and prepared["verification"]["package_digest"] == package.digest, "onnx_probe_mismatch")
            workload = json.loads((package.root / "onnx-validation.json").read_bytes())
            common.require(workload["model_valid"] is True and workload["blank_images"] == 3
                and workload["controlled_tensor_cases"] == 6 and workload["max_image_cases"] == 7
                and workload["boundary_rejections"] == 3, "onnx_validation_failed")
            common.stage("installation")
            plan = await self.cli(state, environment, home, "install", "plan", "--import", import_id,
                "--revision", attached["revision"], "--json")
            expected_tools = [Tool.model_validate(item).mcp_schema() for item in plan["package"]["tools"]]
            common.require(expected_tools == [onnx_hotdog_tool().mcp_schema()] == [item.mcp_schema() for item in package.manifest.tools]
                and plan["start"] is True and plan["start_policy"] == "host", "installed_onnx_contract_mismatch")
            operation = await self.finish_operation(state, environment, home,
                await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json"))
            slot_id = operation["slot_id"]
            installed = await self.cli(state, environment, home, "imports", "inspect", import_id, "--json")
            common.require(installed["state"] == "installed" and installed["result_slot_id"] == slot_id, "installed_onnx_binding_mismatch")
            calls = [(item["tool"], item["arguments"], item["expected"]) for item in onnx_hotdog_fixtures()]
            http_calls = await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
            stdio_calls = await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
            common.stage("source_removal")
            shutil.rmtree(selected)
            if source is not None:
                shutil.rmtree(source)
            await self.shutdown(child, state)
            common.stage("restart")
            child = await self.start(state, environment, home, enabled=False)
            retained = await self.cli(state, environment, home, "recipes", "inspect", "--id", recipe["recipe_id"], "--version", "1.0.0", "--json")
            common.require(retained["state"] == "enabled" and retained["digest"] == recipe["digest"]
                and retained["epoch"] == recipe["epoch"], "onnx_enabled_recipe_restart_failed")
            http_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
            stdio_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
            common.stage("recipe_lifecycle")
            disabled = await self.cli(state, environment, home, "recipes", "disable", "--id", recipe["recipe_id"], "--version", "1.0.0", "--json")
            await self.shutdown(child, state)
            child = await self.start(state, environment, home, enabled=False)
            retained = await self.cli(state, environment, home, "recipes", "inspect", "--id", recipe["recipe_id"], "--version", "1.0.0", "--json")
            common.require(retained["state"] == "disabled" and retained["digest"] == disabled["digest"]
                and retained["epoch"] == disabled["epoch"], "onnx_disabled_recipe_restart_failed")
            http_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
            stdio_calls += await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
            # Probe the exact installed package after removing the complete
            # original isolated state, including captured artifacts. This owner
            # measures its own real process; it never discovers slot PIDs.
            slot = await self.cli(state, environment, home, "slots", "inspect", slot_id, "--json")
            installed_root = Path(slot["package_path"])
            relocated = base / "independent installed package"
            shutil.copytree(installed_root, relocated, symlinks=True)
            independent = validate_package(relocated)
            common.require(independent.digest == package.digest, "installed_package_relocation_mismatch")
            ownership = capture_ownership(state)
            descriptor = parse_preparation_descriptor(selected_descriptor(state, import_id))
            await self.shutdown(child, state)
            self.retire_socket(state)
            shutil.rmtree(state)
            workspace = base / "probe"
            workspace.mkdir(mode=0o700)
            verification = await onnx.relocation_probe(relocated, descriptor, self.reaper, workspace)
            maximum_workspace = base / "maximum probe"
            maximum_workspace.mkdir(mode=0o700)
            maximum = await onnx.max_probe(independent, self.reaper, maximum_workspace, package.manifest.resources.memory_mb)
            common.require(validate_package(relocated).digest == package.digest, "onnx_probe_mutated_package")
            return {"passed": True, "capture_target": label, "native_receipt_sha256": common.digest(output),
                "native_routes": sorted(routes), "captured_collection_digest": captured["collection_digest"],
                "captured_revision": receipt["captured_revision"], "reviewed_revision": receipt["reviewed_revision"],
                "attached_revision": receipt["attached_revision"], "same_revision_attachment": True,
                "original_folder_preserved": label == "supplement", "component_ids": sorted(component_ids),
                "recipe_digest": retained["digest"], "enabled_recipe_restart": True, "disabled_recipe_restart": True,
                "installed_runtime_after_disable": True, "package_digest": package.digest,
                "full_tool_contract_sha256": hashlib.sha256(json.dumps(expected_tools, sort_keys=True).encode()).hexdigest(),
                "http_calls": http_calls, "stdio_calls": stdio_calls, "source_removed": True, "restart_invoked": True,
                "captured_state_removed_before_relocation_probe": True, "relocation_fixtures": verification.fixtures_checked,
                "relocation_probe_peak_memory_bytes": verification.peak_memory_bytes,
                "relocation_probe_confined": True,
                "declared_app_memory_mb": package.manifest.resources.memory_mb,
                "preparation_progress": prepared["progress"], "probe_peak_memory_bytes": prepared["verification"]["peak_memory_bytes"],
                "capture_ownership_remaining": ownership, "workload": workload, **maximum,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
        except BaseException as error:
            if not isinstance(error, common.AcceptanceError):
                error.acceptance_context = dict(common.DIAGNOSTIC)
            raise
        finally:
            if (base / "ownership-unsettled").exists():
                self.lease["safe_to_remove"] = False
            if state.exists():
                await self.shutdown(child, state)
            else:
                await self.stop(child)


async def verify(host_path, native_path, model, report):
    _host_paths(host_path)
    common.require(native_path.is_file() and native_path.resolve() == native_path and os.access(native_path, os.X_OK), "native_acceptance_binary_required")
    with isolated_workspace(report) as (root, lease):
        host, native = common.relocate_host_and_native(root, host_path, native_path, report)
        _host_paths(host); _host_imports(host)
        identity = await asyncio.to_thread(onnx.host_hashes, host)
        report.update({"host_source_commit": identity["source_commit"], "host_source_tree_dirty": False,
            "host_build_sha256": common.digest(host / "build.json"), "host_artifacts_checked": identity["host_hashes_checked"],
            "native_binary_sha256": common.digest(native), "relocated_host": True, "minimal_environment": True,
            "private_sdk": True, "development_execution_only": True, "selected_model_sha256": common.digest(model)})
        harness = Harness(root, host, native, lease, model)
        try:
            code, _ = await harness.run([str(native), "--artifact-capture-ui-self-test"],
                {"HOME": str(root), "PATH": "/usr/bin:/bin", "TMPDIR": str(root)}, root)
            common.require(code == 0, "native_capture_ui_contracts_failed")
            report["native_capture_ui_contracts"] = True
            report["production_defaults_disabled"] = await harness.defaults()
            report["production_native_capture"] = await harness.production_capture_denial()
            report["wrong_weights_native_capture"] = await harness.wrong_weights_denial()
            for label in CASES:
                report["cases"][label] = await harness.onnx_case(label)
            _check_module_origins(host)
        finally:
            try:
                await harness.cleanup()
            except BaseException:
                lease["safe_to_remove"] = False
                raise
            finally:
                report["owned_children_joined"] = not harness.children
        if report.get("native_bundle_preserved"):
            common.verify_native_app_signature(native)
        report["verified"] = report["owned_children_joined"] and len(report["cases"]) == 2 and all(item["passed"] for item in report["cases"].values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "native_installed_onnx_onboarding", "cases": {},
        "limits": ["synthetic_fixtures_do_not_measure_recognition_accuracy", "crash_revision_cancel_races_unverified",
            "probe_confinement_is_exact_platform", "hard_aggregate_live_memory_unverified", "public_execution_disabled"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), args.model.resolve(strict=True), report))
    except (Exception, KeyboardInterrupt) as error:
        error, nested = common.bounded_failure(error)
        code = getattr(error, "code", "acceptance_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            code = "acceptance_failed"
        report["diagnostic"] = {**getattr(error, "context", getattr(error, "acceptance_context", common.DIAGNOSTIC)), "code": code,
            "type": type(error).__name__ if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", type(error).__name__) else "Exception"}
        if nested:
            report["diagnostic"]["nested_types"] = nested
        if hasattr(error, "native_stage"):
            report["diagnostic"]["native_stage"] = error.native_stage
        if hasattr(error, "native_observation"):
            report["native_failure"] = error.native_observation
    if args.output:
        try:
            common.write_private_json(args.output.absolute(), report)
        except Exception:
            report["verified"] = False; report["report_write_error"] = "acceptance_report_write_failed"
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
