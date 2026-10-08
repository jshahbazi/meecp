"""Durable identity receipts require native waitpid proof or no-child proof."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

from meecp.ownership_receipts import (RECEIPT_BYTES, prepare_receipt,
                                    validate_receipt_descriptor, verify_receipt)
from meecp.private import PrivateStateError
from meecp.processes import OwnershipLost, ProcessError, ProcessOwner


@pytest.fixture(scope="module")
def receipt_reaper(tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp("receipt-reaper") / "meecp-reap"
    source = Path(__file__).resolve().parents[1] / "scripts/meecp-reap.c"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(target)],
                   check=True, capture_output=True)
    return target


def receipt_path(slot: Path, metadata: dict) -> Path:
    return slot / "ownership-receipts" / (metadata["instance_id"] + ".receipt")


def complete_fixture(descriptor: int) -> None:
    os.pwrite(descriptor, b"C", RECEIPT_BYTES - 1)
    os.fsync(descriptor)


def test_pending_receipt_is_private_durable_and_not_completion(tmp_path):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex)
    try:
        assert set(metadata) == {"schema_version", "instance_id", "nonce"}
        assert metadata["schema_version"] == 1 and len(metadata["nonce"]) == 64
        assert receipt_path(tmp_path, metadata).stat().st_mode & 0o777 == 0o600
        assert receipt_path(tmp_path, metadata).parent.stat().st_mode & 0o777 == 0o700
        assert not os.get_inheritable(descriptor)
        assert validate_receipt_descriptor(descriptor, metadata["instance_id"], metadata["nonce"])
        assert not verify_receipt(tmp_path, metadata)
        complete_fixture(descriptor)
        assert verify_receipt(tmp_path, metadata)
        assert not validate_receipt_descriptor(descriptor, metadata["instance_id"], metadata["nonce"])
    finally:
        os.close(descriptor)


def test_duplicate_identity_never_overwrites_existing_receipt(tmp_path):
    identity = uuid.uuid4().hex
    descriptor, metadata = prepare_receipt(tmp_path, identity)
    try:
        before = receipt_path(tmp_path, metadata).read_bytes()
        with pytest.raises(PrivateStateError, match="ownership_receipt_unavailable"):
            prepare_receipt(tmp_path, identity)
        assert receipt_path(tmp_path, metadata).read_bytes() == before
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("kind", ["missing", "pending", "truncated", "trailing", "wrong_nonce", "wrong_id", "wrong_magic",
    "public_file", "public_parent", "public_slot", "symlink", "hardlink", "directory", "ancestor_symlink"])
def test_missing_mismatched_and_unsafe_completion_never_resolves(tmp_path, kind):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex)
    complete_fixture(descriptor)
    os.close(descriptor)
    path = receipt_path(tmp_path, metadata)
    if kind == "missing":
        path.unlink()
    elif kind == "pending":
        data = path.read_bytes()
        path.write_bytes(data[:-1] + b"P")
    elif kind == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    elif kind == "trailing":
        path.write_bytes(path.read_bytes() + b"extra")
    elif kind == "wrong_nonce":
        metadata = {**metadata, "nonce": "0" * 64}
    elif kind == "wrong_id":
        metadata = {**metadata, "instance_id": uuid.uuid4().hex}
    elif kind == "wrong_magic":
        path.write_bytes(b"XXXXXXXX" + path.read_bytes()[8:])
    elif kind == "public_file":
        path.chmod(0o644)
    elif kind == "public_parent":
        path.parent.chmod(0o755)
    elif kind == "public_slot":
        tmp_path.chmod(0o755)
    elif kind == "symlink":
        retained = path.with_name("retained")
        path.rename(retained)
        path.symlink_to(retained)
    elif kind == "hardlink":
        os.link(path, path.with_name("other"))
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif kind == "ancestor_symlink":
        alias = tmp_path.parent / ("alias-" + uuid.uuid4().hex)
        alias.symlink_to(tmp_path, target_is_directory=True)
        try:
            assert not verify_receipt(alias, metadata)
        finally:
            alias.unlink()
        return
    assert not verify_receipt(tmp_path, metadata)


@pytest.mark.parametrize("metadata", [None, [], {}, {"schema_version": True, "instance_id": "a" * 32, "nonce": "b" * 64},
    {"schema_version": 1, "instance_id": "../escape", "nonce": "b" * 64},
    {"schema_version": 1, "instance_id": "a" * 32, "nonce": "B" * 64},
    {"schema_version": 1, "instance_id": "a" * 32, "nonce": "b" * 64, "path": "/outside"}])
def test_invalid_metadata_is_closed_and_does_not_create_state(tmp_path, metadata):
    absent = tmp_path / "absent"
    assert not verify_receipt(absent, metadata)
    assert not absent.exists()


@pytest.mark.parametrize("identity", ["", "../escape", "unacknowledged", "A" * 32, "a" * 31])
def test_prepare_requires_closed_instance_identity(tmp_path, identity):
    with pytest.raises(PrivateStateError, match="ownership_receipt_unavailable"):
        prepare_receipt(tmp_path, identity)
    assert not (tmp_path / "ownership-receipts").exists()


async def test_native_clean_exit_and_descriptor_capability_policy(receipt_reaper, tmp_path):
    owner = ProcessOwner(receipt_reaper)
    identity = uuid.uuid4().hex
    descriptor, metadata = prepare_receipt(tmp_path, identity)
    code = ("import os,json; from pathlib import Path; Path('private-created').write_text('synthetic'); "
            "fds=[fd for fd in range(3,256) if os.path.exists('/dev/fd/'+str(fd))]; print(json.dumps(fds),flush=True)")
    try:
        process = await owner.launch(Path(sys.executable), ["-c", code], tmp_path, {}, instance_id=identity,
                                     completion_fd=descriptor, completion_nonce=metadata["nonce"])
        assert json.loads(await process.stdout.readline()) == []
        assert await process.wait() == 0
        assert verify_receipt(tmp_path, metadata)
        assert (tmp_path / "private-created").stat().st_mode & 0o777 == 0o600
        assert not owner.instances
        process.close_stdio()
    finally:
        os.close(descriptor)
        await owner.close()


async def test_native_exec_failure_proves_absence(receipt_reaper, tmp_path):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex)
    owner = ProcessOwner(receipt_reaper)
    try:
        with pytest.raises(ProcessError, match="launch_failed"):
            await owner.launch(tmp_path / "missing", [], tmp_path, {}, instance_id=metadata["instance_id"],
                               completion_fd=descriptor, completion_nonce=metadata["nonce"])
        assert verify_receipt(tmp_path, metadata)
        assert not owner.instances
    finally:
        os.close(descriptor)
        await owner.close()


async def test_receipt_nonce_and_inheritance_must_match_before_launch(receipt_reaper, tmp_path):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex)
    owner = ProcessOwner(receipt_reaper)
    try:
        for values in [{"completion_nonce": "0" * 64}, {"completion_nonce": metadata["nonce"], "inherited_fds": (descriptor,)}]:
            with pytest.raises(ProcessError, match="invalid_launch"):
                await owner.launch(Path(sys.executable), ["-c", "pass"], tmp_path, {}, instance_id=metadata["instance_id"],
                                   completion_fd=descriptor, **values)
        assert not owner.instances and not verify_receipt(tmp_path, metadata)
    finally:
        os.close(descriptor)


async def test_forced_reaper_loss_with_receipt_retains_ownership_block(receipt_reaper, tmp_path, monkeypatch):
    descriptor, metadata = prepare_receipt(tmp_path, uuid.uuid4().hex)
    owner = ProcessOwner(receipt_reaper)
    process = await owner.launch(Path(sys.executable), ["-c", "import time; time.sleep(100)"], tmp_path, {},
        instance_id=metadata["instance_id"], completion_fd=descriptor, completion_nonce=metadata["nonce"])
    target = process.pid
    real_kill = os.kill
    real_kill(process.reaper_pid, signal.SIGKILL)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    try:
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(process.wait(), 5)
        assert process.ownership_lost and process.instance_id in owner.instances
        assert not verify_receipt(tmp_path, metadata)
        with pytest.raises(OwnershipLost):
            await owner.launch(Path(sys.executable), ["-c", "pass"], tmp_path, {})
        assert not signals
    finally:
        os.close(descriptor)
        process.close_stdio()
        with contextlib.suppress(ProcessLookupError):
            real_kill(target, signal.SIGKILL)


@pytest.mark.parametrize("preack", [False, True])
def test_parent_sigkill_has_durable_completion_for_ack_and_preack(receipt_reaper, tmp_path, preack):
    """An actual parent dies; the native child must settle and leave exact proof."""
    metadata_path, ready_path, target_path = [tmp_path / name for name in ("metadata.json", "ready", "target")]
    backend = str(Path(__file__).resolve().parents[1] / "backend")
    helper = (
        "import json,os,socket,struct,subprocess,sys,time,uuid; from pathlib import Path; "
        "from meecp.ownership_receipts import prepare_receipt; "
        "fd,metadata=prepare_receipt(Path(sys.argv[2]),uuid.uuid4().hex); "
        "Path(sys.argv[3]).write_text(json.dumps(metadata)); Path(sys.argv[3]).chmod(0o600); "
        "spec,child=socket.socketpair(); ctrl,control=socket.socketpair(); "
        "inp=os.open('/dev/null',os.O_RDONLY); out=os.open('/dev/null',os.O_WRONLY); "
        "p=subprocess.Popen([sys.argv[1],str(child.fileno()),str(control.fileno()),str(os.getpid()),str(fd)], "
        "pass_fds=(child.fileno(),control.fileno(),inp,out,fd),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "child.close(); control.close(); os.close(fd); "
        "num=lambda n:struct.pack('!I',n); text=lambda s:num(len(s.encode()))+s.encode(); "
        "code='import os,time; from pathlib import Path; Path('+repr(sys.argv[5])+').write_text(str(os.getpid())); time.sleep(100)'; "
        "payload=b'MCPME002'+text(metadata['instance_id'])+text(metadata['nonce'])+num(50)+num(inp)+num(out)+num(0)+text(sys.executable)+text(sys.argv[2])+num(2)+text('-c')+text(code)+num(0); "
        "spec.sendall(payload[:8] if sys.argv[6]=='True' else payload); "
        "Path(sys.argv[4]).write_text(str(p.pid)); time.sleep(100)"
    )
    parent = subprocess.Popen([sys.executable, "-c", helper, str(receipt_reaper), str(tmp_path), str(metadata_path),
        str(ready_path), str(target_path), str(preack)], env={"PYTHONPATH": backend},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = None
    try:
        deadline = time.monotonic() + 8
        while not ready_path.exists() or (not preack and not target_path.exists()):
            assert parent.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        metadata = json.loads(metadata_path.read_text())
        assert not verify_receipt(tmp_path, metadata)
        if target_path.exists():
            target = int(target_path.read_text())
        parent.kill()
        parent.wait(timeout=5)
        while not verify_receipt(tmp_path, metadata):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        if preack:
            assert not target_path.exists()
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(target, 0)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if target is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(target, signal.SIGKILL)
