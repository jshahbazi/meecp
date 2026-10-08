"""Live installed-profile discovery retains the original release authority.

The enclosing app and signature metadata are synthetic. No executable runs,
artifact is signed, provider is called or model is loaded by these controls.
"""
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from meecp.host_command import HostCommands
from meecp.preparation_admission import PublicPreparationAdmission
from meecp.preparation_contracts import PreparationError
from meecp import preparation_release_command_admission as release
from meecp.supervisor import capabilities, management_app
from test_preparation_release_command_admission import packaged, policy


LEGACY_PROFILES = ["mcp-stdio", "mcp-http", "model-service"]


def supported(caps, command=False):
    assert caps["package_schema_versions"] == ([1, 2] if command else [1])
    assert caps["profiles"] == LEGACY_PROFILES + (["host-command"] if command else [])


@pytest.fixture
def public_command(packaged):
    proof = packaged.capture()
    installed, _ = release.command_release_admission(policy(), packaged.host, artifact=proof)
    admission = PublicPreparationAdmission(frozenset({release.COMMAND_COMPONENT}),
        frozenset({frozenset({release.COMMAND_COMPONENT})}), artifact=proof)
    host = SimpleNamespace(closing=False, public_admission=admission,
        preparation_runner=None, resolution_runner=None)
    host.commands = release.ReleasedHostCommands(host, installed, proof)
    return host


def test_default_capabilities_keep_legacy_contract():
    supported(capabilities())


def test_live_command_support_does_not_require_automatic_recipe_execution(public_command):
    caps = capabilities(public_command)
    supported(caps, command=True)
    assert caps["automatic_preparation"] is False and caps["recipe_execution"] is False
    assert "application_directory" not in caps["import_sources"]
    assert caps["preparation_resolution"]["execution"] is False


@pytest.mark.parametrize("closed", ["host", "private", "inventory-only", "missing-installed",
    "no-artifact", "different-artifact", "family", "closure", "lookalike", "subclass"])
def test_closed_or_unbound_authority_never_advertises_schema_two(public_command, packaged, closed):
    host = public_command
    admission = host.public_admission
    if closed == "host": host.closing = True
    elif closed == "private": host.public_admission = None
    elif closed == "inventory-only": host.commands = HostCommands(host, host.commands.admission)
    elif closed == "missing-installed": host.commands.admission = None
    elif closed == "no-artifact": host.public_admission = replace(admission, artifact=None)
    elif closed == "different-artifact": host.public_admission = replace(admission, artifact=packaged.capture())
    elif closed == "family": host.public_admission = replace(admission, families=frozenset())
    elif closed == "closure": host.public_admission = replace(admission, recipe_component_sets=frozenset())
    elif closed == "lookalike": host.commands = SimpleNamespace(**vars(host.commands))
    else:
        class OtherCommands(release.ReleasedHostCommands):
            pass
        host.commands = OtherCommands(host, host.commands.admission, host.commands.artifact)
    supported(capabilities(host))


@pytest.mark.parametrize("member", ["policy", "seal", "build", "native", "helper", "resource-helper",
    "inventory", "source", "release-module", "same-byte-helper"])
def test_current_profile_discovery_denies_changed_held_authority(public_command, packaged, member):
    supported(capabilities(public_command), command=True)
    path = {
        "policy": public_command.commands.artifact.policy,
        "seal": packaged.app / "Contents/_CodeSignature/CodeResources",
        "build": packaged.host / "build.json",
        "native": packaged.app / "Contents/MacOS/meecp",
        "helper": packaged.helpers[0],
        "resource-helper": packaged.helpers[1],
        "inventory": packaged.host / "command-execution.json",
        "source": packaged.host / "backend/meecp/host_command.py",
        "release-module": packaged.host / "backend/meecp/preparation_release_command_admission.py",
        "same-byte-helper": packaged.helpers[0],
    }[member]
    if member == "same-byte-helper":
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o700)
        replacement.replace(path)
    else:
        path.write_bytes(path.read_bytes() + b"Changed synthetic metadata")
    supported(capabilities(public_command))


def test_unsupported_platform_keeps_schema_one_and_other_family_discovery(packaged, monkeypatch):
    proof = packaged.capture()
    monkeypatch.setattr(release, "installed_platform_policy", lambda: "a" * 64)
    with pytest.raises(PreparationError, match="^command_release_platform_unavailable$"):
        release.command_release_admission(policy(), packaged.host, artifact=proof)
    other = ("python-ordinary-v1", "1.0.0", "b" * 64)
    admission = PublicPreparationAdmission(frozenset({other}), frozenset({frozenset({other})}),
        frozenset({"python-callable"}), artifact=proof)
    host = SimpleNamespace(closing=False, public_admission=admission,
        preparation_runner=None, resolution_runner=None)
    host.commands = HostCommands(host, None)
    supported(capabilities(host))
    assert capabilities(host)["protocol_versions"] == ["2025-11-25"]


async def test_authenticated_api_returns_current_package_support(public_command, packaged):
    public_command.agent_preparations = None
    public_command.agent_provider_registry = None
    public_command.pi_setup = None
    app = management_app(public_command, "synthetic-capability-token", 19999)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:19999") as client:
        response = await client.get("/v1/capabilities",
            headers={"Authorization": "Bearer synthetic-capability-token"})
        assert response.status_code == 200
        supported(response.json(), command=True)
        packaged.helpers[0].write_bytes(b"Changed synthetic helper")
        response = await client.get("/v1/capabilities",
            headers={"Authorization": "Bearer synthetic-capability-token"})
        assert response.status_code == 200
        supported(response.json())
