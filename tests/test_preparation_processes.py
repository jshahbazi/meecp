from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
import anyio

from meecp.preparation_contracts import PreparationError
from meecp.preparation_processes import PreparationExecutor
from meecp.processes import OwnershipLost
from meecp.processes import ProcessOwner
from meecp.processes import OwnedProcess


@pytest.fixture(scope="module")
def group_reaper(tmp_path_factory):
    output = tmp_path_factory.mktemp("preparation-reaper") / "meecp-reap"
    source = Path(__file__).resolve().parents[1] / "scripts/meecp-reap.c"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(output)],
                   check=True, capture_output=True)
    return output


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def gone(pid):
    async with asyncio.timeout(5):
        while alive(pid):
            await asyncio.sleep(0.02)


async def wait_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)
    return [int(value) for value in path.read_text().split()]


async def test_bounded_output_and_sanitized_failure(group_reaper, tmp_path, capfd):
    runner = PreparationExecutor(group_reaper)
    result = await runner.execute(Path(sys.executable), ["-c", "print('ready')"], tmp_path, {}, timeout_seconds=5)
    assert result.stdout == b"ready\n" and not runner.owner.instances
    for code, expected in [("print('x'*100)", "preparation_output_limit"),
                           ("import sys; print('private-out'); print('private-error',file=sys.stderr); sys.exit(4)",
                            "preparation_step_failed")]:
        with pytest.raises(PreparationError, match=expected):
            await runner.execute(Path(sys.executable), ["-c", code], tmp_path, {}, timeout_seconds=5, stdout_limit=32)
        assert not runner.owner.instances
    assert "private" not in "".join(capfd.readouterr())
    await runner.close()


@pytest.mark.parametrize("termination", ["exit", "timeout", "cancel", "repeat_cancel"])
async def test_settlement_includes_same_group_descendants(group_reaper, tmp_path, termination):
    pid_file = tmp_path / "children"
    code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "child=os.fork(); "
        "grandchild=os.fork() if child==0 else -1; "
        "Path('children').open('a').write(str(os.getpid())+' ') if (child==0 or grandchild==-1) else None; "
        + ("time.sleep(0.2) if child>0 else time.sleep(100)" if termination == "exit" else "time.sleep(100)")
    )
    runner = PreparationExecutor(group_reaper)
    task = asyncio.create_task(runner.execute(Path(sys.executable), ["-c", code], tmp_path, {},
                                             timeout_seconds=0.5 if termination == "timeout" else 10))
    await wait_file(pid_file)
    async with asyncio.timeout(5):
        while len(pid_file.read_text().split()) < 3:
            await asyncio.sleep(0.01)
    pids = await wait_file(pid_file)
    if "cancel" in termination:
        task.cancel()
        if termination == "repeat_cancel":
            await asyncio.sleep(0.02)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif termination == "timeout":
        with pytest.raises(PreparationError, match="preparation_step_timeout"):
            await task
    else:
        assert (await task).returncode == 0
    for pid in pids:
        await gone(pid)
    assert not runner.owner.instances
    await runner.close()


async def test_wrapper_loss_retains_reservation(group_reaper, tmp_path):
    runner = PreparationExecutor(group_reaper)
    task = asyncio.create_task(runner.execute(Path(sys.executable), ["-c", "import time; time.sleep(100)"],
                                             tmp_path, {}, timeout_seconds=10))
    async with asyncio.timeout(5):
        while not runner.owner.instances or next(iter(runner.owner.instances.values())).pid is None:
            await asyncio.sleep(0.01)
    process = next(iter(runner.owner.instances.values()))
    target = process.pid
    os.kill(process.reaper_pid, signal.SIGKILL)
    try:
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(task, 1)
        assert process.ownership_lost and runner.owner.instances
        with pytest.raises(OwnershipLost):
            await runner.execute(Path(sys.executable), ["-c", "pass"], tmp_path, {}, timeout_seconds=5)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(target, signal.SIGKILL)


async def test_invalid_step_never_launches(group_reaper, tmp_path):
    runner = PreparationExecutor(group_reaper)
    for limit, timeout in [(True, 1), (-1, 1), (1, True), (1, float("nan")), (1, 3601)]:
        with pytest.raises(PreparationError, match="invalid_preparation_step"):
            await runner.execute(Path(sys.executable), [], tmp_path, {}, timeout_seconds=timeout, stdout_limit=limit)
    assert not runner.owner.instances
    await runner.close()


