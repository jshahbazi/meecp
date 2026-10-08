from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

import pytest
import meecp.command_execution_v2 as command_module

from meecp.command_execution_v2 import CommandExecutorV2, CommandExecutionError, CommandSpecV2, SEED_SIZE

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or
                                platform.mac_ver()[0].split(".")[0] != "26", reason="macOS 26 arm64 native ownership")


def compile_c(source, target):
    subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(source), "-o", str(target)],
                   capture_output=True, check=True, timeout=30)
    return target


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    root = tmp_path_factory.mktemp("command-v2")
    helper = compile_c(ROOT / "scripts/meecp-command-reap.c", root / "reaper")
    package = root / "package"
    package.mkdir(mode=0o700)
    target = compile_c(ROOT / "tests/fixtures/command_v2_target.c", package / "target")
    return helper, package, target


def executor(binaries, state, **kwargs):
    helper, _, _ = binaries
    return CommandExecutorV2(helper, hashlib.sha256(helper.read_bytes()).hexdigest(), state,
                             admit=kwargs.get("admit", lambda spec: True))


def spec(binaries, mode="echo", **kwargs):
    _, package, target = binaries
    return CommandSpecV2(package, target, package, hashlib.sha256(target.read_bytes()).hexdigest(),
                         (mode,), **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("echo", b"synthetic"), ("fds", b"closed\n"),
    ("setsid", b"denied\n"), ("setpgid", b"denied\n"), ("fork-exec", b"denied\n"),
    ("spawn-group", b"denied\n"), ("spawn-session", b"denied\n"), ("tree", b"ready\n"), ("fork-race", b"ready\n"),
    ("effects", b"denied\n")])
async def test_real_native_confined_execution_and_settlement(binaries, tmp_path, mode, expected):
    runtime = executor(binaries, tmp_path / "state")
    try:
        result = await runtime.execute(spec(binaries, mode), b"synthetic")
        assert result.stdout == expected and result.returncode == 0
        assert runtime.reconcile() == {"pending": 0, "reserved_units": 0}
        assert b"synthetic" not in runtime.journal_path.read_bytes()
        assert b"ready" not in runtime.journal_path.read_bytes()
        frame = runtime._receipt_path(runtime.journal["entries"][0]).read_bytes()
        receipt_current = frame.startswith(b"MCPMECR3") and frame[SEED_SIZE:SEED_SIZE + 1] == b"C"
        assert receipt_current
        assert runtime._receipt_path(runtime.journal["entries"][0]).stat().st_mode & 0o777 == 0o400
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,limits,code", [("hang", {"deadline_ms": 60}, "command_timeout"),
    ("large", {"output_limit": 32}, "command_output_limit")])
async def test_timeout_and_output_limit_join_real_group(binaries, tmp_path, mode, limits, code):
    runtime = executor(binaries, tmp_path / "state")
    try:
        with pytest.raises(CommandExecutionError, match=code):
            await runtime.execute(spec(binaries, mode, **limits))
        assert runtime.reconcile()["pending"] == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_native_owner(binaries, tmp_path):
    runtime = executor(binaries, tmp_path / "state")
    task = asyncio.create_task(runtime.execute(spec(binaries, "hang", deadline_ms=3000)))
    while runtime._active is None:
        await asyncio.sleep(0.001)
    owned = runtime._active
    task.cancel()
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owned.poll() in (0, 64)  # Settled tree or proven pre-fork cancellation.
    assert runtime.reconcile()["pending"] == 0
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("iteration", range(5))
async def test_cancel_during_actual_fork_workload(binaries, tmp_path, monkeypatch, iteration):
    real_read = os.read
    started_forking = asyncio.Event()
    def read(fd, size):
        value = real_read(fd, size)
        if b"forking\n" in value:
            started_forking.set()
        return value
    monkeypatch.setattr(command_module.os, "read", read)
    runtime = executor(binaries, tmp_path / "state")
    task = asyncio.create_task(runtime.execute(spec(binaries, "fork-hang", deadline_ms=3000)))
    await asyncio.wait_for(started_forking.wait(), 1)
    owned = runtime._active
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owned.poll() == 0 and runtime.reconcile()["pending"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_native_owner_death_retains_reservation_and_restart_rejects_retry(binaries, tmp_path):
    state = tmp_path / "state"
    runtime = executor(binaries, state)
    task = asyncio.create_task(runtime.execute(spec(binaries, "hang", deadline_ms=100)))
    while runtime._active is None:
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.03)
    runtime._active.kill()  # Directly owned helper, test injection only.
    with pytest.raises(CommandExecutionError):
        await task
    assert runtime.reconcile() == {"pending": 1, "reserved_units": 1}
    await runtime.close()
    restarted = executor(binaries, state)
    try:
        with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
            await restarted.execute(spec(binaries))
    finally:
        await restarted.close()
    # Synthetic orphan has an independent 3-second alarm; no PID discovery.
    await asyncio.sleep(3)


