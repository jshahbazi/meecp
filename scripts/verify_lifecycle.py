"""Exercise only this bundle's owned processes and relocation; no UI automation."""
import argparse
import asyncio
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import subprocess
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logging.disable(logging.CRITICAL)
QUESTION = {"type": "noul", "instructions": "Does the stated policy allow access?"}
STATE = "Access requires an active membership. The membership is active."


def require(value, description):
    if not value:
        raise RuntimeError(description)


def command(pid):
    return subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="], text=True).strip()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def wait_gone(pids):
    deadline = time.monotonic() + 8
    while any(alive(p) for p in pids) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    require(not any(alive(p) for p in pids), "Owned process survived termination")


async def status():
    connection = json.loads((Path.home() / "Library/Application Support/JevK5/connection.json").read_text())
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        result = await client.get(connection["endpoint"] + "/api/status", headers={"Authorization": "Bearer " + connection["token"]})
        require(result.status_code == 200, "Authenticated status failed")
        return result.json()


async def through_stdio(app):
    with open(os.devnull, "w") as error_sink:
        async with stdio_client(StdioServerParameters(command=str(app / "Contents/MacOS/jevk5-mcp"), args=[]), errlog=error_sink) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=150)) as session:
                await session.initialize()
                result = await session.call_tool("jevk5_decide", {"state": STATE, "question": QUESTION})
                require(not result.isError and result.structuredContent is not None, "Stdio startup decision failed")
    return await status()


def owned_pids(app, snapshot):
    backend = snapshot["backend_pid"]
    require(command(backend) == str(app / "Contents/Resources/python/bin/python3.12"), "Unexpected backend ownership")
    menu = int(subprocess.check_output(["ps", "-p", str(backend), "-o", "ppid="], text=True))
    require(command(menu) == str(app / "Contents/MacOS/JevK5MenuBar"), "Unexpected menu app ownership")
    supervisor = snapshot["llama_pid"]
    require(command(supervisor) == str(app / "Contents/Resources/llama/llama-server"), "Unexpected supervisor ownership")
    real = int(subprocess.check_output(["pgrep", "-P", str(supervisor)], text=True).strip())
    require(command(real) == str(app / "Contents/Resources/llama/llama-server.real"), "Unexpected inference ownership")
    return menu, backend, supervisor, real


async def main(args):
    app = args.app.resolve()
    snapshot = await status()
    menu, backend, supervisor, real = owned_pids(app, snapshot)
    os.kill(backend, 9)
    await wait_gone([backend, supervisor, real])
    recovered = await through_stdio(app)
    require(recovered["backend_pid"] != backend, "Existing menu app did not recover its backend")
    menu, backend, supervisor, real = owned_pids(app, recovered)
    os.kill(menu, 15)
    await wait_gone([menu, backend, supervisor, real])
    relocation = app.parent / "Relocation Test" / app.name
    relocation.parent.mkdir(exist_ok=True)
    require(not relocation.exists(), "Relocation destination already exists")
    app.rename(relocation)
    try:
        moved = await through_stdio(relocation)
        moved_pids = owned_pids(relocation, moved)
        os.kill(moved_pids[0], 15)
        await wait_gone(moved_pids)
    finally:
        # Restore the requested artifact path. Do not replace unrelated files.
        if relocation.exists() and not app.exists():
            relocation.rename(app)
    final = await through_stdio(app)
    owned_pids(app, final)
    receipt = {"checks": ["backend_sigkill_reaps_inference", "stdio_recovers_existing_menu_app", "app_exit_reaps_backend_and_inference", "relocated_stdio_auto_launch", "relocated_bundled_runtime_decision", "restored_bundle_auto_launch"],
               "app": str(app), "state": final["state"], "ready": final["ready"], "single_owned_model": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"passed": len(receipt["checks"]), "ready": final["ready"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--app", type=Path, default=root / "dist/JevK5.app")
    parser.add_argument("--output", type=Path, default=root / "test-artifacts/lifecycle.json")
    asyncio.run(main(parser.parse_args()))
