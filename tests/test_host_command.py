"""Actual installed Supervisor/private handoff/native helper acceptance."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from meecp.command_execution_v2 import CommandExecutionError
from meecp.contracts import PackageError, validate_package
from meecp.host_command import HostCommandAdmission
from meecp.supervisor import Supervisor, HostError
from test_meecp_host import install, finish

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or
    platform.mac_ver()[0].split(".")[0] != "26", reason="macOS26 arm64 native integration")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    root = tmp_path_factory.mktemp("host-command-native")
    paths = []
    for source, name in [("scripts/meecp-reap.c", "reaper"), ("scripts/meecp-command-reap.c", "command-reaper"),
                         ("tests/fixtures/host_command_target.c", "target")]:
        path = root / name
        subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(ROOT / source), "-o", str(path)],
                       capture_output=True, check=True, timeout=30)
        paths.append(path)
    return tuple(paths)


def package(tmp_path, native, mode="echo", deadline=1000, smoke=True):
    root = tmp_path / "package"
    root.mkdir(mode=0o700)
    shutil.copyfile(native[2], root / "target")
    (root / "target").chmod(0o700)
    (root / "LICENSE").write_text("Synthetic test fixture, repository license.\n")
    input_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    output_schema = input_schema
    if mode == "identity":
        output_schema = {"type": "object", "properties": {key: {"type": "integer"} for key in ("pid", "parent", "group")},
                         "required": ["pid", "parent", "group"], "additionalProperties": False}
    manifest = {"schema_version": 2, "host_api_version": 1, "package_id": "test.host-command", "version": "1.0.0",
        "source_revision": "sha256:" + digest(ROOT / "tests/fixtures/host_command_target.c"), "name": "Host command",
        "profile": "host-command", "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
        "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
        "command": {"version": 1, "component": "macos26-arm64-command-v3", "helper_sha256": digest(native[1]),
                    "commands": {"probe": {"executable": "target", "args": [mode], "cwd": "."}},
                    "deadline_ms": deadline, "input_bytes": 512, "output_bytes": 512, "queue_limit": 2},
        "lifecycle": {"startup_timeout_seconds": 5, "operation_timeout_seconds": 5},
        "tools": [{"name": "probe", "description": "Synthetic bounded command.", "input_schema": input_schema,
                   "output_schema": output_schema}], "resources": {"memory_mb": 32, "cpu_only": True}, "licenses": ["LICENSE"]}
    if smoke:
        manifest["validation"] = {"fixtures": [{"tool": "probe", "arguments": {"ok": True}, "expected": {"ok": True}}]}
    (root / "meecp.package.json").write_text(json.dumps(manifest))
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
        "manifest_sha256": digest(root / "meecp.package.json"), "artifacts": [{"path": name, "sha256": digest(root / name),
          "size": (root / name).stat().st_size, "executable": name == "target", "source": manifest["source_revision"],
          "provenance": "Compiled synthetic fixture with local clang.", "license": "LICENSE"} for name in ("target", "LICENSE")],
        "build": {"recipe": "test-host-command-v1", "builder": "test", "toolchain": "clang"}}
    (root / "meecp.lock.json").write_text(json.dumps(lock))
    return root


def host_at(state, native, enabled=True):
    return Supervisor(state, native[0], command_admission=HostCommandAdmission(native[1], digest(native[1])) if enabled else None)


async def sdk_call(host, slot, expected=None):
    connection = host.connection(slot.id)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listed = await client.list_tools()
                assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools] == [
                    tool.mcp_schema() for tool in host.package(slot).manifest.tools]
                result = await client.call_tool("probe", {"ok": True})
                if expected is not None:
                    assert result.structuredContent == expected
                return result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["echo", "tree", "fds", "escape"])
async def test_installed_http_full_schema_call_and_descendant_settlement(tmp_path, native, mode):
    root = package(tmp_path, native, mode)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Command")
        shutil.rmtree(root)  # Installed sealed snapshot is the sole source closure.
        await sdk_call(host, slot, {"ok": True})
        assert not host.commands.pending(slot.id)
        assert host.commands.executors[slot.id].reconcile()["pending"] == 0
        assert len(host.owner(slot.id).instances) == 1  # Only the gateway is a legacy OwnedProcess.
        assert not host.commands.records[slot.id]
        assert b'"ok"' not in host.commands.path(slot.id).read_bytes()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_closed_admission_and_schema_one_unchanged(tmp_path, native):
    root = package(tmp_path, native)
    host = host_at(tmp_path / "state", native, enabled=False)
    try:
        with pytest.raises(HostError, match="host_command_not_admitted"):
            host.plan_install({"package": str(root)})
        assert not host.registry.list_slots()
    finally:
        await host.close()
    manifest_path = root / "meecp.package.json"
    value = json.loads(manifest_path.read_text())
    value["schema_version"] = 1
    manifest_path.write_text(json.dumps(value))
    with pytest.raises(PackageError):
        validate_package(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exit", "bad"])
async def test_installed_exit_and_invalid_output_sanitized(tmp_path, native, mode):
    root = package(tmp_path, native, mode, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Invalid")
        with pytest.raises(Exception):
            await sdk_call(host, slot)
        assert not host.commands.pending(slot.id)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_current_authority_cancel_and_queue_deadline(tmp_path, native):
    root = package(tmp_path, native, "hang", deadline=150, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Queued")
        first = await host.commands.session(slot)
        second = await host.commands.session(slot)
        tasks = [asyncio.create_task(host.commands.invoke(session, "probe", {"ok": True}, i))
                 for i, session in enumerate((first, second))]
        while host.commands.counts.get(slot.id, 0) < 2:
            await asyncio.sleep(0.001)
        with pytest.raises(CommandExecutionError, match="command_queue_limit"):
            await host.commands.invoke(second, "probe", {"ok": True}, 3)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(value, Exception) for value in results)
        assert not host.commands.pending(slot.id)
        assert len(host.commands.executors[slot.id].journal["entries"]) <= 1  # Queue wait consumes deadline.
        await first.close()
        await second.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_session_close_joins_native_helper_in_separate_session(tmp_path, native):
    root = package(tmp_path, native, "hang", deadline=3000, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Cancel")
        session = await host.commands.session(slot)
        # Use the protocol so the handle owns cancellation and join.
        writer = session.stdin_fd
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "probe", "arguments": {"ok": True}}}]
        for message in messages:
            os.write(writer, json.dumps(message).encode() + b"\n")
        executor = host.commands.executors[slot.id]
        async with asyncio.timeout(2):
            while executor._active is None:
                await asyncio.sleep(0.001)
        native_owner = executor._active
        assert os.getpgid(native_owner.pid) == native_owner.pid
        assert os.getpgid(native_owner.pid) != os.getpgrp()
        await session.close()
        assert native_owner.poll() in (0, 64)
        assert not host.commands.pending(slot.id)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_installed_stdio_bridge_preserves_schema_and_call(tmp_path, native):
    root = package(tmp_path, native)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Stdio")
        params = StdioServerParameters(command=str(ROOT / "scripts/meecp"), args=["--state-dir", str(host.root), "connect", "--slot", slot.id])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert tools.tools[0].inputSchema == host.package(slot).manifest.tools[0].input_schema
                result = await client.call_tool("probe", {"ok": True})
                assert result.structuredContent == {"ok": True}
        assert not host.commands.pending(slot.id)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_generation_change_fences_settled_output(tmp_path, native):
    root = package(tmp_path, native)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Generation")
        session = await host.commands.session(slot)
        executor = host.commands.executors[slot.id]
        admitted = executor.admit
        calls = 0
        def revoke(spec):
            nonlocal calls
            calls += 1
            if calls == 2:
                host.registry.update_slot(slot.id, generation=slot.generation + 1)
            return admitted(spec)
        executor.admit = revoke
        with pytest.raises(CommandExecutionError, match="command_authority_changed"):
            await host.commands.invoke(session, "probe", {"ok": True}, 1)
        assert not host.commands.pending(slot.id)
        await session.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_response_write_fences_generation_after_backpressure(tmp_path, native, monkeypatch):
    import meecp.host_command as module
    root = package(tmp_path, native)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Publication")
        session = await host.commands.session(slot)
        blocked = asyncio.Event()
        original = os.write
        accepted = []
        def write(fd, value):
            if fd == session.write_fd:
                if not blocked.is_set():
                    blocked.set()
                    raise BlockingIOError()
                accepted.append(len(value))
            return original(fd, value)
        monkeypatch.setattr(module.os, "write", write)
        send = asyncio.create_task(session.send({"jsonrpc": "2.0", "id": 1, "result": {}}))
        await blocked.wait()
        host.registry.update_slot(slot.id, generation=slot.generation + 1)
        with pytest.raises(CommandExecutionError, match="command_authority_changed"):
            await send
        assert not accepted
        await session.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_native_loss_retains_reservation_blocks_remove_and_restart(tmp_path, native):
    root = package(tmp_path, native, "hang", deadline=150, smoke=False)
    state = tmp_path / "state"
    host = host_at(state, native)
    slot = await install(host, root, "Retained")
    session = await host.commands.session(slot)
    task = asyncio.create_task(host.commands.invoke(session, "probe", {"ok": True}, 1))
    executor = host.commands.executors[slot.id]
    while executor._active is None:
        await asyncio.sleep(0.001)
    executor._active.kill()  # Exact Popen handle, never rediscovered identity.
    await asyncio.gather(task, return_exceptions=True)
    assert host.commands.pending(slot.id)
    retained = host.reservations[slot.id].copy()
    operation = await finish(host, host.begin_control(slot.id, "remove"))
    assert operation.status == "recovery_required"
    assert Path(slot.package_path).is_dir()
    assert host.reservations[slot.id] == retained
    await host.close()
    restarted = host_at(state, native)
    try:
        await restarted.initialize()
        assert restarted.commands.pending(slot.id)
        assert restarted.reservations[slot.id] == retained
        assert slot.id not in restarted.workers
        assert Path(slot.package_path).is_dir()
    finally:
        await restarted.close()
    await asyncio.sleep(4)  # Fixture's independent orphan lifetime.


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_actual_installed_backend_death_and_ancestor_group_kill(tmp_path, native, group):
    root = package(tmp_path, native, "hang", deadline=3000, smoke=False)
    state = tmp_path / "state"
    code = "\n".join([
        "import asyncio, os, sys",
        "from pathlib import Path",
        "from test_host_command import host_at, sdk_call",
        "from test_meecp_host import install",
        "async def main():",
        "    host = host_at(Path(sys.argv[1]), tuple(Path(x) for x in sys.argv[3:]))",
        "    slot = await install(host, Path(sys.argv[2]), 'Crash')",
        "    task = asyncio.create_task(sdk_call(host, slot))",
        "    executor = host.commands.executors[slot.id]",
        "    while executor._active is None: await asyncio.sleep(0.001)",
        "    native = executor._active",
        "    assert os.getpgid(native.pid) == native.pid and os.getpgid(native.pid) != os.getpgrp()",
        "    print(slot.id, flush=True)",
        "    await task",
        "asyncio.run(main())",
    ])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / name) for name in ("backend", "vendor", "tests"))}
    process = subprocess.Popen([sys.executable, "-c", code, str(state), str(root), *(str(path) for path in native)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)
        slot_id = line.decode().strip()
        assert len(slot_id) == 36
        if group:
            os.killpg(process.pid, signal.SIGKILL)  # Held, live direct-child group leader.
        else:
            process.kill()
        await asyncio.to_thread(process.wait, 5)
        restarted = host_at(state, native)
        try:
            async with asyncio.timeout(8):
                while restarted.commands.pending(slot_id):
                    await asyncio.sleep(0.02)
            executor = restarted.commands.executors[slot_id]
            assert executor.journal["entries"][-1]["state"] == "settled"
            assert not restarted.commands.records[slot_id]
            # SIGKILL of the whole ancestor group can lose legacy gateway proof;
            # no inferred legacy receipt or PID adoption is used to bypass it.
            if not group:
                await restarted.initialize()
                assert slot_id not in restarted.reservations
        finally:
            await restarted.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_fails", [False, True])
async def test_installed_restart_during_receipt_sync_retains_exact_reservation(tmp_path, native, sync_fails):
    from meecp.command_execution_v2 import SEED_SIZE
    source = (ROOT / "scripts/meecp-command-reap.c").read_text()
    needle = "if (fsync(receipt_fd)) return -1;"
    assert source.count(needle) == 1
    replacement = "usleep(1200000); if (1) return -1;" if sync_fails else "usleep(1200000); " + needle
    fixture = tmp_path / "slow.c"
    fixture.write_text(source.replace(needle, replacement))
    helper = tmp_path / "slow-reaper"
    subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(fixture), "-o", str(helper)],
                   capture_output=True, check=True, timeout=30)
    variant = (native[0], helper, native[2])
    root = package(tmp_path, variant, "echo", deadline=3000, smoke=False)
    state = tmp_path / "state"
    code = "\n".join([
        "import asyncio, sys",
        "from pathlib import Path",
        "from test_host_command import host_at, sdk_call",
        "from test_meecp_host import install",
        "async def main():",
        "    host = host_at(Path(sys.argv[1]), tuple(Path(x) for x in sys.argv[3:]))",
        "    slot = await install(host, Path(sys.argv[2]), 'Sync')",
        "    print(slot.id, flush=True)",
        "    await sdk_call(host, slot)",
        "asyncio.run(main())",
    ])
    process = subprocess.Popen([sys.executable, "-c", code, str(state), str(root), *(str(path) for path in variant)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / name) for name in ("backend", "vendor", "tests"))},
        start_new_session=True)
    try:
        slot_id = (await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)).decode().strip()
        assert len(slot_id) == 36
        directory = state / "slots" / slot_id / "command-v3"
        async with asyncio.timeout(5):
            while not any(path.read_bytes()[SEED_SIZE:SEED_SIZE + 1] == b"C" for path in directory.glob("*.command-v3.pending")):
                await asyncio.sleep(0.002)
        process.kill()
        await asyncio.to_thread(process.wait, 5)
        reopened = host_at(state, variant)
        try:
            slot = reopened.registry.get_slot(slot_id)
            reopened.commands.recover(slot)
            assert reopened.commands.pending(slot_id)
            retained = reopened.reservations[slot_id].copy()
            with pytest.raises(CommandExecutionError, match="host_command_not_admitted"):
                await reopened.commands.session(slot)
            await asyncio.sleep(1.4)
            assert reopened.commands.pending(slot_id) is sync_fails
            if sync_fails:
                assert reopened.reservations[slot_id] == retained
                assert not list(directory.glob("*.command-v3.complete"))
                await reopened.initialize()
                assert slot_id not in reopened.workers
            else:
                await reopened.initialize()
                assert slot_id not in reopened.reservations
        finally:
            await reopened.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.asyncio
async def test_installed_closure_tamper_never_launches(tmp_path, native):
    root = package(tmp_path, native)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Closure")
        session = await host.commands.session(slot)
        target = Path(slot.package_path) / "target"
        # Deliberate owner tampering can bypass the accidental-write guard.
        target.chmod(0o700)
        target.write_bytes(target.read_bytes() + b"tampered")
        with pytest.raises(CommandExecutionError, match="command_admission_failed"):
            await host.commands.invoke(session, "probe", {"ok": True}, 1)
        assert not host.commands.pending(slot.id)
        assert len(host.commands.executors[slot.id].journal["entries"]) == 1  # Only install smoke.
        await session.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_stop_revokes_all_sessions_before_first_join(tmp_path, native, monkeypatch):
    root = package(tmp_path, native)
    host = host_at(tmp_path / "state", native)
    release = asyncio.Event()
    stop = None
    try:
        slot = await install(host, root, "Stop fence")
        first = await host.commands.session(slot)
        second = await host.commands.session(slot)
        joining = asyncio.Event()
        cleanup = first.cleanup
        async def delayed_cleanup():
            joining.set()
            await release.wait()
            await cleanup()
        monkeypatch.setattr(first, "cleanup", delayed_cleanup)
        stop = asyncio.create_task(host.stop_gateway(slot.id))
        await joining.wait()
        executor = host.commands.executors[slot.id]
        count = len(executor.journal["entries"])
        with pytest.raises(CommandExecutionError):
            await host.commands.invoke(second, "probe", {"ok": True}, 9)
        assert len(executor.journal["entries"]) == count
    finally:
        release.set()
        if stop is not None:
            await stop
        await host.close()


@pytest.mark.asyncio
async def test_immediate_handle_close_before_reader_runs_releases_every_fd(tmp_path, native):
    root = package(tmp_path, native, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Immediate close")
        handle = await host.commands.session(slot)
        descriptors = (handle.read_fd, handle.write_fd, handle.stdin_fd, handle.stdout_fd)
        await handle.close()  # Deliberately no event-loop yield after creation.
        await asyncio.sleep(0)
        assert handle.instance_id not in host.commands.sessions
        assert handle.close_task.done()
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_fresh_http_session_can_initialize_while_healthy_command_active(tmp_path, native):
    root = package(tmp_path, native, "hang", deadline=3000, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Concurrent session")
        session = await host.commands.session(slot)
        task = asyncio.create_task(host.commands.invoke(session, "probe", {"ok": True}, 1))
        executor = host.commands.executors[slot.id]
        while executor._active is None:
            await asyncio.sleep(0.001)
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
            async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    assert initialized.protocolVersion == "2025-11-25"
                    assert (await client.list_tools()).tools[0].name == "probe"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.close()
        assert not host.commands.pending(slot.id)
    finally:
        await host.close()


def set_schema(root, schema):
    path = root / "meecp.package.json"
    manifest = json.loads(path.read_text())
    manifest["tools"][0]["input_schema"] = schema
    manifest["tools"][0]["output_schema"] = schema
    manifest.pop("validation", None)
    path.write_text(json.dumps(manifest))
    lock_path = root / "meecp.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["manifest_sha256"] = digest(path)
    lock_path.write_text(json.dumps(lock))


@pytest.mark.parametrize("field", [
    {"type": "string", "maxLength": 64, "pattern": "^(a+)+$"},
    {"$ref": "#"}, {"type": ["string", "null"]}, {"type": "string"},
    {"type": "array", "items": {"type": "boolean"}},
    {"type": "array", "maxItems": 65, "items": {"type": "boolean"}},
    {"type": "string", "maxLength": 513},
    {"type": "string", "maxLength": 12, "enum": ["x" * 513]},
    {"type": "integer", "enum": list(range(33))},
    {"type": "object", "properties": {}, "additionalProperties": True},
    {"type": "object", "properties": {}, "additionalProperties": False, "required": ["missing"]},
    {"type": "boolean", "anyOf": [{"type": "boolean"}]},
    {"type": "boolean", "default": True},
    {"type": "string", "maxLength": 10, "description": "\ud800"},
])
def test_untrusted_schema_work_rejected_statically_with_fixed_code(tmp_path, native, field):
    root = package(tmp_path, native, smoke=False)
    set_schema(root, {"type": "object", "properties": {"probe": field}, "additionalProperties": False})
    with pytest.raises(PackageError, match="^unsupported_host_command_schema$"):
        validate_package(root)


@pytest.mark.parametrize("shape", ["depth", "nodes", "properties", "required"])
def test_schema_complexity_bound(tmp_path, native, shape):
    root = package(tmp_path, native, smoke=False)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    if shape == "depth":
        field = {"type": "boolean"}
        for _ in range(9):
            field = {"type": "array", "maxItems": 1, "items": field}
        schema["properties"]["value"] = field
    elif shape == "nodes":
        schema["properties"] = {str(i): {"type": "object", "additionalProperties": False,
            "properties": {str(j): {"type": "boolean"} for j in range(32)}} for i in range(5)}
    else:
        schema["properties"] = {str(i): {"type": "boolean"} for i in range(33 if shape == "properties" else 1)}
        if shape == "required":
            schema["required"] = ["0"] * 33
    set_schema(root, schema)
    with pytest.raises(PackageError, match="^unsupported_host_command_schema$"):
        validate_package(root)


@pytest.mark.asyncio
async def test_nested_bounded_schema_is_preserved_exactly(tmp_path, native):
    root = package(tmp_path, native, smoke=False)
    schema = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
        "items": {"type": "array", "maxItems": 4, "items": {"type": "object", "additionalProperties": False,
          "properties": {"label": {"type": "string", "maxLength": 12, "enum": ["first", "second"]},
                         "value": {"type": "number", "minimum": -10, "maximum": 10}}, "required": ["label", "value"]}}}}
    set_schema(root, schema)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Nested")
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
            async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tool = (await client.list_tools()).tools[0]
                    assert tool.inputSchema == schema and tool.outputSchema == schema
                    value = {"items": [{"label": "first", "value": 1.5}]}
                    result = await client.call_tool("probe", value)
                    assert result.structuredContent == value
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_lifetime_duplicate_tracking_retains_only_fixed_size_digests(tmp_path, native):
    root = package(tmp_path, native, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Bounded IDs")
        handle = await host.commands.session(slot)
        reader = asyncio.StreamReader(limit=1048576)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(handle.stdout_fd, "rb", buffering=0))
        handle.stdout_fd = None
        try:
            os.set_blocking(handle.stdin_fd, False)
            for number in range(32):
                body = json.dumps({"jsonrpc": "2.0", "id": str(number) + "x" * 32768, "method": "ping"}).encode() + b"\n"
                view = memoryview(body)
                while view:
                    try:
                        view = view[os.write(handle.stdin_fd, view):]
                    except BlockingIOError:
                        await asyncio.sleep(0.001)
                assert len(await asyncio.wait_for(reader.readline(), 2)) > 32768
            assert len(handle.seen) == 32
            assert all(type(value) is bytes and len(value) == 32 for value in handle.seen)
            assert not handle.calls
        finally:
            transport.close()
            await handle.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_completed_removal_retires_executor_lock_and_slot_caches(tmp_path, native):
    root = package(tmp_path, native, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        for _ in range(3):
            slot = await install(host, root, "Retire")
            executor = host.commands.executors[slot.id]
            descriptor = executor.lock_fd
            result = await finish(host, host.begin_control(slot.id, "remove"))
            assert result.status == "completed"
            assert executor.lock_fd is None
            with pytest.raises(OSError):
                os.fstat(descriptor)
            assert not host.commands.executors and not host.commands.records and not host.commands.counts
            assert not host.registry.list_slots()
            assert Path(slot.package_path).is_dir()
            assert (host.slot_directory(slot.id) / "command-v3").is_dir()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_argument_validation_time_consumes_deadline_before_native_launch(tmp_path, native, monkeypatch):
    import time
    from jsonschema import Draft202012Validator
    root = package(tmp_path, native, deadline=10, smoke=False)
    host = host_at(tmp_path / "state", native)
    try:
        slot = await install(host, root, "Validation deadline")
        session = await host.commands.session(slot)
        original = Draft202012Validator.is_valid
        def delayed(self, value):
            time.sleep(0.03)  # Controlled latency; no selected regex is evaluated.
            return original(self, value)
        monkeypatch.setattr(Draft202012Validator, "is_valid", delayed)
        with pytest.raises((CommandExecutionError, TimeoutError)):
            await host.commands.invoke(session, "probe", {"ok": True}, 1)
        assert not host.commands.executors[slot.id].journal["entries"]
        assert not host.commands.pending(slot.id)
        await session.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_removal_completion_waits_for_exact_executor_retirement(tmp_path, native, monkeypatch):
    root = package(tmp_path, native, smoke=False)
    host = host_at(tmp_path / "state", native)
    release = asyncio.Event()
    try:
        slot = await install(host, root, "Removal publication")
        executor = host.commands.executors[slot.id]
        retiring = asyncio.Event()
        retire = host.commands.retire
        async def held_retirement(slot_id):
            retiring.set()
            await release.wait()
            await retire(slot_id)
        monkeypatch.setattr(host.commands, "retire", held_retirement)
        operation = host.begin_control(slot.id, "remove")
        await asyncio.wait_for(retiring.wait(), 5)
        assert host.registry.get_operation(operation["operation_id"]).status == "running"
        assert executor.lock_fd is not None
        release.set()
        assert (await finish(host, operation)).status == "completed"
        assert executor.lock_fd is None and slot.id not in host.commands.executors
    finally:
        release.set()
        await host.close()
