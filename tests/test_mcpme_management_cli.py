"""Scoped CLI credentials never recover local owner or invocation authority."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import uuid

import pytest
import uvicorn

from mcpme import __main__ as cli
from mcpme.grants import GrantError, read_credential
from mcpme.private import write_private_json
from mcpme.supervisor import Supervisor, bind_listener, management_app
from mcpme_local_transport import bind_private_listener, socket_path
from test_mcpme_packages import make_package


@asynccontextmanager
async def private_manager(tmp_path, reaper=None):
    host = Supervisor(tmp_path.resolve() / "state", reaper or tmp_path / "unused-reaper")
    tcp = bind_listener()
    private = bind_private_listener(host.root, "manager")
    port = tcp.getsockname()[1]
    owner = secrets.token_urlsafe(32)
    write_private_json(host.root / "manager-connection.json", {"endpoint": f"http://127.0.0.1:{port}", "token": owner})
    server = uvicorn.Server(uvicorn.Config(management_app(host, owner, port), log_config=None,
                                         access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[tcp, private]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield host
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        slot_ids = [slot.id for slot in host.registry.list_slots()]
        await host.close()
        tcp.close()
        private.close()
        private_path = socket_path(host.root, "manager")
        private_path.unlink(missing_ok=True)
        for slot_id in slot_ids:
            socket_path(host.root, slot_id).unlink(missing_ok=True)
        private_path.parent.rmdir()


async def execute(state, *arguments):
    return await cli.execute(cli.parser().parse_args(["--state-dir", str(state), *map(str, arguments)]))


async def test_owner_bootstrap_export_and_read_scope_use_only_file_reference(tmp_path):
    async with private_manager(tmp_path) as host:
        grant = await execute(host.root, "authorize", "--read-only", "--ttl-seconds", "60")
        credential = Path(grant["credential_file"])
        token = read_credential(credential)["token"]
        exported = await execute(host.root, "--credential-file", credential, "management", "export")
        registration = exported["configuration"]["mcpServers"]["mcpme-management"]
        assert registration["args"] == ["--state-dir", str(host.root), "--credential-file", str(credential), "management", "connect"]
        assert Path(registration["command"]).is_file()
        assert token not in json.dumps(exported)
        assert token not in json.dumps(await execute(host.root, "grants", "list"))
        assert token not in json.dumps(await execute(host.root, "grants", "inspect", grant["grant_id"]))
        scoped = ("--credential-file", credential)
        assert "error" not in await execute(host.root, *scoped, "capabilities")
        denied = await execute(host.root, *scoped, "authorize", "--prepare")
        assert denied["error"]["code"] == "grant_scope"
        assert len(host.grants.list()["grants"]) == 1
        await execute(host.root, "grants", "revoke", grant["grant_id"])
        with pytest.raises(GrantError, match="grant_credential_unavailable"):
            await execute(host.root, *scoped, "capabilities")


async def test_grant_file_is_reopened_and_unsafe_file_has_no_owner_fallback(tmp_path):
    async with private_manager(tmp_path) as host:
        grant = await execute(host.root, "authorize", "--read-only")
        credential = Path(grant["credential_file"])
        assert "error" not in await cli.call(host.root, "GET", "/v1/capabilities", credential_file=credential)
        credential.chmod(0o644)
        with pytest.raises(GrantError):
            await cli.call(host.root, "GET", "/v1/capabilities", credential_file=credential)
        credential.chmod(0o600)
        value = read_credential(credential)
        value["token"] = secrets.token_urlsafe(32)
        write_private_json(credential, value)
        rejected = await cli.call(host.root, "GET", "/v1/capabilities", credential_file=credential)
        assert rejected["error"]["code"] == "unauthorized"
        assert "error" not in await cli.call(host.root, "GET", "/v1/capabilities")


async def test_manager_responses_are_bounded_while_streaming(tmp_path, monkeypatch):
    async with private_manager(tmp_path) as host:
        grant = await execute(host.root, "authorize", "--read-only")
        monkeypatch.setattr("mcpme.supervisor.capabilities", lambda host=None: {"payload": "x" * (1024 * 1024 + 1)})
        with pytest.raises(ValueError, match="response_limit"):
            await cli.call(host.root, "GET", "/v1/capabilities", credential_file=Path(grant["credential_file"]))


async def test_exact_plan_authorize_and_inspection_use_fixed_expiry(tmp_path):
    package = make_package(tmp_path / "package")
    async with private_manager(tmp_path) as host:
        plan = await execute(host.root, "install", "plan", "--package", package)
        grant = await execute(host.root, "authorize", "--plan", plan["plan_id"])
        assert grant["mode"] == "install"
        assert grant["expires_at"] - grant["created_at"] == 900
        inspected = await execute(host.root, "--credential-file", grant["credential_file"], "install", "inspect", "--plan", plan["plan_id"])
        assert inspected["plan_id"] == plan["plan_id"]
        assert inspected["plan_digest"] == grant["plan_digest"]
        invalid = await execute(host.root, "authorize", "--plan", plan["plan_id"], "--ttl-seconds", "901")
        assert invalid["error"]["code"] == "invalid_grant_request"
        assert len(host.grants.list()["grants"]) == 1


@pytest.mark.parametrize("arguments", [
    ["management", "export"], ["management", "connect"],
])
async def test_management_requires_explicit_scoped_file(tmp_path, arguments):
    result = await execute(tmp_path, *arguments)
    assert result["error"]["code"] == "management_credential_required"


@pytest.mark.parametrize("arguments", [
    ["connect", "--slot", str(uuid.uuid4())],
    ["connections", "export", "--slot", str(uuid.uuid4())],
    ["serve", "--reaper", "/unused/reaper"],
])
async def test_management_credential_cannot_be_used_for_slot_connections_or_host_serve(tmp_path, arguments):
    result = await execute(tmp_path, "--credential-file", tmp_path / "missing", *arguments)
    assert result["error"]["code"] == "management_credential_not_invocation"


@pytest.mark.parametrize("arguments", [
    ["authorize", "--slot", str(uuid.uuid4())],
    ["authorize", "--read-only", "--action", "start"],
    ["authorize", "--prepare", "--action", "stop"],
])
async def test_bootstrap_rejects_ambiguous_authority_before_request(tmp_path, arguments):
    result = await execute(tmp_path, *arguments)
    assert result["error"]["code"] == "invalid_grant_request"
