"""Installed private-host text fixture, ownership fencing and crash recovery.

Uses isolated state and synthetic calls. Logs only fixed stages/codes, hashes,
counts and sampled resource measurements. Does not enable managed preparation.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)

from mcpme.contracts import validate_package
from mcpme_textgen.fixture import file_hash


def require(value):
    if not value:
        raise ValueError("verification_failed")


@contextlib.contextmanager
def isolated_workspace():
    owned = {"root": Path(tempfile.mkdtemp(prefix="mcpme-installed-text-")).resolve(), "settled": False}
    try:
        yield owned
    finally:
        if owned["settled"]:
            shutil.rmtree(owned["root"])


@contextlib.asynccontextmanager
async def session(connection):
    async with httpx.AsyncClient(headers={"authorization": "Bearer " + connection["token"]},
                                 trust_env=False, timeout=60) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                require(initialized.protocolVersion == "2025-11-25")
                require(set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"})
                yield client


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    require(not result.isError and isinstance(result.structuredContent, dict))
    return result.structuredContent


async def verify(args, report):
    checks = report["checks"]
    package_identity = validate_package(args.package.resolve())
    report["package_digest"] = package_identity.digest
    expected_tools = [tool.mcp_schema() for tool in package_identity.manifest.tools]
    with isolated_workspace() as workspace:
        root = workspace["root"]
        host, package, state, home = (root / name for name in ("relocated host", "selected package", "state", "home"))
        await asyncio.to_thread(shutil.copytree, args.host.resolve(), host, symlinks=True)
        await asyncio.to_thread(shutil.copytree, args.package.resolve(), package, symlinks=False)
        require(validate_package(package).digest == package_identity.digest)
        state.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        policy = state / "resource-policy.json"
        policy.write_text(json.dumps({"memory_budget_mb": 1088, "headroom_mb": 64,
                                      "gpu_policy": "exclusive-residency"}))
        policy.chmod(0o600)
        launcher, reaper = host / "bin/mcpme", host / "bin/mcpme-reap"
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(root),
               "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        owner = None
        stopped_pid = None

        async def cli(*arguments, allow_error=False):
            child = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), *arguments,
                env=env, cwd=home, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                raw, _ = await asyncio.wait_for(child.communicate(), 300)
            finally:
                if child.returncode is None:
                    child.terminate()
                    await child.wait()
            require(len(raw) <= 1024 * 1024)
            value = json.loads(raw)
            if not allow_error and (child.returncode != 0 or "error" in value):
                error = value.get("error")
                code = error.get("code") if isinstance(error, dict) else error
                report["error_code"] = code if isinstance(code, str) and re.fullmatch("[a-z_]{1,100}", code) else "cli_failed"
                require(False)
            return value

        async def start():
            nonlocal owner
            owner = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), "serve",
                "--reaper", str(reaper), env=env, cwd=home,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(60):
                while True:
                    require(owner.returncode is None)
                    if (state / "manager-connection.json").exists():
                        ready = await cli("capabilities", "--json", allow_error=True)
                        if "error" not in ready:
                            break
                    await asyncio.sleep(.05)

        async def stop():
            if owner is not None and owner.returncode is None:
                owner.terminate()
                await asyncio.wait_for(owner.wait(), 45)

        async def finish(operation, expected_error=None):
            async with asyncio.timeout(180):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(.05)
                    operation = await cli("operations", "get", operation["operation_id"], "--json")
            if expected_error:
                require(operation["status"] == "failed" and operation.get("error_code") == expected_error)
            elif operation["status"] != "completed":
                code = operation.get("error_code")
                report["error_code"] = code if isinstance(code, str) and re.fullmatch("[a-z_]{1,100}", code) else "operation_failed"
                require(False)
            return operation

        async def details(slot):
            manager = json.loads((state / "manager-connection.json").read_bytes())
            async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
                response = await http.get(manager["endpoint"] + "/v1/slots/" + slot + "/details",
                    headers={"authorization": "Bearer " + manager["token"]})
            require(response.status_code == 200)
            return response.json()

        def reservation(slot):
            return state / "slots" / slot / "reservation.json"

        def ledger(slot):
            return json.loads((state / "slots" / slot / "ownership.json").read_bytes())

        try:
            report["stage"] = "install"
            await start()
            slots = []
            for _ in range(2):
                plan = await cli("install", "plan", "--package", str(package), "--json")
                installed = await finish(await cli("install", "apply", "--plan", plan["plan_id"], "--json"))
                slots.append(installed["slot_id"])
            first, second = slots
            shutil.rmtree(package)
            checks["installed_relocated_package"] = True
            await finish(await cli("slots", "start", first, "--json"))
            value = await details(first)
            residents = {item["process_id"]: item for item in value["processes"]}
            require(set(residents) == {"engine", "adapter"})
            require(json.loads(reservation(first).read_bytes()) == {"memory_mb": 1088, "gpu": False})
            await finish(await cli("slots", "start", second, "--json"), expected_error="resource_blocked")
            checks["host_resource_admission"] = True
            connection = json.loads((state / "slots" / first / "connection.json").read_bytes())
            engine_id = residents["engine"]["instance_id"]
            report["stage"] = "real_mcp_calls"
            parameters = StdioServerParameters(command=str(launcher), args=["--state-dir", str(state), "connect", "--slot", first],
                                               env=env, cwd=str(home))
            with open(os.devnull, "w") as errlog:
                async with session(connection) as a, session(connection) as b:
                    async with stdio_client(parameters, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as stdio:
                            await stdio.initialize()
                            require([tool.model_dump(by_alias=True, exclude_none=True) for tool in (await a.list_tools()).tools] == expected_tools)
                            statuses = await asyncio.gather(*(call(c, "text_status") for c in (a, b, stdio)))
                            require({item["engine_instance_id"] for item in statuses} == {engine_id})
                            results = await asyncio.gather(*(call(c, "text_generate", {
                                "text": "List integers starting at one.", "max_output_tokens": maximum})
                                for c, maximum in zip((a, b, stdio), (64, 8, 1))))
                            require(all(0 < result["output_tokens"] <= maximum for result, maximum in zip(results, (64, 8, 1))))
                            require((await call(a, "text_status"))["completed"] == 3)
                            report["generated_token_counts"] = [result["output_tokens"] for result in results]
            checks["exact_tools_and_three_clients"] = checks["actual_installed_cpu_inference"] = True
            key = next((state / "slots" / first / "runtime").glob("adapter-*.key")).read_text().strip()
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                endpoint = residents["adapter"]["endpoint"]
                require((await http.get(endpoint + "/health")).status_code == 401)
                auth = {"authorization": "Bearer " + key}
                require((await http.get(endpoint + "/health", headers=auth)).status_code == 200)
                require((await http.get(endpoint + "/health", headers={**auth, "origin": "https://invalid.test"})).status_code == 403)
                require((await http.get(endpoint + "/health", headers={**auth, "host": "invalid.test"})).status_code == 403)
            checks["adapter_transport_policy"] = True

            # Pause this isolated, currently owned test engine to make its
            # transport uncertain. The host's native helper retains identity;
            # production retirement never signals a discovered engine PID.
            report["stage"] = "uncertain_transport_fence"
            stopped_pid = residents["engine"]["pid"]
            os.kill(stopped_pid, signal.SIGSTOP)
            started = time.monotonic()
            async with session(connection) as client:
                result = await client.call_tool("text_generate", {"text": "List integers starting at one.", "max_output_tokens": 64})
                report["fenced_call_is_error"] = result.isError
                require(result.isError)
                report["stage"] = "reservation_after_fence"
                report["reservation_present_at_error"] = reservation(first).exists()
                require(reservation(first).exists())
                require(engine_id in ledger(first))
                checks["reservation_retained_after_transport_failure"] = True
            report["stage"] = "dependency_reap"
            async with asyncio.timeout(30):
                while reservation(first).exists():
                    await asyncio.sleep(.025)
            require(not ledger(first))
            stopped_pid = None
            report["transport_fence_and_reap_seconds"] = round(time.monotonic() - started, 3)
            checks["host_joined_dependency_reap"] = True
            await finish(await cli("slots", "start", second, "--json"))
            require(reservation(second).exists())
            await finish(await cli("slots", "stop", second, "--json"))
            checks["capacity_reusable_only_after_reap"] = True

            report["stage"] = "restart_reconciliation"
            await cli("slots", "configure", first, "--start-policy", "host", "--json")
            await finish(await cli("slots", "start", first, "--json"))
            before = await details(first)
            old_engine = next(p for p in before["processes"] if p["process_id"] == "engine")
            stopped_pid = old_engine["pid"]
            os.kill(stopped_pid, signal.SIGSTOP)
            owner.kill()
            await owner.wait()
            require(reservation(first).exists() and ledger(first))
            await start()
            after = await details(first)
            new_engine = next(p for p in after["processes"] if p["process_id"] == "engine")
            require(new_engine["instance_id"] != old_engine["instance_id"])
            require(all(identifier != old_engine["instance_id"] for identifier in ledger(first)))
            require(after["slot"]["public_port"] == before["slot"]["public_port"])
            stopped_pid = None
            async with session(connection) as client:
                require((await call(client, "text_generate", {"text": "Reply with the word blue.", "max_output_tokens": 8}))["output_tokens"] <= 8)
            checks["crash_restart_receipt_reconciliation"] = True
            checks["stable_endpoint_new_engine_generation"] = True
            report["stage"] = "cleanup"
            await stop()
            require(all(not ledger(slot) and not reservation(slot).exists() for slot in slots))
            checks["all_owned_processes_settled"] = True
            report["host_source_commit"] = json.loads((host / "build.json").read_bytes())["source_commit"]
            report["a4_managed_native_gate_passed"] = False
            report["stage"] = "complete"
        finally:
            # The native helper owns/reaps any stopped test engine; never adopt
            # or signal its old PID during cleanup or after a host restart.
            await stop()
            workspace["settled"] = all(json.loads(path.read_bytes()) == {}
                                        for path in (state / "slots").glob("*/ownership.json"))
            report["ownership_settled"] = workspace["settled"]
            if not workspace["settled"]:
                report["retained_workspace"] = str(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"stage": "start", "checks": {}, "harness_sha256": file_hash(Path(__file__))}
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
