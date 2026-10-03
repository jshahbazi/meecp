"""Exact tools-only MCP surface for the pinned ONNX hotdog task."""
from __future__ import annotations

import functools
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

from detector import DetectorError, detect_hotdog


def failure(code):
    return CallToolResult(isError=True, content=[TextContent(type="text", text=code)])


async def serve():
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    with open(os.devnull, "wb") as discarded:
        os.dup2(discarded.fileno(), 1)
        os.dup2(discarded.fileno(), 2)
    logging.disable(logging.CRITICAL)
    declaration = json.loads((Path(__file__).parent / "tool.json").read_bytes())
    tool = Tool(name=declaration["name"], description=declaration["description"], inputSchema=declaration["input_schema"],
                outputSchema=declaration["output_schema"], annotations=declaration["annotations"])
    input_validator, output_validator = Draft202012Validator(tool.inputSchema), Draft202012Validator(tool.outputSchema)
    server, lock = Server("mcpme-onnx-hotdog", version="1.0.0"), anyio.Lock()

    @server.list_tools()
    async def list_tools():
        return [tool]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name != tool.name:
            return failure("unknown_tool")
        if type(arguments) is not dict or not input_validator.is_valid(arguments):
            return failure("invalid_tool_arguments")
        try:
            lock.acquire_nowait()
        except anyio.WouldBlock:
            return failure("detector_busy")
        try:
            try:
                result = await anyio.to_thread.run_sync(functools.partial(detect_hotdog, **arguments), abandon_on_cancel=False)
                if not output_validator.is_valid(result):
                    return failure("invalid_tool_result")
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, allow_nan=False,
                                      separators=(",", ":")))], structuredContent=result, isError=False)
            except DetectorError as error:
                return failure(str(error))
            except Exception:
                return failure("tool_execution_failed")
        finally:
            lock.release()

    async with stdio_server(stdout=anyio.wrap_file(protocol_output)) as (read_stream, write_stream):
        initialization = server.create_initialization_options()
        initialization.capabilities.experimental = None
        await server.run(read_stream, write_stream, initialization)


if __name__ == "__main__":
    try:
        anyio.run(serve)
    except Exception:
        raise SystemExit(2) from None
