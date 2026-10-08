"""Verify scoped management through a relocated bundled host and real MCP clients."""
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
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify(host_path: Path, package_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="mcpme-management-") as temporary:
        root = Path(temporary).resolve()
        host = root / "relocated host"
        shutil.copytree(host_path, host, symlinks=True)
        package = root / "synthetic package"
        shutil.copytree(package_path, package)
        state = root / "private state"
        launcher, reaper = host / "bin/meecp", host / "bin/meecp-reap"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "en_US.UTF-8"}
        process = None

        def command(*args):
            result = subprocess.run([str(launcher), "--state-dir", str(state), *args],
                cwd=root, env=environment, capture_output=True, text=True, timeout=330)
            value = json.loads(result.stdout)
            if result.returncode or "error" in value:
                raise ValueError("management_cli_failed")
            return value

        async def start():
            launched = subprocess.Popen([str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper)],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with asyncio.timeout(30):
                    while True:
                        if launched.poll() is not None:
                            raise ValueError("management_host_failed")
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

        def parameters(grant):
            exported = command("--credential-file", grant["credential_file"], "management", "export", "--json")
            configuration = next(iter(exported["configuration"]["mcpServers"].values()))
            return StdioServerParameters(**configuration, env=environment)

        async def invoke(session, name, arguments=None, error=None):
            result = await session.call_tool(name, arguments or {})
            if error:
                assert result.isError and result.structuredContent["error"]["code"] == error
            else:
                assert not result.isError
            return result.structuredContent

        async def wait(session, operation):
            async with asyncio.timeout(60):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.05)
                    operation = await invoke(session, "meecp_get_operation", {"operation_id": operation["operation_id"]})
            assert operation["status"] == "completed"
            return operation

        try:
            process = await start()
            preparation = command("authorize", "--prepare", "--json")
            with open(os.devnull, "w") as errors:
                async with stdio_client(parameters(preparation), errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        assert initialized.protocolVersion == "2025-11-25"
                        assert set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}
                        listed = await session.list_tools()
                        assert len(listed.tools) == 18 and not any("authorize" in item.name for item in listed.tools)
                        caps = await invoke(session, "meecp_capabilities")
                        assert "scoped_grants" not in caps["unsupported"] and caps["management_mcp"] == "scoped_stdio"
                        draft = await invoke(session, "meecp_create_import", {
                            "source": str(package), "name": "Management fixture", "start": False})
                        plan = await invoke(session, "meecp_plan_install", {"import_id": draft["draft_id"], "revision": 1})
                        await invoke(session, "meecp_apply_install", {"plan_id": plan["plan_id"]}, error="grant_scope")
                installation = command("authorize", "--plan", plan["plan_id"], "--json")
                installation_parameters = parameters(installation)
                async with stdio_client(installation_parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        reviewed = await invoke(session, "meecp_inspect_install_plan", {"plan_id": plan["plan_id"]})
                        assert reviewed["plan_digest"] == installation["plan_digest"]
                        first, duplicate = await asyncio.gather(*(invoke(session, "meecp_apply_install", {"plan_id": plan["plan_id"]}) for _ in range(2)))
                        assert first["operation_id"] == duplicate["operation_id"]
                        operation = await wait(session, first)
                        await invoke(session, "meecp_start_slot", {"slot_id": operation["slot_id"]}, error="grant_scope")
                process.terminate()
                await asyncio.to_thread(process.wait, 30)
                process = await start()
                async with stdio_client(installation_parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        resumed = await invoke(session, "meecp_apply_install", {"plan_id": plan["plan_id"]})
                        assert resumed["operation_id"] == operation["operation_id"]
                lifecycle = command("authorize", "--slot", operation["slot_id"], "--action", "start", "--action", "stop", "--json")
                async with stdio_client(parameters(lifecycle), errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await wait(session, await invoke(session, "meecp_start_slot", {"slot_id": operation["slot_id"]}))
                        await invoke(session, "meecp_restart_slot", {"slot_id": operation["slot_id"]}, error="grant_scope")
                        await wait(session, await invoke(session, "meecp_stop_slot", {"slot_id": operation["slot_id"]}))
                        command("grants", "revoke", lifecycle["grant_id"], "--json")
                        await invoke(session, "meecp_capabilities", error="grant_credential_unavailable")
            assert len(command("slots", "list", "--json")["slots"]) == 1
            return {"verified": True, "relocated_host": True, "management_stdio_sdk": True,
                    "preparation_scope": True, "exact_plan_grant": True, "duplicate_apply": True,
                    "restart_binding": True, "lifecycle_scope": True, "revocation": True}
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
    parser.add_argument("--package", type=Path, required=True, help="Synthetic JSON Utilities fixture package.")
    arguments = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(asyncio.run(verify(arguments.host.resolve(), arguments.package.resolve()))))
    except Exception:
        raise SystemExit("management_verification_failed") from None
