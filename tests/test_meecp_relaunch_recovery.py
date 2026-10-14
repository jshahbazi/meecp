"""Exact native identity recovery at relaunch and pre-acknowledgment boundaries."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import uuid
import zlib

import pytest

from meecp.boot_session import BootSession
from meecp.contracts import validate_package
from meecp.ownership_receipts import prepare_receipt, recover_receipt, verify_receipt
from meecp.private import read_private_json, write_private_json
from meecp.processes import OwnershipLost, ProcessOwner
from meecp.supervisor import HostError, Supervisor
from test_meecp_host import finish, prepared, tool_call
from test_meecp_receipt_recovery import CURRENT_BOOT, PRIOR_BOOT, ROOT, receipt_path


# Pause after the native wrapper exists, before ProcessOwner can send its launch
# specification or receive an acknowledgment. The durable launch record exists
# even though the Python owner has no OwnedProcess handle yet.
PREACK_HOST_CHILD = """
import asyncio, os, sys
from pathlib import Path
import meecp.supervisor as supervisor
from meecp.boot_session import BootSession
from meecp.private import write_private_json
from meecp.supervisor import Supervisor

async def main():
    state, reaper, package, ready, target, boot_id = sys.argv[1:]
    supervisor.current_boot_session = lambda: BootSession(boot_id)
    host = Supervisor(Path(state), Path(reaper))
    plan = host.plan_install({'package': package, 'start': False, 'start_policy': 'manual'})
    operation = await host.apply_install(plan['plan_id'])
    while host.registry.get_operation(operation['operation_id']).status in {'pending', 'running'}:
        await asyncio.sleep(0.01)
    assert host.registry.get_operation(operation['operation_id']).status == 'completed'
    slot = host.registry.get_slot(operation['slot_id'])
    await host.stop_gateway(slot.id)
    slot = host.registry.update_slot(slot.id, desired_state='running', wake_suppressed=True)
    (host.data_directory(slot) / 'accepted.bin').write_bytes(b'accepted before interrupted launch')
    write_private_json(host.slot_directory(slot.id) / 'reservation.json', {'memory_mb': 32, 'gpu': False})
    original = asyncio.create_subprocess_exec
    async def pause_native_creation(*args, **kwargs):
        wrapper = await original(*args, **kwargs)
        write_private_json(Path(ready), {'slot': slot.to_dict(), 'ledger': host.ledger(slot.id),
            'connection': host.connection(slot.id), 'reaper_pid': wrapper.pid})
        await asyncio.Event().wait()
    asyncio.create_subprocess_exec = pause_native_creation
    code = 'from pathlib import Path; import time; Path(' + repr(target) + ').write_text("executed"); time.sleep(100)'
    await host.launch(slot.id, executable=Path(sys.executable), args=['-c', code], cwd=host.root, env={})

