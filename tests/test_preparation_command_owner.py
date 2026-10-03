"""Real native helpers prove job ownership independently of installed slots."""
from __future__ import annotations

import asyncio
import copy
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from mcpme.host_command import CommandSession, HostCommandAdmission
from mcpme.command_execution_v2 import CommandExecutionError
from mcpme.preparation_command_owner import CommandPreparationOwner, recover_command_ownership
from mcpme.preparation_command_verifier import verify_command_package
from mcpme.preparation_components import parse_component_descriptor
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_contracts import canonical_bytes
from mcpme.private import read_private_json, write_private_json
from test_host_command import native, digest, pytestmark
from test_preparation_command import selected


class Store:
    """In-memory coordinator double only; target and ownership are real native."""
    def __init__(self, workspace):
        self.workspace, self.rows, self.valid = workspace, [], True

    def get(self, job):
        return {"binding": {"import_id": "fixture", "revision": 1}}

    def check_authority(self, job):
        if not self.valid:
            raise PreparationError("stale_preparation")

    def register_command_ownership(self, job, metadata):
        assert read_private_json(self.workspace / "command-probe-v3/command-v2.json") == {"version": 3, "entries": []}
        self.rows.append(copy.deepcopy(metadata))

    def command_ownership(self, job):
        return self.rows[:]

    def confirm_command_ownership(self, job, instance, identity):
        assert recover_command_ownership(self.workspace, self.rows[0]) == identity
        self.rows = [item for item in self.rows if item["instance_id"] != instance]


def owner_at(tmp_path, native, mode="echo"):
    family, draft, descriptor = selected(digest(native[1]), native[2].read_bytes())
    value = descriptor.model_dump(exclude_none=True)
    value["components"][0]["input"]["fixed_args"] = [mode]
    descriptor = parse_component_descriptor(value)
    package = family.prepare(descriptor, draft, None, tmp_path / "candidate", None, lambda: None, acquire_wheels=None, job_id="fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    store = Store(workspace)
    owner = CommandPreparationOwner(HostCommandAdmission(native[1], digest(native[1])), store, "fixture", workspace,
        package, descriptor, attempt_id="a" * 32, check=lambda: None, deadline=time.monotonic() + 10)
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["echo", "tree", "escape", "fds"])
async def test_actual_sdk_job_probe_uses_no_slot_and_settles_descendants(tmp_path, native, mode):
    owner = owner_at(tmp_path, native, mode)
    try:
        assert recover_command_ownership(owner.workspace, owner.metadata) is None  # held writer excludes recovery
        report = await verify_command_package(owner.package.root, owner.descriptor, None, owner.workspace,
            timeout_seconds=5, owner=owner)
        assert report.fixtures_checked == 1 and report.tools_checked == 1 and report.peak_memory_bytes == 0
        assert not owner.store.rows and owner.executor.reconcile()["pending"] == 0
        assert len(owner.executor.journal["entries"]) == 1
        state = owner.workspace / "command-probe-v3"
        assert b'"ok"' not in b"".join(path.read_bytes() for path in state.glob("*.json"))
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_cancel_joins_native_tree_and_prestart_session_cleanup(tmp_path, native):
    owner = owner_at(tmp_path, native, "hang")
    try:
        early = await owner.session(owner.package)
        await early.close()
        assert not owner.sessions
        handle = await owner.session(owner.package)
        task = asyncio.create_task(owner.invoke(handle, "run", {"ok": True}, 1))
        while not owner.executor._native:
            await asyncio.sleep(0.001)
        native_process = next(iter(owner.executor._native.values()))
        assert os.getpgid(native_process.pid) == native_process.pid
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await owner.close()
        assert native_process.poll() in (0, 64)  # Completed tree or proved prelaunch cancellation.
        assert owner.confirm_completed() and not owner.store.rows
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["owner.json", "invocations.json", "command-v2.json", "command-v2.lock"])
async def test_missing_journal_never_proves_nonlaunch(tmp_path, native, missing):
    owner = owner_at(tmp_path, native)
    await owner.close()
    assert recover_command_ownership(owner.workspace, owner.metadata)
    (owner.executor.state / missing).unlink()
    assert recover_command_ownership(owner.workspace, owner.metadata) is None
    assert not owner.confirm_completed() and owner.store.rows


@pytest.mark.asyncio
async def test_unknown_fixture_and_changed_authority_never_launch(tmp_path, native):
    owner = owner_at(tmp_path, native)
    try:
        handle = await owner.session(owner.package)
        with pytest.raises(CommandExecutionError, match="invalid_command_arguments"):
            await owner.invoke(handle, "run", {"ok": False}, 1)
        owner.store.valid = False
        with pytest.raises(PreparationError, match="stale_preparation"):
            await owner.invoke(handle, "run", {"ok": True}, 2)
        assert owner.executor.journal["entries"] == []
    finally:
        await owner.close()
    assert owner.confirm_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fixture", "command", "disk"])
