"""Job-scoped command ownership, separate from slots and legacy PID receipts.

Only immutable authority digests are journaled. A durable initialized native
journal precedes registration; per-invocation intent precedes native launch.
Recovery never creates missing files, adopts PIDs, or executes package code.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
import secrets
import time

from jsonschema import Draft202012Validator

from .command_execution_v2 import (CommandExecutionError, CommandExecutorV2, CommandSpecV2,
    _digest, _hex, _join, validate_command_journal, verify_command_completion)
from .contracts import validate_package
from .gateway import MAX_MESSAGE_BYTES, encode
from .host_command import CommandSession, HostCommandAdmission
from .policy import load_json
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .private import check_private_file, ensure_private_directory, read_private_json, write_private_json


def validate_command_ownership(value):
    keys = {"schema_version", "kind", "instance_id", "attempt_id", "job_binding_digest", "candidate_digest",
            "fixture_digest", "helper_sha256", "policy_digest", "journal_identity", "receipt_format"}
    if (type(value) is not dict or set(value) != keys or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["kind"] != "command-v3"
            or type(value["receipt_format"]) is not int or value["receipt_format"] != 3
            or any(not _hex(value[key], 32) for key in ("instance_id", "attempt_id"))
            or any(not _hex(value[key]) for key in keys - {"schema_version", "kind", "instance_id", "attempt_id", "receipt_format"})):
        raise PreparationError("invalid_command_ownership")
    identity = contract_digest("preparation-command-journal-v1", {k: v for k, v in value.items() if k != "journal_identity"})
    if value["journal_identity"] != identity:
        raise PreparationError("invalid_command_ownership")


def _invocations(value):
    if (type(value) is not dict or set(value) != {"version", "entries"} or type(value["version"]) is not int
            or value["version"] != 1 or type(value["entries"]) is not list or len(value["entries"]) > 64):
        raise PreparationError("invalid_command_ownership")
    identities = set()
    for item in value["entries"]:
        if (type(item) is not dict or set(item) != {"instance", "spec", "authority"}
                or not _hex(item["instance"], 32) or item["instance"] in identities
                or not _hex(item["spec"]) or not _hex(item["authority"])):
            raise PreparationError("invalid_command_ownership")
        identities.add(item["instance"])
    return {item["instance"]: item for item in value["entries"]}


def recover_command_ownership(workspace: Path, metadata: dict) -> str | None:
    """Return a recomputable settlement identity, or retain all reservations.

    The existing lock excludes a live writer. Its absence never proves
    nonlaunch. Publication receipts may safely arrive after the owner dies.
    """
    fd = None
    try:
        validate_command_ownership(metadata)
        state = Path(workspace) / "command-probe-v3"
        if not state.is_dir():
            return None
        ensure_private_directory(state)
        lock = state / "command-v2.lock"
        check_private_file(lock)
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read_private_json(state / "owner.json") != metadata:
            return None
        intents = read_private_json(state / "invocations.json")
        indexed = _invocations(intents)
        journal = read_private_json(state / "command-v2.json")
        validate_command_journal(journal)
        for entry in journal["entries"]:
            intent = indexed.get(entry["instance"])
            if (intent is None or entry["contract_digest"] != intent["spec"]
                    or entry["helper_digest"] != metadata["helper_sha256"]
                    or entry["policy_digest"] != metadata["policy_digest"]
                    or not verify_command_completion(state, entry)[0]):
                return None
        return contract_digest("preparation-command-settlement-v1", {
            "owner": metadata, "invocations": intents, "journal": journal})
    except (OSError, ValueError, RuntimeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


class CommandPreparationOwner:
    """Trusted runner capability: exact job, attempt, candidate and fixtures."""
    def __init__(self, admission: HostCommandAdmission, store, job_id: str, workspace: Path,
                 package, descriptor, *, attempt_id: str, check, deadline: float):
        if type(admission) is not HostCommandAdmission or not callable(check) or not _hex(attempt_id, 32):
            raise PreparationError("command_preparation_not_admitted")
        if package.manifest.profile != "host-command" or package.manifest.command.helper_sha256 != admission.helper_sha256:
            raise PreparationError("command_preparation_not_admitted")
        self.store, self.job_id, self.workspace = store, job_id, workspace
        self.package, self.descriptor = package, descriptor
        self.descriptor_digest = descriptor.digest
        self.check, self.deadline = check, deadline
        self.closed, self.sessions, self.live = False, {}, {}
        self.confirmed = False
        self.intents = {"version": 1, "entries": []}
        state = workspace / "command-probe-v3"
        state.mkdir(mode=0o700)  # Never reopen a previous attempt as fresh authority.
        self.executor = CommandExecutorV2(admission.helper, admission.helper_sha256, state, admit=self.admit)
        metadata = {"schema_version": 1, "kind": "command-v3", "instance_id": secrets.token_hex(16),
            "attempt_id": attempt_id, "job_binding_digest": contract_digest("preparation-command-job-binding-v1", store.get(job_id)["binding"]),
            "candidate_digest": package.digest, "fixture_digest": contract_digest("preparation-command-fixtures-v1",
                [item.model_dump(exclude_none=True) for item in descriptor.validation]),
            "helper_sha256": admission.helper_sha256, "policy_digest": self.executor.policy_digest, "receipt_format": 3}
        metadata["journal_identity"] = contract_digest("preparation-command-journal-v1", metadata)
        self.metadata = metadata
        try:
            write_private_json(state / "owner.json", metadata)
            write_private_json(state / "invocations.json", self.intents)
            write_private_json(self.executor.journal_path, self.executor.journal)
            if not self._verify_package():
                raise PreparationError("command_preparation_not_admitted")
            store.register_command_ownership(job_id, metadata)
        except BaseException:
            os.close(self.executor.lock_fd)
            self.executor.lock_fd = None
            self.executor.closed = True
            raise

    def _authority_current(self):
        if self.closed or time.monotonic() >= self.deadline:
            return False
        self.check()
        self.store.check_authority(self.job_id)
        return True

    def _verify_package(self):
        if not self._authority_current():
            return False
        current = validate_package(self.package.root)
        return (self.descriptor.digest == self.descriptor_digest
            and contract_digest("preparation-command-job-binding-v1", self.store.get(self.job_id)["binding"])
                == self.metadata["job_binding_digest"]
            and current.digest == self.package.digest and current.manifest == self.package.manifest
            and current.lock == self.package.lock)

    async def publication_check(self):
        if not self._authority_current():
            return False
        task = asyncio.create_task(asyncio.to_thread(self._verify_package))
        try:
            verified = await asyncio.shield(task)
        finally:
            await _join(asyncio.gather(task, return_exceptions=True))
        return verified and self._authority_current()

    async def admit(self, spec):
        ticket = self.live.get(spec.authority_digest)
        return (ticket is not None and ticket[0] == spec.digest and not ticket[1].closed
                and time.monotonic() < ticket[2] and await self.publication_check()
                and not ticket[1].closed and time.monotonic() < ticket[2])

    async def session(self, package):
        if package.digest != self.package.digest or not await self.publication_check() or self.sessions:
            raise PreparationError("command_preparation_not_admitted")
        handle = CommandSession(self, None, package, publication_check=self.publication_check)
        self.sessions[handle.instance_id] = handle
        try:
            await handle.start()
        except BaseException:
            await _join(handle.close())
            raise
        return handle

    async def invoke(self, session, name, arguments, request_id):
        package, contract = self.package, self.package.manifest.command
        deadline = min(self.deadline, time.monotonic() + contract.deadline_ms / 1000)
        async with asyncio.timeout_at(deadline):
            if session not in self.sessions.values() or session.closed or not await self.publication_check():
                raise CommandExecutionError("command_authority_changed")
        fixture = next((item for item in self.descriptor.validation if item.tool == name
            and canonical_bytes(item.arguments) == canonical_bytes(arguments)), None)
        tool = next((item for item in package.manifest.tools if item.name == name), None)
        if fixture is None or tool is None or type(arguments) is not dict or not Draft202012Validator(tool.input_schema).is_valid(arguments):
            raise CommandExecutionError("invalid_command_arguments")
        if self.live or len(self.intents["entries"]) >= 64:
            raise CommandExecutionError("command_queue_limit")
        payload = encode(arguments)
        if len(payload) > contract.input_bytes:
            raise CommandExecutionError("command_input_limit")
        instance = secrets.token_hex(16)
        authority = _digest([self.metadata, session.instance_id, request_id, instance, tool.mcp_schema(), contract.model_dump()])
        declaration = contract.commands[name]
        spec = CommandSpecV2(package.root, package.root / declaration.executable,
            (package.root / declaration.cwd).resolve(), package.digest, tuple(declaration.args),
            deadline_ms=contract.deadline_ms, input_limit=contract.input_bytes, output_limit=contract.output_bytes,
            authority_digest=authority)
        self.live[authority] = (spec.digest, session, deadline)
        try:
            self.intents["entries"].append({"instance": instance, "spec": spec.digest, "authority": authority})
            write_private_json(self.executor.state / "invocations.json", self.intents)
            async with asyncio.timeout_at(deadline):
                result = await self.executor.execute(spec, payload, instance_id=instance)
            value = load_json(result.stdout)
            encode(value)
            async with asyncio.timeout_at(deadline):
                if type(value) is not dict or not Draft202012Validator(tool.output_schema).is_valid(value) or not await self.admit(spec):
                    raise CommandExecutionError("invalid_command_result")
            response = {"content": [{"type": "text", "text": encode(value).decode()}], "structuredContent": value, "isError": False}
            if len(encode({"jsonrpc": "2.0", "id": request_id, "result": response})) > MAX_MESSAGE_BYTES:
                raise CommandExecutionError("command_output_limit")
            return response
        finally:
            self.live.pop(authority, None)

    async def close(self):
        self.closed = True
        for session in list(self.sessions.values()):
            await _join(session.close())
        await _join(self.executor.close())

    def confirm_completed(self):
        if not self.closed or self.live or self.sessions:
            return False
        if self.confirmed:
            return not self.store.command_ownership(self.job_id)
        identity = recover_command_ownership(self.workspace, self.metadata)
        if identity is not None:
            self.store.confirm_command_ownership(self.job_id, self.metadata["instance_id"], identity)
            self.confirmed = not self.store.command_ownership(self.job_id)
        return self.confirmed
