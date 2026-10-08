"""Job-scoped command ownership, separate from slots and legacy PID receipts.

Only immutable authority digests are journaled. A durable initialized native
journal precedes registration; per-invocation intent precedes native launch.
Recovery never creates missing files, adopts PIDs, or executes package code.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, replace
import fcntl
import os
from pathlib import Path
import secrets
import stat
import time

from jsonschema import Draft202012Validator

from .command_execution_v2 import (CommandExecutionError, CommandExecutorV2, CommandSpecV2,
    _digest, _hex, _join, validate_command_journal, verify_command_completion)
from . import command_resource_execution_v1 as resource_execution
from .command_resource_policy_v1 import ResourcePolicyV1
from .contracts import LOCK_NAME, MANIFEST_NAME, MAX_ARTIFACTS, validate_package
from .gateway import MAX_MESSAGE_BYTES, encode
from .host_command import CommandSession, HostCommandAdmission
from .policy import load_json
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .private import check_private_file, ensure_private_directory, read_private_json, write_private_json


@dataclass(frozen=True)
class CommandPreparationResourceAdmission:
    """Host-selected helper and exact-platform measured-workload capability."""
    helper: Path
    helper_sha256: str
    platform_policy: str
    policy: ResourcePolicyV1 = ResourcePolicyV1()

    def __post_init__(self):
        if (not isinstance(self.helper, Path) or not _hex(self.helper_sha256)
                or not _hex(self.platform_policy) or type(self.policy) is not ResourcePolicyV1):
            raise PreparationError("command_preparation_not_admitted")


@dataclass(frozen=True)
class _CommandLaunchIdentity:
    """One pre-validation metadata baseline, never replacement byte authority."""
    records: tuple

    @staticmethod
    def _identity(path, full):
        try:
            value = path.lstat()
        except OSError:
            raise PreparationError("preparation_package_mutated") from None
        basic = (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
        return basic + ((value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns) if full else ())

    @classmethod
    def capture(cls, package, helpers):
        files = {package.root / artifact.path for artifact in package.lock.artifacts}
        files.update((*package.metadata_paths, *helpers))
        directories = {package.root}
        for path in tuple(files):
            directories.update(path.parents)
        if len(files) > MAX_ARTIFACTS + 4 or len(directories) > MAX_ARTIFACTS * 2 + 1024:
            raise PreparationError("preparation_package_mutated")
        records = []
        for path in sorted(files | directories):
            full = path in files or path == package.root or package.root in path.parents
            identity = cls._identity(path, full)
            if (path in files and (not stat.S_ISREG(identity[2]) or identity[4] != 1)
                    or path in directories and not stat.S_ISDIR(identity[2])):
                raise PreparationError("preparation_package_mutated")
            records.append((path, full, identity))
        return cls(tuple(records))

    def check(self):
        for path, full, identity in self.records:
            if self._identity(path, full) != identity:
                raise PreparationError("preparation_package_mutated")


def command_owner_paths(workspace, metadata):
    validate_command_ownership(metadata)
    if metadata["receipt_format"] == 4:
        return Path(workspace) / "command-probe-v4", "command-resource-v1"
    return Path(workspace) / "command-probe-v3", "command-v2"


def validate_initial_command_owner(workspace, metadata):
    state, journal = command_owner_paths(workspace, metadata)
    check_private_file(state / (journal + ".lock"))
    if (read_private_json(state / "owner.json") != metadata
            or read_private_json(state / "invocations.json") != {"version": 1, "entries": []}
            or read_private_json(state / (journal + ".json")) != {"version": metadata["receipt_format"], "entries": []}):
        raise PreparationError("invalid_command_ownership")


def validate_command_ownership(value):
    keys = {"schema_version", "kind", "instance_id", "attempt_id", "job_binding_digest", "candidate_digest",
            "fixture_digest", "helper_sha256", "policy_digest", "journal_identity", "receipt_format"}
    measured = type(value) is dict and value.get("receipt_format") == 4
    if measured:
        keys |= {"resource_helper_sha256", "resource_policy_digest"}
    if (type(value) is not dict or set(value) != keys or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["kind"] != ("command-v4" if measured else "command-v3")
            or type(value["receipt_format"]) is not int or value["receipt_format"] != (4 if measured else 3)
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
        state, journal_name = command_owner_paths(workspace, metadata)
        if not state.is_dir():
            return None
        ensure_private_directory(state)
        lock = state / (journal_name + ".lock")
        check_private_file(lock)
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read_private_json(state / "owner.json") != metadata:
            return None
        intents = read_private_json(state / "invocations.json")
        indexed = _invocations(intents)
        journal = read_private_json(state / (journal_name + ".json"))
        measured = metadata["receipt_format"] == 4
        validator = resource_execution.validate_command_journal if measured else validate_command_journal
        validator(journal)
        for entry in journal["entries"]:
            intent = indexed.get(entry["instance"])
            if (intent is None or entry["contract_digest"] != intent["spec"]
                    or entry["helper_digest"] != metadata["resource_helper_sha256" if measured else "helper_sha256"]
                    or entry["policy_digest"] != metadata["policy_digest"]
                    or measured and entry["resource_policy_digest"] != metadata["resource_policy_digest"]
                    or not (resource_execution.read_resource_completion(state, entry)[0] if measured
                            else verify_command_completion(state, entry)[0])):
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
                 package, descriptor, *, attempt_id: str, check, deadline: float, resource_admission=None):
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
        if resource_admission is not None and type(resource_admission) is not CommandPreparationResourceAdmission:
            raise PreparationError("command_preparation_not_admitted")
        self.resource_admission = resource_admission
        self._descriptor_baseline = descriptor.model_copy(deep=True)
        self._binding_baseline = copy.deepcopy(store.get(job_id)["binding"])
        helpers = (admission.helper,) + ((resource_admission.helper,) if resource_admission is not None else ())
        self._launch_identity = _CommandLaunchIdentity.capture(package, helpers)
        self.resource_reports = []
        self.intents = {"version": 1, "entries": []}
        measured = resource_admission is not None
        state = workspace / ("command-probe-v4" if measured else "command-probe-v3")
        state.mkdir(mode=0o700)  # Never reopen a previous attempt as fresh authority.
        if measured:
            self.resource_policy = replace(resource_admission.policy,
                footprint_bytes=min(resource_admission.policy.footprint_bytes, descriptor.resources.memory_mb * 1024**2))
            self.executor = resource_execution.CommandResourceExecutorV1(resource_admission.helper,
                resource_admission.helper_sha256, state, expected_platform_policy=resource_admission.platform_policy,
                admit=self.admit, before_native_create=self.native_admit)
        else:
            self.executor = CommandExecutorV2(admission.helper, admission.helper_sha256, state,
                admit=self.admit, before_native_create=self.native_admit)
        metadata = {"schema_version": 1, "kind": "command-v3", "instance_id": secrets.token_hex(16),
            "attempt_id": attempt_id, "job_binding_digest": contract_digest("preparation-command-job-binding-v1", store.get(job_id)["binding"]),
            "candidate_digest": package.digest, "fixture_digest": contract_digest("preparation-command-fixtures-v1",
                [item.model_dump(exclude_none=True) for item in descriptor.validation]),
            "helper_sha256": admission.helper_sha256, "policy_digest": self.executor.policy_digest, "receipt_format": 3}
        if measured:
            metadata.update(kind="command-v4", receipt_format=4, resource_helper_sha256=resource_admission.helper_sha256,
                resource_policy_digest=self.resource_policy.digest)
        metadata["journal_identity"] = contract_digest("preparation-command-journal-v1", metadata)
        self.metadata = metadata
        self._metadata_baseline = copy.deepcopy(metadata)
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
        self._launch_identity.check()
        current = validate_package(self.package.root)
        self._launch_identity.check()
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
        self._launch_identity.check()
        return verified and self._authority_current()

    def native_admit(self, spec):
        """Synchronous metadata/authority fence at the actual native creation."""
        ticket = self.live.get(spec.authority_digest)
        if (ticket is None or ticket[3] is not spec or ticket[1].closed
                or ticket[1] not in self.sessions.values() or time.monotonic() >= ticket[2]
                or not self._authority_current() or self.descriptor != self._descriptor_baseline
                or self.metadata != self._metadata_baseline
                or self.store.get(self.job_id)["binding"] != self._binding_baseline
                or self.store.command_ownership(self.job_id) != [self._metadata_baseline]):
            return False
        self._launch_identity.check()
        return True

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
        spec_type = resource_execution.CommandResourceSpecV1 if self.resource_admission is not None else CommandSpecV2
        extra = {"resource_policy": self.resource_policy} if self.resource_admission is not None else {}
        spec = spec_type(package.root, package.root / declaration.executable,
            (package.root / declaration.cwd).resolve(), package.digest, tuple(declaration.args),
            deadline_ms=contract.deadline_ms, input_limit=contract.input_bytes, output_limit=contract.output_bytes,
            authority_digest=authority, **extra)
        self.live[authority] = (spec.digest, session, deadline, spec)
        try:
            self.intents["entries"].append({"instance": instance, "spec": spec.digest, "authority": authority})
            write_private_json(self.executor.state / "invocations.json", self.intents)
            async with asyncio.timeout_at(deadline):
                try:
                    result = await self.executor.execute(spec, payload, instance_id=instance,
                        **({"capture_output": True} if self.resource_admission is not None else {}))
                except resource_execution.CommandExecutionError as failure:
                    raise CommandExecutionError(failure.code) from None
            if self.resource_admission is not None:
                report = result.report
                if (result.returncode != 0 or report.reason != "exited" or not report.sample_windows_valid
                        or not report.samples or report.max_group_window_footprint_bytes <= 0
                        or not 0 < report.max_scope_window_footprint_bytes <= self.resource_policy.footprint_bytes
                        or report.max_group_members > self.resource_policy.group_members
                        or report.observed_own_cpu_ns > self.resource_policy.cpu_ns
                        or report.hard_aggregate_enforced or report.lifetime_coverage_complete):
                    raise CommandExecutionError("command_resource_verification_failed")
                self.resource_reports.append((instance, spec.digest, report))
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
