"""Trusted managed stdio selection and actual no-model private probe races."""
from __future__ import annotations

import asyncio
from pathlib import Path
import platform
import shutil
import sys
import threading
from types import SimpleNamespace

import pytest

from meecp.contracts import MAX_ARTIFACTS, validate_package
from meecp.preparation_contracts import PreparationError
from meecp.preparation_onnx import OnnxPreparationFamily
from meecp.preparation_probe_confinement import ProbeConfinementV1
from meecp.preparation_resolved_python import ResolvedPythonFamily
from meecp.preparation_runner import PreparationRunner
from meecp.preparation_verifier import _ProbeLaunchIdentity, verify_prepared_package
from test_meecp_packages import edit_manifest
from test_meecp_preparations import foundation
from test_preparation_processes import group_reaper
from test_preparation_probe_confinement import _lock_all, _private_fixture, private_runtime
from test_preparation_runner import controlled, finished, started
from test_preparation_verifier import probe_reaper

NATIVE_POLICY = pytest.mark.skipif(
    sys.platform != "darwin" or platform.machine() != "arm64" or not platform.mac_ver()[0].startswith("26."),
    reason="macOS 26 arm64 probe policy",
)


@pytest.mark.parametrize("family", [None, ResolvedPythonFamily(None), OnnxPreparationFamily(None)])
async def test_managed_common_selection_is_host_owned(monkeypatch, tmp_path, family):
    families = {} if family is None else {family.component.reference.key: family}
    store = SimpleNamespace(families=families, family_for=lambda _: family)
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True, enabled_families=families)
    descriptor = SimpleNamespace(schema_version=1 if family is None else 2)
    received = []
    async def selected(*args, **kwargs):
        received.append(kwargs)
        return "checked"
    monkeypatch.setattr("meecp.preparation_runner.verify_prepared_package", selected)
    assert await runner._verify(tmp_path, descriptor, tmp_path, timeout_seconds=1, owner=None) == "checked"
    assert received == [{"timeout_seconds": 1, "owner": None, "confine_private_python": True}]
    runner.enabled = False
    with pytest.raises(PreparationError) as failure:
        await runner._verify(tmp_path, descriptor, tmp_path, timeout_seconds=1, owner=None)
    assert failure.value.code == "preparation_runner_unavailable" and len(received) == 1


async def test_specialist_verifier_contract_is_preserved(tmp_path):
    received = []
    async def specialist(*args, timeout_seconds, owner):
        received.append((timeout_seconds, owner))
        return "specialist"
    key = ("test.specialist", "1.0.0", "a" * 64)
    family = SimpleNamespace(component=SimpleNamespace(reference=SimpleNamespace(key=key)),
                             target_profile="host-command", verify=specialist)
    store = SimpleNamespace(families={key: family}, family_for=lambda _: family)
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True, enabled_families=[key])
    assert await runner._verify(tmp_path, SimpleNamespace(schema_version=2), tmp_path,
                                timeout_seconds=1, owner=None) == "specialist"
    assert received == [(1, None)]


@pytest.mark.parametrize("value", [1, None, "true"])
async def test_confinement_host_argument_is_exact_boolean(tmp_path, value):
    with pytest.raises(PreparationError) as failure:
        await verify_prepared_package(tmp_path, None, tmp_path, tmp_path, confine_private_python=value)
    assert failure.value.code == "preparation_probe_confinement_mismatch"


async def test_managed_host_selection_cannot_be_combined_with_supplied_plan(tmp_path):
    with pytest.raises(PreparationError) as failure:
        await verify_prepared_package(tmp_path, None, tmp_path, tmp_path,
                                       confine_private_python=True, probe_confinement=object())
    assert failure.value.code == "preparation_probe_confinement_mismatch"


@pytest.mark.parametrize("value", [1, None, "true"])
def test_probe_graph_flag_is_trusted_exact_boolean(tmp_path, value):
    with pytest.raises(PreparationError) as failure:
        PreparationRunner(SimpleNamespace(families={}), tmp_path, tmp_path,
                          probe_graph_accounting=value)
    assert failure.value.code == "invalid_preparation_probe_accounting"


