"""Public driver authority/routing controls; synthetic metadata, no signing."""
import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_public_native_command")


@pytest.fixture
def release_input(module, tmp_path):
    app = tmp_path.resolve() / "Synthetic metadata.app"
    host = app / "Contents/Resources/host"
    policy = host / module.POLICY_PATH
    policy.parent.mkdir(parents=True)
    native = app / "Contents/MacOS/mcpme"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"Synthetic metadata; never executed.")
    native.chmod(0o700)
    component = dict(zip(("id", "version", "digest"), module.COMPONENT))
    value = {"schema_version": 1, "policy_id": "synthetic-active-command-v1", "command": {
        "component": list(module.COMPONENT), "installed_platform_policy": module.INSTALLED_PLATFORM,
        "resource_platform_policy": module.RESOURCE_PLATFORM}, "preparation": {
        "families": [component], "recipe_component_sets": [[component]], "resolution_candidate_kinds": []},
        "agent": {"components": [], "providers": [], "unlocked_resolution": False}}
    def write(): policy.write_text(json.dumps(value)); policy.chmod(0o600)
    write()
    fixture = tmp_path / "fixture"
    fixture.write_bytes(b"Synthetic fixture metadata; never executed.")
    fixture.chmod(0o700)
    return SimpleNamespace(app=app, host=host, native=native, fixture=fixture, policy=policy, value=value, write=write)


@pytest.mark.asyncio
async def test_every_public_start_and_restart_uses_ordinary_cli(module, release_input, monkeypatch):
    calls, report = [], {}
    async def ordinary(self, state, environment, home, *, enabled):
        calls.append((state, environment, home, enabled)); return "held child"
    async def forbidden(*args, **kwargs): pytest.fail("public path used private enabling start")
    monkeypatch.setattr(module.common.Harness, "start", ordinary)
    monkeypatch.setattr(module.command.CommandHarness, "start", forbidden)
    harness = module.PublicCommandHarness(release_input.app, release_input.host, release_input.native,
        {"safe_to_remove": True}, fixture=release_input.fixture, report=report)
    for enabled in (False, True, True):
        assert await harness.start("state", {"PATH": "/usr/bin:/bin"}, "home", enabled=enabled) == "held child"
    assert all(call[3] is False for call in calls) and report["public_cli_starts"] == 3


@pytest.mark.parametrize("fault", ["closed", "wrong-component", "wrong-platform", "missing-family", "missing-closure",
    "schema-boolean", "unknown", "duplicate", "missing"])
def test_public_driver_cannot_self_enable_missing_or_changed_release_policy(module, release_input, fault):
    value = release_input.value
    if fault == "closed": value["command"] = None
    elif fault == "wrong-component": value["command"]["component"][2] = "a" * 64
    elif fault == "wrong-platform": value["command"]["resource_platform_policy"] = "a" * 64
    elif fault == "missing-family": value["preparation"]["families"] = []
    elif fault == "missing-closure": value["preparation"]["recipe_component_sets"] = []
    elif fault == "schema-boolean": value["schema_version"] = True
    elif fault == "unknown": value["enable"] = True
    release_input.write()
    if fault == "duplicate": release_input.policy.write_text('{"schema_version":1,"schema_version":1}')
    if fault == "missing": release_input.policy.unlink()
    with pytest.raises(module.common.AcceptanceError): module.active_policy(release_input.host)
    assert release_input.value == value  # No policy mutation by the driver.


@pytest.mark.asyncio
async def test_closed_policy_denies_before_signature_or_host_start_and_preserves_inputs(module, release_input, monkeypatch):
    release_input.value["command"] = None; release_input.write()
    def forbidden(*args, **kwargs): pytest.fail("closed release inspected signature or started host")
    monkeypatch.setattr(module, "signature", forbidden)
    monkeypatch.setattr(module.common, "verify", forbidden)
    report = {"public_cli_starts": 0}
    with pytest.raises(module.common.AcceptanceError, match="^public_command_release_inactive$"):
        await module.verify(release_input.host, release_input.native, release_input.fixture, report)
    assert report["public_cli_starts"] == 0
    assert report["original_signed_app_preserved"] and report["original_fixture_preserved"]


