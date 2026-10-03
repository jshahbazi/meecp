"""Distinguish positive host nonlaunch proof from unknown process ownership."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import uuid

import pytest

from mcpme.ownership_receipts import acknowledge_nonlaunch, prepare_receipt, verify_receipt
from mcpme.processes import ProcessError, ProcessOwner
from test_mcpme_processes import reaper


async def _launch(owner, workspace, executable, **kwargs):
    instance = uuid.uuid4().hex
    descriptor, metadata = prepare_receipt(workspace, instance)
    calls = []
    def prove():
        calls.append(True)
        acknowledge_nonlaunch(descriptor, instance, metadata["nonce"])
    try:
        result = await owner.launch(executable, [], workspace, {}, instance_id=instance,
            completion_fd=descriptor, completion_nonce=metadata["nonce"], owned_group=True,
            nonlaunch_callback=prove, **kwargs)
        return result, metadata, calls
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
async def test_missing_wrapper_proves_nonlaunch(tmp_path):
    owner = ProcessOwner(tmp_path / "missing-native-wrapper")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    with pytest.raises(ProcessError):
        await _launch(owner, workspace, Path(sys.executable))
    receipt = next((workspace / "ownership-receipts").iterdir())
    assert receipt.read_bytes()[-1:] == b"N"
    assert owner.instances == {}
    await owner.close()


@pytest.mark.asyncio
async def test_preflight_failure_is_proved_without_calling_native(tmp_path):
    owner = ProcessOwner(tmp_path / "missing-native-wrapper")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    with pytest.raises(ProcessError, match="invalid_launch"):
        await _launch(owner, workspace, Path(sys.executable), startup_timeout=0)
    assert next((workspace / "ownership-receipts").iterdir()).read_bytes()[-1:] == b"N"


@pytest.mark.asyncio
async def test_real_wrapper_completes_native_proof_without_nonlaunch_ack(reaper, tmp_path):
    owner = ProcessOwner(reaper)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    process, metadata, calls = await _launch(owner, workspace, Path(sys.executable))
    process.stdin.close()
    await process.wait()
    assert calls == []
    assert verify_receipt(workspace, metadata)
    assert next((workspace / "ownership-receipts").iterdir()).read_bytes()[-1:] == b"C"
    await owner.close()


@pytest.mark.asyncio
async def test_unknown_creation_failure_does_not_manufacture_settlement(tmp_path, monkeypatch):
    import asyncio
    owner = ProcessOwner(tmp_path / "native-wrapper")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    async def uncertain(*args, **kwargs):
        raise OSError("unknown native creation outcome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", uncertain)
    with pytest.raises(ProcessError):
        await _launch(owner, workspace, Path(sys.executable))
    assert next((workspace / "ownership-receipts").iterdir()).read_bytes()[-1:] == b"P"
