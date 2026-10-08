"""Pi tool boundaries, including actual macOS Seatbelt/process containment."""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import sys

import pytest

from meecp.downloads import DownloadError
from meecp.pi_tools import MAX_COMMAND_RESULT, MAX_FILE, MAX_OUTPUT, MAX_READ, PiToolBroker, PiToolError
from meecp.processes import OwnedProcess, OwnershipLost
from test_preparation_processes import group_reaper


MACOS = (platform.system() == "Darwin" and platform.machine() == "arm64" and
         platform.mac_ver()[0].startswith("26."))
native = pytest.mark.skipif(not MACOS, reason="exact macOS 26 arm64 confinement")


@pytest.fixture
async def broker(tmp_path):
    instance = PiToolBroker(tmp_path / "workspace", Path("/unused-reaper"))
    try:
        yield instance
    finally:
        await instance.close()


async def test_regular_files_are_bounded_private_and_utf8(broker):
    assert await broker.invoke("write_file", {"path": "folder/source.py", "content": "print('test')\n"}) == {
        "success": True, "output": "written"}
    assert await broker.invoke("read_file", {"path": "folder/source.py"}) == {
        "success": True, "output": "print('test')\n"}
    assert (broker.workspace / "folder").stat().st_mode & 0o777 == 0o700
    assert (broker.workspace / "folder/source.py").stat().st_mode & 0o777 == 0o600
    for value in ("x" * (MAX_FILE + 1), "é" * (MAX_FILE // 2 + 1)):
        assert (await broker.invoke("write_file", {"path": "large", "content": value}))["output"] == "file_size_limit"
    (broker.workspace / "binary").write_bytes(b"\xff")
    assert (await broker.invoke("read_file", {"path": "binary"}))["output"] == "file_not_utf8"


async def test_read_rejects_oversized_file_without_reading_it(broker, monkeypatch):
    with (broker.workspace / "large").open("wb") as stream:
        stream.truncate(MAX_FILE + 1)
    def fail_read(*args):
        raise AssertionError("oversized_file_was_read")
    monkeypatch.setattr(os, "read", fail_read)
    assert (await broker.invoke("read_file", {"path": "large"}))["output"] == "file_size_limit"


async def test_read_output_fits_worker_and_host_frame_budgets(broker):
    # ASCII control bytes have the maximum six-character JSON expansion.
    (broker.workspace / "worst-case").write_bytes(b"\x01" * MAX_READ)
    result = await broker.invoke("read_file", {"path": "worst-case"})
    assert result["success"] is True and len(result["output"]) <= 524288
    frame = {"type": "tool_result", "id": "x" * 128, **result}
    assert len(json.dumps(frame, ensure_ascii=True).encode() + b"\n") < 1024 * 1024
    (broker.workspace / "oversized").write_bytes(b"x" * (MAX_READ + 1))
    assert await broker.invoke("read_file", {"path": "oversized"}) == {"success": False, "output": "file_size_limit"}


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "file", "extra": "private"}),
    ("read_file", {}), ("read_file", None),
    ("write_file", {"path": "file", "content": "x", "extra": True}),
    ("run_command", {"command": ["/bin/true"]}),
    ("run_command", {"argv": ["/bin/true"], "environment": {"OPENAI_API_KEY": "private"}}),
    ("fetch_url", {"url": "https://example.com/file", "path": "file", "headers": {}}),
    ("install_dependencies", {"ecosystem": "python", "project_path": ".", "command": "x"}),
])
async def test_argument_contract_is_strict(broker, name, args):
    assert await broker.invoke(name, args) == {"success": False, "output": "invalid_arguments"}


@pytest.mark.parametrize("value", ["../private", "a/../../private", "x\x00", "x\n", "x" * 4097])
async def test_unsafe_path_is_rejected(broker, value):
    assert (await broker.invoke("read_file", {"path": value}))["output"] == "invalid_path"


