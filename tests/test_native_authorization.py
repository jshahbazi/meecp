"""Real private consent channel, scope fences and lost-handoff regression tests."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import httpx
import pytest

from meecp.authorization import scope_digest
from meecp.grants import GrantError, plan_digest
from meecp.private import read_private_json, write_private_json
from meecp.supervisor import Supervisor, management_app
from meecp_local_transport import socket_path
from test_meecp_host import prepared


async def frame(reader, writer, value):
    writer.write(json.dumps({"version": 1, **value}).encode() + b"\n")
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), 2))


async def channel(host):
    if host.authorization.server is None:
        await host.authorization.start("http://127.0.0.1:19384")
    return await asyncio.open_unix_connection(str(socket_path(host.root, "authorization")))


def decision(request, *, remember=False, selected=None, consent=False):
    return {"remember": remember, "account_scoped_consent": consent,
            "scopes": request["scopes"] if selected is None else selected,
            **{key: request[key] for key in ("scope_digest", "policy_revision", "generation")}}


def api(host, token="owner"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                            base_url="http://127.0.0.1:19384", headers={"Authorization": "Bearer " + token})


async def access(host, *, profile=None, remember=False, selected=None):
    reader, writer = await channel(host)
    request = await frame(reader, writer, {"type": "request_access", "profile_id": profile,
        "client": {"name": "Synthetic client", "version": "1"}, "scopes": ["inspect", "plan"]})
    if "handoff" not in request:
        host.authorization.approve(request["request_id"], decision(request, remember=remember, selected=selected, consent=remember))
        request = await frame(reader, writer, {"type": "status"})
    handoff = request["handoff"]
    acknowledged = await frame(reader, writer, {"type": "ack", "request_id": handoff["request_id"], "grant_id": handoff["grant_id"]})
    assert acknowledged["status"] == "approved" and "handoff" not in acknowledged
    return reader, writer, handoff


@pytest.fixture
def host(tmp_path, prepared):
    _, reaper = prepared
    return Supervisor(tmp_path.resolve() / "host", reaper)


@pytest.mark.asyncio
async def test_channel_is_private_and_credentialless_discovery_has_no_owner_token(host):
    try:
        reader, writer = await channel(host)
        discovery = read_private_json(host.root / "authorization-bootstrap.json")
        assert discovery["generation"] == host.authorization.generation
        assert discovery["installation_id"] == host.authorization.installation_id
        assert discovery["supervisor_pid"] == os.getpid()
        assert set(discovery) == {"schema_version", "socket_path", "installation_id", "generation", "supervisor_pid", "manager_endpoint"}
        assert Path(discovery["socket_path"]).stat().st_mode & 0o777 == 0o600
        assert Path(discovery["socket_path"]).parent.stat().st_mode & 0o777 == 0o700
        assert (await frame(reader, writer, {"type": "status"}))["status"] == "not_requested"
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_approval_handoff_requires_bound_ack_and_never_publishes_file(host):
    try:
        reader, writer = await channel(host)
        request = await frame(reader, writer, {"type": "request_access", "profile_id": None,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["plan", "inspect"]})
        assert request["scopes"] == ["inspect", "plan"]
        assert request["scope_digest"] == scope_digest(request["scopes"])
        async with api(host) as owner:
            response = await owner.post(f"/v1/authorization/requests/{request['request_id']}/approve", json=decision(request))
            assert response.status_code == 200 and "token" not in response.text and "credential_file" not in response.text
        handoff = (await frame(reader, writer, {"type": "status"}))["handoff"]
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(handoff["token"])
        other_reader, other_writer = await channel(host)
        replay = await frame(other_reader, other_writer, {"type": "ack", "request_id": request["request_id"], "grant_id": handoff["grant_id"]})
        assert replay["error"]["code"] == "authorization_request_stale"
        assert "handoff" not in (await frame(reader, writer, {"type": "status"}))
        settled = await frame(reader, writer, {"type": "ack", "request_id": request["request_id"], "grant_id": handoff["grant_id"]})
        assert settled["status"] == "approved"
        assert host.grants.authenticate(handoff["token"])["grant_id"] == handoff["grant_id"]
        assert not (host.grants.root / (handoff["grant_id"] + ".json")).exists()
        assert handoff["token"].encode() not in host.registry.path.read_bytes()
        row = host.registry._connection.execute("SELECT token_hash FROM management_grants WHERE grant_id=?", (handoff["grant_id"],)).fetchone()
        assert row[0] == hashlib.sha256(handoff["token"].encode()).hexdigest()
        duplicate = await api_post(host, f"/v1/authorization/requests/{request['request_id']}/approve", decision(request))
        assert duplicate.status_code == 200
        assert host.grants.list()["total"] == 1
        writer.close();other_writer.close()
    finally:
        await host.close()


async def api_post(host, route, payload, token="owner"):
    async with api(host, token) as client:
        return await client.post(route, json=payload)


@pytest.mark.asyncio
async def test_remembering_requires_explicit_account_consent_and_reconnects_fresh(host):
    try:
        profile = host.authorization.create_profile({"label": "Claude Code integration"})["profile"]
        reader, writer = await channel(host)
        request = await frame(reader, writer, {"type": "request_access", "profile_id": profile["profile_id"],
            "client": {"name": "Claude Code", "version": "1"}, "scopes": ["inspect", "plan"]})
        with pytest.raises(GrantError, match="authorization_remember_consent_required"):
            host.authorization.approve(request["request_id"], decision(request, remember=True))
        assert not host.authorization._profile(profile["profile_id"])["approved"]
        host.authorization.approve(request["request_id"], decision(request, remember=True, consent=True))
        first = (await frame(reader, writer, {"type": "status"}))["handoff"]
        await frame(reader, writer, {"type": "ack", "request_id": request["request_id"], "grant_id": first["grant_id"]})
        writer.close();await writer.wait_closed()
        await asyncio.sleep(0)
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(first["token"])
        _, second_writer, second = await access(host, profile=profile["profile_id"])
        assert first["token"] != second["token"]
        assert host.authorization._profile(profile["profile_id"])["identity_mode"] == "account_scoped_integration"
        assert "token" not in json.dumps(host.authorization.profiles)
        second_writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_remembered_policy_survives_host_restart_but_old_token_fails(tmp_path, prepared):
    _, reaper = prepared
    root = tmp_path.resolve() / "host"
    first = Supervisor(root, reaper)
    profile = first.authorization.create_profile({})["profile"]["profile_id"]
    _, writer, grant = await access(first, profile=profile, remember=True)
    await first.close()
    second = Supervisor(root, reaper)
    try:
        with pytest.raises(GrantError, match="grant_revoked"):
            second.grants.authenticate(grant["token"])
        _, next_writer, next_grant = await access(second, profile=profile)
        assert grant["token"] != next_grant["token"]
        writer.close();next_writer.close()
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_scope_reduction_and_owner_only_routes_and_pi_matrix(host, prepared):
    package, _ = prepared
    try:
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        reader, writer, base = await access(host, profile=profile, remember=True)
        async with api(host, base["token"]) as client:
            assert (await client.get("/v1/capabilities")).status_code == 200
            assert (await client.get("/v1/slots")).status_code == 200
            assert (await client.post("/v1/imports", json={"source": str(package), "name": "Synthetic"})).status_code == 200
            for method, route in pi_routes():
                response = await client.request(method, route, **({"json": {}} if method != "GET" else {}))
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            for route in ("/v1/authorization/profiles", "/v1/authorization/requests", "/v1/authorization/sessions", "/v1/grants"):
                assert (await client.get(route)).status_code == 403
        # Same profile, separately requested narrower consent changes revision.
        other_reader, other_writer = await channel(host)
        requested = await frame(other_reader, other_writer, {"type": "request_access", "profile_id": profile,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"]})
        # Existing ceiling autoapproves; revoke and request native consent again.
        host.authorization.owner_route("POST", f"/v1/authorization/profiles/{profile}/revoke", {})
        assert (await frame(reader, writer, {"type": "status"}))["status"] == "revoked"
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(base["token"])
        other_writer.close()
        _, narrow_writer, narrow = await access(host, profile=profile, remember=True, selected=["inspect"])
        async with api(host, narrow["token"]) as client:
            assert (await client.get("/v1/slots")).status_code == 200
            assert (await client.post("/v1/imports", json={"source": str(package), "name": "Synthetic"})).status_code == 403
        writer.close();narrow_writer.close()
    finally:
        await host.close()


def pi_routes():
    job = str(uuid.uuid4())
    return [("GET", "/v1/pi/settings"), ("PUT", "/v1/pi/settings"), ("GET", "/v1/pi/jobs"),
            ("POST", "/v1/pi/jobs"), ("POST", "/v1/pi/imports"), ("GET", f"/v1/pi/jobs/{job}"),
            ("POST", f"/v1/pi/jobs/{job}/answer"), ("POST", f"/v1/pi/jobs/{job}/cancel")]


@pytest.mark.asyncio
async def test_install_action_stays_exact_and_pi_owner_only(host, prepared):
    package, _ = prepared
    try:
        reader, writer, base = await access(host)
        plan = host.plan_install({"package": str(package)})
        request = await frame(reader, writer, {"type": "request_action", "kind": "install", "plan_id": plan["plan_id"],
                                               "plan_digest": plan["plan_digest"]})
        assert request["status"] == "pending" and request["kind"] == "install"
        assert "package_path" not in json.dumps(request["review"])
        host.authorization.approve(request["request_id"], decision(request))
        action = (await frame(reader, writer, {"type": "status"}))["handoff"]
        await frame(reader, writer, {"type": "ack", "request_id": action["request_id"], "grant_id": action["grant_id"]})
        host.grants.authorize_route(action["grant_id"], "POST", "/v1/install/apply", {"plan_id": plan["plan_id"]})
        saved = read_private_json(host.root / "plans" / (plan["plan_id"] + ".json"))
        host.grants.validate_apply(action["grant_id"], saved)
        with pytest.raises(GrantError, match="grant_plan_changed"):
            host.grants.validate_apply(action["grant_id"], {**saved, "name": "Changed"})
        with pytest.raises(GrantError, match="grant_scope"):
            host.grants.authorize_route(base["grant_id"], "POST", "/v1/install/apply", {"plan_id": plan["plan_id"]})
        async with api(host, action["token"]) as client:
            for method, route in pi_routes():
                assert (await client.request(method, route, **({"json": {}} if method != "GET" else {}))).status_code == 403
        writer.close();await writer.wait_closed();await asyncio.sleep(0)
        for token in (base["token"], action["token"]):
            with pytest.raises(GrantError, match="grant_revoked"):
                host.grants.authenticate(token)
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["ack", "disconnect", "deadline"])
async def test_lost_handoff_or_expired_request_cannot_leave_authority(host, lost):
    try:
        reader, writer = await channel(host)
        request = await frame(reader, writer, {"type": "request_access", "profile_id": None,
            "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"]})
        value = host.authorization.requests[request["request_id"]]
        if lost == "deadline":
            value["expires_at"] = time.time() - 1
            with pytest.raises(GrantError, match="authorization_request_stale"):
                host.authorization.approve(request["request_id"], decision(request))
            assert host.grants.list()["total"] == 0
        else:
            host.authorization.approve(request["request_id"], decision(request))
            handoff = (await frame(reader, writer, {"type": "status"}))["handoff"]
            if lost == "ack":
                value["handoff_expires_at"] = time.time() - 1
                assert (await frame(reader, writer, {"type": "status"}))["status"] == "expired"
            else:
                writer.close();await writer.wait_closed();await asyncio.sleep(0)
            with pytest.raises(GrantError, match="grant_revoked"):
                host.grants.authenticate(handoff["token"])
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_same_profile_concurrent_scope_decisions_are_revision_fenced(host):
    try:
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        channels = [await channel(host), await channel(host)]
        requests = []
        for index, (reader, writer) in enumerate(channels):
            requests.append(await frame(reader, writer, {"type": "request_access", "profile_id": profile,
                "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"] if index else ["inspect", "plan"]}))
        host.authorization.approve(requests[0]["request_id"], decision(requests[0], remember=True, consent=True))
        with pytest.raises(GrantError, match="authorization_policy_changed"):
            host.authorization.approve(requests[1]["request_id"], decision(requests[1], remember=True, consent=True))
        assert host.grants.list()["total"] == 1
        for _, writer in channels:
            writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_unknown_profile_and_invalid_frames_cannot_authorize(host):
    try:
        reader, writer = await channel(host)
        for value in ({"version": True, "type": "status"}, {"type": "status", "request_id": str(uuid.uuid4())},
                      {"type": "approve"}, {"type": "request_access", "profile_id": str(uuid.uuid4()),
                       "client": {"name": "Synthetic", "version": "1"}, "scopes": ["inspect"]}):
            result = await frame(reader, writer, value)
            assert result["status"] == "unavailable" and "handoff" not in result
        assert not host.authorization.list_requests()["requests"]
        assert host.grants.list()["total"] == 0
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_live_renewal_rotates_token_and_disconnect_revokes(host):
    try:
        profile = host.authorization.create_profile({})["profile"]["profile_id"]
        reader, writer, first = await access(host, profile=profile, remember=True)
        request = await frame(reader, writer, {"type": "renew"})
        second = request["handoff"]
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(second["token"])
        await frame(reader, writer, {"type": "ack", "request_id": second["request_id"], "grant_id": second["grant_id"]})
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(first["token"])
        assert host.grants.authenticate(second["token"])["grant_id"] == second["grant_id"]
        host.authorization.owner_route("POST", f"/v1/authorization/sessions/{second['grant_id']}/disconnect", {})
        with pytest.raises(GrantError, match="grant_revoked"):
            host.grants.authenticate(second["token"])
        writer.close()
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_pi_rejects_malformed_owner_plus_grant_principal(host, monkeypatch):
    try:
        monkeypatch.setattr(host.grants, "authenticate", lambda token: {"owner": True, "grant_id": str(uuid.uuid4())})
        async with api(host, "synthetic-malformed") as client:
            for method, route in pi_routes():
                assert (await client.request(method, route, **({"json": {}} if method != "GET" else {}))).status_code == 403
            assert (await client.get("/v1/authorization/requests")).status_code == 403
    finally:
        await host.close()
