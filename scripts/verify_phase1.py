"""Synthetic Phase 1 model and deterministic-slot checks; report only safe metadata."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager

# Sealed packages must never acquire undeclared bytecode during verification.
sys.dont_write_bytecode = True

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

CHECKS = (
    "relocated_private_runtime", "model_package_relocated", "original_tool_schemas", "http_and_stdio",
    "one_shared_engine", "one_engine_slot", "literal_string_parity", "model_identity", "fresh_repeat_changed_cache",
    "concurrent_queue_and_counts", "context_rejection", "deterministic_independent_controls",
    "model_restart_generation_cache_reset", "graceful_cleanup", "host_sigkill_surviving_reaper_cleanup",
    "owned_engine_forced_exit", "engine_failure_other_slot_usable", "legacy_endpoint_untouched",
)

QUESTION = {"type": "choice", "instructions": "Choose the matching synthetic category.",
            "criteria": {"object": "A JSON object", "array": "A JSON array"}}
LITERAL = '{"synthetic_kind":"object","values":[1,2]}'


def require(value):
    if not value:
        raise ValueError("verification_failed")


def private_json(path):
    path = Path(path)
    info = path.lstat()
    require(not path.is_symlink() and info.st_uid == os.getuid() and not info.st_mode & 0o077 and info.st_size <= 1024 * 1024)
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict))
    return value


def host_directory(path):
    path = path.resolve(strict=True)
    for candidate in (path, path / "Contents/Resources/host", path / "Contents/Resources/mcpme-host"):
        if (candidate / "bin/mcpme").is_file():
            return candidate
    raise ValueError("host_unavailable")


async def listening_legacy():
    process = await asyncio.create_subprocess_exec("/usr/sbin/lsof", "-t", "-n", "-iTCP:19385", "-sTCP:LISTEN",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await process.communicate()
    return tuple(sorted(set(output.decode().split())))


async def processes_gone(pids, timeout=15):
    if not pids:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process = await asyncio.create_subprocess_exec("/bin/ps", "-p", ",".join(str(pid) for pid in pids), "-o", "pid=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await process.communicate()
        if not output.strip():
            return True
        await asyncio.sleep(0.1)
    return False


async def owned_descendants(parent_pid):
    process = await asyncio.create_subprocess_exec("/bin/ps", "-axo", "pid=,ppid=",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await process.communicate()
    pairs = [tuple(map(int, line.split())) for line in output.decode().splitlines() if len(line.split()) == 2]
    parents, found = {parent_pid}, set()
    while True:
        children = {pid for pid, parent in pairs if parent in parents} - found
        if not children:
            return found
        found.update(children)
        parents.update(children)


def probability_difference(left, right):
    require(type(left) is type(right))
    if isinstance(left, dict):
        require(left.keys() == right.keys())
        return max((probability_difference(left[key], right[key]) for key in left), default=0)
    if isinstance(left, list):
        require(len(left) == len(right))
        return max((probability_difference(a, b) for a, b in zip(left, right)), default=0)
    if isinstance(left, float):
        return abs(left - right)
    require(left == right)
    return 0


@asynccontextmanager
async def http_session(connection):
    async with httpx.AsyncClient(headers={"authorization": "Bearer " + connection["token"]},
            timeout=150, trust_env=False, follow_redirects=False) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                require((await client.initialize()).protocolVersion == "2025-11-25")
                yield client


async def tool(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    require(not result.isError and isinstance(result.structuredContent, dict))
    return result.structuredContent


async def owned_failure_probe(state, reaper, model, deterministic):
    """The local Supervisor retains process identities; fault injection uses its reaper command."""
    from mcpme.supervisor import Supervisor
    host = Supervisor(state.resolve(), reaper.resolve())
    result = {"owned_engine_forced_exit": False, "engine_failure_other_slot_usable": False}
    async def install(package, name):
        planner = getattr(host, "plan_install_async", host.plan_install)
        plan = planner({"package": str(package), "name": name, "start": True})
        if inspect.isawaitable(plan):
            plan = await plan
        applied = await host.apply_install(plan["plan_id"])
        async with asyncio.timeout(240):
            while True:
                operation = host.registry.get_operation(applied["operation_id"])
                if operation.status == "completed":
                    return operation.slot_id
                require(operation.status in {"pending", "running"})
                await asyncio.sleep(0.05)
    try:
        await host.initialize()
        model_id = await install(model.resolve(), "Fault probe model")
        json_id = await install(deterministic.resolve(), "Fault probe JSON")
        resident = host.residents[model_id]["engine"]
        require(resident.process.returncode is None)
        # This is a command to the native reaper holding its child identity, never a discovered-PID signal.
        await resident.process.stop()
        async with asyncio.timeout(15):
            while host.registry.get_slot(model_id).observed_state not in {"failed", "ownership_lost"}:
                await asyncio.sleep(0.05)
        result["owned_engine_forced_exit"] = True
        async with http_session(host.connection(json_id)) as client:
            require(await tool(client, "validate_json", {"document": "false"}) == {"valid": True, "kind": "boolean"})
        result["engine_failure_other_slot_usable"] = True
    finally:
        await host.close()
    require(all(not ledger for ledger in host.ledgers.values()))
    return result


async def verify(args, report):
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="mcpme-phase1-") as directory:
        root = Path(directory).resolve()
        relocated = root / "relocated host"
        await asyncio.to_thread(shutil.copytree, host_directory(args.host), relocated, symlinks=True)
        launcher = relocated / "bin/mcpme"
        reaper = relocated / "bin/mcpme-reap"
        home = root / "private home"
        home.mkdir(mode=0o700)
        env = {"HOME": str(home), "TMPDIR": str(root), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1"}
        state = root / "private state"
        state.mkdir(mode=0o700)
        supervisor = None
        manager = None
        all_pids = set()

        async def cli(*arguments):
            process = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), *arguments,
                env=env, cwd=home, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                data, _ = await asyncio.wait_for(process.communicate(), 300)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                await process.wait()
                raise
            require(process.returncode == 0 and len(data) <= 1024 * 1024)
            value = json.loads(data)
            require(isinstance(value, dict) and "error" not in value)
            return value

        async def management(path):
            async with httpx.AsyncClient(trust_env=False, timeout=150, follow_redirects=False) as client:
                response = await client.get(manager["endpoint"] + path, headers={"authorization": "Bearer " + manager["token"]})
                require(response.status_code == 200)
                value = response.json()
                require("error" not in value)
                return value

        async def finish(operation):
            async with asyncio.timeout(300):
                while True:
                    value = await cli("operations", "get", operation["operation_id"], "--json")
                    if value["status"] == "completed":
                        return value
                    require(value["status"] in {"pending", "running"})
                    await asyncio.sleep(0.1)

        async def install(package, name):
            plan = await cli("install", "plan", "--package", str(package.resolve()), "--name", name, "--start", "--json")
            operation = await finish(await cli("install", "apply", "--plan", plan["plan_id"], "--json"))
            value = await cli("slots", "inspect", operation["slot_id"], "--json")
            require(value["public_port"] != 19385)
            return value

        async def remember_model_pids(slot_id):
            details = await management("/v1/slots/" + slot_id + "/details")
            for value in details["processes"]:
                all_pids.update((value["pid"], value["reaper_pid"]))
            return details

        async def start_host():
            nonlocal supervisor, manager
            supervisor = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), "serve",
                "--reaper", str(reaper), env=env, cwd=home,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(20):
                while not (state / "manager-connection.json").is_file():
                    require(supervisor.returncode is None)
                    await asyncio.sleep(0.05)
            manager = private_json(state / "manager-connection.json")

        try:
            await start_host()
            await cli("capabilities", "--json")
            checks["relocated_private_runtime"] = True
            started = time.monotonic()
            model_slot = await install(args.model_package, "Phase 1 model")
            report["timing_ms"]["model_install"] = round((time.monotonic() - started) * 1000, 2)
            json_slot = await install(args.json_package, "Phase 1 deterministic")
            report["slots"] = 2
            model_id, json_id = model_slot["slot_id"], json_slot["slot_id"]
            model_connection = private_json(state / "slots" / model_id / "connection.json")
            json_connection = private_json(state / "slots" / json_id / "connection.json")
            details = await remember_model_pids(model_id)
            installed = Path(details["slot"]["package_path"])
            require(installed.is_relative_to(state))
            checks["model_package_relocated"] = True
            engines = [value for value in details["processes"] if value["process_id"] == "engine"]
            require(len(engines) == 1)
            engine = engines[0]
            key_files = list((state / "slots" / model_id / "runtime").glob("engine-*.key"))
            require(len(key_files) == 1 and not key_files[0].stat().st_mode & 0o077)
            engine_key = key_files[0].read_text().strip()
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                props = await client.get(engine["endpoint"] + "/props", headers={"authorization": "Bearer " + engine_key})
                require(props.status_code == 200 and props.json().get("total_slots") == 1)
            checks["one_engine_slot"] = True
            sys.path[:0] = [str(installed / "backend"), str(installed / "vendor")]
            from jevk5_app.scorer import LocalScorer
            from jevk5_app.server import make_app
            _, schema_app = make_app(None, "schema-only", tools_only=True)
            expected = [value.model_dump(by_alias=True, exclude_none=True) for value in await schema_app.state.mcp.list_tools()]
            exported = await cli("connections", "export", "--slot", model_id, "--json")
            config = exported["configuration"]["mcpServers"][model_id]
            require(Path(config["command"]) == launcher and engine_key not in json.dumps(exported))
            with open(os.devnull, "w") as errlog:
                parameters = StdioServerParameters(command=config["command"], args=config["args"], env=env, cwd=str(home))
                async with http_session(model_connection) as first, http_session(model_connection) as second:
                    async with stdio_client(parameters, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as stdio:
                            await stdio.initialize()
                            listing = [value.model_dump(by_alias=True, exclude_none=True) for value in (await first.list_tools()).tools]
                            require(listing == expected)
                            checks["original_tool_schemas"] = True
                            statuses = await asyncio.gather(*(tool(client, "jevk5_status") for client in (first, second, stdio)))
                            require({value["llama_pid"] for value in statuses} == {engine["pid"]})
                            require({value["engine_instance_id"] for value in statuses} == {engine["instance_id"]})
                            checks["one_shared_engine"] = checks["http_and_stdio"] = True
                            async def measured(client, evidence, label):
                                began = time.monotonic()
                                result = await tool(client, "jevk5_decide", {"state": evidence, "question": QUESTION})
                                report["timing_ms"][label + "_roundtrip"] = round((time.monotonic() - began) * 1000, 2)
                                report["timing_ms"][label + "_adapter"] = round(result["latency_ms"], 2)
                                report["timing_ms"][label + "_engine"] = round(result["engine_ms"], 2)
                                return result
                            fresh = await measured(first, LITERAL, "fresh")
                            repeated = await measured(stdio, LITERAL, "repeat")
                            changed = await measured(second, LITERAL + " ", "changed")
                            require([fresh["cache"]["hit"], repeated["cache"]["hit"], changed["cache"]["hit"]] == [False, True, False])
                            report["model_decisions"] += 3
                            checks["fresh_repeat_changed_cache"] = True
                            manifest = details["package"]["model"]
                            require(fresh["model"]["sha256"] == manifest["sha256"] and fresh["model"]["id"] == manifest["model_id"])
                            checks["model_identity"] = True
                            scorer = LocalScorer(engine["endpoint"], engine_key, context_tokens=8192,
                                                temperature=1.22, knockout_temperature=0.93)
                            direct = await asyncio.to_thread(scorer.decide, LITERAL, QUESTION)
                            report["direct_scorer_calls"] += 1
                            difference = probability_difference(fresh["answer"], direct)
                            report["max_abs_probability_difference"] = difference
                            require(difference <= 1e-5 and fresh["input_tokens"] == direct["input_tokens"])
                            checks["literal_string_parity"] = True
                            before = await tool(first, "jevk5_status")
                            queued = asyncio.gather(tool(first, "jevk5_decide", {"state": "synthetic queue one " * 256, "question": QUESTION}),
                                                    tool(second, "jevk5_decide", {"state": "synthetic queue two " * 256, "question": QUESTION}))
                            saw_queue = False
                            while not queued.done():
                                value = await tool(stdio, "jevk5_status")
                                saw_queue |= value["queue_depth"] >= 2
                                await asyncio.sleep(0.02)
                            await queued
                            after = await tool(first, "jevk5_status")
                            require(saw_queue and after["counts"]["decisions"] == before["counts"]["decisions"] + 2)
                            report["model_decisions"] += 2
                            checks["concurrent_queue_and_counts"] = True
                            rejected = await first.call_tool("jevk5_decide", {"state": "synthetic context fixture " * 12000, "question": QUESTION})
                            require(rejected.isError and any("context_limit" in getattr(value, "text", "") for value in rejected.content))
                            checks["context_rejection"] = True
            await finish(await cli("slots", "stop", json_id, "--json"))
            async with http_session(model_connection) as client:
                require((await tool(client, "jevk5_status"))["ready"])
            await finish(await cli("slots", "start", json_id, "--json"))
            async with http_session(json_connection) as client:
                require(await tool(client, "validate_json", {"document": "[1,2]"}) == {"valid": True, "kind": "array"})
                report["deterministic_calls"] += 1
            checks["deterministic_independent_controls"] = True
            await finish(await cli("slots", "stop", model_id, "--json"))
            async with http_session(json_connection) as client:
                require(await tool(client, "validate_json", {"document": "true"}) == {"valid": True, "kind": "boolean"})
                report["deterministic_calls"] += 1
            await finish(await cli("slots", "start", model_id, "--json"))
            restarted = await remember_model_pids(model_id)
            new_engine = next(value for value in restarted["processes"] if value["process_id"] == "engine")
            require(new_engine["instance_id"] != engine["instance_id"] and new_engine["generation"] > engine["generation"])
            async with http_session(model_connection) as client:
                decision = await tool(client, "jevk5_decide", {"state": LITERAL, "question": QUESTION})
                require(decision["cache"]["hit"] is False)
                report["model_decisions"] += 1
            require(restarted["slot"]["public_port"] == model_slot["public_port"])
            checks["model_restart_generation_cache_reset"] = True
            all_pids.update(await owned_descendants(supervisor.pid))
            supervisor.terminate()
            await asyncio.wait_for(supervisor.wait(), 30)
            require(not (state / "manager-connection.json").exists())
            require(all(private_json(path) == {} for path in (state / "slots").glob("*/ownership.json")))
            require(await processes_gone(all_pids))
            checks["graceful_cleanup"] = True
            all_pids.clear()
            await start_host()
            await finish(await cli("slots", "start", model_id, "--json"))
            await remember_model_pids(model_id)
            all_pids.update(await owned_descendants(supervisor.pid))
            supervisor.kill()  # This subprocess handle is held by this verifier until reaping.
            await supervisor.wait()
            require(await processes_gone(all_pids))
            checks["host_sigkill_surviving_reaper_cleanup"] = True
        finally:
            if supervisor and supervisor.returncode is None:
                supervisor.terminate()
                await asyncio.wait_for(supervisor.wait(), 30)
        # Execute the ownership fault probe under relocated host Python and relocated package code.
        probe = root / "owned fault probe.py"
        shutil.copyfile(Path(__file__), probe)
        probe_state = root / "probe private state"
        probe_state.mkdir(mode=0o700)
        probe_env = {**env, "PYTHONPATH": ":".join([str(relocated / "backend"), str(relocated / "python/site-packages"),
                         str(installed / "backend"), str(installed / "vendor")])}
        child = await asyncio.create_subprocess_exec(str(relocated / "python/bin/python3.12"), str(probe),
            "--owned-failure-probe", "--state-dir", str(probe_state), "--reaper", str(reaper),
            "--model-package", str(installed), "--json-package", str(args.json_package.resolve()),
            env=probe_env, cwd=home, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            data, _ = await asyncio.wait_for(child.communicate(), 300)
        finally:
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 30)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        child.kill()
                    await child.wait()
        require(child.returncode == 0 and len(data) <= 4096)
        result = json.loads(data)
        require(set(result) == {"owned_engine_forced_exit", "engine_failure_other_slot_usable"})
        checks.update(result)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path)
    parser.add_argument("--model-package", type=Path, required=True)
    parser.add_argument("--json-package", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--owned-failure-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reaper", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.owned_failure_probe:
        try:
            result = asyncio.run(owned_failure_probe(args.state_dir, args.reaper, args.model_package, args.json_package))
            print(json.dumps(result))
            return 0 if all(result.values()) else 1
        except (Exception, KeyboardInterrupt):
            print(json.dumps({"owned_engine_forced_exit": False, "engine_failure_other_slot_usable": False}))
            return 1
    report = {"api_version": 1, "verified": False, "checks": dict.fromkeys(CHECKS, False), "slots": 0,
              "model_decisions": 0, "deterministic_calls": 0, "direct_scorer_calls": 0,
              "max_abs_probability_difference": None, "timing_ms": {}}
    try:
        require(args.host is not None)
        async def run_verification():
            before_legacy = await listening_legacy()
            try:
                await verify(args, report)
            finally:
                report["checks"]["legacy_endpoint_untouched"] = before_legacy == await listening_legacy()
        asyncio.run(run_verification())
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
