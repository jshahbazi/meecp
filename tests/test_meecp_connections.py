import asyncio
import uuid
from pathlib import Path

import pytest

from meecp.connections import connection_names, name_slug
from meecp import __main__ as cli
from meecp.supervisor import Supervisor


def test_name_slug_normalizes_user_app_names_and_bounds_length():
    assert name_slug(" Hot Dog / Or Not! ") == "hot-dog-or-not"
    assert len(name_slug("x" * 200)) == 64
    assert name_slug("你好") == ""
    slot_id = "2722cc9d-475a-402e-92fa-afc36e89b2d3"
    assert connection_names(((slot_id, "Hotdog Or Not Hotdog"),)) == {slot_id: "hotdog-or-not-hotdog"}


def test_connection_names_use_uuid_for_empty_reserved_and_duplicate_names():
    first = "2722cc9d-475a-402e-92fa-afc36e89b2d3"
    second = "2722cc9d-475a-402e-92fa-afc36e89b2d4"
    result = connection_names(((first, "Hotdog Or Not Hotdog"), (second, "hotdog-or-not-hotdog")))
    assert result[first].startswith("hotdog-or-not-hotdog-")
    assert result[second].startswith("hotdog-or-not-hotdog-")
    assert len(result[first]) <= 64
    assert len(result[second]) <= 64
    empty = "5a0b8e3e-1111-4222-8333-444444444444"
    reserved = "6b0b8e3e-1111-4222-8333-444444444444"
    result = connection_names(((empty, "你好"), (reserved, "meecp-management")))
    assert result[empty] == "app-" + empty
    assert result[reserved] == "meecp-management-" + reserved


def test_connection_names_disambiguate_engineered_generated_suffix_and_shared_prefixes():
    first = "12345678-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    second = "12345678-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    engineered = "87654321-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    names = ((first, "demo"), (second, "demo"), (engineered, "demo-" + first))
    result = connection_names(names)
    assert len(set(result.values())) == 3
    assert result[first] != result[second]
    assert result[engineered].endswith(engineered)
    assert connection_names(names) == connection_names(tuple(reversed(names)))


def test_supervisor_slot_snapshot_includes_unique_connection_name(tmp_path):
    supervisor = Supervisor(tmp_path / "state", tmp_path / "reaper")
    try:
        digest = "a" * 64
        first = supervisor.registry.add_slot(package_digest=digest, package_path=tmp_path,
                                              name="Hot Dog", public_port=19385,
                                              slot_id=str(uuid.uuid4()))
        second = supervisor.registry.add_slot(package_digest=digest, package_path=tmp_path,
                                               name="hot-dog", public_port=19386,
                                               slot_id=str(uuid.uuid4()))
        snapshots = [supervisor.slot_snapshot(slot) for slot in supervisor.registry.list_slots()]
        assert {item["connection_name"] for item in snapshots} == {"hot-dog-" + first.id, "hot-dog-" + second.id}
    finally:
        asyncio.run(supervisor.close())


@pytest.mark.asyncio
async def test_cli_connections_export_uses_snapshot_name_and_preserves_slot_args(monkeypatch, tmp_path):
    slot_id = str(uuid.uuid4())
    calls = []

    async def fake_call(state, method, path, body=None, **kwargs):
        calls.append(path)
        return {"slot_id": slot_id, "name": "Hotdog Or Not Hotdog", "connection_name": "hotdog-or-not-hotdog",
                "public_port": 19385}

    monkeypatch.setattr(cli, "call", fake_call)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "connections", "export",
                                                         "--slot", slot_id]))
    registration = result["configuration"]["mcpServers"]["hotdog-or-not-hotdog"]
    assert registration["args"] == ["--state-dir", str(tmp_path), "connect", "--slot", slot_id]
    assert calls == [f"/v1/slots/{slot_id}"]


@pytest.mark.asyncio
async def test_cli_connections_export_legacy_snapshot_resolves_collisions_and_propagates_errors(monkeypatch, tmp_path):
    slot_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    responses = {
        f"/v1/slots/{slot_id}": {"slot_id": slot_id, "name": "Hot Dog", "public_port": 19385},
        "/v1/slots": {"slots": [{"slot_id": slot_id, "name": "Hot Dog"},
                                  {"slot_id": other_id, "name": "hot-dog"}]},
    }

    async def fake_call(state, method, path, body=None, **kwargs):
        return responses[path]

    monkeypatch.setattr(cli, "call", fake_call)
    result = await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "connections", "export",
                                                         "--slot", slot_id]))
    assert result["connection_name"] == "hot-dog-" + slot_id

    async def failing_call(state, method, path, body=None, **kwargs):
        if path == f"/v1/slots/{slot_id}":
            return responses[path]
        return {"error": {"code": "synthetic_failure"}}

    monkeypatch.setattr(cli, "call", failing_call)
    assert (await cli.execute(cli.parser().parse_args(["--state-dir", str(tmp_path), "connections", "export",
                                                        "--slot", slot_id]))) == {"error": {"code": "synthetic_failure"}}
