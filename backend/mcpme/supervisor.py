"""Owner-only management service and isolated deterministic slot supervision."""
from __future__ import annotations

import array
import asyncio
import contextlib
import ctypes
from dataclasses import asdict, dataclass
import fcntl
import json
import hmac
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import sys
import uuid

import httpx
import uvicorn
from mcpme_local_transport import bind_private_listener, checked_socket, socket_path
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .contracts import PackageError, validate_package
from .downloads import DownloadError, DownloadManager
from .connections import connection_names
from .limits import DEFAULT_REQUEST_BYTES, MAX_REQUEST_BYTES
from .activity import ACTIVITY_EVENTS, ACTIVITY_FRAME, Activity, timestamp
from .imports import ImportError as ImportDraftError, ImportStore
from .private import PrivateStateError, check_private_file, ensure_private_directory, read_private_json, snapshot_package, write_private_json
from .ownership_receipts import prepare_receipt, verify_receipt
from .processes import OwnedProcess, ProcessOwner
from .registry import Registry, RegistryError
from .upgrade_host import UpgradeHost
from .upgrades import UpgradeError, UpgradeStore
from .grants import GrantError, GrantStore, plan_digest
from .preparation_contracts import PreparationError
from .preparations import PreparationStore
from .recipes import RecipeStore
from .settings import SecretStore, SettingsError, normalize_settings, settings_digest, validate_settings_choices


class HostError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


class HostServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # Uvicorn's default context re-raises SIGTERM after serving, which would
        # bypass this host's outer cleanup. The host owns the entire process and
        # consumes termination only after its native children have been reaped.
        originals = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in originals.items():
                signal.signal(sig, handler)


def capabilities() -> dict:
    return {"api_version": 1, "host_version": "0.1.0", "package_schema_versions": [1],
            "profiles": ["mcp-stdio", "mcp-http", "model-service"], "protocol_versions": ["2025-11-25"], "capabilities": ["tools"],
            "start_policies": ["manual", "host"], "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "slot_actions": ["start", "stop", "restart", "remove"], "slot_settings": ["start_policy"],
            "limits": {"slots": 16, "request_bytes": DEFAULT_REQUEST_BYTES,
                       "max_request_bytes": MAX_REQUEST_BYTES, "idle_seconds": 300},
            "installation": "prepared_local_packages_and_imports", "management_credentials": "owner_and_scoped_grants",
            "management_grant_modes": ["read", "prepare", "lifecycle", "install"], "management_mcp": "scoped_stdio",
            "import_schema_versions": [1], "import_sources": ["local_folder", "markdown", "text", "prepared_package", "artifact_collection"],
            "preparation_templates": ["python-function-v1", "existing-mcp-v1", "jevk5-llamacpp-v1"],
            "preparation_descriptor_schema_versions": [1], "recipe_bundle_schema_versions": [1],
            "recipe_management": "local_static_declarative", "recipe_execution": False,
            "automatic_preparation": False,
            "configuration": {"schema_versions": [1], "types": ["string", "integer", "boolean"],
                              "delivery": "private_inherited_descriptor", "mutable_installed_settings": True,
                              "updates": "reviewed_generation_upgrade"},
            "secrets": {"references": "immutable_owner_managed", "credentials": "owner_only",
                        "value_export": False, "rotation": "new_reference"},
            "downloads": {"format": "zip", "transport": "https", "credentials": "owner_only",
                          "pins": ["sha256", "size"], "redirects": False, "resume_partial": False},
            "upgrades": {"credentials": "owner_only", "data_policy": "clone_before_probe",
                         "rollback_policy": "before_exposure_only", "recovery": "selected_generation"},
            "model_intake": {"contract_versions": [1], "kind": "artifact_collection", "weights": ["gguf"],
                             "profiles": ["jevk5-llamacpp-v1"], "max_artifact_bytes": 64 * 1024**3},
            "unsupported": ["first_call", "reference_document_service", "integrated_preparation"]}


def bind_listener(port: int = 0) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Reuse TCP TIME_WAIT state after shutdown, never an existing listener.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise HostError("port_conflict", 409) from None


def preferred_listener() -> socket.socket:
    for port in range(19386, 19486):
        try:
            return bind_listener(port)
        except HostError:
            pass
    return bind_listener()


@dataclass
class Worker:
    process: OwnedProcess
    channel: socket.socket
    service: asyncio.Task | None = None
    monitor: asyncio.Task | None = None
    closing: bool = False
    activity_in_flight: int = 0
    activity_channel: socket.socket | None = None
    activity_service: asyncio.Task | None = None


@dataclass
class Resident:
    process_id: str
    process: OwnedProcess
    endpoint: str
    key: str
    nonce: str
    key_file: Path
    generation: int
    monitor: asyncio.Task | None = None


