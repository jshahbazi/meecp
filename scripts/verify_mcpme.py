"""Verify a relocated CLI host using only synthetic protocol data and safe metadata."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


CHECKS = (
    "host_relocated", "minimal_environment", "cli_capabilities", "package_validation",
    "source_snapshot_independent", "two_slots", "distinct_ports", "http_sdk", "stdio_sdk",
    "credential_isolation", "management_isolation", "export_no_credentials",
    "independent_stop", "stable_restart", "shutdown_connection_removed", "shutdown_owned_ledgers_empty",
)


def private_json(path: Path):
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
        raise ValueError("invalid_private_state")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("invalid_private_state")
    return value


async def verify(host: Path, package: Path, report: dict):
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="mcpme-relocation-") as directory:
        root = Path(directory).resolve()
        relocated = root / "relocated host"
        await asyncio.to_thread(shutil.copytree, host, relocated, symlinks=True)
        checks["host_relocated"] = True
        state, home = root / "private state", root / "private home"
        state.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(root),
               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
        launcher = relocated / "bin/mcpme"

        async def cli(*arguments):
            process = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), *arguments,
                env=env, cwd=home, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 30)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                await process.wait()
                raise
            if process.returncode != 0 or len(stdout) > 1024 * 1024:
                raise ValueError("cli_failed")
            value = json.loads(stdout)
            if not isinstance(value, dict) or "error" in value or value.get("api_version") != 1:
                raise ValueError("cli_failed")
            return value

        async def finish(operation):
            async with asyncio.timeout(30):
                while True:
                    value = await cli("operations", "get", operation["operation_id"], "--json")
                    if value["status"] == "completed":
                        return value
                    if value["status"] not in {"pending", "running"}:
                        raise ValueError("operation_failed")
                    await asyncio.sleep(0.05)

        async def http_call(connection, document, expected):
            async with httpx.AsyncClient(trust_env=False, timeout=5,
                    headers={"authorization": "Bearer " + connection["token"]}) as http:
                async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        initialized = await client.initialize()
                        if initialized.protocolVersion != "2025-11-25":
                            raise ValueError("protocol_mismatch")
                        listed = await client.list_tools()
                        if [tool.name for tool in listed.tools] != ["validate_json"]:
                            raise ValueError("tools_mismatch")
                        result = await client.call_tool("validate_json", {"document": document})
                        if result.structuredContent != expected:
                            raise ValueError("result_mismatch")
                        report["http_calls"] += 1

        supervisor = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), "serve",
            "--reaper", str(relocated / "bin/mcpme-reap"), env=env, cwd=home,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            manager_path = state / "manager-connection.json"
            async with asyncio.timeout(15):
                while not manager_path.is_file():
                    if supervisor.returncode is not None:
                        raise ValueError("host_failed")
                    await asyncio.sleep(0.05)
            manager = private_json(manager_path)
            caps = await cli("capabilities", "--json")
            if not caps:
                raise ValueError("capabilities_missing")
            checks["minimal_environment"] = checks["cli_capabilities"] = True
            if not (await cli("validate", "--package", str(package), "--json"))["valid"]:
                raise ValueError("package_invalid")
            checks["package_validation"] = True
            preparation = root / "prepared source"
            await asyncio.to_thread(shutil.copytree, package, preparation, symlinks=True)
            plans = [await cli("install", "plan", "--package", str(preparation), "--name", name, "--start", "--json")
                     for name in ("Relocation One", "Relocation Two")]
            moved = root / "source renamed after planning"
            preparation.rename(moved)
            (moved / "mcpme.package.json").write_text("changed after the package snapshot")
            slots = []
            for plan in plans:
                applied = await cli("install", "apply", "--plan", plan["plan_id"], "--json")
                repeated = await cli("install", "apply", "--plan", plan["plan_id"], "--json")
                if repeated["operation_id"] != applied["operation_id"]:
                    raise ValueError("apply_not_idempotent")
                operation = await finish(applied)
                slots.append(await cli("slots", "inspect", operation["slot_id"], "--json"))
            checks["source_snapshot_independent"] = True
            listed = await cli("slots", "list", "--json")
            if len(listed["slots"]) != 2 or slots[0]["slot_id"] == slots[1]["slot_id"]:
                raise ValueError("slot_identity_mismatch")
            report["slots"] = 2
            checks["two_slots"] = True
            if slots[0]["public_port"] == slots[1]["public_port"]:
                raise ValueError("ports_not_distinct")
            checks["distinct_ports"] = True
            connections = [private_json(state / "slots" / slot["slot_id"] / "connection.json") for slot in slots]
            await asyncio.gather(http_call(connections[0], '{"synthetic":true}', {"valid": True, "kind": "object"}),
                                 http_call(connections[1], "[1,2]", {"valid": True, "kind": "array"}))
            checks["http_sdk"] = True
            async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
                wrong = await http.get(connections[1]["endpoint"],
                    headers={"authorization": "Bearer " + connections[0]["token"]})
                if wrong.status_code != 401:
                    raise ValueError("slot_authority_failure")
                checks["credential_isolation"] = True
                wrong = await http.get(manager["endpoint"] + "/v1/slots",
                    headers={"authorization": "Bearer " + connections[0]["token"]})
                if wrong.status_code != 401:
                    raise ValueError("manager_authority_failure")
                checks["management_isolation"] = True
            exported = await cli("connections", "export", "--slot", slots[1]["slot_id"], "--json")
            serialized = json.dumps(exported)
            if any(connection["token"] in serialized for connection in [manager, *connections]) or '"token"' in serialized:
                raise ValueError("credential_exported")
            config = exported["configuration"]["mcpServers"][slots[1]["slot_id"]]
            if Path(config["command"]) != launcher:
                raise ValueError("launcher_not_relocated")
            checks["export_no_credentials"] = True
            with open(os.devnull, "w") as errlog:
                parameters = StdioServerParameters(command=config["command"], args=config["args"], env=env, cwd=str(home))
                async with stdio_client(parameters, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("validate_json", {"document": "null"})
                        if result.structuredContent != {"valid": True, "kind": "null"}:
                            raise ValueError("stdio_result_mismatch")
                        report["stdio_calls"] += 1
            checks["stdio_sdk"] = True
            await finish(await cli("slots", "stop", slots[0]["slot_id"], "--json"))
            await http_call(connections[1], "false", {"valid": True, "kind": "boolean"})
            async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
                stopped = await http.get(connections[0]["endpoint"], headers={"authorization": "Bearer " + connections[0]["token"]})
                if stopped.status_code != 409 or stopped.json().get("error", {}).get("code") != "slot_stopped":
                    raise ValueError("stopped_slot_responsive")
            checks["independent_stop"] = True
            await finish(await cli("slots", "start", slots[0]["slot_id"], "--json"))
            await finish(await cli("slots", "restart", slots[0]["slot_id"], "--json"))
            restarted = await cli("slots", "inspect", slots[0]["slot_id"], "--json")
            if restarted["public_port"] != slots[0]["public_port"]:
                raise ValueError("port_changed")
            await http_call(connections[0], "invalid", {"valid": False, "kind": "invalid"})
            checks["stable_restart"] = True
        finally:
            if supervisor.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    supervisor.terminate()
            await asyncio.wait_for(supervisor.wait(), 15)
            checks["shutdown_connection_removed"] = not (state / "manager-connection.json").exists()
            ledgers = list((state / "slots").glob("*/ownership.json"))
            checks["shutdown_owned_ledgers_empty"] = bool(ledgers) and all(private_json(path) == {} for path in ledgers)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"api_version": 1, "verified": False, "checks": dict.fromkeys(CHECKS, False),
              "slots": 0, "http_calls": 0, "stdio_calls": 0}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.package.resolve(strict=True), report))
        report["verified"] = all(report["checks"].values())
    except (Exception, KeyboardInterrupt):
        pass
    value = json.dumps(report, sort_keys=True) + "\n"
    if args.output:
        try:
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(value)
        except Exception:
            report["verified"] = False
            value = json.dumps(report, sort_keys=True) + "\n"
    print(value, end="")
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
