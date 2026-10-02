"""Exercise raw Python intake through a relocated private host and real MCP clients."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


async def verify(host_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="mcpme-phase2-") as temporary:
        root = Path(temporary).resolve()
        host = root / "relocated-host"
        shutil.copytree(host_path, host, symlinks=True)
        state = root / "state"
        launcher = host / "bin/mcpme"
        reaper = host / "bin/mcpme-reap"
        process = None
        runtime_environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "en_US.UTF-8"}

        def command(*arguments):
            result = subprocess.run([str(launcher), "--state-dir", str(state), *arguments],
                cwd=root, env=runtime_environment, capture_output=True, text=True, timeout=300)
            value = json.loads(result.stdout)
            if result.returncode or "error" in value:
                raise ValueError("phase2_cli_failed")
            return value

        async def start():
            launched = subprocess.Popen([str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper)],
                cwd=root, env=runtime_environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with asyncio.timeout(30):
                    while True:
                        if launched.poll() is not None:
                            raise ValueError("phase2_host_failed")
                        if (state / "manager-connection.json").exists():
                            try:
                                command("capabilities", "--json")
                                return launched
                            except Exception:
                                pass
                        await asyncio.sleep(0.05)
            except BaseException:
                if launched.poll() is None:
                    launched.terminate()
                    await asyncio.to_thread(launched.wait, 30)
                raise

        try:
            process = await start()
            source = root / "raw-python"
            source.mkdir(mode=0o700)
            (source / "validator.py").write_text(
                "import json\ndef validate_json(document):\n"
                "    try:\n        json.loads(document)\n        return {'valid': True}\n"
                "    except ValueError:\n        return {'valid': False}\n")
            (source / "LICENSE").write_text("Synthetic fixture dedicated to the public domain under CC0.\n")
            draft = command("imports", "create", "--source", str(source), "--name", "Phase 2 Python",
                "--intent", "Expose only validate_json(document) and return a validity flag.", "--start", "--json")
            specification = command("imports", "export", draft["draft_id"], "--revision", str(draft["revision"]), "--json")
            setup = root / "setup.json"
            setup.write_text(json.dumps(specification)); setup.chmod(0o600)
            tool = root / "tool.json"
            tool.write_text(json.dumps({"name": "validate_json", "description": "Report whether a JSON document is valid.",
                "input_schema": {"type": "object", "properties": {"document": {"type": "string"}},
                                 "required": ["document"], "additionalProperties": False},
                "output_schema": {"type": "object", "properties": {"valid": {"type": "boolean"}},
                                  "required": ["valid"], "additionalProperties": False}}))
            package = root / "prepared-python"
            built = subprocess.run([str(host / "python/bin/python3.12"), "-I", "-S", "-c",
                "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                str(host / "python/site-packages"), str(host / "templates/prepare.py"), "python-function",
                "--setup-export", str(setup), "--tool-spec", str(tool), "--module", "validator", "--function", "validate_json",
                "--source-license", "LICENSE", "--package-id", "local.phase2-python", "--name", "Phase 2 Python",
                "--output", str(package)], capture_output=True, text=True, timeout=300)
            if built.returncode or not json.loads(built.stdout).get("prepared"):
                raise ValueError("phase2_preparation_failed")
            direct = command("imports", "create", "--source", str(package), "--name", "Prepared Python", "--json")
            assert direct["state"] == "ready_to_plan" and direct["source"]["file_inventory"] == "mcpme.lock.json"
            attached = command("imports", "attach", draft["draft_id"], "--revision", str(draft["revision"]), "--package", str(package), "--json")
            assert attached["state"] == "ready_to_plan"
            first_plan = command("install", "plan", "--import", draft["draft_id"], "--revision", str(draft["revision"]), "--json")
            second_plan = command("install", "plan", "--import", draft["draft_id"], "--revision", str(draft["revision"]), "--json")
            operation = command("install", "apply", "--plan", first_plan["plan_id"], "--json")
            duplicate = command("install", "apply", "--plan", second_plan["plan_id"], "--json")
            assert operation["operation_id"] == duplicate["operation_id"]
            async with asyncio.timeout(60):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.05)
                    operation = command("operations", "get", operation["operation_id"], "--json")
            assert operation["status"] == "completed"
            installed = command("imports", "inspect", draft["draft_id"], "--json")
            assert installed["state"] == "installed" and installed["result_slot_id"] == operation["slot_id"]
            assert len(command("slots", "list", "--json")["slots"]) == 1
            connection = json.loads((state / "slots" / operation["slot_id"] / "connection.json").read_bytes())
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
                async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        assert [item.name for item in (await client.list_tools()).tools] == ["validate_json"]
                        result = await client.call_tool("validate_json", {"document": '{"synthetic":true}'})
                        assert not result.isError and result.structuredContent == {"valid": True}
            parameters = StdioServerParameters(command=str(launcher), args=["--state-dir", str(state), "connect", "--slot", operation["slot_id"]], env=runtime_environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("validate_json", {"document": "invalid"})
                    assert not result.isError and result.structuredContent == {"valid": False}
            stopped = command("slots", "stop", operation["slot_id"], "--json")
            async with asyncio.timeout(30):
                while stopped["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.05)
                    stopped = command("operations", "get", stopped["operation_id"], "--json")
            assert stopped["status"] == "completed"
            process.terminate()
            await asyncio.to_thread(process.wait, 30)
            process = await start()
            resumed = command("imports", "inspect", draft["draft_id"], "--json")
            assert resumed["state"] == "installed" and resumed["result_slot_id"] == operation["slot_id"]
            return {"verified": True, "raw_python_pipeline": True, "prepared_package_intake": True,
                    "duplicate_plans_one_operation": True,
                    "relocated_host": True, "http_sdk": True, "stdio_sdk": True, "draft_restart": True}
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(process.wait, 30)
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait, 10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    arguments = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(asyncio.run(verify(arguments.host.resolve()))))
    except Exception:
        raise SystemExit("phase2_verification_failed") from None