def test_metadata_guard_enforces_file_membership_cap_before_stat(tmp_path, monkeypatch):
    package = SimpleNamespace(root=tmp_path / "package", lock=SimpleNamespace(
        artifacts=[SimpleNamespace(path="file-" + str(index)) for index in range(MAX_ARTIFACTS + 1)]))
    plan = SimpleNamespace(reaper=tmp_path / "reaper", workspace=tmp_path / "probe",
                           home=tmp_path / "probe/home", temporary=tmp_path / "probe/tmp")
    monkeypatch.setattr(_ProbeLaunchIdentity, "_identity", staticmethod(
        lambda *args: pytest.fail("oversized guard read metadata")))
    with pytest.raises(PreparationError) as failure:
        _ProbeLaunchIdentity.capture(package, plan)
    assert failure.value.code == "preparation_probe_confinement_mismatch"


def _managed_build(runtime, store, *, checks="", unknown=False, onnx_launcher=False):
    def build(descriptor, draft, toolchain, output, run, check, *, acquire_wheels):
        check()
        temporary = output.parent / "synthetic-fixture"
        temporary.mkdir(mode=0o700)
        package, _, _ = _private_fixture(temporary, runtime, checks=checks)
        bootstrap = (package / "bootstrap.py").read_text()
        # A tiny wire server exercises the real SDK and owned sandbox. Builder
        # scheduling is controlled; no dependency resolution or model is run.
        lines = bootstrap.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("tool="):
                lines[index] = "tool=" + repr(descriptor.interface.tools[0].mcp_schema())
            elif "output={'value':" in line:
                lines[index] = "        output=" + repr(descriptor.validation[0].expected)
        (package / "bootstrap.py").write_text("\n".join(lines) + "\n")
        if unknown:
            (package / "bin/service").write_text("#!/bin/sh\nexec /usr/bin/true\n")
        elif onnx_launcher:
            # These are the exact published ONNX launch bytes. The fixture is
            # a tiny wire server, so this case never loads an ONNX model.
            launcher = (package / "bin/service").read_text().replace(' "$@"', '')
            (package / "bin/service").write_text(launcher)
        edit_manifest(package, **store._candidate_contract(descriptor, draft["source"]["revision"]))
        _lock_all(package)
        package.rename(output)
        shutil.rmtree(temporary)
        check()
        return validate_package(output)
    return build


def _real_verifier(controlled, private_runtime, monkeypatch, *, checks="", unknown=False,
                   onnx_launcher=False, graph_accounting=False):
    runner, *_ = controlled
    runner.probe_graph_accounting = graph_accounting
    monkeypatch.setattr("meecp.preparation_runner.prepare_bound",
                        _managed_build(private_runtime, runner.store, checks=checks, unknown=unknown,
                                       onnx_launcher=onnx_launcher))
    monkeypatch.setattr("meecp.preparation_runner.verify_prepared_package", verify_prepared_package)
    return runner


def _settled(controlled, job, *, attached):
    runner, imports, _, draft, _, resources, released = controlled
    assert job["ownership_settled"] and not job["reservation_retained"]
    assert not resources and released == [job["job_id"]]
    assert not Path(job["workspace"]).exists()
    attempt = runner.attempts[job["job_id"]]
    assert not attempt.threads and not attempt.callbacks
    assert all(not owner.instances for owner in attempt.owners)
    assert runner.store.ownership_receipts(job["job_id"]) == []
    current = imports.inspect(draft["draft_id"])
    assert (current["attachment"] is not None) is attached
    assert not [path for path in imports.attachment_staging.iterdir() if path.name != ".lock"]


