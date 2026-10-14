"""Resource manager publication, display-cache races and owned shutdown."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from meecp.imports import ImportError as DraftError
from meecp.pi_resources import PiResourceError
from meecp.pi_setup import PiSetupError
from meecp.pi_tools import PiToolBroker
from meecp.private import read_private_json
from test_meecp_imports import source_folder
from test_pi_setup import configure, host as host


def request_for(host, tmp_path):
    draft = host.imports.create(source_folder(tmp_path), name="Resource fixture", intent="Expose synthetic validation")
    path = tmp_path.resolve() / "runtime.bin"
    path.write_bytes(b"public runtime fixture")
    selection = {"name": "runtime", "path": str(path), "role": "runtime-file"}
    payload = {"import_id": draft["draft_id"], "revision": draft["revision"], "resources": [selection]}
    return draft, selection, payload


def pause_capture(manager, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = manager.resources.capture
    def paused(*args, **kwargs):
        started.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(manager.resources, "capture", paused)
    return started, release, finished


async def test_duplicate_capture_coalesces_and_changes_revision_once(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, _, payload = request_for(host, tmp_path)
    started, release, _ = pause_capture(manager, monkeypatch)
    first = asyncio.create_task(manager.capture_resources(payload))
    assert await asyncio.to_thread(started.wait, 3)
    duplicate = asyncio.create_task(manager.capture_resources(payload))
    await asyncio.sleep(0)
    assert len(manager.resource_capture_tasks) == 1
    release.set()
    result, repeated = await asyncio.gather(first, duplicate)
    assert result == repeated and result["revision"] == draft["revision"] + 1
    assert len(result["resource_inventory"]["resources"]) == 1
    assert await manager.capture_resources(payload) == result
    assert host.imports.inspect(draft["draft_id"])["revision"] == 2


async def test_resource_capture_rejects_inactive_and_closed_before_reading_selection(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, _, payload = request_for(host, tmp_path)
    host.imports.cancel(draft["draft_id"], draft["revision"])
    monkeypatch.setattr(manager.resources, "estimate_capture", lambda *args: pytest.fail("inactive capture must not inspect selected files"))
    with pytest.raises(PiSetupError, match="^pi_source_changed$"):
        await manager.capture_resources(payload)
    assert not manager.probe_reservations
    manager.closed = True
    with pytest.raises(PiSetupError):
        await manager.capture_resources(payload)


async def test_inventory_never_caches_old_rows_with_new_catalog_stamp(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, selection, _ = request_for(host, tmp_path)
    original = manager.resources.list
    calls = 0
    def mutate_after_listing(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            manager.resources.capture(draft["draft_id"], 1, draft["source"]["revision"], selection,
                                      disk_ceiling_bytes=32 * 1024**2)
        return result
    monkeypatch.setattr(manager.resources, "list", mutate_after_listing)
    result = await manager.resource_inventory(draft["draft_id"], 1)
    assert calls == 2 and [item["name"] for item in result["resources"]] == ["runtime"]
    result["resources"].clear()
    again = await manager.resource_inventory(draft["draft_id"], 1)
    assert calls == 2 and len(again["resources"]) == 1


async def test_revision_commit_retains_inputs_when_rebind_fails_and_inventory_recovers(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, _, payload = request_for(host, tmp_path)
    original = manager.resources.rebind
    def failed(*args, **kwargs):
        raise PiResourceError("pi_resource_changed")
    monkeypatch.setattr(manager.resources, "rebind", failed)
    with pytest.raises(PiResourceError, match="^pi_resource_changed$"):
        await manager.capture_resources(payload)
    assert host.imports.inspect(draft["draft_id"])["revision"] == 2
    assert len(manager.resources.list(draft["draft_id"], 1, draft["source"]["revision"])["resources"]) == 1
    journal = read_private_json(manager.resource_changes_root / (draft["draft_id"] + ".json"))
    assert journal["state"] == "rebind_pending" and journal["revision"] == 2
    monkeypatch.setattr(manager.resources, "rebind", original)
    inventory = await manager.resource_inventory(draft["draft_id"], 2)
    assert inventory["binding"]["revision"] == 2 and len(inventory["resources"]) == 1
    assert read_private_json(manager.resource_changes_root / (draft["draft_id"] + ".json"))["state"] == "committed"
    assert not manager.resource_cancel_flags and not manager.probe_reservations


async def test_shutdown_signals_and_joins_resource_writer_before_releasing_charge(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    _, _, payload = request_for(host, tmp_path)
    started, release, finished = pause_capture(manager, monkeypatch)
    capture = asyncio.create_task(manager.capture_resources(payload))
    assert await asyncio.to_thread(started.wait, 3)
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0.02)
    assert not closing.done() and not finished.is_set() and manager.probe_reservations
    assert all(flag.is_set() for flag in manager.resource_cancel_flags.values())
    release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await capture
    assert finished.is_set() and not manager.probe_reservations and not manager.resource_capture_tasks
    assert not list(manager.resources.staging.iterdir())


async def test_resource_capture_blocks_new_setup_until_binding_publish(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, _, payload = request_for(host, tmp_path)
    configure(manager)
    monkeypatch.setattr(manager, "_runtime", lambda: (Path("/unused-node"), Path("/unused-worker")))
    started, release, _ = pause_capture(manager, monkeypatch)
    capture = asyncio.create_task(manager.capture_resources(payload))
    assert await asyncio.to_thread(started.wait, 3)
    start = asyncio.create_task(manager.start({"import_id": draft["draft_id"], "revision": 1}))
    await asyncio.sleep(0.02)
    assert not start.done()
    release.set()
    result = await capture
    assert result["revision"] == 2
    with pytest.raises(DraftError, match="^stale_import_revision$"):
        await start


async def test_capture_queue_is_bounded_while_matching_duplicates_still_join(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    _, selection, payload = request_for(host, tmp_path)
    started, release, _ = pause_capture(manager, monkeypatch)
    pending = [asyncio.create_task(manager.capture_resources(payload))]
    assert await asyncio.to_thread(started.wait, 3)
    for index in range(1, 4):
        other = {**payload, "resources": [{**selection, "name": "runtime-" + str(index)}]}
        pending.append(asyncio.create_task(manager.capture_resources(other)))
        await asyncio.sleep(0)
    assert len(manager.resource_capture_tasks) == 4
    with pytest.raises(PiSetupError, match="^pi_resource_blocked$"):
        await manager.capture_resources({**payload, "resources": [{**selection, "name": "overflow"}]})
    pending.append(asyncio.create_task(manager.capture_resources(payload)))
    await asyncio.sleep(0)
    assert len(manager.resource_capture_tasks) == 4
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    release.set()
    await closing
    await asyncio.gather(*pending, return_exceptions=True)
    assert not manager.resource_capture_tasks and not manager.probe_reservations
    assert not manager.resource_capture_cancellations


async def test_cancel_queued_capture_prevents_selected_file_reads_and_revision_change(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    first_base, second_base = tmp_path / "first", tmp_path / "second"
    first_base.mkdir()
    second_base.mkdir()
    _, _, first_payload = request_for(host, first_base)
    draft, selection, queued_payload = request_for(host, second_base)
    started, release, _ = pause_capture(manager, monkeypatch)
    estimate = manager.resources.estimate_capture

    def reject_queued_read(request):
        if request["path"] == selection["path"]:
            pytest.fail("canceled queued request inspected selected input")
        return estimate(request)

    monkeypatch.setattr(manager.resources, "estimate_capture", reject_queued_read)
    first = asyncio.create_task(manager.capture_resources(first_payload))
    assert await asyncio.to_thread(started.wait, 3)
    queued = asyncio.create_task(manager.capture_resources(queued_payload))
    await asyncio.sleep(0)
    assert not queued.done() and len(manager.resource_capture_tasks) == 2
    assert manager.cancel_resource_capture(draft["draft_id"]) == {"cancel_requested": True}
    release.set()
    assert (await first)["revision"] == 2
    with pytest.raises(PiSetupError, match="^pi_canceled$"):
        await queued
    assert host.imports.inspect(draft["draft_id"])["revision"] == 1
    assert manager.resources.list(draft["draft_id"], 1, draft["source"]["revision"])["resources"] == []
    assert not manager.resource_capture_cancellations and not manager.resource_cancel_flags
    assert not manager.resource_capture_tasks and not manager.probe_reservations


async def test_cancel_capture_during_previous_setup_cleanup_prevents_input_reads(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, _, payload = request_for(host, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    previous_id = str(uuid.uuid4())
    monkeypatch.setattr(manager, "matching_job", lambda *args: {"id": previous_id, "status": "running"})

    async def settle(job_id, *, reason):
        assert job_id == previous_id and reason == "resources_changed"
        started.set()
        await release.wait()

    monkeypatch.setattr(manager, "cancel", settle)
    monkeypatch.setattr(manager.resources, "estimate_capture", lambda *args: pytest.fail("canceled import read selected input"))
    capture = asyncio.create_task(manager.capture_resources(payload))
    await asyncio.wait_for(started.wait(), 3)
    assert manager.cancel_resource_capture(draft["draft_id"]) == {"cancel_requested": True}
    assert not capture.done()
    release.set()
    with pytest.raises(PiSetupError, match="^pi_canceled$"):
        await capture
    assert host.imports.inspect(draft["draft_id"])["revision"] == 1
    assert not manager.resource_capture_cancellations and not manager.resource_cancel_flags
    assert not manager.resource_capture_tasks and not manager.probe_reservations


async def test_selected_input_growth_cannot_exceed_its_admitted_disk_charge(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    draft, selection, payload = request_for(host, tmp_path)
    source = Path(selection["path"])
    estimate = manager.resources.estimate_capture

    def enlarge_after_estimate(request):
        result = estimate(request)
        with source.open("wb") as output:
            output.truncate(32 * 1024**2)
        return result

    monkeypatch.setattr(manager.resources, "estimate_capture", enlarge_after_estimate)
    monkeypatch.setattr("meecp.pi_resources.copy_regular_file", lambda *args, **kwargs: pytest.fail("unadmitted source growth reached copying"))
    with pytest.raises(PiResourceError, match="^pi_resource_disk_limit$"):
        await manager.capture_resources(payload)
    assert host.imports.inspect(draft["draft_id"])["revision"] == 1
    assert manager.resources.list(draft["draft_id"], 1, draft["source"]["revision"])["resources"] == []
    assert not list(manager.resources.staging.iterdir()) and not list(manager.resources.objects.iterdir())
    assert not manager.resource_capture_cancellations and not manager.resource_cancel_flags
    assert not manager.resource_capture_tasks and not manager.probe_reservations


@pytest.mark.parametrize("suffix", ["zip", "tar", "tgz", "gz", "xz", "bz2", "7z", "rar", "whl", "npz", "zst", "lz4", "NPZ"])
async def test_downloaded_opaque_archive_reuses_verified_bytes_without_network(host, tmp_path, suffix):
    manager = host.pi_setup
    draft, _, _ = request_for(host, tmp_path)
    record = {"id": str(uuid.uuid4()), "import_id": draft["draft_id"], "revision": 1,
              "source_revision": draft["source"]["revision"]}
    manager.cancel_flags[record["id"]] = threading.Event()
    broker = PiToolBroker(tmp_path / "download-work", host.reaper)
    content = b"PK synthetic opaque archive bytes"
    checksum = hashlib.sha256(content).hexdigest()
    url = "https://example.com/runtime." + suffix
    calls = []

    @asynccontextmanager
    async def stream(*args, **kwargs):
        calls.append(True)
        assert len(calls) == 1, "retained archive attempted network acquisition again"

        async def chunks():
            yield content

        yield chunks()

    broker.network.stream = stream
    try:
        args = {"url": url, "path": "runtime." + suffix, "sha256": checksum, "size": len(content)}
        first = await manager._fetch_url(record, broker, args)
        assert first["success"] and json.loads(first["output"])["retained"] is True
        (broker.workspace / args["path"]).unlink()
        second = await manager._fetch_url(record, broker, {**args, "path": "recovered/runtime." + suffix})
        assert second["success"] and json.loads(second["output"])["reused"] is True
        assert (broker.workspace / "recovered" / ("runtime." + suffix)).read_bytes() == content
        inventory = await manager.resource_inventory(draft["draft_id"], 1)
        assert inventory["resources"][0]["kind"] == "archive"
        assert len(calls) == 1 and not manager.probe_reservations
    finally:
        await broker.close()


@pytest.mark.parametrize("licenses", [4, "notice", {"path": "notice"}, [None]])
async def test_malformed_retention_arguments_return_tool_failure_without_filesystem_access(host, licenses):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid retention args must not read files")
    broker = SimpleNamespace(_arguments=PiToolBroker._arguments, _path=forbidden)
    result = await host.pi_setup._retain_resource({}, broker, {"path": "runtime", "name": "runtime", "licenses": licenses})
    assert result == {"success": False, "output": "invalid_arguments"}