asyncio.run(main())
"""


@pytest.mark.parametrize("offset", [105, 109, 149, 153, 193])
def test_completed_recoverable_receipt_requires_intact_identity_frame(tmp_path, offset):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex, recoverable=True)
    try:
        os.pwrite(descriptor, b"C", 104)
        os.fsync(descriptor)
        assert verify_receipt(tmp_path, metadata)
        frame = bytearray(os.pread(descriptor, 197, 0))
        frame[offset] ^= 1
        os.pwrite(descriptor, frame, 0)
        os.fsync(descriptor)
        assert not verify_receipt(tmp_path, metadata)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("boot,launch_boot_id", [
    (None, CURRENT_BOOT), (BootSession(CURRENT_BOOT), None),
    (BootSession(CURRENT_BOOT), "invalid-boot-id"),
    (BootSession(CURRENT_BOOT), PRIOR_BOOT), (BootSession("invalid-boot-id"), CURRENT_BOOT),
])
async def test_unconfirmed_same_boot_never_invokes_native_recovery(
        tmp_path, monkeypatch, boot, launch_boot_id):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex, recoverable=True)
    os.close(descriptor)
    path = receipt_path(tmp_path, metadata)
    original = path.read_bytes()
    invoked = []
    async def forbidden_helper(*args, **kwargs):
        invoked.append(True)
        raise AssertionError("native recovery ran without current-boot proof")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_helper)
    assert not await recover_receipt(tmp_path, metadata, tmp_path / "unused-reaper",
        boot=boot, launch_boot_id=launch_boot_id)
    assert not invoked and path.read_bytes() == original


async def test_sigkill_during_launch_before_ack_recovers_without_target_exec(
        tmp_path, prepared, monkeypatch):
    package, reaper = prepared
    state, ready, target = [tmp_path / name for name in ("state", "ready.json", "target-executed")]
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "backend"), PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.Popen([sys.executable, "-c", PREACK_HOST_CHILD, str(state), str(reaper),
        str(package), str(ready), str(target), CURRENT_BOOT], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    real_kill = os.kill
    wrapper_pid = None
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                assert child.poll() is None, "synthetic host exited before native creation"
                await asyncio.sleep(0.01)
        before = read_private_json(ready)
        wrapper_pid = before["reaper_pid"]
        assert len(before["ledger"]) == 1
        instance, record = next(iter(before["ledger"].items()))
        assert record["state"] == "launching" and record["completion"]["schema_version"] == 2
        assert record["boot_id"] == CURRENT_BOOT
        assert not target.exists()
        real_kill(wrapper_pid, signal.SIGKILL)
        child.kill()
        await asyncio.to_thread(child.wait, timeout=5)
        monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
        restarted = Supervisor(state, reaper)
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        try:
            await restarted.initialize()
            slot_id = before["slot"]["slot_id"]
            recovered = restarted.registry.get_slot(slot_id)
            assert recovered.observed_state == "stopped" and recovered.error_code is None
            for field in ("generation", "package_digest", "package_path", "data_path", "credential_ref",
                          "public_port", "start_policy", "wake_suppressed", "desired_state"):
                assert getattr(recovered, field) == before["slot"][field]
            connection_unchanged = restarted.connection(slot_id) == before["connection"]
            assert connection_unchanged
            assert restarted.boot_session.identity == CURRENT_BOOT
            assert restarted.data_directory(recovered).joinpath("accepted.bin").read_bytes() == b"accepted before interrupted launch"
            assert instance not in restarted.ledger(slot_id)
            assert not receipt_path(restarted.slot_directory(slot_id), record["completion"]).exists()
            assert not (restarted.slot_directory(slot_id) / "reservation.json").exists()
            assert slot_id not in restarted.reservations
            assert not target.exists()
            await restarted.control(slot_id, "start")
            assert restarted.registry.get_slot(slot_id).observed_state == "ready"
            assert await tool_call(restarted, slot_id) == {"valid": True, "kind": "object"}
            assert not signals
        finally:
            await restarted.close()
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)
        if wrapper_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                real_kill(wrapper_pid, signal.SIGKILL)


def recover_native(reaper: Path, path: Path) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        recovered = subprocess.run([str(reaper), "--recover-receipt", str(descriptor)],
            pass_fds=(descriptor,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=8, check=False)
        return recovered.returncode
    finally:
        os.close(descriptor)


async def orphaned_receipt(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "state", reaper)
    package = validate_package(package)
    # No listener is opened for this direct native launch.
    slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root,
        name="Exact native identity fixture", public_port=19385)
    process = await host.launch(slot.id, executable=Path(sys.executable),
        args=["-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(100)"],
        cwd=host.root, env={})
    completion = host.ledger(slot.id)[process.instance_id]["completion"]
    path = receipt_path(host.slot_directory(slot.id), completion)
    frame = path.read_bytes()
    assert frame[:8] == b"MCPMEOR2" and len(frame) == 197 and frame[104:105] == b"R"
    assert struct.unpack("!I", frame[105:109])[0] == process.pid
    assert struct.unpack("!I", frame[149:153])[0] == process.reaper_pid
    os.kill(process.reaper_pid, signal.SIGKILL)
    with pytest.raises(OwnershipLost):
        await asyncio.wait_for(process.wait(), 5)
    return host, process, completion, path, frame


@pytest.mark.parametrize("field", ["pid", "unique"])
async def test_changed_native_lifetime_never_signals_an_unrelated_live_child(
        tmp_path, prepared, field):
    host, process, completion, path, original = await orphaned_receipt(tmp_path, prepared)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(100)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    real_kill = os.kill
    try:
        changed = bytearray(original)
        if field == "pid":
            changed[105:109] = struct.pack("!I", unrelated.pid)
        else:
            unique = struct.unpack("!Q", original[109:117])[0]
            changed[109:117] = struct.pack("!Q", unique + 1)
        path.write_bytes(changed)
        assert await asyncio.to_thread(recover_native, prepared[1], path) != 0
        assert path.read_bytes() == changed
        assert unrelated.poll() is None
        real_kill(process.pid, 0)
        # Put back the authentic capability so the fixture's original orphan is
        # settled using its exact native identity after the adversarial check.
        path.write_bytes(original)
        assert await asyncio.to_thread(recover_native, prepared[1], path) == 0
        assert verify_receipt(path.parent.parent, completion)
        with pytest.raises(ProcessLookupError):
            real_kill(process.pid, 0)
        assert unrelated.poll() is None
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        await asyncio.to_thread(unrelated.wait, timeout=5)
        process.close_stdio()
        with contextlib.suppress(ProcessLookupError):
            real_kill(process.pid, signal.SIGKILL)
        await host.close()


@pytest.mark.parametrize("fault", ["magic", "length", "nonce", "state", "empty_target_identity"])
async def test_malformed_native_recovery_frame_fails_closed(tmp_path, prepared, fault):
    host, process, completion, path, original = await orphaned_receipt(tmp_path, prepared)
    real_kill = os.kill
    try:
        changed = bytearray(original)
        if fault == "magic":
            changed[0] = ord("X")
        elif fault == "length":
            changed.append(0)
        elif fault == "nonce":
            changed[40] = ord("G")
        elif fault == "state":
            changed[104] = ord("X")
        else:
            changed[105:149] = bytes(44)
        path.write_bytes(changed)
        assert await asyncio.to_thread(recover_native, prepared[1], path) != 0
        assert path.read_bytes() == changed
        assert not verify_receipt(path.parent.parent, completion)
        real_kill(process.pid, 0)
        path.write_bytes(original)
        assert await asyncio.to_thread(recover_native, prepared[1], path) == 0
        assert verify_receipt(path.parent.parent, completion)
        with pytest.raises(ProcessLookupError):
            real_kill(process.pid, 0)
    finally:
        process.close_stdio()
        with contextlib.suppress(ProcessLookupError):
            real_kill(process.pid, signal.SIGKILL)
        await host.close()


async def test_repeated_recovery_cancellation_joins_native_completion_before_closing_capability(
        tmp_path, prepared, monkeypatch):
    host, process, completion, path, _ = await orphaned_receipt(tmp_path, prepared)
    real_kill = os.kill
    created = asyncio.Event()
    release_wait = asyncio.Event()
    capability = []
    original_spawn = asyncio.create_subprocess_exec
    recovery = None
    async def barrier_spawn(*args, **kwargs):
        helper = await original_spawn(*args, **kwargs)
        capability.append(kwargs["pass_fds"][0])
        original_wait = helper.wait
        async def barrier_wait():
            created.set()
            await release_wait.wait()
            return await original_wait()
        helper.wait = barrier_wait
        return helper
    monkeypatch.setattr(asyncio, "create_subprocess_exec", barrier_spawn)
    try:
        recovery = asyncio.create_task(recover_receipt(path.parent.parent, completion, prepared[1],
            boot=host.boot_session, launch_boot_id=host.boot_session.identity))
        await asyncio.wait_for(created.wait(), 5)
        recovery.cancel()
        await asyncio.sleep(0.01)
        recovery.cancel()
        await asyncio.sleep(0.01)
        assert not recovery.done()
        assert path.exists()
        os.fstat(capability[0])
        release_wait.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(recovery, 8)
        assert verify_receipt(path.parent.parent, completion)
        await wait_for_test_pid_exit(process.pid, real_kill)
        assert path.exists()
        with pytest.raises(OSError):
            os.fstat(capability[0])
    finally:
        release_wait.set()
        if recovery is not None and not recovery.done():
            with contextlib.suppress(asyncio.CancelledError):
                await recovery
        process.close_stdio()
        with contextlib.suppress(ProcessLookupError):
            real_kill(process.pid, signal.SIGKILL)
        await host.close()


GROUP_CHILD = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pid = Path(sys.argv[1])
pid.write_text(str(os.getpid()))
pid.chmod(0o600)
time.sleep(100)
"""

