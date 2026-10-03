"""Actual pinned-model intake, external preparation, and shared-engine acceptance.

Uses a temporary private installation and synthetic requests. Reports only fixed
check labels, counts and numeric parity; source/model/build output is discarded.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)
CHECKS = ("actual_pinned_gguf", "relocated_host", "raw_weight_engine_intake", "draft_restart_resume",
          "exact_export_generator", "source_independence", "stale_return_rejected", "edited_attachment_rejected",
          "authorized_import_install", "exact_tools", "http_and_stdio", "two_clients_one_engine",
          "one_engine_slot", "pinned_engine_identity", "fresh_repeat_changed_cache", "upstream_scorer_parity",
          "restart_stable_endpoint_cache_reset", "graceful_cleanup")
QUESTION = {"type": "choice", "instructions": "Choose the matching synthetic category.",
            "criteria": {"object": "A JSON object", "array": "A JSON array"}}
EVIDENCE = '{"synthetic_kind":"object","values":[1,2]}'


def require(value):
    if not value:
        raise ValueError("verification_failed")


def host_directory(path):
    for candidate in (path, path / "Contents/Resources/host"):
        if (candidate / "bin/mcpme").is_file():
            return candidate.resolve()
    raise ValueError("host_unavailable")


def clone_file(source, destination):
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["/bin/cp", "-c", str(source), str(destination)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        shutil.copyfile(source, destination)
    destination.chmod(0o700 if source.stat().st_mode & 0o111 else 0o600)
    return str(destination)


@contextlib.asynccontextmanager
async def http_session(connection):
    async with httpx.AsyncClient(headers={"authorization": "Bearer " + connection["token"]}, timeout=180,
                                 trust_env=False, follow_redirects=False) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                require((await client.initialize()).protocolVersion == "2025-11-25")
                yield client


async def tool(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    require(not result.isError and isinstance(result.structuredContent, dict))
    return result.structuredContent


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


async def verify(args, report):
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="mcpme-model-phase2-") as temporary:
        root = Path(temporary).resolve()
        host = root / "relocated private host"
        await asyncio.to_thread(shutil.copytree, host_directory(args.host), host, symlinks=True)
        launcher, reaper = host / "bin/mcpme", host / "bin/mcpme-reap"
        state, home = root / "private state", root / "private home"
        state.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(root),
               "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "en_US.UTF-8"}
        owner = None

        async def cli(*arguments, error=None):
            process = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), *arguments,
                env=env, cwd=home, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                raw, _ = await asyncio.wait_for(process.communicate(), 300)
            finally:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    await process.wait()
            require(len(raw) <= 1024 * 1024)
            value = json.loads(raw)
            received_error = value.get("error")
            received_code = received_error.get("code") if isinstance(received_error, dict) else received_error
            if error is None:
                if process.returncode != 0 or "error" in value:
                    allowed = {"invalid_source_selection", "unsafe_source_file", "unsafe_source_path", "engine_receipt_invalid",
                               "engine_build_unsupported", "engine_license_required", "unsupported_model_artifact",
                               "unsupported_model_format", "invalid_import_requirements", "invalid_import_source", "unknown_field",
                               "source_changed", "source_file_excluded", "invalid_import_request", "invalid_request",
                               "imports_unavailable", "insufficient_disk_space", "source_identity_mismatch", "artifact_hash_mismatch",
                               "model_artifact_mismatch", "model_preparation_receipt_required"}
                    report["failure_code"] = received_code if isinstance(received_code, str) and received_code in allowed else "cli_failed"
                require(process.returncode == 0 and "error" not in value)
            else:
                require(process.returncode != 0 and received_code == error)
            return value

        async def start_host():
            nonlocal owner
            owner = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), "serve",
                "--reaper", str(reaper), env=env, cwd=home, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(30):
                while not (state / "manager-connection.json").exists():
                    require(owner.returncode is None)
                    await asyncio.sleep(0.05)
            await cli("capabilities", "--json")

        async def stop_host():
            require(owner is not None)
            if owner.returncode is None:
                owner.terminate()
                await asyncio.wait_for(owner.wait(), 30)

        async def finish(operation):
            async with asyncio.timeout(300):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.1)
                    operation = await cli("operations", "get", operation["operation_id"], "--json")
            require(operation["status"] == "completed")
            return operation

        async def details(slot_id):
            connection = json.loads((state / "manager-connection.json").read_bytes())
            async with httpx.AsyncClient(trust_env=False, timeout=180) as http:
                response = await http.get(connection["endpoint"] + "/v1/slots/" + slot_id + "/details",
                                          headers={"authorization": "Bearer " + connection["token"]})
                require(response.status_code == 200)
                return response.json()

        try:
            await start_host()
            checks["relocated_host"] = True
            # Only the verifier's private input copies are deleted; the supplied
            # real model and build remain untouched throughout acceptance.
            selected = root / "selected raw sources"
            report["stage"] = "copy_selected_artifacts"
            selected.mkdir(mode=0o700)
            weights = selected / "weights.gguf"
            await asyncio.to_thread(clone_file, args.weights.resolve(), weights)
            engine_source = args.engine_build.resolve()
            if (engine_source / "Contents/Resources").is_dir():
                engine_source = engine_source / "Contents/Resources"
            engine = selected / "engine build"
            engine.mkdir(mode=0o700)
            await asyncio.to_thread(shutil.copytree, engine_source / "llama", engine / "llama", copy_function=clone_file)
            await asyncio.to_thread(shutil.copytree, engine_source / "licenses", engine / "licenses", copy_function=clone_file)
            clone_file(engine_source / "runtime-manifest.json", engine / "runtime-manifest.json")
            report["stage"] = "raw_intake"
            draft = await cli("imports", "create", "--source", str(weights), "--model-engine", str(engine),
                              "--name", "Phase 2 actual JevK5", "--intent", "Expose decisions and status", "--start", "--json")
            require(draft["source"]["kind"] == "artifact_collection")
            checks["actual_pinned_gguf"] = checks["raw_weight_engine_intake"] = True
            report["stage"] = "draft_restart"
            await stop_host()
            await start_host()
            resumed = await cli("imports", "inspect", draft["draft_id"], "--json")
            require(resumed["source"] == draft["source"] and resumed["revision"] == draft["revision"])
            checks["draft_restart_resume"] = True
            requested = root / "requirements.json"
            requested.write_text(json.dumps({"engine_profile": "jevk5-llamacpp-v1", "profile": "model-service",
                                             "tools": ["jevk5_decide", "jevk5_status"], "memory_mb": 8192}))
            requested.chmod(0o600)
            revised = await cli("imports", "revise", draft["draft_id"], "--revision", str(draft["revision"]),
                                "--requirements", str(requested), "--json")
            exported = await cli("imports", "export", draft["draft_id"], "--revision", str(revised["revision"]), "--json")
            setup = root / "setup.json"
            setup.write_text(json.dumps(exported))
            setup.chmod(0o600)
            await asyncio.to_thread(shutil.rmtree, selected)
            checks["source_independence"] = True
            report["stage"] = "external_generator"
            package = root / "external agent workspace/jevk5 package"
            package.parent.mkdir(mode=0o700)
            generator = host / "templates/jevk5-llamacpp-v1/prepare.py"
            process = await asyncio.create_subprocess_exec(str(host / "python/bin/python3.12"), "-I", "-S", "-B", "-c",
                "import runpy,sys;sys.path[:0]=[sys.argv[1],sys.argv[2]];sys.argv=sys.argv[3:];runpy.run_path(sys.argv[0],run_name='__main__')",
                str(host / "backend"), str(host / "python/site-packages"), str(generator),
                "--setup-export", str(setup), "--host", str(host), "--output", str(package),
                env=env, cwd=home, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                raw, _ = await asyncio.wait_for(process.communicate(), 600)
                require(process.returncode == 0 and json.loads(raw).get("prepared") is True)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
            receipt = json.loads((package / "preparation-receipt.json").read_bytes())
            require(receipt["source_revision"] == exported["source"]["revision"]
                    and receipt["source_files"] == exported["source"]["files"])
            checks["exact_export_generator"] = True
            report["stage"] = "returned_package"
            await cli("imports", "attach", draft["draft_id"], "--revision", str(draft["revision"]),
                      "--package", str(package), "--json", error="stale_import_revision")
            checks["stale_return_rejected"] = True
            attached = await cli("imports", "attach", draft["draft_id"], "--revision", str(revised["revision"]),
                                 "--package", str(package), "--json")
            require(attached["state"] == "ready_to_plan")
            sealed = Path(attached["attachment"]["path"])
            candidate = sealed / "preparation-receipt.json"
            original = candidate.read_bytes()
            candidate.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            rejected = await cli("install", "plan", "--import", draft["draft_id"], "--revision", str(revised["revision"]),
                                 "--json", error="artifact_hash_mismatch")
            candidate.write_bytes(original)
            checks["edited_attachment_rejected"] = True
            report["stage"] = "install"
            plan = await cli("install", "plan", "--import", draft["draft_id"], "--revision", str(revised["revision"]), "--json")
            operation = await finish(await cli("install", "apply", "--plan", plan["plan_id"], "--json"))
            slot_id = operation["slot_id"]
            checks["authorized_import_install"] = True
            report["stage"] = "actual_model_clients"
            await asyncio.to_thread(shutil.rmtree, package)
            connection = json.loads((state / "slots" / slot_id / "connection.json").read_bytes())
            value = await details(slot_id)
            installed = Path(value["slot"]["package_path"])
            engines = [item for item in value["processes"] if item["process_id"] == "engine"]
            require(len(engines) == 1 and value["package"]["model"]["engine_identity"]["build"] ==
                    "11146-7fe450e19305b828c199d602c23a8337aaa1f03b")
            checks["pinned_engine_identity"] = True
            engine = engines[0]
            keys = list((state / "slots" / slot_id / "runtime").glob("engine-*.key"))
            require(len(keys) == 1)
            key = keys[0].read_text().strip()
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                response = await http.get(engine["endpoint"] + "/props", headers={"authorization": "Bearer " + key})
                require(response.status_code == 200 and response.json()["total_slots"] == 1)
            checks["one_engine_slot"] = True
            sys.path[:0] = [str(installed / "backend"), str(installed / "vendor")]
            from jevk5_app.scorer import LocalScorer
            from jevk5_app.server import make_app
            _, schema = make_app(None, "synthetic-schema-only", tools_only=True)
            expected = [tool.model_dump(by_alias=True, exclude_none=True) for tool in await schema.state.mcp.list_tools()]
            parameters = StdioServerParameters(command=str(launcher), args=["--state-dir", str(state), "connect", "--slot", slot_id],
                                               env=env, cwd=str(home))
            with open(os.devnull, "w") as errlog:
                async with http_session(connection) as first, http_session(connection) as second:
                    async with stdio_client(parameters, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as stdio:
                            await stdio.initialize()
                            require([tool.model_dump(by_alias=True, exclude_none=True) for tool in (await first.list_tools()).tools] == expected)
                            checks["exact_tools"] = True
                            statuses = await asyncio.gather(*(tool(client, "jevk5_status") for client in (first, second, stdio)))
                            require({item["llama_pid"] for item in statuses} == {engine["pid"]}
                                    and {item["engine_instance_id"] for item in statuses} == {engine["instance_id"]})
                            checks["http_and_stdio"] = checks["two_clients_one_engine"] = True
                            fresh = await tool(first, "jevk5_decide", {"state": EVIDENCE, "question": QUESTION})
                            repeated = await tool(stdio, "jevk5_decide", {"state": EVIDENCE, "question": QUESTION})
                            changed = await tool(second, "jevk5_decide", {"state": EVIDENCE + " ", "question": QUESTION})
                            require([fresh["cache"]["hit"], repeated["cache"]["hit"], changed["cache"]["hit"]] == [False, True, False])
                            checks["fresh_repeat_changed_cache"] = True
                            scorer = LocalScorer(engine["endpoint"], key, context_tokens=8192, temperature=1.22, knockout_temperature=0.93)
                            direct = await asyncio.to_thread(scorer.decide, EVIDENCE, QUESTION)
                            difference = probability_difference(fresh["answer"], direct)
                            require(difference <= 1e-5 and fresh["input_tokens"] == direct["input_tokens"])
                            report["max_abs_probability_difference"] = difference
                            checks["upstream_scorer_parity"] = True
            await finish(await cli("slots", "stop", slot_id, "--json"))
            await stop_host()
            await start_host()
            await finish(await cli("slots", "start", slot_id, "--json"))
            restarted = await details(slot_id)
            new_engine = next(item for item in restarted["processes"] if item["process_id"] == "engine")
            require(restarted["slot"]["public_port"] == value["slot"]["public_port"]
                    and new_engine["instance_id"] != engine["instance_id"])
            async with http_session(connection) as client:
                require((await tool(client, "jevk5_decide", {"state": EVIDENCE, "question": QUESTION}))["cache"]["hit"] is False)
            checks["restart_stable_endpoint_cache_reset"] = True
            report["stage"] = "cleanup"
            await stop_host()
            require(all(json.loads(path.read_bytes()) == {} for path in (state / "slots").glob("*/ownership.json")))
            checks["graceful_cleanup"] = True
        finally:
            if owner is not None and owner.returncode is None:
                owner.terminate()
                await asyncio.wait_for(owner.wait(), 30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--engine-build", type=Path, required=True)
    args = parser.parse_args()
    report = {"verified": False, "actual_model": True, "checks": {name: False for name in CHECKS}}
    try:
        asyncio.run(verify(args, report))
        require(all(report["checks"].values()))
        report["verified"] = True
        report.pop("stage", None)
    except BaseException:
        report["error"] = "model_phase2_verification_failed"
        print(json.dumps(report, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
