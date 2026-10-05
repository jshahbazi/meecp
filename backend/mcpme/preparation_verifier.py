"""Independent SDK probes of exact sealed preparation results.

Probe messages and functional outputs remain in bounded memory. Reports only
contain identities and counts. Execution/attachment authority belongs to the
coordinator, never to a recipe-provided receipt or claimed passing gate.
"""
from __future__ import annotations

from .preparation_inputs import descriptor_tools

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from .contracts import LOCK_NAME, MANIFEST_NAME, MAX_ARTIFACTS, PackageError, validate_package
from .preparation_contracts import PreparationError, PreparationDescriptor, canonical_bytes
from .preparation_processes import _settle
from .processes import OwnedProcess, OwnershipLost, ProcessOwner, _await_ownership, _watch_ownership

MAX_PROBE_MESSAGE = 1024 * 1024


@dataclass(frozen=True)
class VerificationReport:
    package_digest: str
    tools_checked: int
    fixtures_checked: int
    protocol_version: str
    peak_memory_bytes: int


async def _validate_joined(package_root: Path):
    """Cancellation cannot return while package hashing still reads its tree."""
    task = asyncio.create_task(asyncio.to_thread(validate_package, package_root))
    try:
        return await asyncio.shield(task)
    finally:
        await _await_ownership(asyncio.gather(task, return_exceptions=True))


