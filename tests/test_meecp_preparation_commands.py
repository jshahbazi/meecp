"""Preparation commands preserve explicit reviewed owner intent."""
from __future__ import annotations

import json
import uuid

import pytest

from meecp import __main__ as cli
from meecp.preparation_contracts import PreparationError
from test_meecp_management_cli import execute, private_manager


async def test_cli_static_inspection_policy_plan_and_start_keep_exact_body(tmp_path, monkeypatch):
    calls = []
    async def api(state, method, path, body=None, **options):
        calls.append((state, method, path, body, options))
        return {"accepted": True}
    monkeypatch.setattr(cli, "call", api)
    import_id, plan_id, key = (str(uuid.uuid4()) for _ in range(3))
    descriptor = tmp_path / "descriptor.json"
    policy = tmp_path / "policy.json"
    descriptor.write_text(json.dumps({"synthetic": "descriptor"}))
    policy.write_text(json.dumps({"synthetic": "policy"}))
    base = ["--state-dir", str(tmp_path)]
    async def run(*arguments):
        return await cli.execute(cli.parser().parse_args([*base, *map(str, arguments)]))
    await run("preparations", "inspect-source", "--import", import_id, "--revision", "2")
    await run("preparations", "plan", "--import", import_id, "--revision", "2", "--descriptor", descriptor)
    await run("preparations", "plan", "--import", import_id, "--revision", "2", "--descriptor", descriptor, "--policy", policy)
    await run("preparations", "start", "--plan", plan_id, "--plan-digest", "a" * 64, "--idempotency-key", key)
    await run("preparations", "recover", "--job-id", import_id)
    assert [call[2] for call in calls] == ["/v1/preparations/inspect-source", "/v1/preparations/plan",
                                         "/v1/preparations/plan", "/v1/preparations/start",
                                         "/v1/preparations/" + import_id + "/recover"]
    assert calls[0][3] == {"import_id": import_id, "revision": 2}
    assert calls[1][3] == {"import_id": import_id, "revision": 2, "descriptor": {"synthetic": "descriptor"}}
    assert calls[2][3] == {**calls[1][3], "execution_policy": {"synthetic": "policy"}}
    assert calls[3][3] == {"plan_id": plan_id, "plan_digest": "a" * 64, "idempotency_key": key}
    assert calls[4][1] == "POST" and calls[4][3] == {}


async def test_inspection_revision_and_policy_file_are_validated_before_dispatch(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid local input reached management")
    monkeypatch.setattr(cli, "call", unexpected)
    base = ["--state-dir", str(tmp_path), "preparations"]
    with pytest.raises(PreparationError, match="invalid_import_revision"):
        await cli.execute(cli.parser().parse_args([*base, "inspect-source", "--import", str(uuid.uuid4()), "--revision", "0"]))
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}")
    bad_policy = tmp_path / "policy.json"
    bad_policy.write_text("[]")
    with pytest.raises(PreparationError):
        await cli.execute(cli.parser().parse_args([*base, "plan", "--import", str(uuid.uuid4()), "--revision", "1",
                                                  "--descriptor", str(descriptor), "--policy", str(bad_policy)]))


@pytest.mark.parametrize("flag,value", [("--plan", "../escape"), ("--plan-digest", "A" * 64),
                                       ("--idempotency-key", "not-a-uuid")])
def test_start_requires_canonical_owner_identities(flag, value):
    arguments = {"--plan": str(uuid.uuid4()), "--plan-digest": "a" * 64, "--idempotency-key": str(uuid.uuid4())}
    arguments[flag] = value
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["preparations", "start", *[part for pair in arguments.items() for part in pair]])


async def test_cli_owner_uses_private_transport_and_scoped_credentials_cannot_start(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("raise RuntimeError('inspection executed source')\ndef calculate(value: str) -> dict:\n    return {}\n")
    async with private_manager(tmp_path) as host:
        draft = host.imports.create(source, name="Synthetic")
        inspected = await execute(host.root, "preparations", "inspect-source", "--import", draft["draft_id"], "--revision", "1")
        assert inspected["source_revision"] == draft["source"]["revision"]
        arguments = ("preparations", "start", "--plan", str(uuid.uuid4()), "--plan-digest", "a" * 64,
                     "--idempotency-key", str(uuid.uuid4()))
        disabled = await execute(host.root, *arguments)
        assert disabled["error"]["code"] == "preparation_runner_unavailable"
        grant = await execute(host.root, "authorize", "--prepare")
        denied = await execute(host.root, "--credential-file", grant["credential_file"], *arguments)
        assert denied["error"]["code"] == "grant_scope"
        assert not host.preparations.list()