async def test_group_leader_identity_is_held_until_cleanup(group_reaper, tmp_path):
    owner = ProcessOwner(group_reaper)
    process = await owner.launch(Path(sys.executable), ["-c", "print('exit',flush=True)"],
                                 tmp_path, {}, owned_group=True, stop_grace=1)
    assert await process.stdout.readline() == b"exit\n"
    await asyncio.sleep(0.15)
    # The wrapper deliberately retains the exited child's identity during its
    # group cleanup interval. It is not available for PID/group-ID reuse.
    # Darwin no longer exposes a zombie's group through getpgid(), but kill(0)
    # still confirms that its unreaped PID remains occupied.
    assert alive(process.pid)
    assert not process._done.done() and process.instance_id in owner.instances
    assert await process.wait() == 0
    await gone(process.pid)
    process.close_stdio()
    await owner.close()


@pytest.mark.parametrize("boundary", ["spawn", "streams"])
async def test_repeated_cancel_during_launch_settles_before_return(group_reaper, tmp_path, monkeypatch, boundary):
    reached, resume = asyncio.Event(), asyncio.Event()
    if boundary == "spawn":
        original = asyncio.create_subprocess_exec
        async def paused(*args, **kwargs):
            process = await original(*args, **kwargs)
            reached.set()
            await resume.wait()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", paused)
    else:
        original = OwnedProcess._streams
        async def paused(process):
            reached.set()
            await resume.wait()
            await original(process)
        monkeypatch.setattr(OwnedProcess, "_streams", paused)
    runner = PreparationExecutor(group_reaper)
    task = asyncio.create_task(runner.execute(Path(sys.executable), ["-c", "import time; time.sleep(100)"],
                                             tmp_path, {}, timeout_seconds=10))
    await reached.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner.owner.instances and not runner.owner._pending_ids and not runner.owner._launches
    await runner.close()


async def test_anyio_level_cancellation_during_stream_setup(group_reaper, tmp_path, monkeypatch):
    reached, resume, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = OwnedProcess._streams
    async def paused(process):
        reached.set()
        await resume.wait()
        await original(process)
    monkeypatch.setattr(OwnedProcess, "_streams", paused)
    runner = PreparationExecutor(group_reaper)
    async def execute():
        try:
            await runner.execute(Path(sys.executable), ["-c", "import time; time.sleep(100)"],
                                 tmp_path, {}, timeout_seconds=10)
        finally:
            settled.set()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(execute)
        await reached.wait()
        tasks.cancel_scope.cancel()
        with anyio.CancelScope(shield=True):
            resume.set()
            await settled.wait()
            assert not runner.owner.instances
    await runner.close()


def test_parent_sigkill_reaps_group_descendants(group_reaper, tmp_path):
    pid_file = tmp_path / "parent-death-children"
    helper = tmp_path / "owner.py"
    backend = Path(__file__).resolve().parents[1] / "backend"
    target = (
        "import os,time,signal; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); child=os.fork(); "
        "grandchild=os.fork() if child==0 else -1; "
        "Path('parent-death-children').open('a').write(str(os.getpid())+' '); time.sleep(100)"
    )
    helper.write_text("\n".join([
        "import asyncio,sys", "from pathlib import Path", "sys.path.insert(0," + repr(str(backend)) + ")",
        "from meecp.processes import ProcessOwner", "async def run():",
        "    owner=ProcessOwner(Path(" + repr(str(group_reaper)) + "))",
        "    process=await owner.launch(Path(sys.executable), ['-c'," + repr(target) + "],Path(" + repr(str(tmp_path)) + "),{},owned_group=True,stop_grace=0.05)",
        "    Path('wrapper').write_text(str(process.reaper_pid))", "    await asyncio.sleep(100)", "asyncio.run(run())",
    ]) + "\n")
    parent = subprocess.Popen([sys.executable, str(helper)], cwd=tmp_path,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    wrapper = None
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "wrapper").exists() or not pid_file.exists() or len(pid_file.read_text().split()) != 3:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pids = [int(value) for value in pid_file.read_text().split()]
        wrapper = int((tmp_path / "wrapper").read_text())
        parent.kill()
        parent.wait(timeout=5)
        while any(alive(pid) for pid in [*pids, wrapper]):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