@pytest.mark.parametrize("fault", ["same-byte-replacement", "changed-bytes", "changed-mode", "hardlink", "symlink"])
def test_complete_signed_input_snapshot_tracks_identity_and_rejects_unsafe_members(module, release_input, fault):
    before = module.snapshot(release_input.app)
    path = release_input.native
    if fault == "same-byte-replacement":
        other = path.with_name("replacement"); other.write_bytes(path.read_bytes()); other.chmod(0o700); other.replace(path)
    elif fault == "changed-bytes": path.write_bytes(b"Changed synthetic bytes")
    elif fault == "changed-mode": path.chmod(0o777)
    elif fault == "hardlink": path.with_name("link").hardlink_to(path)
    else: path.with_name("link").symlink_to(path)
    if fault in {"same-byte-replacement", "changed-bytes"}:
        assert module.snapshot(release_input.app) != before
    else:
        with pytest.raises(module.common.AcceptanceError): module.snapshot(release_input.app)


@pytest.mark.asyncio
async def test_private_fault_results_never_attest_public_fault_authority(module, release_input, monkeypatch):
    async def private(self, label):
        return {"passed": True, "ordinary_native_install_ownership_settled": True,
            "command_topology": {"verified": True, "deliberate_ancestor_group_loss": {
                "unknown_legacy_child_joined": False, "reconciliation_required": True}}}
    monkeypatch.setattr(module.command.CommandHarness, "case", private)
    harness = module.PublicCommandHarness(release_input.app, release_input.host, release_input.native,
        {"safe_to_remove": True}, fixture=release_input.fixture, report={})
    result = await harness.case("public")
    assert result["public_execution_authority"] == "shipped_signed_release_policy"
    assert result["public_cli_factory_or_admission_injection"] is False
    fault = result["additional_private_fault_controls"]
    assert fault["scope"] == "trusted_private_embedding" and fault["public_fault_authority_verified"] is False
    assert fault["controls"]["deliberate_ancestor_group_loss"]["unknown_legacy_child_joined"] is False
    assert "command_topology" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "capability", "closure", "form", "recipe", "review", "job"])
async def test_public_defaults_require_admitted_form_review_and_no_job(module, release_input, monkeypatch, failure):
    root = release_input.app.parent / "workspace"; root.mkdir(mode=0o700)
    report = {}
    harness = module.PublicCommandHarness(root, release_input.host, release_input.native,
        {"safe_to_remove": True}, fixture=release_input.fixture, report=report)
    calls, routes = [], []
    async def ordinary(*args, **kwargs):
        assert kwargs == {"enabled": False}; return "held"
    async def shutdown(*args): pass
    monkeypatch.setattr(module.common.Harness, "start", ordinary)
    monkeypatch.setattr(harness, "shutdown", shutdown)
    recipe = {"id": "mcpme.native-command", "version": "1.0.1", "digest": "a" * 64}
    async def cli(state, environment, home, *args):
        calls.append(args)
        if args[0] == "capabilities":
            return {"automatic_preparation": failure != "capability", "recipe_execution": True,
                "preparation_execution": {"families": [list(module.COMPONENT)],
                    "recipe_component_sets": [] if failure == "closure" else [[list(module.COMPONENT)]]}}
        if args[0] == "imports": return {"draft_id": "synthetic", "revision": 1, "source": {"kind": "application_directory"}}
        if args[0] == "recipes":
            return {"state": "disabled" if failure == "recipe" else "enabled", "revoked": False,
                "digest": recipe["digest"], "manifest": {"steps": [dict(zip(("component", "version", "digest"), module.COMPONENT))]}}
        if args[0] == "preparations": return {"preparations": [{"state": "queued"}] if failure == "job" else []}
        if args[0] == "resolutions": return {"resolutions": []}
        pytest.fail("public review used an execution command")
    async def manager(state, route, body):
        routes.append(route)
        if route.endswith("/form"):
            return {"execution_available": failure != "form", "resolution_required": False, "recipe": recipe,
                "candidate_id": "b" * 64, "form_digest": "c" * 64, "fields": [
                    {"id": "d" * 64, "label": "Synthetic test input", "kind": "object", "required": True}]}
        assert route.endswith("/plan-form") and body["answers"] == {"d" * 64: {"ok": True}}
        return {"execution_available": failure != "review", "blockers": [], "recipe": recipe,
            "plan_id": "synthetic", "plan_digest": "e" * 64}
    monkeypatch.setattr(harness, "cli", cli); monkeypatch.setattr(harness, "manager", manager)
    if failure is None:
        assert await harness.defaults() is True
        assert report["public_review"]["review_created_jobs"] is False
        assert routes == ["/v1/preparations/form", "/v1/preparations/plan-form"]
    else:
        with pytest.raises(module.common.AcceptanceError): await harness.defaults()
        assert "public_review" not in report
    assert not any("start" in call or "apply" in call for call in calls)


