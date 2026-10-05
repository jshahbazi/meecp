"""The embedded command fixture must satisfy the signed CLI's owner check."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

from mcpme import __main__ as cli
from mcpme.private import read_private_json
from mcpme.supervisor import Supervisor
from mcpme_local_transport import private_transport, socket_path

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("command_fixture", ROOT / "scripts/verify_phase3_command_host.py")
fixture = module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


async def test_fixture_manager_satisfies_bundled_owner_check_and_keeps_authentication(tmp_path, monkeypatch):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    monkeypatch.setattr(cli, "bundled_app", lambda: tmp_path / "synthetic.app")
    try:
        with pytest.raises(ValueError, match="host_unavailable"):
            await cli.ensure_host(host.root)
        async with fixture.fixture_owner_manager(host):
            await cli.ensure_host(host.root)
            connection = read_private_json(host.root / "manager-connection.json")
            path = socket_path(host.root, "manager")
            async with httpx.AsyncClient(base_url=connection["endpoint"], trust_env=False,
                    transport=private_transport(path)) as client:
                assert (await client.get("/v1/capabilities")).status_code == 401
                assert (await client.get("/v1/capabilities", headers={
                    "Authorization": "Bearer " + connection["token"]})).status_code == 200
                assert (await client.get("/v1/capabilities", headers={
                    "Authorization": "Bearer " + connection["token"],
                    "Host": "untrusted.invalid"})).status_code != 200
        assert not (host.root / "manager-connection.json").exists()
        assert not path.exists()
        with pytest.raises(ValueError, match="host_unavailable"):
            await cli.ensure_host(host.root)
    finally:
        await host.close()


async def test_fixture_manager_preserves_an_existing_owner_connection(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    path = host.root / "manager-connection.json"
    original = b"existing owner connection\n"
    path.write_bytes(original)
    try:
        with pytest.raises(ValueError, match="command_host_verification_failed"):
            async with fixture.fixture_owner_manager(host):
                pytest.fail("existing owner connection was replaced")
        assert path.read_bytes() == original
    finally:
        path.unlink()
        await host.close()
