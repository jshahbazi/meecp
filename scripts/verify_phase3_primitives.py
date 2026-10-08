"""Verify preparation primitives from one built CLI host.

Run this development verification harness with a compatible Python 3.12 parent
interpreter and --host pointing to an absolute, built CLI-host directory. The
harness imports the selected host's implementation and dependencies. Build and
probe processes use its private tools and native ownership executor.

This verifies direct composition of the primitives. Public automatic execution
remains unavailable, and the harness creates no installation or slot.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
import threading
import time

WALL_SECONDS = 300
DISK_BYTES = 4 * 1024**3
ALLOWED_WHEEL_HOSTS = frozenset({"files.pythonhosted.org"})
MAX_WORKSPACE_ENTRIES = 200000


class VerificationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _host_paths(host: Path) -> None:
    if not host.is_absolute() or host.resolve() != host or not host.is_dir() or host.is_symlink():
        raise VerificationError("phase3_primitives_absolute_host_required")
    required = ("backend/meecp/__init__.py", "python/bin/python3.12", "python/site-packages",
                "preparation-toolchain/toolchain.json", "bin/meecp-reap", "pyproject.toml", "uv.lock",
                "templates/LICENSE", "build.json")
    for relative in required:
        path = host / relative
        if not path.exists() or path.resolve() != path:
            raise VerificationError("phase3_primitives_built_host_required")
    if not os.access(host / "bin/meecp-reap", os.X_OK):
        raise VerificationError("phase3_primitives_native_reaper_required")


def _host_imports(host: Path) -> None:
    """Retain the parent standard library; resolve every package from the host."""
    if sys.version_info[:2] != (3, 12):
        raise VerificationError("phase3_primitives_python312_required")
    paths = sysconfig.get_paths()
    standard = []
    for value in (paths["stdlib"], paths["platstdlib"], str(Path(paths["stdlib"]) / "lib-dynload")):
        path = Path(value).resolve()
        if not any(part in {"site-packages", "dist-packages"} for part in path.parts) and str(path) not in standard:
            standard.append(str(path))
    sys.path[:] = [str(host / "backend"), str(host / "python/site-packages"), *standard]
    sys.dont_write_bytecode = True
    logging.disable(logging.CRITICAL)


def _check_module_origins(host: Path) -> None:
    backend = host / "backend"
    site = host / "python/site-packages"
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if name == "meecp._private_preparation_generator":
            if not filename or Path(filename).resolve() != host / "preparation-toolchain/templates/prepare.py":
                raise VerificationError("phase3_primitives_bundled_import_required")
            continue
        if name == "meecp" or name.startswith("meecp."):
            allowed = (backend,)
        elif name in {"mcp", "packaging"} or name.startswith(("mcp.", "packaging.")):
            allowed = (site,)
        else:
            continue
        origins = [filename] if filename else list(getattr(module, "__path__", ()))
        if not origins or len(origins) > 64:
            raise VerificationError("phase3_primitives_bundled_import_required")
        for origin in origins:
            path = Path(origin).resolve()
            if not any(path.is_relative_to(root) for root in allowed):
                raise VerificationError("phase3_primitives_bundled_import_required")


def _workspace_bytes(base: Path) -> int:
    """Count bounded job-private files without following cache aliases."""
    pending = [base]
    total = entries = 0
    while pending:
        directory = pending.pop()
        try:
            visible = list(directory.iterdir())
        except FileNotFoundError:
            continue
        for path in visible:
            entries += 1
            if entries > MAX_WORKSPACE_ENTRIES:
                raise VerificationError("phase3_primitives_workspace_limit")
            try:
                details = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(details.st_mode):
                pending.append(path)
            elif stat.S_ISREG(details.st_mode):
                total += details.st_size
            elif stat.S_ISLNK(details.st_mode) and path.resolve().is_relative_to(base):
                # uv's private cache can index an already counted artifact by
                # an internal alias. Installed package closure is checked later.
                continue
            else:
                raise VerificationError("phase3_primitives_unsafe_workspace")
            if total > DISK_BYTES:
                raise VerificationError("phase3_primitives_disk_limit")
    return total


async def _settle_task(task: asyncio.Task) -> None:
    """Wait through caller cancellation so no callback or builder is abandoned."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()


