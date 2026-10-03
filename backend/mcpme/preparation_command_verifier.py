"""Actual SDK probe over a trusted host command protocol session."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os

import anyio
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from .command_execution_v2 import _join
from .preparation_command_owner import CommandPreparationOwner
from .preparation_contracts import PreparationError, canonical_bytes
from .preparation_verifier import MAX_PROBE_MESSAGE, VerificationReport, _validate_joined


@asynccontextmanager
async def command_transport(handle):
    reader = asyncio.StreamReader(limit=MAX_PROBE_MESSAGE)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(handle.stdout_fd, "rb", buffering=0))
    handle.stdout_fd = None
    writer, handle.stdin_fd = handle.stdin_fd, None
    os.set_blocking(writer, False)
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
        # No aggregate process-tree memory measurement/enforcement exists here.
        # Zero means unavailable and must not be presented as a measured peak.
        return VerificationReport(package.digest, len(listing.tools), len(descriptor.validation), initialized.protocolVersion, 0)
    except TimeoutError:
        raise PreparationError("preparation_probe_timeout") from None
    finally:
        await _join(owner.close())
