"""Installed app controls use the authenticated management routes."""

from pathlib import Path
import uuid

import pytest

from meecp import __main__ as cli


@pytest.mark.parametrize("policy", ["manual", "host"])
async def test_configure_existing_app(monkeypatch, tmp_path: Path, policy: str):
    slot_id = str(uuid.uuid4())
    calls = []

    async def request(state, method, path, body):
        calls.append((state, method, path, body))
        return {"slot": {"slot_id": slot_id, "start_policy": policy}}

    monkeypatch.setattr(cli, "call", request)
    args = cli.parser().parse_args(["--state-dir", str(tmp_path), "slots", "configure", slot_id,
                                   "--start-policy", policy, "--json"])
    result = await cli.execute(args)
    assert result["slot"]["start_policy"] == policy
    assert calls == [(tmp_path, "PATCH", f"/v1/slots/{slot_id}", {"start_policy": policy})]


async def test_remove_returns_operation(monkeypatch, tmp_path: Path):
    slot_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())

    async def request(state, method, path, body):
        assert (state, method, path, body) == (tmp_path, "POST", f"/v1/slots/{slot_id}/remove", {})
        return {"operation_id": operation_id, "action": "remove"}

    monkeypatch.setattr(cli, "call", request)
    args = cli.parser().parse_args(["--state-dir", str(tmp_path), "slots", "remove", slot_id, "--json"])
    assert (await cli.execute(args))["operation_id"] == operation_id


@pytest.mark.parametrize("policy", ["first_call", "auto", ""])
def test_configure_rejects_unsupported_policy(policy):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["slots", "configure", str(uuid.uuid4()), "--start-policy", policy])


@pytest.mark.parametrize("action,method,payload", [
    ("configure", "PATCH", {"start_policy": "manual"}),
    ("remove", "POST", {}),
])
async def test_app_controls_preserve_explicit_scoped_credential(monkeypatch, tmp_path, action, method, payload):
    slot_id = str(uuid.uuid4())
    credential = tmp_path / "grant.json"
    async def request(state, actual_method, path, body, *, credential_file):
        route = f"/v1/slots/{slot_id}" + ("/remove" if action == "remove" else "")
        assert (state, actual_method, path, body, credential_file) == (tmp_path, method, route, payload, credential)
        return {"error": {"code": "grant_scope"}}
    monkeypatch.setattr(cli, "call", request)
    arguments = ["--state-dir", str(tmp_path), "--credential-file", str(credential), "slots", action, slot_id]
    if action == "configure":
        arguments.extend(["--start-policy", "manual"])
    result = await cli.execute(cli.parser().parse_args(arguments))
    assert result["error"]["code"] == "grant_scope"