@NATIVE_POLICY
@pytest.mark.parametrize("onnx_launcher", [False, True])
async def test_actual_managed_sdk_confinement_with_original_removed(
        controlled, private_runtime, monkeypatch, tmp_path, onnx_launcher):
    canary = tmp_path / "synthetic-host-canary"
    canary.write_text("synthetic")
    checks = ("def denied(action):\n"
              " try: action()\n"
              " except OSError: return\n"
              " raise RuntimeError('unexpected permission')\n"
              "denied(lambda:open(" + repr(str(canary)) + ").read())\n"
              "denied(lambda:os.stat(" + repr(str(canary)) + "))\n"
              "denied(lambda:open('LICENSE','w'))\n"
              "denied(os.fork)\n"
              "denied(os.setsid)\n"
              "open(os.path.join(os.environ['HOME'],'cache'),'w').write('synthetic')\n")
    runner = _real_verifier(controlled, private_runtime, monkeypatch, checks=checks, onnx_launcher=onnx_launcher)
    shutil.rmtree(tmp_path / "source")
    try:
        initial = await started(controlled)
        final = await finished(runner, initial["job_id"])
        assert final["state"] == "succeeded"
        assert final["verification"]["tools_checked"] == final["verification"]["fixtures_checked"] == 1
        assert 0 < final["verification"]["peak_memory_bytes"] <= 64 * 1024**2
        _settled(controlled, final, attached=True)
    finally:
        await runner.close()


