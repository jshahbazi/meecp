"""Controlled, durable preparation coordinator for the private checkpoint.

The installed public feature remains disabled. Reviewed authority selects a
closed Python builder and SDK probe; this coordinator grants no slot or install
authority. Native receipts prove subprocess settlement across host restarts.
Foreground peak memory is checked after native completion; OS memory and probe
filesystem/network isolation remain separate activation requirements.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading
import time
import uuid

import mcp

from .contracts import LOCK_NAME, MAX_LOCK_METADATA_BYTES, PackageError, _read_file, validate_package
from .ownership_receipts import acknowledge_nonlaunch, prepare_receipt, verify_receipt
from .preparation_acquisition import WheelAcquirer
from .preparation_admission import PublicPreparationAdmission
from .preparation_builder import CommandSpec, PrivateToolchain, load_private_toolchain, prepare_bound
from .preparation_components import read_snapshot
from .preparation_contracts import ExecutionPolicy, PreparationError, PreparationReceipt, canonical_bytes, contract_digest
from .preparation_inputs import PreparationReceiptV2, parse_preparation_descriptor
from .preparation_processes import PreparationExecutor
from .preparation_verifier import verify_prepared_package
from .preparations import RECEIPT_NAME, TERMINAL
from .processes import OwnershipLost, ProcessOwner, _await_ownership

WHEEL_HOSTS = frozenset({"files.pythonhosted.org"})
MAX_WORKSPACE_ENTRIES = 200000
RUNNER_FILES = ("preparation_runner.py", "preparation_builder.py", "preparation_acquisition.py",
                "preparation_components.py", "preparation_processes.py", "preparations.py",
                "preparation_authority.py", "preparation_contracts.py", "processes.py", "ownership_receipts.py", "boot_session.py",
                "imports.py", "preparation_application_source.py", "private.py", "recipes.py", "contracts.py", "preparation_inputs.py",
                "preparation_resolved_python.py", "preparation_resolution.py", "preparation_admission.py", "preparation_release_policy.py", "preparation_release_artifact.py",
                "management_workflow_grants.py", "grants.py",
                "preparation_service_contracts.py", "preparation_service_receipts.py")
VERIFIER_FILES = ("preparation_verifier.py", "preparation_probe_confinement.py", "process_graph_accounting_v1.py",
                  "preparation_processes.py", "preparation_contracts.py",
                  "contracts.py", "processes.py", "ownership_receipts.py", "boot_session.py")


def implementation_digest(domain: str, filenames: tuple[str, ...], *, reaper: Path | None = None,
                          dependencies: str | None = None) -> str:
    """Bind authority to actual host implementation bytes, never a version label."""
    root = Path(__file__).resolve().parent
    records = []
    try:
        for name in filenames:
            path = root / name
            if path.resolve() != path or not path.is_file():
                raise PreparationError("preparation_implementation_unavailable")
            data = _read_file(path, 1024 * 1024)
            records.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        if reaper is not None:
            if not reaper.is_absolute() or reaper.resolve() != reaper or not os.access(reaper, os.X_OK):
                raise PreparationError("preparation_implementation_unavailable")
            data = _read_file(reaper, 8 * 1024 * 1024)
            records.append({"path": "native/meecp-reap", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        if dependencies is not None:
            records.append({"path": "host-dependency-closure", "sha256": dependencies})
    except (OSError, PackageError):
        raise PreparationError("preparation_implementation_unavailable") from None
    return contract_digest(domain, records)


def dependency_identity(host_root: Path, check_canceled=lambda: None) -> str:
    """Hash the actual loaded SDK's complete dependency tree, including bytecode.

    Installed host code requires its own bundled site-packages origin. Explicit
    development verification may use the parent's actual dependency tree. Both
    bind real regular-file bytes, native extensions and distribution metadata.
    """
    try:
        filename = getattr(mcp, "__file__", None)
        if not filename:
            raise PreparationError("preparation_dependency_origin_mismatch")
        root = Path(filename).resolve().parent.parent
        installed = Path(__file__).resolve().is_relative_to(host_root / "backend")
        if (root.resolve() != root or not root.is_dir() or
                installed and root != host_root / "python/site-packages"):
            raise PreparationError("preparation_dependency_origin_mismatch")
        top = {path.name.removesuffix(".py") for path in root.iterdir()}
        for name, module in tuple(sys.modules.items()):
            if name.partition(".")[0] not in top:
                continue
            source = getattr(module, "__file__", None)
            origins = [source] if source else list(getattr(module, "__path__", ()))
            if not origins or len(origins) > 64 or any(not Path(origin).resolve().is_relative_to(root) for origin in origins):
                raise PreparationError("preparation_dependency_origin_mismatch")
        pending, files, count, total = [root], [], 0, 0
        while pending:
            check_canceled()
            directory = pending.pop()
            for path in directory.iterdir():
                count += 1
                if count > 65536:
                    raise PreparationError("preparation_dependency_identity_limit")
                details = path.lstat()
                if stat.S_ISDIR(details.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(details.st_mode):
                    total += details.st_size
                    if details.st_nlink != 1 or total > 512 * 1024 * 1024 or details.st_size > 32 * 1024 * 1024:
                        raise PreparationError("preparation_dependency_identity_limit")
                    files.append(path)
                else:
                    raise PreparationError("preparation_dependency_origin_mismatch")
        digest = hashlib.sha256(b"mcpme-actual-host-dependency-closure-v1\0")
        for path in sorted(files):
            check_canceled()
            if path.resolve() != path:
                raise PreparationError("preparation_dependency_origin_mismatch")
            data = _read_file(path, 32 * 1024 * 1024)
            digest.update(canonical_bytes({"path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}))
            digest.update(b"\n")
        return digest.hexdigest()
    except (OSError, PackageError):
        raise PreparationError("preparation_dependency_identity_unavailable") from None


def _policy(value, descriptor) -> ExecutionPolicy:
    if value is None:
        value = ExecutionPolicy(schema_version=1, operations=["build", "probe"], network=sorted(WHEEL_HOSTS),
                                wall_seconds=600, disk_bytes=4 * 1024**3,
                                memory_mb=max(2048, descriptor.resources.memory_mb))
    elif isinstance(value, dict):
        try:
            from .preparation_service_contracts import parse_policy
            value = parse_policy(value)
        except ValueError:
            raise PreparationError("invalid_preparation_policy") from None
    if (not isinstance(value, ExecutionPolicy) or set(value.operations) != {"build", "probe"}
            or len(set(value.network)) != len(value.network) or not set(value.network) <= WHEEL_HOSTS
            or value.disk_bytes > 8 * 1024**3 or value.memory_mb < descriptor.resources.memory_mb):
        raise PreparationError("preparation_policy_mismatch")
    return value


def _workspace_size(root: Path, disk_bytes: int, canceled: threading.Event) -> int:
    """Bound a live private tree without following cache aliases or special files."""
    pending, total, count = [root], 0, 0
    while pending:
        if canceled.is_set():
            raise PreparationError("preparation_canceled")
        directory = pending.pop()
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            continue
        for path in entries:
            count += 1
            if count > MAX_WORKSPACE_ENTRIES:
                raise PreparationError("preparation_workspace_limit")
            try:
                mode = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode.st_mode):
                pending.append(path)
            elif stat.S_ISREG(mode.st_mode):
                total += mode.st_size
            elif stat.S_ISLNK(mode.st_mode) and path.resolve().is_relative_to(root):
                # uv may index copies using aliases inside its private cache.
                continue
            else:
                raise PreparationError("unsafe_preparation_workspace")
            if total > disk_bytes:
                raise PreparationError("preparation_disk_limit")
    return total


def _private_workspace(state: Path, job_id: str) -> Path:
    root = state / "preparations"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.resolve() != root or not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise PreparationError("unsafe_preparation_workspace")
    workspace = root / job_id
    # A durable attempt never reuses an existing workspace or its cache.
    workspace.mkdir(mode=0o700)
    return workspace


def _remove_workspace(state: Path, job_id: str, workspace: Path):
    expected = state / "preparations" / job_id
    if workspace != expected:
        raise PreparationError("unsafe_preparation_workspace")
    try:
        if not workspace.exists() and not workspace.is_symlink():
            return
        details = workspace.lstat()
        if (workspace.resolve() != workspace or not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700):
            raise PreparationError("unsafe_preparation_workspace")
        shutil.rmtree(workspace)
    except OSError:
        raise PreparationError("preparation_workspace_cleanup_failed") from None


def seal_receipt(package, job: dict):
    """Lock the authority receipt before the independent probe sees the package."""
    if (job.get("bound_inputs") or {}).get("schema_version") == 2:
        from .preparation_service_receipts import ServicePreparationReceiptV1
        receipt = ServicePreparationReceiptV1(schema_version=3, binding=job["binding"], inputs=job["bound_inputs"],
            fixture_digest=job["fixture_digest"], service_probe_grant_digest=job["service_probe_grant_digest"])
    elif job.get("bound_inputs") is not None:
        receipt = PreparationReceiptV2(schema_version=2, binding=job["binding"], inputs=job["bound_inputs"],
            fixture_digest=job["fixture_digest"])
    else:
        receipt = PreparationReceipt(schema_version=1, binding=job["binding"],
            dependency_lock_digest=job["dependency_lock_digest"], runtime_digest=job["runtime_digest"],
            fixture_digest=job["fixture_digest"])
    data = canonical_bytes(receipt.model_dump(mode="json"))
    path = package.root / RECEIPT_NAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    lock = package.lock.model_dump()
    digest = hashlib.sha256(data).hexdigest()
    lock["artifacts"].append({"path": RECEIPT_NAME, "sha256": digest, "size": len(data), "executable": False,
        "source": "prepared:sha256:" + digest, "provenance": "Host reviewed preparation authority",
        "license": package.manifest.licenses[0]})
    lock_path = package.root / package.lock_name
    lock_data = json.dumps(lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(lock_data) > MAX_LOCK_METADATA_BYTES:
        raise PreparationError("preparation_package_lock_limit")
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "wb") as output:
        output.write(lock_data)
        output.flush()
        os.fsync(output.fileno())
    return validate_package(package.root)


class JournaledPreparationOwner(ProcessOwner):
    """Journal pending native completion proof before any child can be launched."""
    def __init__(self, reaper: Path, store, job_id: str, workspace: Path, *, check=None, graph_accounting=None):
        self.store, self.job_id, self.workspace = store, job_id, workspace
        self._check = check
        super().__init__(reaper, graph_accounting=graph_accounting,
                         graph_admission_check=self.check_current if graph_accounting is not None else None)

    def check_current(self) -> bool:
        """Trusted metadata fence, also called inside the native creation task."""
        if self._closed:
            raise PreparationError("preparation_canceled")
        if self._check is not None:
            self._check()
        self.store.check_authority(self.job_id)
        return True

    async def launch(self, executable, args, cwd, env, *, admit=None, **kwargs):
        if not kwargs.get("owned_group") or any(key in kwargs for key in
                ("completion_fd", "completion_nonce", "instance_id", "graph_role")):
            raise PreparationError("invalid_preparation_ownership")
        def current():
            self.check_current()
            # A trusted probe may add a metadata guard, but it cannot replace
            # the exact job/attempt admission required by this journal owner.
            self._check_admit(admit)
            self.check_current()
            return True
        identifier = uuid.uuid4().hex
        descriptor, metadata = prepare_receipt(self.workspace, identifier)
        try:
            self.store.register_ownership(self.job_id, metadata)
            return await super().launch(executable, args, cwd, env, instance_id=identifier,
                completion_fd=descriptor, completion_nonce=metadata["nonce"],
                nonlaunch_callback=lambda: acknowledge_nonlaunch(descriptor, identifier, metadata["nonce"]),
                admit=current,
                **({"graph_role": "probe"} if self.graph_accounting is not None else {}), **kwargs)
        finally:
            os.close(descriptor)

    def confirm_completed(self) -> bool:
        for metadata in self.store.ownership_receipts(self.job_id):
            if verify_receipt(self.workspace, metadata):
                self.store.confirm_ownership(self.job_id, metadata["instance_id"])
        return not self.store.ownership_receipts(self.job_id)


@dataclass
class _Attempt:
    job_id: str
    command_attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    stop: threading.Event = field(default_factory=threading.Event)
    threads: set = field(default_factory=set)
    callbacks: set = field(default_factory=set)
    owners: list = field(default_factory=list)
    workspace: Path | None = None
    deadline: float = 0
    policy: ExecutionPolicy | None = None
    error_code: str | None = None
    reserved: bool = False
    stage: dict | None = None
    worker: asyncio.Task | None = None
    commands_finished: int = 0
    wheels_acquired: int = 0
    bytes_acquired: int = 0

    def check(self):
        if self.stop.is_set():
            raise PreparationError(self.error_code or "preparation_canceled")
        if self.deadline and time.monotonic() >= self.deadline:
            raise PreparationError("preparation_wall_limit")


async def _joined(task: asyncio.Task):
    """Collect a task result while repeated cancellation cannot abandon work."""
    try:
        return await _await_ownership(task)
    except asyncio.CancelledError:
        return None


class PreparationRunner:
    def __init__(self, store, host_root: Path, reaper: Path, *, reserve_resources=None,
                 release_resources=None, enabled: bool = False, enabled_families=(), command_admission=None,
                 probe_graph_accounting: bool = False, command_resource_admission=None, public_admission=None):
        self.store, self.host_root, self.reaper = store, host_root, reaper
        self.reserve_resources, self.release_resources = reserve_resources, release_resources
        self.enabled = enabled is True
        if public_admission is not None and type(public_admission) is not PublicPreparationAdmission:
            raise PreparationError("invalid_public_preparation_admission")
        self.public_admission = public_admission
        if type(probe_graph_accounting) is not bool:
            raise PreparationError("invalid_preparation_probe_accounting")
        self.probe_graph_accounting = probe_graph_accounting
        if command_admission is not None:
            from .host_command import HostCommandAdmission
            if type(command_admission) is not HostCommandAdmission:
                raise PreparationError("command_preparation_not_admitted")
        self.command_admission = command_admission
        if command_resource_admission is not None:
            from .preparation_command_owner import CommandPreparationResourceAdmission
            if command_admission is None or type(command_resource_admission) is not CommandPreparationResourceAdmission:
                raise PreparationError("command_preparation_not_admitted")
        self.command_resource_admission = command_resource_admission
        # Admission belongs to an exact reviewed family version. Enabling the
        # legacy runner cannot enable a newly registered composite family.
        self.enabled_families = frozenset(enabled_families)
        if any(key not in store.families for key in self.enabled_families):
            raise PreparationError("unknown_preparation_component")
        if public_admission is not None and self.enabled_families != public_admission.families:
            raise PreparationError("invalid_public_preparation_admission")
        self.tasks: dict[str, asyncio.Task] = {}
        self.attempts: dict[str, _Attempt] = {}
        self._closed = False
        self._close_task = None
        self._recovery_task = None
        self._serial = asyncio.Semaphore(1)
        self._control = asyncio.Lock()
        self._admission_active = False
        self._planning: set[asyncio.Task] = set()
        self._admissions: set[asyncio.Task] = set()
        self._startup_recovered = False
        self._released: set[str] = set()

    def _require_admission(self, descriptor):
        if self._closed or not self.enabled:
            raise PreparationError("preparation_runner_unavailable")
        if self.public_admission is not None:
            self.public_admission.check_current()
            record = self.store.recipes.inspect(descriptor.recipe.id, descriptor.recipe.version)
            if record["digest"] != descriptor.recipe.digest or not self._recipe_admitted(record):
                raise PreparationError("preparation_recipe_not_admitted")
        if descriptor.schema_version == 1:
            return None
        family = self.store.family_for(descriptor)
        if family.component.reference.key not in self.enabled_families:
            raise PreparationError("preparation_family_unavailable")
        if getattr(family, "preparation_owner_kind", None) == "command-v3":
            from .host_command import HostCommandAdmission
            if (type(self.command_admission) is not HostCommandAdmission
                    or self.command_admission.helper_sha256 != family.helper_sha256
                    or self.public_admission is not None and self.command_resource_admission is None):
                raise PreparationError("command_preparation_not_admitted")
        return family

    def execution_available_for_recipe(self, record):
        if self._closed or not self.enabled:
            return False
        if self.public_admission is not None:
            try:
                self.public_admission.check_current()
            except PreparationError:
                return False
        if self.public_admission is not None and not self._recipe_admitted(record):
            return False
        steps = record["manifest"]["steps"]
        keys = {(step["component"], step["version"], step["digest"]) for step in steps}
        matching = [family for family in self.store.families.values() if keys == {
            component.reference.key for component in getattr(family, "components", (family.component,))}]
        if matching:
            return (len(matching) == 1 and matching[0].component.reference.key in self.enabled_families
                and (getattr(matching[0], "preparation_owner_kind", None) != "command-v3"
                     or self.command_admission is not None and self.command_admission.helper_sha256 == matching[0].helper_sha256
                     and (self.public_admission is None or self.command_resource_admission is not None)))
        from .recipes import LEGACY_COMPONENT_KEYS, builtin_components
        catalog = builtin_components()
        return len(keys) == 1 and all(component + "@" + version in LEGACY_COMPONENT_KEYS
            and catalog[component + "@" + version]["digest"] == digest for component, version, digest in keys)

    def _recipe_admitted(self, record):
        # Registry metadata and transitive exact references only; full bundle
        # hashing remains in the store's off-loop descriptor binding.
        from .recipes import RecipeError
        try:
            closure = self.store.recipes._component_closure(record["manifest"])
        except RecipeError:
            return False
        keys = frozenset((*key.rsplit("@", 1), item["digest"]) for key, item in closure.items())
        return keys in self.public_admission.recipe_component_sets

    def _validate_start_admission(self, plan_id):
        with self.store.imports._lock:
            plan = self.store._execution_plan(plan_id)
            self._require_admission(parse_preparation_descriptor(plan["descriptor"]))

    async def _implementation_identities(self, descriptor, dependencies, *, attempt=None):
        await self._check_release(attempt)
        family = self._require_admission(descriptor)
        async def calculate(domain, files):
            if attempt is not None:
                return await self._thread(attempt, implementation_digest, domain, files,
                                          reaper=self.reaper, dependencies=dependencies)
            return await asyncio.to_thread(implementation_digest, domain, files,
                                           reaper=self.reaper, dependencies=dependencies)
        runner = await calculate("preparation-runner-implementation-v1", RUNNER_FILES)
        verifier = await calculate("preparation-verifier-implementation-v1", VERIFIER_FILES)
        if self.public_admission is not None:
            runner = contract_digest("public-preparation-runner-v1", {
                "implementation": runner, "admission": self.public_admission.digest})
        if family is not None:
            identities = sorted(list(component.reference.key) for component in getattr(family, "components", (family.component,)))
            runner = contract_digest("preparation-family-runner-v1", {"implementation": runner, "components": identities})
            verifier = contract_digest("preparation-family-verifier-v1", {"implementation": verifier,
                "components": identities, "profile": getattr(family, "target_profile", "mcp-stdio"),
                "specialist": callable(getattr(family, "verify", None))})
            if getattr(family, "preparation_owner_kind", None) == "command-v3" and self.command_resource_admission is not None:
                admission = self.command_resource_admission
                verifier = contract_digest("preparation-command-measured-verifier-v1", {"implementation": verifier,
                    "helper_sha256": admission.helper_sha256, "platform_policy": admission.platform_policy,
                    "resource_policy_digest": admission.policy.digest})
        return runner, verifier

    async def _check_release(self, attempt=None):
        if self.public_admission is None:
            return
        if attempt is not None:
            await self._thread(attempt, lambda: self.public_admission.check_current(full=True))
            return
        task = asyncio.create_task(asyncio.to_thread(lambda: self.public_admission.check_current(full=True)))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _joined(task)
            except Exception:
                pass
            raise

    async def _verify(self, package_root, descriptor, workspace, *, timeout_seconds, owner):
        await self._check_release()
        family = self._require_admission(descriptor)
        verifier = getattr(family, "verify", None)
        if verifier is None:
            if family is not None and getattr(family, "target_profile", "mcp-stdio") != "mcp-stdio":
                raise PreparationError("independent_runtime_verifier_unavailable")
            return await verify_prepared_package(package_root, descriptor, self.reaper, workspace,
                                                 timeout_seconds=timeout_seconds, owner=owner,
                                                 confine_private_python=True)
        return await verifier(package_root, descriptor, self.reaper, workspace,
                              timeout_seconds=timeout_seconds, owner=owner)

    @staticmethod
    async def _callback(callback, *args, **kwargs):
        if callback is None:
            return None
        result = callback(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def plan(self, import_id, revision, descriptor, policy=None, *, expires_in=300):
        if self._closed or not self.enabled:
            raise PreparationError("preparation_runner_unavailable")
        task = asyncio.create_task(self._plan(import_id, revision, descriptor, policy, expires_in))
        self._planning.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            try:
                await _joined(task)
            except Exception:
                pass
            finally:
                self._planning.discard(task)

    async def _plan(self, import_id, revision, descriptor, policy, expires_in):
        # The store binds recipe form, source closure and interface before a
        # host-selected policy or implementation identity can be reviewed.
        value, _, _ = await asyncio.to_thread(self.store._bound_descriptor, import_id, revision, descriptor)
        self._require_admission(value)
        toolchain = await asyncio.to_thread(load_private_toolchain, self.host_root)
        family = self.store.family_for(value) if value.schema_version == 2 else None
        if family is not None and hasattr(family, "execution_policy"):
            draft = self.store.imports.inspect(import_id)
            if draft["revision"] != revision:
                raise PreparationError("import_revision_mismatch")
            policy = await asyncio.to_thread(family.execution_policy, value, draft, policy)
        selected = _policy(policy, value)
        from .preparation_service_contracts import ServiceExecutionPolicyV1
        if isinstance(selected, ServiceExecutionPolicyV1) and (family is None or family.source_kind != "service_connection"):
            raise PreparationError("service_probe_authority_mismatch")
        dependencies = await asyncio.to_thread(dependency_identity, self.host_root)
        runner, verifier = await self._implementation_identities(value, dependencies)
        return await asyncio.to_thread(self.store.execution_plan, import_id, revision, descriptor, selected, runner_digest=runner,
            verifier_digest=verifier, runtime_digest=toolchain.identity, expires_in=expires_in)

    async def start(self, plan_id, plan_digest, idempotency_key, *, grant_id=None):
        if self._closed or not self.enabled:
            raise PreparationError("preparation_runner_unavailable")
        task = asyncio.create_task(self._start(plan_id, plan_digest, idempotency_key, grant_id=grant_id))
        self._admissions.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            try:
                await _joined(task)
            except Exception:
                pass
            finally:
                self._admissions.discard(task)

    async def _start(self, plan_id, plan_digest, idempotency_key, *, grant_id=None):
        await self.recover()
        async with self._control:
            self._admission_active = True
            try:
                self._validate_start_admission(plan_id)
                job = await asyncio.to_thread(self.store.authorize_start, plan_id, plan_digest, idempotency_key,
                    **({"grant_id": grant_id} if grant_id is not None else {}))
                identifier = job["job_id"]
                job = self.store.get(identifier)
                if identifier not in self.tasks and job["state"] == "queued":
                    attempt = _Attempt(identifier)
                    self.attempts[identifier] = attempt
                    task = asyncio.create_task(self._work(attempt))
                    attempt.worker = task
                    self.tasks[identifier] = task
                return job
            finally:
                self._admission_active = False

    async def cancel(self, job_id):
        job = self.store.cancel(job_id)
        attempt = self.attempts.get(job_id)
        task = self.tasks.get(job_id)
        if attempt is not None:
            attempt.stop.set()
        if task is not None and not task.done():
            task.cancel()
            await _joined(task)
        return self.store.get(job_id)

    async def recover(self):
        # Status readers may observe the durable queued row while its owned
        # admission is finishing. They must neither interrupt it nor wait on
        # potentially large off-loop input checks to read bounded metadata.
        if self._admission_active:
            return self.store.list_summaries()
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._reconcile())
        return await _await_ownership(self._recovery_task)

    async def _reconcile(self):
        async with self._control:
            return await self._recover()

    async def _recover(self):
        # No PID discovery, adoption or automatic replay. Native proofs alone
        # release persisted unknown ownership after an interrupted host.
        if not self._startup_recovered:
            self.store.recover()
            self._startup_recovered = True
        for job in self.store.list():
            if (not job.get("managed") or job["job_id"] in self.tasks and not self.tasks[job["job_id"]].done()
                    or job["state"] not in TERMINAL):
                continue
            workspace = job.get("workspace")
            for metadata in self.store.ownership_receipts(job["job_id"]):
                if workspace is not None and verify_receipt(Path(workspace), metadata):
                    self.store.confirm_ownership(job["job_id"], metadata["instance_id"])
            from .preparation_command_owner import recover_command_ownership
            for metadata in self.store.command_ownership(job["job_id"]):
                identity = recover_command_ownership(Path(workspace), metadata) if workspace is not None else None
                if identity is not None:
                    self.store.confirm_command_ownership(job["job_id"], metadata["instance_id"], identity)
            if not self.store.pending_ownership(job["job_id"]):
                job = self.store.settle(job["job_id"])
            if job["ownership_settled"] and not job["reservation_retained"]:
                cleanup_available = True
                try:
                    await asyncio.to_thread(self.store.release_family_inputs, job["job_id"])
                except PreparationError as error:
                    if error.code != "unknown_preparation_component":
                        raise
                    # Historical jobs can reference an implementation absent
                    # from this host. Ownership is already settled, so retain
                    # their inputs/workspace without granting that component
                    # current execution authority or blocking host startup.
                    cleanup_available = False
                if cleanup_available and workspace is not None and Path(workspace).exists():
                    await asyncio.to_thread(_remove_workspace, self.store.imports.root, job["job_id"], Path(workspace))
                await self._release(job["job_id"])
            elif job["reservation_retained"]:
                await self._callback(self.reserve_resources, job["job_id"], self.store.recovery_policy(job["job_id"]))
        return self.store.list_summaries()

    async def close(self):
        self._closed = True
        if self._close_task is None or (self._close_task.done() and self._close_task.exception() is not None):
            self._close_task = asyncio.create_task(self._close())
        return await _await_ownership(self._close_task)

    async def _close(self):
        for task in tuple(self._planning):
            await _await_ownership(asyncio.gather(task, return_exceptions=True))
        for task in tuple(self._admissions):
            await _await_ownership(asyncio.gather(task, return_exceptions=True))
        if self._recovery_task is not None:
            try:
                await _joined(self._recovery_task)
            except Exception:
                # Consume the old failure, join current owned work, then retry
                # exact durable reconciliation below. A failed earlier attempt
                # must not permanently poison cleanup or close retries.
                pass
        for identifier, task in self.tasks.items():
            if not task.done():
                self.store.cancel(identifier)
                self.attempts[identifier].stop.set()
                task.cancel()
        for task in self.tasks.values():
            try:
                await _joined(task)
            except Exception:
                # A completed worker can retain an earlier cleanup exception.
                # Join every worker, then judge shutdown by fresh durable proof
                # and cleanup below, not by replaying an obsolete task result.
                pass
        # A previous shutdown may have retained a pending proof that the native
        # owner completes later. Retry shutdown can reconcile that exact proof
        # without reopening public controls or discovering a process identity.
        await self.recover()
        if any(job.get("managed") and job["reservation_retained"] and not job["ownership_settled"]
               for job in self.store.list()):
            raise OwnershipLost()

    async def _release(self, job_id):
        if job_id not in self._released:
            await self._callback(self.release_resources, job_id, ownership_settled=True)
            self._released.add(job_id)

    async def _thread(self, attempt, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        attempt.threads.add(task)
        try:
            return await asyncio.shield(task)
        except BaseException as failure:
            if isinstance(failure, asyncio.CancelledError):
                attempt.stop.set()
                for callback in tuple(attempt.callbacks):
                    callback.cancel()
            try:
                await _joined(task)
            except BaseException:
                pass
            raise
        finally:
            # A running thread cannot be canceled. Stop its controlled callbacks
            # and join it before releasing any workspace or reservation.
            if not task.done() and attempt.stop.is_set():
                for callback in tuple(attempt.callbacks):
                    callback.cancel()
            attempt.threads.discard(task)

    async def _controlled(self, attempt, coroutine):
        task = asyncio.create_task(coroutine)
        attempt.callbacks.add(task)
        try:
            attempt.check()
            return await task
        finally:
            if not task.done():
                task.cancel()
            try:
                await _joined(task)
            finally:
                attempt.callbacks.discard(task)

    async def _monitor(self, attempt):
        try:
            while True:
                attempt.check()
                await self._thread(attempt, self.store.check_authority, attempt.job_id)
                if attempt.workspace is not None:
                    await self._disk_check(attempt)
                await asyncio.sleep(0.05)
        except PreparationError as failure:
            attempt.error_code = failure.code
            attempt.stop.set()
            if attempt.worker is not None:
                attempt.worker.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            code = getattr(failure, "code", None)
            attempt.error_code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "preparation_resource_measurement_failed"
            attempt.stop.set()
            if attempt.worker is not None:
                attempt.worker.cancel()

    async def _disk_check(self, attempt, *, extra=0):
        used = await self._thread(attempt, _workspace_size, attempt.workspace, attempt.policy.disk_bytes, attempt.stop)
        if attempt.stage is not None:
            used += await self._thread(attempt, _workspace_size, Path(attempt.stage["path"]), attempt.policy.disk_bytes, attempt.stop)
        if used + extra > attempt.policy.disk_bytes:
            raise PreparationError("preparation_disk_limit")
        attempt.check()

    async def _command(self, attempt, executor, toolchain, candidate, spec: CommandSpec):
        await self._check_release(attempt)
        attempt.check()
        work = candidate.parent / (candidate.name + "-builder")
        if (not isinstance(spec, CommandSpec) or Path(spec.argv[0]) not in
                {toolchain.uv, candidate / "python/bin/python3.12"} or spec.network_hosts or spec.cwd != work
                or spec.env.get("PATH") != "/usr/bin:/bin" or spec.env.get("UV_PYTHON_DOWNLOADS") != "never"
                or spec.env.get("PYTHONNOUSERSITE") != "1"):
            raise PreparationError("preparation_command_scope_mismatch")
        for name in ("HOME", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON"):
            path = Path(spec.env.get(name, "/"))
            if not path.is_absolute() or not path.is_relative_to(attempt.workspace):
                raise PreparationError("preparation_command_scope_mismatch")
        try:
            self.store.record_progress(attempt.job_id, spec.operation)
            await self._disk_check(attempt)
            result = await self._controlled(attempt, executor.execute(Path(spec.argv[0]), list(spec.argv[1:]),
                spec.cwd, spec.env, timeout_seconds=min(spec.timeout_seconds, attempt.deadline - time.monotonic()),
                stdout_limit=spec.stdout_limit))
            attempt.check()
            await self._disk_check(attempt)
            if result.peak_memory_bytes > min(spec.memory_mb, attempt.policy.memory_mb) * 1024**2:
                raise PreparationError("preparation_step_memory_limit")
            attempt.commands_finished += 1
            self.store.record_progress(attempt.job_id, spec.operation, commands_finished=attempt.commands_finished)
            try:
                return result.stdout.decode("utf-8")
            except UnicodeError:
                raise PreparationError("invalid_preparation_step_output") from None
        finally:
            executor.owner.confirm_completed()

    async def _build(self, attempt, descriptor, draft, toolchain, candidate, executor):
        loop = asyncio.get_running_loop()
        acquisition = WheelAcquirer()
        def command(spec):
            attempt.check()
            return asyncio.run_coroutine_threadsafe(self._command(attempt, executor, toolchain, candidate, spec), loop).result()
        async def acquire(requests, destination, check):
            self.store.record_progress(attempt.job_id, "dependency_acquisition")
            await self._controlled(attempt, acquisition.acquire(requests, destination,
                allowed_hosts=frozenset(attempt.policy.network), disk_bytes=attempt.policy.disk_bytes,
                wall_seconds=attempt.deadline - time.monotonic(), check_canceled=check))
            attempt.wheels_acquired += len(requests)
            attempt.bytes_acquired += sum(request.size for request in requests)
            self.store.record_progress(attempt.job_id, "dependency_acquisition", wheels_acquired=attempt.wheels_acquired,
                                       bytes_acquired=attempt.bytes_acquired)
        def wheels(requests, destination, check):
            attempt.check()
            return asyncio.run_coroutine_threadsafe(acquire(requests, destination, check), loop).result()
        if descriptor.schema_version == 2:
            return await self._thread(attempt, self.store.family_for(descriptor).prepare,
                descriptor, draft, toolchain, candidate, command, attempt.check, acquire_wheels=wheels, job_id=attempt.job_id)
        return await self._thread(attempt, prepare_bound, descriptor, draft, toolchain, candidate,
                                  command, attempt.check, acquire_wheels=wheels)

    async def _work(self, attempt):
        monitor = None
        try:
            descriptor, draft, policy = self.store.job_inputs(attempt.job_id)
            self._require_admission(descriptor)
            attempt.policy = _policy(policy, descriptor)
            attempt.deadline = time.monotonic() + policy.wall_seconds
            monitor = asyncio.create_task(self._monitor(attempt))
            async with self._serial:
                attempt.check()
                self.store.check_authority(attempt.job_id)
                attempt.reserved = True
                await self._callback(self.reserve_resources, attempt.job_id, policy)
                self.store.check_authority(attempt.job_id)
                self.store.transition(attempt.job_id, "snapshotting")
                self.store.record_progress(attempt.job_id, "snapshotting")
                def workspace():
                    attempt.workspace = _private_workspace(self.store.imports.root, attempt.job_id)
                    return attempt.workspace
                await self._thread(attempt, workspace)
                self.store.set_workspace(attempt.job_id, attempt.workspace)
                if descriptor.schema_version == 1 or draft["source"]["kind"] in {"local_folder", "document"}:
                    snapshot = await self._thread(attempt, read_snapshot, draft)
                    self.store.record_progress(attempt.job_id, "snapshotting", files_checked=len(snapshot.files))
                else:
                    await self._thread(attempt, self.store.resolved_inputs, descriptor, draft)
                toolchain = await self._thread(attempt, load_private_toolchain, self.host_root, attempt.check)
                job = self.store.get(attempt.job_id)
                dependencies = await self._thread(attempt, dependency_identity, self.host_root, attempt.check)
                runner, verifier = await self._implementation_identities(descriptor, dependencies, attempt=attempt)
                if (job["binding"]["runner_digest"] != runner or job["binding"]["verifier_digest"] != verifier
                        or job["runtime_digest"] != toolchain.identity):
                    raise PreparationError("preparation_implementation_changed")
                self.store.transition(attempt.job_id, "preparing")
                def current():
                    attempt.check()
                    self._require_admission(descriptor)
                owner = JournaledPreparationOwner(self.reaper, self.store, attempt.job_id, attempt.workspace, check=current)
                attempt.owners.append(owner)
                executor = PreparationExecutor(self.reaper, owner=owner)
                candidate = attempt.workspace / "candidate"
                package = await self._build(attempt, descriptor, draft, toolchain, candidate, executor)
                attempt.check()
                await self._disk_check(attempt)
                self.store.record_progress(attempt.job_id, "sealing")
                package = await self._thread(attempt, seal_receipt, package, job)
                await executor.close()
                owner.confirm_completed()
                self.store.transition(attempt.job_id, "verifying")
                self.store.record_progress(attempt.job_id, "verifying")
                probe_workspace = attempt.workspace / "probe"
                probe_workspace.mkdir(mode=0o700)
                family = self._require_admission(descriptor)
                if getattr(family, "preparation_owner_kind", None) == "command-v3":
                    from .preparation_command_owner import CommandPreparationOwner
                    def create_command_owner():
                        created = CommandPreparationOwner(self.command_admission, self.store, attempt.job_id,
                            attempt.workspace, package, descriptor, attempt_id=attempt.command_attempt_id,
                            check=current, deadline=attempt.deadline, resource_admission=self.command_resource_admission)
                        # Cancellation joins this thread before cleanup reads the
                        # list, so a just-created owner cannot be abandoned.
                        attempt.owners.append(created)
                        return created
                    probe_owner = await self._thread(attempt, create_command_owner)
                else:
                    def create_probe_owner():
                        current()
                        self.store.check_authority(attempt.job_id)
                        graph = None
                        if self.probe_graph_accounting and getattr(family, "verify", None) is None:
                            from .preparation_probe_confinement import SANDBOX, _file
                            from .process_graph_accounting_v1 import FixedProcessGraphV1, GraphMemberV1
                            # The native foreground request is sandbox-exec.
                            # Confinement separately binds its exact exec into
                            # the sealed private interpreter and bootstrap.
                            sandbox_hash, _ = _file(SANDBOX)
                            graph = FixedProcessGraphV1(package.digest,
                                contract_digest("preparation-fixed-probe-authority-v1", {
                                    "job_id": attempt.job_id, "binding": self.store.get(attempt.job_id)["binding"]}),
                                (GraphMemberV1("probe", SANDBOX, sandbox_hash, descriptor.resources.memory_mb * 1024**2),))
                        created = JournaledPreparationOwner(self.reaper, self.store, attempt.job_id,
                            attempt.workspace, check=current, graph_accounting=graph)
                        attempt.owners.append(created)
                        return created
                    probe_owner = await self._thread(attempt, create_probe_owner)
                report = await self._controlled(attempt, self._verify(package.root, descriptor,
                    probe_workspace, timeout_seconds=attempt.deadline - time.monotonic(), owner=probe_owner))
                probe_owner.confirm_completed()
                if report.package_digest != package.digest:
                    raise PreparationError("preparation_result_mismatch")
                self.store.record_progress(attempt.job_id, "verifying", fixtures_checked=report.fixtures_checked)
                attempt.check()
                self._require_admission(descriptor)
                self.store.settle(attempt.job_id)
                self.store.begin_verification_publication(attempt.job_id)
                await self._check_release(attempt)
                await self._thread(attempt, self.store.record_verified, attempt.job_id, package, verification=report)
                stage_bytes = await self._thread(attempt, _workspace_size, package.root, policy.disk_bytes, attempt.stop)
                await self._disk_check(attempt, extra=stage_bytes)
                self.store.begin_attachment(attempt.job_id)
                self.store.record_progress(attempt.job_id, "attaching")
                def stage():
                    attempt.stage = self.store.imports.stage_attachment(job["binding"]["import_id"],
                        job["binding"]["revision"], package.root, expected_digest=package.digest)
                    return attempt.stage
                await self._thread(attempt, stage)
                attempt.check()
                self._require_admission(descriptor)
                await self._disk_check(attempt)
                # This transaction shares the atomic winner with cancellation,
                # recipe revocation and import revision changes.
                await self._check_release(attempt)
                await self._thread(attempt, self.store.admit, attempt.job_id, attempt.stage)
                attempt.stage = None
        except asyncio.CancelledError:
            attempt.stop.set()
        except OwnershipLost:
            attempt.error_code = "preparation_ownership_lost"
            attempt.stop.set()
        except (PreparationError, PackageError) as failure:
            attempt.error_code = failure.code
            attempt.stop.set()
        except Exception as failure:
            code = getattr(failure, "code", None)
            attempt.error_code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "preparation_failed"
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
        for task in tuple(attempt.callbacks):
            try:
                await _joined(task)
            except Exception:
                pass
        for task in tuple(attempt.threads):
            try:
                await _joined(task)
            except Exception:
                pass
        settled = True
        for owner in attempt.owners:
            try:
                await owner.close()
            except Exception:
                settled = False
            if not owner.confirm_completed():
                settled = False
        if self.store.pending_ownership(attempt.job_id):
            settled = False
        if attempt.stage is not None:
            await asyncio.to_thread(self.store.imports._discard_attachment_stage, Path(attempt.stage["path"]))
            attempt.stage = None
        job = self.store.get(attempt.job_id)
        if job["state"] not in TERMINAL:
            self.store.record_progress(attempt.job_id, "settling")
        if settled:
            await asyncio.to_thread(self.store.release_family_inputs, attempt.job_id)
            self.store.settle(attempt.job_id)
            job = self.store.get(attempt.job_id)
            if job["state"] not in TERMINAL:
                if attempt.error_code:
                    self.store.transition(attempt.job_id, "failed", ownership_settled=True, error_code=attempt.error_code)
                else:
                    self.store.cancel(attempt.job_id)
        elif job["state"] not in TERMINAL:
            self.store.transition(attempt.job_id, "interrupted", ownership_settled=False,
                                  error_code=attempt.error_code or "preparation_ownership_lost")
        job = self.store.get(attempt.job_id)
        if settled and attempt.workspace is not None:
            await asyncio.to_thread(_remove_workspace, self.store.imports.root, attempt.job_id, attempt.workspace)
        if attempt.reserved and job["ownership_settled"] and not job["reservation_retained"]:
            await self._release(attempt.job_id)
