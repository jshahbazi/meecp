"""Real MCP/CPU probes for the one admitted text-generation composition.

The durable caller supplies the journaled owner. Only joined native completion
of both instances can return a report or release that caller's reservations.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import socket
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import PackageError
from .preparation_contracts import PreparationError, canonical_bytes
from .preparation_processes import _settle
from .preparation_textgen import model_contract, validate_model_package, task_fixtures
from .preparation_verifier import VerificationReport, _validate_joined
from .processes import ProcessOwner, OwnershipLost, _await_ownership, _watch_ownership
from mcpme_textgen.adapter import render_prompt


def require(value, code="textgen_probe_failed"):
    if not value:
        raise PreparationError(code)


def endpoint():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return {"host": "127.0.0.1", "port": port}, "http://127.0.0.1:" + str(port)


async def bounded_json(client, method, url, *, headers, payload=None):
    async with client.stream(method, url, headers=headers, json=payload) as response:
        require(response.status_code == 200)
        data = bytearray()
        async for chunk in response.aiter_bytes():
            require(len(data) + len(chunk) <= 1024**2, "textgen_probe_message_limit")
            data.extend(chunk)
    return json.loads(data)


async def verify_textgen_package(package_root: Path, descriptor, reaper: Path, workspace: Path, *,
                                 timeout_seconds=60, owner=None):
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600):
        raise PreparationError("invalid_preparation_probe_limit")
    started = time.monotonic()
    async with asyncio.timeout(timeout_seconds):
        package = await _validate_joined(package_root)
        validate_model_package(package, descriptor, require_receipt=True)
    require([f.model_dump(exclude_none=True) for f in descriptor.validation] == task_fixtures(),
            "textgen_fixture_contract_mismatch")
    owner = owner if owner is not None else ProcessOwner(reaper)
    processes = []
    try:
        async with asyncio.timeout(max(0, timeout_seconds - (time.monotonic() - started))):
            async def launch(role, dependencies):
                address, url = endpoint()
                key = secrets.token_urlsafe(32)
                keyfile = workspace / (role + ".key")
                with keyfile.open("x") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(key)
                config = {"model": model_contract(), "endpoint": address, "api_key": key,
                    "api_key_file": str(keyfile), "generation": 1, "nonce": secrets.token_hex(32),
                    "dependencies": dependencies}
                read, write = os.pipe()
                try:
                    data = canonical_bytes(config)
                    require(len(data) <= 4096)
                    os.write(write, data)
                finally:
                    os.close(write)
                try:
                    process = await owner.launch(package.root / ("bin/textgen-" + role),
                        ["--mcpme-config-fd", str(read)], package.root,
                        {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "TMPDIR": str(workspace), "LANG": "C"},
                        inherited_fds=(read,), owned_group=True, io_mode="fds",
                        startup_timeout=min(timeout_seconds, 120), stop_grace=.1)
                    processes.append(process)
                    process.close_stdio()
                finally:
                    os.close(read)
                async def readiness():
                    async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
                        while True:
                            require(process.returncode is None and not process.ownership_lost)
                            try:
                                result = await bounded_json(http, "GET", url + ("/props" if role == "engine" else "/health"),
                                    headers={"authorization": "Bearer " + key})
                                if role == "adapter":
                                    require(result == {"ready": True, "generation": 1, "nonce": config["nonce"]})
                                return
                            except (httpx.HTTPError, PreparationError):
                                await asyncio.sleep(.05)
                await _watch_ownership(process, readiness())
                return process, url, key

            engine, engine_url, engine_key = await launch("engine", {})
            adapter, adapter_url, adapter_key = await launch("adapter", {"engine": {
                "endpoint": engine_url, "api_key": engine_key, "instance_id": engine.instance_id, "pid": engine.pid}})

            async def probe():
                auth = {"authorization": "Bearer " + adapter_key}
                async with httpx.AsyncClient(trust_env=False, timeout=30, headers=auth) as http:
                    # Authentication and authority checks include the health route.
                    require((await http.get(adapter_url + "/health", headers={"authorization": "Bearer invalid"})).status_code == 401)
                    for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
                        require((await http.get(adapter_url + "/health", headers=extra)).status_code == 403)
                    async with streamable_http_client(adapter_url + "/mcp", http_client=http) as (read, write, _):
                        async with ClientSession(read, write) as client:
                            initialization = await client.initialize()
                            require(initialization.protocolVersion == "2025-11-25" and
                                set(initialization.capabilities.model_dump(exclude_none=True)) == {"tools"},
                                "unsupported_preparation_probe_capabilities")
                            listing = await client.list_tools()
                            require(listing.nextCursor is None and
                                [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] ==
                                [tool.mcp_schema() for tool in descriptor.tools], "preparation_tool_schema_mismatch")
                            for fixture in descriptor.validation:
                                result = await client.call_tool(fixture.tool, fixture.arguments)
                                require(not result.isError and canonical_bytes(result.structuredContent) == canonical_bytes(fixture.expected),
                                        "preparation_fixture_failed")
                            # Exact actual-token boundary uses the pinned tokenizer;
                            # token data never enters a report or durable journal.
                            text = None
                            async with httpx.AsyncClient(trust_env=False, timeout=10) as engine_http:
                                for count in range(380, 450):
                                    candidate = "x " * count + "\nList integers starting at one."
                                    tokens = await bounded_json(engine_http, "POST", engine_url + "/tokenize",
                                        headers={"authorization": "Bearer " + engine_key},
                                        payload={"content": render_prompt(candidate), "add_special": False, "parse_special": True})
                                    if len(tokens["tokens"]) == 440:
                                        text = candidate
                                        break
                            require(text is not None)
                            results = await asyncio.gather(*(client.call_tool("text_generate", {
                                "text": text, "max_output_tokens": 64}) for _ in range(4)))
                            require(all(not r.isError and r.structuredContent["input_tokens"] == 440
                                        and 0 < r.structuredContent["output_tokens"] <= 64 for r in results))
                            require(any(r.structuredContent["output_tokens"] == 64 for r in results))
                            for invalid in ({"text": text + " x", "max_output_tokens": 64},
                                            {"text": "é" * 4097, "max_output_tokens": 64},
                                            {"text": "synthetic", "max_output_tokens": 65}):
                                require((await client.call_tool("text_generate", invalid)).isError)
                            status = await client.call_tool("text_status", {})
                            model = model_contract()
                            require(not status.isError and status.structuredContent == {
                                "ready": True, "model_id": model["model_id"], "model_sha256": model["sha256"],
                                "profile": model["profile"], "context_tokens": 512, "max_output_tokens": 64,
                                "queue_depth": 0, "completed": len(descriptor.validation) + 4,
                                "generation": 1, "engine_instance_id": engine.instance_id})
                            return len(listing.tools), len(descriptor.validation) + 8
            tools, fixtures = await _watch_ownership(engine, _watch_ownership(adapter, probe()))
        for process in reversed(processes):
            await _settle(process)
        peaks = [p.peak_memory_bytes for p in processes]
        require(all(type(peak) is int and peak > 0 for peak in peaks), "preparation_probe_measurement_unavailable")
        require(sum(peaks) <= descriptor.resources.memory_mb * 1024**2, "preparation_probe_memory_limit")
        current = await _validate_joined(package.root)
        require(current.digest == package.digest, "preparation_package_mutated")
        return VerificationReport(package.digest, tools, fixtures, "2025-11-25", sum(peaks))
    except OwnershipLost:
        raise
    except TimeoutError:
        raise PreparationError("preparation_probe_timeout") from None
    except PreparationError:
        raise
    except Exception:
        raise PreparationError("textgen_probe_failed") from None
    finally:
        # One failed/lost instance must not prevent the other from being asked
        # to settle. ProcessOwner retains every unproven native ownership row.
        await _await_ownership(asyncio.gather(*(_settle(p) for p in reversed(processes)), return_exceptions=True))
        await _await_ownership(owner.close())