GROUP_LEADER = """
import os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[2]],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pid = Path(sys.argv[1])
pid.write_text(str(os.getpid()))
pid.chmod(0o600)
while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
"""


async def wait_for_test_pid_exit(pid: int, real_kill):
    async with asyncio.timeout(5):
        while True:
            try:
                real_kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)


async def launch_group_fixture(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path / "state", reaper)
    process = None
    try:
        plan = host.plan_install({"package": str(package), "start": False, "start_policy": "manual"})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        await host.stop_gateway(slot.id)
        slot = host.registry.update_slot(slot.id, desired_state="running", wake_suppressed=True)
        data = host.data_directory(slot)
        accepted = data / "accepted.bin"
        accepted.write_bytes(b"accepted before grouped relaunch")
        accepted.chmod(0o600)
        leader_file, child_file, exit_request = [data / name for name in ("leader.pid", "child.pid", "leader-exit")]
        process = await host.launch(slot.id, executable=Path(sys.executable), args=["-c", GROUP_LEADER,
            str(leader_file), str(child_file), str(exit_request), GROUP_CHILD], cwd=data, env={},
            owned_group=True, stop_grace=2)
        completion = host.ledger(slot.id)[process.instance_id]["completion"]
        assert completion["schema_version"] == 3
        frame = receipt_path(host.slot_directory(slot.id), completion).read_bytes()
        assert frame[:8] == b"MCPMEOG2" and len(frame) == 197 and frame[104:105] == b"R"
        inner_pid = struct.unpack("!I", frame[149:153])[0]
        assert inner_pid > 1 and inner_pid != process.reaper_pid
        assert struct.unpack("!I", frame[105:109])[0] == process.pid
        async with asyncio.timeout(10):
            while not child_file.exists() or not leader_file.exists():
                await asyncio.sleep(0.01)
        assert int(leader_file.read_text()) == process.pid
        child_pid = int(child_file.read_text())
        assert child_file.stat().st_mode & 0o777 == 0o600
        reservation = host.slot_directory(slot.id) / "reservation.json"
        retained = {"memory_mb": 32, "gpu": False}
        write_private_json(reservation, retained)
        host.reservations[slot.id] = retained
        return host, slot, process, completion, child_pid, inner_pid, exit_request, host.connection(slot.id)
    except BaseException:
        await host.close()
        if process is not None:
            process.close_stdio()
        raise


