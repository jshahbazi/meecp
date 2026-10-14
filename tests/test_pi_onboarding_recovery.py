"""Fresh setup recovery and native authority using real private host state."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import threading
import uuid
from pathlib import Path

import pytest
from meecp.imports import ImportError as DraftError
from meecp.pi_setup import PiSetup, PiSetupError
from meecp.pi_tools import PiToolBroker, PiToolError
from meecp.preparation_capture import CapturePolicyV1
from meecp.processes import OwnershipLost, ProcessError, _await_ownership
from meecp.supervisor import HostError, Supervisor
from test_artifact_capture_api import body as capture_body
from test_meecp_imports import source_folder
from test_meecp_packages import edit_manifest, make_package
from test_pi_setup import _relock_probe_package, configure
from test_pi_setup import native_probe_server as _probe_fixture
from test_preparation_processes import group_reaper as _reaper_fixture


@pytest.fixture(scope="module")
def reaper(tmp_path_factory):
    return _reaper_fixture.__wrapped__(tmp_path_factory)


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    return _probe_fixture.__wrapped__(tmp_path_factory)


@pytest.fixture
async def host(tmp_path, monkeypatch, reaper):
    root = tmp_path / "host"
    for name in ("bin", "python/bin", "preparation-toolchain/bin", "pi-runtime/bin"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(reaper, root / "bin/meecp-reap")
    (root / "bin/meecp-reap").chmod(0o700)
    value = Supervisor(tmp_path.resolve() / "state", root / "bin/meecp-reap")
    configure(value.pi_setup)

    async def preflight(broker):
        # These manager tests never contend with the user's installed GPU app.
        return {
            "schema_version": 1,
            "metal_runtime": {"status": "unavailable"},
            "metal_compiler": {"status": "missing", "available": False, "required": False},
        }

    monkeypatch.setattr(PiToolBroker, "preflight", preflight)
    try:
        yield value
    finally:
        with contextlib.suppress(OwnershipLost):
            await value.pi_setup.close()
        value.imports.close()
        value.registry.close()
        os.close(value.lock_fd)


def enable(manager, monkeypatch, agent):
    monkeypatch.setattr(manager, "_runtime", lambda: (Path("/unused-model-worker"), Path("/unused-session")))
    monkeypatch.setattr(manager, "_run_agent", agent)


def draft(host, root):
    value = host.imports.create(source_folder(root), name="Synthetic tool", intent="Expose synthetic validation")
    return value, {"import_id": value["draft_id"], "revision": value["revision"]}


async def wait_stage(manager, job, stage):
    async with asyncio.timeout(5):
        while manager.get(job["id"])["stage"] != stage:
            task = manager.tasks.get(job["id"])
            assert task is None or not task.done(), "setup ended before expected stage"
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("reason,status", [("host_restart", "interrupted"), ("user_cancel", "canceled")])
async def test_unstarted_job_close_distinguishes_interruption_from_user_cancel(
    host, tmp_path, monkeypatch, reason, status
):
    manager = host.pi_setup
    calls = []

    async def agent(*args):
        calls.append(True)
        await asyncio.Event().wait()

    enable(manager, monkeypatch, agent)
    _, payload = draft(host, tmp_path)
    job = await manager.start(payload)
    if reason == "user_cancel":
        await manager.cancel(job["id"])
    await manager.close()
    completed = manager.get(job["id"])
    assert completed["status"] == status and completed["interruption_reason"] == reason
    assert calls == [] and manager.tasks[job["id"]].done()


async def test_restart_resume_reuses_verified_resources_files_and_opaque_credentials_without_session_replay(
    host, tmp_path, monkeypatch, probe
):
    manager = host.pi_setup
    original, payload = draft(host, tmp_path)
    resource = tmp_path / "public-weight.bin"
    resource.write_bytes(b"public synthetic model weights")
    license_file = tmp_path / "NOTICE"
    license_file.write_text("Synthetic redistribution notice")
    updated = await manager.capture_resources(
        {
            **payload,
            "resources": [
                {"name": "weights", "role": "model-weights", "path": str(resource), "licenses": [str(license_file)]}
            ],
        }
    )
    payload["revision"] = updated["revision"]
    first_started, answer_received = asyncio.Event(), asyncio.Event()
    calls = []

    async def first(record, workspace, broker, runtime, settings, key):
        calls.append(record["id"])
        (workspace / "adapter.py").write_text("public reusable adapter fixture\n")
        (workspace / ".pi-home").mkdir(mode=0o700)
        (workspace / ".pi-home/session-marker").write_text("synthetic old session fixture")
        first_started.set()
        reply = await manager._ask(
            record, {"kind": "secret", "credential_name": "APP_KEY", "question": "Synthetic fixture credential"}
        )
        assert "synthetic-app-secret" not in json.dumps(reply)
        answer_received.set()
        await asyncio.Event().wait()

    enable(manager, monkeypatch, first)
    old = await manager.start(payload)
    await asyncio.wait_for(first_started.wait(), 5)
    await wait_stage(manager, old, "waiting_for_user")
    question = manager.public_job(old["id"])["question"]
    manager.answer(old["id"], {"question_id": question["id"], **payload, "value": "synthetic-app-secret"})
    await asyncio.wait_for(answer_received.wait(), 5)
    references = manager.questions.credentials(old["id"])
    await manager.close()
    assert manager.get(old["id"])["status"] == "interrupted"
    host.pi_setup = fresh = PiSetup(host)
    resource.unlink()
    license_file.unlink()
    second_started, finish = asyncio.Event(), asyncio.Event()

    async def second(record, workspace, broker, runtime, settings, key):
        calls.append(record["id"])
        assert record["id"] != old["id"] and record["resumed_from"] == old["id"]
        assert (workspace / "adapter.py").read_text() == "public reusable adapter fixture\n"
        assert not (workspace / ".pi-home").exists()
        setup = json.loads((workspace / "setup.json").read_text())
        assert setup["credential_references"] == references
        assert "synthetic-app-secret" not in json.dumps(setup)
        inventory = json.loads((workspace / "resources.json").read_text())
        mounted = inventory["resources"][0]
        assert (workspace / mounted["file_path"]).read_bytes() == b"public synthetic model weights"
        assert fresh._resolve_credentials(record["id"], references) == {"APP_KEY": "synthetic-app-secret"}
        second_started.set()
        await finish.wait()
        package = make_package(workspace / "package")
        shutil.copyfile(probe, package / "bin/service")
        (package / "bin/service").chmod(0o700)
        edit_manifest(package, source_revision=record["source_revision"])
        (package / "verification.json").write_text(
            json.dumps([{"tool": "validate_json", "arguments": {}, "expected": {"valid": True}}])
        )
        _relock_probe_package(package)

    enable(fresh, monkeypatch, second)
    resumed, duplicate = await asyncio.gather(fresh.resume(old["id"], payload), fresh.resume(old["id"], payload))
    assert resumed["id"] == duplicate["id"]
    await asyncio.wait_for(second_started.wait(), 5)
    finish.set()
    await fresh.tasks[resumed["id"]]
    result = fresh.get(resumed["id"])
    assert result["status"] == "ready" and result["resource_count"] == 1
    assert len(calls) == 2 and len(set(calls)) == 2
    assert host.imports.inspect(original["draft_id"])["attachment"] is not None
    for path in fresh.jobs_root.glob("*.json"):
        content = path.read_text()
        assert "synthetic-app-secret" not in content and "synthetic old session fixture" not in content


async def test_matching_job_prefers_current_active_then_ready_over_newer_failed(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    started = asyncio.Event()

    async def agent(*args):
        started.set()
        await asyncio.Event().wait()

    enable(manager, monkeypatch, agent)
    selected, payload = draft(host, tmp_path)
    active = await manager.start(payload)
    await asyncio.wait_for(started.wait(), 5)
    newer = {**active, "id": str(uuid.uuid4()), "status": "failed", "created_at": active["created_at"] + 10}
    manager._update(newer)
    assert (
        manager.matching_job(selected["draft_id"], selected["revision"], selected["source"]["revision"])["id"]
        == active["id"]
    )
    await manager.cancel(active["id"])
    ready = {**active, "id": str(uuid.uuid4()), "status": "ready", "stage": "ready"}
    manager._update(ready)
    assert (
        manager.matching_job(selected["draft_id"], selected["revision"], selected["source"]["revision"])["id"]
        == ready["id"]
    )


@pytest.mark.parametrize("change", ["revision", "source", "ownership"])
async def test_resume_denies_stale_or_unknown_binding_before_a_new_session(host, tmp_path, monkeypatch, change):
    manager = host.pi_setup

    async def agent(*args):
        pytest.fail("stale recovery reached a fresh model session")

    enable(manager, monkeypatch, agent)
    _, payload = draft(host, tmp_path)
    record = {
        "id": str(uuid.uuid4()),
        **payload,
        "source_revision": host.imports.inspect(payload["import_id"])["source"]["revision"],
        "status": "interrupted",
        "stage": "thinking",
        "created_at": 1,
        "code": "pi_interrupted",
        "package": None,
    }
    manager._update(record)
    if change == "revision":
        host.imports.revise(payload["import_id"], payload["revision"], intent="Changed synthetic tool")
    elif change == "source":
        manager._update(record, source_revision="sha256:" + "b" * 64)
    else:
        manager._update(record, code="pi_ownership_lost")
    with pytest.raises((PiSetupError, DraftError)):
        await manager.resume(record["id"], payload)
    assert not manager.tasks and len(manager.list()["jobs"]) == 1


def record_for(manager, selected):
    record = {
        "id": str(uuid.uuid4()),
        "import_id": selected["draft_id"],
        "revision": selected["revision"],
        "source_revision": selected["source"]["revision"],
        "status": "running",
        "stage": "thinking",
        "created_at": 1,
        "code": None,
        "package": None,
    }
    manager._update(record)
    return record


async def test_native_admission_is_exclusive_and_held_through_canceled_cleanup(host, tmp_path):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)
    running, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Broker:
        native_ownership_settled = False

        async def native_run_impl(self, args):
            self.native_ownership_settled = False
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await _await_ownership(release.wait())
                self.native_ownership_settled = True

    broker = Broker()
    args = {"argv": ["/bin/true"], "memory_mb": 256}
    task = asyncio.create_task(manager._native_command(record, broker, args))
    await asyncio.wait_for(running.wait(), 3)
    key = record["id"] + "-native"
    assert manager.probe_reservations[key]["gpu"]
    with pytest.raises(PiToolError, match="^native_resource_blocked$"):
        await manager._native_command(record_for(manager, selected), Broker(), args)
    task.cancel()
    await asyncio.wait_for(cleanup.wait(), 3)
    task.cancel()
    assert key in manager.probe_reservations and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert key not in manager.probe_reservations and broker.native_ownership_settled


async def test_native_reap_uncertainty_retains_charge_and_closes_admission(host, tmp_path):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)

    class Broker:
        native_ownership_settled = False

        async def native_run_impl(self, args):
            raise ProcessError("reap_timeout")

    with pytest.raises(OwnershipLost):
        await manager._native_command(record, Broker(), {"argv": ["/bin/true"], "memory_mb": 256})
    assert manager.closed and manager.probe_reservations[record["id"] + "-native"]["gpu"]


async def test_system_action_continues_same_tool_after_recheck_without_journaling_conversation(host, tmp_path):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)

    class Manager:
        async def ensure(self, args):
            assert args == {"required": False}
            return {"available": True}

    class Broker:
        toolchain_manager = Manager()

        async def invoke(self, name, args):
            assert name == "ensure_metal_toolchain" and args == {"required": True}
            return {"success": True, "output": "ready"}

    initial = {
        "success": False,
        "output": json.dumps(
            {
                "status": "action_required",
                "action_required": {"kind": "open_xcode", "application_path": "/Applications/Xcode.app"},
            }
        ),
    }
    task = asyncio.create_task(
        manager._continue_system(record, Broker(), "ensure_metal_toolchain", {"required": True}, initial)
    )
    async with asyncio.timeout(3):
        while manager.get(record["id"])["stage"] != "waiting_for_system":
            await asyncio.sleep(0.01)
    manager.continue_system(record["id"])
    assert await task == {"success": True, "output": "ready"}
    assert manager.get(record["id"])["system_action"] is None
    assert record["id"] not in manager.system_events


async def test_capture_disk_admission_waits_for_owned_pi_resource_work(host, tmp_path, monkeypatch):
    payload = capture_body(tmp_path / "selected-capture")
    policy = CapturePolicyV1.model_validate(payload["policy"])
    # Capture alone fits; adding the in-flight Pi acquisition charge does not.
    held_bytes = policy.disk_ceiling_bytes
    available = policy.disk_ceiling_bytes * 3 // 2 + 256 * 1024**2
    real_usage = shutil.disk_usage(host.root)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type(real_usage)(available * 2, available, available))
    started, release = asyncio.Event(), asyncio.Event()

    async def resource_work(ceiling):
        started.set()
        await release.wait()
        return "settled"

    task = asyncio.create_task(host.pi_setup._resource_work(resource_work, held_bytes))
    try:
        await asyncio.wait_for(started.wait(), 3)
        queued = host.captures.begin(
            payload["request"],
            payload["policy"],
            payload["target"],
            payload["idempotency_key"],
            host.validate_capture_target,
        )
        with pytest.raises(HostError, match="^resource_blocked$"):
            await host.reserve_capture(queued["capture_id"], policy)
        assert any(item.get("disk_bytes") == held_bytes for item in host.pi_setup.probe_reservations.values())
        release.set()
        assert await task == "settled" and not host.pi_setup.probe_reservations
        await host.reserve_capture(queued["capture_id"], policy)
        assert host.capture_reservations[queued["capture_id"]]["disk_bytes"] == policy.disk_ceiling_bytes
    finally:
        release.set()
        await _await_ownership(task)


async def test_native_waits_for_gpu_and_holds_its_own_charge_until_completion(host, tmp_path):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)
    host.reservations["occupied-gpu"] = {"memory_mb": 256, "gpu": True}
    launched, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Broker:
        native_ownership_settled = False

        async def native_run_impl(self, args):
            calls.append(args)
            assert "occupied-gpu" not in host.reservations
            assert manager.probe_reservations[record["id"] + "-native"]["gpu"]
            launched.set()
            await release.wait()
            self.native_ownership_settled = True
            return {"success": True, "output": "completed"}

    task = asyncio.create_task(
        manager._native_command(record, Broker(), {"argv": ["/bin/true"], "memory_mb": 256}, wait_for_resources=True)
    )
    try:
        await wait_stage(manager, record, "waiting_for_resources")
        assert not calls and not manager.probe_reservations
        async with host.resource_lock:
            host.reservations.pop("occupied-gpu")
        await asyncio.wait_for(launched.wait(), 3)
        assert not task.done() and record["id"] + "-native" in manager.probe_reservations
        release.set()
        assert await task == {"success": True, "output": "completed"}
        assert len(calls) == 1 and not manager.probe_reservations
    finally:
        release.set()
        host.reservations.pop("occupied-gpu", None)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_while_waiting_for_gpu_never_executes_or_reserves(host, tmp_path):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)
    host.reservations["occupied-gpu"] = {"memory_mb": 256, "gpu": True}

    class Broker:
        async def native_run_impl(self, args):
            pytest.fail("canceled resource wait executed native code")

    task = asyncio.create_task(
        manager._native_command(record, Broker(), {"argv": ["/bin/true"], "memory_mb": 256}, wait_for_resources=True)
    )
    await wait_stage(manager, record, "waiting_for_resources")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.probe_reservations and not manager.closed
    assert host.reservations == {"occupied-gpu": {"memory_mb": 256, "gpu": True}}
    host.reservations.pop("occupied-gpu")


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({}, "invalid_arguments"),
        ({"url": "http://example.com/file", "path": "file"}, "invalid_download_url"),
        ({"url": "https://example.com/file", "path": "file", "size": True}, "invalid_download_size"),
        ({"url": "https://example.com/file", "path": "file", "sha256": "not-a-hash"}, "invalid_download_spec"),
    ],
)
async def test_invalid_host_fetch_returns_typed_tool_error_and_preserves_job(host, tmp_path, arguments, code):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)
    broker = PiToolBroker(tmp_path / "work", host.reaper)
    try:
        result = await manager._fetch_url(record, broker, arguments)
        assert result == {"success": False, "output": code}
        assert manager.get(record["id"])["status"] == "running" and not manager.probe_reservations
    finally:
        await broker.close()


async def test_retained_download_restores_verified_bytes_without_network_and_releases_disk_charge(host, tmp_path):
    manager = host.pi_setup
    selected, _ = draft(host, tmp_path)
    record = record_for(manager, selected)
    manager.cancel_flags[record["id"]] = threading.Event()
    content = b"public retained download fixture"
    original = tmp_path / "download.bin"
    original.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    manager.resources.capture(
        selected["draft_id"],
        selected["revision"],
        selected["source"]["revision"],
        {
            "name": "retained-download",
            "path": str(original),
            "role": "runtime-file",
            "origin": {"kind": "https", "url": "https://example.com/download.bin"},
            "expected_sha256": checksum,
            "expected_size": len(content),
        },
        disk_ceiling_bytes=64 * 1024**2,
    )
    original.unlink()
    broker = PiToolBroker(tmp_path / "work", host.reaper)
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("verified retained download attempted network")

    broker.invoke = forbidden
    try:
        result = await manager._fetch_url(
            record,
            broker,
            {
                "url": "https://example.com/download.bin",
                "path": "restored/download.bin",
                "sha256": checksum,
                "size": len(content),
            },
        )
        assert result["success"] and json.loads(result["output"])["reused"] is True
        assert (broker.workspace / "restored/download.bin").read_bytes() == content
        assert not calls and not manager.probe_reservations
        assert not list((broker.workspace / "restored").glob(".pi-tool-*"))
    finally:
        await broker.close()
