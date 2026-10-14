"""Post-build write failures through the production runner and stage cleanup.

The recipe builder/verifier use the existing private controlled fixture. The
builder additionally runs a real small child through the normal scoped command
path and compiled native reaper. No released-host or public admission is claimed.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace

import pytest

from meecp.preparation_builder import CommandSpec
from meecp.preparation_runner import JournaledPreparationOwner
from meecp.preparations import RECEIPT_NAME
from test_preparation_runner import controlled, finished, foundation, group_reaper, started


@pytest.mark.parametrize("boundary,hold_cleanup", [("receipt", False), ("stage", False), ("stage", True)])
async def test_post_build_enospc_cannot_attach_or_release_before_owned_cleanup(
    controlled, monkeypatch, capfd, boundary, hold_cleanup,
):
    import meecp.preparation_runner as runner_module
    import meecp.private as private_module

    # Exercise streamed-write ENOSPC independently of APFS clone availability.
    # A successful clone legitimately bypasses the write hook below.
    monkeypatch.setattr(private_module, "_clone_file", lambda *args, **kwargs: False)

    runner, imports, _, draft, _, resources, released = controlled
    child_handles = []
    built = threading.Event()
    faulted = threading.Event()
    cleanup_entered, cleanup_resume, cleanup_finished = (threading.Event() for _ in range(3))
    writes = []
    launch = JournaledPreparationOwner.launch

    async def observe_launch(self, *args, **kwargs):
        process = await launch(self, *args, **kwargs)
        child_handles.append(process)
        return process

    monkeypatch.setattr(JournaledPreparationOwner, "launch", observe_launch)
    toolchain = SimpleNamespace(identity="d" * 64, uv=Path(sys.executable).resolve())
    monkeypatch.setattr(runner_module, "load_private_toolchain", lambda *args: toolchain)
    build = runner_module.prepare_bound

    def owned_build(descriptor, draft, toolchain, output, run, check, *, acquire_wheels):
        work = output.parent / (output.name + "-builder")
        work.mkdir(mode=0o700)
        environment = {"PATH": "/usr/bin:/bin", "UV_PYTHON_DOWNLOADS": "never", "PYTHONNOUSERSITE": "1",
            **{key: str(work / key.lower()) for key in ("HOME", "TMPDIR", "UV_CACHE_DIR", "UV_PYTHON")}}
        result = run(CommandSpec((str(toolchain.uv), "-I", "-B", "-c", "print('owned-build-completed')"),
            work, environment, 5, "private_python_version", memory_mb=128))
        assert result == "owned-build-completed\n"
        assert len(child_handles) == 1 and child_handles[0].returncode == 0
        assert child_handles[0].wrapper.returncode == 0 and child_handles[0]._monitor.done()
        built.set()
        return build(descriptor, draft, toolchain, output, run, check, acquire_wheels=acquire_wheels)

    monkeypatch.setattr(runner_module, "prepare_bound", owned_build)
    opened = {}
    real_open, real_fdopen = os.open, os.fdopen

    def fault_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        candidate = Path(os.fsdecode(path))
        receipt = candidate.name == RECEIPT_NAME and candidate.parent.name == "candidate"
        stage = candidate.is_relative_to(imports.attachment_staging) and candidate.parent != imports.attachment_staging
        if flags & os.O_WRONLY and ((boundary == "receipt" and receipt) or (boundary == "stage" and stage)):
            opened[descriptor] = candidate
        return descriptor

    class FailingOutput:
        def __init__(self, output, path):
            self.output, self.path = output, path
        def __enter__(self):
            self.output.__enter__()
            return self
        def __exit__(self, *args):
            return self.output.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.output, name)
        def write(self, data):
            assert built.is_set() and not faulted.is_set() and data
            amount = self.output.write(data[:min(7, len(data))])
            self.output.flush()
            writes.append((self.path, amount, threading.get_ident()))
            faulted.set()
            raise OSError(errno.ENOSPC, "synthetic-private-enospc")

    def fault_fdopen(descriptor, mode="r", *args, **kwargs):
        path = opened.pop(descriptor, None)
        output = real_fdopen(descriptor, mode, *args, **kwargs)
        return FailingOutput(output, path) if path is not None and mode == "wb" else output

    monkeypatch.setattr(os, "open", fault_open)
    monkeypatch.setattr(os, "fdopen", fault_fdopen)
    remove = shutil.rmtree

    def held_remove(path, *args, **kwargs):
        path = Path(path)
        if hold_cleanup and path.parent == imports.attachment_staging and path.name.endswith(".tmp"):
            assert faulted.is_set() and path.exists()
            cleanup_entered.set()
            assert cleanup_resume.wait(15)
            try:
                return remove(path, *args, **kwargs)
            finally:
                cleanup_finished.set()
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", held_remove)
    release = runner.release_resources
    release_snapshots = []

    async def release_after_join(identifier, *, ownership_settled):
        attempt = runner.attempts[identifier]
        assert built.is_set() and faulted.is_set() and ownership_settled
        assert not attempt.threads and not attempt.callbacks
        assert all(not owner.instances and not owner._launches and not owner._pending_ids for owner in attempt.owners)
        assert all(process.returncode == 0 and process.wrapper.returncode == 0 and process._monitor.done()
            and process._done.done() for process in child_handles)
        assert not attempt.workspace.exists()
        assert not [path for path in imports.attachment_staging.iterdir() if path.name != ".lock"]
        assert not hold_cleanup or cleanup_finished.is_set()
        release_snapshots.append(identifier)
        await release(identifier, ownership_settled=ownership_settled)

    runner.release_resources = release_after_join
    job = await started(controlled)
    try:
        if hold_cleanup:
            async with asyncio.timeout(10):
                while not cleanup_entered.is_set():
                    await asyncio.sleep(.01)
            attempt = runner.attempts[job["job_id"]]
            current = runner.store.get(job["job_id"])
            assert current["state"] == "attaching" and not current["ownership_settled"] and current["reservation_retained"]
            assert resources == {job["job_id"]: attempt.policy} and not released and not release_snapshots
            assert attempt.threads and any(not task.done() for task in attempt.threads)
            assert not runner.tasks[job["job_id"]].done() and attempt.workspace.exists()
            assert imports.inspect(draft["draft_id"])["attachment"] is None
            assert len(writes) == 1 and writes[0][0].exists() and writes[0][0].stat().st_size == writes[0][1]
            assert not cleanup_finished.is_set()
            cleanup_resume.set()
        result = await finished(runner, job["job_id"])
        expected = "preparation_failed" if boundary == "receipt" else "package_snapshot_failed"
        assert result["state"] == "failed" and result["error_code"] == expected
        assert result["ownership_settled"] and not result["reservation_retained"]
        assert result["progress"]["commands_finished"] == 1
        assert len(writes) == 1 and writes[0][1] > 0 and writes[0][2] != threading.get_ident()
        assert not resources and released == release_snapshots == [job["job_id"]]
        assert imports.inspect(draft["draft_id"])["attachment"] is None
        assert not runner.store.pending_ownership(job["job_id"])
        assert not Path(result["workspace"]).exists()
        assert not [path for path in imports.attachment_staging.iterdir() if path.name != ".lock"]
        assert "synthetic-private-enospc" not in json.dumps(result)
        assert "synthetic-private-enospc" not in "".join(capfd.readouterr())
    finally:
        cleanup_resume.set()
        await runner.close()
    assert not runner.attempts[job["job_id"]].threads and runner.tasks[job["job_id"]].done()
    assert not resources and released == release_snapshots == [job["job_id"]]
    await runner.close()
    assert released == release_snapshots == [job["job_id"]]