async def test_symlinks_and_hardlinks_rejected_before_open(broker, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("private-state")
    (broker.workspace / "leaf").symlink_to(outside)
    (broker.workspace / "directory").symlink_to(tmp_path, target_is_directory=True)
    (broker.workspace / "inside").write_text("inside")
    (broker.workspace / "internal-link").symlink_to(broker.workspace / "inside")
    os.link(outside, broker.workspace / "hard")
    for path in ("leaf", "directory/outside", "internal-link", "hard"):
        result = await broker.invoke("read_file", {"path": path})
        assert result["success"] is False and "private-state" not in result["output"]
        result = await broker.invoke("write_file", {"path": path, "content": "replacement"})
        assert result["success"] is False
    assert outside.read_text() == "private-state"
    assert (broker.workspace / "inside").read_text() == "inside"


async def test_fifo_and_external_path_never_open_as_stream(broker, tmp_path):
    os.mkfifo(broker.workspace / "fifo")
    assert (await broker.invoke("read_file", {"path": "fifo"}))["output"] == "unsafe_file"
    assert (await broker.invoke("read_file", {"path": str(tmp_path / "private")}))["output"] == "path_outside_workspace"


async def test_root_replacement_invalidates_held_binding(broker):
    previous = broker.workspace.with_name("previous")
    broker.workspace.rename(previous)
    broker.workspace.mkdir(mode=0o700)
    assert (await broker.invoke("write_file", {"path": "x", "content": "x"}))["output"] == "root_changed"
    assert not (broker.workspace / "x").exists()
    assert not (previous / "x").exists()


async def test_private_root_mode_change_invalidates_binding(broker):
    broker.home.chmod(0o755)
    assert (await broker.invoke("read_file", {"path": "missing"}))["output"] == "root_changed"


@pytest.mark.parametrize("kind", ["symlink", "public", "writable-read-root"])
def test_root_admission_rejects_unsafe_paths(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    reads = ()
    if kind == "symlink":
        actual = workspace
        workspace = tmp_path / "link"
        workspace.symlink_to(actual, target_is_directory=True)
    elif kind == "public":
        workspace.chmod(0o755)
    else:
        read = tmp_path / "read"; read.mkdir(mode=0o777); read.chmod(0o777)
        reads = (read,)
    with pytest.raises(PiToolError, match="unsafe_root"):
        PiToolBroker(workspace, Path("/unused"), read_roots=reads)


def test_environment_cannot_import_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    with pytest.raises(PiToolError, match="invalid_environment"):
        PiToolBroker(tmp_path / "workspace", Path("/unused"), executable_env={"OPENAI_API_KEY": "private-key"})


@native
async def test_launch_spec_readonly_package_cwd_and_clean_environment(broker, tmp_path, monkeypatch):
    await broker.close()
    package = tmp_path / "package"; package.mkdir(mode=0o700)
    instance = PiToolBroker(broker.workspace, Path("/unused"), read_roots=(package,))
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    try:
        exe, args, cwd, env = instance.launch_spec(["/bin/true"], str(package))
        assert exe == Path("/usr/bin/sandbox-exec") and args[-1] == "/bin/true"
        assert cwd == package and "OPENAI_API_KEY" not in env
        assert env["HOME"].startswith(str(instance.workspace))
        assert '(deny default (with no-log))' in args[1]
        assert '(subpath "/System")' not in args[1]
        assert '(syscall-number 82 147)' in args[1]
        assert (await instance.invoke("write_file", {"path": str(package / "x"), "content": "x"}))["output"] == "path_outside_workspace"
    finally:
        await instance.close()


async def test_unavailable_dependency_host_returns_sanitized_code(broker):
    assert await broker.invoke("install_dependencies", {"ecosystem": "python", "project_path": "."}) == {
        "success": False, "output": "dependency_ecosystem_unavailable"}


class Network:
    def __init__(self, chunks=(), failure=None):
        self.chunks, self.failure, self.calls = chunks, failure, []

    @asynccontextmanager
    async def stream(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.failure:
            raise self.failure
        async def chunks():
            for chunk in self.chunks:
                yield chunk
        yield chunks()


async def test_fetch_streams_into_private_file_and_verifies_hash(broker):
    data = b"synthetic-package"
    broker.network = Network([data[:3], data[3:]])
    digest = hashlib.sha256(data).hexdigest()
    result = await broker.invoke("fetch_url", {"url": "https://files.pythonhosted.org/packages/test.whl",
        "path": "downloads/test.whl", "sha256": digest})
    assert result["success"] is True
    assert json.loads(result["output"]) == {"path": "downloads/test.whl", "size": len(data), "sha256": digest}
    assert (broker.workspace / "downloads/test.whl").read_bytes() == data
    assert broker.network.calls[0][1] == {"accept": "application/octet-stream"}
    assert not list((broker.workspace / "downloads").glob(".pi-tool-*"))


@pytest.mark.parametrize("failure", ["hash", "limit", "redirect", "exception"])
async def test_fetch_failure_does_not_publish_or_log_payload(broker, monkeypatch, capfd, failure):
    data = b"private-response"
    broker.network = Network([data])
    args = {"url": "https://example.com/file", "path": "download"}
    if failure == "hash": args["sha256"] = "0" * 64
    if failure == "limit": monkeypatch.setattr("meecp.pi_tools.MAX_FETCH", 4)
    if failure == "redirect": broker.network.failure = DownloadError("download_redirect_blocked")
    if failure == "exception": broker.network.failure = RuntimeError("private-response https://private-path")
    result = await broker.invoke("fetch_url", args)
    assert result["success"] is False and "private-response" not in result["output"]
    assert not (broker.workspace / "download").exists()
    assert not list(broker.workspace.glob(".pi-tool-*"))
    assert "private-response" not in "".join(capfd.readouterr())


@pytest.mark.parametrize("url", ["http://example.com/file", "https://127.0.0.1/file", "https://[::1]/file",
    "https://169.254.169.254/file", "https://user:secret@example.com/file", "https://example.com:444/file"])
async def test_fetch_rejects_unsafe_url_before_network(broker, url):
    broker.network = Network([b"data"])
    assert (await broker.invoke("fetch_url", {"url": url, "path": "download"}))["success"] is False
    assert broker.network.calls == []


async def test_real_fetch_resolution_blocks_private_address(broker, monkeypatch):
    import socket
    calls = []
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]
    async def connect(*args, **kwargs):
        calls.append(args)
        raise AssertionError("private_connection_attempted")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert await broker.invoke("fetch_url", {"url": "https://example.com/file", "path": "download"}) == {
        "success": False, "output": "download_network_blocked"}
    assert calls == []


@pytest.fixture
async def native_broker(tmp_path, group_reaper):
    if not MACOS:
        pytest.skip("exact macOS 26 arm64 confinement")
    python = Path(sys.executable).resolve()
    instance = PiToolBroker(tmp_path / "workspace", group_reaper, read_roots=(python.parents[1],))
    try:
        yield instance, python
    finally:
        await instance.close()


async def test_actual_shell_python_fork_exec_and_stdin_eof(native_broker):
    broker, python = native_broker
    result = await broker.invoke("run_command", {"argv": ["/bin/sh", "-c", "printf ready"], "timeout_seconds": 5})
    assert result["success"] is True, result
    assert json.loads(result["output"]) == {"returncode": 0, "stdout": "ready"}
    code = "import sys,subprocess; assert sys.stdin.read()==''; subprocess.run(['/bin/echo','child'],check=True); print('python')"
    result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code], "timeout_seconds": 5})
    assert result["success"] is True, result
    assert json.loads(result["output"])["stdout"] == "child\npython\n"
    assert not broker.owner.instances


