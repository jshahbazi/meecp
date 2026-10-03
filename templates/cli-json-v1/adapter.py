"""One bounded foreground JSON command, served through tools-only MCP."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sys

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from mcpme.cli_execution import CliExecutionError, CliExecutor
from mcpme.preparation_cli import parse_cli_spec


def failure(code):
    return CallToolResult(isError=True, content=[TextContent(type="text", text=code)])


async def serve():
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    with open(os.devnull, "wb") as discarded:
        os.dup2(discarded.fileno(), 1)
        os.dup2(discarded.fileno(), 2)
    logging.disable(logging.CRITICAL)
    root = Path(__file__).resolve().parent
    state = os.environ.get("MCPME_CLI_STATE_DIRECTORY")
    if not state or not Path(state).is_absolute() or Path(state).absolute().is_relative_to(root):
        raise CliExecutionError("cli_state_unavailable")
    spec = parse_cli_spec((root / "cli.json").read_bytes())
    memory_mb = json.loads((root / "mcpme.package.json").read_bytes())["resources"]["memory_mb"]
    executor = CliExecutor(root, Path(state), root / "bin/mcpme-reap", root / "bin/mcpme-cli-launch", memory_mb=memory_mb)
    tool = Tool(**spec.tool.mcp_schema())
    server = Server("mcpme-cli-json", version="1.0.0")

    @server.list_tools()
    async def list_tools():
        return [tool]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name != tool.name:
            return failure("unknown_tool")
        try:
            result = await executor.execute(spec, arguments)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result.value, allow_nan=False,
                ensure_ascii=False, separators=(",", ":")))], structuredContent=result.value, isError=False)
        except CliExecutionError as error:
            return failure(error.code)
        except Exception:
            return failure("cli_runtime_failed")

    try:
        async with stdio_server(stdout=anyio.wrap_file(protocol_output)) as (read_stream, write_stream):
            initialization = server.create_initialization_options()
            initialization.capabilities.experimental = None
            await server.run(read_stream, write_stream, initialization)
    finally:
        await executor.close()


if __name__ == "__main__":
    try:
        anyio.run(serve)
    except Exception:
        raise SystemExit(2) from None
