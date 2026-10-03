"""Verify durable preparation from one complete built CLI host.

This development harness runs with a Python 3.12 parent, imports only the
selected host implementation/dependencies, and explicitly opts into its private
runner. Actual commands/probes use its bundled tools and native ownership
receipts. Public automatic execution remains disabled; no slot is installed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from verify_phase3_primitives import VerificationError, _check_module_origins, _host_imports, _host_paths, _workspace_bytes

WALL_SECONDS = 300
JOB_WALL_SECONDS = 120
DISK_BYTES = 4 * 1024**3

CALLABLE_SOURCE = "def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n"
WAITING_SOURCE = "import time\ndef double(n: int) -> dict[str, int]:\n    time.sleep(100)\n    return {'value': n * 2}\n"
MCP_SOURCE = (
    "import anyio\nfrom mcp.server import Server\nfrom mcp.server.stdio import stdio_server\n"
    "from mcp.types import Tool, CallToolResult, TextContent\n"
    "app = Server('Synthetic durable MCP', version='1.0.0')\n"
    "@app.list_tools()\nasync def listing():\n"
    "    return [Tool(name='double', description='Double a synthetic integer', "
    "inputSchema={'type':'object','properties':{'n':{'type':'integer'}},'required':['n'],'additionalProperties':False}, "
    "outputSchema={'type':'object','properties':{'value':{'type':'integer'}},'required':['value'],'additionalProperties':False})]\n"
    "@app.call_tool(validate_input=False)\nasync def double(name, arguments):\n"
    "    return CallToolResult(content=[TextContent(type='text',text='synthetic')], structuredContent={'value':arguments['n']*2})\n"
    "async def serve():\n    async with stdio_server() as (incoming,outgoing):\n"
    "        options = app.create_initialization_options()\n        options.capabilities.experimental = None\n"
    "        await app.run(incoming,outgoing,options)\n"
    "if __name__ == '__main__':\n    anyio.run(serve)\n"
)


async def verify(host: Path, base: Path) -> dict:
    _host_paths(host)
    _host_imports(host)
    from mcpme.contracts import validate_package
    from mcpme.imports import ImportStore
    from mcpme.ownership_receipts import verify_receipt
    from mcpme.preparation_contracts import ExecutionPolicy, PreparationError
    from mcpme.preparation_processes import PreparationExecutor
    from mcpme.preparation_runner import JournaledPreparationOwner, PreparationRunner
    from mcpme.preparations import PreparationStore
    from mcpme.processes import OwnershipLost, _await_ownership
    from mcpme.recipe_sdk import scaffold_recipe
    from mcpme.recipes import RecipeStore
    _check_module_origins(host)
    required = host / "backend/mcpme/preparation_runner.py"
    if not required.is_file():
        raise VerificationError("phase3_durable_built_runner_required")
    started = time.monotonic()
    deadline = started + WALL_SECONDS
    imports = ImportStore(base / "state")
    recipes = RecipeStore(imports)
    store = PreparationStore(imports, recipes)
    reserved = {}
    released = set()
    runners = []
    extra_owner = None
    outcomes = []
    package_paths = []
    sources = []
    peak_disk = 0

    def check():
        if time.monotonic() >= deadline:
            raise PreparationError("phase3_durable_wall_limit")

    async def files(function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        finally:
            await _await_ownership(asyncio.gather(task, return_exceptions=True))

    async def reserve(identifier, policy):
        check()
        if identifier in reserved and reserved[identifier] != policy.digest:
            raise PreparationError("phase3_durable_reservation_mismatch")
        reserved[identifier] = policy.digest

    async def release(identifier, *, ownership_settled):
        job = store.get(identifier)
        if not ownership_settled or not job["ownership_settled"] or job["reservation_retained"]:
            raise PreparationError("phase3_durable_premature_release")
        reserved.pop(identifier, None)
        released.add(identifier)

    runner = PreparationRunner(store, host, host / "bin/mcpme-reap", enabled=True,
                               reserve_resources=reserve, release_resources=release)
    runners.append(runner)
    policy = ExecutionPolicy(schema_version=1, operations=["build", "probe"], network=["files.pythonhosted.org"],
                             wall_seconds=JOB_WALL_SECONDS, disk_bytes=DISK_BYTES, memory_mb=2048)

    def fixture(label, kind, source_text):
        source = base / (label + "-source")
        source.mkdir(mode=0o700)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(host / name, source / name)
        shutil.copyfile(host / "templates/LICENSE", source / "LICENSE")
        (source / "tool.py").write_text(source_text)
        sources.append(source)
        draft = imports.create(source, name="Synthetic durable fixture", intent="Expose one synthetic integer transformation")
        bundle = base / (label + "-recipe")
        scaffold_recipe(bundle, "test.durable-" + label,
                        component="python-function-v1" if kind == "python-callable" else "existing-mcp-v1")
        recipe = recipes.add(bundle)
        recipe = recipes.enable(recipe["recipe_id"], recipe["version"])
        tool = {"name": "double", "description": "Double a synthetic integer",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}},
                             "required": ["n"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}},
                              "required": ["value"], "additionalProperties": False}}
        interface = {"kind": kind, "module": "tool", "tools": [tool]}
        if kind == "python-callable":
            interface["function"] = "double"
        descriptor = {"schema_version": 1,
            "recipe": {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
            "package": {"id": "test.durable-" + label, "version": "1.0.0", "name": "Synthetic durable fixture"},
            "interface": interface,
            "dependencies": {"project": ".", "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(),
                             "python_version": "3.12.13", "mcp_version": "1.30.0"},
            "source_license": "LICENSE",
            "inputs": {field["field"]: field["default"] for field in recipe["manifest"]["form"] if "default" in field},
            "resources": {"memory_mb": 128},
            "validation": [{"id": "synthetic", "tool": "double", "arguments": {"n": 3},
                            "expected": {"value": 6}, "synthetic": True}]}
        static = store.plan(draft["draft_id"], 1, descriptor)
        if static["execution_available"] is not False:
            raise PreparationError("phase3_durable_public_boundary_changed")
        return draft, descriptor

    async def wait_task(job_id):
        nonlocal peak_disk
        task = runner.tasks[job_id]
        while not task.done():
            check()
            peak_disk = max(peak_disk, await files(_workspace_bytes, base))
            await asyncio.wait({task}, timeout=0.05)
        await task
        check()
        return store.get(job_id)

    def clean_owners(job_id):
        return all(not owner.instances and not owner._launches and not owner._pending_ids
                   for owner in runner.attempts[job_id].owners)

    try:
        async with asyncio.timeout(WALL_SECONDS):
            # PATH deliberately has no developer tools. Product command envs
            # are closed and every executable is an absolute private path.
            os.environ["PATH"] = "/mcpme-verification-no-global-tools"
            for label, kind, text in (("callable", "python-callable", CALLABLE_SOURCE),
                                      ("existing", "python-mcp", MCP_SOURCE)):
                draft, descriptor = await files(fixture, label, kind, text)
                plan = await runner.plan(draft["draft_id"], 1, descriptor, policy)
                key = str(uuid.uuid4())
                job = await runner.start(plan["plan_id"], plan["plan_digest"], key)
                duplicate = await runner.start(plan["plan_id"], plan["plan_digest"], key)
                if duplicate["job_id"] != job["job_id"]:
                    raise PreparationError("phase3_durable_idempotency_failed")
                final = await wait_task(job["job_id"])
                if final["state"] != "succeeded":
                    raise PreparationError(final["error_code"] or "phase3_durable_attachment_failed")
                attached = imports.inspect(draft["draft_id"])
                if (attached["revision"] != 1 or attached["attachment"] is None
                        or attached["attachment"]["digest"] != final["expected_package_digest"]
                        or not final["ownership_settled"] or final["reservation_retained"]
                        or store.ownership_receipts(job["job_id"]) or not clean_owners(job["job_id"])
                        or Path(final["workspace"]).exists() or final["progress"]["commands_finished"] != 5):
                    raise PreparationError("phase3_durable_result_binding_failed")
                package_paths.append((Path(attached["attachment"]["path"]), final["expected_package_digest"]))
                outcomes.append({"case": label, "state": final["state"], "same_revision": True,
                    "package_digest": final["expected_package_digest"], "runner_digest": final["binding"]["runner_digest"],
                    "verifier_digest": final["binding"]["verifier_digest"], "runtime_digest": final["runtime_digest"],
                    "commands": final["progress"]["commands_finished"], "wheels": final["progress"]["wheels_acquired"],
                    "dependency_bytes": final["progress"]["bytes_acquired"], "fixtures": final["verification"]["fixtures_checked"],
                    "probe_peak_memory_bytes": final["verification"]["peak_memory_bytes"]})

            draft, descriptor = await files(fixture, "canceled", "python-callable", WAITING_SOURCE)
            plan = await runner.plan(draft["draft_id"], 1, descriptor, policy)
            job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
            while (store.get(job["job_id"])["state"] != "verifying" or
                    len(runner.attempts[job["job_id"]].owners) < 2 or
                    not runner.attempts[job["job_id"]].owners[-1].instances):
                check()
                if runner.tasks[job["job_id"]].done():
                    raise PreparationError(store.get(job["job_id"])["error_code"] or "phase3_durable_cancel_probe_missing")
                await asyncio.sleep(0.01)
            canceled = await runner.cancel(job["job_id"])
            if (canceled["state"] != "canceled" or not canceled["ownership_settled"] or canceled["reservation_retained"]
                    or store.ownership_receipts(job["job_id"]) or not clean_owners(job["job_id"])
                    or Path(canceled["workspace"]).exists() or imports.inspect(draft["draft_id"])["attachment"] is not None):
                raise PreparationError("phase3_durable_cancel_settlement_failed")
            outcomes.append({"case": "probe-cancellation", "state": "canceled", "owned_steps_remaining": 0,
                             "pending_receipts": 0, "attachment_created": False})

            # Native completion persists before the coordinator records settle.
            # A reconstructed runner releases it without adopting or replaying.
            draft, descriptor = await files(fixture, "restart", "python-callable", CALLABLE_SOURCE)
            plan = await runner.plan(draft["draft_id"], 1, descriptor, policy)
            job = await files(store.authorize_start, plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
            store.transition(job["job_id"], "snapshotting")
            workspace = imports.root / "preparations" / job["job_id"]
            workspace.mkdir(mode=0o700)
            store.set_workspace(job["job_id"], workspace)
            store.transition(job["job_id"], "preparing")
            await reserve(job["job_id"], policy)
            extra_owner = JournaledPreparationOwner(host / "bin/mcpme-reap", store, job["job_id"], workspace)
            executor = PreparationExecutor(host / "bin/mcpme-reap", owner=extra_owner)
            result = await executor.execute(host / "python/bin/python3.12", ["-I", "-S", "-c", "pass"],
                                           workspace, {}, timeout_seconds=10, stdout_limit=1024)
            await executor.close()
            metadata = store.ownership_receipts(job["job_id"])
            if (len(metadata) != 1 or not verify_receipt(workspace, metadata[0]) or extra_owner.instances
                    or result.peak_memory_bytes <= 0):
                raise PreparationError("phase3_durable_native_restart_proof_missing")
            restarted = PreparationRunner(store, host, host / "bin/mcpme-reap", enabled=True,
                                          reserve_resources=reserve, release_resources=release)
            runners.append(restarted)
            await restarted.recover()
            recovered = store.get(job["job_id"])
            if (recovered["state"] != "interrupted" or not recovered["ownership_settled"] or recovered["reservation_retained"]
                    or store.ownership_receipts(job["job_id"]) or workspace.exists() or restarted.tasks):
                raise PreparationError("phase3_durable_restart_replayed_or_unsettled")
            outcomes.append({"case": "native-restart-reconciliation", "state": "interrupted", "replayed": False,
                             "owned_steps_remaining": 0, "pending_receipts": 0})
            if reserved or len(released) != 4:
                raise PreparationError("phase3_durable_resource_reservation_remaining")
            for item in runners:
                await item.close()
            _check_module_origins(host)
            moved = base / "relocated"
            moved.mkdir(mode=0o700)
            relocated = []
            for index, (package, digest) in enumerate(package_paths):
                destination = moved / str(index)
                package.rename(destination)
                relocated.append((destination, digest))
            imports.close()
            imports = None
            for source in sources:
                shutil.rmtree(source)
            shutil.rmtree(base / "state")
            for destination, digest in relocated:
                package = await files(validate_package, destination)
                if package.digest != digest:
                    raise PreparationError("phase3_durable_relocated_package_changed")
            return {"verified": True, "scope": "development-durable-preparation-checkpoint",
                    "bundled_implementation": True, "public_execution_available": False,
                    "private_toolchain": True, "fresh_private_workspaces": True, "global_tools_available": False,
                    "source_recipe_bindings_validated": True, "packages_relocated": 2,
                    "source_state_builder_removed": True, "owned_steps_remaining": 0, "retained_reservations": 0,
                    "wall_limit_seconds": WALL_SECONDS, "job_wall_limit_seconds": JOB_WALL_SECONDS,
                    "disk_limit_bytes": DISK_BYTES, "peak_workspace_bytes": peak_disk,
                    "elapsed_seconds": round(time.monotonic() - started, 3), "cases": outcomes}
    finally:
        lost = False
        for item in reversed(runners):
            try:
                await _await_ownership(item.close())
            except Exception:
                lost = True
        if extra_owner is not None:
            try:
                await _await_ownership(extra_owner.close())
            except Exception:
                lost = True
        if lost:
            # Preserve durable proof/reservation state on uncertain ownership.
            (base / "ownership-unsettled").touch(mode=0o600)
            raise PreparationError("phase3_durable_ownership_unsettled") from None
        elif imports is not None:
            imports.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="Absolute complete built CLI-host directory.")
    parser.add_argument("--keep-artifacts", action="store_true", help="Retain the private fixture directory and relocated packages.")
    arguments = parser.parse_args()
    os.umask(0o077)
    base = result = failure = None
    try:
        _host_paths(arguments.host)
        base = Path(tempfile.mkdtemp(prefix="mcpme-phase3-durable-", dir="/private/tmp")).resolve()
        result = asyncio.run(verify(arguments.host, base))
    except KeyboardInterrupt:
        failure = "phase3_durable_interrupted"
    except Exception as error:
        code = getattr(error, "code", "phase3_durable_verification_failed")
        failure = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,127}", code) else "phase3_durable_verification_failed"
    preserve = base is not None and (arguments.keep_artifacts or (base / "ownership-unsettled").exists())
    if base is not None and not preserve:
        try:
            shutil.rmtree(base)
            if base.exists():
                raise OSError()
            if result is not None:
                result["temporary_artifacts_removed"] = True
        except OSError:
            failure = "phase3_durable_cleanup_failed"
    if failure is not None:
        result = {"verified": False, "error": failure}
    if preserve:
        result["artifact_directory"] = str(base)
    print(json.dumps(result, sort_keys=True))
    return 1 if failure is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