@pytest.mark.asyncio
async def test_restart_rejects_legacy_and_foreign_receipts(binaries, tmp_path):
    state = tmp_path / "state"
    runtime = executor(binaries, state)
    await runtime.execute(spec(binaries))
    receipt = runtime._receipt_path(runtime.journal["entries"][0])
    original = receipt.read_bytes()
    await runtime.close()
    for bad in (b"MCPMEOR1" + b"0" * 96 + b"C", b"MCPMECR2" + original[8:],
                b"MCPMECR2" + original[8:SEED_SIZE] + b"N" + original[-4:], original[:8] + b"0" * 32 + original[40:],
                original[:168] + b"0" * 64 + original[232:], original + b"x"):
        receipt.chmod(0o600)
        receipt.write_bytes(bad)
        receipt.chmod(0o400)
        restarted = executor(binaries, state)
        assert restarted.reconcile()["pending"] == 1
        await restarted.close()
    receipt.chmod(0o600)
    receipt.write_bytes(original)
    receipt.chmod(0o400)
    restarted = executor(binaries, state)
    assert restarted.reconcile()["pending"] == 0
    await restarted.close()


@pytest.mark.asyncio
async def test_revocation_fences_output_after_settlement(binaries, tmp_path):
    calls = 0
    def admit(value):
        nonlocal calls
        calls += 1
        return calls == 1
    runtime = executor(binaries, tmp_path / "state", admit=admit)
    try:
        with pytest.raises(CommandExecutionError, match="command_authority_changed"):
            await runtime.execute(spec(binaries), b"private synthetic")
        assert runtime.reconcile()["pending"] == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_query_failures_and_deadline_keep_pending(binaries, tmp_path):
    source = (ROOT / "scripts/meecp-command-reap.c").read_text()
    for name, expression in [("error", "-1"), ("zero", "0"), ("full", "2"), ("foreign", "1")]:
        # Compile-time test substitution only; no production fault switches.
        altered = source.replace("proc_listpgrppids(child, members, sizeof(members))", expression)
        # Keep the parameter used under every substituted implementation.
        altered = altered.replace("pid_t members[2] = {0, 0};", "(void)child; pid_t members[2] = {0, 0};")
        fixture = tmp_path / (name + ".c")
        fixture.write_text(altered)
        helper = compile_c(fixture, tmp_path / name)
        runtime = executor((helper, *binaries[1:]), tmp_path / (name + "-state"))
        with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
            await runtime.execute(spec(binaries, "tree", settlement_ms=60))
        assert runtime.reconcile()["pending"] == 1
        await runtime.close()


