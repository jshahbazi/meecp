"""Development-only installed command proof using one relocated private host.

Run with that host's Python -B -I. The fixture is an explicitly supplied compiled
synthetic executable. This harness never enables production admission and never
prints command payloads, responses, owner credentials, or exception details.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import sysconfig


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(value):
    if not value:
        raise ValueError("command_host_verification_failed")


def verify_payload(host):
    require(host.is_absolute() and host.resolve() == host)
    receipt = json.loads((host / "build.json").read_bytes())
    require(receipt["source_tree_dirty"] is False)
    require(set(receipt["artifact_sha256"]) == {path.relative_to(host).as_posix() for path in host.rglob("*")
                                               if path.is_file() and path != host / "build.json"})
    for name, expected in receipt["artifact_sha256"].items():
        path = host / name
        require(path.resolve() == path and path.is_relative_to(host) and sha(path) == expected)
    require({"command-execution.json", "bin/mcpme-command-reap"} <= receipt["artifact_sha256"].keys())
    inventory = json.loads((host / "command-execution.json").read_bytes())
    require(inventory["production_admission"] == "disabled" and inventory["configuration_authority"] == "inventory-only")
    require(inventory["receipt_format"] == 3 and inventory["package_schema"] == 2)
    require(inventory["helper"] == "bin/mcpme-command-reap" and sha(host / inventory["helper"]) == inventory["helper_sha256"])
    return receipt, inventory


def bootstrap(host):
    require(Path(sys.executable).resolve() == host / "python/bin/python3.12")
    require(sys.dont_write_bytecode)
    receipt, inventory = verify_payload(host)
    stdlib = Path(sysconfig.get_path("stdlib"))
    sys.path[:] = [str(host / "backend"), str(host / "python/site-packages"), str(stdlib), str(stdlib / "lib-dynload")]
    sys.dont_write_bytecode = True
    logging.disable(logging.CRITICAL)
    return receipt, inventory


def origins(host):
    for name, module in tuple(sys.modules.items()):
        if name == "mcpme" or name.startswith("mcpme."):
            base = host / "backend"
        elif name in {"mcp", "httpx", "pydantic", "jsonschema"} or name.startswith(("mcp.", "httpx.", "pydantic.", "jsonschema.")):
            base = host / "python/site-packages"
        else:
            continue
        path = getattr(module, "__file__", None)
        require(path is not None and Path(path).resolve().is_relative_to(base))


def make_host(host, state, inventory, *, enabled=True):
    from mcpme.host_command import HostCommandAdmission
    from mcpme.supervisor import Supervisor
    admission = HostCommandAdmission(host / inventory["helper"], inventory["helper_sha256"]) if enabled else None
    return Supervisor(state, host / "bin/mcpme-reap", command_admission=admission)


def package(base, fixture, inventory, mode):
    root = base / "package"
    root.mkdir(mode=0o700, parents=True)
    shutil.copyfile(fixture, root / "target"); (root / "target").chmod(0o700)
    (root / "LICENSE").write_text("Synthetic acceptance fixture, repository license.\n")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    manifest = {"schema_version": 2, "host_api_version": 1, "package_id": "test.bundled-command", "version": "1.0.0",
        "source_revision": "sha256:" + sha(fixture), "name": "Synthetic command", "profile": "host-command",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "command": {"version": 1, "component": inventory["component"], "helper_sha256": inventory["helper_sha256"],
                    "commands": {"probe": {"executable": "target", "args": [mode], "cwd": "."}},
                    "deadline_ms": 3000, "input_bytes": 512, "output_bytes": 512, "queue_limit": 2},
        "lifecycle": {"startup_timeout_seconds": 5, "operation_timeout_seconds": 5},
        "tools": [{"name": "probe", "description": "Synthetic bounded command", "input_schema": schema, "output_schema": schema}],
        "resources": {"memory_mb": 32, "cpu_only": True}, "licenses": ["LICENSE"]}
    (root / "mcpme.package.json").write_text(json.dumps(manifest))
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
        "manifest_sha256": sha(root / "mcpme.package.json"), "artifacts": [{"path": name, "sha256": sha(root / name),
            "size": (root / name).stat().st_size, "executable": name == "target", "source": manifest["source_revision"],
            "provenance": "Explicit synthetic native fixture", "license": "LICENSE"} for name in ("target", "LICENSE")],
        "build": {"recipe": "synthetic-bundled-command-v1", "builder": "acceptance", "toolchain": "explicit clang fixture"}}
    (root / "mcpme.lock.json").write_text(json.dumps(lock))
    return root


async def install(host, source):
    plan = host.plan_install({"package": str(source), "start": True})
    operation = await host.apply_install(plan["plan_id"])
    async with asyncio.timeout(20):
        while True:
            value = host.registry.get_operation(operation["operation_id"])
            if value.status not in {"pending", "running"}:
                require(value.status == "completed")
                return host.registry.get_slot(value.slot_id)
            await asyncio.sleep(0.01)


async def http_call(host, slot):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    connection = host.connection(slot.id)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                require([t.model_dump(by_alias=True, exclude_none=True) for t in tools.tools] ==
                        [t.mcp_schema() for t in host.package(slot).manifest.tools])
                result = await client.call_tool("probe", {"ok": True})
                require(result.structuredContent == {"ok": True})


async def stdio_call(host_path, host, slot):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=str(host_path / "bin/mcpme"), args=["--state-dir", str(host.root), "connect", "--slot", slot.id])
    with open(os.devnull, "w") as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                require([t.model_dump(by_alias=True, exclude_none=True) for t in tools.tools] ==
                        [t.mcp_schema() for t in host.package(slot).manifest.tools])
                require((await client.call_tool("probe", {"ok": True})).structuredContent == {"ok": True})


async def held_call(host, slot):
    handle = await host.commands.session(slot)
    for message in [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "probe", "arguments": {"ok": True}}}]:
        os.write(handle.stdin_fd, json.dumps(message).encode() + b"\n")
    executor = host.commands.executors[slot.id]
    async with asyncio.timeout(3):
        while executor._active is None:
            await asyncio.sleep(0.001)
    native = executor._active
    require(os.getpgid(native.pid) == native.pid and native.pid != os.getpgrp())
    return handle, native


async def worker(args, inventory):
    host = make_host(args.host, args.workspace / "state", inventory)
    slot = await install(host, args.workspace / "package")
    await held_call(host, slot)
    # This private parent pipe carries only the generated slot identity.
    print(slot.id, flush=True)
    await asyncio.sleep(30)
    await host.close()


async def verify(args, receipt, inventory):
    from mcpme.supervisor import HostError
    for mode in ("echo", "tree", "fds", "escape"):
        base = args.workspace / mode
        source = package(base, args.fixture, inventory, mode)
        closed = make_host(args.host, base / "closed-state", inventory, enabled=False)
        try:
            try:
                closed.plan_install({"package": str(source)})
            except HostError as error:
                require(error.code == "host_command_not_admitted")
            else:
                require(False)
        finally:
            await closed.close()
        host = make_host(args.host, base / "state", inventory)
        try:
            slot = await install(host, source)
            shutil.rmtree(source)
            await http_call(host, slot)
            await stdio_call(args.host, host, slot)
            require(not host.commands.pending(slot.id))
        finally:
            await host.close()
    base = args.workspace / "cancel"
    source = package(base, args.fixture, inventory, "hang")
    host = make_host(args.host, base / "state", inventory)
    try:
        slot = await install(host, source)
        handle, native = await held_call(host, slot)
        await handle.close()
        require(native.poll() in (0, 64) and not host.commands.pending(slot.id))
    finally:
        await host.close()
    for group in (False, True):
        base = args.workspace / ("group-kill" if group else "owner-kill")
        package(base, args.fixture, inventory, "hang")
        process = subprocess.Popen([str(args.host / "python/bin/python3.12"), "-B", "-I", str(Path(__file__).resolve()),
            "--host", str(args.host), "--workspace", str(base), "--worker"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 30)
            slot_id = line.decode().strip()
            import uuid
            require(str(uuid.UUID(slot_id)) == slot_id)
            if group:
                os.killpg(process.pid, signal.SIGKILL)  # Held live direct-child group leader.
            else:
                process.kill()
            await asyncio.to_thread(process.wait, 5)
            restarted = make_host(args.host, base / "state", inventory)
            try:
                async with asyncio.timeout(8):
                    while restarted.commands.pending(slot_id):
                        await asyncio.sleep(0.02)
                require(restarted.commands.executors[slot_id].journal["entries"][-1]["state"] == "settled")
                require(not restarted.commands.records[slot_id])
                if not group:
                    await restarted.initialize()
                    require(slot_id not in restarted.reservations)
            finally:
                await restarted.close()
        finally:
            if process.poll() is None:
                process.kill(); await asyncio.to_thread(process.wait, 5)
            process.stdout.close()
    origins(args.host)
    return {"verified": True, "scope": "development-relocated-cli-command-host", "source_commit": receipt["source_commit"],
        "build_receipt_sha256": sha(args.host / "build.json"), "helper_sha256": inventory["helper_sha256"],
        "private_python": True, "private_sdk": True, "http_stdio_full_schema": True, "fork_exec_settlement": True,
        "forbidden_escape_denied": True, "cancel_joined": True, "owner_sigkill_restart": True, "ancestor_group_sigkill": True,
        "production_admission": "disabled", "aggregate_os_limits_proven": False, "signed_native_app_proven": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    try:
        receipt, inventory = bootstrap(args.host)
        if args.worker:
            asyncio.run(worker(args, inventory))
        else:
            require(args.fixture is not None and args.fixture.is_file() and args.fixture.resolve() == args.fixture)
            args.workspace.mkdir(mode=0o700)
            result = asyncio.run(verify(args, receipt, inventory))
            print(json.dumps(result, sort_keys=True))
    except (Exception, KeyboardInterrupt):
        raise SystemExit("command_host_verification_failed") from None


if __name__ == "__main__":
    main()
