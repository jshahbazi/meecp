"""Drive the packaged native consent flow in a temporary installation.

Allow the two synthetic requests in the app. No default installation, client
configuration, provider, or model is used. Output contains fixed receipts only.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from mcpme.__main__ import call
from mcpme.authorization import process_start
from mcpme.grants import GrantError
from mcpme.private import read_private_json
from mcpme_local_transport import checked_socket, socket_path


def receipt(stage):
    print(json.dumps({"stage": stage}), flush=True)


def same_process(identity):
    try:
        return process_start(identity[0]) == identity[1]
    except GrantError:
        return False


@asynccontextmanager
async def client(configuration):
    entry = configuration["mcpServers"]["mcpme-management"]
    with open(os.devnull, "w") as errors:
        async with stdio_client(StdioServerParameters(command=entry["command"], args=entry["args"]), errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                started = time.monotonic()
                async with asyncio.timeout(2):
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 42
                assert time.monotonic() - started < 2
                yield session


async def tool(session, name, arguments):
    value = await session.call_tool(name, arguments)
    assert not value.isError, "native_tool_failed"
    return value.structuredContent


async def approved(session):
    async with asyncio.timeout(120):
        while True:
            value = await tool(session, "mcpme_get_authorization_status", {})
            assert value["status"] not in {"denied", "expired", "revoked", "unavailable"}, "native_approval_unavailable"
            if value["status"] == "approved":
                return
            await asyncio.sleep(0.2)


async def verify(app):
    executable = app / "Contents/MacOS/mcpme"
    starter = app / "Contents/Resources/starter-packages/json-utilities"
    assert executable.is_file() and starter.is_dir(), "native_bundle_invalid"
    scope = Path(tempfile.mkdtemp(prefix="mcpme-native-authorization-")).resolve()
    state = scope / "state"
    supervisor = None
    slot_ids = set()
    process = await asyncio.create_subprocess_exec(str(executable), "--ui-state-dir", str(state),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        receipt("native_host_starting")
        async with asyncio.timeout(120):
            while not (state / "authorization-bootstrap.json").is_file():
                assert process.returncode is None, "native_host_failed"
                await asyncio.sleep(0.1)
        supervisor_pid = read_private_json(state / "authorization-bootstrap.json")["supervisor_pid"]
        supervisor = (supervisor_pid, process_start(supervisor_pid))
        receipt("native_host_ready")
        exported = await asyncio.create_subprocess_exec(str(executable), "--state-dir", str(state), "management", "export", "--json",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await exported.communicate()
        assert exported.returncode == 0 and len(output) < 8192, "native_export_failed"
        registration = json.loads(output)
        configuration = registration["configuration"]
        assert "--credential-file" not in configuration["mcpServers"]["mcpme-management"]["args"]
        async with client(configuration) as session:
            receipt("native_base_pending")
            await approved(session)
            await tool(session, "mcpme_capabilities", {})
            initial = (await call(state, "GET", "/v1/authorization/sessions"))["sessions"]
            assert len(initial) == 1
            plan = await tool(session, "mcpme_plan_install", {"package": str(starter), "name": "Authorization fixture", "start": False})
            slot_ids.add(plan["slot_id"])
            denied = await session.call_tool("mcpme_apply_install", {"plan_id": plan["plan_id"]})
            assert denied.isError and denied.structuredContent["error"]["code"] == "authorization_required"
            await tool(session, "mcpme_request_access", {"action": {"kind": "install", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"]}})
            receipt("native_exact_plan_pending")
            await approved(session)
            assert not (await call(state, "GET", "/v1/status"))["operations"], "native_work_replayed"
            operation = await tool(session, "mcpme_apply_install", {"plan_id": plan["plan_id"]})
            async with asyncio.timeout(45):
                while True:
                    status = await tool(session, "mcpme_get_operation", {"operation_id": operation["operation_id"]})
                    if status["status"] == "completed":
                        break
                    assert status["status"] in {"pending", "running"}, "native_install_failed"
                    await asyncio.sleep(0.2)
        async with asyncio.timeout(5):
            while (await call(state, "GET", "/v1/authorization/sessions"))["sessions"]:
                await asyncio.sleep(0.1)
        async with client(configuration) as session:
            await approved(session)  # Remembered account policy; no third popup.
            renewed = (await call(state, "GET", "/v1/authorization/sessions"))["sessions"]
            assert len(renewed) == 1 and renewed[0]["session_id"] != initial[0]["session_id"]
            await call(state, "POST", "/v1/authorization/profiles/" + registration["profile_id"] + "/revoke", {})
            denied = await session.call_tool("mcpme_capabilities", {})
            assert denied.isError, "native_revocation_failed"
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill(); await process.wait()
        async with asyncio.timeout(45):
            while supervisor is not None and same_process(supervisor):
                await asyncio.sleep(0.1)
        if state.exists():
            try:
                manager_socket = socket_path(state, "manager")
            except FileNotFoundError:
                manager_socket = None
            if manager_socket is not None:
                for service in {"manager", "authorization", *slot_ids}:
                    endpoint = socket_path(state, service)
                    if endpoint.exists():
                        checked_socket(endpoint)
                        endpoint.unlink()
                manager_socket.parent.rmdir()
        # Only this verifier's freshly allocated, private temporary scope.
        for directory, subdirectories, _ in os.walk(scope, followlinks=False):
            os.chmod(directory, 0o700)
        shutil.rmtree(scope)
    receipt("native_authorization_passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(verify(args.app.resolve(strict=True)))
    except (Exception, KeyboardInterrupt):
        receipt("native_authorization_failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