async def verify(host: Path, base: Path) -> dict:
    _host_paths(host)
    _host_imports(host)
    from meecp.imports import ImportStore
    from meecp.preparation_acquisition import WheelAcquirer
    from meecp.preparation_builder import load_private_toolchain, prepare_bound
    from meecp.preparation_contracts import PreparationError, parse_descriptor
    from meecp.preparation_processes import PreparationExecutor
    from meecp.preparation_verifier import verify_prepared_package
    from meecp.preparations import PreparationStore
    from meecp.recipe_sdk import scaffold_recipe
    from meecp.recipes import RecipeStore
    _check_module_origins(host)

    started = time.monotonic()
    deadline = started + WALL_SECONDS
    canceled = threading.Event()
    imports = executor = build_task = None
    command_count = wheel_count = peak_disk_bytes = 0
    step_peaks = []
    cache_was_empty = False
    loop = asyncio.get_running_loop()

    def check():
        if canceled.is_set():
            raise PreparationError("phase3_primitives_canceled")
        if time.monotonic() >= deadline:
            raise PreparationError("phase3_primitives_deadline")

    async def check_disk():
        nonlocal peak_disk_bytes
        check()
        used = await asyncio.to_thread(_workspace_bytes, base)
        peak_disk_bytes = max(peak_disk_bytes, used)
        check()

    async def guarded(coroutine):
        task = asyncio.create_task(coroutine)
        try:
            while not task.done():
                await check_disk()
                await asyncio.wait({task}, timeout=0.05)
            check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await _settle_task(task)

    try:
        source = base / "source"
        source.mkdir(mode=0o700)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(host / name, source / name)
        shutil.copyfile(host / "templates/LICENSE", source / "LICENSE")
        (source / "tool.py").write_text("def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n")
        imports = ImportStore(base / "state")
        draft = await asyncio.to_thread(imports.create, source, name="Controlled preparation fixture",
                                        intent="Expose one synthetic integer transformation")
        recipes = RecipeStore(imports)
        await asyncio.to_thread(scaffold_recipe, base / "recipe", "test.controlled-preparation",
                                component="python-function-v1")
        recipe = await asyncio.to_thread(recipes.add, base / "recipe")
        recipe = await asyncio.to_thread(recipes.enable, recipe["recipe_id"], recipe["version"])
        inputs = {field["field"]: field["default"] for field in recipe["manifest"]["form"] if "default" in field}
        tool = {"name": "double", "description": "Double a synthetic integer",
                "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}},
                                 "required": ["n"], "additionalProperties": False},
                "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}},
                                  "required": ["value"], "additionalProperties": False}}
        descriptor = parse_descriptor({"schema_version": 1,
            "recipe": {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
            "package": {"id": "test.controlled-double", "version": "1.0.0", "name": "Controlled Double"},
            "interface": {"kind": "python-callable", "module": "tool", "function": "double", "tools": [tool]},
            "dependencies": {"project": ".", "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(),
                             "python_version": "3.12.13", "mcp_version": "1.30.0"},
            "source_license": "LICENSE", "inputs": inputs, "resources": {"memory_mb": 128},
            "validation": [{"id": "synthetic", "tool": "double", "arguments": {"n": 3},
                            "expected": {"value": 6}, "synthetic": True}]})
        store = PreparationStore(imports, recipes)
        plan = await asyncio.to_thread(store.plan, draft["draft_id"], draft["revision"],
                                       descriptor.model_dump(exclude_none=True))
        if plan["execution_available"] is not False or plan["descriptor_digest"] != descriptor.digest:
            raise PreparationError("phase3_primitives_public_boundary_changed")
        toolchain = await asyncio.to_thread(load_private_toolchain, host, check)
        reaper = host / "bin/meecp-reap"
        executor = PreparationExecutor(reaper)
        acquisition = WheelAcquirer()
        candidate = base / "candidate"
        builder = base / "candidate-builder"
        allowed_executables = {toolchain.uv, candidate / "python/bin/python3.12"}

        async def command(spec):
            nonlocal cache_was_empty
            check()
            if (Path(spec.argv[0]) not in allowed_executables or spec.network_hosts
                    or spec.env.get("PATH") != "/usr/bin:/bin" or spec.env.get("UV_PYTHON_DOWNLOADS") != "never"
                    or spec.cwd != builder):
                raise PreparationError("phase3_primitives_command_scope_mismatch")
            for name in ("HOME", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON"):
                path = Path(spec.env.get(name, "/"))
                if not path.is_absolute() or not path.is_relative_to(base):
                    raise PreparationError("phase3_primitives_private_environment_required")
            if spec.operation == "private_uv_version":
                cache_was_empty = not any(Path(spec.env["UV_CACHE_DIR"]).iterdir())
                if not cache_was_empty:
                    raise PreparationError("phase3_primitives_fresh_cache_required")
            if spec.disk_limit_bytes > DISK_BYTES:
                raise PreparationError("phase3_primitives_disk_policy_mismatch")
            result = await guarded(executor.execute(Path(spec.argv[0]), list(spec.argv[1:]), spec.cwd, spec.env,
                timeout_seconds=min(spec.timeout_seconds, deadline - time.monotonic()), stdout_limit=spec.stdout_limit))
            if result.peak_memory_bytes > spec.memory_mb * 1024**2:
                raise PreparationError("phase3_primitives_step_memory_limit")
            step_peaks.append(result.peak_memory_bytes)
            return result.stdout.decode("utf-8")

        def controlled_command(spec):
            nonlocal command_count
            check()
            command_count += 1
            return asyncio.run_coroutine_threadsafe(command(spec), loop).result()

        def controlled_acquire(requests, destination, check_canceled):
            nonlocal wheel_count
            check()
            wheel_count = len(requests)
            return asyncio.run_coroutine_threadsafe(guarded(acquisition.acquire(requests, destination,
                allowed_hosts=ALLOWED_WHEEL_HOSTS, disk_bytes=DISK_BYTES,
                wall_seconds=deadline - time.monotonic(), check_canceled=check_canceled)), loop).result()

        await check_disk()
        build_task = asyncio.create_task(asyncio.to_thread(prepare_bound, descriptor, draft, toolchain, candidate,
            controlled_command, check, acquire_wheels=controlled_acquire))
        package = await asyncio.shield(build_task)
        await check_disk()
        relocated = base / "test Applications/Relocated Package"
        relocated.parent.mkdir(mode=0o700)
        package.root.rename(relocated)
        imports.close()
        imports = None
        for path in (source, base / "state", builder):
            shutil.rmtree(path)
        if any(path.exists() for path in (source, base / "state", builder, candidate)):
            raise PreparationError("phase3_primitives_input_cleanup_failed")
        workspace = base / "probe"
        workspace.mkdir(mode=0o700)
        report = await guarded(verify_prepared_package(relocated, descriptor, reaper, workspace,
                               timeout_seconds=min(60, deadline - time.monotonic())))
        await check_disk()
        await executor.close()
        if executor.owner.instances:
            raise PreparationError("phase3_primitives_owned_processes_remaining")
        _check_module_origins(host)
        return {"verified": True, "scope": "development-preparation-primitives", "bundled_implementation": True,
                "public_execution_available": plan["execution_available"], "source_recipe_plan_validated": True,
                "commands": command_count, "wheels": wheel_count, "fresh_private_cache": cache_was_empty,
                "private_runtime": True, "offline_wheel_installation": True, "package_relocated": True,
                "source_state_builder_removed": True, "owned_steps_remaining": len(executor.owner.instances),
                "step_peak_max_bytes": max(step_peaks), "peak_workspace_bytes": peak_disk_bytes,
                "wall_limit_seconds": WALL_SECONDS, "disk_limit_bytes": DISK_BYTES,
                "elapsed_seconds": round(time.monotonic() - started, 3), **asdict(report)}
    finally:
        canceled.set()
        if build_task is not None:
            await _settle_task(build_task)
        if executor is not None:
            await executor.close()
        if imports is not None:
            imports.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="Absolute built CLI-host directory.")
    parser.add_argument("--keep-artifacts", action="store_true", help="Retain the private fixture directory and package.")
    arguments = parser.parse_args()
    os.umask(0o077)
    base = None
    result = None
    failure = None
    try:
        _host_paths(arguments.host)
        base = Path(tempfile.mkdtemp(prefix="meecp-phase3-primitives-", dir="/private/tmp")).resolve()
        result = asyncio.run(verify(arguments.host, base))
        if arguments.keep_artifacts:
            result["artifact_directory"] = str(base)
            result["package_path"] = str(base / "test Applications/Relocated Package")
    except KeyboardInterrupt:
        failure = "phase3_primitives_interrupted"
    except Exception as error:
        code = getattr(error, "code", "phase3_primitives_verification_failed")
        failure = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,127}", code) else "phase3_primitives_verification_failed"
    finally:
        if base is not None and not arguments.keep_artifacts:
            try:
                shutil.rmtree(base)
                if base.exists():
                    raise OSError()
                if result is not None:
                    result["temporary_artifacts_removed"] = True
            except OSError:
                failure = "phase3_primitives_cleanup_failed"
    if failure is not None:
        output = {"verified": False, "error": failure}
        if base is not None and arguments.keep_artifacts:
            output["artifact_directory"] = str(base)
        print(json.dumps(output, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
