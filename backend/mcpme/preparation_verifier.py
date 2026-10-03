"""Independent SDK probes of exact sealed preparation results.

Probe messages and functional outputs remain in bounded memory. Reports only
contain identities and counts. Execution/attachment authority belongs to the
coordinator, never to a recipe-provided receipt or claimed passing gate.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from .contracts import PackageError, validate_package
from .preparation_contracts import PreparationError, PreparationDescriptor, canonical_bytes
from .preparation_processes import _settle
from .processes import OwnedProcess, OwnershipLost, ProcessOwner, _watch_ownership

MAX_PROBE_MESSAGE = 1024 * 1024


@dataclass(frozen=True)
class VerificationReport:
    package_digest: str
    tools_checked: int
    fixtures_checked: int
    protocol_version: str
    peak_memory_bytes: int


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
    timeout_seconds: float = 60,
) -> VerificationReport:
    """Use a real MCP SDK session; check schemas, fixtures, mutation and cleanup.

    The caller supplies a relocated closed package and private probe workspace.
    OS filesystem/network restrictions are a separate gate; this primitive does
    not claim them. Only the current tools-only stdio profile is implemented.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 3600:
        raise PreparationError("invalid_preparation_probe_limit")
    try:
        package = await asyncio.to_thread(validate_package, package_root)
    except PackageError as failure:
        raise PreparationError(failure.code) from None
    if (package.manifest.profile != "mcp-stdio" or package.manifest.tools != descriptor.interface.tools
            or package.manifest.access != descriptor.access
            or package.manifest.resources.model_dump() != descriptor.resources.model_dump()
            or package.manifest.lifecycle != descriptor.lifecycle
            or package.manifest.package_id != descriptor.package.id
            or package.manifest.version != descriptor.package.version
            or package.manifest.name != descriptor.package.name):
        raise PreparationError("preparation_candidate_mismatch")
    if {fixture.tool for fixture in descriptor.validation} != {tool.name for tool in descriptor.interface.tools}:
        raise PreparationError("preparation_fixture_coverage_required")
    owner = ProcessOwner(reaper)
    process = None
    try:
        async with asyncio.timeout(timeout_seconds):
            entry = package.manifest.entrypoint
            process = await owner.launch(
                package.root / entry.executable, entry.args, package.root / entry.cwd,
                {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "TMPDIR": str(workspace), "LANG": "en_US.UTF-8"},
                owned_group=True, startup_timeout=min(timeout_seconds, 120), stop_grace=0.1,
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
                                [tool.mcp_schema() for tool in descriptor.interface.tools]):
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
        try:
            current = await asyncio.to_thread(validate_package, package.root)
        except PackageError:
            raise PreparationError("preparation_package_mutated") from None
        if current.digest != package.digest:
            raise PreparationError("preparation_package_mutated")
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
