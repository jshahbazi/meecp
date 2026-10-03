"""Actual scheduled-native-task authority fences with polling held explicitly."""
from __future__ import annotations

import asyncio
import threading

import pytest

from mcpme.processes import ProcessOwner
from mcpme.process_graph_accounting_v1 import FixedProcessGraphV1
from test_mcpme_preparations import foundation
from test_preparation_processes import group_reaper
from test_preparation_probe_confinement import private_runtime
from test_preparation_runner import controlled, finished, started
from test_preparation_managed_confinement import NATIVE_POLICY, _real_verifier, _settled


@NATIVE_POLICY
@pytest.mark.parametrize("mutation", ["cancel", "revoke", "revision", "disable", "bootstrap", "locked-asset",
                                       "runtime-module", "package-extra", "nested-extra", "probe-dir"])
@pytest.mark.parametrize("boundary,with_graph", [("native-task", False), ("native-task", True), ("graph-hash", True)])
async def test_actual_native_task_rechecks_job_authority_without_monitor_polling(
        controlled, private_runtime, monkeypatch, mutation, boundary, with_graph):
    runner = _real_verifier(controlled, private_runtime, monkeypatch, graph_accounting=with_graph)
    entered, resume, monitor_hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
    hash_entered, hash_resume = threading.Event(), threading.Event()
    original = ProcessOwner._launch
    async def held(self, *args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(self, *args, **kwargs)
    async def no_polling(attempt):
        # Trusted scheduling control only. The real store, journal registration,
        # native owner, policy capture/check and atomic attachment still run.
        await monitor_hold.wait()
    if boundary == "native-task":
        monkeypatch.setattr(ProcessOwner, "_launch", held)
    else:
        original_hash = FixedProcessGraphV1.verify_executable
        def held_hash(self, role):
            original_hash(self, role)
            hash_entered.set()
            assert hash_resume.wait(10)
        monkeypatch.setattr(FixedProcessGraphV1, "verify_executable", held_hash)
    monkeypatch.setattr(runner, "_monitor", no_polling)
    native = []
    original_spawn = asyncio.create_subprocess_exec
    async def tracked(*args, **kwargs):
        native.append(True)
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    cancellation = None
    try:
        initial = await started(controlled)
        async with asyncio.timeout(10):
            if boundary == "native-task":
                await entered.wait()
            else:
                while not hash_entered.is_set():
                    await asyncio.sleep(.005)
        assert runner.store.get(initial["job_id"])["state"] == "verifying"
        assert len(runner.store.ownership_receipts(initial["job_id"])) == 1
        assert controlled[-2] and not native
        raw = controlled[4]
        if mutation == "cancel":
            cancellation = asyncio.create_task(runner.cancel(initial["job_id"]))
            async with asyncio.timeout(5):
                while runner.store.get(initial["job_id"])["state"] != "cancel_requested":
                    await asyncio.sleep(.005)
            assert not cancellation.done()
        elif mutation == "revoke":
            controlled[2].revoke(raw["recipe"]["id"], raw["recipe"]["version"])
        elif mutation == "revision":
            controlled[1].revise(controlled[3]["draft_id"], controlled[3]["revision"], name="Changed synthetic task")
        elif mutation in {"bootstrap", "locked-asset", "runtime-module", "package-extra", "nested-extra"}:
            package = runner.attempts[initial["job_id"]].workspace / "candidate"
            relative = {"bootstrap": "bootstrap.py", "locked-asset": "LICENSE",
                        "runtime-module": "python/lib/python3.12/json/__init__.py",
                        "package-extra": "unexpected-file", "nested-extra": "python/lib/python3.12/json/unexpected-file"}[mutation]
            with (package / relative).open("ab") as output:
                output.write(b"\n# late synthetic mutation\n")
        elif mutation == "probe-dir":
            workspace = runner.attempts[initial["job_id"]].workspace / "probe"
            workspace.rename(workspace.with_name("replaced-probe"))
            workspace.mkdir(mode=0o700)
        else:
            controlled[2].disable(raw["recipe"]["id"], raw["recipe"]["version"])
        resume.set()
        hash_resume.set()
        if cancellation is not None:
            await cancellation
        final = await finished(runner, initial["job_id"])
        assert final["state"] == ("succeeded" if mutation == "disable" else "canceled" if mutation == "cancel" else "failed")
        _settled(controlled, final, attached=mutation == "disable")
        assert len(native) == (1 if mutation == "disable" else 0)
        graph = runner.attempts[initial["job_id"]].owners[-1].graph_accounting
        if not with_graph:
            assert graph is None
            return
        report = graph.report()
        assert len(report.members) == 1 and report.members[0].role == "probe"
        assert report.ownership_settled and not report.ownership_lost
        assert report.members[0].state == ("settled" if mutation == "disable" else "nonlaunch")
        assert report.foreground_measurements_complete is (mutation == "disable")
        assert not report.hard_aggregate_enforced and not report.lifetime_descendant_coverage_complete
    finally:
        resume.set()
        hash_resume.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
        await runner.close()