class Supervisor(UpgradeHost):
    def __init__(self, state_dir: Path, reaper: Path):
        self.root = ensure_private_directory(state_dir.absolute())
        self.lock_fd = os.open(self.root / "supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise HostError("host_already_running", 409) from None
        try:
            self.registry = Registry(self.root)
        except BaseException:
            os.close(self.lock_fd)
            raise
        self.reaper = reaper.absolute()
        self.listeners: dict[str, socket.socket] = {}
        self.plan_listeners: dict[str, socket.socket] = {}
        self.workers: dict[str, Worker] = {}
        self.started_at = timestamp()
        self.activities: dict[str, Activity] = {}
        self.owners: dict[str, ProcessOwner] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.ledgers: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.static_tasks: set[asyncio.Task] = set()
        self.install_lock = asyncio.Lock()
        self.closing = False
        self.residents: dict[str, dict[str, Resident]] = {}
        self.stopping_residents: set[str] = set()
        self.reservations: dict[str, dict] = {}
        self.resource_lock = asyncio.Lock()
        self.legacy_locks: dict[str, int] = {}
        self.migration_blocked: set[str] = set()
        self.package_cache: dict[str, tuple[object, tuple]] = {}
        self.plan_imports: dict[str, tuple[str, int]] = {}
        self.candidate_slots = {}
        self.upgrade_cancels = {}
        try:
            self.imports = ImportStore(self.root)
            self.recipes = RecipeStore(self.imports)
            self.preparations = PreparationStore(self.imports, self.recipes)
            self.preparations.recover()
            self.grants = GrantStore(self.registry)
            self.resource_policy = self.load_resource_policy()
            self.secrets = SecretStore(self.root)
            self.downloads = DownloadManager(self.root)
            self.upgrades = UpgradeStore(self.root)
        except BaseException:
            if hasattr(self, "imports"):
                self.imports.close()
            self.registry.close()
            os.close(self.lock_fd)
            raise

    def load_resource_policy(self) -> dict:
        path = self.root / "resource-policy.json"
        if path.exists():
            policy = read_private_json(path)
        else:
            try:
                physical_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            except (ValueError, OSError):
                physical = ctypes.c_uint64()
                size = ctypes.c_size_t(ctypes.sizeof(physical))
                result = ctypes.CDLL(None).sysctlbyname(b"hw.memsize", ctypes.byref(physical), ctypes.byref(size), None, 0)
                if result != 0:
                    raise HostError("resource_policy_required")
                physical_bytes = physical.value
            policy = {"memory_budget_mb": max(1024, physical_bytes // (1024 * 1024) * 3 // 4),
                      "headroom_mb": 1024, "gpu_policy": "exclusive-residency"}
            write_private_json(path, policy)
        if (set(policy) != {"memory_budget_mb", "headroom_mb", "gpu_policy"}
                or any(type(policy.get(key)) is not int or policy[key] < 0 for key in ("memory_budget_mb", "headroom_mb"))
                or policy["memory_budget_mb"] < 1 or policy["gpu_policy"] != "exclusive-residency"):
            raise HostError("invalid_resource_policy")
        return policy

    async def static_work(self, function, *args):
        """Keep bounded file work owned until its thread has actually settled."""
        if self.closing:
            raise HostError("host_closing", 409)
        if len(self.static_tasks) >= 4:
            raise HostError("static_work_busy", 429)
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self.static_tasks.add(task)
        def completed(result):
            self.static_tasks.discard(result)
            if not result.cancelled():
                result.exception()  # Consume sanitized failures after a caller leaves.
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    def activity(self, slot_id: str) -> Activity:
        if slot_id not in self.activities:
            self.activities[slot_id] = Activity(started_at=self.started_at)
        return self.activities[slot_id]

    def slot_snapshot(self, slot) -> dict:
        names = connection_names((item.id, item.name) for item in self.registry.list_slots())
        return {**slot.to_dict(), "connection_name": names[slot.id],
                "activity": self.activity(slot.id).snapshot()}

    async def runtime_metrics(self, slot, package) -> dict | None:
        """Only known numeric JevK5 statistics cross into manager/UI snapshots."""
        if (package.manifest.profile != "model-service"
                or {tool.name for tool in package.manifest.tools} != {"jevk5_decide", "jevk5_status"}):
            return None
        runtime = self.model_runtime(slot.id)
        if runtime is None:
            return None
        try:
            async with asyncio.timeout(1), httpx.AsyncClient(timeout=1, trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", runtime["endpoint"].removesuffix("/mcp") + "/api/status",
                        headers={"Authorization": "Bearer " + runtime["key"]}) as response:
                    if response.status_code != 200:
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 32768:
                            return None
            value = json.loads(body)
            counts = value.get("counts", {})
            result = {"counts": {key: counts[key] for key in ("decisions", "cache_hits", "errors")
                                  if type(counts.get(key)) is int and counts[key] >= 0}}
            if type(value.get("queue_depth")) is int and value["queue_depth"] >= 0:
                result["queue_depth"] = value["queue_depth"]
            return result if result["counts"] or "queue_depth" in result else None
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
            return None

    def retain_legacy_lock(self, slot_id: str, descriptor: int):
        if slot_id in self.legacy_locks:
            raise HostError("legacy_lock_already_retained")
        os.set_inheritable(descriptor, False)
        self.legacy_locks[slot_id] = descriptor

    def slot_directory(self, slot_id: str) -> Path:
        return ensure_private_directory(self.root / "slots" / slot_id)

    def ledger(self, slot_id: str) -> dict:
        if slot_id not in self.ledgers:
            path = self.slot_directory(slot_id) / "ownership.json"
            self.ledgers[slot_id] = read_private_json(path) if path.exists() else {}
        return self.ledgers[slot_id]

    def save_ledger(self, slot_id: str):
        write_private_json(self.slot_directory(slot_id) / "ownership.json", self.ledger(slot_id))

    def check_recovery_ownership(self, slot_id: str):
        ledger = self.ledger(slot_id)
        owner = self.owners.get(slot_id)
        if owner is None:
            if ledger:
                raise UpgradeError("ownership_lost")
            return
        if (any(not isinstance(value, dict) or value.get("state") != "owned" for value in ledger.values())
                or not owner.holds_live_instances(set(ledger))):
            raise UpgradeError("ownership_lost")

    def owner(self, slot_id: str) -> ProcessOwner:
        if slot_id not in self.owners:
            async def lost(process):
                self.ledger(slot_id).setdefault(process.instance_id, {})["state"] = "ownership_lost"
                self.save_ledger(slot_id)
                self.registry.update_slot(slot_id, observed_state="ownership_lost", error_code="ownership_lost")
                if not self.closing:
                    self.track(asyncio.create_task(self.close_lost_slot(slot_id)))
            self.owners[slot_id] = ProcessOwner(self.reaper, on_ownership_lost=lost)
        return self.owners[slot_id]

    def track(self, task: asyncio.Task):
        self.tasks.add(task)
        def completed(value):
            self.tasks.discard(value)
            if not value.cancelled():
                value.exception()  # Never let asyncio emit raw background exceptions.
        task.add_done_callback(completed)

    async def close_lost_slot(self, slot_id: str):
        # The loss callback must return before waiting on the lost process's
        # completion future. Close admission and reap all remaining owned children.
        with contextlib.suppress(Exception):
            await self.stop_gateway(slot_id)
        with contextlib.suppress(Exception):
            await self.stop_residents(slot_id)
        for process in list(self.owner(slot_id).instances.values()):
            if not process.ownership_lost:
                with contextlib.suppress(Exception):
                    await self.reap(slot_id, process)

    async def launch(self, slot_id: str, **kwargs) -> OwnedProcess:
        identifier = uuid.uuid4().hex
        completion_fd, completion = prepare_receipt(self.slot_directory(slot_id), identifier)
        try:
            self.ledger(slot_id)[identifier] = {"state": "launching", "completion": completion}
            self.save_ledger(slot_id)
            process = await self.owner(slot_id).launch(instance_id=identifier,
                completion_fd=completion_fd, completion_nonce=completion["nonce"], **kwargs)
        except BaseException:
            process = self.owner(slot_id).instances.get(identifier)
            if process is None:
                self.ledger(slot_id).pop(identifier, None)
                self.save_ledger(slot_id)
                self.retire_completion(slot_id, completion)
            raise
        finally:
            os.close(completion_fd)
        self.ledger(slot_id)[identifier]["state"] = "owned"
        self.save_ledger(slot_id)
        return process

    async def reap(self, slot_id: str, process: OwnedProcess):
        await process.stop()
        self.record_owned_exit(slot_id, process)

    def record_owned_exit(self, slot_id: str, process: OwnedProcess):
        completion = self.ledger(slot_id).get(process.instance_id, {}).get("completion")
        if completion is not None and not verify_receipt(self.slot_directory(slot_id), completion):
            raise HostError("completion_unconfirmed", 409)
        self.ledger(slot_id).pop(process.instance_id, None)
        self.save_ledger(slot_id)
        if completion is not None:
            self.retire_completion(slot_id, completion)

    def retire_completion(self, slot_id: str, completion: dict):
        if not verify_receipt(self.slot_directory(slot_id), completion):
            return
        path = self.slot_directory(slot_id) / "ownership-receipts" / (completion["instance_id"] + ".receipt")
        with contextlib.suppress(PrivateStateError, OSError):
            check_private_file(path)
            path.unlink()
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    async def reconcile_owned_receipts(self):
        # Reapers retain the child identity after this process dies. A bounded
        # wait lets their stop grace finish before classifying unresolved state.
        deadline = asyncio.get_running_loop().time() + 35
        while True:
            pending = False
            for slot in self.registry.list_slots():
                ledger = self.ledger(slot.id)
                removed = []
                for identifier, value in list(ledger.items()):
                    completion = value.get("completion")
                    if (isinstance(completion, dict) and completion.get("instance_id") == identifier
                            and verify_receipt(self.slot_directory(slot.id), completion)):
                        ledger.pop(identifier)
                        removed.append(completion)
                    elif completion is not None and value.get("state") != "ownership_lost":
                        pending = True
                if removed:
                    self.save_ledger(slot.id)
                    for completion in removed:
                        self.retire_completion(slot.id, completion)
                    if not ledger:
                        self.registry.reconcile_owned_exit(slot.id, expected_generation=slot.generation)
            if not pending or asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(0.05)

    def connection(self, slot_id: str) -> dict:
        return read_private_json(self.slot_directory(slot_id) / "connection.json")

    def package(self, slot):
        cached = self.package_cache.get(slot.package_path)
        if cached and self.package_stamp(cached[0]) == cached[1]:
            package = cached[0]
        else:
            package = validate_package(Path(slot.package_path))
            self.package_cache[slot.package_path] = (package, self.package_stamp(package))
        if package.digest != slot.package_digest:
            raise HostError("package_digest_mismatch")
        return package

    @staticmethod
    def package_stamp(package) -> tuple:
        paths = {package.root, package.root / "mcpme.package.json", package.root / "mcpme.lock.json"}
        for artifact in package.lock.artifacts:
            path = package.root / artifact.path
            paths.add(path)
            paths.update(parent for parent in path.parents if parent == package.root or package.root in parent.parents)
        try:
            result = []
            for path in sorted(paths):
                details = path.lstat()
                result.append((str(path), *[getattr(details, attr) for attr in
                              ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink", "st_uid")]))
            return tuple(result)
        except OSError:
            raise HostError("package_changed") from None

    def activation(self, slot_id: str) -> dict:
        path = self.slot_directory(slot_id) / "activation.json"
        return read_private_json(path) if path.exists() else {}

    def package_settings(self, slot, package=None) -> dict:
        package = package or self.package(slot)
        if slot.settings_digest is None:
            # Existing installations predate user settings. Do not discover an
            # unbound file, or silently supply newly required configuration.
            return normalize_settings(package.manifest)
        path = Path(slot.settings_path) if slot.settings_path else self.slot_directory(slot.id) / "settings.json"
        self.check_slot_reference(slot.id, path)
        settings = normalize_settings(package.manifest, read_private_json(path, max_bytes=65536))
        if settings_digest(settings) != slot.settings_digest:
            raise SettingsError("settings_digest_mismatch")
        return settings

    async def launch_package(self, slot, package, **kwargs) -> OwnedProcess:
        settings = self.package_settings(slot, package)
        self.secrets.resolve(settings["secret_references"])
        if not package.manifest.configuration.fields and not package.manifest.access.secrets:
            return await self.launch(slot.id, **kwargs)
        # Only the selected package child inherits this descriptor. The gateway,
        # argv, environment values, and durable slot state contain no material.
        sender, receiver = socket.socketpair()
        sender.setblocking(False)
        process = None
        try:
            environment = dict(kwargs.pop("env"))
            environment["MCPME_SETTINGS_FD"] = str(receiver.fileno())
            inherited = (*kwargs.pop("inherited_fds", ()), receiver.fileno())
            process = await self.launch(slot.id, env=environment, inherited_fds=inherited, **kwargs)
            # Spawn acknowledgment yields to owner mutations. Resolve again now,
            # so a revocation during launch cannot deliver a cached value.
            values = self.secrets.resolve(settings["secret_references"])
            payload = json.dumps({"schema_version": 1, "configuration": settings["configuration"],
                                  "secrets": values}, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
            # JSON escaping can expand each permitted secret byte sixfold.
            # This bound covers all 16 x 4 KiB values plus bounded settings.
            if len(payload) > 524288:
                raise SettingsError("invalid_settings")
            # Spawn acknowledgment precedes this send. A child that never reads
            # cannot block the host; failed delivery reaps the owned child.
            async with asyncio.timeout(10):
                await asyncio.get_running_loop().sock_sendall(sender, payload)
            sender.shutdown(socket.SHUT_WR)
            return process
        except BaseException:
            if process is not None:
                cleanup = asyncio.create_task(self.reap(slot.id, process))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
            raise
        finally:
            sender.close()
            receiver.close()

    async def reserve_model(self, slot, package):
        async with self.resource_lock:
            if slot.id in self.reservations:
                return
            memory = package.manifest.resources.memory_mb + self.resource_policy["headroom_mb"]
            gpu = package.manifest.resources.gpu
            if (sum(value["memory_mb"] for value in self.reservations.values()) + memory > self.resource_policy["memory_budget_mb"]
                    or gpu and any(value["gpu"] for value in self.reservations.values())):
                raise HostError("resource_blocked", 409)
            reservation = {"memory_mb": memory, "gpu": gpu}
            write_private_json(self.slot_directory(slot.id) / "reservation.json", reservation)
            self.reservations[slot.id] = reservation

    async def release_model(self, slot_id: str):
        async with self.resource_lock:
            if any(process.ownership_lost for process in self.owner(slot_id).instances.values()):
                return
            if self.residents.get(slot_id):
                return
            self.reservations.pop(slot_id, None)
            (self.slot_directory(slot_id) / "reservation.json").unlink(missing_ok=True)

    @staticmethod
    def process_order(package):
        remaining = list(package.manifest.processes)
        ordered = []
        while remaining:
            ready = [process for process in remaining if set(process.depends_on) <= {value.id for value in ordered}]
            if not ready:
                raise HostError("invalid_process_graph")
            for process in ready:
                ordered.append(process)
                remaining.remove(process)
        return ordered

    async def launch_resident(self, slot, package, declaration, *, deadline: float | None = None) -> Resident:
        runtime = ensure_private_directory(self.slot_directory(slot.id) / "runtime")
        data = self.data_directory(slot)
        dependencies = self.residents.setdefault(slot.id, {})
        for attempt in range(3):
            listener = bind_listener()
            port = listener.getsockname()[1]
            listener.close()  # Unsupported socket-activation engines retry below.
            key, nonce = secrets.token_urlsafe(48), secrets.token_hex(32)
            key_file = runtime / (declaration.id + "-" + secrets.token_hex(12) + ".key")
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(key + "\n")
            config_read, config_write = os.pipe()
            endpoint = f"http://127.0.0.1:{port}"
            config = {"slot_id": slot.id, "generation": slot.generation, "process_id": declaration.id,
                      "nonce": nonce, "endpoint": {"host": "127.0.0.1", "port": port}, "api_key": key,
                      "api_key_file": str(key_file), "runtime_dir": str(runtime), "package_dir": str(package.root),
                      "public_endpoint": f"http://127.0.0.1:{slot.public_port}",
                      "model": package.manifest.model.model_dump() if package.manifest.model else None,
                      "lifecycle": package.manifest.lifecycle.model_dump(),
                      "dependencies": {name: {"endpoint": dependencies[name].endpoint, "api_key": dependencies[name].key,
                                              "nonce": dependencies[name].nonce, "instance_id": dependencies[name].process.instance_id,
                                              "pid": dependencies[name].process.pid}
                                       for name in declaration.depends_on}}
            payload = json.dumps(config, separators=(",", ":")).encode()
            if len(payload) > 65536:
                os.close(config_read); os.close(config_write); key_file.unlink()
                raise HostError("runtime_config_limit")
            # A writer thread avoids blocking the supervisor on a full config pipe.
            def write_config():
                try:
                    with os.fdopen(config_write, "wb", buffering=0) as stream:
                        view = memoryview(payload)
                        while view:
                            view = view[stream.write(view):]
                except OSError:
                    pass
            writer = asyncio.create_task(asyncio.to_thread(write_config))
            process = None
            resident = None
            try:
                entry = declaration.entrypoint
                acknowledgment_timeout = min(package.manifest.lifecycle.startup_timeout_seconds, 120)
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError()
                    acknowledgment_timeout = min(acknowledgment_timeout, remaining)
                process = await self.launch_package(slot, package, executable=package.root / entry.executable,
                    args=[*entry.args, "--mcpme-config-fd", str(config_read)], cwd=package.root / entry.cwd,
                    env={"PATH": "/usr/bin:/bin", "HOME": str(data), "TMPDIR": str(runtime), "LANG": "en_US.UTF-8",
                         "PYTHONDONTWRITEBYTECODE": "1"}, inherited_fds=(config_read,), io_mode="fds",
                    startup_timeout=acknowledgment_timeout,
                    stop_grace=package.manifest.lifecycle.stop_grace_seconds)
                process.close_stdio()
                resident = Resident(declaration.id, process, endpoint, key, nonce, key_file, slot.generation)
                dependencies[declaration.id] = resident
                await asyncio.wait_for(writer, 5)
                async with asyncio.timeout(package.manifest.lifecycle.startup_timeout_seconds), httpx.AsyncClient(timeout=1, trust_env=False) as client:
                    while True:
                        if process.returncode is not None or process.ownership_lost:
                            raise HostError("runtime_start_failed")
                        try:
                            response = await client.get(endpoint + declaration.readiness.path,
                                                        headers={"Authorization": "Bearer " + key})
                            if response.status_code == 200:
                                denied = await client.get(endpoint + declaration.readiness.path)
                                if denied.status_code != 401:
                                    raise HostError("runtime_authentication_failed")
                                if declaration.readiness.echo_generation:
                                    value = response.json()
                                    if value.get("generation") != slot.generation or value.get("nonce") != nonce or value.get("ready") is not True:
                                        raise HostError("runtime_identity_mismatch")
                                # Recheck native ownership after authenticated readiness.
                                if process.returncode is not None or process.ownership_lost:
                                    raise HostError("runtime_start_failed")
                                return resident
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
            except BaseException as error:
                if process is not None:
                    await self.reap(slot.id, process)
                dependencies.pop(declaration.id, None)
                if (not isinstance(error, HostError) or error.code != "runtime_start_failed"
                        or attempt == 2 or process is None or process.returncode in (None, 0)):
                    raise
            finally:
                os.close(config_read)
                if not writer.done():
                    # Closing all read ends after reaping makes the writer fail safely.
                    await asyncio.gather(writer, return_exceptions=True)
                if ((resident is None or declaration.id not in dependencies)
                        and not any(value.ownership_lost for value in self.owner(slot.id).instances.values())):
                    key_file.unlink(missing_ok=True)
        raise HostError("runtime_start_failed")

    async def start_residents(self, slot, *, business_smoke: bool = True):
        package = await asyncio.to_thread(self.package, slot)
        await self.reserve_model(slot, package)
        try:
            # The declared startup budget covers the entire graph and protocol
            # probe; adding dependencies must not multiply the public deadline.
            deadline = asyncio.get_running_loop().time() + package.manifest.lifecycle.startup_timeout_seconds
            async with asyncio.timeout_at(deadline):
                for declaration in self.process_order(package):
                    await self.launch_resident(slot, package, declaration, deadline=deadline)
                await self.check_http_candidate(slot, package, business_smoke=business_smoke)
            for resident in self.residents[slot.id].values():
                resident.monitor = asyncio.create_task(self.monitor_resident(slot.id, resident))
        except BaseException:
            await self.stop_residents(slot.id)
            raise

    async def monitor_resident(self, slot_id: str, resident: Resident):
        try:
            await resident.process.wait()
            self.record_owned_exit(slot_id, resident.process)
        except Exception:
            pass
        if slot_id not in self.stopping_residents and not self.closing:
            if self.registry.get_slot(slot_id).error_code != "ownership_lost":
                self.registry.update_slot(slot_id, observed_state="failed", error_code="runtime_crashed")
            self.track(asyncio.create_task(self.close_lost_slot(slot_id)))

    async def stop_residents(self, slot_id: str):
        self.stopping_residents.add(slot_id)
        try:
            residents = self.residents.get(slot_id, {})
            for resident in reversed(list(residents.values())):
                if resident.monitor and resident.monitor is not asyncio.current_task():
                    resident.monitor.cancel()
                    await asyncio.gather(resident.monitor, return_exceptions=True)
                if resident.process.ownership_lost:
                    continue
                await self.reap(slot_id, resident.process)
                resident.key_file.unlink(missing_ok=True)
                residents.pop(resident.process_id, None)
            if not residents:
                self.residents.pop(slot_id, None)
            await self.release_model(slot_id)
        finally:
            self.stopping_residents.discard(slot_id)

    def model_runtime(self, slot_id: str) -> dict | None:
        slot = self.candidate_slots.get(slot_id) or self.registry.get_slot(slot_id)
        package = self.package(slot)
        resident = self.residents.get(slot_id, {}).get(package.manifest.adapter_process)
        if resident is None or resident.process.returncode is not None or resident.process.ownership_lost:
            return None
        return {"endpoint": resident.endpoint + "/mcp", "key": resident.key, "nonce": resident.nonce, "generation": resident.generation}

    async def check_http_candidate(self, slot, package, *, business_smoke: bool = True):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        runtime = self.model_runtime(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + runtime["key"]}, timeout=10, trust_env=False) as http:
            async with asyncio.timeout(package.manifest.lifecycle.startup_timeout_seconds):
                async with streamable_http_client(runtime["endpoint"], http_client=http) as (read, write, get_session_id):
                    async with ClientSession(read, write) as session:
                        result = await session.initialize()
                        if result.protocolVersion != "2025-11-25" or result.capabilities.model_dump(exclude_none=True).keys() - {"tools"}:
                            raise HostError("unsupported_mcp_capabilities")
                        listing = await session.list_tools()
                        if listing.nextCursor or [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] != [tool.mcp_schema() for tool in package.manifest.tools]:
                            raise HostError("tool_schema_mismatch")
                        for fixture in package.manifest.validation.fixtures if business_smoke else ():
                            smoke_result = await session.call_tool(fixture.tool, fixture.arguments)
                            if smoke_result.isError or smoke_result.structuredContent != fixture.expected:
                                raise HostError("smoke_probe_failed")
                        private_session = get_session_id()
                        if private_session is not None:
                            headers = {"Mcp-Session-Id": private_session, "Mcp-Protocol-Version": result.protocolVersion}
                            response = await http.delete(runtime["endpoint"], headers=headers)
                            if response.status_code not in {200, 204, 404}:
                                raise HostError("session_termination_failed")
                            response = await http.post(runtime["endpoint"], headers=headers, json={
                                "jsonrpc": "2.0", "id": "termination-probe", "method": "ping"})
                            if response.status_code != 404:
                                raise HostError("session_termination_unverified")

    async def package_process(self, slot, *, io_mode="streams") -> OwnedProcess:
        package = self.package(slot)
        entry = package.manifest.entrypoint
        data = self.data_directory(slot)
        runtime = ensure_private_directory(self.slot_directory(slot.id) / "runtime")
        return await self.launch_package(slot, package,
            executable=package.root / entry.executable, args=entry.args, cwd=package.root / entry.cwd,
            env={"PATH": "/usr/bin:/bin", "HOME": str(data), "TMPDIR": str(runtime), "LANG": "en_US.UTF-8"},
            io_mode=io_mode, startup_timeout=min(package.manifest.lifecycle.startup_timeout_seconds, 120),
            stop_grace=package.manifest.lifecycle.stop_grace_seconds)

    async def check_candidate(self, slot, *, business_smoke: bool = True):
        package = self.package(slot)
        if package.manifest.profile in {"model-service", "mcp-http"}:
            await self.start_residents(slot, business_smoke=business_smoke)
            return
        process = await self.package_process(slot)
        async def exchange(message):
            process.stdin.write(json.dumps(message).encode() + b"\n")
            await process.stdin.drain()
            for _ in range(32):
                line = await process.stdout.readline()
                if not line or len(line) > 1048576:
                    raise HostError("invalid_mcp_response")
                value = json.loads(line)
                if value.get("id") == message["id"]:
                    if "error" in value:
                        raise HostError("candidate_validation_failed")
                    return value["result"]
            raise HostError("invalid_mcp_response")
        try:
            async with asyncio.timeout(package.manifest.lifecycle.startup_timeout_seconds):
                result = await exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "mcpme-verifier", "version": "0.1.0"}}})
                if result.get("protocolVersion") != "2025-11-25" or set(result.get("capabilities", {})) != {"tools"}:
                    raise HostError("unsupported_mcp_capabilities")
                process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await process.stdin.drain()
                listing = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                if listing.get("nextCursor") or listing.get("tools") != [tool.mcp_schema() for tool in package.manifest.tools]:
                    raise HostError("tool_schema_mismatch")
                from mcp.types import CallToolResult
                for index, fixture in enumerate(package.manifest.validation.fixtures if business_smoke else (), 3):
                    response = await exchange({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                                               "params": {"name": fixture.tool, "arguments": fixture.arguments}})
                    try:
                        result = CallToolResult.model_validate(response)
                    except Exception:
                        raise HostError("smoke_probe_failed") from None
                    if result.isError or result.structuredContent != fixture.expected:
                        raise HostError("smoke_probe_failed")
        finally:
            await self.reap(slot.id, process)

    async def start_gateway(self, slot, active: bool, *, inactive_code="slot_stopped"):
        package = self.package(slot)
        lifecycle = package.manifest.lifecycle
        listener = self.listeners[slot.id]
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        parent.setblocking(False)
        activity_parent, activity_child = socket.socketpair()
        activity_parent.setblocking(False)
        config_read, config_write = os.pipe()
        config = {"token": self.connection(slot.id)["token"], "port": slot.public_port, "active": active,
                  "inactive_code": inactive_code,
                  "max_sessions": lifecycle.max_sessions, "idle_timeout": lifecycle.idle_timeout_seconds,
                  "max_request_bytes": lifecycle.max_request_bytes,
                  "request_timeout": lifecycle.operation_timeout_seconds,
                  "control_timeout": lifecycle.startup_timeout_seconds +
                      max(1, len(package.manifest.processes)) * (lifecycle.stop_grace_seconds + 5) + 5}
        if package.manifest.profile in {"model-service", "mcp-http"}:
            model_runtime = self.model_runtime(slot.id) if active else None
            config.update({"profile": package.manifest.profile, "upstream_endpoint": model_runtime["endpoint"] if model_runtime else None,
                           "upstream_key": model_runtime["key"] if model_runtime else None,
                           "upstream_nonce": model_runtime["nonce"] if model_runtime else None,
                           "generation": model_runtime["generation"] if model_runtime else slot.generation,
                           "legacy_routes": ["/api/status", "/api/decide", "/api/control"] if self.activation(slot.id).get("legacy_jev") else []})
        os.write(config_write, json.dumps(config).encode())
        os.close(config_write)
        host_paths = [str(Path(__file__).resolve().parents[1])]
        bundled_dependencies = Path(sys.executable).parent.parent / "site-packages"
        if bundled_dependencies.is_dir():
            host_paths.append(str(bundled_dependencies))
        private_listeners = []
        try:
            private_listeners.append(bind_private_listener(self.root, slot.id))
            # Updated legacy adapters retain their original state location after
            # migration. Serve its private alias only while holding that lock.
            if slot.id in self.legacy_locks:
                for path in (self.root / "transfers").glob("*.json"):
                    transfer = read_private_json(path)
                    if transfer["slot_id"] == slot.id and transfer["stage"] != "rolled_back":
                        private_listeners.append(bind_private_listener(Path(transfer["legacy_state"]), "jevk5"))
                        break
            private_args = [value for listener in private_listeners
                            for value in ("--private-listener-fd", str(listener.fileno()))]
            process = await self.launch(slot.id, executable=Path(sys.executable),
                args=["-P", "-m", "mcpme.worker", "--config-fd", str(config_read), "--listener-fd", str(listener.fileno()),
                      "--channel-fd", str(child.fileno()), "--activity-fd", str(activity_child.fileno()), *private_args], cwd=self.root,
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": os.pathsep.join(host_paths),
                     "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                inherited_fds=(config_read, listener.fileno(), child.fileno(), activity_child.fileno(),
                               *(private.fileno() for private in private_listeners)), io_mode="fds", stop_grace=3)
            process.close_stdio()
        except BaseException:
            parent.close()
            activity_parent.close()
            raise
        finally:
            child.close()
            activity_child.close()
            os.close(config_read)
            for private in private_listeners:
                private.close()
        worker = Worker(process, parent)
        worker.activity_channel = activity_parent
        self.workers[slot.id] = worker
        worker.activity_service = asyncio.create_task(self.serve_activity(slot.id, worker))
        worker.service = asyncio.create_task(self.serve_channel(slot, worker, active))
        worker.monitor = asyncio.create_task(self.monitor_gateway(slot.id, worker))
        # Verify the owned worker actually serves the listener before publishing Ready.
        try:
            async with asyncio.timeout(10), httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                while True:
                    if process.returncode is not None or process.ownership_lost:
                        raise HostError("gateway_failed")
                    try:
                        response = await client.get(f"http://127.0.0.1:{slot.public_port}/mcp")
                        if response.status_code == 401:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
        except BaseException:
            await self.stop_gateway(slot.id)
            raise

    async def serve_activity(self, slot_id: str, worker: Worker):
        activity = self.activity(slot_id)
        buffered = bytearray()
        complete = False
        try:
            while True:
                chunk = await asyncio.get_running_loop().sock_recv(worker.activity_channel, 4096)
                if not chunk:
                    break
                buffered.extend(chunk)
                while len(buffered) >= ACTIVITY_FRAME.size:
                    code, latency = ACTIVITY_FRAME.unpack(buffered[:ACTIVITY_FRAME.size])
                    del buffered[:ACTIVITY_FRAME.size]
                    if code == 0 and latency == 0 and not buffered:
                        complete = True
                        return
                    event = ACTIVITY_EVENTS[code]
                    if event != "received" and worker.activity_in_flight == 0:
                        raise ValueError("invalid_activity")
                    if event == "received" and latency != 0:
                        raise ValueError("invalid_activity")
                    activity.record(event, None if event == "received" else latency)
                    worker.activity_in_flight += 1 if event == "received" else -1
        except (OSError, ValueError, KeyError):
            pass
        finally:
            if not complete or buffered or worker.activity_in_flight:
                activity.available = False
            activity.abandon(worker.activity_in_flight)
            worker.activity_in_flight = 0
            worker.activity_channel.close()

    async def serve_channel(self, slot, worker: Worker, active: bool):
        instances: dict[str, OwnedProcess] = {}
        session_ids: set[str] = set()
        instance_sessions: dict[str, str] = {}
        package = self.package(slot)
        try:
            while True:
                payload = await asyncio.get_running_loop().sock_recv(worker.channel, 8192)
                if not payload:
                    break
                response, fds = {}, []
                process = None
                try:
                    request = json.loads(payload)
                    if (package.manifest.profile == "model-service" and set(request) == {"action", "control"}
                            and request["action"] == "slot_control" and request["control"] in {"status", "pause", "resume"}):
                        if self.workers.get(slot.id) is not worker or worker.closing or not self.activation(slot.id).get("legacy_jev"):
                            raise HostError("invalid_control")
                        response = await self.legacy_model_control(slot.id, request["control"])
                    elif (package.manifest.profile == "mcp-stdio" and set(request) == {"action", "session_id"}
                          and request["action"] == "launch_session"):
                        session_id = request["session_id"]
                        if not isinstance(session_id, str) or not 16 <= len(session_id) <= 128 or session_id in session_ids:
                            raise HostError("invalid_session")
                        current = self.registry.get_slot(slot.id)
                        if not active or worker.closing or current.generation != slot.generation or current.error_code == "ownership_lost":
                            raise HostError("slot_stopped")
                        if len(instances) >= package.manifest.lifecycle.max_sessions:
                            raise HostError("session_limit")
                        session_ids.add(session_id)
                        process = await self.package_process(slot, io_mode="fds")
                        instances[process.instance_id] = process
                        instance_sessions[process.instance_id] = session_id
                        # Recheck after launch acknowledgment before exporting capabilities.
                        if worker.closing or self.registry.get_slot(slot.id).generation != slot.generation:
                            await self.reap(slot.id, process)
                            raise HostError("slot_stopped")
                        response = {"instance_id": process.instance_id}
                        fds = [process.stdin_fd, process.stdout_fd]
                    elif set(request) == {"action", "instance_id"} and request["action"] == "close_session":
                        target = instances.get(request["instance_id"])
                        if target:
                            await self.reap(slot.id, target)
                            instances.pop(target.instance_id, None)
                            session_ids.discard(instance_sessions.pop(target.instance_id, ""))
                        response = {"closed": True}
                    else:
                        raise HostError("invalid_control")
                except Exception:
                    response, fds = {"error": "launch_unavailable"}, []
                ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
                worker.channel.sendmsg([json.dumps(response).encode()], ancillary)
                if process is not None and fds:
                    process.close_stdio()
        except (OSError, ValueError):
            pass
        finally:
            for process in list(instances.values()):
                with contextlib.suppress(Exception):
                    await self.reap(slot.id, process)
            if not worker.closing and not self.closing:
                async def channel_lost():
                    if self.registry.get_slot(slot.id).error_code != "ownership_lost":
                        self.registry.update_slot(slot.id, observed_state="failed", error_code="control_channel_lost")
                    with contextlib.suppress(Exception):
                        await self.stop_gateway(slot.id)
                self.track(asyncio.create_task(channel_lost()))

    async def monitor_gateway(self, slot_id: str, worker: Worker):
        try:
            await worker.process.wait()
            self.record_owned_exit(slot_id, worker.process)
        except Exception:
            pass
        with contextlib.suppress(OSError):
            worker.channel.shutdown(socket.SHUT_RDWR)
        if worker.service:
            if not worker.service.cancelling():
                worker.service.cancel()
            await asyncio.gather(worker.service, return_exceptions=True)
        if worker.activity_service:
            # The owned process has exited, so its stream reaches EOF after all
            # already-sent events. Ownership loss may leave its target alive;
            # never wait indefinitely on a descriptor held by that target.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(worker.activity_service, return_exceptions=True), 1)
        if not worker.closing:
            code = "ownership_lost" if worker.process.ownership_lost or self.registry.get_slot(slot_id).error_code == "ownership_lost" else "gateway_failed"
            self.registry.update_slot(slot_id, observed_state="ownership_lost" if code == "ownership_lost" else "failed", error_code=code)

    async def stop_gateway(self, slot_id: str, *, stop_runtime: bool = True):
        worker = self.workers.pop(slot_id, None)
        if worker is None:
            if stop_runtime:
                await self.stop_residents(slot_id)
            return
        worker.closing = True
        try:
            await self.reap(slot_id, worker.process)
        finally:
            with contextlib.suppress(OSError):
                worker.channel.shutdown(socket.SHUT_RDWR)
            if worker.service:
                if not worker.service.cancelling():
                    worker.service.cancel()
                await asyncio.gather(worker.service, return_exceptions=True)
            if worker.monitor:
                await worker.monitor
            worker.channel.close()
            if stop_runtime:
                await self.stop_residents(slot_id)

    def migration_running(self, slot_id: str) -> bool:
        directory = self.root / "transfers"
        if not directory.exists():
            return False
        return any(record.get("slot_id") == slot_id and record.get("status") == "running"
                   for record in (read_private_json(path) for path in directory.glob("*.json")))

    def check_mutation(self, slot, *, operation_id: str | None = None, migration_internal: bool = False,
                       allow_exposed_stop: bool = False):
        if self.closing:
            raise HostError("host_closing", 409)
        if slot.id in self.migration_blocked and not migration_internal:
            raise HostError("migration_recovery_required", 409)
        if not migration_internal and self.migration_running(slot.id):
            raise HostError("operation_in_progress", 409)
        for operation in self.registry.list_unfinished_operations():
            if operation.slot_id != slot.id or operation.id == operation_id:
                continue
            if (allow_exposed_stop and operation.action in {"install", "upgrade"} and operation.status == "recovery_required"
                    and operation.details.get("exposed")):
                continue
            if operation.status in {"pending", "running"}:
                raise HostError("operation_in_progress", 409)
            if operation.action in {"install", "remove", "upgrade"}:
                code = {"install": "installation_recovery_required", "remove": "removal_recovery_required",
                        "upgrade": "upgrade_recovery_required"}[operation.action]
                raise HostError(code, 409)
        if slot.error_code in {"installation_recovery_required", "upgrade_recovery_required"}:
            recovery = self.registry.get_operation(operation_id) if operation_id else None
            if (not allow_exposed_stop
                    and (recovery is None or recovery.slot_id != slot.id or recovery.action not in {"install", "upgrade"}
                         or recovery.status != "recovery_required")):
                raise HostError(slot.error_code, 409)
        if (slot.observed_state == "ownership_lost" or slot.error_code == "ownership_lost"
                or any(value.get("state") == "ownership_lost" for value in self.ledger(slot.id).values())
                or any(process.ownership_lost for process in self.owner(slot.id).instances.values())):
            raise HostError("ownership_lost", 409)

    async def update_settings(self, slot_id: str, payload: dict) -> dict:
        if (set(payload) != {"start_policy"} or not isinstance(payload["start_policy"], str)
                or payload["start_policy"] not in {"manual", "host"}):
            raise HostError("invalid_start_policy")
        lock = self.locks.setdefault(slot_id, asyncio.Lock())
        if lock.locked():
            raise HostError("operation_in_progress", 409)
        async with lock:
            slot = self.registry.get_slot(slot_id)
            self.check_mutation(slot)
            # Startup preference does not change the current runtime or an
            # explicit stop's persisted wake suppression.
            updated = self.registry.update_slot(slot_id, expected_generation=slot.generation,
                                                start_policy=payload["start_policy"])
            return {"slot": updated.to_dict()}

    async def remove(self, slot, operation_id: str):
        directory = self.slot_directory(slot.id)
        self.registry.update_slot(slot.id, desired_state="stopped", observed_state="draining", wake_suppressed=True)
        write_private_json(directory / "activation.json", {**self.activation(slot.id), "exposure_permitted": False})
        await self.stop_gateway(slot.id)
        await self.stop_residents(slot.id)
        owner = self.owner(slot.id)
        for process in list(owner.instances.values()):
            await self.reap(slot.id, process)
        if owner.instances or self.ledger(slot.id) or owner.persistence_failed:
            raise HostError("ownership_lost" if any(process.ownership_lost for process in owner.instances.values())
                            else "shutdown_unconfirmed", 409)
        await self.release_model(slot.id)
        listener = self.listeners.pop(slot.id, None)
        if listener is not None:
            listener.close()
        # Public and private gateways are gone before their credential is revoked.
        with contextlib.suppress(FileNotFoundError):
            private_path = socket_path(self.root, slot.id)
            if private_path.exists() or private_path.is_symlink():
                checked_socket(private_path)
                private_path.unlink()
        (directory / "connection.json").unlink(missing_ok=True)
        from .migrations import retire_transfer
        retire_transfer(self, slot.id, operation_id)
        self.registry.update_slot(slot.id, observed_state="stopped", error_code=None)
        self.registry.complete_removal(slot.id, operation_id, expected_generation=slot.generation)
        await owner.close()
        self.owners.pop(slot.id, None)
        self.residents.pop(slot.id, None)
        self.ledgers.pop(slot.id, None)
        self.activities.pop(slot.id, None)
        self.migration_blocked.discard(slot.id)
        # Keep private data and shared package snapshots for deliberate recovery.

    async def control(self, slot_id: str, action: str, *, migration_internal: bool = False,
                      operation_id: str | None = None):
        if not isinstance(action, str) or action not in {"start", "stop", "restart", "remove"}:
            raise HostError("invalid_control")
        lock = self.locks.setdefault(slot_id, asyncio.Lock())
        if lock.locked():
            raise HostError("operation_in_progress", 409)
        async with lock:
            slot = self.registry.get_slot(slot_id)
            self.check_mutation(slot, operation_id=operation_id, migration_internal=migration_internal,
                                allow_exposed_stop=action == "stop")
            if action == "remove":
                if operation_id is None:
                    raise HostError("removal_operation_required")
                await self.remove(slot, operation_id)
                return {"slot_id": slot.id, "removed": True}
            if slot_id not in self.listeners:
                self.listeners[slot_id] = bind_listener(slot.public_port)
            worker = self.workers.get(slot_id)
            if (action == "start" and slot.observed_state == "ready" and worker is not None
                    and not worker.closing and worker.process.returncode is None and not worker.process.ownership_lost):
                return asdict(slot)
            self.registry.update_slot(slot_id, observed_state="draining")
            await self.stop_gateway(slot_id)
            await self.stop_residents(slot_id)
            slot = self.registry.update_slot(slot_id, generation=slot.generation + 1, error_code=None,
                desired_state="stopped" if action == "stop" else "running", observed_state="stopped" if action == "stop" else "starting",
                wake_suppressed=action == "stop")
            try:
                if action != "stop":
                    await self.check_candidate(slot, business_smoke=not bool(self.activation(slot_id).get("verified")))
                    self.secrets.resolve(self.package_settings(slot)["secret_references"])
                    write_private_json(self.slot_directory(slot_id) / "activation.json", {
                        **self.activation(slot_id), "generation": slot.generation, "verified": True, "exposure_permitted": True})
                await self.start_gateway(slot, active=action != "stop")
                slot = self.registry.update_slot(slot_id, observed_state="stopped" if action == "stop" else "ready")
            except Exception as error:
                code = error.code if isinstance(error, (HostError, PackageError, SettingsError)) else "start_failed"
                with contextlib.suppress(Exception):
                    await self.stop_gateway(slot_id)
                with contextlib.suppress(Exception):
                    await self.stop_residents(slot_id)
                if self.registry.get_slot(slot_id).error_code == "ownership_lost":
                    code = "ownership_lost"
                self.registry.update_slot(slot_id, observed_state="ownership_lost" if code == "ownership_lost" else "failed", error_code=code)
                raise HostError(code, 409) from None
            return asdict(slot)

    async def legacy_status(self, slot_id: str) -> dict:
        slot = self.registry.get_slot(slot_id)
        package = self.package(slot)
        runtime = self.model_runtime(slot_id)
        if runtime is not None:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                try:
                    response = await client.get(runtime["endpoint"].removesuffix("/mcp") + "/api/status", headers={"Authorization": "Bearer " + runtime["key"]})
                    if response.status_code == 200:
                        value = response.json()
                        if isinstance(value, dict):
                            return value
                except (httpx.HTTPError, ValueError):
                    pass
        model = package.manifest.model
        paused = slot.wake_suppressed or slot.desired_state == "stopped"
        return {"state": "paused" if paused else "error", "ready": False, "paused": paused,
                "model": {"id": model.model_id, "sha256": model.sha256, "profile": model.profile, "context_tokens": model.context_tokens},
                "endpoint": f"http://127.0.0.1:{slot.public_port}", "mcp_endpoint": f"http://127.0.0.1:{slot.public_port}/mcp",
                "counts": {"decisions": 0, "cache_hits": 0, "errors": 0}, "queue_depth": 0,
                "last_error": slot.error_code, "backend_pid": os.getpid(), "llama_pid": None}

    async def legacy_model_control(self, slot_id: str, action: str) -> dict:
        async with self.locks.setdefault(slot_id, asyncio.Lock()):
            slot = self.registry.get_slot(slot_id)
            if slot.error_code == "ownership_lost" or slot.id in self.migration_blocked:
                raise HostError(slot.error_code or "migration_recovery_required", 409)
            if action != "status":
                self.check_mutation(slot)
            if slot.error_code == "installation_recovery_required":
                raise HostError("installation_recovery_required", 409)
            if action != "status":
                if action == "resume" and self.model_runtime(slot_id) is not None and slot.observed_state == "ready":
                    return {"status": await self.legacy_status(slot_id), "runtime": self.model_runtime(slot_id)}
                self.registry.update_slot(slot_id, observed_state="draining")
                await self.stop_residents(slot_id)
                slot = self.registry.update_slot(slot_id, generation=slot.generation + 1, error_code=None,
                    desired_state="stopped" if action == "pause" else "running", wake_suppressed=action == "pause",
                    observed_state="stopped" if action == "pause" else "starting")
                if action == "resume":
                    try:
                        await self.start_residents(slot, business_smoke=not bool(self.activation(slot_id).get("verified")))
                        write_private_json(self.slot_directory(slot_id) / "activation.json", {
                            **self.activation(slot_id), "generation": slot.generation, "verified": True, "exposure_permitted": True})
                        self.registry.update_slot(slot_id, observed_state="ready")
                    except Exception as error:
                        code = error.code if isinstance(error, (HostError, PackageError)) else "start_failed"
                        if self.registry.get_slot(slot_id).error_code == "ownership_lost":
                            code = "ownership_lost"
                        self.registry.update_slot(slot_id, observed_state="ownership_lost" if code == "ownership_lost" else "failed", error_code=code)
                        raise HostError(code, 409) from None
            return {"status": await self.legacy_status(slot_id), "runtime": self.model_runtime(slot_id)}

    async def initialize(self):
        self.reconcile_imports()
        await self.reconcile_owned_receipts()
        try:
            from .migrations import initialize_transfers
        except ImportError:
            initialize_transfers = None
        if initialize_transfers is not None:
            await initialize_transfers(self)
        for slot in self.registry.list_slots():
            path = self.slot_directory(slot.id) / "reservation.json"
            if path.exists():
                reservation = read_private_json(path)
                if (set(reservation) != {"memory_mb", "gpu"} or type(reservation["memory_mb"]) is not int
                        or reservation["memory_mb"] < 1 or type(reservation["gpu"]) is not bool):
                    raise HostError("invalid_resource_reservation")
                if self.ledger(slot.id):
                    self.reservations[slot.id] = reservation
                else:
                    path.unlink()
        interrupted = set()
        for operation in self.registry.list_unfinished_operations():
            if operation.status in {"pending", "running"}:
                self.registry.update_operation(operation.id, "recovery_required", error_code="operation_interrupted")
            if operation.status in {"pending", "running", "recovery_required"} and operation.action == "install":
                interrupted.add(operation.slot_id)
        # A remove intent closes admission across host restarts. Replay only
        # after an empty ownership ledger proves no unresolved target remains.
        for operation in self.registry.list_unfinished_operations():
            if operation.action != "remove":
                continue
            if self.ledger(operation.slot_id):
                self.registry.update_operation(operation.id, "recovery_required", error_code="ownership_lost")
                continue
            await self.execute_control(operation)
        pending_removals = {operation.slot_id for operation in self.registry.list_unfinished_operations()
                            if operation.action == "remove"}
        recovered_upgrades = set()
        for operation in self.registry.list_unfinished_operations():
            if operation.action != "upgrade":
                continue
            recovered_upgrades.add(operation.slot_id)
            if self.ledger(operation.slot_id):
                self.registry.update_slot(operation.slot_id, observed_state="ownership_lost", error_code="ownership_lost")
                self.registry.update_operation(operation.id, "recovery_required", error_code="ownership_lost")
                continue
            await self.execute_upgrade(operation.id, self.read_upgrade_plan(operation.details["plan_id"]), recovery=True)
        self.reconcile_imports()
        for slot in self.registry.list_slots():
            if slot.id in self.migration_blocked:
                continue
            if slot.id in recovered_upgrades:
                # Recovery already classified prior ownership and may have
                # launched a new held gateway. Do not treat its fresh ledger
                # as an unresolved owner from the previous supervisor.
                continue
            if self.ledger(slot.id):
                self.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
                continue
            if slot.id in pending_removals:
                # Failed revocation or finalization must never republish a
                # partially removed app's endpoint during ordinary startup.
                continue
            if slot.id in interrupted:
                self.registry.update_slot(slot.id, observed_state="failed", error_code="installation_recovery_required")
                continue
            try:
                self.listeners[slot.id] = bind_listener(slot.public_port)
                if slot.start_policy == "host" and not slot.wake_suppressed:
                    await self.control(slot.id, "start")
                else:
                    self.registry.update_slot(slot.id, observed_state="stopped", error_code=None)
                    await self.start_gateway(slot, active=False)
            except Exception as error:
                code = error.code if isinstance(error, HostError) else "start_failed"
                self.registry.update_slot(slot.id, observed_state="failed", error_code=code)

    def reconcile_imports(self):
        # The durable registry intent is the authority boundary. Recover a crash
        # between that write and draft submission before any draft mutation.
        for operation in self.registry.list_import_operations():
            details = operation.details
            self.imports.submit(details["import_id"], details["import_revision"], details["import_digest"], operation.id)
            self.imports.operation_status(details["import_id"], operation.to_dict())

    def invalidate_import_plans(self, draft_id: str):
        for plan_id, binding in list(self.plan_imports.items()):
            if binding[0] == draft_id:
                listener = self.plan_listeners.pop(plan_id, None)
                if listener is not None:
                    listener.close()
                self.plan_imports.pop(plan_id, None)

    def _discard_unpublished_plan(self, plan_id: str):
        listener = self.plan_listeners.pop(plan_id, None)
        if listener is not None:
            listener.close()
        self.plan_imports.pop(plan_id, None)
        (self.root / "plans" / (plan_id + ".json")).unlink(missing_ok=True)
        directory = self.root / "staging" / plan_id
        if directory.exists() or directory.is_symlink():
            ensure_private_directory(directory)
            shutil.rmtree(directory)

    async def plan_install_async(self, payload: dict, *, _import_binding: dict | None = None, **overrides) -> dict:
        if "import_id" in payload:
            if set(payload) != {"import_id", "revision"} or overrides or _import_binding is not None:
                raise HostError("invalid_import_plan")
            self.reconcile_imports()
            draft = self.imports.inspect(payload["import_id"])
            attachment = draft.get("attachment")
            if attachment is None:
                raise HostError("package_required")
            draft = self.imports.validate_binding(draft["draft_id"], payload["revision"], attachment["digest"])
            binding = {"import_id": draft["draft_id"], "import_revision": payload["revision"],
                       "import_digest": attachment["digest"]}
            references = draft["requirements"].get("secret_references", {})
            result = await self.plan_install_async({"package": attachment["path"], "name": draft["name"],
                                                   "start": draft["start"], "start_policy": draft["start_policy"],
                                                   "settings": {"configuration": draft["requirements"].get("configuration", {}),
                                                       "secret_references": references if isinstance(references, dict) else {}}},
                                                  _import_binding=binding)
            try:
                self.imports.validate_binding(draft["draft_id"], payload["revision"], attachment["digest"])
                self.plan_imports[result["plan_id"]] = (draft["draft_id"], payload["revision"])
                return result
            except BaseException:
                self._discard_unpublished_plan(result["plan_id"])
                raise
        if set(payload) - {"package", "name", "start_policy", "start", "settings"} or not isinstance(payload.get("package"), str):
            raise HostError("invalid_install_plan")
        plan_id = str(uuid.uuid4())
        def stage():
            package = validate_package(Path(payload["package"]))
            return snapshot_package(package, self.root / "staging" / plan_id / "package")
        staging = asyncio.create_task(asyncio.to_thread(stage))
        try:
            snapshot = await asyncio.shield(staging)
            return self.plan_install(payload, _prepared=snapshot, _plan_id=plan_id,
                                     _import_binding=_import_binding, **overrides)
        except BaseException:
            # A canceled await does not stop a copying thread. Wait for that
            # thread before reclaiming its tree so it cannot recreate artifacts.
            while not staging.done():
                try:
                    await asyncio.shield(staging)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not staging.cancelled():
                staging.exception()
            self._discard_unpublished_plan(plan_id)
            raise

    def plan_install(self, payload: dict, *, slot_id: str | None = None, public_port: int | None = None,
                     invocation_token: str | None = None, legacy_jev: bool = False, _prepared=None, _plan_id=None,
                     _import_binding: dict | None = None) -> dict:
        if set(payload) - {"package", "name", "start_policy", "start", "settings"} or not isinstance(payload.get("package"), str):
            raise HostError("invalid_install_plan")
        if len(self.registry.list_slots()) >= 16 or len(self.plan_listeners) >= 32:
            raise HostError("capacity_exceeded", 409)
        start_policy = payload.get("start_policy", "manual")
        if start_policy not in {"manual", "host"} or type(payload.get("start", False)) is not bool:
            raise HostError("unsupported_start_policy")
        package = _prepared or validate_package(Path(payload["package"]))
        settings = normalize_settings(package.manifest, payload.get("settings"))
        self.secrets.resolve(settings["secret_references"])
        name = payload.get("name", package.manifest.name)
        if not isinstance(name, str) or not 1 <= len(name) <= 256 or any(ord(c) < 32 for c in name):
            raise HostError("invalid_slot_name")
        plan_id, slot_id = _plan_id or str(uuid.uuid4()), slot_id or str(uuid.uuid4())
        if str(uuid.UUID(slot_id)) != slot_id:
            raise HostError("invalid_slot_identity")
        if legacy_jev and (package.manifest.profile != "model-service" or {tool.name for tool in package.manifest.tools} != {"jevk5_decide", "jevk5_status"}):
            raise HostError("invalid_legacy_profile")
        snapshot = _prepared or snapshot_package(package, self.root / "staging" / plan_id / "package")
        if _import_binding is not None:
            self.imports.validate_binding(_import_binding["import_id"], _import_binding["import_revision"],
                                          _import_binding["import_digest"])
        listener = bind_listener(public_port) if public_port is not None else preferred_listener()
        plan = {"plan_id": plan_id, "slot_id": slot_id, "package_digest": snapshot.digest,
                "package_path": str(snapshot.root), "name": name, "start_policy": start_policy,
                "start": payload.get("start", False), "public_port": listener.getsockname()[1], "legacy_jev": legacy_jev,
                "settings": settings, "settings_digest": settings_digest(settings)}
        if _import_binding is not None:
            # The first durable publication must already enforce draft authority,
            # including if the host dies before returning the plan to its caller.
            plan.update(_import_binding)
        try:
            if invocation_token is not None:
                if not isinstance(invocation_token, str) or not 32 <= len(invocation_token) <= 512:
                    raise HostError("invalid_invocation_token")
                write_private_json(self.slot_directory(slot_id) / "connection.json", {
                    "slot_id": slot_id, "endpoint": f"http://127.0.0.1:{plan['public_port']}/mcp", "token": invocation_token,
                    "max_request_bytes": snapshot.manifest.lifecycle.max_request_bytes})
            write_private_json(self.root / "plans" / (plan_id + ".json"), plan)
        except BaseException:
            listener.close()
            raise
        self.plan_listeners[plan_id] = listener
        return {**plan, "plan_digest": plan_digest(plan), "package": package.manifest.model_dump(),
                "installed_bytes": sum(artifact.size for artifact in package.lock.artifacts),
                "publisher_trust": "local_owner", "access_enforcement": "declarations_only"}

    def inspect_plan(self, plan_id: str) -> dict:
        if not isinstance(plan_id, str) or str(uuid.UUID(plan_id)) != plan_id:
            raise HostError("invalid_plan")
        plan = read_private_json(self.root / "plans" / (plan_id + ".json"))
        package = validate_package(Path(plan["package_path"]))
        if package.digest != plan["package_digest"]:
            raise HostError("package_digest_mismatch", 409)
        self.check_plan_settings(plan, package)
        return {**plan, "plan_digest": plan_digest(plan), "package": package.manifest.model_dump(),
                "installed_bytes": sum(artifact.size for artifact in package.lock.artifacts),
                "publisher_trust": "local_owner", "access_enforcement": "declarations_only"}

    def check_plan_settings(self, plan: dict, package) -> dict:
        settings = normalize_settings(package.manifest, plan.get("settings"))
        self.check_settings_binding(plan, settings)
        return settings

    @staticmethod
    def check_settings_binding(plan: dict, settings: dict) -> None:
        if "settings_digest" in plan and settings_digest(settings) != plan["settings_digest"]:
            raise SettingsError("settings_digest_mismatch")
        if "settings" in plan and "settings_digest" not in plan:
            raise SettingsError("settings_digest_mismatch")

    async def apply_install(self, plan_id: str, *, grant_id: str | None = None) -> dict:
        if str(uuid.UUID(plan_id)) != plan_id:
            raise HostError("invalid_plan")
        async with self.install_lock:
            plan = read_private_json(self.root / "plans" / (plan_id + ".json"))
            if grant_id is not None:
                self.grants.validate_apply(grant_id, plan)
            key = plan_id
            details = {"plan_id": plan_id, "package_digest": plan["package_digest"], "package_path": plan["package_path"],
                       "selected": False, "exposed": False, "cancel_requested": False, "stage": "staging"}
            if "settings_digest" in plan:
                details["settings_digest"] = plan["settings_digest"]
            if "import_id" in plan:
                self.reconcile_imports()
                key = str(uuid.uuid5(uuid.UUID(plan["import_id"]), str(plan["import_revision"])))
                existing = self.registry.get_operation_by_key(key)
                if existing is not None:
                    if grant_id is not None:
                        with self.registry.transaction():
                            self.grants.bind_operation(grant_id, plan, existing)
                    if existing.details["plan_id"] != plan_id:
                        listener = self.plan_listeners.pop(plan_id, None)
                        if listener is not None:
                            listener.close()
                    # A transient draft-submission failure can leave a durable
                    # pending intent without its in-process execution task. Retry
                    # the originally authorized plan, even through another plan
                    # for the same import revision; never execute the alternate.
                    if existing.status == "pending":
                        original = read_private_json(self.root / "plans" / (existing.details["plan_id"] + ".json"))
                        self.schedule_install(existing, original)
                    return existing.to_dict()
                self.imports.validate_binding(plan["import_id"], plan["import_revision"], plan["import_digest"])
                details.update({field: plan[field] for field in ("import_id", "import_revision", "import_digest")})
            else:
                existing = self.registry.get_operation_by_key(key)
                if existing is not None:
                    if existing.action != "install" or existing.details.get("plan_id") != plan_id:
                        raise HostError("operation_conflict", 409)
                    if grant_id is not None:
                        with self.registry.transaction():
                            self.grants.bind_operation(grant_id, plan, existing)
                    if existing.status == "pending":
                        self.schedule_install(existing, plan)
                    return existing.to_dict()
            if self.registry.get_operation_by_key(key) is None:
                # Plans already contain effective choices. Preserve asynchronous
                # artifact verification and its durable failed-operation result.
                # No package read/execution is needed for reference availability.
                settings = validate_settings_choices(plan.get("settings"))
                self.check_settings_binding(plan, settings)
                self.secrets.resolve(settings["secret_references"])
            with self.registry.transaction():
                operation = self.registry.create_operation("install", slot_id=plan["slot_id"],
                    idempotency_key=key, details=details)
                if grant_id is not None:
                    self.grants.bind_operation(grant_id, plan, operation)
            if "import_id" in plan:
                self.imports.submit(plan["import_id"], plan["import_revision"], plan["import_digest"], operation.id)
            # A persisted pending operation belongs to this apply; in-process task
            # registration happens before yielding, so concurrent retries coalesce.
            self.schedule_install(operation, plan)
            return operation.to_dict()

    def schedule_install(self, operation, plan: dict, *, recovery: bool = False):
        if (operation.status == "pending" or recovery and operation.status == "recovery_required") and not any(
                getattr(task, "operation_id", None) == operation.id for task in self.tasks):
            task = asyncio.create_task(self.install(operation.id, plan))
            task.operation_id = operation.id
            self.track(task)

    async def install(self, operation_id: str, plan: dict):
        from .install_recovery import install_generation
        async with self.locks.setdefault(plan["slot_id"], asyncio.Lock()):
            await install_generation(self, operation_id, plan)

    async def install_checkpoint(self, operation_id: str, stage: str):
        await asyncio.sleep(0)

    def begin_control(self, slot_id: str, action: str) -> dict:
        if not isinstance(action, str) or action not in {"start", "stop", "restart", "remove"}:
            raise HostError("invalid_control")
        slot = self.registry.get_slot(slot_id)
        if self.locks.setdefault(slot_id, asyncio.Lock()).locked():
            raise HostError("operation_in_progress", 409)
        retry = next((operation for operation in self.registry.list_unfinished_operations()
                      if action == "remove" and operation.slot_id == slot_id and operation.action == "remove"
                      and operation.status == "recovery_required"), None)
        self.check_mutation(slot, operation_id=retry.id if retry else None, allow_exposed_stop=action == "stop")
        operation = (self.registry.update_operation(retry.id, "running") if retry else
                     self.registry.create_operation(action, slot_id=slot_id, generation=slot.generation))
        task = asyncio.create_task(self.execute_control(operation))
        task.operation_id = operation.id
        self.track(task)
        return operation.to_dict()

    async def execute_control(self, operation):
        self.registry.update_operation(operation.id, "running")
        try:
            await self.control(operation.slot_id, operation.action, operation_id=operation.id)
            if operation.action != "remove":
                self.registry.update_operation(operation.id, "completed")
        except Exception as error:
            code = error.code if isinstance(error, (HostError, PackageError, RegistryError, SettingsError)) else (
                "removal_failed" if operation.action == "remove" else "control_failed")
            if operation.action == "remove":
                with contextlib.suppress(RegistryError):
                    slot = self.registry.get_slot(operation.slot_id)
                    if slot.observed_state == "ownership_lost" or any(
                            process.ownership_lost for process in self.owner(slot.id).instances.values()):
                        code = "ownership_lost"
                    self.registry.update_slot(slot.id, desired_state="stopped", wake_suppressed=True,
                                              observed_state="ownership_lost" if code == "ownership_lost" else "failed",
                                              error_code=code)
            # Registry deletion and operation completion are one transaction.
            # Cleanup after that point must not rewrite a completed result.
            if self.registry.get_operation(operation.id).status != "completed":
                self.registry.update_operation(operation.id, "recovery_required" if operation.action == "remove" or code == "ownership_lost"
                                               else "failed", error_code=code)

    async def close(self):
        self.closing = True
        # Downloads own network and extraction work independently of callers.
        # Keep the state lock until every owned writer has settled.
        download_error = None
        try:
            await self.downloads.close()
        except PrivateStateError as error:
            # The downloader reports this only after its writer barrier. Finish
            # app/process cleanup before reporting failed journal persistence.
            download_error = error
        if self.static_tasks:
            # Canceling an asyncio wrapper does not stop its underlying thread.
            # Shield each owned task even if shutdown itself is canceled, so a
            # retry retains the database and owner lock until file work settles.
            await asyncio.gather(*(asyncio.shield(task) for task in list(self.static_tasks)),
                                 return_exceptions=True)
        if self.tasks:
            pending = list(self.tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for slot_id in list(self.workers):
            with contextlib.suppress(Exception):
                await self.stop_gateway(slot_id)
        for slot_id in list(self.residents):
            with contextlib.suppress(Exception):
                await self.stop_residents(slot_id)
        for slot_id, owner in self.owners.items():
            for process in list(owner.instances.values()):
                if not process.ownership_lost:
                    with contextlib.suppress(Exception):
                        await self.reap(slot_id, process)
            with contextlib.suppress(Exception):
                await owner.close()
        for listener in [*self.listeners.values(), *self.plan_listeners.values()]:
            listener.close()
        for descriptor in self.legacy_locks.values():
            os.close(descriptor)
        self.legacy_locks.clear()
        self.imports.close()
        self.registry.close()
        os.close(self.lock_fd)
        if download_error is not None:
            raise download_error


def management_app(host: Supervisor, token: str, port: int):
    from .policy import AuthenticationError, LocalPolicy, load_json

    def authenticate(authorization: str):
        if hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            return {"owner": True}
        if not authorization.startswith("Bearer ") or len(authorization) > 128:
            raise AuthenticationError("unauthorized")
        try:
            return host.grants.authenticate(authorization[7:])
        except GrantError as error:
            raise AuthenticationError(error.code) from None

    async def dispatch(request: Request):
        try:
            route = request.url.path
            grant_id = request.scope["mcpme.principal"].get("grant_id")
            payload = None
            if request.method != "GET":
                payload = load_json(await request.body())
                if not isinstance(payload, dict):
                    raise HostError("invalid_request")
            if grant_id is not None:
                if (route.startswith(("/v1/recipes", "/v1/preparations", "/v1/downloads", "/v1/secrets", "/v1/upgrade"))
                        or route.startswith("/v1/operations/") and request.method != "GET"):
                    # Existing metadata/attachment grants retain their scope.
                    # This foundation introduces no scoped execution authority.
                    raise GrantError("grant_scope", 403)
                host.grants.authorize_route(grant_id, request.method, route, payload)
            if request.method == "GET":
                if route == "/v1/capabilities":
                    result = capabilities()
                elif route == "/v1/recipes":
                    result = {"recipes": host.recipes.list_summaries()}
                elif route == "/v1/downloads":
                    result = {"downloads": host.downloads.list(), "count": host.downloads.count()}
                elif route == "/v1/secrets":
                    result = {"secrets": host.secrets.list(), "count": host.secrets.count()}
                elif route.startswith("/v1/secrets/"):
                    result = host.secrets.get(request.path_params["secret_id"])
                elif route.startswith("/v1/downloads/"):
                    result = host.downloads.get(request.path_params["download_id"])
                elif route == "/v1/preparations":
                    result = {"preparations": host.preparations.list_summaries()}
                elif route.startswith("/v1/preparations/"):
                    result = host.preparations.get(request.path_params["job_id"])
                elif route == "/v1/status":
                    result = {"supervisor_pid": os.getpid(), "slots": [host.slot_snapshot(slot) for slot in host.registry.list_slots()],
                              "operations": [host.operation_snapshot(operation) for operation in host.registry.list_operations()],
                              "resource_policy": host.resource_policy,
                              "reservations": {slot_id: dict(value) for slot_id, value in host.reservations.items()}}
                elif route == "/v1/slots":
                    result = {"slots": [host.slot_snapshot(slot) for slot in host.registry.list_slots()]}
                    if grant_id is not None:
                        result["slots"] = host.grants.filter_slots(grant_id, result["slots"])
                elif route == "/v1/grants":
                    result = host.grants.list()
                elif route.startswith("/v1/grants/"):
                    result = host.grants.inspect(request.path_params["grant_id"])
                elif route.startswith("/v1/plans/"):
                    result = await asyncio.to_thread(host.inspect_plan, request.path_params["plan_id"])
                elif route.startswith("/v1/upgrade/plans/"):
                    result = await host.static_work(host.inspect_upgrade_plan, request.path_params["plan_id"])
                elif route == "/v1/imports":
                    host.reconcile_imports()
                    result = {"imports": host.imports.list_summaries()}
                elif route.startswith("/v1/imports/"):
                    host.reconcile_imports()
                    result = host.imports.inspect(request.path_params["draft_id"])
                elif route.endswith("/details"):
                    slot = host.registry.get_slot(request.path_params["slot_id"])
                    package = await asyncio.to_thread(host.package, slot)
                    result = {"slot": host.slot_snapshot(slot), "package": package.manifest.model_dump(),
                              "settings": host.package_settings(slot, package),
                              "runtime_metrics": await host.runtime_metrics(slot, package),
                              "processes": [{"process_id": resident.process_id, "instance_id": resident.process.instance_id,
                                             "pid": resident.process.pid, "reaper_pid": resident.process.reaper_pid,
                                             "endpoint": resident.endpoint, "generation": resident.generation,
                                             "ownership_lost": resident.process.ownership_lost}
                                            for resident in host.residents.get(slot.id, {}).values()]}
                elif route.startswith("/v1/migrations/"):
                    from .migrations import inspect_transfer
                    result = inspect_transfer(host, request.path_params["transfer_id"])
                elif route.startswith("/v1/slots/"):
                    result = host.slot_snapshot(host.registry.get_slot(request.path_params["slot_id"]))
                else:
                    result = host.operation_snapshot(host.registry.get_operation(request.path_params["operation_id"]))
            else:
                if route == "/v1/upgrade/plan":
                    result = await host.plan_upgrade(payload)
                elif route == "/v1/upgrade/apply" and set(payload) == {"plan_id"}:
                    result = await host.apply_upgrade(payload["plan_id"])
                elif route.startswith("/v1/operations/"):
                    if payload or request.path_params["action"] not in {"cancel", "recover"}:
                        raise HostError("invalid_operation_request")
                    result = await getattr(host, request.path_params["action"] + "_operation")(request.path_params["operation_id"])
                elif route == "/v1/secrets":
                    if set(payload) != {"name", "value"}:
                        raise SettingsError("invalid_secret")
                    result = host.secrets.create(payload["name"], payload["value"])
                elif route.startswith("/v1/secrets/"):
                    if payload:
                        raise SettingsError("invalid_secret")
                    result = host.secrets.revoke(request.path_params["secret_id"])
                elif route == "/v1/downloads":
                    result = await host.downloads.start(payload)
                elif route.startswith("/v1/downloads/"):
                    if payload:
                        raise HostError("invalid_download_spec")
                    result = await host.downloads.cancel(request.path_params["download_id"])
                elif route == "/v1/recipes":
                    if set(payload) != {"path"} or not isinstance(payload["path"], str) or not Path(payload["path"]).is_absolute():
                        raise HostError("invalid_recipe_request")
                    result = await host.static_work(host.recipes.add, Path(payload["path"]))
                elif route.startswith("/v1/recipes/"):
                    action = request.path_params["action"]
                    if (action not in {"inspect", "enable", "disable", "revoke", "remove", "deprecate"}
                            or set(payload) != {"recipe_id", "version"}):
                        raise HostError("invalid_recipe_request")
                    result = await host.static_work(getattr(host.recipes, action), payload["recipe_id"], payload["version"])
                elif route == "/v1/preparations/plan":
                    if set(payload) != {"import_id", "revision", "descriptor"}:
                        raise HostError("invalid_preparation_request")
                    result = await host.static_work(host.preparations.plan, payload["import_id"], payload["revision"], payload["descriptor"])
                elif route.endswith("/cancel") and "job_id" in request.path_params:
                    if payload:
                        raise HostError("invalid_preparation_request")
                    result = host.preparations.cancel(request.path_params["job_id"])
                elif route == "/v1/grants":
                    plan = None
                    if payload.get("mode") == "install":
                        plan_id = payload.get("plan_id")
                        if not isinstance(plan_id, str) or str(uuid.UUID(plan_id)) != plan_id:
                            raise HostError("invalid_plan")
                        plan = read_private_json(host.root / "plans" / (plan_id + ".json"))
                        if "import_id" in plan:
                            host.reconcile_imports()
                            host.imports.validate_binding(plan["import_id"], plan["import_revision"], plan["import_digest"])
                    result = host.grants.issue(payload, plan=plan)
                elif route.endswith("/revoke") and "grant_id" in request.path_params and not payload:
                    result = host.grants.revoke(request.path_params["grant_id"])
                elif request.method == "PATCH" and "slot_id" in request.path_params:
                    result = await host.update_settings(request.path_params["slot_id"], payload)
                elif route == "/v1/imports":
                    if set(payload) - {"source", "name", "intent", "document_role", "files", "start", "start_policy", "model_engine"}:
                        raise HostError("invalid_import_request")
                    if not isinstance(payload.get("source"), str) or not isinstance(payload.get("name"), str):
                        raise HostError("invalid_import_request")
                    settings = {key: value for key, value in payload.items() if key != "source"}
                    if "model_engine" in settings:
                        if not isinstance(settings["model_engine"], str) or not Path(settings["model_engine"]).is_absolute():
                            raise HostError("invalid_import_request")
                        settings["model_engine"] = Path(settings["model_engine"])
                    # ImportStore is synchronous so intake cannot race revision,
                    # cancel, or apply. It never executes the selected source.
                    result = await host.static_work(lambda: host.imports.create(Path(payload["source"]), **settings))
                elif "draft_id" in request.path_params:
                    host.reconcile_imports()
                    draft_id = request.path_params["draft_id"]
                    action = request.path_params["action"]
                    revision = payload.get("revision")
                    if action == "revise":
                        if "revision" not in payload:
                            raise HostError("invalid_import_request")
                        result = host.imports.revise(draft_id, revision, **{key: value for key, value in payload.items() if key != "revision"})
                        host.invalidate_import_plans(draft_id)
                    elif action == "attach" and set(payload) == {"revision", "package"} and isinstance(payload["package"], str):
                        result = await host.static_work(host.imports.attach, draft_id, revision, Path(payload["package"]))
                    elif action == "engine" and set(payload) == {"revision", "model_engine"} and isinstance(payload["model_engine"], str):
                        if not Path(payload["model_engine"]).is_absolute():
                            raise HostError("invalid_import_request")
                        result = await host.static_work(host.imports.add_engine, draft_id, revision, Path(payload["model_engine"]))
                        host.invalidate_import_plans(draft_id)
                    elif action in {"export", "cancel", "expire"} and set(payload) == {"revision"}:
                        result = getattr(host.imports, action)(draft_id, revision)
                        if action in {"cancel", "expire"}:
                            host.invalidate_import_plans(draft_id)
                    else:
                        raise HostError("invalid_import_request")
                elif route == "/v1/install/plan":
                    result = await host.plan_install_async(payload)
                elif route == "/v1/migrations/jevk5/plan":
                    from .migrations import plan_transfer
                    result = await plan_transfer(host, payload)
                elif route == "/v1/migrations/jevk5/apply" and set(payload) == {"transfer_id"}:
                    from .migrations import apply_transfer
                    result = await apply_transfer(host, payload["transfer_id"])
                elif route.endswith("/rollback") and "transfer_id" in request.path_params and not payload:
                    from .migrations import rollback_transfer
                    result = await rollback_transfer(host, request.path_params["transfer_id"])
                elif route.endswith("/recover") and "transfer_id" in request.path_params and not payload:
                    from .migrations import recover_transfer
                    result = await recover_transfer(host, request.path_params["transfer_id"])
                elif route == "/v1/install/apply" and set(payload) == {"plan_id"}:
                    result = await host.apply_install(payload["plan_id"], grant_id=grant_id)
                elif "slot_id" in request.path_params and not payload:
                    action = request.path_params["action"]
                    if action not in {"start", "stop", "restart", "remove"}:
                        raise HostError("invalid_control")
                    result = host.begin_control(request.path_params["slot_id"], action)
                else:
                    raise HostError("invalid_request")
            return JSONResponse({"api_version": 1, **result})
        except (HostError, PackageError, RegistryError, ImportDraftError, GrantError, PreparationError, DownloadError, SettingsError, UpgradeError) as error:
            return JSONResponse({"api_version": 1, "error": {"code": error.code}}, status_code=getattr(error, "status", 400))
        except Exception:
            return JSONResponse({"api_version": 1, "error": {"code": "invalid_request"}}, status_code=400)

    app = Starlette(routes=[Route("/v1/capabilities", dispatch), Route("/v1/status", dispatch), Route("/v1/slots", dispatch),
        Route("/v1/downloads", dispatch, methods=["GET", "POST"]),
        Route("/v1/downloads/{download_id}", dispatch),
        Route("/v1/downloads/{download_id}/cancel", dispatch, methods=["POST"]),
        Route("/v1/secrets", dispatch, methods=["GET", "POST"]),
        Route("/v1/secrets/{secret_id}", dispatch),
        Route("/v1/secrets/{secret_id}/revoke", dispatch, methods=["POST"]),
        Route("/v1/recipes", dispatch, methods=["GET", "POST"]),
        Route("/v1/recipes/{action}", dispatch, methods=["POST"]),
        Route("/v1/preparations", dispatch),
        Route("/v1/preparations/plan", dispatch, methods=["POST"]),
        Route("/v1/preparations/{job_id}", dispatch),
        Route("/v1/preparations/{job_id}/cancel", dispatch, methods=["POST"]),
        Route("/v1/grants", dispatch, methods=["GET", "POST"]),
        Route("/v1/grants/{grant_id}", dispatch), Route("/v1/grants/{grant_id}/revoke", dispatch, methods=["POST"]),
        Route("/v1/plans/{plan_id}", dispatch),
        Route("/v1/upgrade/plan", dispatch, methods=["POST"]),
        Route("/v1/upgrade/plans/{plan_id}", dispatch),
        Route("/v1/upgrade/apply", dispatch, methods=["POST"]),
        Route("/v1/slots/{slot_id}/details", dispatch),
        Route("/v1/slots/{slot_id}", dispatch, methods=["GET", "PATCH"]), Route("/v1/operations/{operation_id}", dispatch),
        Route("/v1/operations/{operation_id}/{action}", dispatch, methods=["POST"]),
        Route("/v1/imports", dispatch, methods=["GET", "POST"]),
        Route("/v1/imports/{draft_id}", dispatch),
        Route("/v1/imports/{draft_id}/{action}", dispatch, methods=["POST"]),
        Route("/v1/install/plan", dispatch, methods=["POST"]), Route("/v1/install/apply", dispatch, methods=["POST"]),
        Route("/v1/migrations/jevk5/plan", dispatch, methods=["POST"]),
        Route("/v1/migrations/jevk5/apply", dispatch, methods=["POST"]), Route("/v1/migrations/{transfer_id}", dispatch),
        Route("/v1/migrations/{transfer_id}/rollback", dispatch, methods=["POST"]),
        Route("/v1/migrations/{transfer_id}/recover", dispatch, methods=["POST"]),
        Route("/v1/slots/{slot_id}/{action}", dispatch, methods=["POST"])])
    return LocalPolicy(app, token=token, port=port, authenticate=authenticate)


async def serve(state_dir: Path, reaper: Path, parent_pid: int | None = None):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or int(platform.mac_ver()[0].split(".")[0]) < 26:
        raise HostError("unsupported_platform")
    host = Supervisor(state_dir, reaper)
    listener = bind_listener()
    token = secrets.token_urlsafe(32)
    port = listener.getsockname()[1]
    server = HostServer(uvicorn.Config(management_app(host, token, port), access_log=False,
        log_config=None, log_level="critical", timeout_graceful_shutdown=5))
    watchdog = None
    private_listener = None
    try:
        private_listener = bind_private_listener(host.root, "manager")
        await host.initialize()
        write_private_json(host.root / "manager-connection.json", {"api_version": 1,
            "endpoint": f"http://127.0.0.1:{port}", "token": token, "supervisor_pid": os.getpid()})
        if parent_pid is not None:
            async def watch():
                while not server.should_exit:
                    if os.getppid() != parent_pid:
                        server.should_exit = True
                    await asyncio.sleep(0.2)
            watchdog = asyncio.create_task(watch())
        await server.serve(sockets=[listener, private_listener])
    finally:
        if watchdog:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        (host.root / "manager-connection.json").unlink(missing_ok=True)
        await host.close()
        listener.close()
        if private_listener:
            private_listener.close()