async def test_actual_private_state_read_write_network_and_group_escape_denied(native_broker, tmp_path):
    broker, python = native_broker
    private = tmp_path / "private-state"; private.write_text("private-value"); private.chmod(0o600)
    inside = broker.workspace / "secret-link"; inside.symlink_to(private)
    listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    operations = [f"lambda:open({str(private)!r}).read()", f"lambda:open({str(inside)!r}).read()",
        f"lambda:open({str(tmp_path / 'outside')!r},'w').close()",
        f"lambda:socket.create_connection(('127.0.0.1',{port}),.1)", "os.setsid", "lambda:os.setpgid(0,0)",
        "lambda:os.kill(" + str(os.getpid()) + ",0)"]
    # The mounted Data alias must not broaden the system-library allowance.
    alias = Path("/System/Volumes/Data") / private.relative_to("/")
    if alias.exists():
        operations.append(f"lambda:open({str(alias)!r}).read()")
    code = "import os,socket,json; results=[]\nfor action in [" + ",".join(operations) + "]:\n try: action(); results.append(False)\n except OSError: results.append(True)\nprint(json.dumps(results))\n"
    try:
        result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code], "timeout_seconds": 5})
        assert result["success"] is True, result
        assert json.loads(json.loads(result["output"])["stdout"]) == [True] * len(operations)
        assert not (tmp_path / "outside").exists()
        assert private.read_text() == "private-value"
    finally:
        listener.close(); await listener.wait_closed()


