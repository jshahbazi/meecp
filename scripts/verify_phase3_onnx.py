"""Development acceptance for one fresh built host's actual captured ONNX lane.

The Python 3.12 parent is a development verifier. Product code and all launched
commands come from the selected host and its verified private toolchain. This
does not install a slot or enable the public automatic-preparation capability.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import time
import uuid
import zlib

from verify_phase3_primitives import VerificationError, _check_module_origins, _host_imports, _host_paths

STAGE = "bootstrap"
WALL_SECONDS = 300
DISK_BYTES = 4 * 1024**3
SAFE_STAGES = frozenset({"bootstrap", "host_hashes", "capture", "forms", "planning", "building", "relocation",
                        "removing_inputs", "relocation_probe", "max_image_probe", "closing"})


def stage(value):
    global STAGE
    STAGE = value if value in SAFE_STAGES else "bootstrap"


def host_hashes(host):
    stage("host_hashes")
    data = json.loads((host / "build.json").read_bytes())
    hashes = data["artifact_sha256"]
    if data["source_tree_dirty"] is not False or not isinstance(hashes, dict) or not 1 <= len(hashes) <= 30000:
        raise VerificationError("phase3_onnx_frozen_host_required")
    for relative, expected in hashes.items():
        path = host / relative
        if (not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.resolve() != path or not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise VerificationError("phase3_onnx_host_identity_mismatch")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise VerificationError("phase3_onnx_host_identity_mismatch")
    return {"source_commit": data["source_commit"], "host_version": data["host_version"], "host_hashes_checked": len(hashes)}


def blank_png(width, height):
    def chunk(kind, payload):
        return struct.pack("!I",len(payload)) + kind + payload + struct.pack("!I",zlib.crc32(kind+payload)&0xffffffff)
    image = zlib.compress((b"\0" + b"\0"*(width*3))*height,level=9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR",struct.pack("!IIBBBBB",width,height,8,2,0,0,0))
            + chunk(b"IDAT",image) + chunk(b"IEND",b""))


def copy_selected_model(source, target, size, digest):
    from mcpme.preparation_builder import _opened
    from mcpme.preparation_contracts import PreparationError
    total, value = 0, hashlib.sha256()
    with _opened(source) as original, target.open("xb") as copied:
        while chunk := original.read(1024*1024):
            total += len(chunk)
            if total > size:
                raise PreparationError("phase3_onnx_model_identity_mismatch")
            copied.write(chunk); value.update(chunk)
    if total != size or value.hexdigest() != digest:
        raise PreparationError("phase3_onnx_model_identity_mismatch")


@asynccontextmanager
async def transport(process):
    import anyio
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage
    from mcpme.preparation_contracts import PreparationError
    incoming_send, incoming = anyio.create_memory_object_stream(1)
    outgoing, outgoing_receive = anyio.create_memory_object_stream(1)
    async def read():
        async with incoming_send:
            try:
                while line := await process.stdout.readline():
                    if len(line) > 1024*1024:
                        raise PreparationError("phase3_onnx_response_limit")
                    await incoming_send.send(SessionMessage(JSONRPCMessage.model_validate_json(line)))
            except Exception:
                await incoming_send.send(PreparationError("phase3_onnx_protocol_failed"))
    async def write():
        async with outgoing_receive:
            async for message in outgoing_receive:
                data = message.message.model_dump_json(by_alias=True,exclude_none=True).encode()+b"\n"
                if len(data) > 2*1024*1024:
                    raise PreparationError("phase3_onnx_request_limit")
                process.stdin.write(data); await process.stdin.drain()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(read); tasks.start_soon(write)
        try:
            async with incoming,outgoing:
                yield incoming,outgoing
        finally:
            tasks.cancel_scope.cancel()


async def max_probe(package, reaper, workspace, memory_mb):
    from mcp import ClientSession
    from mcpme.preparation_contracts import PreparationError
    from mcpme.preparation_probe_confinement import ProbeConfinementV1
    from mcpme.preparation_verifier import capture_confined_probe_launch
    from mcpme.processes import ProcessOwner, _await_ownership, _watch_ownership
    owner, process = ProcessOwner(reaper), None
    started = time.monotonic()
    try:
        async with asyncio.timeout(120):
            deadline = time.monotonic() + 120
            task = asyncio.create_task(asyncio.to_thread(ProbeConfinementV1.capture, package, workspace, reaper))
            try:
                confinement = await asyncio.shield(task)
            finally:
                await _await_ownership(asyncio.gather(task, return_exceptions=True))
            launch, identity = await capture_confined_probe_launch(package, confinement, reaper)
            def admit():
                identity.admit()
                if time.monotonic() >= deadline:
                    raise PreparationError("phase3_onnx_probe_timeout")
                return True
            process = await owner.launch(*launch, admit=admit, expected_reaper=confinement.reaper,
                owned_group=True,startup_timeout=30,stop_grace=.1)
            async def calls():
                async with transport(process) as (incoming,outgoing):
                    async with ClientSession(incoming,outgoing) as client:
                        initialized = await client.initialize()
                        if initialized.protocolVersion != "2025-11-25":
                            raise PreparationError("phase3_onnx_protocol_mismatch")
                        listing = await client.list_tools()
                        if [item.model_dump(by_alias=True,exclude_none=True) for item in listing.tools] != [
                                item.mcp_schema() for item in package.manifest.tools]:
                            raise PreparationError("phase3_onnx_tools_mismatch")
                        data = blank_png(1024,1024)
                        data += b"\0"*(1024*1024-len(data))
                        encoded = base64.b64encode(data).decode()
                        start = time.monotonic()
                        result = await client.call_tool("detect_hotdog",{"image_base64":encoded})
                        duration = time.monotonic()-start
                        if (result.isError or result.structuredContent["image"] != {"width":1024,"height":1024}
                                or duration >= 60):
                            raise PreparationError("phase3_onnx_max_image_failed")
                        results = await asyncio.gather(*(client.call_tool("detect_hotdog",{"image_base64":encoded})
                                                        for _ in range(4)))
                        busy = sum(item.isError and len(item.content)==1 and item.content[0].text=="detector_busy" for item in results)
                        if busy < 1 or any(item.isError and item.content[0].text != "detector_busy" for item in results):
                            raise PreparationError("phase3_onnx_concurrency_failed")
                        boundary = [base64.b64encode(data+b"\0").decode(),base64.b64encode(blank_png(1025,1024)).decode()]
                        for value,code in zip(boundary,["image_size_limit","image_pixel_limit"]):
                            rejected = await client.call_tool("detect_hotdog",{"image_base64":value})
                            if not rejected.isError or len(rejected.content)!=1 or rejected.content[0].text != code:
                                raise PreparationError("phase3_onnx_boundary_failed")
                        return {"max_request_call_seconds":round(duration,6),"concurrent_calls":4,
                                "busy_rejections":busy,"max_sdk_bytes":len(data),"max_sdk_pixels":1024*1024,
                                "sdk_boundary_rejections":2}
            result = await _watch_ownership(process,calls())
        await _await_ownership(process.stop())
        if not process.peak_memory_bytes or process.peak_memory_bytes > memory_mb*1024**2:
            raise PreparationError("phase3_onnx_memory_limit")
        def validate_finished():
            from mcpme.contracts import validate_package
            identity.check()
            current = validate_package(package.root)
            if current.digest != package.digest:
                raise PreparationError("phase3_onnx_probe_mutated_package")
            confinement.check(current, reaper)
            identity.check()
        task = asyncio.create_task(asyncio.to_thread(validate_finished))
        try:
            await asyncio.shield(task)
        finally:
            await _await_ownership(asyncio.gather(task, return_exceptions=True))
        if owner.reaper != confinement.reaper:
            raise PreparationError("phase3_onnx_probe_identity_mismatch")
        result["max_sdk_peak_memory_bytes"] = process.peak_memory_bytes
        result["max_sdk_wall_seconds"] = round(time.monotonic()-started,6)
        result["max_sdk_confined"] = True
        result["max_sdk_policy_digest"] = confinement.policy_digest
        return result
    finally:
        try:
            if process is not None:
                await _await_ownership(process.stop())
            await _await_ownership(owner.close())
            if owner.instances:
                raise PreparationError("phase3_onnx_owned_work_remaining")
        except BaseException:
            (workspace.parent / "ownership-unsettled").touch(mode=0o600)
            raise


async def relocation_probe(package_root, descriptor, reaper, workspace):
    from mcpme.preparation_contracts import PreparationError
    from mcpme.preparation_verifier import verify_prepared_package
    from mcpme.processes import ProcessOwner, _await_ownership
    owner = ProcessOwner(reaper)
    try:
        return await verify_prepared_package(package_root, descriptor, reaper, workspace,
                                             timeout_seconds=60, owner=owner, confine_private_python=True)
    finally:
        try:
            await _await_ownership(owner.close())
            if owner.instances:
                raise PreparationError("phase3_onnx_owned_work_remaining")
        except BaseException:
            (workspace.parent / "ownership-unsettled").touch(mode=0o600)
            raise


async def verify(host, model, base):
    _host_paths(host)
    receipt = await asyncio.to_thread(host_hashes,host)
    _host_imports(host)
    from mcpme.contracts import validate_package
    from mcpme.preparation_artifacts import AcquisitionRequestV1
    from mcpme.preparation_capture import CapturePolicyV1, CreateImportTargetV1
    from mcpme.preparation_contracts import ExecutionPolicy, PreparationError
    from mcpme.preparation_onnx import MODEL_BYTES, MODEL_SHA256
    from mcpme.processes import _await_ownership
    from mcpme.supervisor import Supervisor
    _check_module_origins(host)
    product = Supervisor(base / "state", host / "bin/mcpme-reap")
    imports, capture_store, store = product.imports, product.captures, product.preparations
    forms, coordinator, runner = product.preparation_forms, product.capture_runner, product.preparation_runner
    reserved = product.preparation_reservations
    async def files(function,*args,**kwargs):
        task = asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
        try:
            return await asyncio.shield(task)
        finally:
            await _await_ownership(asyncio.gather(task,return_exceptions=True))
    started = time.monotonic()
    try:
        if runner.enabled or not all(item["ready"] for item in product.builtin_recipe_metadata["recipes"]):
            raise PreparationError("phase3_onnx_default_registration_failed")
        # Only this private development host executes. Product defaults stay gated.
        runner.enabled = True
        from mcpme.preparation_onnx import component_reference
        runner.enabled_families = frozenset({component_reference().key})
        async with asyncio.timeout(WALL_SECONDS):
            stage("capture")
            selected = base / "selected"; selected.mkdir(mode=0o700)
            await files(copy_selected_model,model,selected / "model.onnx",MODEL_BYTES,MODEL_SHA256)
            await files(shutil.copyfile,host / "templates/onnx-hotdog-v1/LICENSE-MODEL",selected / "LICENSE")
            request = AcquisitionRequestV1(entries=[
                {"name":"weights","role":"model-weights","path":str(selected / "model.onnx"),"format":"onnx",
                 "expected_sha256":MODEL_SHA256,"expected_size":MODEL_BYTES,"licenses":["license"]},
                {"name":"license","role":"license","path":str(selected / "LICENSE"),"format":"text"}])
            captured = await coordinator.start(request,CapturePolicyV1(disk_ceiling_bytes=64*1024**2),
                CreateImportTargetV1(name="Hotdog Detector",requirements={"tools":["detect_hotdog"],"memory_mb":512,
                                                                         "profile":"mcp-stdio"}),"actual-onnx")
            await asyncio.shield(coordinator.tasks[captured["capture_id"]])
            completed = capture_store.get(captured["capture_id"])
            if completed["state"] != "succeeded":
                raise PreparationError(completed["error_code"] or "phase3_onnx_capture_failed")
            draft = await files(imports._get,completed["result"]["import_id"])
            await files(shutil.rmtree,selected)
            stage("forms")
            form = await files(forms.form,draft["draft_id"],draft["revision"])
            if form["blockers"] or form["execution_available"] is not False:
                raise PreparationError("phase3_onnx_form_failed")
            descriptor,context = await files(forms.resolve,draft["draft_id"],draft["revision"],
                candidate_id=form["candidate_id"],recipe=form["recipe"],form_digest=form["form_digest"],answers={})
            draft = await files(store.select_descriptor,draft["draft_id"],draft["revision"],descriptor,
                                candidate_id=form["candidate_id"],form_digest=form["form_digest"])
            stage("planning")
            policy = ExecutionPolicy(schema_version=1,operations=["build","probe"],network=["files.pythonhosted.org"],
                                     wall_seconds=120,disk_bytes=DISK_BYTES,memory_mb=2048)
            plan = await runner.plan(draft["draft_id"],draft["revision"],descriptor.model_dump(exclude_none=True),policy)
            stage("building")
            job = await runner.start(plan["plan_id"],plan["plan_digest"],str(uuid.uuid4()))
            await asyncio.shield(runner.tasks[job["job_id"]])
            final = store.get(job["job_id"])
            if final["state"] != "succeeded":
                raise PreparationError(final["error_code"] or "phase3_onnx_build_failed")
            attached = await files(imports.inspect,draft["draft_id"])
            if (attached["revision"] != draft["revision"] or not attached["attachment"]
                    or attached["attachment"]["digest"] != final["expected_package_digest"]
                    or not final["ownership_settled"] or final["reservation_retained"] or reserved
                    or store.ownership_receipts(job["job_id"]) or Path(final["workspace"]).exists()
                    or any(item["kind"]=="job" for item in capture_store.references(completed["collection_digest"]))):
                raise PreparationError("phase3_onnx_settlement_failed")
            package = await files(validate_package,Path(attached["attachment"]["path"]))
            workload = json.loads((package.root / "onnx-validation.json").read_bytes())
            await _await_ownership(product.close()); product=None
            stage("relocation")
            relocated = base / "relocated-package"
            await files(package.root.rename,relocated)
            digest = package.digest
            stage("removing_inputs")
            for path in (base / "state",):
                await files(shutil.rmtree,path)
            package = await files(validate_package,relocated)
            if package.digest != digest:
                raise PreparationError("phase3_onnx_relocation_identity_mismatch")
            probe = base / "probe"; probe.mkdir(mode=0o700)
            stage("relocation_probe")
            report = await relocation_probe(relocated,descriptor,host / "bin/mcpme-reap",probe)
            stage("max_image_probe")
            maximum_workspace = base / "maximum probe"; maximum_workspace.mkdir(mode=0o700)
            max_report = await max_probe(package,host / "bin/mcpme-reap",maximum_workspace,descriptor.resources.memory_mb)
            after = await files(validate_package,relocated)
            if after.digest != digest:
                raise PreparationError("phase3_onnx_probe_mutated_package")
            _check_module_origins(host)
            stage("closing")
            return {**receipt,"verified":True,"public_execution_available":False,"slot_installed":False,
                "model_sha256":MODEL_SHA256,"model_bytes":MODEL_BYTES,"package_digest":digest,
                "descriptor_version":descriptor.schema_version,"same_revision_attachment":True,
                "default_host_registration":True,"selected_components":len(descriptor.components),
                "selected_sources_removed":True,"captured_state_removed_before_relocation_probe":True,
                "commands":final["progress"]["commands_finished"],"wheels":final["progress"]["wheels_acquired"],
                "dependency_bytes":final["progress"]["bytes_acquired"],"fixtures":report.fixtures_checked,
                "relocation_probe_peak_memory_bytes":report.peak_memory_bytes,"relocation_probe_confined":True,
                "declared_memory_mb":descriptor.resources.memory_mb,
                "owned_work_remaining":0,"pending_receipts":0,"job_references_remaining":0,
                "workload":workload,**max_report,"wall_seconds":round(time.monotonic()-started,6)}
    finally:
        try:
            if product is not None:
                await _await_ownership(product.close())
        except BaseException:
            (base / "ownership-unsettled").touch(mode=0o600)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",type=Path,required=True,help="Absolute fresh complete built CLI host.")
    parser.add_argument("--model",type=Path,required=True,help="Absolute exact pinned ONNX file; never removed or modified.")
    parser.add_argument("--keep-artifacts",action="store_true")
    options = parser.parse_args()
    os.umask(0o077)
    base = Path(tempfile.mkdtemp(prefix="mcpme-phase3-onnx-",dir="/private/tmp")).resolve()
    result,failed = None,False
    try:
        if not options.model.is_absolute() or options.model.resolve() != options.model:
            raise VerificationError("phase3_onnx_absolute_model_required")
        result = asyncio.run(verify(options.host,options.model,base))
    except (Exception,KeyboardInterrupt) as error:
        code = getattr(error,"code","phase3_onnx_verification_failed")
        if not isinstance(code,str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,127}",code):
            code = "phase3_onnx_verification_failed"
        error_type = type(error).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}",error_type):
            error_type = "UnknownError"
        result = {"verified":False,"error":code,"stage":STAGE,"exception_type":error_type}
        failed = True
    if options.keep_artifacts or failed or (base / "ownership-unsettled").exists():
        result["private_artifact_directory"] = str(base)
        if failed:
            result["failure_artifacts_retained"] = True
        if (base / "ownership-unsettled").exists():
            result["ownership_retained"] = True
    else:
        try:
            shutil.rmtree(base)
        except OSError:
            result = {"verified":False,"error":"phase3_onnx_cleanup_failed","stage":"closing",
                      "exception_type":"OSError","private_artifact_directory":str(base)}
            failed = True
    print(json.dumps(result,sort_keys=True))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