@pytest.mark.parametrize("phase", ["pre-fork", "forking"])
def test_actual_host_sigkill_native_cleanup_then_receipt_only_restart(binaries, tmp_path, phase):
    helper, package, target = binaries
    state = tmp_path / "state"
    marker = tmp_path / "host-ready"
    program = "\n".join([
        "import asyncio, hashlib, os", "from pathlib import Path",
        "from meecp.command_execution_v2 import CommandExecutorV2, CommandSpecV2",
        "async def main():",
        f" r = CommandExecutorV2(Path({str(helper)!r}), {hashlib.sha256(helper.read_bytes()).hexdigest()!r}, Path({str(state)!r}), admit=lambda s: True)",
        f" s = CommandSpecV2(Path({str(package)!r}), Path({str(target)!r}), Path({str(package)!r}), {'a' * 64!r}, ('fork-hang',), deadline_ms=3000)",
        " seen = asyncio.Event()", " original_read = os.read", " def read(fd, size):",
        "  value = original_read(fd, size)", "  if b'forking\\n' in value: seen.set()", "  return value", " os.read = read",
        " t = asyncio.create_task(r.execute(s))", " while r._active is None: await asyncio.sleep(.001)",
        " await seen.wait()" if phase == "forking" else " await asyncio.sleep(0)",
        f" Path({str(marker)!r}).touch()", " await t", "asyncio.run(main())",
    ])
    host = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")})
    import time
    deadline = time.monotonic() + 3
    while not marker.exists() and host.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert marker.exists()
    host.send_signal(signal.SIGKILL)
    host.wait(timeout=3)
    async def recover():
        runtime = executor(binaries, state)
        try:
            for _ in range(400):
                if runtime.reconcile()["pending"] == 0:
                    break
                await asyncio.sleep(0.005)
            assert runtime.reconcile()["pending"] == 0
            assert (await runtime.execute(spec(binaries), b"after restart")).stdout == b"after restart"
        finally:
            await runtime.close()
    asyncio.run(recover())


def test_exact_helper_binding_required(binaries, tmp_path):
    with pytest.raises(CommandExecutionError, match="command_helper_mismatch"):
        CommandExecutorV2(binaries[0], "0" * 64, tmp_path / "state", admit=lambda s: True)


