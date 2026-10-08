"""Real helper/host loss and versioned resource receipt publication barriers."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from meecp import command_resource_execution_v1 as module
from meecp.command_resource_execution_v1 import CommandExecutionError, SEED_SIZE, read_resource_completion
from test_command_resource_v1 import ROOT, binaries, digest, executor, spec, pytestmark


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "old-format", "hard-claim", "wrong-policy"])
async def test_reconstruction_requires_exact_resource_proof(binaries, tmp_path, monkeypatch, damage):
    state = tmp_path / "state"
    owner = executor(binaries, state)
    await owner.execute(spec(binaries, "echo"))
    await owner.close()
    item, = owner.journal["entries"]
    path = owner._receipt_path(item)
    frame = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "wrong-policy":
        monkeypatch.setattr(module, "_platform_policy", lambda: "f" * 64)
    else:
        data = bytearray(frame)
        if damage == "old-format":
            data[:8] = b"MCPMECR3"
        else:
            data[-1] = 1
        path.chmod(0o600)
        path.write_bytes(data)
        path.chmod(0o400)
    reconstructed = executor(binaries, state)
    try:
        assert reconstructed.reconcile() == {"pending": 1, "reserved_units": 1}
        with pytest.raises(CommandExecutionError, match="command_ownership_unsettled"):
            await reconstructed.execute(spec(binaries, "echo"))
    finally:
        await reconstructed.close()


@pytest.mark.asyncio
async def test_cancel_repeatedly_joins_actual_native_group(binaries, tmp_path, monkeypatch):
    owner = executor(binaries, tmp_path / "state")
    entered = asyncio.Event()
    original = os.read

    def observed(fd, size):
        data = original(fd, size)
        if data and any(p.stdout and not p.stdout.closed and p.stdout.fileno() == fd for p in owner._native.values()):
            entered.set()
        return data

    monkeypatch.setattr(os, "read", observed)
    work = asyncio.create_task(owner.execute(spec(binaries, "hang")))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        work.cancel()
        await asyncio.sleep(.01)
        work.cancel()
        result = (await asyncio.gather(work, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert owner.reconcile()["pending"] == 0
        valid, _, report = read_resource_completion(owner.state, owner.journal["entries"][0])
        assert valid and report.reason == "control_closed" and not report.result_admissible
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_helper_loss_retains_ownership_even_after_fixture_self_exit(binaries, tmp_path, monkeypatch):
    owner = executor(binaries, tmp_path / "state")
    entered = asyncio.Event()
    original = os.read

    def observed(fd, size):
        data = original(fd, size)
        if data and any(p.stdout and not p.stdout.closed and p.stdout.fileno() == fd for p in owner._native.values()):
            entered.set()
        return data

    monkeypatch.setattr(os, "read", observed)
    work = asyncio.create_task(owner.execute(spec(binaries, "hang")))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        native = owner._active
        native.kill()  # Exact held helper; no discovered target PID is signaled.
        await asyncio.to_thread(native.wait, 3)
        await asyncio.gather(work, return_exceptions=True)
        assert owner.reconcile() == {"pending": 1, "reserved_units": 1}
        # The compiled fixture's alarm bounds orphan lifetime independently;
        # elapsed time still cannot manufacture a settlement certificate.
        await asyncio.sleep(5.1)
        assert owner.reconcile() == {"pending": 1, "reserved_units": 1}
    finally:
        await owner.close()


CHILD = '''import asyncio,sys
from pathlib import Path
from meecp.command_resource_execution_v1 import CommandResourceExecutorV1,CommandResourceSpecV1
async def main():
 helper,target,state=map(Path,sys.argv[1:4])
 owner=CommandResourceExecutorV1(helper,sys.argv[4],state,expected_platform_policy=sys.argv[6],admit=lambda spec:True)
 spec=CommandResourceSpecV1(target.parent,target,target.parent,sys.argv[5],("echo",))
 await owner.execute(spec)
 await owner.close()
asyncio.run(main())
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_failure", [False, True])
async def test_host_sigkill_while_final_resource_payload_sync_has_no_visible_proof(binaries, tmp_path, sync_failure):
    source = (ROOT / "scripts/meecp-command-resource-reap.c").read_text()
    source = source.replace('if (fsync(receipt_fd)) return -1;',
        'usleep(600000); ' + ('return -1;' if sync_failure else 'if (fsync(receipt_fd)) return -1;'))
    variant = tmp_path / "variant.c"
    variant.write_text(source)
    helper = tmp_path / "helper"
    compiled = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", "-I", str(ROOT / "scripts"),
        str(variant), "-o", str(helper)], capture_output=True, timeout=30)
    assert compiled.returncode == 0
    pair = (helper, binaries[1])
    state = tmp_path / "state"
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "backend")}
    child = subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(helper), str(binaries[1]), str(state),
        digest(helper), digest(binaries[1]), module._platform_policy()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    recovered = None
    try:
        async with asyncio.timeout(4):
            while True:
                paths = list(state.glob("*.pending"))
                if paths and paths[0].read_bytes()[SEED_SIZE:SEED_SIZE + 1] == b"C":
                    break
                assert child.poll() is None
                await asyncio.sleep(.005)
        child.kill()
        await asyncio.to_thread(child.wait, 3)
        recovered = executor(pair, state)
        assert recovered.reconcile() == {"pending": 1, "reserved_units": 1}
        assert not list(state.glob("*.complete"))
        await asyncio.sleep(.8)
        assert recovered.reconcile()["pending"] == int(sync_failure)
        if not sync_failure:
            valid, _, report = read_resource_completion(state, recovered.journal["entries"][0])
            assert valid and report.samples > 0 and not report.result_admissible
    finally:
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.wait, 3)
        if recovered is not None:
            await recovered.close()