GROUP_HOST_CHILD = """
import asyncio, sys
from pathlib import Path
import meecp.supervisor as supervisor
from meecp.boot_session import BootSession
from meecp.private import write_private_json
from test_meecp_relaunch_recovery import launch_group_fixture

async def main():
    directory, reaper, package, ready, boot_id = sys.argv[1:]
    supervisor.current_boot_session = lambda: BootSession(boot_id)
    host, slot, process, completion, child_pid, inner_pid, exit_request, connection = await launch_group_fixture(
        Path(directory), (Path(package), Path(reaper)))
    write_private_json(Path(ready), {'slot': slot.to_dict(), 'completion': completion,
        'instance_id': process.instance_id, 'outer_pid': process.reaper_pid, 'leader_pid': process.pid,
        'child_pid': child_pid, 'inner_pid': inner_pid, 'exit_request': str(exit_request), 'connection': connection})
    await asyncio.Event().wait()

asyncio.run(main())
"""


@pytest.mark.parametrize("leader_exited,stop_inner", [(False, False), (False, True), (True, True)])
async def test_grouped_outer_owner_loss_recovers_descendants_on_same_boot(
        tmp_path, prepared, monkeypatch, leader_exited, stop_inner):
    package, reaper = prepared
    ready = tmp_path / "group-ready.json"
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "backend"), str(ROOT / "tests")]),
        PYTHONDONTWRITEBYTECODE="1")
    child_host = subprocess.Popen([sys.executable, "-c", GROUP_HOST_CHILD, str(tmp_path), str(reaper),
        str(package), str(ready), CURRENT_BOOT], env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    real_kill = os.kill
    restarted = None
    before = None
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                assert child_host.poll() is None, "synthetic grouped host exited before readiness"
                await asyncio.sleep(0.01)
        before = read_private_json(ready)
        if leader_exited:
            Path(before["exit_request"]).touch(mode=0o600)
            async with asyncio.timeout(5):
                while True:
                    status = await asyncio.to_thread(subprocess.run,
                        ["/bin/ps", "-p", str(before["leader_pid"]), "-o", "stat="], check=False,
                        capture_output=True, text=True)
                    if status.stdout.strip().startswith("Z"):
                        break
                    await asyncio.sleep(0.01)
        if stop_inner:
            real_kill(before["inner_pid"], signal.SIGSTOP)
        real_kill(before["child_pid"], 0)
        real_kill(before["outer_pid"], signal.SIGKILL)
        child_host.kill()
        await asyncio.to_thread(child_host.wait, timeout=5)
        slot_id = before["slot"]["slot_id"]
        directory = tmp_path / "state" / "slots" / slot_id
        if stop_inner:
            assert receipt_path(directory, before["completion"]).read_bytes()[104:105] == b"R"
            real_kill(before["child_pid"], 0)
        monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
        restarted = Supervisor(tmp_path / "state", reaper)
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        await restarted.initialize()
        recovered = restarted.registry.get_slot(slot_id)
        assert recovered.observed_state == "stopped" and recovered.error_code is None
        for field in ("generation", "package_digest", "package_path", "data_path", "credential_ref",
                      "public_port", "start_policy", "wake_suppressed", "desired_state"):
            assert getattr(recovered, field) == before["slot"][field]
        connection_unchanged = restarted.connection(slot_id) == before["connection"]
        assert connection_unchanged
        assert restarted.boot_session.identity == CURRENT_BOOT
        assert restarted.data_directory(recovered).joinpath("accepted.bin").read_bytes() == b"accepted before grouped relaunch"
        assert before["instance_id"] not in restarted.ledger(slot_id)
        assert not receipt_path(restarted.slot_directory(slot_id), before["completion"]).exists()
        reservation = restarted.slot_directory(slot_id) / "reservation.json"
        assert slot_id not in restarted.reservations and not reservation.exists()
        for pid in (before["leader_pid"], before["child_pid"], before["inner_pid"]):
            await wait_for_test_pid_exit(pid, real_kill)
        await restarted.control(slot_id, "start")
        started = restarted.registry.get_slot(slot_id)
        assert started.observed_state == "ready" and started.generation == before["slot"]["generation"] + 1
        assert not started.wake_suppressed
        assert await tool_call(restarted, slot_id) == {"valid": True, "kind": "object"}
        assert not signals
    finally:
        if restarted is not None:
            await restarted.close()
        if child_host.poll() is None:
            child_host.kill()
            await asyncio.to_thread(child_host.wait, timeout=5)
        if before is not None:
            for pid, signum in ((before["inner_pid"], signal.SIGCONT), (before["inner_pid"], signal.SIGTERM),
                                (before["leader_pid"], signal.SIGKILL), (before["child_pid"], signal.SIGKILL)):
                with contextlib.suppress(ProcessLookupError):
                    real_kill(pid, signum)


async def test_grouped_both_owners_lost_stays_blocked_and_never_signals_reused_group(
        tmp_path, prepared, monkeypatch):
    monkeypatch.setattr("meecp.supervisor.current_boot_session", lambda: BootSession(CURRENT_BOOT))
    host, slot, process, completion, child_pid, inner_pid, _, _ = await launch_group_fixture(tmp_path, prepared)
    real_kill = os.kill
    restarted = None
    host_closed = False
    unrelated_owner = ProcessOwner(prepared[1])
    unrelated = None
    descriptor = None
    try:
        unrelated_directory = tmp_path / "unrelated-group"
        descriptor, unrelated_completion = prepare_receipt(unrelated_directory, uuid.uuid4().hex,
            recoverable=True, owned_group=True)
        unrelated = await unrelated_owner.launch(Path(sys.executable), ["-c", "import time; time.sleep(100)"],
            tmp_path, {}, instance_id=unrelated_completion["instance_id"], completion_fd=descriptor,
            completion_nonce=unrelated_completion["nonce"], owned_group=True)
        os.close(descriptor)
        descriptor = None
        unrelated_frame = receipt_path(unrelated_directory, unrelated_completion).read_bytes()
        real_kill(process.reaper_pid, signal.SIGSTOP)
        real_kill(inner_pid, signal.SIGKILL)
        real_kill(process.reaper_pid, signal.SIGKILL)
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(process.wait(), 5)
        path = receipt_path(host.slot_directory(slot.id), completion)
        reused_group = bytearray(path.read_bytes())
        # A live unrelated group now has the recorded leader's PGID/lifetime.
        # The dead inner owner still cannot prove settlement of the old group.
        reused_group[105:149] = unrelated_frame[105:149]
        reused_group[193:197] = struct.pack("!I", zlib.crc32(reused_group[:104] + reused_group[105:193]))
        path.write_bytes(reused_group)
        await host.close()
        host_closed = True
        restarted = Supervisor(tmp_path / "state", prepared[1])
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
        await restarted.initialize()
        blocked = restarted.registry.get_slot(slot.id)
        assert blocked.observed_state == "ownership_lost" and blocked.error_code == "ownership_lost"
        assert blocked.generation == slot.generation
        assert restarted.ledger(slot.id)[process.instance_id]["state"] == "ownership_lost"
        assert path.read_bytes() == reused_group
        assert restarted.reservations[slot.id] == {"memory_mb": 32, "gpu": False}
        assert slot.id not in restarted.workers and slot.id not in restarted.listeners
        for action in ("start", "stop", "restart"):
            with pytest.raises(HostError, match="^ownership_lost$"):
                await restarted.control(slot.id, action)
        await asyncio.sleep(0.1)
        real_kill(unrelated.pid, 0)
        assert not unrelated._done.done()
        assert not verify_receipt(unrelated_directory, unrelated_completion)
        real_kill(process.pid, 0)
        real_kill(child_pid, 0)
        assert not signals
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if restarted is not None:
            await restarted.close()
        if not host_closed:
            await host.close()
        for pid in (process.pid, child_pid):
            with contextlib.suppress(ProcessLookupError):
                real_kill(pid, signal.SIGKILL)
        process.close_stdio()
        if unrelated is not None:
            await unrelated_owner.close()
            unrelated.close_stdio()


async def stopped_slot_for_bind_recovery(tmp_path, prepared, proof):
    package, reaper = prepared
    host = Supervisor(tmp_path / "state", reaper)
    try:
        plan = host.plan_install({"package": str(package), "start": False})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        await host.stop_gateway(slot.id)
        if proof == "completed":
            process = await host.launch(slot.id, executable=Path(sys.executable), args=["-c", "pass"],
                cwd=host.root, env={})
            assert await process.wait() == 0
            process.close_stdio()
            assert verify_receipt(host.slot_directory(slot.id), host.ledger(slot.id)[process.instance_id]["completion"])
            slot = host.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
        elif proof == "pending":
            descriptor, completion = prepare_receipt(host.slot_directory(slot.id), uuid.uuid4().hex)
            os.close(descriptor)
            host.ledger(slot.id)[completion["instance_id"]] = {"state": "ownership_lost", "completion": completion,
                "boot_id": host.boot_session.identity}
            host.save_ledger(slot.id)
            slot = host.registry.update_slot(slot.id, observed_state="ownership_lost", error_code="ownership_lost")
        return slot
    finally:
        await host.close()


async def test_startup_port_retry_after_completed_receipt_waits_for_inherited_socket_cleanup(
        tmp_path, prepared, monkeypatch):
    import meecp.supervisor as supervisor

    before = await stopped_slot_for_bind_recovery(tmp_path, prepared, "completed")
    original_bind = supervisor.bind_listener
    inherited = original_bind(before.public_port)
    conflict = asyncio.Event()
    attempts = []
    def record_bind(port=0):
        attempts.append(port)
        try:
            return original_bind(port)
        except HostError as error:
            if error.code == "port_conflict":
                conflict.set()
            raise
    monkeypatch.setattr(supervisor, "bind_listener", record_bind)
    async def finish_inherited_cleanup():
        await conflict.wait()
        await asyncio.sleep(0.1)
        inherited.close()
    cleanup = asyncio.create_task(finish_inherited_cleanup())
    restarted = Supervisor(tmp_path / "state", prepared[1])
    try:
        await asyncio.wait_for(restarted.initialize(), 5)
        recovered = restarted.registry.get_slot(before.id)
        assert recovered.observed_state == "stopped" and recovered.error_code is None
        assert recovered.generation == before.generation and recovered.public_port == before.public_port
        assert before.id in restarted.workers
        assert len(attempts) >= 2 and set(attempts) == {before.public_port}
        assert conflict.is_set() and cleanup.done()
    finally:
        inherited.close()
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        await restarted.close()


async def test_startup_port_retry_for_recovered_slot_stops_when_conflict_persists(
        tmp_path, prepared, monkeypatch):
    import meecp.supervisor as supervisor

    before = await stopped_slot_for_bind_recovery(tmp_path, prepared, "completed")
    original_bind = supervisor.bind_listener
    conflict = original_bind(before.public_port)
    attempts = []
    def record_bind(port=0):
        attempts.append(port)
        return original_bind(port)
    monkeypatch.setattr(supervisor, "bind_listener", record_bind)
    restarted = Supervisor(tmp_path / "state", prepared[1])
    try:
        await asyncio.wait_for(restarted.initialize(), 3)
        blocked = restarted.registry.get_slot(before.id)
        assert blocked.observed_state == "failed" and blocked.error_code == "port_conflict"
        assert blocked.generation == before.generation and blocked.public_port == before.public_port
        assert before.id not in restarted.workers
        assert len(attempts) >= 2 and set(attempts) == {before.public_port}
    finally:
        conflict.close()
        await restarted.close()


@pytest.mark.parametrize("proof,expected_attempts", [("none", 1), ("pending", 0)])
async def test_startup_port_conflict_has_no_retry_without_settled_ownership(
        tmp_path, prepared, monkeypatch, proof, expected_attempts):
    import meecp.supervisor as supervisor

    before = await stopped_slot_for_bind_recovery(tmp_path, prepared, proof)
    original_bind = supervisor.bind_listener
    conflict = original_bind(before.public_port)
    attempts = []
    def record_bind(port=0):
        attempts.append(port)
        return original_bind(port)
    monkeypatch.setattr(supervisor, "bind_listener", record_bind)
    restarted = Supervisor(tmp_path / "state", prepared[1])
    try:
        await restarted.initialize()
        blocked = restarted.registry.get_slot(before.id)
        if proof == "none":
            assert blocked.observed_state == "failed" and blocked.error_code == "port_conflict"
        else:
            assert blocked.observed_state == "ownership_lost" and blocked.error_code == "ownership_lost"
            assert restarted.ledger(before.id)
        assert len(attempts) == expected_attempts
        assert before.id not in restarted.workers
        assert blocked.generation == before.generation and blocked.public_port == before.public_port
    finally:
        conflict.close()
        await restarted.close()
