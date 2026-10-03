"""Private owner CLI resolution stays closed and never selects owner fallback."""
import json
import uuid

import pytest

from mcpme import __main__ as cli
from mcpme.preparation_contracts import PreparationError
from test_mcpme_management_cli import execute, private_manager
from test_preparation_resolution import policy


@pytest.mark.parametrize("change", [{"wall_seconds": True}, {"network": ["example.com"]}, {"shell": "execute"}])
async def test_resolution_cli_rejects_unreviewable_policy_before_dispatch(tmp_path, monkeypatch, change):
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid resolution policy reached management")
    monkeypatch.setattr(cli, "call", unexpected)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**policy(), **change}))
    arguments = cli.parser().parse_args(["--state-dir", str(tmp_path), "resolutions", "plan",
        "--import", str(uuid.uuid4()), "--revision", "1", "--candidate", "a" * 64, "--policy", str(path)])
    with pytest.raises(PreparationError, match="invalid_resolution_policy"):
        await cli.execute(arguments)


async def test_resolution_cli_private_transport_owner_gate_and_scoped_denial(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def double(n: int) -> dict:\n return {'value': n*2}\n")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy()))
    async with private_manager(tmp_path) as host:
        draft = host.imports.create(source, name="Synthetic")
        inspection = await execute(host.root, "preparations", "inspect-source", "--import", draft["draft_id"], "--revision", "1")
        arguments = ("resolutions", "plan", "--import", draft["draft_id"], "--revision", "1",
            "--candidate", inspection["candidates"][0]["candidate_id"], "--policy", str(path))
        disabled = await execute(host.root, *arguments)
        assert disabled["error"]["code"] == "resolution_runner_unavailable"
        assert (await execute(host.root, "resolutions", "list"))["resolutions"] == []
        grant = await execute(host.root, "authorize", "--prepare")
        denied = await execute(host.root, "--credential-file", grant["credential_file"], *arguments)
        assert denied["error"]["code"] == "grant_scope"
        assert host.resolutions.list() == [] and not host.resolution_reservations
