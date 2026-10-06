from __future__ import annotations

import array
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shlex
import signal
import socket
import subprocess
import sys
import time

import pytest
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcpme.contracts import PackageError, validate_package
from mcpme.processes import OwnershipLost
from mcpme.supervisor import HostError, Supervisor, Worker
from mcpme.worker import RemoteLauncher
from mcpme import worker as worker_module
from test_mcpme_gateway import CHILD, TOOL
from test_mcpme_packages import make_package, relock


def reply(channel, instance_id):
    read_fd, write_fd = os.pipe()
    try:
        channel.sendmsg([json.dumps({"instance_id": instance_id}).encode()], [
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [write_fd, read_fd])),
        ])
    finally:
        os.close(read_fd)
        os.close(write_fd)


async def receive(channel):
    return json.loads(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(channel, 8192), 5))


async def test_canceled_launch_drains_response_reaps_and_preserves_order(monkeypatch):
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.setblocking(False)
    launcher = RemoteLauncher(client)
    observed = []
    original = launcher._exchange
    def record(message):
        result, descriptors = original(message)
        observed.append((message["action"], list(descriptors)))
        return result, descriptors
    monkeypatch.setattr(launcher, "_exchange", record)
    first = asyncio.create_task(launcher.launch("first"))
    second = None
    try:
        assert await receive(server) == {"action": "launch_session", "session_id": "first"}
        first.cancel()
        second = asyncio.create_task(launcher.launch("second"))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(server, 8192), 0.05)
        reply(server, "first-instance")
        assert await receive(server) == {"action": "close_session", "instance_id": "first-instance"}
        for fd in observed[0][1]:
            with pytest.raises(OSError):
                os.fstat(fd)
        # Repeated cancellation cannot release the lock while cleanup is pending.
        first.cancel()
        await asyncio.sleep(0)
        server.send(b'{"closed":true}')
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await receive(server) == {"action": "launch_session", "session_id": "second"}
        reply(server, "second-instance")
        pipes = await second
        assert pipes.instance_id == "second-instance"
        os.close(pipes.stdin_fd)
        os.close(pipes.stdout_fd)
    finally:
        with contextlib.suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        client.close()
        server.close()
        for task in (first, second):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


async def test_canceled_close_consumes_ack_before_next_launch():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.setblocking(False)
    launcher = RemoteLauncher(client)
    closing = asyncio.create_task(launcher.close("old-instance"))
    launch = None
    try:
        assert await receive(server) == {"action": "close_session", "instance_id": "old-instance"}
        closing.cancel()
        launch = asyncio.create_task(launcher.launch("new"))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.get_running_loop().sock_recv(server, 8192), 0.05)
        server.send(b'{"closed":true}')
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert await receive(server) == {"action": "launch_session", "session_id": "new"}
        reply(server, "new-instance")
        pipes = await launch
        assert pipes.instance_id == "new-instance"
        os.close(pipes.stdin_fd)
        os.close(pipes.stdout_fd)
    finally:
        with contextlib.suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        client.close()
        server.close()
        for task in (closing, launch):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (closing, launch) if task), return_exceptions=True)


async def test_silent_control_peer_has_bounded_sanitized_deadline():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.setblocking(False)
    launcher = RemoteLauncher(client, timeout=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="^supervisor_unavailable$"):
            await asyncio.wait_for(launcher.launch("silent"), 1)
        assert time.monotonic() - started < 1
        assert client.fileno() == -1
        with pytest.raises(RuntimeError, match="^supervisor_unavailable$"):
            await asyncio.wait_for(launcher.launch("retry"), 1)
    finally:
        server.close()
        client.close()


async def test_canceled_exchange_settlement_has_finite_deadline():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.setblocking(False)
    launcher = RemoteLauncher(client, timeout=0.05)
    launch = asyncio.create_task(launcher.launch("silent"))
    try:
        assert await receive(server) == {"action": "launch_session", "session_id": "silent"}
        launch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(launch, 1)
        assert client.fileno() == -1
        assert not launcher.lock.locked()
    finally:
        server.close()
        client.close()


