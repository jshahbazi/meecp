"""Actual SDK probe over a trusted host command protocol session."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import os

import anyio
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from .command_execution_v2 import _join
from .preparation_command_owner import CommandPreparationOwner
from .preparation_contracts import PreparationError, canonical_bytes
from .preparation_verifier import MAX_PROBE_MESSAGE, VerificationReport, _validate_joined


@dataclass(frozen=True)
class CommandVerificationReport(VerificationReport):
    # The common peak field is the maximum observed sequential-read scope
    # footprint. It is neither a lifetime peak nor an enforced allocation cap.
    measurement_scope: str
    command_resource_evidence: tuple[dict, ...]


@asynccontextmanager
async def command_transport(handle):
    reader = asyncio.StreamReader(limit=MAX_PROBE_MESSAGE)
    # Transfer ownership before an await: connection cancellation can close the
    # pipe, and its integer may already identify an unrelated descriptor later.
    read_fd, handle.stdout_fd = handle.stdout_fd, None
    pipe = None
    transport = None
    writer = None
    try:
        pipe = os.fdopen(read_fd, "rb", buffering=0)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), pipe)
        writer, handle.stdin_fd = handle.stdin_fd, None
        os.set_blocking(writer, False)
    except BaseException:
        if transport is not None:
            transport.close()
        if pipe is not None:
            pipe.close()
        else:
            os.close(read_fd)
        if writer is not None:
            os.close(writer)
        raise
    incoming_send, incoming = anyio.create_memory_object_stream[SessionMessage | Exception](1)
    outgoing, outgoing_receive = anyio.create_memory_object_stream[SessionMessage](1)

    async def read():
        async with incoming_send:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    if len(line) > MAX_PROBE_MESSAGE or not line.endswith(b"\n"):
                        raise ValueError()
                    await incoming_send.send(SessionMessage(JSONRPCMessage.model_validate_json(line)))
            except (OSError, ValueError):
                await incoming_send.send(PreparationError("invalid_preparation_probe_message"))

    async def write():
        async with outgoing_receive:
            async for message in outgoing_receive:
                data = message.message.model_dump_json(by_alias=True, exclude_none=True).encode() + b"\n"
                if len(data) > MAX_PROBE_MESSAGE:
                    raise PreparationError("preparation_probe_message_limit")
                view = memoryview(data)
                while view:
                    try:
                        view = view[os.write(writer, view):]
                    except BlockingIOError:
                        await asyncio.sleep(0.005)

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(read)
            tasks.start_soon(write)
            try:
                async with incoming, outgoing:
                    yield incoming, outgoing
            finally:
                tasks.cancel_scope.cancel()
    finally:
        os.close(writer)
        transport.close()


async def verify_command_package(package_root, descriptor, reaper, workspace, *, timeout_seconds=60, owner=None):
    if type(owner) is not CommandPreparationOwner:
        raise PreparationError("command_preparation_not_admitted")
    package = await _validate_joined(package_root)
    if package.digest != owner.package.digest or descriptor.digest != owner.descriptor.digest:
        raise PreparationError("preparation_candidate_mismatch")
    handle = None
    try:
        async with asyncio.timeout(timeout_seconds):
            handle = await owner.session(package)
            async with command_transport(handle) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    initialized = await client.initialize()
                    if (initialized.protocolVersion != "2025-11-25"
                            or set(initialized.capabilities.model_dump(exclude_none=True)) != {"tools"}):
                        raise PreparationError("unsupported_preparation_probe_capabilities")
                    listing = await client.list_tools()
                    if (listing.nextCursor is not None or
                            [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] !=
                            [tool.mcp_schema() for tool in descriptor.tools]):
                        raise PreparationError("preparation_tool_schema_mismatch")
                    for fixture in descriptor.validation:
                        result = await client.call_tool(fixture.tool, fixture.arguments)
                        if result.isError or canonical_bytes(result.structuredContent) != canonical_bytes(fixture.expected):
                            raise PreparationError("preparation_fixture_failed")
        await _join(owner.close())
        if not owner.confirm_completed():
            raise PreparationError("preparation_ownership_lost")
        current = await _validate_joined(package.root)
        if current.digest != package.digest:
            raise PreparationError("preparation_package_mutated")
        if owner.resource_admission is not None:
            from .command_resource_execution_v1 import read_resource_completion
            entries = owner.executor.journal["entries"]
            if len(owner.resource_reports) != len(descriptor.validation) or len(entries) != len(owner.resource_reports):
                raise PreparationError("command_resource_verification_failed")
            evidence = []
            for entry, (instance, spec_digest, measured) in zip(entries, owner.resource_reports):
                valid, code, recorded = read_resource_completion(owner.executor.state, entry)
                if (not valid or code != 0 or recorded != measured or entry["instance"] != instance
                        or entry["contract_digest"] != spec_digest or entry["state"] != "settled"):
                    raise PreparationError("command_resource_verification_failed")
                evidence.append({"instance": instance, "spec_digest": spec_digest,
                    "owner_journal_identity": owner.metadata["journal_identity"],
                    "fixture_digest": owner.metadata["fixture_digest"], "job_binding_digest": owner.metadata["job_binding_digest"],
                    "helper_sha256": entry["helper_digest"], "platform_policy": entry["policy_digest"],
                    "resource_policy_digest": entry["resource_policy_digest"], "report": asdict(measured)})
            return CommandVerificationReport(package.digest, len(listing.tools), len(descriptor.validation),
                initialized.protocolVersion, max(item[2].max_scope_window_footprint_bytes for item in owner.resource_reports),
                "held-group-plus-helper-windowed-footprint-v1", tuple(evidence))
        # Unmeasured format-3 probes retain the closed common attachment gate.
        return VerificationReport(package.digest, len(listing.tools), len(descriptor.validation), initialized.protocolVersion, 0)
    except TimeoutError:
        raise PreparationError("preparation_probe_timeout") from None
    finally:
        await _join(owner.close())
