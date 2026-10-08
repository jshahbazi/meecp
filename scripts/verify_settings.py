"""Verify configuration and secret references through a relocated bundled host.

The sealed synthetic stdio service reads the inherited private settings descriptor
once, closes it, and exposes only booleans. No secret is returned by its MCP tool
or printed by this verifier. Revocation prevents later delivery; a running service
retains the synthetic value it already received until that process exits.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from meecp.contracts import validate_package  # noqa: E402


SYNTHETIC_VALUE = "phase2 synthetic service key"
SYNTHETIC_GREETING = "Synthetic greeting"
EXPECTED_RESULT = {"settings_available": True, "secret_matches": True, "configuration_matches": True}
TOOL = {"name": "check_settings", "description": "Check synthetic inherited settings without returning their values.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    "output_schema": {"type": "object", "properties": {
        "settings_available": {"type": "boolean"}, "secret_matches": {"type": "boolean"},
        "configuration_matches": {"type": "boolean"}},
        "required": ["settings_available", "secret_matches", "configuration_matches"], "additionalProperties": False}}


class VerificationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

ADAPTER = '''"""Synthetic stdio fixture. Never emit settings or raw failures."""
import json, os, sys

try:
    descriptor = int(os.environ.pop("MCPME_SETTINGS_FD"))
    with os.fdopen(descriptor, "rb") as source:
        payload = source.read(512 * 1024 + 1)
        if len(payload) > 512 * 1024:
            raise ValueError()
        settings = json.loads(payload)
    checked = {"settings_available": settings.get("schema_version") == 1,
        "secret_matches": settings.get("secrets", {}).get("service_key") == "phase2 synthetic service key",
        "configuration_matches": settings.get("configuration") == {"greeting": "Synthetic greeting", "enabled": True}}
except Exception:
    checked = {"settings_available": False, "secret_matches": False, "configuration_matches": False}

tool = json.loads((__import__("pathlib").Path(__file__).parent / "tool.json").read_bytes())
for line in sys.stdin.buffer:
    try:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-settings", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": tool["name"], "description": tool["description"],
                "inputSchema": tool["input_schema"], "outputSchema": tool["output_schema"]}]}
        elif method == "tools/call" and request.get("params", {}).get("name") == "check_settings":
            result = {"content": [{"type": "text", "text": json.dumps(checked)}], "structuredContent": checked, "isError": False}
        elif method == "ping":
            result = {}
        else:
            response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method unavailable"}}
            print(json.dumps(response), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    except Exception:
        raise SystemExit(1) from None
'''


def synthetic_package(host: Path, base: Path, output: Path) -> None:
    """Seal a fixture with the host's copied CPython and retained runtime licenses."""
    original = validate_package(base)
    output.mkdir(mode=0o700)
    (output / "licenses").mkdir(mode=0o700)
    shutil.copyfile(base / original.manifest.licenses[0], output / "licenses/fixture-LICENSE.txt")
    shutil.copytree(host / "python", output / "python", symlinks=False,
                   ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"))
    shutil.copyfile(host / "python/lib/python3.12/LICENSE.txt", output / "licenses/python-LICENSE.txt")
    (output / "licenses/runtime-NOTICE.md").write_text(
        "# Synthetic verifier runtime\n\n"
        "The interpreter is copied from the relocated, bundled host. CPython's license is "
        "python-LICENSE.txt. The runtime's accompanying third-party licenses are retained "
        "inside python. This fixture uses only the standard library.\n")
    (output / "adapter.py").write_text(ADAPTER)
    (output / "tool.json").write_text(json.dumps(TOOL))
    (output / "bin").mkdir(mode=0o700)
    launcher = output / "bin/service"
    launcher.write_text('#!/bin/sh\nset -eu\n'
        'BIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'PACKAGE=$(CDPATH= cd -- "$BIN/.." && pwd)\n'
        'exec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/adapter.py" 2>/dev/null\n')
    launcher.chmod(0o700)
    manifest = {"schema_version": 1, "host_api_version": 1, "package_id": "local.settings-fixture",
        "version": "1.0.0", "source_revision": "sha256:" + hashlib.sha256(ADAPTER.encode()).hexdigest(),
        "name": "Synthetic Settings", "profile": "mcp-stdio",
        "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "entrypoint": {"executable": "bin/service", "args": [], "cwd": "."},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "lifecycle": {"startup_timeout_seconds": 30, "operation_timeout_seconds": 30},
        "tools": [TOOL], "access": {"filesystem": [], "network": [], "secrets": ["service_key"], "writes": False},
        "configuration": {"schema_version": 1, "fields": {
            "greeting": {"type": "string", "required": True, "min_length": 1, "max_length": 64},
            "enabled": {"type": "boolean", "default": True}}},
        "resources": {"memory_mb": 256, "cpu_only": True},
        "licenses": ["licenses/fixture-LICENSE.txt", "licenses/python-LICENSE.txt", "licenses/runtime-NOTICE.md"]}
    manifest_path = output / "meecp.package.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    artifacts = []
    for directory, names, files in os.walk(output):
        names.sort()
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(output).as_posix()
            if relative == "meecp.package.json":
                continue
            with path.open("rb") as source:
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
            runtime = relative.startswith("python/")
            artifacts.append({"path": relative, "sha256": checksum, "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
                "source": "bundled-host-private-python" if runtime else "synthetic-verifier-fixture",
                "provenance": "Copied bundled private runtime" if runtime else "Synthetic MIT-licensed settings acceptance fixture",
                "license": "licenses/runtime-NOTICE.md" if runtime else "licenses/fixture-LICENSE.txt"})
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": "1.0.0",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "artifacts": artifacts,
        "build": {"recipe": "synthetic-settings-verifier-v1", "builder": "verify_settings.py",
                  "toolchain": "copied bundled CPython; standard-library stdio fixture"}}
    (output / "meecp.lock.json").write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n")
    validate_package(output)


