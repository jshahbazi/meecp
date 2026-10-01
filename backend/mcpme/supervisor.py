"""Owner-only management service and isolated deterministic slot supervision."""
from __future__ import annotations

import array
import asyncio
import contextlib
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import sys
import uuid

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .contracts import PackageError, validate_package
from .private import ensure_private_directory, read_private_json, snapshot_package, write_private_json
from .processes import OwnedProcess, ProcessOwner
from .registry import Registry, RegistryError


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
            "profiles": ["mcp-stdio"], "protocol_versions": ["2025-11-25"], "capabilities": ["tools"],
            "start_policies": ["manual", "host"], "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "limits": {"slots": 16, "request_bytes": 1048576, "idle_seconds": 300},
            "installation": "prepared_local_packages", "management_credentials": "local_owner",
            "unsupported": ["mcp-http", "model-service", "first_call", "upgrades", "downloads", "imports", "scoped_grants"]}


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


class Supervisor:
    def __init__(self, state_dir: Path, reaper: Path):
        self.root = ensure_private_directory(state_dir.absolute())
        self.lock_fd = os.open(self.root / "supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise HostError("host_already_running", 409) from None
        self.registry = Registry(self.root)
        self.reaper = reaper.absolute()
        self.listeners: dict[str, socket.socket] = {}
        self.plan_listeners: dict[str, socket.socket] = {}
        self.workers: dict[str, Worker] = {}
        self.owners: dict[str, ProcessOwner] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.ledgers: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.install_lock = asyncio.Lock()
        self.closing = False

    def slot_directory(self, slot_id: str) -> Path:
        return ensure_private_directory(self.root / "slots" / slot_id)

    def ledger(self, slot_id: str) -> dict:
        if slot_id not in self.ledgers:
            path = self.slot_directory(slot_id) / "ownership.json"
            self.ledgers[slot_id] = read_private_json(path) if path.exists() else {}
        return self.ledgers[slot_id]

    def save_ledger(self, slot_id: str):
        write_private_json(self.slot_directory(slot_id) / "ownership.json", self.ledger(slot_id))

    def owner(self, slot_id: str) -> ProcessOwner:
        if slot_id not in self.owners:
            async def lost(process):
                self.ledger(slot_id)[process.instance_id] = {"state": "ownership_lost"}
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
        for process in list(self.owner(slot_id).instances.values()):
            if not process.ownership_lost:
                with contextlib.suppress(Exception):
                    await self.reap(slot_id, process)

    async def launch(self, slot_id: str, **kwargs) -> OwnedProcess:
        identifier = uuid.uuid4().hex
        self.ledger(slot_id)[identifier] = {"state": "launching"}
        self.save_ledger(slot_id)
        try:
            process = await self.owner(slot_id).launch(instance_id=identifier, **kwargs)
        except BaseException:
            process = self.owner(slot_id).instances.get(identifier)
            if process is None:
                self.ledger(slot_id).pop(identifier, None)
                self.save_ledger(slot_id)
            raise
        self.ledger(slot_id)[identifier] = {"state": "owned"}
        self.save_ledger(slot_id)
        return process

    async def reap(self, slot_id: str, process: OwnedProcess):
        await process.stop()
        self.ledger(slot_id).pop(process.instance_id, None)
        self.save_ledger(slot_id)

    def connection(self, slot_id: str) -> dict:
        return read_private_json(self.slot_directory(slot_id) / "connection.json")

    def package(self, slot):
        package = validate_package(Path(slot.package_path))
        if package.digest != slot.package_digest:
            raise HostError("package_digest_mismatch")
        return package

    async def package_process(self, slot, *, io_mode="streams") -> OwnedProcess:
        package = self.package(slot)
        entry = package.manifest.entrypoint
        data = ensure_private_directory(self.slot_directory(slot.id) / "data")
        runtime = ensure_private_directory(self.slot_directory(slot.id) / "runtime")
        return await self.launch(slot.id,
            executable=package.root / entry.executable, args=entry.args, cwd=package.root / entry.cwd,
            env={"PATH": "/usr/bin:/bin", "HOME": str(data), "TMPDIR": str(runtime), "LANG": "en_US.UTF-8"},
            io_mode=io_mode, startup_timeout=min(package.manifest.lifecycle.startup_timeout_seconds, 120),
            stop_grace=package.manifest.lifecycle.stop_grace_seconds)

    async def check_candidate(self, slot):
        package = self.package(slot)
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
        finally:
            await self.reap(slot.id, process)

    async def start_gateway(self, slot, active: bool):
        package = self.package(slot)
        lifecycle = package.manifest.lifecycle
        listener = self.listeners[slot.id]
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        parent.setblocking(False)
        config_read, config_write = os.pipe()
        config = {"token": self.connection(slot.id)["token"], "port": slot.public_port, "active": active,
                  "max_sessions": lifecycle.max_sessions, "idle_timeout": lifecycle.idle_timeout_seconds,
                  "request_timeout": lifecycle.operation_timeout_seconds,
                  "control_timeout": min(lifecycle.startup_timeout_seconds, 120) + lifecycle.stop_grace_seconds + 5}
        os.write(config_write, json.dumps(config).encode())
        os.close(config_write)
        host_paths = [str(Path(__file__).resolve().parents[1])]
        bundled_dependencies = Path(sys.executable).parent.parent / "site-packages"
        if bundled_dependencies.is_dir():
            host_paths.append(str(bundled_dependencies))
        try:
            process = await self.launch(slot.id, executable=Path(sys.executable),
                args=["-m", "mcpme.worker", "--config-fd", str(config_read), "--listener-fd", str(listener.fileno()),
                      "--channel-fd", str(child.fileno())], cwd=self.root,
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": os.pathsep.join(host_paths),
                     "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                inherited_fds=(config_read, listener.fileno(), child.fileno()), io_mode="fds", stop_grace=3)
            process.close_stdio()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
            os.close(config_read)
        worker = Worker(process, parent)
        self.workers[slot.id] = worker
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
                    if set(request) == {"action", "session_id"} and request["action"] == "launch_session":
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
            self.ledger(slot_id).pop(worker.process.instance_id, None)
            self.save_ledger(slot_id)
        except Exception:
            pass
        with contextlib.suppress(OSError):
            worker.channel.shutdown(socket.SHUT_RDWR)
        if worker.service:
            if not worker.service.cancelling():
                worker.service.cancel()
            await asyncio.gather(worker.service, return_exceptions=True)
        if not worker.closing:
            code = "ownership_lost" if worker.process.ownership_lost or self.registry.get_slot(slot_id).error_code == "ownership_lost" else "gateway_failed"
            self.registry.update_slot(slot_id, observed_state="ownership_lost" if code == "ownership_lost" else "failed", error_code=code)

    async def stop_gateway(self, slot_id: str):
        worker = self.workers.pop(slot_id, None)
        if worker is None:
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

    async def control(self, slot_id: str, action: str):
        async with self.locks.setdefault(slot_id, asyncio.Lock()):
            slot = self.registry.get_slot(slot_id)
            if any(op.slot_id == slot_id and op.action == "install" for op in self.registry.list_unfinished_operations()):
                raise HostError("installation_recovery_required" if slot.error_code == "installation_recovery_required" else "operation_in_progress", 409)
            if slot.error_code == "installation_recovery_required":
                raise HostError("installation_recovery_required", 409)
            if slot.error_code == "ownership_lost" or any(v.get("state") == "ownership_lost" for v in self.ledger(slot_id).values()):
                raise HostError("ownership_lost", 409)
            if slot_id not in self.listeners:
                self.listeners[slot_id] = bind_listener(slot.public_port)
            worker = self.workers.get(slot_id)
            if (action == "start" and slot.observed_state == "ready" and worker is not None
                    and not worker.closing and worker.process.returncode is None and not worker.process.ownership_lost):
                return asdict(slot)
            self.registry.update_slot(slot_id, observed_state="draining")
            await self.stop_gateway(slot_id)
            slot = self.registry.update_slot(slot_id, generation=slot.generation + 1, error_code=None,
                desired_state="stopped" if action == "stop" else "running", observed_state="stopped" if action == "stop" else "starting",
                wake_suppressed=action == "stop")
            try:
                if action != "stop":
                    await self.check_candidate(slot)
                    write_private_json(self.slot_directory(slot_id) / "activation.json", {
                        "generation": slot.generation, "verified": True, "exposure_permitted": True})
                await self.start_gateway(slot, active=action != "stop")
                slot = self.registry.update_slot(slot_id, observed_state="stopped" if action == "stop" else "ready")
            except Exception as error:
                code = error.code if isinstance(error, (HostError, PackageError)) else "start_failed"
                if self.registry.get_slot(slot_id).error_code == "ownership_lost":
                    code = "ownership_lost"
                self.registry.update_slot(slot_id, observed_state="ownership_lost" if code == "ownership_lost" else "failed", error_code=code)
                raise HostError(code, 409) from None
            return asdict(slot)

    async def initialize(self):
        interrupted = set()
        for operation in self.registry.list_unfinished_operations():
            if operation.status in {"pending", "running"}:
                self.registry.update_operation(operation.id, "recovery_required", error_code="operation_interrupted")
            if operation.status in {"pending", "running", "recovery_required"} and operation.action == "install":
                interrupted.add(operation.slot_id)
        for slot in self.registry.list_slots():
            if self.ledger(slot.id):
                self.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
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

    def plan_install(self, payload: dict) -> dict:
        if set(payload) - {"package", "name", "start_policy", "start"} or not isinstance(payload.get("package"), str):
            raise HostError("invalid_install_plan")
        if len(self.registry.list_slots()) >= 16 or len(self.plan_listeners) >= 32:
            raise HostError("capacity_exceeded", 409)
        start_policy = payload.get("start_policy", "manual")
        if start_policy not in {"manual", "host"} or type(payload.get("start", False)) is not bool:
            raise HostError("unsupported_start_policy")
        package = validate_package(Path(payload["package"]))
        name = payload.get("name", package.manifest.name)
        if not isinstance(name, str) or not 1 <= len(name) <= 256 or any(ord(c) < 32 for c in name):
            raise HostError("invalid_slot_name")
        plan_id, slot_id = str(uuid.uuid4()), str(uuid.uuid4())
        snapshot = snapshot_package(package, self.root / "staging" / plan_id / "package")
        listener = preferred_listener()
        plan = {"plan_id": plan_id, "slot_id": slot_id, "package_digest": snapshot.digest,
                "package_path": str(snapshot.root), "name": name, "start_policy": start_policy,
                "start": payload.get("start", False), "public_port": listener.getsockname()[1]}
        try:
            write_private_json(self.root / "plans" / (plan_id + ".json"), plan)
        except BaseException:
            listener.close()
            raise
        self.plan_listeners[plan_id] = listener
        return {**plan, "package": package.manifest.model_dump(),
                "installed_bytes": sum(artifact.size for artifact in package.lock.artifacts),
                "publisher_trust": "local_owner", "access_enforcement": "declarations_only"}

    async def apply_install(self, plan_id: str) -> dict:
        if str(uuid.UUID(plan_id)) != plan_id:
            raise HostError("invalid_plan")
        async with self.install_lock:
            plan = read_private_json(self.root / "plans" / (plan_id + ".json"))
            operation = self.registry.create_operation("install", slot_id=plan["slot_id"],
                idempotency_key=plan_id, details={"plan_id": plan_id, "package_digest": plan["package_digest"],
                                                "package_path": plan["package_path"]})
            # A persisted pending operation belongs to this apply; in-process task
            # registration happens before yielding, so concurrent retries coalesce.
            if operation.status == "pending" and not any(getattr(task, "operation_id", None) == operation.id for task in self.tasks):
                task = asyncio.create_task(self.install(operation.id, plan))
                task.operation_id = operation.id
                self.track(task)
            return operation.to_dict()

    async def install(self, operation_id: str, plan: dict):
        slot_id = plan["slot_id"]
        listener = self.plan_listeners.pop(plan["plan_id"], None)
        self.registry.update_operation(operation_id, "running")
        created = False
        exposure_permitted = False
        try:
            if len(self.registry.list_slots()) >= 16:
                raise HostError("capacity_exceeded")
            if listener is None:
                listener = bind_listener(plan["public_port"])
            package = validate_package(Path(plan["package_path"]))
            if package.digest != plan["package_digest"]:
                raise HostError("package_digest_mismatch")
            package = snapshot_package(package, self.root / "packages" / package.manifest.package_id / package.digest)
            directory = self.slot_directory(slot_id)
            write_private_json(directory / "connection.json", {
                "slot_id": slot_id, "endpoint": f"http://127.0.0.1:{plan['public_port']}/mcp", "token": secrets.token_urlsafe(32)})
            slot = self.registry.add_slot(slot_id=slot_id, package_digest=package.digest, package_path=package.root,
                name=plan["name"], public_port=plan["public_port"], start_policy=plan["start_policy"],
                credential_ref=directory / "connection.json")
            created = True
            self.listeners[slot_id] = listener
            self.registry.update_slot(slot_id, observed_state="starting")
            await self.check_candidate(slot)
            # Ordinary admission is opened only after the verified candidate is
            # durably selected. Install has no previous data generation to restore.
            write_private_json(directory / "activation.json", {"generation": slot.generation, "verified": True,
                                                               "exposure_permitted": plan["start"]})
            exposure_permitted = plan["start"]
            await self.start_gateway(slot, active=plan["start"])
            self.registry.update_slot(slot_id, observed_state="ready" if plan["start"] else "stopped",
                                      desired_state="running" if plan["start"] else "stopped")
            self.registry.update_operation(operation_id, "completed")
        except Exception as error:
            code = error.code if isinstance(error, (HostError, PackageError, RegistryError)) else "installation_failed"
            with contextlib.suppress(Exception):
                await self.stop_gateway(slot_id)
            ownership_lost = bool(self.ledger(slot_id))
            if exposure_permitted and not ownership_lost:
                code = "installation_recovery_required"
            if created:
                self.registry.update_slot(slot_id, observed_state="ownership_lost" if ownership_lost else "failed",
                                          error_code="ownership_lost" if ownership_lost else code)
                if not ownership_lost and not exposure_permitted:
                    self.registry.update_slot(slot_id, desired_state="stopped")
                    self.registry.discard_slot(slot_id)
            if listener is not None and not ownership_lost and not exposure_permitted:
                listener.close()
                self.listeners.pop(slot_id, None)
            self.registry.update_operation(operation_id, "recovery_required" if ownership_lost or exposure_permitted else "failed",
                                           error_code="ownership_lost" if ownership_lost else code)

    def begin_control(self, slot_id: str, action: str) -> dict:
        slot = self.registry.get_slot(slot_id)
        operation = self.registry.create_operation(action, slot_id=slot_id, generation=slot.generation)
        async def execute():
            self.registry.update_operation(operation.id, "running")
            try:
                await self.control(slot_id, action)
                self.registry.update_operation(operation.id, "completed")
            except Exception as error:
                code = error.code if isinstance(error, (HostError, PackageError, RegistryError)) else "control_failed"
                self.registry.update_operation(operation.id, "failed", error_code=code)
        task = asyncio.create_task(execute())
        self.track(task)
        return operation.to_dict()

    async def close(self):
        self.closing = True
        if self.tasks:
            pending = list(self.tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for slot_id in list(self.workers):
            with contextlib.suppress(Exception):
                await self.stop_gateway(slot_id)
        for slot_id, owner in self.owners.items():
            for process in list(owner.instances.values()):
                if not process.ownership_lost:
                    with contextlib.suppress(Exception):
                        await self.reap(slot_id, process)
            with contextlib.suppress(Exception):
                await owner.close()
        for listener in [*self.listeners.values(), *self.plan_listeners.values()]:
            listener.close()
        self.registry.close()
        os.close(self.lock_fd)


def management_app(host: Supervisor, token: str, port: int):
    from .policy import LocalPolicy

    async def dispatch(request: Request):
        try:
            route = request.url.path
            if request.method == "GET":
                if route == "/v1/capabilities":
                    result = capabilities()
                elif route == "/v1/slots":
                    result = {"slots": [slot.to_dict() for slot in host.registry.list_slots()]}
                elif route.startswith("/v1/slots/"):
                    result = host.registry.get_slot(request.path_params["slot_id"]).to_dict()
                else:
                    result = host.registry.get_operation(request.path_params["operation_id"]).to_dict()
            else:
                payload = await request.json()
                if not isinstance(payload, dict):
                    raise HostError("invalid_request")
                if route == "/v1/install/plan":
                    result = host.plan_install(payload)
                elif route == "/v1/install/apply" and set(payload) == {"plan_id"}:
                    result = await host.apply_install(payload["plan_id"])
                elif "slot_id" in request.path_params and not payload:
                    action = request.path_params["action"]
                    if action not in {"start", "stop", "restart"}:
                        raise HostError("invalid_control")
                    result = host.begin_control(request.path_params["slot_id"], action)
                else:
                    raise HostError("invalid_request")
            return JSONResponse({"api_version": 1, **result})
        except (HostError, PackageError, RegistryError) as error:
            return JSONResponse({"api_version": 1, "error": {"code": error.code}}, status_code=getattr(error, "status", 400))
        except Exception:
            return JSONResponse({"api_version": 1, "error": {"code": "invalid_request"}}, status_code=400)

    app = Starlette(routes=[Route("/v1/capabilities", dispatch), Route("/v1/slots", dispatch),
        Route("/v1/slots/{slot_id}", dispatch), Route("/v1/operations/{operation_id}", dispatch),
        Route("/v1/install/plan", dispatch, methods=["POST"]), Route("/v1/install/apply", dispatch, methods=["POST"]),
        Route("/v1/slots/{slot_id}/{action}", dispatch, methods=["POST"])])
    return LocalPolicy(app, token=token, port=port)


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
    try:
        await host.initialize()
        write_private_json(host.root / "manager-connection.json", {"api_version": 1,
            "endpoint": f"http://127.0.0.1:{port}", "token": token})
        if parent_pid is not None:
            async def watch():
                while not server.should_exit:
                    if os.getppid() != parent_pid:
                        server.should_exit = True
                    await asyncio.sleep(0.2)
            watchdog = asyncio.create_task(watch())
        await server.serve(sockets=[listener])
    finally:
        if watchdog:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        (host.root / "manager-connection.json").unlink(missing_ok=True)
        await host.close()
        listener.close()
