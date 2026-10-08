"""Exercise real packaged upgrades, synthetic persistent writes, and SIGKILL recovery.

The temporary subprocess harness wraps the actual gateway launch only to pause
after admission opens. It changes no bundled files and adds no production option
or environment bypass. All package processes, ownership receipts, transports,
configuration injection, CLI routes, and recovery use the bundled host.
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
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


class VerificationError(ValueError):
    pass


def require(condition: bool, code: str = "upgrade_contract_failed") -> None:
    if not condition:
        raise VerificationError(code)


SOURCE = '''"""Approved synthetic persistence fixture; no user inputs or diagnostics."""
import json, os
from pathlib import Path

configuration = 1
descriptor = os.environ.pop("MCPME_SETTINGS_FD", None)
if descriptor is not None:
    with os.fdopen(int(descriptor), "rb") as source:
        settings = json.loads(source.read(512 * 1024 + 1))
    configuration = settings["configuration"]["configuration"]

def persistent_counter(action, value=0):
    path = Path.home() / "counter.json"
    if action == "write":
        temporary = path.with_name("counter.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as target:
            json.dump({"value": value}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    current = json.loads(path.read_bytes())["value"] if path.exists() else 0
    return {"value": current, "configuration": configuration}
'''

TOOL = {"name": "persistent_counter", "description": "Read or write only the approved synthetic counter fixture.",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["read", "write"]}, "value": {"type": "integer"}},
            "required": ["action"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {
            "value": {"type": "integer"}, "configuration": {"type": "integer"}},
            "required": ["value", "configuration"], "additionalProperties": False}}

# A test-only in-memory override. The original method creates the real worker,
# authenticates its readiness, and starts its ordinary MCP admission first.
HARNESS = '''import asyncio,sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from meecp.private import write_private_json
from meecp.supervisor import Supervisor,HostError,serve
target, marker, release = sys.argv[5], Path(sys.argv[6]), Path(sys.argv[7])
original = Supervisor.start_gateway
async def gateway(self, slot, active, **kwargs):
    await original(self, slot, active, **kwargs)
    if active and slot.package_digest == target and not getattr(self,"verification_paused",False):
        self.verification_paused = True
        write_private_json(marker,{"admission_open":True})
        while not release.exists():
            await asyncio.sleep(0.02)
        raise HostError("synthetic_routing_failure")
Supervisor.start_gateway = gateway
asyncio.run(serve(Path(sys.argv[3]),Path(sys.argv[4])))
'''


def json_file(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")))
    path.chmod(0o600)


def package_revision(source: Path, destination: Path | None, version: str, *, configuration: int = 1, broken: bool = False) -> Path:
    package = source
    if destination is not None:
        shutil.copytree(source, destination, symlinks=True)
        package = destination
    metadata_pairs = (("meecp.package.json", "meecp.lock.json"),
                      ("mcpme.package.json", "mcpme.lock.json"))
    present = [pair for pair in metadata_pairs if any(os.path.lexists(package / name) for name in pair)]
    require(len(present) == 1 and all(os.path.lexists(package / name) for name in present[0]),
            "upgrade_package_metadata_invalid")
    manifest_name, lock_name = present[0]
    manifest_path, lock_path = package / manifest_name, package / lock_name
    manifest = json.loads(manifest_path.read_bytes())
    manifest["version"] = version
    manifest["configuration"] = {"schema_version": 1, "fields": {
        "configuration": {"type": "integer", "default": 1, "minimum": 1, "maximum": 4}}}
    manifest["access"] = {"filesystem": ["slot-owned synthetic HOME counter"], "writes": True}
    # Only the inactive candidate may run this approved write fixture. Once a
    # client has written a later value, startup/recovery must preserve it.
    manifest["validation"] = {"fixtures": [{"tool": "persistent_counter",
        "arguments": {"action": "write", "value": 0},
        "expected": {"value": 0, "configuration": configuration}}]}
    if broken:
        # The fixture writes an approved synthetic value to the inactive clone,
        # then fails the business smoke check with a schema-valid expectation.
        manifest["validation"] = {"fixtures": [{"tool": "persistent_counter",
            "arguments": {"action": "write", "value": 999},
            "expected": {"value": 998, "configuration": 1}}]}
    json_file(manifest_path, manifest)
    lock = json.loads(lock_path.read_bytes())
    lock["version"] = version
    lock["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    for artifact in lock["artifacts"]:
        path = package / artifact["path"]
        with path.open("rb") as stream:
            artifact["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        artifact["size"] = path.stat().st_size
    json_file(lock_path, lock)
    return package


async def verify(host_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="meecp upgrade acceptance ") as temporary:
        root = Path(temporary).resolve()
        host = root / "relocated host"
        shutil.copytree(host_path, host, symlinks=True)
        state, launcher, reaper = root / "private state", host / "bin/meecp", host / "bin/meecp-reap"
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "en_US.UTF-8"}
        process = None
        phase = "start"

        def command(*arguments):
            result = subprocess.run([str(launcher), "--state-dir", str(state), *arguments],
                cwd=root, env=environment, capture_output=True, text=True, timeout=300)
            try:
                value = json.loads(result.stdout)
            except (ValueError, UnicodeError):
                raise VerificationError("upgrade_cli_invalid_response") from None
            code = value.get("error", {}).get("code") if isinstance(value.get("error"), dict) else None
            safe_code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "unknown"
            require(result.returncode == 0 and "error" not in value, "upgrade_cli_failed_" + safe_code)
            return value

        async def start(*, paused_digest: str | None = None, marker: Path | None = None, release: Path | None = None):
            if paused_digest is None:
                arguments = [str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper)]
            else:
                arguments = [str(host / "python/bin/python3.12"), "-P", "-c", HARNESS,
                    str(host / "backend"), str(host / "python/site-packages"), str(state), str(reaper),
                    paused_digest, str(marker), str(release)]
            launched = subprocess.Popen(arguments, cwd=root, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with asyncio.timeout(60):
                    while True:
                        require(launched.poll() is None, "upgrade_host_failed")
                        if (state / "manager-connection.json").exists():
                            try:
                                command("capabilities", "--json")
                                return launched
                            except VerificationError:
                                pass
                        await asyncio.sleep(0.05)
            except BaseException:
                if launched.poll() is None:
                    launched.terminate()
                    await asyncio.to_thread(launched.wait, 30)
                raise

        async def completed(operation: dict, *, allowed: set[str] = {"completed"}):
            async with asyncio.timeout(90):
                while (operation["status"] in {"pending", "running"}
                       or operation["status"] == "recovery_required" and "recovery_required" not in allowed):
                    await asyncio.sleep(0.05)
                    operation = command("operations", "get", operation["operation_id"], "--json")
            require(operation["status"] in allowed, "upgrade_operation_failed")
            return operation

        def connection(slot_id):
            return json.loads((state / "slots" / slot_id / "connection.json").read_bytes())

        async def http_call(slot_id, action: str, value: int, expected: dict):
            selected = connection(slot_id)
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + selected["token"]}, trust_env=False) as http:
                async with streamable_http_client(selected["endpoint"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        require([tool.name for tool in (await session.list_tools()).tools] == ["persistent_counter"])
                        result = await session.call_tool("persistent_counter", {"action": action, "value": value})
                        require(not result.isError and result.structuredContent == expected, "upgrade_http_result_mismatch")

        async def stdio_call(slot_id, expected: dict):
            parameters = StdioServerParameters(command=str(launcher),
                args=["--state-dir", str(state), "connect", "--slot", slot_id], env=environment)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("persistent_counter", {"action": "read"})
                    require(not result.isError and result.structuredContent == expected, "upgrade_stdio_result_mismatch")

        def settings(value: int) -> Path:
            path = root / ("settings-" + str(value) + ".json")
            json_file(path, {"configuration": {"configuration": value}, "secret_references": {}})
            return path

        async def install(package: Path, name: str):
            plan = command("install", "plan", "--package", str(package), "--name", name,
                "--settings", str(settings(1)), "--start", "--start-policy", "host", "--json")
            return await completed(command("install", "apply", "--plan", plan["plan_id"], "--json"))

        async def stop_host():
            nonlocal process
            if process is not None and process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 30)
            process = None

        async def wait_marker(marker: Path):
            async with asyncio.timeout(90):
                while not marker.exists():
                    require(process is not None and process.poll() is None, "upgrade_harness_failed")
                    await asyncio.sleep(0.02)
            require(json.loads(marker.read_bytes()) == {"admission_open": True})

        async def wait_receipts(slot_ids: list[str]):
            # Observe only fixed identity metadata and reaper completion frames.
            # No PID is discovered, signaled, or adopted during reconciliation.
            observed = False
            async with asyncio.timeout(30):
                while True:
                    all_completed = True
                    for slot_id in slot_ids:
                        directory = state / "slots" / slot_id
                        ledger = json.loads((directory / "ownership.json").read_bytes())
                        for identity, entry in ledger.items():
                            observed = True
                            completion = entry.get("completion", {})
                            path = directory / "ownership-receipts" / (identity + ".receipt")
                            expected = b"MCPMEOR1" + identity.encode("ascii") + completion.get("nonce", "").encode("ascii") + b"C"
                            if not path.exists() or path.read_bytes() != expected:
                                all_completed = False
                    if all_completed:
                        require(observed, "upgrade_no_native_receipts")
                        return
                    await asyncio.sleep(0.05)

        try:
            process = await start()
            phase = "prepare"
            source = root / "raw Python"
            source.mkdir(mode=0o700)
            (source / "counter.py").write_text(SOURCE)
            (source / "LICENSE").write_text("Approved synthetic fixture dedicated to the public domain under CC0.\n")
            draft = command("imports", "create", "--source", str(source), "--name", "Synthetic persistence",
                "--intent", "Expose persistent_counter(action,value) using only a synthetic counter in slot-owned HOME.", "--json")
            exported = command("imports", "export", draft["draft_id"], "--revision", str(draft["revision"]), "--json")
            setup, specification = root / "setup.json", root / "tool.json"
            json_file(setup, exported)
            json_file(specification, TOOL)
            package1 = root / "package 1"
            built = subprocess.run([str(host / "python/bin/python3.12"), "-I", "-S", "-c",
                "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                str(host / "python/site-packages"), str(host / "templates/prepare.py"), "python-function",
                "--setup-export", str(setup), "--tool-spec", str(specification), "--module", "counter",
                "--function", "persistent_counter", "--source-license", "LICENSE", "--package-id", "local.upgrade-fixture",
                "--name", "Synthetic persistence", "--output", str(package1)],
                capture_output=True, text=True, timeout=300)
            require(built.returncode == 0, "upgrade_fixture_preparation_failed")
            package_revision(package1, None, "1.0.0")
            package2 = package_revision(package1, root / "package 2", "2.0.0", configuration=2)
            broken = package_revision(package1, root / "bad candidate", "9.0.0", broken=True)
            package3 = package_revision(package1, root / "package 3", "3.0.0", configuration=3)
            package4 = package_revision(package1, root / "package 4", "4.0.0", configuration=4)

            phase = "install"
            primary, control = await install(package1, "Persistent primary"), await install(package1, "Independent control")
            slot_id, control_id = primary["slot_id"], control["slot_id"]
            original_connection = connection(slot_id)
            original_data = state / "slots" / slot_id / "data"
            await http_call(slot_id, "write", 11, {"value": 11, "configuration": 1})
            await stdio_call(slot_id, {"value": 11, "configuration": 1})

            phase = "rollback"
            bad_plan = command("upgrade", "plan", "--slot", slot_id, "--package", str(broken), "--json")
            bad_operation = command("upgrade", "apply", "--plan", bad_plan["plan_id"], "--json")
            await http_call(control_id, "read", 0, {"value": 0, "configuration": 1})
            rejected = await completed(bad_operation, allowed={"failed"})
            require(not rejected["details"]["exposed"])
            require(rejected.get("error_code") == "smoke_probe_failed")
            require(json.loads((original_data / "counter.json").read_bytes()) == {"value": 11})
            record = json.loads((state / "upgrades/records" / (rejected["operation_id"] + ".json")).read_bytes())
            require(json.loads((Path(record["candidate"]["data_path"]) / "counter.json").read_bytes()) == {"value": 999})
            await http_call(slot_id, "read", 0, {"value": 11, "configuration": 1})
            await http_call(control_id, "read", 0, {"value": 0, "configuration": 1})

            phase = "configuration_upgrade"
            plan2 = command("upgrade", "plan", "--slot", slot_id, "--package", str(package2), "--settings", str(settings(2)), "--json")
            submitted2 = command("upgrade", "apply", "--plan", plan2["plan_id"], "--json")
            await http_call(control_id, "read", 0, {"value": 0, "configuration": 1})
            upgrade2 = await completed(submitted2)
            require(command("upgrade", "apply", "--plan", plan2["plan_id"], "--json")["operation_id"] == upgrade2["operation_id"])
            await http_call(slot_id, "write", 22, {"value": 22, "configuration": 2})
            await stdio_call(slot_id, {"value": 22, "configuration": 2})
            require(connection(slot_id) == original_connection)
            require(json.loads((original_data / "counter.json").read_bytes()) == {"value": 11})

            phase = "exposed_stop"
            # Obtain the exact sealed digest before pausing its actual gateway.
            plan3 = command("upgrade", "plan", "--slot", slot_id, "--package", str(package3), "--settings", str(settings(3)), "--json")
            phase = "exposed_host_restart"
            await stop_host()
            marker, release = root / "admission 3.json", root / "release 3.json"
            process = await start(paused_digest=plan3["package_digest"], marker=marker, release=release)
            phase = "exposed_admission"
            # Host start advances runtime generations, so plan again under the
            # current exact generation rather than applying a stale review.
            plan3 = command("upgrade", "plan", "--slot", slot_id, "--package", str(package3), "--settings", str(settings(3)), "--json")
            upgrade3 = command("upgrade", "apply", "--plan", plan3["plan_id"], "--json")
            await wait_marker(marker)
            await http_call(slot_id, "write", 33, {"value": 33, "configuration": 3})
            await http_call(control_id, "write", 55, {"value": 55, "configuration": 1})
            json_file(release, {"release": True})
            upgrade3 = await completed(upgrade3, allowed={"recovery_required"})
            phase = "exposed_stop_recovery"
            await completed(command("slots", "stop", slot_id, "--json"))
            stopped = command("slots", "inspect", slot_id, "--json")
            require(stopped["observed_state"] == "stopped" and stopped["desired_state"] == "stopped")
            phase = "exposed_recover"
            await completed(command("operations", "recover", upgrade3["operation_id"], "--json"))
            phase = "exposed_start"
            await completed(command("slots", "start", slot_id, "--json"))
            restarted = command("slots", "inspect", slot_id, "--json")
            require(restarted["observed_state"] == "ready" and restarted["desired_state"] == "running")
            phase = "exposed_verify"
            await stdio_call(slot_id, {"value": 33, "configuration": 3})
            require(connection(slot_id) == original_connection)

            phase = "sigkill_recovery"
            plan4 = command("upgrade", "plan", "--slot", slot_id, "--package", str(package4), "--settings", str(settings(4)), "--json")
            phase = "sigkill_host_restart"
            await stop_host()
            marker, release = root / "admission 4.json", root / "release 4.json"
            process = await start(paused_digest=plan4["package_digest"], marker=marker, release=release)
            phase = "sigkill_admission"
            plan4 = command("upgrade", "plan", "--slot", slot_id, "--package", str(package4), "--settings", str(settings(4)), "--json")
            upgrade4 = command("upgrade", "apply", "--plan", plan4["plan_id"], "--json")
            await wait_marker(marker)
            await http_call(slot_id, "write", 44, {"value": 44, "configuration": 4})
            await stdio_call(slot_id, {"value": 44, "configuration": 4})
            await http_call(control_id, "read", 0, {"value": 55, "configuration": 1})
            require(command("operations", "get", upgrade4["operation_id"], "--json")["status"] == "running")
            phase = "sigkill_receipts"
            process.kill()
            await asyncio.to_thread(process.wait, 10)
            process = None
            await wait_receipts([slot_id, control_id])
            phase = "sigkill_reconciliation"
            process = await start()
            recovered = command("operations", "get", upgrade4["operation_id"], "--json")
            require(recovered["status"] == "completed" and recovered["details"]["exposed"])
            require(connection(slot_id) == original_connection)
            phase = "sigkill_sdk_reads"
            await http_call(slot_id, "read", 0, {"value": 44, "configuration": 4})
            await stdio_call(slot_id, {"value": 44, "configuration": 4})
            await http_call(control_id, "read", 0, {"value": 55, "configuration": 1})
            require(json.loads((original_data / "counter.json").read_bytes()) == {"value": 11})
            return {"verified": True, "synthetic_only": True, "relocated_host": True,
                    "http_sdk": True, "stdio_sdk": True, "independent_slot": True,
                    "preexposure_data_rollback": True, "configuration_upgrade": True,
                    "stable_port_and_credential": True, "exposed_stop_and_recovery": True,
                    "accepted_write_sigkill_recovery": True, "native_completion_receipts": True}
        except VerificationError as error:
            # VerificationError carries only a fixed code supplied by this
            # harness. Never print the underlying runtime exception or values.
            raise VerificationError("upgrade_verification_" + phase + "_failed:" + str(error)) from None
        except Exception as error:
            kind = type(error).__name__
            safe_kind = kind if kind in {"TimeoutError", "KeyError", "ExceptionGroup", "OSError", "FileNotFoundError",
                "ConnectionError", "TypeError", "ValueError"} else "unexpected"
            raise VerificationError("upgrade_verification_" + phase + "_failed_" + safe_kind) from None
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
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(asyncio.run(verify(arguments.host.resolve()))))
    except Exception as error:
        raise SystemExit(str(error) if isinstance(error, VerificationError) else "upgrade_verification_failed") from None