async def verify(host_path: Path, package_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="meecp-settings-") as temporary:
        root = Path(temporary).resolve()
        host, package, state = root / "relocated host", root / "synthetic package", root / "private state"
        shutil.copytree(host_path, host, symlinks=True)
        try:
            synthetic_package(host, package_path, package)
        except Exception:
            raise VerificationError("settings_fixture_failed") from None
        launcher, reaper = host / "bin/meecp", host / "bin/meecp-reap"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "en_US.UTF-8"}
        process = None

        def error_code(value):
            code = value.get("error", {}).get("code", value.get("error_code"))
            return code if isinstance(code, str) and re.fullmatch("[a-z][a-z0-9_]{0,63}", code) else "unexpected_error"

        def command(*arguments, error=False):
            result = subprocess.run([str(launcher), "--state-dir", str(state), *arguments],
                cwd=root, env=environment, capture_output=True, text=True, timeout=330)
            if SYNTHETIC_VALUE in result.stdout or SYNTHETIC_VALUE in result.stderr:
                raise ValueError("settings_value_disclosed")
            value = json.loads(result.stdout)
            if error:
                if result.returncode != 1 or "error" not in value:
                    raise ValueError("settings_expected_denial")
            elif result.returncode or "error" in value:
                raise VerificationError("settings_" + stage + "_" + error_code(value))
            return value

        def private_json(path, value):
            path.write_text(json.dumps(value))
            path.chmod(0o600)

        async def start():
            launched = subprocess.Popen([str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper)],
                cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with asyncio.timeout(30):
                    while True:
                        if launched.poll() is not None:
                            raise ValueError("settings_host_failed")
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

        async def wait(operation, *, failure=None):
            async with asyncio.timeout(60):
                while operation["status"] in {"pending", "running"}:
                    await asyncio.sleep(0.05)
                    operation = command("operations", "get", operation["operation_id"], "--json")
            if failure is None:
                if operation["status"] != "completed":
                    raise VerificationError("settings_" + stage + "_" + error_code(operation))
            else:
                assert operation["status"] == "failed" and error_code(operation) == failure
            return operation

        async def http_check(slot_id, *, revoke_secret=None):
            connection = json.loads((state / "slots" / slot_id / "connection.json").read_bytes())
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
                async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        assert [item.name for item in (await client.list_tools()).tools] == ["check_settings"]
                        result = await client.call_tool("check_settings", {})
                        assert not result.isError and result.structuredContent == EXPECTED_RESULT
                        if revoke_secret is not None:
                            revoked = command("secrets", "revoke", revoke_secret, "--json")
                            assert revoked["status"] == "revoked"
                            result = await client.call_tool("check_settings", {})
                            assert not result.isError and result.structuredContent == EXPECTED_RESULT

        def deny_secret_access(grant, secret_id, secret_file):
            credential = grant["credential_file"]
            for arguments in [("secrets", "list", "--json"), ("secrets", "inspect", secret_id, "--json"),
                ("secrets", "create", "--name", "denied", "--file", str(secret_file), "--json"),
                ("secrets", "revoke", secret_id, "--json")]:
                denial = command("--credential-file", credential, *arguments, error=True)
                assert denial["error"]["code"] == "grant_scope"

        stage = "host_start"
        try:
            process = await start()
            stage = "secret_metadata"
            secret_file = root / "private secret input"
            secret_file.write_text(SYNTHETIC_VALUE)
            secret_file.chmod(0o600)
            secret = command("secrets", "create", "--name", "Synthetic service key", "--file", str(secret_file), "--json")
            identity = secret["secret_id"]
            assert secret["status"] == "active"
            assert command("secrets", "inspect", identity, "--json")["secret_id"] == identity
            assert command("secrets", "list", "--json")["count"] == 1
            stage = "missing_configuration"
            missing_config = command("install", "plan", "--package", str(package), "--json", error=True)
            assert missing_config["error"]["code"] == "configuration_required"
            settings_path = root / "private settings.json"
            private_json(settings_path, {"configuration": {"greeting": SYNTHETIC_GREETING}, "secret_references": {}})
            missing_reference = command("install", "plan", "--package", str(package), "--settings", str(settings_path), "--json", error=True)
            assert missing_reference["error"]["code"] == "secret_reference_unavailable"
            stage = "exact_plan"
            settings = {"schema_version": 1, "configuration": {"greeting": SYNTHETIC_GREETING},
                        "secret_references": {"service_key": identity}}
            private_json(settings_path, settings)
            plan_args = ("install", "plan", "--package", str(package), "--settings", str(settings_path), "--start", "--json")
            first = command(*plan_args)
            second = command(*plan_args)
            effective = {**settings, "configuration": {"greeting": SYNTHETIC_GREETING, "enabled": True}}
            assert first["settings"] == second["settings"] == effective
            assert first["settings_digest"] == second["settings_digest"]
            inspected = command("install", "inspect", "--plan", first["plan_id"], "--json")
            assert inspected["settings"] == effective and inspected["settings_digest"] == first["settings_digest"]
            stage = "grant_denial"
            grants = [command("authorize", "--read-only", "--json"), command("authorize", "--prepare", "--json"),
                      command("authorize", "--plan", first["plan_id"], "--json")]
            for grant in grants:
                deny_secret_access(grant, identity, secret_file)
            stage = "install_apply"
            operation = command("install", "apply", "--plan", first["plan_id"], "--json")
            stage = "duplicate_apply"
            duplicate = command("install", "apply", "--plan", first["plan_id"], "--json")
            assert operation["operation_id"] == duplicate["operation_id"]
            stage = "install_operation"
            operation = await wait(operation)
            slot_id = operation["slot_id"]
            assert len(command("slots", "list", "--json")["slots"]) == 1
            lifecycle = command("authorize", "--slot", slot_id, "--action", "start", "--action", "stop", "--json")
            deny_secret_access(lifecycle, identity, secret_file)
            stage = "http_sdk"
            await http_check(slot_id)
            stage = "stdio_sdk"
            parameters = StdioServerParameters(command=str(launcher),
                args=["--state-dir", str(state), "connect", "--slot", slot_id], env=environment)
            with open(os.devnull, "w") as errors:
                async with stdio_client(parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("check_settings", {})
                        assert not result.isError and result.structuredContent == EXPECTED_RESULT
            stage = "host_restart"
            await wait(command("slots", "stop", slot_id, "--json"))
            process.terminate()
            await asyncio.to_thread(process.wait, 30)
            process = await start()
            assert command("secrets", "inspect", identity, "--json")["status"] == "active"
            retained = command("install", "inspect", "--plan", first["plan_id"], "--json")
            assert retained["settings"] == effective and retained["settings_digest"] == first["settings_digest"]
            await wait(command("slots", "start", slot_id, "--json"))
            await http_check(slot_id)
            stage = "running_secret_retention"
            await http_check(slot_id, revoke_secret=identity)
            stage = "revoked_stop"
            await wait(command("slots", "stop", slot_id, "--json"))
            stage = "revoked_start"
            unavailable = command("slots", "start", slot_id, "--json")
            await wait(unavailable, failure="secret_reference_unavailable")
            assert command("secrets", "list", "--json")["count"] == 1
            return {"verified": True, "relocated_host": True, "private_secret_input": True,
                "metadata_only_secret_output": True, "all_grants_secret_denied": True,
                "configuration_required": True, "exact_settings_plan": True,
                "duplicate_apply_one_slot": True, "http_sdk": True, "stdio_sdk": True,
                "settings_restart": True, "secret_reference_restart": True,
                "revocation_blocks_new_delivery": True, "running_service_retains_delivered_secret": True}
        except VerificationError:
            raise
        except Exception:
            raise VerificationError("settings_" + stage + "_failed") from None
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
    parser.add_argument("--package", type=Path, required=True, help="Sealed synthetic JSON Utilities fixture for its MIT license.")
    arguments = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(asyncio.run(verify(arguments.host.resolve(), arguments.package.resolve()))))
    except VerificationError as error:
        raise SystemExit(error.code) from None
    except Exception:
        raise SystemExit("settings_verification_failed") from None