async def test_actual_failed_command_returns_output_for_agent_repair(native_broker, capfd):
    broker, _ = native_broker
    result = await broker.invoke("run_command", {"argv": ["/bin/sh", "-c", "printf repairable; exit 4"], "timeout_seconds": 5})
    assert result["success"] is False
    assert json.loads(result["output"]) == {"returncode": 4, "stdout": "repairable"}
    assert "repairable" not in "".join(capfd.readouterr())
    assert not broker.owner.instances


async def test_actual_stderr_diagnostic_is_returned_for_repair_without_logging(native_broker, capfd):
    broker, python = native_broker
    code = "import sys; print('synthetic-build-error',file=sys.stderr); sys.exit(4)"
    result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code], "timeout_seconds": 5})
    assert result["success"] is False
    assert json.loads(result["output"]) == {"returncode": 4, "stdout": "synthetic-build-error\n"}
    assert "synthetic-build-error" not in "".join(capfd.readouterr())
    assert not broker.owner.instances


async def test_stderr_wrapper_forwards_metacharacters_as_literal_arguments(native_broker):
    broker, python = native_broker
    argument = 'literal; $(touch injected); `touch injected`; $HOME; "quotes"'
    code = "import sys; print(sys.argv[1])"
    result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code, argument], "timeout_seconds": 5})
    assert result["success"] is True
    assert json.loads(result["output"])["stdout"] == argument + "\n"
    assert not (broker.workspace / "injected").exists()


async def test_actual_output_limit_reaps_owner(native_broker):
    broker, python = native_broker
    result = await broker.invoke("run_command", {"argv": [str(python), "-c", f"print('x'*{MAX_OUTPUT + 1})"], "timeout_seconds": 5})
    assert result == {"success": False, "output": "output_limit"}
    assert not broker.owner.instances


