"""Generated local service adapter. Secrets arrive only on the inherited FD."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time

import anyio
from jsonschema import Draft202012Validator
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .protocol import completion, strict_json, ServiceFailure


def settings():
    raw = os.environ.pop("MCPME_SETTINGS_FD", "")
    if not raw.isdecimal() or not 3 <= int(raw) <= 1048576:
        raise ServiceFailure("service_settings_required")
    with socket.socket(fileno=int(raw)) as channel:
        deadline = time.monotonic() + 10
        data = bytearray()
        while len(data) <= 524288:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceFailure("service_settings_invalid")
            channel.settimeout(remaining)
            chunk = channel.recv(min(65536, 524289 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 524288:
            raise ServiceFailure("service_settings_invalid")
    value = strict_json(data)
    if (type(value) is not dict or set(value) != {"schema_version", "configuration", "secrets"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["configuration"] != {} or type(value["secrets"]) is not dict or set(value["secrets"]) != {"api_key"}):
        raise ServiceFailure("service_settings_invalid")
    return value["secrets"]["api_key"]


async def serve(root):
    protocol_output = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    with open(os.devnull, "wb") as discarded:
        os.dup2(discarded.fileno(), 1); os.dup2(discarded.fileno(), 2)
    logging.disable(logging.CRITICAL)
    credential = settings()
    configuration = strict_json((root / "service.json").read_bytes())
    manifest = strict_json((root / "mcpme.package.json").read_bytes())
    declarations = [Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"],
        outputSchema=t["output_schema"], annotations=t.get("annotations")) for t in manifest["tools"]]
    validators = {tool.name: Draft202012Validator(tool.inputSchema) for tool in declarations}
    lock = asyncio.Lock()
    server = Server("mcpme-local-service", version="1.0.0")

    @server.list_tools()
    async def list_tools():
        return declarations

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        def failure(code):
            return CallToolResult(isError=True, content=[TextContent(type="text", text=code)])
        if name not in validators or type(arguments) is not dict or not validators[name].is_valid(arguments):
            return failure("service_arguments_invalid")
        if lock.locked():
            return failure("service_busy")
        try:
            # Serialize compatibility checks and generation against the same
            # selected endpoint. Cancellation settles only this owned socket.
            async with lock:
                if name == "check_connection":
                    await completion(configuration, credential, {"prompt": "Hi"[:configuration["limits"]["max_input_bytes"]],
                        "max_output_tokens": min(16, configuration["limits"]["max_output_tokens"])})
                    result = {"compatible": True}
                else:
                    result = await completion(configuration, credential, arguments)
            response = CallToolResult(isError=False, structuredContent=result,
                content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, allow_nan=False))])
            # Both structured and text representations share one bounded MCP
            # envelope. A hostile service cannot exceed the gateway's framing.
            if len(response.model_dump_json(by_alias=True, exclude_none=True).encode()) > 1044480:
                return failure("service_response_limit")
            return response
        except ServiceFailure as error:
            return failure(error.code)
        except Exception:
            return failure("service_call_failed")

    async with stdio_server(stdout=anyio.wrap_file(protocol_output)) as (incoming, outgoing):
        initialization = server.create_initialization_options()
        initialization.capabilities.experimental = None
        await server.run(incoming, outgoing, initialization)


def main(root):
    try:
        asyncio.run(serve(Path(root)))
    except Exception:
        raise SystemExit(2) from None
