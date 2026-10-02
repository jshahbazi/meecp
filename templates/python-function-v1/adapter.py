"""One selected Python callable, tools-only MCP, with sanitized failures."""
from __future__ import annotations

import functools
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
import sys

import anyio
from jsonschema import Draft202012Validator
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool


def failure(code: str) -> CallToolResult:
    return CallToolResult(isError=True, content=[TextContent(type="text", text=code)])


async def serve():
    # Keep the sole MCP writer separate from user code's Python/native stdout.
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    with open(os.devnull, "wb") as discarded:
        os.dup2(discarded.fileno(), 1)
        os.dup2(discarded.fileno(), 2)
    logging.disable(logging.CRITICAL)
    configuration = json.loads((Path(__file__).resolve().parent / "callable.json").read_bytes())
    declaration = configuration["tool"]
    tool = Tool(name=declaration["name"], description=declaration["description"],
                inputSchema=declaration["input_schema"], outputSchema=declaration.get("output_schema"),
                annotations=declaration.get("annotations"))
    function = getattr(importlib.import_module(configuration["module"]), configuration["function"])
    if not callable(function):
        raise ValueError("invalid_python_callable")
    input_validator = Draft202012Validator(tool.inputSchema)
    output_validator = Draft202012Validator(tool.outputSchema) if tool.outputSchema else None
    lock = anyio.Lock()
    server = Server("mcpme-python-function", version="1.0.0")

    @server.list_tools()
    async def list_tools():
        return [tool]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict | None):
        if name != tool.name:
            return failure("unknown_tool")
        if not isinstance(arguments, dict) or not input_validator.is_valid(arguments):
            return failure("invalid_tool_arguments")
        async with lock:
            try:
                if inspect.iscoroutinefunction(function):
                    result = await function(**arguments)
                else:
                    # Shield cancellation until a synchronous callable returns. The
                    # host deadline/reaper bounds a noncooperative function process.
                    result = await anyio.to_thread.run_sync(functools.partial(function, **arguments),
                                                            abandon_on_cancel=False)
                if not isinstance(result, dict) or (output_validator and not output_validator.is_valid(result)):
                    return failure("invalid_tool_result")
                encoded = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > 512 * 1024:
                    return failure("tool_result_too_large")
                return CallToolResult(content=[TextContent(type="text", text=encoded)],
                                      structuredContent=result, isError=False)
            except Exception:
                return failure("tool_execution_failed")

    async with stdio_server(stdout=anyio.wrap_file(protocol_output)) as (read_stream, write_stream):
        initialization = server.create_initialization_options()
        # The locked SDK otherwise emits experimental:{}, which exceeds the
        # host's explicitly tools-only capability profile.
        initialization.capabilities.experimental = None
        await server.run(read_stream, write_stream, initialization)


if __name__ == "__main__":
    try:
        anyio.run(serve)
    except Exception:
        # Import/configuration/transport errors must not dump source or values.
        raise SystemExit(2) from None
