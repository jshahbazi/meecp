"""Same SDK invocation supplies functional output and native resource proof."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from functools import partial
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

from meecp.command_resource_execution_v1 import _platform_policy
from meecp.command_resource_policy_v1 import ResourcePolicyV1
from meecp.preparation_command_owner import CommandPreparationResourceAdmission, recover_command_ownership
from meecp.preparation_command_verifier import verify_command_package
from meecp.preparation_contracts import PreparationError
from meecp.private import read_private_json, write_private_json
from test_host_command import ROOT, native, digest, pytestmark
from test_preparation_command_managed import intake


@pytest.fixture(scope="module")
def measured_admission(tmp_path_factory):
    helper = tmp_path_factory.mktemp("measured-command") / "helper"
    completed = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2",
        str(ROOT / "scripts/meecp-command-resource-reap.c"), "-o", str(helper)], capture_output=True, timeout=30)
    assert completed.returncode == 0
    return CommandPreparationResourceAdmission(helper, digest(helper), _platform_policy())


def measured_owner(tmp_path, native, measured_admission, mode="measured-tree"):
    imports, store, factory = intake(tmp_path, native, mode, create_owner=False)
    return imports, store, partial(factory, resource_admission=measured_admission)()


@pytest.mark.asyncio
async def test_real_sdk_forked_workload_has_positive_same_invocation_evidence(tmp_path, native, measured_admission):
    imports, store, owner = measured_owner(tmp_path, native, measured_admission)
    try:
        try:
            report = await verify_command_package(owner.package.root, owner.descriptor, None, owner.workspace, owner=owner)
        except Exception as failure:
            from meecp.command_resource_execution_v1 import read_resource_completion
            diagnostics = []
            for entry in owner.executor.journal["entries"]:
                valid, code, measurement = read_resource_completion(owner.executor.state, entry)
                diagnostics.append({"completion_valid": valid, "returncode": code,
                    "reason": measurement.reason if measurement is not None else None,
                    "valid_windows": measurement.sample_windows_valid if measurement is not None else None,
                    "samples": measurement.samples if measurement is not None else None})
            receipt = {"failure_type": type(failure).__name__, "measurements": diagnostics}
            (tmp_path / "positive-failure-diagnostic.json").write_text(json.dumps(receipt, sort_keys=True))
            pytest.fail(json.dumps(receipt, sort_keys=True), pytrace=False)
        assert report.peak_memory_bytes > 8 * 1024**2
        assert report.measurement_scope == "held-group-plus-helper-windowed-footprint-v1"
        evidence, = report.command_resource_evidence
        assert evidence["report"]["max_group_members"] == 3
        assert not evidence["report"]["hard_aggregate_enforced"]
        assert not evidence["report"]["lifetime_coverage_complete"]
        assert evidence["spec_digest"] == owner.intents["entries"][0]["spec"]
        assert recover_command_ownership(owner.workspace, owner.metadata)
        store.settle(owner.job_id)
        store.begin_verification_publication(owner.job_id)
        verified = store.record_verified(owner.job_id, owner.package, verification=report)
        assert verified["state"] == "ready_to_attach"
        assert verified["verification"]["measurement_scope"] == report.measurement_scope
        assert not store.pending_ownership(owner.job_id)
    finally:
        await owner.close()
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"footprint_bytes": 16 * 1024**2}, {"group_members": 1}])
async def test_actual_overload_fails_and_settles_before_reservation_release(tmp_path, native, measured_admission, change):
    admission = replace(measured_admission, policy=replace(ResourcePolicyV1(), **change))
    imports, store, owner = measured_owner(tmp_path, native, admission, "measured-overload")
    try:
        with pytest.raises(BaseExceptionGroup):
            await verify_command_package(owner.package.root, owner.descriptor, None, owner.workspace, owner=owner)
        assert not owner.resource_reports
        assert owner.confirm_completed()
        store.settle(owner.job_id)
        store.transition(owner.job_id, "failed", ownership_settled=True, error_code="preparation_fixture_failed")
        job = store.get(owner.job_id)
        assert not job["reservation_retained"]
        assert imports.inspect(job["binding"]["import_id"])["attachment"] is None
    finally:
        await owner.close()
        imports.close()


@pytest.mark.asyncio
async def test_measured_runner_attaches_installs_and_relocates_actual_directory(tmp_path, native, measured_admission, monkeypatch):
    from types import SimpleNamespace
    from meecp.host_command import HostCommandAdmission
    from meecp.preparation_runner import PreparationRunner
    imports, store, draft, descriptor = intake(tmp_path, native, "measured-tree", prepare_job=False)
    monkeypatch.setattr("meecp.preparation_runner.load_private_toolchain", lambda *args: SimpleNamespace(identity="d" * 64))
    monkeypatch.setattr("meecp.preparation_runner.dependency_identity", lambda *args: "e" * 64)
    runner = PreparationRunner(store, tmp_path / "host", native[0], enabled=True, enabled_families=tuple(store.families),
        command_admission=HostCommandAdmission(native[1], digest(native[1])), command_resource_admission=measured_admission)
    try:
        plan = await runner.plan(draft["draft_id"], draft["revision"], descriptor.model_dump(exclude_none=True))
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        await asyncio.wait_for(asyncio.shield(runner.tasks[job["job_id"]]), 10)
        final = store.get(job["job_id"])
        assert final["state"] == "succeeded", final["error_code"]
        assert final["ownership_settled"] and not final["reservation_retained"]
        attachment = imports.inspect(draft["draft_id"])["attachment"]
        assert attachment is not None
        relocated = tmp_path / "relocated-command"
        shutil.copytree(attachment["path"], relocated)
        await runner.close()
        imports.close()
        shutil.rmtree(imports.root)
        from meecp.supervisor import Supervisor
        from test_meecp_host import install
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        import httpx
        host = Supervisor(tmp_path / "installed", native[0],
            command_admission=HostCommandAdmission(native[1], digest(native[1])))
        try:
            slot = await install(host, relocated, "Measured command")
            assert host.package(slot).digest == attachment["digest"]
            shutil.rmtree(relocated)
            connection = host.connection(slot.id)
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
                async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("run", {"ok": True})
                        assert not result.isError and result.structuredContent == {"ok": True}
            params = StdioServerParameters(command=str(ROOT / "scripts/meecp"),
                args=["--state-dir", str(host.root), "connect", "--slot", slot.id])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools] == [
                        tool.mcp_schema() for tool in host.package(slot).manifest.tools]
                    result = await client.call_tool("run", {"ok": True})
                    assert not result.isError and result.structuredContent == {"ok": True}
            assert not host.commands.pending(slot.id)
        finally:
            await host.close()
    finally:
        await runner.close()
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["helper", "policy", "spec", "missing", "hard", "lifetime", "format"])
async def test_recovery_rejects_changed_same_invocation_proof(tmp_path, native, measured_admission, damage):
    imports, store, owner = measured_owner(tmp_path, native, measured_admission, "echo")
    try:
        handle = await owner.session(owner.package)
        await owner.invoke(handle, "run", {"ok": True}, 1)
        await owner.close()
        assert recover_command_ownership(owner.workspace, owner.metadata)
        path = owner.executor.journal_path
        journal = read_private_json(path)
        if damage in {"helper", "policy", "spec"}:
            key = {"helper": "helper_digest", "policy": "resource_policy_digest", "spec": "contract_digest"}[damage]
            journal["entries"][0][key] = "0" * 64
            write_private_json(path, journal)
        elif damage == "missing":
            path.unlink()
        else:
            receipt, = owner.executor.state.glob("*.complete")
            data = bytearray(receipt.read_bytes())
            offset = 365 + (14 if damage == "lifetime" else 15) * 8
            if damage == "format":
                data[:8] = b"MCPMECR3"
            else:
                data[offset:offset+8] = (1).to_bytes(8, "big")
            receipt.chmod(0o600)
            receipt.write_bytes(data)
            receipt.chmod(0o400)
        assert recover_command_ownership(owner.workspace, owner.metadata) is None
        assert not owner.confirm_completed() and store.pending_ownership(owner.job_id)
        with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
            store.settle(owner.job_id)
        assert store.get(owner.job_id)["reservation_retained"]
    finally:
        await owner.close()
        imports.close()


@pytest.mark.asyncio
async def test_measured_cancellation_holds_reservation_until_native_settlement(tmp_path, native, measured_admission):
    imports, store, owner = measured_owner(tmp_path, native, measured_admission, "hang")
    try:
        handle = await owner.session(owner.package)
        task = asyncio.create_task(owner.invoke(handle, "run", {"ok": True}, 1))
        async with asyncio.timeout(2):
            while not owner.executor._native:
                await asyncio.sleep(.001)
        store.cancel(owner.job_id)
        with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
            store.settle(owner.job_id)
        task.cancel()
        await asyncio.sleep(.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await owner.close()
        assert owner.confirm_completed()
        job = store.settle(owner.job_id)
        assert job["state"] == "canceled" and not job["reservation_retained"]
        assert not owner.resource_reports
    finally:
        await owner.close()
        imports.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["helper", "platform", "policy"])
async def test_changed_measured_admission_invalidates_plan_before_native_launch(tmp_path, native, measured_admission, monkeypatch, changed):
    from types import SimpleNamespace
    from meecp.host_command import HostCommandAdmission
    from meecp.preparation_runner import PreparationRunner
    imports, store, draft, descriptor = intake(tmp_path, native, "echo", prepare_job=False)
    monkeypatch.setattr("meecp.preparation_runner.load_private_toolchain", lambda *args: SimpleNamespace(identity="d" * 64))
    monkeypatch.setattr("meecp.preparation_runner.dependency_identity", lambda *args: "e" * 64)
    runner = PreparationRunner(store, tmp_path / "host", native[0], enabled=True, enabled_families=tuple(store.families),
        command_admission=HostCommandAdmission(native[1], digest(native[1])), command_resource_admission=measured_admission)
    launched = []
    try:
        plan = await runner.plan(draft["draft_id"], draft["revision"], descriptor.model_dump(exclude_none=True))
        changes = {"helper": {"helper_sha256": "0" * 64}, "platform": {"platform_policy": "0" * 64},
            "policy": {"policy": replace(measured_admission.policy, group_members=32)}}
        runner.command_resource_admission = replace(measured_admission, **changes[changed])
        def forbidden(*args, **kwargs):
            launched.append(True)
            raise AssertionError("unexpected_native_launch")
        monkeypatch.setattr("meecp.command_resource_execution_v1.subprocess.Popen", forbidden)
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        await asyncio.wait_for(asyncio.shield(runner.tasks[job["job_id"]]), 10)
        final = store.get(job["job_id"])
        assert final["state"] == "failed" and final["error_code"] == "preparation_implementation_changed"
        assert final["ownership_settled"] and not final["reservation_retained"]
        assert not launched and not store.command_ownership(job["job_id"])
    finally:
        await runner.close()
        imports.close()
