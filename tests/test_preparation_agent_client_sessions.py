"""Generated apps serve an editor's discovery and execution clients together."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

from mcpme.contracts import Tool
from mcpme.preparation_agent_adapter import descriptor_for as locked_descriptor
from mcpme.preparation_agent_unlocked import descriptor_for as unlocked_descriptor
from mcpme.preparation_components import ComponentReference
from mcpme.preparation_contracts import RecipeReference, canonical_bytes
from mcpme.preparation_inputs import DescriptorView
from test_mcpme_gateway import live_gateway


# A complete synthetic one-pixel PNG, never an owner image or filesystem path.
IMAGE = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
         "/x8AAwMCAO+a7YQAAAAASUVORK5CYII=")


def generated_contract(factory, component):
    tool = Tool.model_validate({"name": "echo", "description": "Return supplied inline data.",
        "input_schema": {"type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False}})
    result = SimpleNamespace(digest="a" * 64,
        request=SimpleNamespace(recipe=RecipeReference(id="mcpme.test-adapter", version="1.0.3", digest="b" * 64),
            producer=ComponentReference(id=component, version="1.0.3", digest="c" * 64)),
        specification=SimpleNamespace(tool=tool, arguments={"value": IMAGE}, expected={"value": IMAGE}))
    descriptor = factory(result, "11111111-1111-4111-8111-111111111111",
        {"draft_id": "22222222-2222-4222-8222-222222222222", "name": "Inline image app"})
    view = DescriptorView(descriptor, (), canonical_bytes(descriptor.model_dump(exclude_none=True)))
    return view.expected_package_contract("sha256:" + "d" * 64, profile="mcp-stdio")


@asynccontextmanager
async def editor_client(endpoint, socket_path, limit):
    program = ("import asyncio; from pathlib import Path; from mcpme.bridge import run_bridge; "
        "asyncio.run(run_bridge(" + repr(endpoint) + ", 'secret', socket_path=Path("
        + repr(str(socket_path)) + "), max_request_bytes=" + str(limit) + "))")
    parameters = StdioServerParameters(command=sys.executable, args=["-c", program],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend")})
    with open(os.devnull, "w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.mark.parametrize("factory,component", [
    (locked_descriptor, "agent-python-adapter-v1"),
    (unlocked_descriptor, "agent-unlocked-python-adapter-v1"),
])
async def test_discovery_session_does_not_block_separate_image_execution(factory, component):
    contract = generated_contract(factory, component)
    lifecycle = contract["lifecycle"]
    # Historical descriptors still retain this immutable metadata; production
    # gateways no longer use it to limit connection admission.
    assert lifecycle["max_sessions"] == 2
    async with live_gateway(max_request_bytes=lifecycle["max_request_bytes"]) as (endpoint, owner, gateway):
        async with editor_client(endpoint, owner.socket_path, lifecycle["max_request_bytes"]) as discovery:
            assert (await discovery.list_tools()).tools[0].name == "echo"
            async with editor_client(endpoint, owner.socket_path, lifecycle["max_request_bytes"]) as execution:
                result = await execution.call_tool("echo", {"value": "data:image/png;base64," + IMAGE})
                assert not result.isError
                assert result.structuredContent == {"value": "data:image/png;base64," + IMAGE}
                assert len(owner.processes) == owner.launched == 2
                async with editor_client(endpoint, owner.socket_path, lifecycle["max_request_bytes"]) as third:
                    output = await third.call_tool("echo", {"value": IMAGE})
                    assert not output.isError and output.structuredContent == {"value": IMAGE}
                    assert len(owner.processes) == owner.launched == 3
                    assert (await discovery.list_tools()).tools[0].name == "echo"
            assert (await discovery.list_tools()).tools[0].name == "echo"
        for _ in range(100):
            if not gateway.sessions:
                break
            await asyncio.sleep(0.01)
        assert not gateway.sessions and not owner.processes
        assert len(owner.closed) == 3
