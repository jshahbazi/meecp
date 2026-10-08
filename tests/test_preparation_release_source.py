"""Source startup retains manual package operation without release authority."""
import json
from pathlib import Path

import pytest

from meecp import preparation_release_policy as policy
from meecp.preparation_contracts import PreparationError
from test_preparation_release_policy import closed, write_policy
from test_meecp_host import prepared, install, tool_call


def source_module(root, monkeypatch):
    path = root / "backend/meecp/preparation_release_policy.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# Synthetic module location only.\n")
    monkeypatch.setattr(policy, "__file__", str(path))
    return path


def active(root):
    value = closed()
    value["preparation"]["resolution_candidate_kinds"] = ["python-callable"]
    return write_policy(root, value)


@pytest.mark.parametrize("content", ["active", "malformed", "missing"])
def test_source_context_never_loads_distribution_authority(tmp_path, monkeypatch, content):
    root = tmp_path / "source"
    source_module(root, monkeypatch)
    if content == "active":
        active(root)
    elif content == "malformed":
        write_policy(root, closed()).write_bytes(b"{malformed")
    def forbidden(*args):
        pytest.fail("source context attempted distribution admission")
    monkeypatch.setattr(policy, "_load_policy", forbidden)
    monkeypatch.setenv("MCPME_RELEASE_POLICY", "/untrusted/policy")
    monkeypatch.setenv("MCPME_AGENT_EXECUTION", "true")
    assert policy.release_admissions() == (None, None, (None, None))


@pytest.mark.parametrize("layout", ["standard", "malformed", "linked-out", "linked-in"])
def test_enclosed_app_cannot_fall_back_to_source_context(tmp_path, monkeypatch, layout):
    outside = tmp_path / "source"
    inside = tmp_path / "Synthetic.app" / ("Contents/Resources/host" if layout != "malformed" else "wrong-layout")
    if layout == "linked-out":
        module = source_module(outside, monkeypatch)
        alias = inside / "backend/meecp/preparation_release_policy.py"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(module)
        monkeypatch.setattr(policy, "__file__", str(alias))
        root = outside
    elif layout == "linked-in":
        module = source_module(inside, monkeypatch)
        alias = outside / "backend/meecp/preparation_release_policy.py"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(module)
        monkeypatch.setattr(policy, "__file__", str(alias))
        root = inside
    else:
        source_module(inside, monkeypatch)
        root = inside
    active(root)
    with pytest.raises(PreparationError):
        policy.release_admissions()


@pytest.mark.parametrize("signing", ["ad_hoc", "developer_id", "unknown", None])
def test_receipt_signing_claim_cannot_downgrade_active_app_policy(tmp_path, monkeypatch, signing):
    root = tmp_path / "Synthetic.app/Contents/Resources/host"
    source_module(root, monkeypatch)
    active(root)
    (root / "build.json").write_text(json.dumps({"signing": signing}))
    with pytest.raises(PreparationError):
        policy.release_admissions()


def test_private_policy_seam_remains_strict_outside_app(tmp_path):
    active(tmp_path)
    with pytest.raises(PreparationError):
        policy._load_policy(tmp_path)


@pytest.mark.asyncio
async def test_actual_source_serve_keeps_manual_package_sdk_with_active_tracked_policy(tmp_path, monkeypatch, prepared):
    from meecp import supervisor
    source = tmp_path / "source"
    source_module(source, monkeypatch)
    active(source)
    package, reaper = prepared
    instances = []
    original = supervisor.Supervisor
    class ObservedSupervisor(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)
    class BoundedManagerServer:
        should_exit = False
        def __init__(self, config):
            self.config = config
        async def serve(self, *, sockets):
            host = instances[0]
            assert len(sockets) == 2
            assert host.public_admission is None
            assert host.commands.admission is None
            assert not host.preparation_runner.enabled
            assert not host.resolution_runner.enabled
            assert not host.agent_provider_registry.list()["live_execution_enabled"]
            slot = await install(host, package, "Manual source package")
            assert await tool_call(host, slot.id) == {"valid": True, "kind": "object"}
    monkeypatch.setattr(supervisor, "Supervisor", ObservedSupervisor)
    monkeypatch.setattr(supervisor, "HostServer", BoundedManagerServer)
    await supervisor.serve(tmp_path / "state", reaper)
    assert len(instances) == 1 and instances[0].closing
    assert not instances[0].workers
    assert all(not owner.instances for owner in instances[0].owners.values())
    assert not (tmp_path / "state/manager-connection.json").exists()