def test_review_choices_fail_closed_when_production_form_changes(module):
    form = {"fields": [{"id": "a" * 64, "label": "Confirm the documented command interface",
        "kind": "choice", "required": True, "choices": [{"id": "b" * 64}, {"id": "c" * 64}]}]}
    with pytest.raises(module.common.AcceptanceError): module.review_answers(form)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "same-build", "changed-helper", "inactive-upgrade", "unproved-upgrade"])
async def test_cross_build_requires_separate_active_signed_input_and_exact_helpers(module, release_input, monkeypatch, fault):
    first = release_input.app
    receipt = first / "Contents/Resources/app-build.json"
    receipt.write_bytes(b'{"synthetic_build":"one"}')
    (release_input.host / "bin").mkdir()
    for name in ("mcpme-command-reap", "mcpme-command-resource-reap"):
        helper = release_input.host / "bin" / name
        helper.write_bytes(b"Synthetic metadata: " + name.encode()); helper.chmod(0o700)
    second = first.with_name("Second synthetic metadata.app")
    shutil.copytree(first, second)
    if fault == "same-build": second = first
    else: (second / "Contents/Resources/app-build.json").write_bytes(b'{"synthetic_build":"two"}')
    if fault == "changed-helper": (second / "Contents/Resources/host/bin/mcpme-command-reap").write_bytes(b"Changed helper")
    if fault == "inactive-upgrade":
        value = dict(release_input.value, command=None)
        (second / "Contents/Resources/host" / module.POLICY_PATH).write_text(json.dumps(value))
    original_first, original_second = module.snapshot(first), module.snapshot(second)
    signature_calls, host_calls = [], []
    monkeypatch.setattr(module, "signature", lambda path, **options: signature_calls.append((path, options)))
    monkeypatch.setattr(module.common, "verify_host_payload", lambda host: {})
    async def verify(host, native, report, *, harness_factory, cases):
        host_calls.append(host)
        assert harness_factory.keywords["upgrade_app"] == second
        report.update(owned_children_joined=True, production_defaults_disabled=True,
            cases={"public_native_command": {"ordinary_native_install_ownership_settled": True,
                "cross_build_upgrade": {"verified": fault != "unproved-upgrade"}}})
    monkeypatch.setattr(module.common, "verify", verify)
    report = {}
    if fault is None:
        await module.verify(release_input.host, release_input.native, release_input.fixture, report, upgrade_app=second)
        assert report["cross_build_upgrade_verified"] is True and len(host_calls) == 1
        assert report["original_upgrade_signed_app_preserved"] is True
        assert len(signature_calls) == 6
    else:
        with pytest.raises(module.common.AcceptanceError):
            await module.verify(release_input.host, release_input.native, release_input.fixture, report, upgrade_app=second)
        assert report.get("cross_build_upgrade_verified", False) is False
        assert len(host_calls) == (1 if fault == "unproved-upgrade" else 0)
    assert module.snapshot(first) == original_first and module.snapshot(second) == original_second