@pytest.mark.asyncio
async def test_native_receipt_write_failure_retains_pending(binaries, tmp_path):
    source = (ROOT / "scripts/meecp-command-reap.c").read_text().replace(
        "if (result != child || completed(status)) return 74;",
        "close(receipt_fd); if (result != child || completed(status)) return 74;")
    fixture = tmp_path / "receipt-failure.c"
    fixture.write_text(source)
    helper = compile_c(fixture, tmp_path / "receipt-failure")
    runtime = executor((helper, *binaries[1:]), tmp_path / "state")
    with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
        await runtime.execute(spec(binaries, "tree"))
    assert runtime.reconcile()["pending"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_native_launch_has_positive_nonlaunch_receipt(binaries, tmp_path, monkeypatch):
    runtime = executor(binaries, tmp_path / "state")
    def fail(*args, **kwargs):
        raise OSError("synthetic source must never appear in diagnostics")
    monkeypatch.setattr(command_module.subprocess, "Popen", fail)
    with pytest.raises(CommandExecutionError, match="command_io_failed") as error:
        await runtime.execute(spec(binaries))
    assert "synthetic source" not in str(error.value)
    assert runtime.reconcile()["pending"] == 0
    assert runtime._receipt_path(runtime.journal["entries"][0]).read_bytes()[SEED_SIZE:SEED_SIZE + 1] == b"N"
    await runtime.close()


@pytest.mark.asyncio
async def test_journal_failure_prevents_launch(binaries, tmp_path, monkeypatch):
    runtime = executor(binaries, tmp_path / "state")
    real_write = command_module.write_private_json
    calls = 0
    def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError()
        return real_write(*args)
    monkeypatch.setattr(command_module, "write_private_json", fail_once)
    with pytest.raises(CommandExecutionError, match="command_io_failed"):
        await runtime.execute(spec(binaries))
    assert runtime._active is None and runtime.reconcile()["pending"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_private_lock_prevents_concurrent_owner(binaries, tmp_path):
    state = tmp_path / "state"
    runtime = executor(binaries, state)
    try:
        with pytest.raises(BlockingIOError):
            executor(binaries, state)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_readiness_marker_arriving_between_initial_read_and_exit_proof(binaries, tmp_path):
    source = (ROOT / "scripts/meecp-command-reap.c").read_text()
    initial_read = "if (read(ready[0], &marker, 1) == 1 && marker == 'G') group_ready = 1;"
    assert source.count(initial_read) == 1
    # Force initial reads to miss G. The post-exit read must establish group
    # ownership before settlement even with no grace interval.
    source = source.replace(initial_read, "/* test: initial readiness observation missed */")
    fixture = tmp_path / "ready-race.c"
    fixture.write_text(source)
    helper = compile_c(fixture, tmp_path / "ready-race")
    runtime = executor((helper, *binaries[1:]), tmp_path / "state")
    result = await runtime.execute(spec(binaries, "tree", grace_ms=0))
    assert result.stdout == b"ready\n" and runtime.reconcile()["pending"] == 0
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sync", "publish"])
async def test_completion_sync_error_never_releases_reservation(binaries, tmp_path, failure):
    source = (ROOT / "scripts/meecp-command-reap.c").read_text()
    if failure == "publish":
        source = source.replace("return renameatx_np(directory_fd, staged_name, directory_fd, complete_name, RENAME_EXCL);", "return -1;")
    else:
        source = source.replace("if (fsync(receipt_fd)) return -1;", "if (1) return -1;")
    fixture = tmp_path / "sync-failure.c"
    fixture.write_text(source)
    helper = compile_c(fixture, tmp_path / "sync-failure")
    state = tmp_path / "state"
    runtime = executor((helper, *binaries[1:]), state)
    with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
        await runtime.execute(spec(binaries, "tree"))
    assert runtime.reconcile()["pending"] == 1
    assert not runtime._receipt_path(runtime.journal["entries"][0]).exists()
    assert runtime.journal["entries"][0]["receipt_failed"]
    await runtime.close()
    restarted = executor((helper, *binaries[1:]), state)
    assert restarted.reconcile()["pending"] == 1
    await restarted.close()


@pytest.mark.asyncio
async def test_live_reconcile_ignores_unpublished_payload_before_native_sync(binaries, tmp_path):
    source = (ROOT / "scripts/meecp-command-reap.c").read_text().replace(
        "if (fsync(receipt_fd)) return -1;", "usleep(200000); if (fsync(receipt_fd)) return -1;")
    fixture = tmp_path / "slow-sync.c"
    fixture.write_text(source)
    helper = compile_c(fixture, tmp_path / "slow-sync")
    runtime = executor((helper, *binaries[1:]), tmp_path / "state")
    task = asyncio.create_task(runtime.execute(spec(binaries, "tree", deadline_ms=3000)))
    try:
        for _ in range(2500):
            if runtime.journal["entries"]:
                item = runtime.journal["entries"][0]
                staged = runtime._staged_path(item)
                if staged.exists() and staged.read_bytes()[SEED_SIZE:SEED_SIZE + 1] == b"C":
                    break
            await asyncio.sleep(0.001)
        else:
            pytest.fail("synthetic completion was not observed")
        assert runtime._active.poll() is None
        assert not runtime._receipt_path(item).exists()
        assert runtime.reconcile()["pending"] == 1
        await task
        assert runtime.reconcile()["pending"] == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("sync_fails", [False, True])
def test_host_sigkill_during_prepublication_sync_never_releases_early(binaries, tmp_path, sync_fails):
    import time
    source = (ROOT / "scripts/meecp-command-reap.c").read_text()
    needle = "if (fsync(receipt_fd)) return -1;"
    assert source.count(needle) == 1
    replacement = "usleep(1200000); if (1) return -1;" if sync_fails else "usleep(1200000); " + needle
    fixture = tmp_path / "delayed-sync.c"
    fixture.write_text(source.replace(needle, replacement))
    delayed = compile_c(fixture, tmp_path / "delayed-sync")
    _, package, target = binaries
    state = tmp_path / "state"
    program = "\n".join([
        "import asyncio", "from pathlib import Path",
        "from meecp.command_execution_v2 import CommandExecutorV2, CommandSpecV2",
        "async def main():",
        f" r = CommandExecutorV2(Path({str(delayed)!r}), {hashlib.sha256(delayed.read_bytes()).hexdigest()!r}, Path({str(state)!r}), admit=lambda s: True)",
        f" s = CommandSpecV2(Path({str(package)!r}), Path({str(target)!r}), Path({str(package)!r}), {'a' * 64!r}, ('tree',), deadline_ms=5000)",
        " await r.execute(s)", " await r.close()", "asyncio.run(main())",
    ])
    host = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "backend")})
    try:
        staged = None
        deadline = time.monotonic() + 4
        while host.poll() is None and time.monotonic() < deadline:
            for candidate in state.glob("*.command-v3.pending"):
                if candidate.read_bytes()[SEED_SIZE:SEED_SIZE + 1] == b"C":
                    staged = candidate
                    break
            if staged is not None:
                break
            time.sleep(0.002)
        assert staged is not None, "synthetic prepublication sync window not reached"
        assert not list(state.glob("*.command-v3.complete"))
        host.kill()
        host.wait(timeout=3)
        async def recover():
            runtime = executor(binaries, state)
            try:
                assert runtime.reconcile() == {"pending": 1, "reserved_units": 1}
                with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
                    await runtime.execute(spec(binaries))
                await asyncio.sleep(1.4)
                expected = 1 if sync_fails else 0
                assert runtime.reconcile()["pending"] == expected
                if sync_fails:
                    assert not runtime._receipt_path(runtime.journal["entries"][0]).exists()
                    with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
                        await runtime.execute(spec(binaries))
                else:
                    assert (await runtime.execute(spec(binaries), b"after barrier")).stdout == b"after barrier"
            finally:
                await runtime.close()
        asyncio.run(recover())
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=3)