async def test_worker_control_failure_closes_admission_and_exits_server(monkeypatch):
    server_channel, worker_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    server_activity, worker_activity = socket.socketpair()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    config_read, config_write = os.pipe()
    config = {"token": "test-invocation-token", "port": listener.getsockname()[1], "active": True,
              "max_sessions": 2, "idle_timeout": 5, "request_timeout": 1, "control_timeout": 0.05}
    os.write(config_write, json.dumps(config).encode())
    os.close(config_write)
    captured = {}
    loop = asyncio.get_running_loop()
    class FakeGateway:
        stopping = False
        async def shutdown(self):
            captured["shutdown"] = True
    gateway = FakeGateway()
    def fake_app(**kwargs):
        captured["launch"] = kwargs["launch_session"]
        return object(), gateway
    class FakeServer:
        should_exit = False
        def __init__(self, config):
            captured["server"] = self
        async def serve(self, **kwargs):
            with pytest.raises(RuntimeError, match="^supervisor_unavailable$"):
                await captured["launch"]("silent-peer")
            await asyncio.sleep(0)
            assert self.should_exit
            assert gateway.stopping
            # Further calls cannot send or spawn after failure.
            with pytest.raises(RuntimeError, match="^supervisor_unavailable$"):
                await captured["launch"]("retry")
    monkeypatch.setattr(worker_module, "make_app", fake_app)
    monkeypatch.setattr(worker_module, "WorkerServer", FakeServer)
    callback_threads = []
    original = loop.call_soon_threadsafe
    def thread_handoff(*args, **kwargs):
        import threading
        callback_threads.append(threading.current_thread())
        return original(*args, **kwargs)
    monkeypatch.setattr(loop, "call_soon_threadsafe", thread_handoff)
    try:
        await asyncio.wait_for(worker_module.run(config_read, listener.detach(), worker_channel.detach(), worker_activity.detach()), 1)
        assert captured["shutdown"]
        import threading
        assert any(thread is not threading.main_thread() for thread in callback_threads)
    finally:
        server_activity.close()
        worker_activity.close()
        server_channel.close()
        listener.close()
        worker_channel.close()


async def test_real_http_worker_exits_after_silent_control_peer():
    server_channel, worker_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    server_activity, worker_activity = socket.socketpair()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    config_read, config_write = os.pipe()
    config = {"token": "lifecycle-invocation-token", "port": port, "active": True,
              "max_sessions": 2, "idle_timeout": 5, "request_timeout": 1, "control_timeout": 0.05}
    os.write(config_write, json.dumps(config).encode())
    os.close(config_write)
    task = asyncio.create_task(worker_module.run(config_read, listener.detach(), worker_channel.detach(), worker_activity.detach()))
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            response = await client.post(f"http://127.0.0.1:{port}/mcp", headers={
                "Authorization": "Bearer lifecycle-invocation-token",
                "Accept": "application/json, text/event-stream",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "lifecycle-fixture", "version": "1"},
            }})
            assert response.status_code == 503
        await asyncio.wait_for(task, 3)
        assert task.done()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server_activity.close()
        worker_activity.close()
        server_channel.close()
        listener.close()
        worker_channel.close()


@pytest.fixture(scope="module")
def reaper(tmp_path_factory):
    executable = tmp_path_factory.mktemp("supervisor-reaper") / "mcpme-reap"
    source = Path(__file__).resolve().parents[1] / "scripts/mcpme-reap.c"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
                   capture_output=True, check=True)
    return executable


def add_slot(host, tmp_path, port):
    return host.registry.add_slot(package_digest="a" * 64, package_path=tmp_path,
                                  name="Lifecycle fixture", public_port=port)


