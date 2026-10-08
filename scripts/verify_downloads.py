"""Verify pinned package staging with an offline fixture and a relocated bundled host.

The fixture injects an in-process synthetic transport into the bundled downloader
before the real supervisor starts. The supervisor, owner CLI, HTTPS URL policy,
installation, and MCP connections then run through their production paths.
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
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from prepare_package_archive import prepare


BUNDLED_FIXTURE = """
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sys

from meecp.downloads import DownloadManager

class SyntheticNetwork:
    @asynccontextmanager
    async def stream(self, url):
        async def chunks():
            with Path(sys.argv[2]).open('rb') as source:
                if url.endswith('/interrupted.zip') or url.endswith('/canceled.zip'):
                    yield source.read(1)
                    await asyncio.Event().wait()
                while chunk := source.read(16384):
                    yield chunk
                    await asyncio.sleep(0)
        yield chunks()

async def run():
    manager = DownloadManager(Path(sys.argv[1]), network=SyntheticNetwork())
    specification = json.loads(sys.argv[3])

    async def received(identity):
        async with asyncio.timeout(30):
            while manager.get(identity)['received_bytes'] < 1:
                await asyncio.sleep(0.01)

    try:
        record = await manager.start(specification)
        async with asyncio.timeout(30):
            while record['status'] in {'pending', 'running'}:
                await asyncio.sleep(0.01)
                record = manager.get(record['download_id'])
        if record['status'] != 'completed':
            raise ValueError('synthetic_download_failed')
        canceled = await manager.start({**specification, 'url': 'https://packages.example.test/canceled.zip'})
        await received(canceled['download_id'])
        await manager.cancel(canceled['download_id'])
        async with asyncio.timeout(30):
            while manager.get(canceled['download_id'])['status'] in {'pending', 'running'}:
                await asyncio.sleep(0.01)
        if manager.get(canceled['download_id'])['status'] != 'canceled':
            raise ValueError('synthetic_cancel_failed')
        interrupted = await manager.start({**specification, 'url': 'https://packages.example.test/interrupted.zip'})
        await received(interrupted['download_id'])
    finally:
        await manager.close()
    recovered = DownloadManager(Path(sys.argv[1]), network=SyntheticNetwork())
    try:
        retained = recovered.get(interrupted['download_id'])
        if retained['status'] != 'failed' or retained['error_code'] != 'download_interrupted':
            raise ValueError('synthetic_interruption_failed')
        print(json.dumps({'completed': recovered.get(record['download_id']),
            'canceled_download_id': canceled['download_id'], 'interrupted_download_id': interrupted['download_id']}))
    finally:
        await recovered.close()

os.umask(0o077)
logging.disable(logging.CRITICAL)
try:
    asyncio.run(run())
except Exception:
    raise SystemExit('synthetic_download_failed') from None
