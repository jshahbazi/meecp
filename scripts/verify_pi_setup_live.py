"""Run one paid OpenAI Pi setup and disposable synthetic app acceptance check.

Build the host separately. This CLI uses its bundled backend/runtime unchanged,
keeps each run in fresh private state, and emits only acceptance metadata.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import platform
import signal
import stat
import sys
import uuid
import warnings

ROOT = Path(__file__).resolve().parents[1]
STAGES = {"starting", "thinking", "reading", "writing", "downloading", "building", "testing", "verifying", "ready"}


class AcceptanceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def emit(event, **metadata):
    print(json.dumps({"event": event, **metadata}, separators=(",", ":")), flush=True)


def require(condition, code):
    if not condition:
        raise AcceptanceError(code)


def key_from_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        require(stat.S_ISREG(details.st_mode) and details.st_nlink == 1
                and 0 < details.st_size <= 8192, "acceptance_key_file_invalid")
        data = stream.read(8193)
    key = data.decode("utf-8").strip()
    require(1 <= len(key) <= 4096 and all(33 <= ord(char) <= 126 for char in key),
            "acceptance_key_file_invalid")
    return key


def bundled_host(path):
    root = path.resolve(strict=True)
    require(root.is_dir(), "acceptance_host_missing")
    for name in ("bin/mcpme-reap", "backend/mcpme/pi_setup.py", "templates/pi_package.py",
                 "pi-runtime/inventory.json", "pi-runtime/bin/node", "pi-runtime/worker.mjs"):
        require((root / name).is_file(), "acceptance_host_incomplete")
    require((root / "python/site-packages").is_dir(), "acceptance_host_incomplete")
    return root


def synthetic_source(base):
    source = base / "source"
    source.mkdir(mode=0o700)
    (source / "normalizer.py").write_text(
        "def normalize_text(text):\n"
        "    words = text.split()\n"
        "    return {'normalized': ' '.join(words).casefold(), 'word_count': len(words)}\n")
    (source / "LICENSE").write_text(
        "Synthetic acceptance source dedicated to the public domain under CC0-1.0.\n"
        "To the extent possible under law, its authors waive copyright and related rights.\n"
        "https://creativecommons.org/publicdomain/zero/1.0/\n")
    return source


async def gateway_fixtures(host, slot_id):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    connection = host.connection(slot_id)
    fixtures = (("  HeLLo\n WORLD  ", {"normalized": "hello world", "word_count": 2}),
                ("", {"normalized": "", "word_count": 0}),
                ("Straße", {"normalized": "strasse", "word_count": 1}))
    async with asyncio.timeout(90):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]},
                                     trust_env=False, timeout=45) as http:
            async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    require(initialized.protocolVersion == "2025-11-25", "acceptance_gateway_protocol_failed")
                    for text, expected in fixtures:
                        result = await client.call_tool("normalize_text", {"text": text})
                        require(not result.isError and result.structuredContent == expected,
                                "acceptance_gateway_fixture_failed")


async def acceptance(args):
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "acceptance_platform_unsupported")
    bundle = bundled_host(args.host_root)
    # Exercise the supplied built host, rather than a newer checkout's modules.
    sys.path[:0] = [str(bundle / "backend"), str(bundle / "python/site-packages")]
    from mcpme.supervisor import Supervisor
    from mcpme.private import ensure_private_directory

    parent = args.run_root.resolve()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_private_directory(parent)
    run_id = str(uuid.uuid4())
    base = parent / run_id
    base.mkdir(mode=0o700)
    source = synthetic_source(base)
    host, job_id, cleanup_failed = None, None, False
    try:
        host = Supervisor(base / "state", bundle / "bin/mcpme-reap")
        await host.initialize()
        key = key_from_file(args.key_file)
        try:
            host.pi_setup.save_settings({"provider": "openai", "model": args.model, "api_key": key})
        finally:
            del key
        draft = host.imports.create(source, name="Text Normalizer", intent=
            "Expose normalizer.py normalize_text(text) as the normalize_text MCP tool. "
            "Use the selected implementation. Accept one string text. Return its exact normalized and word_count fields. "
            "Build a self-contained Python package. Test whitespace, case folding, and empty strings.", start=True)
        job = await host.pi_setup.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
        job_id = job["id"]
        emit("started", run_id=run_id)
        previous = None
        task = host.pi_setup.tasks[job_id]
        while not task.done():
            current = host.pi_setup.get(job_id)
            stage = current["stage"]
            if stage != previous and stage in STAGES:
                emit("progress", stage=stage)
                previous = stage
            await asyncio.sleep(1)
        await task
        current = host.pi_setup.get(job_id)
        require(current["status"] == "ready" and current["package"] is not None, "acceptance_setup_failed")
        emit("package_verified", attached=True)
        plan = await host.plan_install_async({"import_id": draft["draft_id"], "revision": draft["revision"]})
        receipt = await host.apply_install(plan["plan_id"])
        async with asyncio.timeout(90):
            while True:
                operation = host.registry.get_operation(receipt["operation_id"])
                if operation.status not in {"pending", "running"}:
                    break
                await asyncio.sleep(0.1)
        require(operation.status == "completed", "acceptance_install_failed")
        emit("installed", status="completed")
        await gateway_fixtures(host, operation.slot_id)
        emit("gateway_verified", fixtures=3)
    finally:
        if host is not None:
            try:
                if job_id is not None and not host.pi_setup.tasks[job_id].done():
                    await host.pi_setup.cancel(job_id)
            except BaseException:
                cleanup_failed = True
            try:
                # This fresh run owns every secret record in its disposable state.
                # Remove the provider value while retaining inspectable artifacts.
                for secret in host.pi_setup.secrets.list():
                    host.pi_setup.secrets.revoke(secret["secret_id"])
            except BaseException:
                cleanup_failed = True
            try:
                await host.close()
            except BaseException:
                cleanup_failed = True
            if cleanup_failed:
                raise AcceptanceError("acceptance_cleanup_failed") from None
    return {"fixtures": 3, "scope": "synthetic_normalizer", "run_id": run_id}


async def run(args):
    loop, stopped = asyncio.get_running_loop(), asyncio.Event()
    originals = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        originals[sig] = signal.getsignal(sig)
        loop.add_signal_handler(sig, stopped.set)
    workflow = asyncio.create_task(acceptance(args))
    interrupt = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait({workflow, interrupt}, timeout=args.timeout_seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        if workflow not in done:
            workflow.cancel()
            outcomes = await asyncio.gather(workflow, return_exceptions=True)
            if isinstance(outcomes[0], AcceptanceError) and outcomes[0].code == "acceptance_cleanup_failed":
                raise outcomes[0]
            raise AcceptanceError("acceptance_canceled" if interrupt in done else "acceptance_timeout")
        return await workflow
    finally:
        interrupt.cancel()
        await asyncio.gather(interrupt, return_exceptions=True)
        if not workflow.done():
            workflow.cancel()
            await asyncio.gather(workflow, return_exceptions=True)
        for sig, original in originals.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, original)


class Parser(argparse.ArgumentParser):
    def error(self, _):
        raise AcceptanceError("acceptance_arguments_invalid")


def main(argv=None):
    logging.disable(logging.CRITICAL)
    warnings.filterwarnings("ignore")
    os.umask(0o077)
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--key-file", type=Path, required=True, help="OpenAI key file; its value is never emitted.")
        parser.add_argument("--model", default="gpt-5.4", help="Exact supported OpenAI model ID (default: gpt-5.4).")
        parser.add_argument("--host-root", type=Path, required=True, help="Built host directory, including backend, Python, Pi, and reaper.")
        parser.add_argument("--run-root", type=Path, default=ROOT / "test-artifacts/pi-live",
                            help="Parent directory for a fresh private UUID run (default: ignored test-artifacts/pi-live).")
        parser.add_argument("--timeout-seconds", type=int, default=1800, help="Whole-run deadline, including setup (default: 1800).")
        args = parser.parse_args(argv)
        require(1 <= args.timeout_seconds <= 3600, "acceptance_arguments_invalid")
        report = asyncio.run(run(args))
    except AcceptanceError as error:
        emit("acceptance_failed", code=error.code)
        return 1
    except KeyboardInterrupt:
        emit("acceptance_failed", code="acceptance_canceled")
        return 1
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    except BaseException:
        emit("acceptance_failed", code="acceptance_failed")
        return 1
    emit("acceptance_passed", **report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
