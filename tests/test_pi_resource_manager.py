"""Resource manager publication, display-cache races and owned shutdown."""
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("licenses", [4, "notice", {"path": "notice"}, [None]])
async def test_malformed_retention_arguments_return_tool_failure_without_filesystem_access(host, licenses):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid retention args must not read files")
    broker = SimpleNamespace(_arguments=PiToolBroker._arguments, _path=forbidden)
    result = await host.pi_setup._retain_resource({}, broker, {"path": "runtime", "name": "runtime", "licenses": licenses})
    assert result == {"success": False, "output": "invalid_arguments"}