"""


async def verify(host_path: Path, package_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="meecp-downloads-") as temporary:
        root = Path(temporary).resolve()
        host = root / "relocated host"
        shutil.copytree(host_path, host, symlinks=True)
        package = root / "synthetic package"
        shutil.copytree(package_path, package)
        archive = root / "pinned package.zip"
        pins = prepare(package, archive)
        state = root / "private state"
        launcher, reaper = host / "bin/meecp", host / "bin/meecp-reap"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "en_US.UTF-8"}
        process = None

        def command(*arguments, error=False):
            result = subprocess.run([str(launcher), "--state-dir", str(state), *arguments],
                cwd=root, env=environment, capture_output=True, text=True, timeout=330)
            value = json.loads(result.stdout)
            if error:
                if result.returncode != 1 or "error" not in value:
                    raise ValueError("download_expected_denial")
            elif result.returncode or "error" in value:
                raise ValueError("download_cli_failed")
            return value

        async def start():
            launched = subprocess.Popen([str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper)],
                cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with asyncio.timeout(30):
                    while True:
                        if launched.poll() is not None:
                            raise ValueError("download_host_failed")
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

        async def wait(operation):
            async with asyncio.timeout(60):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.05)
                    operation = command("operations", "get", operation["operation_id"], "--json")
            if operation["status"] != "completed":
                raise ValueError("download_install_failed")
            return operation

        try:
            specification = {"url": "https://packages.example.test/synthetic-package.zip",
                "sha256": pins["sha256"], "size": pins["size"], "package_digest": pins["package_digest"]}
            staged = subprocess.run([str(host / "python/bin/python3.12"), "-I", "-S", "-c",
                "import sys; sys.path[:0]=sys.argv[1:3]; code=sys.argv[3]; sys.argv=sys.argv[3:]; exec(code)",
                str(host / "backend"), str(host / "python/site-packages"), BUNDLED_FIXTURE,
                str(state), str(archive), json.dumps(specification)],
                cwd=root, env=environment, capture_output=True, text=True, timeout=60)
            if staged.returncode:
                raise ValueError("bundled_download_fixture_failed")
            fixture_result = json.loads(staged.stdout)
            fixture = fixture_result["completed"]
            assert fixture["status"] == "completed" and fixture["package_digest"] == pins["package_digest"]
            process = await start()
            caps = command("capabilities", "--json")
            assert "downloads" not in caps["unsupported"]
            retained = command("downloads", "inspect", fixture["download_id"], "--json")
            assert retained["status"] == "completed" and retained["package_path"] == fixture["package_path"]
            repeated = command("downloads", "fetch", "--url", specification["url"],
                "--sha256", pins["sha256"], "--size", str(pins["size"]),
                "--package-digest", pins["package_digest"], "--json")
            assert repeated["download_id"] == fixture["download_id"] and repeated["status"] == "completed"
            listed = command("downloads", "list", "--json")
            assert any(item["download_id"] == fixture["download_id"] for item in listed["downloads"])
            canceled = command("downloads", "inspect", fixture_result["canceled_download_id"], "--json")
            assert canceled["status"] == "canceled" and "package_path" not in canceled
            interrupted = command("downloads", "inspect", fixture_result["interrupted_download_id"], "--json")
            assert interrupted["status"] == "failed" and interrupted["error_code"] == "download_interrupted"
            assert interrupted["received_bytes"] == 1 and "package_path" not in interrupted
            cancel_retained = command("downloads", "cancel", canceled["download_id"], "--json")
            assert cancel_retained["status"] == "canceled"
            command("downloads", "fetch", "--url", "https://127.0.0.1/package.zip",
                "--sha256", pins["sha256"], "--size", str(pins["size"]), "--json", error=True)
            grant = command("authorize", "--read-only", "--json")
            denied = command("--credential-file", grant["credential_file"], "downloads", "inspect",
                fixture["download_id"], "--json", error=True)
            assert denied["error"]["code"] == "grant_scope"
            process.terminate()
            await asyncio.to_thread(process.wait, 30)
            process = await start()
            resumed = command("downloads", "inspect", fixture["download_id"], "--json")
            assert resumed["status"] == "completed" and resumed["package_digest"] == pins["package_digest"]
            plan = command("install", "plan", "--package", resumed["package_path"], "--start", "--json")
            assert plan["package_digest"] == pins["package_digest"]
            operation = await wait(command("install", "apply", "--plan", plan["plan_id"], "--json"))
            connection = json.loads((state / "slots" / operation["slot_id"] / "connection.json").read_bytes())
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
                async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        assert "validate_json" in {item.name for item in (await client.list_tools()).tools}
                        result = await client.call_tool("validate_json", {"document": '{"value":1}'})
                        assert not result.isError and result.structuredContent == {"valid": True, "kind": "object"}
            parameters = StdioServerParameters(command=str(launcher),
                args=["--state-dir", str(state), "connect", "--slot", operation["slot_id"]], env=environment)
            with open(os.devnull, "w") as errors:
                async with stdio_client(parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("validate_json", {"document": '{"value":1}'})
                        assert not result.isError and result.structuredContent == {"valid": True, "kind": "object"}
            await wait(command("slots", "stop", operation["slot_id"], "--json"))
            return {"verified": True, "relocated_host": True, "synthetic_download_transport": True,
                    "pinned_archive_staging": True, "duplicate_fetch": True, "completed_download_restart": True,
                    "partial_download_interrupted": True, "download_cancellation": True,
                    "private_url_rejected": True, "owner_only_downloads": True,
                    "installed_download_package": True, "http_sdk": True, "stdio_sdk": True}
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
        raise SystemExit("download_verification_failed") from None
