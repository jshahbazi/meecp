"""Verify native GGUF capture and real installed managed text generation.

The private factory admits exactly the reviewed text-generation task. Product
code, SDK dependencies and child tools come from a sealed relocated host. The
frozen native driver calls the visible capture view's actual handlers. This
script never authors a preparation descriptor, tool schema or dependency lock.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

import verify_phase3_native_onboarding as common
import verify_phase3_native_onnx as capture
import verify_phase3_onnx as primitives
from verify_phase3_primitives import _check_module_origins, _host_imports, _host_paths

MODEL_BYTES = 491400032
MODEL_SHA256 = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
LICENSE_SHA256 = "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
CASES = ("create", "supplement")
RECIPE_ID = "mcpme.text-generation"
COMPONENT_IDS = frozenset({"llamacpp-cpu-v1", "text-generate-v1"})
ENABLED_HOST = (
    "import asyncio,logging,sys\nfrom pathlib import Path\n"
    "sys.path[:0]=[sys.argv[1],sys.argv[2]]\nlogging.disable(logging.CRITICAL)\n"
    "from meecp.supervisor import serve\nfrom meecp.preparation_runner import PreparationRunner\n"
    "from meecp.preparation_textgen import references\n"
    "def preparation(*args,**kwargs):\n kwargs['enabled']=True\n runner=PreparationRunner(*args,**kwargs)\n"
    " runner.enabled_families=frozenset({references()[1].key})\n return runner\n"
    "asyncio.run(serve(Path(sys.argv[3]),Path(sys.argv[4]),int(sys.argv[5]),preparation_runner_factory=preparation))\n"
)


@contextmanager
def isolated_workspace(report):
    temporary = tempfile.TemporaryDirectory(prefix="meecp-native-textgen-", delete=False)
    root, lease = Path(temporary.name).resolve(), {"safe_to_remove": True}
    try:
        yield root, lease
    except BaseException:
        lease["safe_to_remove"] = False
        report["failure_artifacts_retained"] = True
        raise
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


def check_origins(host):
    _check_module_origins(host)
    import sys
    for name, module in tuple(sys.modules.items()):
        if name.startswith(("meecp_textgen", "jevk5_app")):
            path = getattr(module, "__file__", None)
            common.require(path and Path(path).resolve().is_relative_to(host / "backend"),
                "textgen_private_module_required")


def harness_identities():
    return {"native_textgen": common.digest(Path(__file__)),
        "native_capture": common.digest(Path(capture.__file__)),
        "native_onboarding": common.digest(Path(common.__file__)),
        "preparation_primitives": common.digest(Path(primitives.__file__))}


def sealed_host_identity(host):
    """Reject unlisted runtime bytes as well as changed listed artifacts."""
    receipt = host / "build.json"
    common.require(receipt.stat().st_size <= 4 * 1024**2, "textgen_host_receipt_limit")
    metadata = json.loads(receipt.read_bytes())
    common.require(set(metadata["artifact_sha256"]) == {path.relative_to(host).as_posix()
        for path in host.rglob("*") if path.is_file() and path != receipt},
        "textgen_host_payload_membership_mismatch")
    return primitives.host_hashes(host)


async def join_tasks(tasks):
    """Cancellation never abandons SDK requests or private file-copy threads."""
    from meecp.processes import _await_ownership
    return await _await_ownership(asyncio.gather(*tasks, return_exceptions=True))


class ObservedWrite:
    """Observe only SDK-generated request IDs; retain no arguments or results."""
    def __init__(self, stream):
        self.stream = stream
        self.request_ids = []
        self.sent = asyncio.Event()

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *arguments):
        return await self.stream.__aexit__(*arguments)

    async def send(self, message):
        root = message.message.root
        if getattr(root, "method", None) == "tools/call" and getattr(root, "params", {}).get("name") == "text_generate":
            identifier = root.id
            common.require(type(identifier) in {int, str} and len(self.request_ids) < 64,
                "textgen_sdk_request_identity_missing")
            await self.stream.send(message)
            self.request_ids.append(identifier)
            self.sent.set()
        else:
            await self.stream.send(message)


def has_error_code(result, code):
    if result.isError is not True:
        return False
    # The product emits fixed ToolError codes. Read bounded fields only to
    # recognize the expected code; never return or print the error body.
    for item in result.content[:8]:
        text = getattr(item, "text", None)
        if isinstance(text, str) and len(text.encode()) <= 4096 and re.search(r"\b" + re.escape(code) + r"\b", text):
            return True
    return False


def validate_generation(value, maximum, expected_tools):
    from jsonschema import Draft202012Validator
    tool = next(item for item in expected_tools if item["name"] == "text_generate")
    Draft202012Validator(tool["outputSchema"]).validate(value)
    common.require(type(value.get("input_tokens")) is int and value["input_tokens"] > 0
        and type(value.get("output_tokens")) is int and 0 < value["output_tokens"] <= maximum
        and len(value["text"].encode()) <= maximum * 256
        and value["stop_reason"] in {"eos", "stop", "length"}, "installed_textgen_output_bound_failed")
    return {"input_tokens": value["input_tokens"], "output_tokens": value["output_tokens"]}


def expected_source_files(draft):
    source = draft["source"]
    if source["kind"] == "captured_artifact_collection":
        return [{"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
            for item in source["collection"]["entries"]]
    return source["files"]


def resident_graph(details, ledger, engine_id):
    residents = {item["process_id"]: item for item in details["processes"]}
    common.require(len(details["processes"]) == 2 and set(residents) == {"engine", "adapter"}
        and len({item["instance_id"] for item in residents.values()}) == 2
        and {item["instance_id"] for item in residents.values()} <= set(ledger)
        and residents["engine"]["instance_id"] == engine_id
        and all(item["ownership_lost"] is False for item in residents.values())
        and len(ledger) == 3 and all(item["state"] == "owned" for item in ledger.values()),
        "installed_textgen_resident_graph_mismatch")
    return {"resident_processes": 2, "host_owned_processes": 3}


async def relocation_probe(package_root, descriptor, reaper, workspace):
    from meecp.preparation_textgen_verifier import verify_textgen_package
    from meecp.processes import ProcessOwner, _await_ownership
    owner = ProcessOwner(reaper)
    try:
        return await verify_textgen_package(package_root, descriptor, reaper, workspace,
            timeout_seconds=120, owner=owner)
    finally:
        try:
            await _await_ownership(owner.close())
            common.require(not owner.instances, "textgen_owned_work_remaining")
        except BaseException:
            (workspace.parent / "ownership-unsettled").touch(mode=0o600)
            raise


class Harness(capture.Harness):
    async def recipe(self, state, environment, home, action="inspect"):
        from meecp.preparation_textgen import RECIPE_VERSION
        return await self.cli(state, environment, home, "recipes", action, "--id", RECIPE_ID,
            "--version", RECIPE_VERSION, "--json")

    async def start(self, state, environment, home, *, enabled):
        if not enabled:
            return await common.Harness.start(self, state, environment, home, enabled=False)
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

    def selected(self, base):
        selected = base / "selected"
        selected.mkdir(mode=0o700)
        model, license = selected / "model.gguf", selected / "LICENSE"
        primitives.copy_selected_model(self.model, model, MODEL_BYTES, MODEL_SHA256)
        shutil.copyfile(self.host / "templates/text-generate-v1/LICENSE-MODEL", license)
        common.require(common.digest(license) == LICENSE_SHA256, "textgen_license_identity_mismatch")
        return selected, model, license

    async def native_capture(self, base, state, home, environment, model, license, draft=None):
        fixture, output = base / "native-case.json", base / "native-receipt.json"
        case = {"state_dir": str(state), "output_path": str(output), "capture_model_path": str(model),
            "capture_license_path": str(license), "capture_format": "gguf", "answers_by_label": {}, "recipe_id": RECIPE_ID}
        if draft is not None:
            case["draft_id"] = draft["draft_id"]
        common.write_private_json(fixture, case)
        common.stage("native")
        code, _ = await self.run([str(self.native), "--preparation-native-acceptance", str(fixture)],
            environment, home, timeout=1200)
        common.require(output.is_file(), "native_receipt_missing")
        return code, common.private_json(output, 256 * 1024), output

    @asynccontextmanager
    async def sdk_session(self, state, environment, home, slot_id, expected_tools, transport):
        import httpx
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from meecp_local_transport import private_transport, socket_path

        @asynccontextmanager
        async def initialized(read, write):
            observed = ObservedWrite(write)
            async with ClientSession(read, observed) as client:
                result = await client.initialize()
                common.require(result.protocolVersion == "2025-11-25"
                    and set(result.capabilities.model_dump(exclude_none=True)) == {"tools"}, "installed_protocol_mismatch")
                listing = await client.list_tools()
                common.require(listing.nextCursor is None and [item.model_dump(by_alias=True, exclude_none=True)
                    for item in listing.tools] == expected_tools, "installed_full_tool_contract_mismatch")
                yield client, observed

        if transport == "http":
            connection = common.private_json(state / "slots" / slot_id / "connection.json")
            async with httpx.AsyncClient(trust_env=False, timeout=60, follow_redirects=False,
                    transport=private_transport(socket_path(state, slot_id)),
                    headers={"Authorization": "Bearer " + connection["token"]}) as http:
                async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                    async with initialized(read, write) as result:
                        yield result
        else:
            parameters = StdioServerParameters(command=str(self.launcher),
                args=["--state-dir", str(state), "connect", "--slot", slot_id], env=environment, cwd=str(home))
            with open(os.devnull, "w") as quiet:
                async with stdio_client(parameters, errlog=quiet) as (read, write):
                    async with initialized(read, write) as result:
                        yield result

    async def status(self, client, expected_tools):
        from jsonschema import Draft202012Validator
        result = await client.call_tool("text_status", {})
        common.require(not result.isError and isinstance(result.structuredContent, dict), "installed_textgen_status_failed")
        value = result.structuredContent
        Draft202012Validator(next(t for t in expected_tools if t["name"] == "text_status")["outputSchema"]).validate(value)
        common.require(value["ready"] is True and value["model_sha256"] == MODEL_SHA256
            and value["context_tokens"] == 512 and value["max_output_tokens"] == 64
            and type(value["queue_depth"]) is int and 0 <= value["queue_depth"] <= 4,
            "installed_textgen_identity_mismatch")
        return value

    async def generation(self, client, expected_tools, maximum=8):
        result = await client.call_tool("text_generate", {"text": "List integers starting at one.", "max_output_tokens": maximum})
        common.require(not result.isError, "installed_textgen_generation_failed")
        return validate_generation(result.structuredContent, maximum, expected_tools)

    async def sdk_generation(self, state, environment, home, slot_id, expected_tools, transport):
        common.stage(transport + "_sdk")
        async with asyncio.timeout(90):
            async with self.sdk_session(state, environment, home, slot_id, expected_tools, transport) as (client, _):
                before = await self.status(client, expected_tools)
                bounds = await self.generation(client, expected_tools)
                after = await self.status(client, expected_tools)
                common.require(after["completed"] == before["completed"] + 1 and after["queue_depth"] == 0
                    and after["engine_instance_id"] == before["engine_instance_id"], "installed_textgen_generation_accounting_failed")
                return bounds

    async def runtime_limits(self, state, environment, home, slot_id, expected_tools):
        from mcp.types import ClientNotification, CancelledNotification, CancelledNotificationParams
        common.stage("http_sdk")
        pending = []
        async with asyncio.timeout(180):
            async with self.sdk_session(state, environment, home, slot_id, expected_tools, "http") as (first, observed):
                async with self.sdk_session(state, environment, home, slot_id, expected_tools, "http") as (second, _):
                    async with self.sdk_session(state, environment, home, slot_id, expected_tools, "stdio") as (third, _):
                        clients = [first, second, third]
                        baseline = [await self.status(client, expected_tools) for client in clients]
                        common.require(len({s["engine_instance_id"] for s in baseline}) == 1 and all(s["queue_depth"] == 0 for s in baseline),
                            "installed_textgen_engine_not_shared")
                        identities = common.private_json(state / "slots" / slot_id / "ownership.json")
                        graph = resident_graph(await self.manager_get(state, "/v1/slots/" + slot_id + "/details"),
                            identities, baseline[0]["engine_instance_id"])
                        common.require(common.private_json(state / "slots" / slot_id / "reservation.json")["gpu"] is False,
                            "installed_textgen_resident_graph_mismatch")
                        # All requests use the actual SDK and actual pinned CPU
                        # engine. Long bounded input makes live admission visible.
                        arguments = {"text": "x " * 380 + "\nList integers starting at one.", "max_output_tokens": 64}
                        pending = [asyncio.create_task(clients[i % 3].call_tool("text_generate", arguments)) for i in range(8)]
                        peak = 0
                        try:
                            while not all(task.done() for task in pending):
                                value = await self.status(first, expected_tools)
                                peak = max(peak, value["queue_depth"])
                                await asyncio.sleep(.005)
                            results = await join_tasks(pending)
                            completed = [r for r in results if not isinstance(r, BaseException) and not r.isError]
                            rejected = [r for r in results if not isinstance(r, BaseException) and has_error_code(r, "queue_full")]
                            common.require(len(completed) + len(rejected) == 8 and len(completed) == 4
                                and len(rejected) == 4 and peak == 4, "installed_textgen_queue_bound_failed")
                            for result in completed:
                                validate_generation(result.structuredContent, 64, expected_tools)
                            settled = await self.status(first, expected_tools)
                            common.require(settled["queue_depth"] == 0 and settled["completed"] == baseline[0]["completed"] + 4,
                                "installed_textgen_queue_accounting_failed")
                        finally:
                            await join_tasks(pending)
                        pending = []
                        rejections = 0
                        for invalid, expected in (({"text": "synthetic", "max_output_tokens": 65}, "invalid_request"),
                                ({"text": "é" * 4097, "max_output_tokens": 64}, "input_limit"),
                                ({"text": "x " * 1000, "max_output_tokens": 64}, "context_limit")):
                            result = await first.call_tool("text_generate", invalid)
                            common.require(has_error_code(result, expected),
                                "installed_textgen_boundary_admitted")
                            rejections += 1
                        before = await self.status(first, expected_tools)
                        observed.sent.clear()
                        requested_before = len(observed.request_ids)
                        active = asyncio.create_task(first.call_tool("text_generate", arguments))
                        pending = [active]
                        try:
                            await observed.sent.wait()
                            common.require(len(observed.request_ids) == requested_before + 1, "textgen_cancel_request_identity_missing")
                            async with asyncio.timeout(10):
                                while True:
                                    during = await self.status(second, expected_tools)
                                    if during["queue_depth"] == 1:
                                        common.require(during["completed"] == before["completed"]
                                            and during["engine_instance_id"] == before["engine_instance_id"],
                                            "textgen_cancel_inference_already_completed")
                                        break
                                    common.require(not active.done(), "textgen_cancel_inference_not_observed")
                                    await asyncio.sleep(.005)
                            await first.send_notification(ClientNotification(CancelledNotification(
                                params=CancelledNotificationParams(requestId=observed.request_ids[-1]))))
                            samples, live_samples = 0, 0
                            while not active.done():
                                common.require(common.private_json(state / "slots" / slot_id / "ownership.json") == identities
                                    and (state / "slots" / slot_id / "reservation.json").is_file(),
                                    "textgen_cancel_ownership_released_early")
                                value = await self.status(second, expected_tools)
                                samples += 1
                                if value["queue_depth"] > 0 and value["completed"] == before["completed"]:
                                    live_samples += 1
                                await asyncio.sleep(.005)
                            canceled = (await join_tasks([active]))[0]
                            if not isinstance(canceled, BaseException) or type(canceled).__name__ != "McpError":
                                failure = common.AcceptanceError("textgen_cancel_sdk_response_mismatch")
                                failure.context = {**common.DIAGNOSTIC, "sdk_result_type": type(canceled).__name__,
                                    "sdk_tool_error": getattr(canceled, "isError", None),
                                    "queue_successes": len(completed), "queue_rejections": len(rejected),
                                    "boundary_rejections": rejections}
                                raise failure
                            live_before_sdk_completion = live_samples
                            async with asyncio.timeout(60):
                                while True:
                                    common.require(common.private_json(state / "slots" / slot_id / "ownership.json") == identities
                                        and (state / "slots" / slot_id / "reservation.json").is_file(), "textgen_cancel_ownership_released_early")
                                    settled = await self.status(second, expected_tools)
                                    samples += 1
                                    if settled["queue_depth"] > 0 and settled["completed"] == before["completed"]:
                                        live_samples += 1
                                    if settled["queue_depth"] == 0:
                                        break
                                    await asyncio.sleep(.005)
                            common.require(settled["completed"] == before["completed"] + 1
                                and settled["engine_instance_id"] == before["engine_instance_id"], "textgen_cancel_inference_unsettled")
                            common.require(live_samples > 0, "textgen_cancel_live_ownership_not_observed")
                            recovered = await self.generation(third, expected_tools, 1)
                        finally:
                            await join_tasks(pending)
                        return {"clients": 3, **graph, "shared_engine": True, "queue_limit": 4,
                            "queue_depth_peak": peak, "queue_successes": len(completed), "queue_rejections": len(rejected),
                            "boundary_rejections": rejections, "sdk_cancellation_observed": True,
                            "canceled_inference_completed": True, "ownership_samples_during_settlement": samples,
                            "live_ownership_samples_during_settlement": live_samples,
                            "live_ownership_samples_before_sdk_completion": live_before_sdk_completion,
                            "reservation_preserved_until_settlement": True, "post_cancel_generation": recovered}

    async def capacity_denial(self, state, environment, home, package, slot_id):
        plan = await self.cli(state, environment, home, "install", "plan", "--package", package,
            "--start", "--start-policy", "manual", "--json")
        operation = await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json")
        async with asyncio.timeout(180):
            while operation["status"] in {"pending", "running"}:
                await asyncio.sleep(.1)
                operation = await self.cli(state, environment, home, "operations", "get", operation["operation_id"], "--json")
        common.require(operation["status"] == "failed" and operation["error_code"] == "resource_blocked", "textgen_second_slot_capacity_admitted")
        common.require(common.private_json(state / "slots" / slot_id / "reservation.json") == {"memory_mb": 1088, "gpu": False},
            "textgen_reservation_mismatch")
        second = state / "slots" / operation["slot_id"]
        common.require(not (second / "reservation.json").exists()
            and (not (second / "ownership.json").exists() or common.private_json(second / "ownership.json") == {}),
            "textgen_capacity_denial_retained_work")
        return {"memory_budget_mb": 1088, "declared_memory_mb": 1024, "headroom_mb": 64,
            "gpu": False, "second_slot_denied": True, "error_code": "resource_blocked"}

    async def textgen_case(self, label):
        from meecp.contracts import Tool, validate_package
        from meecp.preparation_inputs import parse_preparation_receipt, parse_preparation_descriptor
        from meecp.preparation_textgen import task_tools, task_fixtures, references, VERSION
        from meecp.preparation_contracts import canonical_bytes
        common.stage("source_intake")
        common.DIAGNOSTIC["case"] = label
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
                (source / "README.md").write_text("Ordinary project notes for a local text app.\n")
                draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                    "--name", "Native supplemented text app", "--intent", "Generate bounded local text.",
                    "--start", "--start-policy", "host", "--json")
                original = draft["source"]
            recipe = await self.recipe(state, environment, home)
            common.require(recipe["state"] == "enabled", "textgen_default_recipe_missing")
            code, receipt, output = await self.native_capture(base, state, home, environment, model, license, draft)
            if code or receipt.get("passed") is not True:
                failure = common.AcceptanceError(receipt.get("error_code", "native_acceptance_failed"))
                failure.native_stage = receipt.get("stage") if receipt.get("stage") in capture.NATIVE_STAGES else "connection"
                failure.native_observation = {"case": label, "native_exit_code": code, "receipt_sha256": common.digest(output),
                    "capture_completed": "capture_job_id" in receipt, "reviewed_revision": receipt.get("reviewed_revision")}
                raise failure
            common.require(receipt["stage"] == "attached" and receipt["capture_target"] == ("create-import" if label == "create" else "attach-to-import")
                and receipt["recipe_id"] == recipe["recipe_id"] and receipt["recipe_digest"] == recipe["digest"], "native_textgen_binding_mismatch")
            capture_id, import_id = receipt["capture_job_id"], receipt["draft_id"]
            routes = set(receipt["routes"])
            common.require({"POST v1/artifact-captures", "POST v1/preparations/form", "POST v1/preparations/plan-form",
                "POST v1/preparations/start", "GET v1/artifact-captures/" + capture_id} <= routes
                and not any("v1/resolutions/" in route for route in routes), "native_textgen_routes_mismatch")
            captured = await self.manager_get(state, "/v1/artifact-captures/" + capture_id)
            common.require(captured["state"] == "succeeded" and captured["ownership_settled"] is True
                and captured["reservation_retained"] is False and captured["collection_digest"] == receipt["capture_collection_digest"]
                and captured["result"] == {"import_id": import_id, "revision": receipt["captured_revision"]}, "native_capture_receipt_mismatch")
            prepared = await self.cli(state, environment, home, "preparations", "get", receipt["preparation_job_id"], "--json")
            attached = await self.cli(state, environment, home, "imports", "inspect", import_id, "--json")
            common.require(prepared["state"] == "succeeded" and prepared["ownership_settled"] is True
                and prepared["reservation_retained"] is False and common.preparation_ownership_count(state) == 0
                and not any(capture.capture_ownership(state).values()), "native_textgen_ownership_retained")
            common.require(attached["revision"] == receipt["reviewed_revision"] == receipt["attached_revision"]
                and attached["revision"] >= receipt["captured_revision"] and attached["source"]["revision"] == receipt["source_revision"]
                and attached["state"] == "ready_to_plan" and attached["start"] is True and attached["start_policy"] == "host",
                "native_textgen_revision_mismatch")
            if original is not None:
                common.require(attached["source"] == original and attached["captured_artifacts"]["digest"] == captured["collection_digest"],
                    "supplement_original_namespace_changed")
            else:
                common.require(attached["source"]["kind"] == "captured_artifact_collection"
                    and attached["source"]["revision"] == "sha256:" + captured["collection_digest"], "captured_source_identity_mismatch")
            package = validate_package(Path(attached["attachment"]["path"]))
            common.require(package.digest == attached["attachment"]["digest"] == prepared["expected_package_digest"]
                == receipt["attached_package_digest"], "native_textgen_package_identity_mismatch")
            proof = parse_preparation_receipt((common.receipt_path(package)).read_bytes())
            descriptor = parse_preparation_descriptor(capture.selected_descriptor(state, import_id))
            common.require(proof.schema_version == 2 and proof.inputs.descriptor_version == 2
                and len(proof.inputs.components) == 2 and {item.key for item in proof.inputs.components} == {ref.key for ref in references()}
                and {item.id for item in proof.inputs.components} == COMPONENT_IDS
                and proof.inputs.captured_collection.digest == captured["collection_digest"]
                and proof.binding.revision == attached["revision"] and proof.binding.source_revision == attached["source"]["revision"]
                and not proof.inputs.original_dependency_locks and proof.inputs.digest == prepared["bound_inputs_digest"]
                and canonical_bytes(proof.inputs.model_dump(mode="json")) == canonical_bytes(prepared["bound_inputs"]),
                "native_textgen_input_closure_mismatch")
            expected_records = expected_source_files(attached)
            common.require(len(proof.inputs.source_validations) == len(proof.inputs.components)
                and all([item.model_dump(mode="json") for item in binding.files] == expected_records
                for binding in proof.inputs.source_validations), "native_textgen_original_files_mismatch")
            common.require(common.digest(model) == MODEL_SHA256 and model.stat().st_size == MODEL_BYTES,
                "selected_model_mutated")
            common.require(prepared["verification"]["tools_checked"] == 2
                and prepared["verification"]["fixtures_checked"] == len(task_fixtures())
                and prepared["verification"]["package_digest"] == package.digest, "textgen_probe_mismatch")
            common.stage("installation")
            plan = await self.cli(state, environment, home, "install", "plan", "--import", import_id,
                "--revision", attached["revision"], "--json")
            expected_tools = [Tool.model_validate(item).mcp_schema() for item in plan["package"]["tools"]]
            common.require(expected_tools == [tool.mcp_schema() for tool in task_tools()]
                == [item.mcp_schema() for item in package.manifest.tools] and package.manifest.profile == "model-service"
                and plan["start"] is True and plan["start_policy"] == "host", "installed_textgen_contract_mismatch")
            operation = await self.finish_operation(state, environment, home,
                await self.cli(state, environment, home, "install", "apply", "--plan", plan["plan_id"], "--json"))
            slot_id = operation["slot_id"]
            installed = await self.cli(state, environment, home, "imports", "inspect", import_id, "--json")
            common.require(installed["state"] == "installed" and installed["result_slot_id"] == slot_id, "installed_textgen_binding_mismatch")
            calls = [(item["tool"], item["arguments"], item["expected"]) for item in task_fixtures()]
            http_calls = await self.sdk(state, environment, home, slot_id, expected_tools, calls, "http")
            stdio_calls = await self.sdk(state, environment, home, slot_id, expected_tools, calls, "stdio")
            generations = [await self.sdk_generation(state, environment, home, slot_id, expected_tools, transport) for transport in ("http", "stdio")]
            limits = await self.runtime_limits(state, environment, home, slot_id, expected_tools)
            common.stage("source_removal")
            shutil.rmtree(selected)
            if source is not None:
                shutil.rmtree(source)
            await self.shutdown(child, state)
            # Production admission policy is read only at startup. Change only
            # the isolated state after its owner and all instances have joined.
            policy = state / "resource-policy.json"
            policy.unlink()
            common.write_private_json(policy, {"memory_budget_mb": 1088, "headroom_mb": 64, "gpu_policy": "exclusive-residency"})
            common.stage("restart")
            child = await self.start(state, environment, home, enabled=False)
            retained = await self.recipe(state, environment, home)
            common.require(retained["state"] == "enabled" and retained["digest"] == recipe["digest"] and retained["epoch"] == recipe["epoch"],
                "textgen_enabled_recipe_restart_failed")
            capacity = await self.capacity_denial(state, environment, home, package.root, slot_id)
            for transport in ("http", "stdio"):
                count = await self.sdk(state, environment, home, slot_id, expected_tools, calls, transport)
                if transport == "http":
                    http_calls += count
                else:
                    stdio_calls += count
                generations.append(await self.sdk_generation(state, environment, home, slot_id, expected_tools, transport))
            common.stage("recipe_lifecycle")
            disabled = await self.recipe(state, environment, home, "disable")
            await self.shutdown(child, state)
            child = await self.start(state, environment, home, enabled=False)
            retained = await self.recipe(state, environment, home)
            common.require(retained["state"] == "disabled" and retained["digest"] == disabled["digest"] and retained["epoch"] == disabled["epoch"],
                "textgen_disabled_recipe_restart_failed")
            for transport in ("http", "stdio"):
                count = await self.sdk(state, environment, home, slot_id, expected_tools, calls, transport)
                if transport == "http":
                    http_calls += count
                else:
                    stdio_calls += count
                generations.append(await self.sdk_generation(state, environment, home, slot_id, expected_tools, transport))
            slot = await self.cli(state, environment, home, "slots", "inspect", slot_id, "--json")
            relocated = base / "independent installed package"
            shutil.copytree(Path(slot["package_path"]), relocated, symlinks=True)
            common.require(validate_package(relocated).digest == package.digest, "installed_package_relocation_mismatch")
            ownership = capture.capture_ownership(state)
            await self.shutdown(child, state)
            self.retire_socket(state)
            shutil.rmtree(state)
            workspace = base / "probe"
            workspace.mkdir(mode=0o700)
            verification = await relocation_probe(relocated, descriptor, self.reaper, workspace)
            common.require(verification.tools_checked == 2 and verification.fixtures_checked == len(task_fixtures())
                and verification.protocol_version == "2025-11-25"
                and 0 < verification.peak_memory_bytes <= package.manifest.resources.memory_mb * 1024**2,
                "textgen_relocation_probe_report_mismatch")
            common.require(validate_package(relocated).digest == package.digest, "textgen_probe_mutated_package")
            return {"passed": True, "capture_target": label, "native_receipt_sha256": common.digest(output),
                "native_routes": sorted(routes), "captured_collection_digest": captured["collection_digest"],
                "captured_revision": receipt["captured_revision"], "reviewed_revision": receipt["reviewed_revision"],
                "attached_revision": receipt["attached_revision"], "same_revision_attachment": True,
                "original_folder_preserved": label == "supplement", "component_ids": sorted(COMPONENT_IDS),
                "recipe_digest": retained["digest"], "recipe_version": VERSION, "enabled_recipe_restart": True, "disabled_recipe_restart": True,
                "installed_runtime_after_disable": True, "package_digest": package.digest,
                "full_tool_contract_sha256": hashlib.sha256(canonical_bytes(expected_tools)).hexdigest(),
                "http_fixture_calls": http_calls, "stdio_fixture_calls": stdio_calls, "generations": generations,
                "source_removed": True, "restart_invoked": True, "captured_state_removed_before_relocation_probe": True,
                "relocation_fixtures": verification.fixtures_checked, "relocation_probe_peak_memory_bytes": verification.peak_memory_bytes,
                "declared_app_memory_mb": package.manifest.resources.memory_mb, "runtime_limits": limits, "capacity": capacity,
                "preparation_progress": prepared["progress"], "probe_peak_memory_bytes": prepared["verification"]["peak_memory_bytes"],
                "capture_ownership_remaining": ownership, "elapsed_seconds": round(time.monotonic() - started, 3)}
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
    verifier_identities = harness_identities()
    _host_paths(host_path)
    common.require(native_path.is_file() and native_path.resolve() == native_path and os.access(native_path, os.X_OK), "native_acceptance_binary_required")
    common.require(model.is_file() and model.stat().st_size == MODEL_BYTES and common.digest(model) == MODEL_SHA256,
        "textgen_selected_model_identity_mismatch")
    with isolated_workspace(report) as (root, lease):
        host, native = common.relocate_host_and_native(root, host_path, native_path, report)
        _host_paths(host); _host_imports(host)
        identity = await asyncio.to_thread(sealed_host_identity, host)
        from meecp_textgen import fixture
        common.require((fixture.MODEL_BYTES, fixture.MODEL_SHA256, fixture.LICENSE_SHA256) == (MODEL_BYTES, MODEL_SHA256, LICENSE_SHA256),
            "textgen_host_fixture_identity_mismatch")
        report.update({"host_source_commit": identity["source_commit"], "host_source_tree_dirty": False,
            "host_build_sha256": common.digest(host / "build.json"), "host_artifacts_checked": identity["host_hashes_checked"],
            "native_binary_sha256": common.digest(native), "relocated_host": True, "minimal_environment": True,
            "private_sdk": True, "development_execution_only": True, "admitted_task": "text-generate-v1",
            "selected_model_sha256": MODEL_SHA256, "selected_model_bytes": MODEL_BYTES,
            "harness_artifact_sha256": verifier_identities})
        harness = Harness(root, host, native, lease, model)
        try:
            code, _ = await harness.run([str(native), "--artifact-capture-ui-self-test"],
                {"HOME": str(root), "PATH": "/usr/bin:/bin", "TMPDIR": str(root)}, root)
            common.require(code == 0, "native_capture_ui_contracts_failed")
            report["native_capture_ui_contracts"] = True
            report["production_defaults_disabled"] = await harness.defaults()
            report["production_native_capture"] = await harness.production_capture_denial()
            for label in CASES:
                report["cases"][label] = await harness.textgen_case(label)
            check_origins(host)
            common.require(await asyncio.to_thread(sealed_host_identity, host) == identity, "textgen_sealed_host_mutated")
            report["sealed_host_unchanged"] = True
        finally:
            try:
                await harness.cleanup()
            except BaseException:
                lease["safe_to_remove"] = False
                raise
            finally:
                report["owned_children_joined"] = not harness.children
        common.require(common.digest(model) == MODEL_SHA256 and model.stat().st_size == MODEL_BYTES, "textgen_original_model_mutated")
        common.require(harness_identities() == verifier_identities, "textgen_harness_mutated")
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
    report = {"schema_version": 1, "verified": False, "scope": "native_installed_textgen_onboarding", "cases": {},
        "a4_scope_complete": False, "limits": ["synthetic_generation_does_not_measure_language_quality",
            "crash_revision_cancel_races_unverified", "probe_filesystem_network_isolation_unverified",
            "hard_aggregate_live_memory_unverified", "distribution_signing_unverified", "final_adversarial_review_open", "public_execution_disabled"]}
    harness_hash = common.digest(Path(__file__))
    report["harness_sha256"] = harness_hash
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), args.model.resolve(strict=True), report))
        common.require(common.digest(Path(__file__)) == harness_hash, "textgen_harness_mutated")
    except (Exception, KeyboardInterrupt) as error:
        report["verified"] = False
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
