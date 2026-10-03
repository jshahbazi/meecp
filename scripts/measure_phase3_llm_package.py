"""Native wait4 peak measurements for the exact sealed LLM package workload.

Runs the packaged engine/adapter under the existing native foreground-group
owner. This complements installed-host admission/lifecycle verification; it is
not an OS memory ceiling or a measurement of every possible model input.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)

from mcpme.contracts import validate_package
from mcpme.processes import ProcessOwner
from mcpme_textgen.adapter import LoopbackTransport, render_prompt
from mcpme_textgen.fixture import file_hash
from mcpme_textgen.service import model_declaration


def require(value):
    if not value:
        raise ValueError("verification_failed")


def endpoint():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return {"host": "127.0.0.1", "port": port}, "http://127.0.0.1:" + str(port)


async def verify(args, report):
    source = args.package.resolve()
    package_identity = validate_package(source)
    report["package_digest"] = package_identity.digest
    root = Path(tempfile.mkdtemp(prefix="mcpme-text-memory-")).resolve()
    owner = ProcessOwner(args.reaper.resolve())
    engine = adapter = None
    try:
        package = root / "relocated package"
        await asyncio.to_thread(shutil.copytree, source, package, symlinks=False)
        require(validate_package(package).digest == package_identity.digest)
        report["stage"] = "launch_packaged_processes"
        env = {"HOME": str(root), "TMPDIR": str(root), "PATH": "/usr/bin:/bin", "LANG": "C"}

        async def launch(role, dependencies):
            address, url = endpoint()
            key = secrets.token_urlsafe(32)
            keyfile = root / (role + ".key")
            keyfile.write_text(key)
            keyfile.chmod(0o600)
            config = {"model": model_declaration(), "endpoint": address, "api_key": key,
                      "api_key_file": str(keyfile), "generation": 1, "nonce": secrets.token_hex(32),
                      "dependencies": dependencies}
            read, write = os.pipe()
            data = json.dumps(config).encode()
            require(len(data) < 4096)
            os.write(write, data)
            os.close(write)
            try:
                process = await owner.launch(package / ("bin/textgen-" + role),
                    ["--mcpme-config-fd", str(read)], package, env, inherited_fds=(read,),
                    io_mode="fds", owned_group=True, stop_grace=.1)
                process.close_stdio()
            finally:
                os.close(read)
            async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
                async with asyncio.timeout(90):
                    while True:
                        require(process.returncode is None and not process.ownership_lost)
                        try:
                            response = await http.get(url + ("/props" if role == "engine" else "/health"),
                                                      headers={"authorization": "Bearer " + key})
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.05)
            return process, url, key

        engine, engine_url, engine_key = await launch("engine", {})
        adapter, adapter_url, adapter_key = await launch("adapter", {"engine": {
            "endpoint": engine_url, "api_key": engine_key, "instance_id": engine.instance_id, "pid": engine.pid}})
        transport = LoopbackTransport(engine_url, engine_key)
        text = None
        report["stage"] = "maximum_workload"
        for count in range(380, 450):
            candidate = "x " * count + "\nList integers starting at one."
            ids = await asyncio.to_thread(transport, "/tokenize", {"content": render_prompt(candidate),
                                          "add_special": False, "parse_special": True})
            if len(ids["tokens"]) == 440:
                text = candidate
                break
        require(text is not None)
        async with httpx.AsyncClient(trust_env=False, timeout=60,
            headers={"authorization": "Bearer " + adapter_key}) as http:
            async with streamable_http_client(adapter_url + "/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    start = time.monotonic()
                    results = await asyncio.gather(*(client.call_tool("text_generate", {
                        "text": text, "max_output_tokens": 64}) for _ in range(4)))
                    elapsed = time.monotonic() - start
                    values = [r.structuredContent for r in results]
                    require(all(not r.isError for r in results))
                    require(all(v["input_tokens"] == 440 and 0 < v["output_tokens"] <= 64 for v in values))
                    require(any(v["output_tokens"] == 64 for v in values) and elapsed < 60)
                    report["maximum_queue_seconds"] = round(elapsed, 3)
                    report["output_token_counts"] = [v["output_tokens"] for v in values]
                    for invalid in ({"text": text + " x", "max_output_tokens": 64},
                                    {"text": "x" * 8192, "max_output_tokens": 64},
                                    {"text": "é" * 4097, "max_output_tokens": 64}):
                        require((await client.call_tool("text_generate", invalid)).isError)
                    report["input_tokens"] = 440
                    report["context_and_byte_bound_cases"] = 3
        await adapter.stop()
        await engine.stop()
        require(not owner.instances and all(p.peak_memory_bytes and not p.ownership_lost for p in (engine, adapter)))
        measured = {"engine": engine.peak_memory_bytes, "adapter": adapter.peak_memory_bytes}
        report["foreground_peak_rss_bytes"] = measured
        report["sum_individual_peaks_bytes"] = sum(measured.values())
        report["declared_memory_bytes"] = package_identity.manifest.resources.memory_mb * 1024**2
        require(sum(measured.values()) < report["declared_memory_bytes"])
        report["stage"] = "complete"
    finally:
        await owner.close()
        report["ownership_settled"] = not owner.instances
        if not owner.instances:
            shutil.rmtree(root)
        else:
            report["retained_workspace"] = str(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--reaper", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"stage": "start", "harness_sha256": file_hash(Path(__file__))}
    code = 0
    try:
        asyncio.run(verify(args, report))
        require(file_hash(Path(__file__)) == report["harness_sha256"])
    except BaseException as error:
        report["error_type"] = type(error).__name__
        code = 1
    data = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.write_text(data)
    print(data, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
