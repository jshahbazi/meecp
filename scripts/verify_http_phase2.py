"""Real SDK native-HTTP intake, preparation, identity, and session acceptance.

The selected synthetic factory remains in one foreground resident. Private keys,
settings, request bodies, response bodies and raw failures are never reported.
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
import sys
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)
CHECKS = ("relocated_host", "source_intake", "export_generator", "source_independence", "authorized_install",
          "configuration_inherited", "one_shared_http_resident", "http_and_stdio", "private_identity_authentication",
          "public_host_origin_authentication", "session_delete", "generation_restart_key_rotation", "smoke_fixture",
          "stale_return_rejected", "cleanup")
TOOL = {"name": "add", "description": "Add synthetic integers with the configured factor.",
        "input_schema": {"type": "object", "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}},
                         "required": ["left", "right"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"total": {"type": "integer"}}, "required": ["total"], "additionalProperties": False}}
IDENTITY = {"name": "identity", "description": "Report synthetic resident identity and settings presence.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"pid": {"type": "integer"}, "settings_available": {"type": "boolean"}},
                              "required": ["pid", "settings_available"], "additionalProperties": False}}


def require(value):
    if not value:
        raise ValueError("verification_failed")


@contextlib.asynccontextmanager
async def http_session(connection):
    async with httpx.AsyncClient(headers={"authorization": "Bearer " + connection["token"]}, trust_env=False, timeout=30) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, get_session):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                require(initialized.protocolVersion == "2025-11-25" and set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"})
                yield client, get_session


async def call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    require(not result.isError and isinstance(result.structuredContent, dict))
    return result.structuredContent


async def verify(args, report):
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="mcpme-http-phase2-") as temporary:
        root = Path(temporary).resolve()
        original_host = args.host.resolve()
        if (original_host / "Contents/Resources/host/bin/mcpme").is_file():
            original_host = original_host / "Contents/Resources/host"
        host = root / "relocated private host"
        await asyncio.to_thread(shutil.copytree, original_host, host, symlinks=True)
        launcher, reaper = host / "bin/mcpme", host / "bin/mcpme-reap"
        home, state = root / "private home", root / "private state"
        home.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8", "TMPDIR": str(root),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        owner = None

        async def cli(*arguments, error=None):
            child = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), *arguments, env=env, cwd=home,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                raw, _ = await asyncio.wait_for(child.communicate(), 300)
            finally:
                if child.returncode is None:
                    child.terminate()
                    await child.wait()
            require(len(raw) <= 1024 * 1024)
            value = json.loads(raw)
            received = value.get("error")
            code = received.get("code") if isinstance(received, dict) else received
            require(child.returncode == 0 and received is None if error is None else child.returncode != 0 and code == error)
            return value

        async def start():
            nonlocal owner
            owner = await asyncio.create_subprocess_exec(str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper),
                env=env, cwd=home, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(30):
                while not (state / "manager-connection.json").is_file():
                    require(owner.returncode is None)
                    await asyncio.sleep(0.05)
            require("mcp-http" in (await cli("capabilities", "--json"))["profiles"])

        async def stop():
            if owner and owner.returncode is None:
                owner.terminate()
                await asyncio.wait_for(owner.wait(), 30)

        async def finish(operation):
            async with asyncio.timeout(60):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.05)
                    operation = await cli("operations", "get", operation["operation_id"], "--json")
            if operation["status"] != "completed":
                code = operation.get("error_code")
                report["operation_error"] = code if isinstance(code, str) and re.fullmatch("[a-z_]{1,128}", code) else "operation_failed"
                require(False)
            return operation

        async def details(slot_id):
            manager = json.loads((state / "manager-connection.json").read_bytes())
            async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
                response = await http.get(manager["endpoint"] + "/v1/slots/" + slot_id + "/details",
                    headers={"authorization": "Bearer " + manager["token"]})
                require(response.status_code == 200)
                return response.json()

        try:
            report["stage"] = "host_start"
            await start()
            checks["relocated_host"] = True
            source = root / "selected source"
            source.mkdir(mode=0o700)
            (source / "LICENSE").write_text("Synthetic verification fixture license\n")
            tools = [TOOL, IDENTITY]
            tool_definitions = [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"],
                                 "outputSchema": tool["output_schema"]} for tool in tools]
            source_code = '''from mcp.server import Server
from mcp import types
import json,os

def create_server():
    with os.fdopen(int(os.environ.pop("MCPME_SETTINGS_FD")), "rb") as stream:
        settings=json.loads(stream.read(512*1024))
    factor=settings["configuration"]["factor"]
    server=Server("synthetic-http")
    @server.list_tools()
    async def listing():
        return [types.Tool(**tool) for tool in TOOLS]
    @server.call_tool()
    async def call(name,arguments):
        if name=="identity":
            return {"pid":os.getpid(),"settings_available":settings.get("schema_version")==1}
        return {"total":(arguments["left"]+arguments["right"])*factor}
    return server
'''
            (source / "service.py").write_text(source_code + "\nTOOLS=" + repr(tool_definitions) + "\n")
            draft = await cli("imports", "create", "--source", str(source), "--name", "Synthetic HTTP service",
                              "--intent", "Expose addition and resident identity", "--start", "--json")
            requirements = root / "requirements.json"
            requirements.write_text(json.dumps({"profile": "mcp-http", "tools": ["add", "identity"], "configuration": {"factor": 3}}))
            revised = await cli("imports", "revise", draft["draft_id"], "--revision", "1", "--requirements", str(requirements), "--json")
            exported = await cli("imports", "export", draft["draft_id"], "--revision", str(revised["revision"]), "--json")
            setup = root / "setup.json"
            setup.write_text(json.dumps(exported))
            tools_file = root / "tools.json"
            tools_file.write_text(json.dumps(tools))
            configuration = root / "configuration.json"
            configuration.write_text(json.dumps({"fields": {"factor": {"type": "integer", "default": 2, "minimum": 1, "maximum": 4}}}))
            smoke = root / "validation.json"
            smoke.write_text(json.dumps({"fixtures": [{"tool": "add", "arguments": {"left": 1, "right": 2}, "expected": {"total": 9}}]}))
            checks["source_intake"] = True
            shutil.rmtree(source)
            checks["source_independence"] = True
            report["stage"] = "external_generator"
            package = root / "external agent package"
            builder = await asyncio.create_subprocess_exec(str(host / "python/bin/python3.12"), "-I", "-S", "-B", "-c",
                "import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name='__main__')",
                str(host / "python/site-packages"), str(host / "templates/prepare.py"), "existing-mcp", "--setup-export", str(setup),
                "--tool-spec", str(tools_file), "--module", "service", "--http-factory", "create_server",
                "--configuration-spec", str(configuration), "--validation-spec", str(smoke), "--source-license", "LICENSE",
                "--package-id", "local.phase2-http", "--name", "Synthetic HTTP service", "--output", str(package),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}, cwd=home,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            raw, _ = await asyncio.wait_for(builder.communicate(), 300)
            require(builder.returncode == 0 and json.loads(raw)["prepared"] is True)
            checks["export_generator"] = True
            await cli("imports", "attach", draft["draft_id"], "--revision", "1", "--package", str(package), "--json", error="stale_import_revision")
            checks["stale_return_rejected"] = True
            attached = await cli("imports", "attach", draft["draft_id"], "--revision", str(revised["revision"]), "--package", str(package), "--json")
            require(attached["state"] == "ready_to_plan")
            report["stage"] = "installation"
            plan = await cli("install", "plan", "--import", draft["draft_id"], "--revision", str(revised["revision"]), "--json")
            operation = await finish(await cli("install", "apply", "--plan", plan["plan_id"], "--json"))
            slot_id = operation["slot_id"]
            checks["authorized_install"] = checks["smoke_fixture"] = True
            shutil.rmtree(package)
            connection = json.loads((state / "slots" / slot_id / "connection.json").read_bytes())
            value = await details(slot_id)
            require(len(value["processes"]) == 1)
            resident = value["processes"][0]
            keys = list((state / "slots" / slot_id / "runtime").glob("service-*.key"))
            require(len(keys) == 1)
            key = keys[0].read_text().strip()
            report["stage"] = "runtime_clients"
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                private = resident["endpoint"]
                require((await http.get(private + "/health")).status_code == 401)
                require((await http.get(private + "/health", headers={"authorization": "Bearer wrong"})).status_code == 401)
                health = await http.get(private + "/health", headers={"authorization": "Bearer " + key})
                require(health.status_code == 200 and health.json()["ready"] is True and health.json()["generation"] == value["slot"]["generation"])
                require((await http.get(private + "/health", headers={"authorization": "Bearer " + key, "origin": "https://invalid.test"})).status_code == 403)
                require((await http.get(private + "/health", headers={"authorization": "Bearer " + key, "host": "invalid.test"})).status_code == 403)
                checks["private_identity_authentication"] = True
                require((await http.get(connection["endpoint"])).status_code == 401)
                auth = {"authorization": "Bearer " + connection["token"]}
                require((await http.get(connection["endpoint"], headers={**auth, "host": "invalid.test"})).status_code == 403)
                require((await http.get(connection["endpoint"], headers={**auth, "origin": "https://invalid.test"})).status_code == 403)
                checks["public_host_origin_authentication"] = True
            parameters = StdioServerParameters(command=str(launcher), args=["--state-dir", str(state), "connect", "--slot", slot_id], env=env, cwd=str(home))
            with open("/dev/null", "w") as errlog:
                async with http_session(connection) as (first, session), http_session(connection) as (second, _):
                    async with stdio_client(parameters, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as stdio:
                            await stdio.initialize()
                            require([tool.model_dump(by_alias=True, exclude_none=True) for tool in (await first.list_tools()).tools] == tool_definitions)
                            identities = await asyncio.gather(*(call(client, "identity") for client in (first, second, stdio)))
                            require({item["pid"] for item in identities} == {resident["pid"]} and all(item["settings_available"] for item in identities))
                            checks["one_shared_http_resident"] = checks["http_and_stdio"] = True
                            require(await call(first, "add", {"left": 1, "right": 2}) == {"total": 9})
                            checks["configuration_inherited"] = True
                            closed_id = session()
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                response = await http.delete(connection["endpoint"], headers={**auth, "mcp-session-id": closed_id, "mcp-protocol-version": "2025-11-25"})
                require(response.status_code == 404)
                checks["session_delete"] = True
            await finish(await cli("slots", "stop", slot_id, "--json"))
            await stop()
            await start()
            await finish(await cli("slots", "start", slot_id, "--json"))
            restarted = await details(slot_id)
            new = restarted["processes"][0]
            new_keys = list((state / "slots" / slot_id / "runtime").glob("service-*.key"))
            new_key = new_keys[0].read_text().strip()
            require(new_key != key and new["instance_id"] != resident["instance_id"] and new["generation"] > resident["generation"]
                    and restarted["slot"]["public_port"] == value["slot"]["public_port"])
            async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
                require((await http.get(new["endpoint"] + "/health", headers={"authorization": "Bearer " + key})).status_code == 401)
                fresh_health = await http.get(new["endpoint"] + "/health", headers={"authorization": "Bearer " + new_key})
                require(fresh_health.status_code == 200 and fresh_health.json()["nonce"] != health.json()["nonce"]
                        and fresh_health.json()["generation"] == new["generation"])
                require((await http.post(connection["endpoint"], headers={**auth, "mcp-session-id": closed_id,
                    "mcp-protocol-version": "2025-11-25"}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 404)
            async with http_session(connection) as (client, _):
                require(await call(client, "add", {"left": 1, "right": 2}) == {"total": 9})
            checks["generation_restart_key_rotation"] = True
            await stop()
            require(all(json.loads(path.read_bytes()) == {} for path in (state / "slots").glob("*/ownership.json")))
            checks["cleanup"] = True
        finally:
            await stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    args = parser.parse_args()
    report = {"verified": False, "checks": {check: False for check in CHECKS}}
    try:
        asyncio.run(verify(args, report))
        require(all(report["checks"].values()))
        report["verified"] = True
        report.pop("stage", None)
    except BaseException:
        report["error"] = "http_phase2_verification_failed"
        print(json.dumps(report, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