@pytest.mark.parametrize("size,success", [(MAX_OUTPUT // 4, True), (MAX_OUTPUT // 2, False)])
async def test_command_json_escape_expansion_fits_shared_protocol_budget(native_broker, size, success):
    broker, python = native_broker
    code = f"import sys; sys.stdout.buffer.write(bytes([1])*{size})"
    result = await broker.invoke("run_command", {"argv": [str(python), "-I", "-S", "-B", "-c", code], "timeout_seconds": 5})
    assert result["success"] is success
    if success:
        assert json.loads(result["output"])["stdout"] == "\x01" * size
        assert len(result["output"].encode()) <= MAX_COMMAND_RESULT
        assert len(result["output"]) < 524288
        frame = {"type": "tool_result", "id": "x" * 128, **result}
        assert len(json.dumps(frame, ensure_ascii=True).encode() + b"\n") < 1024 * 1024
    else:
        # Raw stdout fits its byte bound, but nested JSON escaping does not.
        assert size < MAX_OUTPUT and result["output"] == "output_limit"
    assert not broker.owner.instances


@pytest.mark.parametrize("mode", ["timeout", "cancel", "repeated-cancel"])
async def test_actual_timeout_and_cancel_reap_descendants(native_broker, mode):
    broker, python = native_broker
    code = "import os,time; from pathlib import Path; child=os.fork(); Path('pid-'+str(os.getpid())).write_text(str(os.getpid())); time.sleep(100)"
    task = asyncio.create_task(broker.invoke("run_command", {"argv": [str(python), "-c", code],
        "timeout_seconds": 1 if mode == "timeout" else 10}))
    async with asyncio.timeout(5):
        while len(list(broker.workspace.glob("pid-*"))) < 2:
            await asyncio.sleep(.01)
    pids = [int(path.read_text()) for path in broker.workspace.glob("pid-*")]
    if mode == "timeout":
        assert await task == {"success": False, "output": "command_timeout"}
    else:
        task.cancel()
        if mode == "repeated-cancel":
            await asyncio.sleep(.02); task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with asyncio.timeout(5):
        for pid in pids:
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.02)
    assert not broker.owner.instances


async def test_actual_commands_serialize(native_broker):
    broker, python = native_broker
    first = asyncio.create_task(broker.invoke("run_command", {"argv": [str(python), "-c",
        "from pathlib import Path; import time; Path('started').touch(); time.sleep(.2); Path('ended').touch()"]}))
    async with asyncio.timeout(5):
        while not (broker.workspace / "started").exists():
            await asyncio.sleep(.01)
    second = asyncio.create_task(broker.invoke("run_command", {"argv": [str(python), "-c",
        "from pathlib import Path; assert Path('ended').exists(); print('serialized')"]}))
    assert (await first)["success"] is True
    assert (await second)["success"] is True


@pytest.mark.parametrize("boundary", ["spawn", "drain", "cleanup"])
async def test_command_deadline_covers_spawn_drain_and_cleanup(native_broker, monkeypatch, boundary):
    broker, python = native_broker
    if boundary == "spawn":
        original = broker.owner.launch
        async def slow_launch(*args, **kwargs):
            await asyncio.sleep(.7)
            return await original(*args, **kwargs)
        monkeypatch.setattr(broker.owner, "launch", slow_launch)
        code = "import time; time.sleep(.5)"
    elif boundary == "cleanup":
        original = OwnedProcess.stop
        async def slow_stop(self, *args, **kwargs):
            await asyncio.sleep(1)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(OwnedProcess, "stop", slow_stop)
        code = "print('done')"
    else:
        code = "import time; print('draining',flush=True); time.sleep(2)"
    result = await broker.invoke("run_command", {"argv": [str(python), "-c", code], "timeout_seconds": 1})
    assert result == {"success": False, "output": "command_timeout"}
    assert not broker.owner.instances and not broker.owner._launches


async def test_cancel_during_cleanup_settles_then_propagates(native_broker, monkeypatch):
    broker, _ = native_broker
    entered, release = asyncio.Event(), asyncio.Event()
    original = OwnedProcess.stop
    async def stopped(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(OwnedProcess, "stop", stopped)
    task = asyncio.create_task(broker.invoke("run_command", {"argv": ["/bin/true"]}))
    await entered.wait()
    task.cancel(); await asyncio.sleep(0); task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not broker.owner.instances


@native
async def test_actual_wrapper_loss_interrupts_drain_and_retains_unknown_owner(tmp_path, group_reaper):
    python = Path(sys.executable).resolve()
    broker = PiToolBroker(tmp_path / "workspace", group_reaper, read_roots=(python.parents[1],))
    task = asyncio.create_task(broker.invoke("run_command", {"argv": [str(python), "-c", "import time; time.sleep(100)"]}))
    async with asyncio.timeout(5):
        while not broker.owner.instances or next(iter(broker.owner.instances.values())).pid is None:
            await asyncio.sleep(.01)
    process = next(iter(broker.owner.instances.values()))
    target = process.pid
    os.kill(process.reaper_pid, signal.SIGKILL)
    try:
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(task, 1)
        assert process.ownership_lost and broker.owner.instances
        with pytest.raises(OwnershipLost):
            await broker.invoke("run_command", {"argv": ["/bin/true"]})
        with pytest.raises(OwnershipLost):
            await broker.close()
    finally:
        # The synthetic target belongs only to this test. Production does not
        # signal a discovered PID after losing its native ownership capability.
        with contextlib.suppress(ProcessLookupError):
            os.kill(target, signal.SIGKILL)


@pytest.mark.parametrize("timeout", [True, 1.5, 0, 1801, "60"])
async def test_timeout_contract_requires_bounded_integer(broker, timeout):
    assert (await broker.invoke("run_command", {"argv": ["/bin/true"], "timeout_seconds": timeout}))["output"] == "invalid_timeout"


@pytest.mark.parametrize("command", [[], "ls", [""], ["x\0"], ["x"] * 129, ["x" * 32769]])
async def test_command_contract_is_bounded(broker, command):
    assert (await broker.invoke("run_command", {"argv": command}))["success"] is False