@pytest.mark.asyncio
async def test_publication_never_replaces_existing_completion(binaries, tmp_path):
    source = (ROOT / "scripts/meecp-command-reap.c").read_text().replace(
        "if (fsync(receipt_fd)) return -1;", "usleep(250000); if (fsync(receipt_fd)) return -1;")
    fixture = tmp_path / "exclusive-publication.c"
    fixture.write_text(source)
    helper = compile_c(fixture, tmp_path / "exclusive-publication")
    runtime = executor((helper, *binaries[1:]), tmp_path / "state")
    task = asyncio.create_task(runtime.execute(spec(binaries, "tree", deadline_ms=3000)))
    try:
        while not runtime.journal["entries"]:
            await asyncio.sleep(0.001)
        item = runtime.journal["entries"][0]
        final = runtime._receipt_path(item)
        final.write_bytes(b"existing synthetic entry")
        final.chmod(0o400)
        with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
            await task
        assert final.read_bytes() == b"existing synthetic entry"
        assert runtime.reconcile()["pending"] == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_missing_published_name_retains_reservation(binaries, tmp_path):
    state = tmp_path / "state"
    runtime = executor(binaries, state)
    await runtime.execute(spec(binaries))
    final = runtime._receipt_path(runtime.journal["entries"][0])
    await runtime.close()
    final.unlink()  # Model a lost directory entry, not successful durability.
    restarted = executor(binaries, state)
    assert restarted.reconcile() == {"pending": 1, "reserved_units": 1}
    await restarted.close()
