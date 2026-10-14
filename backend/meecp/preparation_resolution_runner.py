"""Owned private dependency resolution and explicit foreground MCP discovery."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import tomllib

from mcp import ClientSession
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from .contracts import Tool
from .ownership_receipts import verify_receipt
from .preparation_acquisition import WheelAcquirer
from .preparation_admission import PublicPreparationAdmission
from .preparation_builder import MCP_VERSION, PYTHON_VERSION, _runtime, load_private_toolchain
from .preparation_components import ComponentReference, read_snapshot
from .preparation_contracts import PreparationError, canonical_bytes, check_schema, contract_digest
from .preparation_processes import PreparationExecutor, _settle
from .preparation_resolution import ResolutionPolicyV1, ResolutionRequestV1, TERMINAL, result_from_files
from .preparation_resolution_network import ResolutionIndexBroker, sandbox_profile
from .preparation_runner import (PreparationRunner, JournaledPreparationOwner, _joined,
    dependency_identity, implementation_digest)
from .preparation_verifier import _owned_transport
from .processes import OwnershipLost, _await_ownership, _watch_ownership

FILES = ("preparation_resolution.py", "preparation_resolution_runner.py", "preparation_resolution_network.py",
         "preparation_builder.py", "preparation_acquisition.py", "preparation_inspection.py", "preparation_components.py",
         "preparation_processes.py", "preparation_verifier.py", "preparation_probe_confinement.py", "preparation_runner.py", "processes.py",
         "ownership_receipts.py", "boot_session.py", "downloads.py", "preparation_contracts.py", "preparation_inputs.py",
         "contracts.py", "imports.py", "preparation_application_source.py", "recipes.py", "private.py", "preparation_admission.py",
         "preparation_release_policy.py", "preparation_release_artifact.py", "preparation_agent_public_resolution.py",
         "management_workflow_grants.py", "grants.py",
         "_native_tls/__init__.py", "_native_tls/_api.py", "_native_tls/_macos.py",
         "_native_tls/_openssl.py", "_native_tls/_ssl_constants.py", "_native_tls/_windows.py")
SANDBOX = Path("/usr/bin/sandbox-exec")
_QUIET_DISCOVERY = contextvars.ContextVar("meecp_quiet_resolution_discovery", default=False)


class _DiscoveryLogFilter(logging.Filter):
    def filter(self, record):
        # The SDK may log rejected protocol payloads through the root logger.
        # Context-local suppression covers its inherited receive-loop tasks.
        return not _QUIET_DISCOVERY.get()


logging.getLogger().addFilter(_DiscoveryLogFilter())


def dependency_project(draft, candidate):
    """Derive a separate static project; source metadata never selects hooks."""
    snapshot = read_snapshot(draft)
    prefix = "" if candidate["root"] == "." else candidate["root"] + "/"
    source = snapshot.by_path().get(prefix + "pyproject.toml")
    try:
        metadata = tomllib.loads(source.data.decode()) if source else {}
        project = metadata.get("project", {})
        if (not isinstance(project, dict) or project.get("dynamic") or metadata.get("tool", {}).get("uv")
                or metadata.get("dependency-groups")):
            raise PreparationError("unsupported_resolution_project")
        requires_python = project.get("requires-python", ">=3.12")
        if not isinstance(requires_python, str) or PYTHON_VERSION not in SpecifierSet(requires_python):
            raise PreparationError("unsupported_resolution_python")
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or len(dependencies) > 128:
            raise PreparationError("resolution_dependency_limit")
        for raw in dependencies:
            if not isinstance(raw, str) or len(raw) > 4096:
                raise PreparationError("unsupported_resolution_dependency")
            requirement = Requirement(raw)
            if requirement.url:
                raise PreparationError("unsupported_resolution_dependency")
            if canonicalize_name(requirement.name) == "mcp" and MCP_VERSION not in requirement.specifier:
                raise PreparationError("unsupported_resolution_mcp")
        # The adapter/discovery SDK is an explicit additional preparation input.
        # Original requirements retain their exact strings and markers.
        dependencies = [*dependencies, "mcp==" + MCP_VERSION]
        data = ('[project]\nname = "meecp-derived-preparation"\nversion = "1.0.0"\n'
                'requires-python = "==' + PYTHON_VERSION + '"\ndependencies = ' + json.dumps(dependencies) + '\n').encode()
        return data
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise PreparationError("unsupported_resolution_project") from None


async def discover_interface(owner, python, runtime, source, workspace, candidate, *, timeout, memory_mb, check):
    """Bound real SDK initialization/listing, with no tool invocations or hooks."""
    module = candidate.get("declared_module")
    entrypoint = candidate.get("declared_entrypoint", "")
    pieces = entrypoint.split(":")
    if (not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module)
            or pieces[0] != module or len(pieces) > 2
            or len(pieces) == 2 and not re.fullmatch(r"[A-Za-z_]\w*", pieces[1])):
        raise PreparationError("unsupported_resolution_entrypoint")
    # Host-written launch code supports exactly module or declared no-arg entry.
    # Paths arrive as argv; source cannot interpolate Python code or shell text.
    code = ("import sys,runpy,importlib; sys.path[:0]=sys.argv[1:3]; "
            "m=sys.argv[3]; f=sys.argv[4]; "
            "getattr(importlib.import_module(m),f)() if f else runpy.run_module(m,run_name='__main__')")
    profile = sandbox_profile(workspace, runtime, python, read_paths=[source, workspace / "home", workspace / "tmp"],
                              write_paths=[workspace / "home", workspace / "tmp"])
    env = {"PATH": "/usr/bin:/bin", "HOME": str(workspace / "home"), "TMPDIR": str(workspace / "tmp"),
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C", "LANG": "C"}
    process = None
    try:
        async with asyncio.timeout(timeout):
            check()
            process = await owner.launch(SANDBOX, ["-p", profile, str(python), "-I", "-S", "-B", "-c", code,
                str(runtime.parent / "python/site-packages"), str(source), module, pieces[1] if len(pieces) == 2 else ""],
                source, env, owned_group=True, startup_timeout=min(10, timeout), stop_grace=0.1)
            async def probe():
                token = _QUIET_DISCOVERY.set(True)
                try:
                    return await session()
                finally:
                    _QUIET_DISCOVERY.reset(token)
            async def session():
                async with _owned_transport(process) as (incoming, outgoing):
                    async with ClientSession(incoming, outgoing) as client:
                        initialized = await client.initialize()
                        if (initialized.protocolVersion != "2025-11-25"
                                or set(initialized.capabilities.model_dump(exclude_none=True)) != {"tools"}):
                            raise PreparationError("unsupported_resolution_capabilities")
                        listing = await client.list_tools()
                        if listing.nextCursor is not None or not 1 <= len(listing.tools) <= 128:
                            raise PreparationError("resolution_tool_limit")
                        tools = []
                        for item in listing.tools:
                            raw = item.model_dump(by_alias=True, exclude_none=True)
                            # Preserve every supported tool field; unknown metadata
                            # requires a new contract instead of silent truncation.
                            if set(raw) - {"name", "description", "inputSchema", "outputSchema", "annotations"}:
                                raise PreparationError("unsupported_resolution_tool_metadata")
                            tool = Tool.model_validate({"name": raw["name"], "description": raw.get("description", ""),
                                "input_schema": raw["inputSchema"], "output_schema": raw.get("outputSchema"),
                                "annotations": raw.get("annotations")})
                            check_schema(tool.input_schema)
                            if tool.output_schema is not None:
                                check_schema(tool.output_schema)
                            tools.append(tool)
                        if len({tool.name for tool in tools}) != len(tools) or len(canonical_bytes([tool.model_dump() for tool in tools])) > 512 * 1024:
                            raise PreparationError("resolution_tool_limit")
                        check()
                        return tools
            tools = await _watch_ownership(process, probe())
            await _settle(process)
            check()
            peak = process.peak_memory_bytes
            if peak is None or not 0 < peak <= memory_mb * 1024**2:
                raise PreparationError("resolution_memory_limit")
            return tools, peak
    except TimeoutError:
        raise PreparationError("resolution_discovery_timeout") from None
    except (PreparationError, OwnershipLost, asyncio.CancelledError):
        raise
    except Exception as error:
        pending = [error]
        for _ in range(64):
            if not pending:
                break
            value = pending.pop()
            if isinstance(value, PreparationError):
                raise PreparationError(value.code) from None
            if isinstance(value, BaseExceptionGroup):
                pending.extend(value.exceptions)
        raise PreparationError("resolution_discovery_failed") from None
    finally:
        if process is not None:
            await _settle(process)


class OwnedResolutionRunner(PreparationRunner):
    def _validate_start_admission(self, plan_id):
        # Resolution has its own authority store and capability gate. It never
        # dispatches a package family; authorize_start validates its exact scope.
        return None

    """Reuse joined ownership plumbing, with separate resolution authority."""
    def __init__(self, store, host_root, reaper, *, reserve_resources=None, release_resources=None, enabled=False,
                 public_admission=None):
        if public_admission is not None and (type(public_admission) is not PublicPreparationAdmission
                or store.public_admission is not public_admission):
            raise PreparationError("invalid_public_preparation_admission")
        super().__init__(store, host_root, reaper, reserve_resources=reserve_resources,
                         release_resources=release_resources, enabled=enabled)
        self.public_resolution_admission = public_admission

    async def plan(self, import_id, revision, candidate_id, policy, *, expires_in=300):
        if self._closed or not self.enabled:
            raise PreparationError("resolution_runner_unavailable")
        task = asyncio.create_task(self._resolution_plan(import_id, revision, candidate_id, policy, expires_in))
        self._planning.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            try:
                await _joined(task)
            finally:
                self._planning.discard(task)

    async def start(self, plan_id, plan_digest, idempotency_key, *, grant_id=None):
        if self._closed or not self.enabled:
            raise PreparationError("resolution_runner_unavailable")
        return await super().start(plan_id, plan_digest, idempotency_key, grant_id=grant_id)

    async def _resolution_plan(self, import_id, revision, candidate_id, policy, expires_in):
        try:
            policy = ResolutionPolicyV1.model_validate(policy)
        except ValueError:
            raise PreparationError("invalid_resolution_policy") from None
        draft, candidate = await asyncio.to_thread(self.store.candidate, import_id, revision, candidate_id)
        if self.public_resolution_admission is not None:
            self.public_resolution_admission.require_candidate(candidate)
        expected = {"resolve_dependencies"} | ({"discover_interface"} if candidate["kind"] == "python-mcp" else set())
        if set(policy.operations) != expected or set(policy.network) != {"pypi.org", "files.pythonhosted.org"}:
            raise PreparationError("resolution_policy_mismatch")
        await asyncio.to_thread(dependency_project, draft, candidate)
        toolchain = await asyncio.to_thread(load_private_toolchain, self.host_root)
        producer = await self._producer()
        request = ResolutionRequestV1(schema_version=1, import_id=import_id, revision=revision,
            source_revision=draft["source"]["revision"], candidate_id=candidate_id, candidate_kind=candidate["kind"],
            candidate_digest=contract_digest("preparation-resolution-candidate-v1", candidate), policy=policy.model_dump(mode="json"),
            runtime={"name": "private-python-toolchain", "digest": toolchain.identity}, producer=producer.model_dump())
        return await asyncio.to_thread(self.store.plan, request, candidate, expires_in=expires_in)

    async def _producer(self):
        task = asyncio.create_task(asyncio.to_thread(self._producer_sync))
        return await _await_ownership(task)

    def _producer_sync(self):
        if not SANDBOX.is_file():
            raise PreparationError("resolution_sandbox_unavailable")
        dependencies = dependency_identity(self.host_root)
        implementation = implementation_digest("preparation-resolution-implementation-v1", FILES,
            reaper=self.reaper, dependencies=dependencies)
        digest = contract_digest("preparation-resolution-producer-v1", {"implementation": implementation,
            "sandbox": hashlib.sha256(SANDBOX.read_bytes()).hexdigest()})
        if self.public_resolution_admission is not None:
            digest = contract_digest("public-preparation-resolution-producer-v1", {
                "implementation": digest, "admission": self.public_resolution_admission.digest})
            return ComponentReference(id="python-resolution-v1", version="1.0.1", digest=digest)
        return ComponentReference(id="python-resolution-v1", version="1.0.0", digest=digest)

    async def _release(self, job_id):
        if job_id not in self._released:
            await self._callback(self.release_resources, "resolution:" + job_id, ownership_settled=True)
            self._released.add(job_id)

    async def _close(self):
        await super()._close()
        if any(job["reservation_retained"] and not job["ownership_settled"] for job in self.store.list()):
            raise OwnershipLost()

    def _workspace(self, job_id):
        root = self.store.imports.root / "resolutions"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.resolve() != root or root.stat().st_mode & 0o077:
            raise PreparationError("unsafe_resolution_workspace")
        return root / job_id

    async def _remove(self, job):
        if job["workspace"] is None:
            return
        path = Path(job["workspace"])
        if path != self._workspace(job["job_id"]) or path.resolve() != path:
            raise PreparationError("unsafe_resolution_workspace")
        if path.exists():
            await asyncio.to_thread(shutil.rmtree, path)

    async def _prune(self, job):
        path = Path(job["workspace"])
        if path != self._workspace(job["job_id"]) or path.resolve() != path:
            raise PreparationError("unsafe_resolution_workspace")
        def prune():
            for child in path.iterdir():
                if child.name in {"derived", "ownership-receipts"}:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        task = asyncio.create_task(asyncio.to_thread(prune))
        await _await_ownership(task)

    async def _recover(self):
        for job in self.store.list():
            if job["job_id"] in self.tasks and not self.tasks[job["job_id"]].done():
                continue
            for metadata in job["ownership"]:
                if job["workspace"] and verify_receipt(Path(job["workspace"]), metadata):
                    self.store.confirm_ownership(job["job_id"], metadata["instance_id"])
            if job["state"] != "succeeded":
                job = self.store.finish(job["job_id"], job["error_code"] or "resolution_interrupted", interrupted=True)
            if job["ownership_settled"]:
                if job["state"] != "succeeded":
                    await self._remove(job)
                else:
                    await self._prune(job)
                await self._release(job["job_id"])
            else:
                policy = ResolutionPolicyV1.model_validate(job["request"]["policy"])
                await self._callback(self.reserve_resources, "resolution:" + job["job_id"], policy)
        return self.store.list_summaries()

    async def _execute(self, attempt, executor, toolchain, argv, *, broker=None, env_extra=None, stdout_limit=4096):
        attempt.check()
        if self.public_resolution_admission is not None:
            job = self.store.get(attempt.job_id)
            await self._thread(attempt, self.store.require_public_request,
                ResolutionRequestV1.model_validate(job["request"]), job["candidate"])
            attempt.check()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(attempt.workspace / "home"), "TMPDIR": str(attempt.workspace / "tmp"),
            "UV_CACHE_DIR": str(attempt.workspace / "cache"), "UV_PYTHON": str(toolchain.python_runtime / "bin/python3.12"),
            "UV_PYTHON_DOWNLOADS": "never", "UV_NO_ENV_FILE": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1", "LC_ALL": "C", "LANG": "C", **(env_extra or {})}
        profile = sandbox_profile(attempt.workspace, toolchain.python_runtime, Path(argv[0]),
                                  broker_port=broker.port if broker else None, fork=True, interpreter=Path(env["UV_PYTHON"]))
        result = await self._controlled(attempt, executor.execute(SANDBOX, ["-p", profile, *map(str, argv)],
            attempt.workspace, env, timeout_seconds=min(600, attempt.deadline - time.monotonic()), stdout_limit=stdout_limit))
        executor.owner.confirm_completed()
        attempt.check()
        if result.peak_memory_bytes > attempt.policy.memory_mb * 1024**2:
            raise PreparationError("resolution_memory_limit")
        attempt.peak = max(attempt.peak, result.peak_memory_bytes)
        attempt.commands_finished += 1
        self.store.progress(attempt.job_id, commands_finished=attempt.commands_finished)
        return result.stdout.decode("utf-8")

    async def _resolve(self, attempt, toolchain, draft, candidate, executor):
        directory = attempt.workspace / "derived"
        directory.mkdir(mode=0o700)
        project = await self._thread(attempt, dependency_project, draft, candidate)
        (directory / "pyproject.toml").write_bytes(project)
        self.store.stage(attempt.job_id, "resolving")
        async with ResolutionIndexBroker(attempt.workspace / "index", byte_limit=attempt.policy.disk_bytes,
                                         check=attempt.check) as broker:
            config = attempt.workspace / "uv.toml"
            config.write_text('[[index]]\nname = "mcpme-resolution"\nurl = ' + json.dumps(broker.base + "/simple") +
                              '\ndefault = true\nauthenticate = "always"\n')
            try:
                await self._execute(attempt, executor, toolchain, [toolchain.uv, "lock", "--quiet", "--project", directory,
                    "--config-file", config, "--no-sources", "--no-build", "--no-python-downloads", "--keyring-provider", "disabled"],
                    broker=broker, env_extra={"UV_INDEX_MCPME_RESOLUTION_USERNAME": "meecp",
                                             "UV_INDEX_MCPME_RESOLUTION_PASSWORD": broker.password})
            except PreparationError:
                if broker.error_code:
                    raise PreparationError(broker.error_code) from None
                raise
            lock = directory / "uv.lock"
            if lock.stat().st_size > 16 * 1024**2:
                raise PreparationError("dependency_lock_limit")
            lock.write_bytes(broker.canonical_lock(lock.read_bytes()))
        return directory

    async def _install(self, attempt, toolchain, directory, executor):
        self.store.stage(attempt.job_id, "installing")
        destination, work = attempt.workspace / "runtime", attempt.workspace / "runtime-builder"
        destination.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        loop = asyncio.get_running_loop()
        def command(spec):
            try:
                return asyncio.run_coroutine_threadsafe(self._execute(attempt, executor, toolchain, spec.argv,
                    env_extra=spec.env, stdout_limit=spec.stdout_limit), loop).result()
            except PreparationError as error:
                if error.code == "preparation_step_failed":
                    raise PreparationError("resolution_" + spec.operation + "_failed") from None
                raise
        async def acquire(requests, target, check):
            await self._controlled(attempt, WheelAcquirer().acquire(requests, target,
                allowed_hosts=frozenset({"files.pythonhosted.org"}), disk_bytes=attempt.policy.disk_bytes,
                wall_seconds=attempt.deadline - time.monotonic(), check_canceled=check))
            attempt.wheels_acquired += len(requests)
            attempt.bytes_acquired += sum(item.size for item in requests)
            self.store.progress(attempt.job_id, wheels_acquired=attempt.wheels_acquired, bytes_acquired=attempt.bytes_acquired)
        def wheels(requests, target, check):
            return asyncio.run_coroutine_threadsafe(acquire(requests, target, check), loop).result()
        await self._thread(attempt, _runtime, destination, directory, toolchain, command, attempt.check, work, wheels)
        return destination

    async def _work(self, attempt):
        monitor = None
        attempt.peak = 0
        try:
            job = self.store.get(attempt.job_id)
            request = ResolutionRequestV1.model_validate(job["request"])
            attempt.policy = request.policy
            attempt.deadline = time.monotonic() + request.policy.wall_seconds
            monitor = asyncio.create_task(self._monitor(attempt))
            async with self._serial:
                draft = self.store.check_authority(attempt.job_id)
                attempt.reserved = True
                await self._callback(self.reserve_resources, "resolution:" + attempt.job_id, request.policy)
                self.store.check_authority(attempt.job_id)
                attempt.workspace = self._workspace(attempt.job_id)
                # Journal the exact path before its first creation; recovery
                # cleans only after receipts settle, even across initial crash.
                self.store.begin(attempt.job_id, attempt.workspace)
                attempt.workspace.mkdir(mode=0o700)
                for name in ("home", "tmp", "cache"):
                    (attempt.workspace / name).mkdir(mode=0o700)
                candidate = job["candidate"]
                if self.public_resolution_admission is not None:
                    await self._thread(attempt, self.store.require_public_request, request, candidate)
                current_draft, current_candidate = await self._thread(attempt, self.store.candidate,
                    request.import_id, request.revision, request.candidate_id)
                if current_candidate != candidate or contract_digest("preparation-resolution-candidate-v1", candidate) != request.candidate_digest:
                    raise PreparationError("resolution_candidate_mismatch")
                self.store.progress(attempt.job_id, files_checked=len(current_draft["source"]["files"]))
                toolchain = await self._thread(attempt, load_private_toolchain, self.host_root, attempt.check)
                if toolchain.identity != request.runtime.digest or await self._producer() != request.producer:
                    raise PreparationError("resolution_implementation_changed")
                owner = JournaledPreparationOwner(self.reaper, self.store, attempt.job_id, attempt.workspace)
                attempt.owners.append(owner)
                executor = PreparationExecutor(self.reaper, owner=owner)
                directory = await self._resolve(attempt, toolchain, draft, candidate, executor)
                tools, protocol = [], None
                if "discover_interface" in request.policy.operations:
                    runtime = await self._install(attempt, toolchain, directory, executor)
                    snapshot = await self._thread(attempt, read_snapshot, draft)
                    source = attempt.workspace / "source"
                    source.mkdir(mode=0o700)
                    prefix = "" if candidate["root"] == "." else candidate["root"] + "/"
                    for item in snapshot.files:
                        attempt.check()
                        if item.path.startswith(prefix):
                            path = source / item.path[len(prefix):]
                            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            path.write_bytes(item.data)
                    self.store.stage(attempt.job_id, "discovering")
                    await self._thread(attempt, self.store.require_public_request, request, candidate)
                    attempt.check()
                    tools, peak = await self._controlled(attempt, discover_interface(owner,
                        runtime / "python/bin/python3.12", runtime / "python", source, attempt.workspace, candidate,
                        timeout=min(60, attempt.deadline - time.monotonic()), memory_mb=request.policy.memory_mb, check=attempt.check))
                    owner.confirm_completed()
                    attempt.commands_finished += 1
                    self.store.progress(attempt.job_id, tools_discovered=len(tools), commands_finished=attempt.commands_finished)
                    attempt.peak = max(attempt.peak, peak)
                    protocol = "2025-11-25"
                    (directory / "interface.json").write_bytes(canonical_bytes({"protocol_version": protocol,
                        "tools": [tool.model_dump() for tool in tools], "entrypoint": candidate["declared_entrypoint"]}))
                await executor.close()
                if not owner.confirm_completed():
                    raise OwnershipLost()
                attempt.check()
                self.store.stage(attempt.job_id, "publishing")
                result = await self._thread(attempt, result_from_files, request, directory, tools, protocol, attempt.peak)
                for path in directory.iterdir():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
                    path.chmod(0o400)
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                attempt.check()
                await self._thread(attempt, self.store.publish, attempt.job_id, result)
        except asyncio.CancelledError:
            attempt.stop.set()
        except Exception as error:
            attempt.error_code = error.code if isinstance(error, PreparationError) else "resolution_ownership_lost" if isinstance(error, OwnershipLost) else "resolution_failed"
            attempt.stop.set()
        finally:
            if monitor is not None:
                monitor.cancel()
                await _joined(monitor)
            await _await_ownership(self._finish(attempt))

    async def _finish(self, attempt):
        attempt.stop.set()
        for task in tuple(attempt.callbacks):
            task.cancel()
        for task in (*tuple(attempt.callbacks), *tuple(attempt.threads)):
            try:
                await _joined(task)
            except Exception:
                pass
        for owner in attempt.owners:
            try:
                await owner.close()
            except Exception:
                pass
            owner.confirm_completed()
        job = self.store.finish(attempt.job_id, attempt.error_code)
        if job["ownership_settled"]:
            if job["state"] != "succeeded":
                await self._remove(job)
            else:
                await self._prune(job)
            if attempt.reserved:
                await self._release(attempt.job_id)