@dataclass(frozen=True)
class _ProbeLaunchIdentity:
    """Metadata from the same joined validation, never a new byte authority."""
    records: tuple

    @staticmethod
    def _identity(path, full):
        try:
            value = path.lstat()
        except OSError:
            raise PreparationError("preparation_probe_confinement_mismatch") from None
        basic = (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
        return basic + ((value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns) if full else ())

    @classmethod
    def capture(cls, package, plan):
        from .preparation_probe_confinement import SANDBOX
        files = {package.root / item.path for item in package.lock.artifacts}
        files.update((package.root / MANIFEST_NAME, package.root / LOCK_NAME, plan.reaper, SANDBOX))
        # Every locked file's ancestor guards membership/substitution in the
        # complete closed package. Outside ancestors use stable directory
        # identity, so unrelated sibling creation cannot invalidate the plan.
        directories = {plan.workspace, plan.home, plan.temporary, package.root}
        for path in tuple(files) + tuple(directories):
            directories.update(path.parents)
        if len(files) > MAX_ARTIFACTS + 4 or len(directories) > MAX_ARTIFACTS * 2 + 1024:
            raise PreparationError("preparation_probe_confinement_mismatch")
        records = []
        for path in sorted(files | directories):
            full = path in files or path == package.root or package.root in path.parents
            records.append((path, full, cls._identity(path, full)))
        return cls(tuple(records))

    def check(self):
        for path, full, identity in self.records:
            if self._identity(path, full) != identity:
                raise PreparationError("preparation_probe_confinement_mismatch")

    def admit(self) -> bool:
        self.check()
        return True


def _bound_confined_launch(package, plan, reaper):
    from .preparation_probe_confinement import ProbeConfinementV1
    if type(plan) is not ProbeConfinementV1:
        raise PreparationError("preparation_probe_confinement_mismatch")
    identity = _ProbeLaunchIdentity.capture(package, plan)
    try:
        current = validate_package(package.root)
    except PackageError:
        raise PreparationError("preparation_package_mutated") from None
    if current.digest != package.digest:
        raise PreparationError("preparation_package_mutated")
    launch = plan.launch(current, reaper)
    identity.check()
    return launch, identity


async def capture_confined_probe_launch(package, plan, reaper):
    """Trusted host seam: joined validation plus immutable native admit guard.

    The caller owns a private workspace and typed plan. It must pass the
    returned guard's ``admit`` to ProcessOwner.launch and bind expected_reaper.
    This is not a portable/public policy selector or job authority callback.
    """
    task = asyncio.create_task(asyncio.to_thread(_bound_confined_launch, package, plan, reaper))
    try:
        return await asyncio.shield(task)
    finally:
        await _await_ownership(asyncio.gather(task, return_exceptions=True))


@asynccontextmanager
async def _owned_transport(process: OwnedProcess):
    assert process.stdout is not None and process.stdin is not None
    incoming_send, incoming = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    outgoing, outgoing_receive = anyio.create_memory_object_stream[SessionMessage](1)

    async def read():
        async with incoming_send:
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        return
                    if len(line) > MAX_PROBE_MESSAGE:
                        raise ValueError()
                    await incoming_send.send(SessionMessage(JSONRPCMessage.model_validate_json(line)))
            except (ValueError, OSError):
                await incoming_send.send(PreparationError("invalid_preparation_probe_message"))

    async def write():
        async with outgoing_receive:
            async for message in outgoing_receive:
                data = message.message.model_dump_json(by_alias=True, exclude_none=True).encode() + b"\n"
                if len(data) > MAX_PROBE_MESSAGE:
                    raise PreparationError("preparation_probe_message_limit")
                process.stdin.write(data)
                await process.stdin.drain()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(read)
        tasks.start_soon(write)
        try:
            async with incoming, outgoing:
                yield incoming, outgoing
        finally:
            tasks.cancel_scope.cancel()


async def verify_prepared_package(
    package_root: Path, descriptor: PreparationDescriptor, reaper: Path, workspace: Path, *,
    timeout_seconds: float = 60, owner: ProcessOwner | None = None, probe_confinement=None,
    confine_private_python: bool = False,
) -> VerificationReport:
    """Use a real MCP SDK session; check schemas, fixtures, mutation and cleanup.

    The caller supplies a relocated closed package and private probe workspace.
    Managed callers select confinement through a trusted host argument. The
    verifier captures the v1 plan from the exact sealed private Python package
    and workspace; package descriptors cannot select or widen the policy.
    Direct callers retain their existing default and may supply a host-created
    plan. Installed policy acceptance remains separate. Only tools-only stdio
    is implemented here.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 3600:
        raise PreparationError("invalid_preparation_probe_limit")
    if type(confine_private_python) is not bool or (confine_private_python and probe_confinement is not None):
        raise PreparationError("preparation_probe_confinement_mismatch")
    try:
        package = await _validate_joined(package_root)
    except PackageError as failure:
        raise PreparationError(failure.code) from None
    if (package.manifest.profile != "mcp-stdio" or package.manifest.tools != descriptor_tools(descriptor)
            or package.manifest.access != descriptor.access
            or package.manifest.resources.model_dump() != descriptor.resources.model_dump()
            or package.manifest.lifecycle != descriptor.lifecycle
            or package.manifest.package_id != descriptor.package.id
            or package.manifest.version != descriptor.package.version
            or package.manifest.name != descriptor.package.name):
        raise PreparationError("preparation_candidate_mismatch")
    if {fixture.tool for fixture in descriptor.validation} != {tool.name for tool in descriptor_tools(descriptor)}:
        raise PreparationError("preparation_fixture_coverage_required")
    owner = owner if owner is not None else ProcessOwner(reaper)
    process = None
    try:
        async with asyncio.timeout(timeout_seconds):
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            entry = package.manifest.entrypoint
            launch_identity = None
            if confine_private_python:
                from .preparation_probe_confinement import ProbeConfinementV1
                if owner.reaper != Path(reaper):
                    raise PreparationError("preparation_probe_confinement_mismatch")
                task = asyncio.create_task(asyncio.to_thread(ProbeConfinementV1.capture, package, workspace, reaper))
                try:
                    probe_confinement = await asyncio.shield(task)
                finally:
                    await _await_ownership(asyncio.gather(task, return_exceptions=True))
            if probe_confinement is None:
                launch = (package.root / entry.executable, entry.args, package.root / entry.cwd,
                          {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "TMPDIR": str(workspace), "LANG": "en_US.UTF-8"})
            else:
                from .preparation_probe_confinement import ProbeConfinementV1
                if (type(probe_confinement) is not ProbeConfinementV1 or probe_confinement.workspace != workspace
                        or owner.reaper != Path(reaper)):
                    raise PreparationError("preparation_probe_confinement_mismatch")
                launch, launch_identity = await capture_confined_probe_launch(package, probe_confinement, reaper)
                if owner.reaper != probe_confinement.reaper:
                    raise PreparationError("preparation_probe_confinement_mismatch")
            def admit():
                launch_identity.check()
                if asyncio.get_running_loop().time() >= deadline:
                    raise PreparationError("preparation_probe_timeout")
                return True
            process = await owner.launch(
                *launch,
                owned_group=True, startup_timeout=min(timeout_seconds, 120), stop_grace=0.1,
                **({"expected_reaper": probe_confinement.reaper} if probe_confinement is not None else {}),
                **({"admit": admit} if launch_identity is not None else {}),
            )
            async def probe():
                async with _owned_transport(process) as (incoming, outgoing):
                    async with ClientSession(incoming, outgoing) as client:
                        result = await client.initialize()
                        if result.protocolVersion != "2025-11-25" or set(result.capabilities.model_dump(exclude_none=True)) != {"tools"}:
                            raise PreparationError("unsupported_preparation_probe_capabilities")
                        listing = await client.list_tools()
                        if (listing.nextCursor is not None or
                                [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] !=
                                [tool.mcp_schema() for tool in descriptor_tools(descriptor)]):
                            raise PreparationError("preparation_tool_schema_mismatch")
                        for fixture in descriptor.validation:
                            output = await client.call_tool(fixture.tool, fixture.arguments)
                            if output.isError or canonical_bytes(output.structuredContent) != canonical_bytes(fixture.expected):
                                raise PreparationError("preparation_fixture_failed")
                        return result, listing
            result, listing = await _watch_ownership(process, probe())
        # Shutdown precedes revalidation so asynchronous writes cannot race a
        # passing result while the verifier still owns live package execution.
        await _settle(process)
        peak = process.peak_memory_bytes
        if peak is None or peak <= 0:
            raise PreparationError("preparation_probe_measurement_unavailable")
        if peak > descriptor.resources.memory_mb * 1024 * 1024:
            raise PreparationError("preparation_probe_memory_limit")
        if confine_private_python and owner.graph_accounting is not None:
            graph_report = owner.graph_accounting.report()
            if (not graph_report.ownership_settled or not graph_report.foreground_measurements_complete
                    or graph_report.conservative_member_rss_max_sum_bytes != peak):
                raise PreparationError("preparation_probe_measurement_unavailable")
        try:
            current = await _validate_joined(package.root)
        except PackageError:
            raise PreparationError("preparation_package_mutated") from None
        if current.digest != package.digest:
            raise PreparationError("preparation_package_mutated")
        if probe_confinement is not None:
            task = asyncio.create_task(asyncio.to_thread(probe_confinement.check, current, reaper))
            try:
                await asyncio.shield(task)
            finally:
                await _await_ownership(asyncio.gather(task, return_exceptions=True))
            if owner.reaper != probe_confinement.reaper:
                raise PreparationError("preparation_probe_confinement_mismatch")
        return VerificationReport(package.digest, len(listing.tools), len(descriptor.validation), result.protocolVersion, peak)
    except OwnershipLost:
        raise
    except TimeoutError:
        raise PreparationError("preparation_probe_timeout") from None
    except PreparationError:
        raise
    except Exception as failure:
        # Task groups wrap host checks. Recover only our fixed error codes;
        # raw protocol/source exceptions never enter the returned diagnostic.
        pending = [failure]
        for _ in range(64):
            if not pending:
                break
            item = pending.pop()
            if isinstance(item, PreparationError):
                raise PreparationError(item.code) from None
            if isinstance(item, ExceptionGroup):
                pending.extend(item.exceptions)
        raise PreparationError("preparation_probe_failed") from None
    finally:
        if process is not None:
            await _settle(process)
        await owner.close()