@NATIVE_POLICY
async def test_unknown_managed_launcher_never_creates_native_process(controlled, private_runtime, monkeypatch):
    runner = _real_verifier(controlled, private_runtime, monkeypatch, unknown=True)
    native = []
    original = asyncio.create_subprocess_exec
    async def tracked(*args, **kwargs):
        native.append(True)
        return await original(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    try:
        initial = await started(controlled)
        final = await finished(runner, initial["job_id"])
        assert final["state"] == "failed" and final["error_code"] == "unsupported_preparation_probe_confinement"
        assert native == []
        _settled(controlled, final, attached=False)
    finally:
        await runner.close()


async def _barrier(entered):
    async with asyncio.timeout(10):
        while not entered.is_set():
            await asyncio.sleep(.005)


@NATIVE_POLICY
@pytest.mark.parametrize("mutation", ["cancel", "revoke", "revision"])
async def test_managed_capture_worker_is_joined_before_authority_cleanup(
        controlled, private_runtime, monkeypatch, mutation):
    runner = _real_verifier(controlled, private_runtime, monkeypatch)
    entered, resume, completed = threading.Event(), threading.Event(), threading.Event()
    original = ProbeConfinementV1.capture.__func__
    def held(cls, *args):
        plan = original(cls, *args)
        entered.set()
        assert resume.wait(10)
        completed.set()
        return plan
    monkeypatch.setattr(ProbeConfinementV1, "capture", classmethod(held))
    native = []
    original_spawn = asyncio.create_subprocess_exec
    async def tracked(*args, **kwargs):
        native.append(True)
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    cancellation = None
    try:
        initial = await started(controlled)
        await _barrier(entered)
        current = runner.store.get(initial["job_id"])
        assert current["state"] == "verifying" and current["reservation_retained"]
        if mutation == "cancel":
            cancellation = asyncio.create_task(runner.cancel(initial["job_id"]))
        elif mutation == "revoke":
            controlled[2].revoke(controlled[4]["recipe"]["id"], controlled[4]["recipe"]["version"])
        else:
            controlled[1].revise(controlled[3]["draft_id"], controlled[3]["revision"], name="Changed synthetic task")
        async with asyncio.timeout(5):
            while not runner.attempts[initial["job_id"]].stop.is_set():
                await asyncio.sleep(.005)
        runner.tasks[initial["job_id"]].cancel()
        await asyncio.sleep(.02)
        assert not runner.tasks[initial["job_id"]].done() and not completed.is_set()
        assert controlled[-2] and Path(current["workspace"]).exists() and native == []
        resume.set()
        if cancellation is not None:
            await cancellation
        final = await finished(runner, initial["job_id"])
        assert final["state"] in {"canceled", "failed"} and completed.is_set() and native == []
        _settled(controlled, final, attached=False)
    finally:
        resume.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
        await runner.close()


@NATIVE_POLICY
@pytest.mark.parametrize("mutation", ["bootstrap", "runtime", "workspace", "owner-helper"])
async def test_managed_capture_rechecks_exact_private_bindings_before_launch(
        controlled, private_runtime, monkeypatch, mutation):
    runner = _real_verifier(controlled, private_runtime, monkeypatch)
    entered, resume = threading.Event(), threading.Event()
    plans = []
    original = ProbeConfinementV1.capture.__func__
    def held(cls, *args):
        plan = original(cls, *args)
        plans.append(plan)
        entered.set()
        assert resume.wait(10)
        return plan
    monkeypatch.setattr(ProbeConfinementV1, "capture", classmethod(held))
    native = []
    original_spawn = asyncio.create_subprocess_exec
    async def tracked(*args, **kwargs):
        native.append(True)
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    try:
        initial = await started(controlled)
        await _barrier(entered)
        plan = plans[0]
        if mutation == "bootstrap":
            with (plan.package_root / "bootstrap.py").open("ab") as output:
                output.write(b"\n# changed synthetic fixture\n")
        elif mutation == "runtime":
            with (plan.package_root / "python/bin/python3.12").open("ab") as output:
                output.write(b"synthetic mutation")
        elif mutation == "workspace":
            plan.workspace.rename(plan.workspace.with_name("replaced-probe"))
            plan.workspace.mkdir(mode=0o700)
        else:
            runner.attempts[initial["job_id"]].owners[-1].reaper = plan.workspace / "unadmitted-helper"
        resume.set()
        final = await finished(runner, initial["job_id"])
        expected = "preparation_package_mutated" if mutation in {"bootstrap", "runtime"} else "preparation_probe_confinement_mismatch"
        assert final["state"] == "failed" and final["error_code"] == expected
        assert native == []
        _settled(controlled, final, attached=False)
    finally:
        resume.set()
        await runner.close()


@NATIVE_POLICY
async def test_started_managed_disable_preserves_existing_job_semantics(controlled, private_runtime, monkeypatch):
    runner = _real_verifier(controlled, private_runtime, monkeypatch)
    entered, resume = threading.Event(), threading.Event()
    original = ProbeConfinementV1.capture.__func__
    def held(cls, *args):
        plan = original(cls, *args)
        entered.set()
        assert resume.wait(10)
        return plan
    monkeypatch.setattr(ProbeConfinementV1, "capture", classmethod(held))
    try:
        initial = await started(controlled)
        await _barrier(entered)
        raw = controlled[4]
        disabled = controlled[2].disable(raw["recipe"]["id"], raw["recipe"]["version"])
        assert disabled["state"] == "disabled" and disabled["digest"] == raw["recipe"]["digest"]
        runner.store.check_authority(initial["job_id"])
        resume.set()
        final = await finished(runner, initial["job_id"])
        assert final["state"] == "succeeded"
        _settled(controlled, final, attached=True)
        assert controlled[2].inspect(raw["recipe"]["id"], raw["recipe"]["version"]) == disabled
    finally:
        resume.set()
        await runner.close()


@NATIVE_POLICY
async def test_joined_validation_never_recaptures_changed_locked_bytes(controlled, private_runtime, monkeypatch):
    runner = _real_verifier(controlled, private_runtime, monkeypatch)
    validations, native = [], []
    original = validate_package
    def validate_then_change(root):
        package = original(root)
        validations.append(package.digest)
        if len(validations) == 2:
            # This is after real full hashing inside the bound launch worker,
            # before its final metadata fence. The plan's critical code hashes
            # do not cover this notice; the complete artifact guard must do so.
            with (package.root / "LICENSE").open("ab") as output:
                output.write(b"changed synthetic notice\n")
        return package
    monkeypatch.setattr("meecp.preparation_verifier.validate_package", validate_then_change)
    original_spawn = asyncio.create_subprocess_exec
    async def tracked(*args, **kwargs):
        native.append(True)
        return await original_spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    try:
        initial = await started(controlled)
        final = await finished(runner, initial["job_id"])
        assert final["state"] == "failed" and final["error_code"] == "preparation_probe_confinement_mismatch"
        assert len(validations) == 2 and not native
        _settled(controlled, final, attached=False)
    finally:
        await runner.close()
