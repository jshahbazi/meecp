"""Installed app controls use the authenticated management routes."""

from pathlib import Path
import uuid

import pytest

from mcpme import __main__ as cli


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
