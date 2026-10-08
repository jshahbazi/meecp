from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meecp.processes import OwnershipLost, ProcessError, ProcessOwner


@pytest.fixture(scope="session")
def reaper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("reaper") / "meecp-reap"
    source = Path(__file__).resolve().parents[1] / "scripts/meecp-reap.c"
    subprocess.run(
        ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(target)],
        check=True, capture_output=True,
    )
    return target


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pid: int) -> None:
    async with asyncio.timeout(5):
        while alive(pid):
            await asyncio.sleep(0.02)


async def test_stdio_ack_and_clean_exit(reaper: Path, tmp_path: Path) -> None:
    owner = ProcessOwner(reaper)
    code = "import sys; print(sys.stdin.readline().strip(), flush=True)"
    process = await owner.launch(Path(sys.executable), ["-c", code], tmp_path, {})
    assert process.pid is not None and alive(process.pid)
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(b'{"id":7}\n')
    await process.stdin.drain()
    assert await process.stdout.readline() == b'{"id":7}\n'
    assert await asyncio.wait_for(process.wait(), 5) == 0
    assert not owner.instances
    process.close_stdio()
    await gone(process.pid)


async def test_stop_kills_uncooperative_owned_target(reaper: Path, tmp_path: Path) -> None:
    owner = ProcessOwner(reaper)
    code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(100)"
    process = await owner.launch(Path(sys.executable), ["-c", code], tmp_path, {}, stop_grace=0.05)
    assert await process.stdout.readline() == b"ready\n"
    assert await process.stop() == -signal.SIGKILL
    assert process.pid is not None
    await gone(process.pid)
    assert not owner.instances


async def test_environment_stderr_and_descriptor_policy(reaper: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    owner = ProcessOwner(reaper)
    retained, retained_other = socket.socketpair()
    unwanted, unwanted_other = socket.socketpair()
    code = (
        "import os,json,sys; "
        "fds=[fd for fd in range(3,256) if os.path.exists('/dev/fd/'+str(fd))]; "
        "print(json.dumps({'env':dict(os.environ),'fds':fds}),flush=True); "
        "print('private-stderr-canary',file=sys.stderr)"
    )
    try:
        process = await owner.launch(
            Path(sys.executable), ["-c", code], tmp_path, {"SELECTED": "yes", "LC_CTYPE": "UTF-8"},
            inherited_fds=(retained.fileno(),),
        )
        result = json.loads(await process.stdout.readline())
        assert result["env"]["SELECTED"] == "yes"
        assert result["env"]["LC_CTYPE"] == "UTF-8"
        # macOS CoreFoundation can add this nonsecret value inside CPython.
        assert set(result["env"]) <= {"SELECTED", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
        assert result["fds"] == [retained.fileno()]
        assert await process.wait() == 0
        process.close_stdio()
        assert "private-stderr-canary" not in "".join(capfd.readouterr())
    finally:
        retained.close(); retained_other.close(); unwanted.close(); unwanted_other.close()


async def test_raw_descriptors_can_be_transferred(reaper: Path, tmp_path: Path) -> None:
    owner = ProcessOwner(reaper)
    process = await owner.launch(
        Path(sys.executable), ["-c", "import sys; print(sys.stdin.readline().strip(),flush=True)"],
        tmp_path, {}, io_mode="fds",
    )
    assert process.stdin is None and process.stdout is None
    assert process.stdin_fd is not None and process.stdout_fd is not None
    sender, receiver = socket.socketpair()
    import array
    try:
        sender.sendmsg([b"stdio"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [process.stdin_fd, process.stdout_fd]))])
        _, ancillary, _, _ = receiver.recvmsg(32, socket.CMSG_SPACE(8))
        received = array.array("i")
        received.frombytes(ancillary[0][2])
        process.close_stdio()
        os.write(received[0], b"hello\n")
        os.close(received[0])
        data = await asyncio.to_thread(os.read, received[1], 100)
        os.close(received[1])
        assert data == b"hello\n"
        assert await process.wait() == 0
    finally:
        sender.close(); receiver.close()