async def test_in_memory_and_disk_mutation_fence_before_native_launch(tmp_path, native, change):
    owner = owner_at(tmp_path, native)
    try:
        handle = await owner.session(owner.package)
        if change == "fixture":
            owner.descriptor.validation[0].arguments["ok"] = False
        elif change == "command":
            owner.package.manifest.command.commands["run"].args.append("unbound")
        else:
            (owner.package.root / "source/LICENSE").write_bytes(b"Changed notice")
        with pytest.raises(Exception):
            await owner.invoke(handle, "run", {"ok": True}, 1)
        assert not owner.executor.journal["entries"]
    finally:
        await owner.close()
    assert owner.confirm_completed()


def test_protocol_constructor_cannot_override_installed_guard():
    with pytest.raises(CommandExecutionError, match="command_admission_required"):
        CommandSession(None, None, None)
    with pytest.raises(CommandExecutionError, match="command_admission_required"):
        CommandSession(None, object(), None, publication_check=lambda: True)


@pytest.mark.asyncio
async def test_sdk_deadline_error_still_settles_job_ownership(tmp_path, native):
    owner = owner_at(tmp_path, native, "hang")
    with pytest.raises((PreparationError, ExceptionGroup)):
        await verify_command_package(owner.package.root, owner.descriptor, None, owner.workspace,
            timeout_seconds=.15, owner=owner)
    assert owner.closed and not owner.sessions and owner.confirm_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["spec", "orphan", "legacy", "nonce", "receipt_failure"])
async def test_recovery_rejects_unmatched_or_failed_native_evidence(tmp_path, native, change):
    owner = owner_at(tmp_path, native)
    await verify_command_package(owner.package.root, owner.descriptor, None, owner.workspace, timeout_seconds=5, owner=owner)
    state = owner.executor.state
    journal = read_private_json(state / "command-v2.json")
    if change == "spec":
        journal["entries"][0]["contract_digest"] = "f" * 64
    elif change == "orphan":
        write_private_json(state / "invocations.json", {"version": 1, "entries": []})
    elif change == "legacy":
        journal["version"] = 1
    elif change == "nonce":
        journal["entries"][0]["nonce"] = "f" * 64
    else:
        journal["entries"][0]["receipt_failed"] = True
    write_private_json(state / "command-v2.json", journal)
    assert recover_command_ownership(owner.workspace, owner.metadata) is None


@pytest.mark.asyncio
async def test_actual_host_sigkill_recovery_requires_native_publication(tmp_path, native):
    # Build without launching in this process. The child owns the helper session;
    # only the held child Popen handle is killed, never discovered target PIDs.
    family, draft, descriptor = selected(digest(native[1]), native[2].read_bytes())
    value = descriptor.model_dump(exclude_none=True)
    value["components"][0]["input"]["fixed_args"] = ["hang"]
    value["components"][0]["input"]["deadline_ms"] = 3000
    descriptor = parse_component_descriptor(value)
    package = family.prepare(descriptor, draft, None, tmp_path / "candidate", None, lambda: None, acquire_wheels=None, job_id="fixture")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    script = '''import asyncio,json,os,sys,time
from pathlib import Path
from mcpme.contracts import validate_package
from mcpme.host_command import HostCommandAdmission
from mcpme.preparation_components import parse_component_descriptor
from mcpme.preparation_command_owner import CommandPreparationOwner
class Store:
 def get(self,job): return {"binding":{"import_id":"fixture","revision":1}}
 def check_authority(self,job): pass
 def register_command_ownership(self,job,metadata): pass
async def main():
 descriptor=parse_component_descriptor(json.loads(sys.stdin.buffer.readline()))
 owner=CommandPreparationOwner(HostCommandAdmission(Path(sys.argv[1]),sys.argv[2]),Store(),"fixture",Path(sys.argv[3]),validate_package(Path(sys.argv[4])),descriptor,attempt_id="a"*32,check=lambda:None,deadline=time.monotonic()+10)
 handle=await owner.session(owner.package)
 task=asyncio.create_task(owner.invoke(handle,"run",{"ok":True},1))
 while not owner.executor._native: await asyncio.sleep(.001)
 await asyncio.sleep(.05)
 os.write(1,b"R")
 await task
asyncio.run(main())
'''
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend")}
    child = subprocess.Popen([sys.executable, "-B", "-c", script, str(native[1]), digest(native[1]), str(workspace), str(package.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
    try:
        child.stdin.write(canonical_bytes(descriptor.model_dump(exclude_none=True)) + b"\n")
        child.stdin.close()
        os.set_blocking(child.stdout.fileno(), False)
        end = time.monotonic() + 5
        ready = b""
        while time.monotonic() < end and not ready:
            try:
                ready = os.read(child.stdout.fileno(), 1)
            except BlockingIOError:
                pass
            await asyncio.sleep(.005)
        assert ready == b"R"
        metadata = read_private_json(workspace / "command-probe-v3/owner.json")
        assert recover_command_ownership(workspace, metadata) is None
        child.kill()
        await asyncio.to_thread(child.wait, 5)
        end = time.monotonic() + 5
        identity = None
        while time.monotonic() < end and identity is None:
            identity = recover_command_ownership(workspace, metadata)
            await asyncio.sleep(.01)
        assert identity is not None
        receipt = next((workspace / "command-probe-v3").glob("*.complete"))
        assert receipt.read_bytes()[296:297] == b"C"
        receipt.unlink()
        assert recover_command_ownership(workspace, metadata) is None
    finally:
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.wait, 5)
        child.stdout.close()