@pytest.mark.parametrize("legacy_environment", [True, False])
async def test_relocated_locked_python_cache_survives_install_and_gateway_session(reaper, tmp_path,
                                                                                legacy_environment):
    source = make_package(tmp_path / "source")
    library = source / "lib"
    library.mkdir()
    module = library / "cache_input.py"
    module.write_text("MESSAGE = 'unchanged locked input'\n")
    cached = Path(py_compile.compile(str(module), doraise=True,
                                    invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))
    # A stale cache is still a legitimate locked artifact. Importing the source
    # after relocation must compile in memory, without replacing package bytes.
    os.utime(module, (1, 1))
    (library / "server.py").write_text("from cache_input import MESSAGE\nassert MESSAGE == 'unchanged locked input'\n" + CHILD)
    executable = source / "bin/service"
    executable.write_text("#!/bin/sh\nset -eu\nPACKAGE_DIR=$(CDPATH= cd -- \"$(dirname -- \"$0\")/..\" && pwd)\n"
                          "exec " + shlex.quote(sys.executable) + " \"$PACKAGE_DIR/lib/server.py\"\n")
    manifest_path = source / "mcpme.package.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tools"] = [{"name": TOOL["name"], "description": TOOL["description"],
                          "input_schema": TOOL["inputSchema"], "output_schema": TOOL["outputSchema"]}]
    manifest["validation"] = {"fixtures": [{"tool": "echo", "arguments": {"value": "candidate"},
                                             "expected": {"value": "candidate"}}]}
    manifest_path.write_text(json.dumps(manifest))
    relock(source)
    lock_path = source / "mcpme.lock.json"
    lock = json.loads(lock_path.read_text())
    for path in sorted(library.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            lock["artifacts"].append({"path": path.relative_to(source).as_posix(), "sha256": digest,
                                      "size": len(data), "executable": False,
                                      "source": "prepared:sha256:" + digest,
                                      "provenance": "Synthetic relocated Python fixture", "license": "LICENSE"})
    lock_path.write_text(json.dumps(lock))
    original = validate_package(source)
    cache_bytes = cached.read_bytes()
    cache_relative = cached.relative_to(source)
    host = Supervisor(tmp_path / "state", reaper)
    if legacy_environment:
        launch = host.launch_package
        async def without_guard(*args, **kwargs):
            kwargs["env"] = {key: value for key, value in kwargs["env"].items()
                             if key != "PYTHONDONTWRITEBYTECODE"}
            return await launch(*args, **kwargs)
        host.launch_package = without_guard
    try:
        plan = await host.plan_install_async({"package": str(source), "start": True})
        result = await host.apply_install(plan["plan_id"])
        async with asyncio.timeout(30):
            while True:
                operation = host.registry.get_operation(result["operation_id"])
                if operation.status not in {"pending", "running"}:
                    break
                await asyncio.sleep(.05)
        slot = host.registry.get_slot(plan["slot_id"])
        installed = Path(slot.package_path)
        assert installed != source and original.digest == plan["package_digest"]
        if legacy_environment:
            assert operation.status == "recovery_required" and operation.error_code == "installation_recovery_required"
            assert (installed / cache_relative).read_bytes() != cache_bytes
            with pytest.raises(PackageError, match="^artifact_(size|hash)_mismatch$"):
                validate_package(installed)
            return
        assert operation.status == "completed" and slot.observed_state == "ready"
        assert validate_package(installed).digest == original.digest
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]},
                                     trust_env=False, timeout=10) as client:
            async with streamable_http_client(connection["endpoint"], http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert [tool.name for tool in (await session.list_tools()).tools] == ["echo"]
                    output = await session.call_tool("echo", {"value": "gateway stdio session"})
                    assert not output.isError and output.structuredContent == {"value": "gateway stdio session"}
        assert (installed / cache_relative).read_bytes() == cache_bytes
        assert validate_package(installed).digest == original.digest
    finally:
        await host.close()
        assert not host.workers and all(not owner.instances for owner in host.owners.values())
        assert all(not ledger for ledger in host.ledgers.values())
    assert validate_package(source).digest == original.digest


async def test_ownership_loss_persists_blocks_restart_and_is_scoped(reaper, tmp_path):
    host = Supervisor(tmp_path / "state", reaper)
    slot = add_slot(host, tmp_path, 19386)
    independent = add_slot(host, tmp_path, 19387)
    process = await host.launch(slot.id, executable=Path(sys.executable),
                               args=["-c", "import time; time.sleep(100)"], cwd=tmp_path, env={})
    target_pid = process.pid
    assert target_pid is not None
    try:
        os.kill(process.reaper_pid, signal.SIGKILL)
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(process.wait(), 5)
        assert host.ledger(slot.id)[process.instance_id]["state"] == "ownership_lost"
        assert host.registry.get_slot(slot.id).observed_state == "ownership_lost"
        with pytest.raises(HostError, match="ownership_lost"):
            await host.control(slot.id, "start")
        other = await host.launch(independent.id, executable=Path(sys.executable),
                                  args=["-c", "pass"], cwd=tmp_path, env={})
        await host.reap(independent.id, other)
        assert not host.ledger(independent.id)
    finally:
        await host.close()
        with contextlib.suppress(ProcessLookupError):
            os.kill(target_pid, signal.SIGKILL)
    reopened = Supervisor(tmp_path / "state", reaper)
    try:
        # Restart rejection must occur before package validation or a new launch.
        with pytest.raises(HostError, match="ownership_lost"):
            await reopened.control(slot.id, "start")
        assert reopened.ledger(slot.id)[process.instance_id]["state"] == "ownership_lost"
        assert slot.id not in reopened.workers
    finally:
        await reopened.close()


async def test_spontaneous_gateway_exit_clears_confirmed_ownership(reaper, tmp_path):
    host = Supervisor(tmp_path / "state", reaper)
    slot = add_slot(host, tmp_path, 19386)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        process = await host.launch(slot.id, executable=Path(sys.executable),
                                   args=["-c", "pass"], cwd=tmp_path, env={})
        worker = Worker(process, parent)
        host.workers[slot.id] = worker
        await host.monitor_gateway(slot.id, worker)
        assert process.returncode == 0
        assert not host.ledger(slot.id)
        assert host.registry.get_slot(slot.id).error_code == "gateway_failed"
    finally:
        child.close()
        await host.close()


async def test_lost_instance_stops_other_owned_slot_children(reaper, tmp_path):
    host = Supervisor(tmp_path / "state", reaper)
    slot = add_slot(host, tmp_path, 19386)
    lost = await host.launch(slot.id, executable=Path(sys.executable),
                            args=["-c", "import time; time.sleep(100)"], cwd=tmp_path, env={})
    sibling = await host.launch(slot.id, executable=Path(sys.executable),
                               args=["-c", "import time; time.sleep(100)"], cwd=tmp_path, env={})
    assert lost.pid is not None
    try:
        os.kill(lost.reaper_pid, signal.SIGKILL)
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(lost.wait(), 5)
        assert await asyncio.wait_for(sibling.wait(), 5) == -signal.SIGTERM
        if host.tasks:
            await asyncio.wait_for(asyncio.gather(*list(host.tasks)), 5)
        assert set(host.ledger(slot.id)) == {lost.instance_id}
        assert host.ledger(slot.id)[lost.instance_id]["state"] == "ownership_lost"
    finally:
        await host.close()
        with contextlib.suppress(ProcessLookupError):
            os.kill(lost.pid, signal.SIGKILL)