async def test_reaper_loss_retains_reservation_and_blocks_launch(reaper: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    losses = []
    owner = ProcessOwner(reaper, on_ownership_lost=lambda process: losses.append(process.instance_id))
    process = await owner.launch(Path(sys.executable), ["-c", "import time; time.sleep(100)"], tmp_path, {})
    assert process.pid is not None
    # Fixture teardown alone knows the child identity from this live test.
    target = process.pid
    real_kill = os.kill
    real_kill(process.reaper_pid, signal.SIGKILL)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(process.wait(), 5)
        assert process.ownership_lost and process.instance_id in owner.instances
        assert losses == [process.instance_id]
        with pytest.raises(OwnershipLost):
            await owner.launch(Path(sys.executable), ["-c", "pass"], tmp_path, {})
        with pytest.raises(OwnershipLost):
            await process.stop()
        assert not signals
        real_kill(target, 0)
    finally:
        process.close_stdio()
        with contextlib.suppress(ProcessLookupError):
            real_kill(target, signal.SIGKILL)


async def test_launch_failure_does_not_lose_ownership(reaper: Path, tmp_path: Path) -> None:
    owner = ProcessOwner(reaper)
    with pytest.raises(ProcessError, match="launch_failed"):
        await owner.launch(tmp_path / "missing", [], tmp_path, {})
    assert not owner.instances
    with pytest.raises(ProcessError, match="invalid_launch"):
        await owner.launch(Path(sys.executable), [], tmp_path, {"MCPME_CONTROL_FD": "3"})


async def test_cancel_during_spawn_ack_reaps_target(reaper: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = ProcessOwner(reaper)
    original = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    resume = asyncio.Event()
    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.set()
        await resume.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(owner.launch(Path(sys.executable), ["-c", "import time; time.sleep(100)"], tmp_path, {}, stop_grace=0.05))
    await spawned.wait()
    task.cancel()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not owner.instances


async def test_reaper_loss_before_ack_retains_unknown_identity(reaper: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    losses = []
    async def persist_loss(process):
        await asyncio.sleep(0.01)
        losses.append((process.instance_id, process.pid))
    owner = ProcessOwner(reaper, on_ownership_lost=persist_loss)
    original = asyncio.create_subprocess_exec
    async def destroyed_wrapper(*args, **kwargs):
        process = await original(*args, **kwargs)
        os.kill(process.pid, signal.SIGKILL)
        await process.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", destroyed_wrapper)
    with pytest.raises(OwnershipLost):
        await owner.launch(Path(sys.executable), ["-c", "pass"], tmp_path, {}, instance_id="unacknowledged")
    assert losses == [("unacknowledged", None)]
    assert owner.instances["unacknowledged"].ownership_lost


async def test_close_during_pending_launch_waits_for_reaping(reaper: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = ProcessOwner(reaper)
    original = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    resume = asyncio.Event()
    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.set()
        await resume.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    launch = asyncio.create_task(owner.launch(Path(sys.executable), ["-c", "import time; time.sleep(100)"], tmp_path, {}, instance_id="pending"))
    await spawned.wait()
    with pytest.raises(ProcessError, match="duplicate_instance"):
        await owner.launch(Path(sys.executable), [], tmp_path, {}, instance_id="pending")
    shutdown = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    resume.set()
    with pytest.raises(ProcessError):
        await launch
    await shutdown
    assert not owner.instances


def test_parent_sigkill_cleanup_including_unacknowledged_launch(reaper: Path, tmp_path: Path) -> None:
    for delay in (False, True):
        pid_file = tmp_path / f"target-{delay}"
        parent_ready = tmp_path / f"ready-{delay}"
        # An actual OS parent owns the wrapper. Delay specification completion
        # for the pre-ack death boundary; the wrapper must abort without spawn.
        helper = (
            "import os,socket,struct,subprocess,sys,time; from pathlib import Path; "
            "spec,child=socket.socketpair(); ctrl,control=socket.socketpair(); "
            "inp=os.open('/dev/null',os.O_RDONLY); out=os.open('/dev/null',os.O_WRONLY); "
            "p=subprocess.Popen([sys.argv[1],str(child.fileno()),str(control.fileno()),str(os.getpid())], "
            "pass_fds=(child.fileno(),control.fileno(),inp,out),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "child.close(); control.close(); "
            "num=lambda n:struct.pack('!I',n); text=lambda s:num(len(s.encode()))+s.encode(); "
            "code='import os,time; from pathlib import Path; Path('+repr(sys.argv[2])+').write_text(str(os.getpid())); time.sleep(100)'; "
            "payload=b'MCPME001'+num(50)+num(inp)+num(out)+num(0)+text(sys.executable)+text(sys.argv[4])+num(2)+text('-c')+text(code)+num(0); "
            "spec.sendall(payload if sys.argv[5]=='False' else payload[:8]); "
            "Path(sys.argv[3]).write_text(str(p.pid)); time.sleep(100)"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", helper, str(reaper), str(pid_file), str(parent_ready), str(tmp_path), str(delay)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not parent_ready.exists() or (not delay and not pid_file.exists()):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            wrapper_pid = int(parent_ready.read_text())
            parent.kill(); parent.wait(timeout=5)
            while alive(wrapper_pid):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            if delay:
                assert not pid_file.exists()
            else:
                target_pid = int(pid_file.read_text())
                assert not alive(target_pid)
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait(timeout=5)
            if pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
