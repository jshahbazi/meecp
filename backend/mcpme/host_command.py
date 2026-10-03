"""Explicitly admitted host-command sessions; no selected Python code is imported.

Only immutable identities and reservation metadata are durable. Native ownership
proof uses format 3 independently of gateway/legacy process ownership receipts.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import secrets
import time

from jsonschema import Draft202012Validator
from mcp.types import JSONRPCMessage

from .command_execution_v2 import CommandExecutorV2, CommandExecutionError, CommandSpecV2, _digest, _hex, _join
from .gateway import MAX_MESSAGE_BYTES, encode, error_message, request_id_digest
from .policy import load_json
from .private import ensure_private_directory, read_private_json, write_private_json


@dataclass(frozen=True)
class HostCommandAdmission:
    helper: Path
    helper_sha256: str


class HostCommands:
    def __init__(self, host, admission: HostCommandAdmission | None):
        self.host, self.admission = host, admission
        self.executors = {}
        self.records = {}
        self.live = {}
        self.sessions = {}
        self.counts = {}
        self.stopping = set()

    def path(self, slot_id):
        return self.host.slot_directory(slot_id) / "command-reservations.json"

    def load(self, slot_id):
        if slot_id in self.records:
            return
        path = self.path(slot_id)
        value = read_private_json(path) if path.exists() else {"version": 1, "entries": []}
        if type(value) is not dict or set(value) != {"version", "entries"} or type(value["version"]) is not int or value["version"] != 1 or type(value["entries"]) is not list or len(value["entries"]) > 256:
            raise CommandExecutionError("invalid_command_reservations")
        seen = set()
        for item in value["entries"]:
            if (type(item) is not dict or set(item) != {"instance", "authority", "spec", "package", "generation", "memory_mb"}
                    or not _hex(item["instance"], 32) or item["instance"] in seen
                    or any(not _hex(item[key]) for key in ("authority", "spec", "package"))
                    or type(item["generation"]) is not int or item["generation"] < 1
                    or type(item["memory_mb"]) is not int or not 1 <= item["memory_mb"] <= 262144):
                raise CommandExecutionError("invalid_command_reservations")
            seen.add(item["instance"])
        self.records[slot_id] = value["entries"]

    def save(self, slot_id):
        write_private_json(self.path(slot_id), {"version": 1, "entries": self.records[slot_id]})

    def executor(self, slot_id):
        if self.admission is None:
            raise CommandExecutionError("host_command_not_admitted")
        if slot_id not in self.executors:
            self.executors[slot_id] = CommandExecutorV2(self.admission.helper, self.admission.helper_sha256,
                ensure_private_directory(self.host.slot_directory(slot_id) / "command-v3"), admit=self.admit)
        return self.executors[slot_id]

    def pending(self, slot_id):
        self.load(slot_id)
        records = self.records[slot_id]
        state = self.host.slot_directory(slot_id) / "command-v3" / "command-v2.json"
        if not records:
            # Native journal without a host reservation is never discarded.
            if not state.exists():
                return False
        if self.admission is None:
            return bool(records) or state.exists()
        executor = self.executor(slot_id)
        executor.reconcile()
        entries = {item["instance"]: item for item in executor.journal["entries"]}
        retained = []
        for record in records:
            entry = entries.get(record["instance"])
            if record["authority"] in self.live:
                retained.append(record)
            elif entry is not None and (entry["contract_digest"] != record["spec"] or entry["state"] != "settled"):
                retained.append(record)
            # No native entry proves nonlaunch: its durable write precedes Popen.
        if retained != records:
            self.records[slot_id] = retained
            self.save(slot_id)
        return bool(retained) or bool(executor.reconcile()["pending"])

    def recover(self, slot):
        if self.pending(slot.id):
            records = self.records[slot.id]
            memory = max((item["memory_mb"] for item in records), default=262144)
            self.host.reservations[slot.id] = {"memory_mb": memory, "gpu": False}

    def unresolved(self, slot_id):
        """Healthy held calls may share a lane; unknown native ownership may not."""
        if not self.pending(slot_id):
            return False
        if self.admission is None:
            return True
        known = {item["instance"] for item in self.records[slot_id] if item["authority"] in self.live}
        if len(known) != len(self.records[slot_id]):
            return True
        executor = self.executor(slot_id)
        for item in executor.journal["entries"]:
            if item["state"] == "pending":
                native = executor._native.get(item["instance"])
                if item["instance"] not in known or native is None or native.poll() is not None:
                    return True
        return False

    def current(self, slot):
        current = self.host.candidate_slots.get(slot.id) or self.host.registry.get_slot(slot.id)
        return (current.generation, current.package_digest, current.package_path) == (slot.generation, slot.package_digest, slot.package_path)

    def admit(self, spec):
        ticket = self.live.get(spec.authority_digest)
        if ticket is None:
            return False
        session, slot, package, deadline = ticket
        return (not session.closed and slot.id not in self.stopping and not self.host.closing and self.current(slot) and time.monotonic() < deadline
                and spec.package_digest == package.digest and self.host.package(slot).digest == package.digest)

    async def session(self, slot):
        package = self.host.package(slot)
        if (package.manifest.profile != "host-command" or self.admission is None
                or package.manifest.command.helper_sha256 != self.admission.helper_sha256 or self.host.closing
                or slot.id in self.stopping or not self.current(slot) or self.unresolved(slot.id)):
            raise CommandExecutionError("host_command_not_admitted")
        executor = self.executor(slot.id)
        executor._verify_helper()
        handle = CommandSession(self, slot, package)
        self.sessions[handle.instance_id] = handle
        await handle.start()
        return handle

    async def invoke(self, session, name, arguments, request_id):
        slot, package = session.slot, session.package
        contract = package.manifest.command
        deadline = time.monotonic() + contract.deadline_ms / 1000
        tool = next((tool for tool in package.manifest.tools if tool.name == name), None)
        if tool is None or type(arguments) is not dict or not Draft202012Validator(tool.input_schema).is_valid(arguments):
            raise CommandExecutionError("invalid_command_arguments")
        payload = encode(arguments)
        if len(payload) > contract.input_bytes or self.counts.get(slot.id, 0) >= contract.queue_limit:
            raise CommandExecutionError("command_queue_limit")
        self.counts[slot.id] = self.counts.get(slot.id, 0) + 1
        instance = secrets.token_hex(16)
        authority = _digest([slot.id, slot.generation, package.digest, tool.mcp_schema(), contract.model_dump(),
                             session.instance_id, request_id, instance, self.executor(slot.id).policy_digest])
        declaration = contract.commands[name]
        spec = CommandSpecV2(package.root, package.root / declaration.executable,
            (package.root / declaration.cwd).resolve(), package.digest, tuple(declaration.args),
            deadline_ms=contract.deadline_ms, input_limit=contract.input_bytes, output_limit=contract.output_bytes,
            authority_digest=authority)
        try:
            await self.host.reserve_model(slot, package)
            if not self.current(slot) or session.closed or slot.id in self.stopping or self.host.closing:
                raise CommandExecutionError("command_authority_changed")
            self.live[authority] = (session, slot, package, deadline)
            self.load(slot.id)
            if len(self.records[slot.id]) >= 256:
                raise CommandExecutionError("command_journal_limit")
            self.records[slot.id].append({"instance": instance, "authority": authority, "spec": spec.digest,
                "package": package.digest, "generation": slot.generation,
                "memory_mb": self.host.reservations[slot.id]["memory_mb"]})
            self.save(slot.id)
            async with asyncio.timeout_at(deadline):
                result = await self.executor(slot.id).execute(spec, payload, instance_id=instance)
            value = load_json(result.stdout)
            encode(value)  # Reject overflow-to-infinity floats, including nested values.
            if type(value) is not dict or not Draft202012Validator(tool.output_schema).is_valid(value) or not self.admit(spec):
                raise CommandExecutionError("invalid_command_result")
            response = {"content": [{"type": "text", "text": encode(value).decode()}], "structuredContent": value, "isError": False}
            if len(encode({"jsonrpc": "2.0", "id": request_id, "result": response})) > MAX_MESSAGE_BYTES:
                raise CommandExecutionError("command_output_limit")
            return response
        finally:
            self.live.pop(authority, None)
            self.counts[slot.id] -= 1
            self.pending(slot.id)

    async def stop(self, slot_id):
        self.stopping.add(slot_id)
        sessions = [session for session in self.sessions.values() if session.slot.id == slot_id]
        # Revoke every handle before joining the first one; joins yield.
        for session in sessions:
            session.closed = True
        try:
            for session in sessions:
                await session.close()
            self.pending(slot_id)
        finally:
            self.stopping.discard(slot_id)

    async def verify(self, slot, business_smoke):
        session = await self.session(slot)
        reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(session.stdout_fd, "rb", buffering=0))
        session.stdout_fd = None
        writer = session.stdin_fd
        session.stdin_fd = None
        os.set_blocking(writer, False)
        async def write(message):
            view = memoryview(encode(message) + b"\n")
            while view:
                try:
                    view = view[os.write(writer, view):]
                except BlockingIOError:
                    await asyncio.sleep(0.005)
        async def exchange(message):
            await write(message)
            result = load_json(await reader.readline())
            if result.get("id") != message["id"] or "error" in result:
                raise CommandExecutionError("candidate_validation_failed")
            return result["result"]
        try:
            async with asyncio.timeout(session.package.manifest.lifecycle.startup_timeout_seconds):
                await exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "host-verifier", "version": "1"}}})
                await write({"jsonrpc": "2.0", "method": "notifications/initialized"})
                listed = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                if listed != {"tools": [tool.mcp_schema() for tool in session.package.manifest.tools]}:
                    raise CommandExecutionError("tool_schema_mismatch")
                for index, fixture in enumerate(session.package.manifest.validation.fixtures if business_smoke else (), 3):
                    result = await exchange({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
                        "name": fixture.tool, "arguments": fixture.arguments}})
                    if result.get("isError") or result.get("structuredContent") != fixture.expected:
                        raise CommandExecutionError("smoke_probe_failed")
        finally:
            os.close(writer)
            transport.close()
            await session.close()

    async def close(self):
        for session in list(self.sessions.values()):
            await session.close()
        for executor in self.executors.values():
            await executor.close()

    async def retire(self, slot_id):
        if (self.pending(slot_id) or self.counts.get(slot_id, 0)
                or any(session.slot.id == slot_id for session in self.sessions.values())):
            raise CommandExecutionError("command_ownership_unsettled")
        executor = self.executors.get(slot_id)
        if executor is not None:
            await _join(executor.close())
        self.executors.pop(slot_id, None)
        self.records.pop(slot_id, None)
        self.counts.pop(slot_id, None)
        self.stopping.discard(slot_id)


class CommandSession:
    """A protocol handle, deliberately not a legacy OwnedProcess/PID receipt."""
    def __init__(self, service, slot, package, *, publication_check=None):
        # Preparation ownership is a separate trusted constructor capability.
        # Installed sessions must always retain the generation/package guard.
        if ((slot is None and not callable(publication_check))
                or (slot is not None and publication_check is not None)):
            raise CommandExecutionError("command_admission_required")
        self.service, self.slot, self.package = service, slot, package
        self.publication_check = publication_check
        self.instance_id = secrets.token_hex(16)
        self.closed = False
        self.initialized = False
        self.ready = False
        self.calls = {}
        self.seen = set()
        self.reader = asyncio.StreamReader(limit=package.manifest.lifecycle.max_request_bytes)
        self.read_fd, self.stdin_fd = os.pipe()
        self.stdout_fd, self.write_fd = os.pipe()
        os.set_blocking(self.write_fd, False)
        self.write_lock = asyncio.Lock()
        self.transport = None
        self.task = None
        self.close_task = None

    async def start(self):
        self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self.reader), os.fdopen(self.read_fd, "rb", buffering=0))
        self.task = asyncio.create_task(self.run())

    def close_stdio(self):
        for name in ("stdin_fd", "stdout_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    async def send(self, message, *, deadline=None):
        body = encode(message) + b"\n"
        if len(body) > MAX_MESSAGE_BYTES:
            raise CommandExecutionError("command_output_limit")
        async with self.write_lock, asyncio.timeout(2):
            view = memoryview(body)
            while view:
                if self.slot is None:
                    current = self.publication_check()
                    current = (await current if inspect.isawaitable(current) else current) is True
                else:
                    current = (not self.service.host.closing and self.service.current(self.slot)
                        and self.service.host.package(self.slot).digest == self.package.digest)
                if (self.closed or not current
                        or deadline is not None and time.monotonic() >= deadline):
                    raise CommandExecutionError("command_authority_changed")
                try:
                    view = view[os.write(self.write_fd, view):]
                except BlockingIOError:
                    await asyncio.sleep(0.005)

    async def call(self, message):
        request_id = message["id"]
        key = (type(request_id), request_id)
        deadline = time.monotonic() + self.package.manifest.command.deadline_ms / 1000
        try:
            params = message.get("params", {})
            response = await self.service.invoke(self, params.get("name"), params.get("arguments", {}), request_id)
            if not self.closed:
                await self.send({"jsonrpc": "2.0", "id": request_id, "result": response}, deadline=deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.closed:
                with contextlib.suppress(Exception):
                    await self.send(error_message(request_id))
        finally:
            self.calls.pop(key, None)

    async def run(self):
        try:
            while not self.closed:
                line = await self.reader.readline()
                if not line or not line.endswith(b"\n") or len(line) > self.package.manifest.lifecycle.max_request_bytes:
                    break
                message = load_json(line)
                JSONRPCMessage.model_validate(message)
                method = message.get("method")
                if "id" not in message:
                    if method == "notifications/initialized" and self.initialized:
                        self.ready = True
                    elif method == "notifications/cancelled":
                        identifier = message.get("params", {}).get("requestId")
                        task = self.calls.get((type(identifier), identifier))
                        if task is not None:
                            task.cancel()
                    else:
                        break
                    continue
                key = (type(message["id"]), message["id"])
                identity = request_id_digest(message["id"])
                if identity in self.seen or len(self.seen) >= 1024:
                    break
                self.seen.add(identity)
                if method == "initialize" and not self.initialized:
                    self.initialized = True
                    result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "mcpme-host-command", "version": "1.0.0"}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list" and self.ready:
                    result = {"tools": [tool.mcp_schema() for tool in self.package.manifest.tools]}
                elif method == "tools/call" and self.ready and len(self.calls) < 16:
                    self.calls[key] = asyncio.create_task(self.call(message))
                    continue
                else:
                    await self.send(error_message(message["id"]))
                    continue
                await self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            # Own cleanup in a separate task so cancellation never strands H.
            if self.close_task is None:
                self.close_task = asyncio.create_task(self.cleanup())

    async def cleanup(self):
        self.closed = True
        for task in list(self.calls.values()):
            task.cancel()
        await asyncio.gather(*list(self.calls.values()), return_exceptions=True)
        if self.transport is not None:
            self.transport.close()
        self.close_stdio()
        with contextlib.suppress(OSError):
            os.close(self.write_fd)
        self.service.sessions.pop(self.instance_id, None)

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            # A task canceled before its first instruction never runs finally.
            await _join(asyncio.gather(self.task, return_exceptions=True))
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.cleanup())
        await _join(self.close_task)
