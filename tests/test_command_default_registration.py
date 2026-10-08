"""Shipped CLI questions never supply execution admission."""
from __future__ import annotations

import hashlib

import pytest

from meecp.command_execution_v2 import CommandExecutionError
from meecp.preparation_command import COMPONENT_ID, RECIPE_VERSION
from meecp import preparation_command_registration as registration
from meecp.preparation_contracts import canonical_bytes
from meecp.supervisor import Supervisor
from test_preparation_command_source import image


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(registration, "_platform_eligible", lambda: True)
    root = tmp_path.resolve() / "packaged"
    (root / "bin").mkdir(parents=True)
    helper = root / "bin/mcpme-command-reap"
    helper.write_bytes(b"Static inventory fixture; never executed.\n")
    helper.chmod(0o700)
    reaper = root / "bin/meecp-reap"
    reaper.write_bytes(b"Static owner fixture; never executed.\n")
    reaper.chmod(0o700)
    inventory = {"schema_version": 1, "package_schema": 2, "profile": "host-command",
        "component": "macos26-arm64-command-v3", "receipt_format": 3,
        "helper": "bin/mcpme-command-reap", "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "helper_source_sha256": "a" * 64, "platform": {"os": "macos", "major": 26, "architecture": "arm64"},
        "production_admission": "disabled", "configuration_authority": "inventory-only",
        "open_gates": ["aggregate-process-memory-enforcement", "signed-relocated-native-app"]}
    (root / "command-execution.json").write_bytes(canonical_bytes(inventory))
    return root, reaper, inventory


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("receipt_format", 4), ("profile", "mcp-stdio"),
    ("helper", "../outside"), ("helper_sha256", "f" * 64),
    ("helper_source_sha256", "invalid"), ("production_admission", "enabled"),
    ("configuration_authority", "runtime-admission"), ("unknown", True),
    ("platform", {"os": "macos", "major": 26.0, "architecture": "arm64"}),
])
def test_missing_or_changed_inventory_never_registers_provider(packaged, field, value):
    root, _, inventory = packaged
    inventory[field] = value
    (root / "command-execution.json").write_bytes(canonical_bytes(inventory))
    assert registration.shipped_command_family(root) is None


@pytest.mark.parametrize("mutation", ["inventory-missing", "helper-missing", "helper-not-executable",
                                     "helper-link", "helper-hardlink", "unsupported-platform"])
def test_unbundled_unsafe_or_unsupported_helpers_are_omitted(packaged, mutation, monkeypatch):
    root, _, _ = packaged
    helper = root / "bin/mcpme-command-reap"
    if mutation == "inventory-missing":
        (root / "command-execution.json").unlink()
    elif mutation == "helper-missing":
        helper.unlink()
    elif mutation == "helper-not-executable":
        helper.chmod(0o600)
    elif mutation == "helper-link":
        other = root / "bin/other"
        helper.rename(other)
        helper.symlink_to(other)
    elif mutation == "helper-hardlink":
        (root / "bin/other").hardlink_to(helper)
    else:
        monkeypatch.setattr(registration, "_platform_eligible", lambda: False)
    assert registration.shipped_command_family(root) is None


async def test_cold_packaged_host_questions_resolve_without_source_execution(tmp_path, packaged, monkeypatch):
    root, reaper, inventory = packaged
    from meecp.processes import ProcessOwner
    async def no_launch(*args, **kwargs):
        pytest.fail("static registration or questions launched source")
    monkeypatch.setattr(ProcessOwner, "launch", no_launch)
    source = tmp_path.resolve() / "selected"
    source.mkdir()
    (source / "native").write_bytes(image())
    (source / "native").chmod(0o700)
    (source / "run.input.schema.json").write_bytes(canonical_bytes(SCHEMA))
    (source / "run.output.schema.json").write_bytes(canonical_bytes(SCHEMA))
    (source / "LICENSE").write_text("Synthetic source notice.\n")
    host = Supervisor(tmp_path.resolve() / "state", reaper)
    try:
        metadata = next(item for item in host.builtin_recipe_metadata["recipes"]
                        if item["recipe_id"] == "mcpme.native-command")
        assert metadata["ready"] and metadata["execution_available"] is False
        family = next(item for item in host.preparations.families.values()
                      if item.component.reference.id == COMPONENT_ID)
        assert family.helper_sha256 == inventory["helper_sha256"]
        assert host.commands.admission is None
        assert host.preparation_runner.enabled is False
        assert family.component.reference.key not in host.preparation_runner.enabled_families
        draft = host.imports.create(source, name="Synthetic command app", source_mode="automatic")
        form = host.preparation_forms.form(draft["draft_id"], draft["revision"])
        assert form["blockers"] == [] and form["execution_available"] is False
        assert form["recipe"] == metadata["recipe"]
        answers = {field["id"]: (field["choices"][0]["id"] if field["choices"] else
            field["value"] if field["value"] is not None else {"ok": True}) for field in form["fields"]}
        descriptor, _ = host.preparation_forms.resolve(draft["draft_id"], draft["revision"],
            candidate_id=form["candidate_id"], recipe=form["recipe"], form_digest=form["form_digest"], answers=answers)
        assert descriptor.components[0].input["helper_sha256"] == inventory["helper_sha256"]
        assert family.expected_package_contract(descriptor, draft)["profile"] == "host-command"
        with pytest.raises(CommandExecutionError, match="host_command_not_admitted"):
            host.commands.executor("00000000-0000-4000-8000-000000000000")
        assert not host.owners
        assert not host.preparation_runner.tasks
    finally:
        await host.close()


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
async def test_default_command_recipe_lifecycle_survives_cold_restart(tmp_path, packaged, action):
    _, reaper, _ = packaged
    state = tmp_path.resolve() / "state"
    host = Supervisor(state, reaper)
    try:
        before = getattr(host.recipes, action)("mcpme.native-command", RECIPE_VERSION)
    finally:
        await host.close()
    restarted = Supervisor(state, reaper)
    try:
        assert restarted.recipes.inspect("mcpme.native-command", RECIPE_VERSION) == before
        metadata = next(item for item in restarted.builtin_recipe_metadata["recipes"]
                        if item["recipe_id"] == "mcpme.native-command")
        assert metadata["ready"] is False and metadata["execution_available"] is False
        assert restarted.commands.admission is None and restarted.preparation_runner.enabled is False
    finally:
        await restarted.close()


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
async def test_previous_command_recipe_opt_out_is_preserved_on_upgrade(tmp_path, packaged, action):
    from pathlib import Path
    from meecp.imports import ImportStore
    from meecp.recipes import RecipeStore
    _, reaper, _ = packaged
    state = tmp_path.resolve() / "state"
    legacy = Path(__file__).resolve().parents[1] / "templates/builtin-recipes/native-command-v1"
    with ImportStore(state) as imports:
        recipes = RecipeStore(imports)
        recipes.add(legacy)
        before = getattr(recipes, action)("mcpme.native-command", "1.0.0")
    host = Supervisor(state, reaper)
    try:
        assert host.recipes.inspect("mcpme.native-command", "1.0.0") == before
        metadata = next(item for item in host.builtin_recipe_metadata["recipes"]
            if item["recipe_id"] == "mcpme.native-command")
        assert metadata["version"] == RECIPE_VERSION and not metadata["ready"]
        assert "builtin_recipe_lifecycle_preserved" in metadata["blockers"]
        assert not metadata["execution_available"] and not host.preparation_runner.enabled
    finally:
        await host.close()
