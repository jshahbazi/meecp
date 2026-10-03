"""Recipe admission is static, immutable, and shares the draft transaction."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_contracts import PreparationError
from mcpme.recipes import RecipeError, RecipeStore, builtin_components, validate_bundle


def recipe(tmp_path: Path, name="bundle", **changes) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "LICENSE").write_text("Synthetic fixture dedicated to the public domain under CC0.\n")
    (root / "README.md").write_text("A nonsecret declarative recipe.\n")
    (root / "worker.py").write_text("raise RuntimeError('this file must never execute')\n")
    component = builtin_components()["python-function-v1@1.0.0"]
    manifest = {"schema_version": 1, "id": "local.synthetic", "version": "1.0.0", "kind": "declarative",
                "worker_api_version": 1, "input_schema": {"type": "object", "additionalProperties": False,
                "properties": {"label": {"type": "string"}}},
                "form": [{"field": "label", "label": "Tool label", "kind": "string", "default": "Synthetic"}],
                "steps": [{"component": "python-function-v1", "version": "1.0.0",
                           "digest": component["digest"], "config": {}}],
                "requirements": {}, "licenses": ["LICENSE"], "provenance": {"origin": "synthetic-fixture"}}
    manifest.update(changes)
    (root / "mcpme.recipe.json").write_text(json.dumps(manifest, sort_keys=True))
    lock(root)
    return root


def lock(root: Path):
    records = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"mcpme.recipe.json", "mcpme.recipe.lock.json"}:
            continue
        data = path.read_bytes()
        records.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data), "license": "LICENSE", "origin": "synthetic-fixture"})
    (root / "mcpme.recipe.lock.json").write_text(json.dumps({"schema_version": 1,
        "manifest_sha256": hashlib.sha256((root / "mcpme.recipe.json").read_bytes()).hexdigest(), "artifacts": records}))


def change_manifest(root: Path, **changes):
    path = root / "mcpme.recipe.json"
    value = json.loads(path.read_bytes())
    value.update(changes)
    path.write_text(json.dumps(value))
    lock(root)


def test_static_add_enable_pinned_resolution_and_private_snapshot(tmp_path, monkeypatch):
    root = recipe(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("static recipe operations must never spawn execution")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(root)
        assert added["state"] == "proposed" and not added["runnable"] and added["evidence"] is None
        assert added["blockers"] == []
        with pytest.raises(RecipeError, match="recipe_disabled"):
            store.resolve(added["recipe_id"], added["version"], added["digest"])
        enabled = store.enable(added["recipe_id"], added["version"])
        assert enabled["state"] == "enabled" and enabled["runnable"] is False
        assert enabled["evidence"]["gate"] == "static-declarative-compatibility-v2"
        assert store.resolve(added["recipe_id"], added["version"], added["digest"])["digest"] == added["digest"]
        assert store.list() == [enabled]
        (root / "README.md").write_text("Changed original input")
        assert store.resolve(added["recipe_id"], added["version"], added["digest"])["digest"] == added["digest"]
        snapshot = Path(added["path"])
        assert stat_mode(snapshot) == 0o700
        assert all(stat_mode(path) == 0o600 for path in snapshot.rglob("*") if path.is_file())


def test_removed_recipe_cannot_be_resurrected_by_another_lifecycle_action(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(recipe(tmp_path))
        store.enable(added["recipe_id"], added["version"])
        removed = store.remove(added["recipe_id"], added["version"])
        for action in (store.disable, store.deprecate, store.enable):
            with pytest.raises(RecipeError, match="recipe_unavailable"):
                action(added["recipe_id"], added["version"])
        assert store.inspect(added["recipe_id"], added["version"]) == removed
        assert store.revoke(added["recipe_id"], added["version"])["revoked"]


def test_registry_discovery_omits_large_schema_form_and_evidence(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(recipe(tmp_path))
        summary = store.list_summaries()[0]
        assert summary["digest"] == added["digest"]
        assert not {"manifest", "form", "input_schema", "evidence", "path"} & summary.keys()


def stat_mode(path):
    return path.stat().st_mode & 0o777


def test_reopened_registry_preserves_recipe_identity_and_lifecycle(tmp_path):
    root = recipe(tmp_path)
    state = tmp_path / "state"
    with ImportStore(state) as imports:
        store = RecipeStore(imports)
        added = store.add(root)
        store.enable(added["recipe_id"], added["version"])
        disabled = store.disable(added["recipe_id"], added["version"])
        with pytest.raises(RecipeError, match="recipe_disabled"):
            store.assert_admissible(added["recipe_id"], added["version"], added["digest"])
        assert store.assert_admissible(added["recipe_id"], added["version"], added["digest"], existing_job=True) == disabled
    with ImportStore(state) as imports:
        store = RecipeStore(imports)
        assert store.inspect(added["recipe_id"], added["version"])["state"] == "disabled"
        store.enable(added["recipe_id"], added["version"])
        deprecated = store.deprecate(added["recipe_id"], added["version"])
        assert store.assert_admissible(added["recipe_id"], added["version"], added["digest"], existing_job=True) == deprecated
        removed = store.remove(added["recipe_id"], added["version"])
        assert Path(removed["path"]).is_dir() and removed["evidence"]
        assert store.assert_admissible(added["recipe_id"], added["version"], added["digest"], existing_job=True) == removed
        with pytest.raises(RecipeError, match="recipe_unavailable"):
            store.enable(added["recipe_id"], added["version"])


def test_revocation_is_sticky_through_removal_and_blocks_existing_jobs(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(recipe(tmp_path))
        store.enable(added["recipe_id"], added["version"])
        store.revoke(added["recipe_id"], added["version"])
        removed = store.remove(added["recipe_id"], added["version"])
        assert removed["revoked"] is True
        with pytest.raises(RecipeError, match="recipe_revoked"):
            store.disable(added["recipe_id"], added["version"])
        with pytest.raises(RecipeError, match="recipe_revoked"):
            store.assert_admissible(added["recipe_id"], added["version"], added["digest"], existing_job=True)


def test_version_collision_and_same_digest_add_are_idempotent(tmp_path):
    root = recipe(tmp_path)
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(root)
        assert store.add(root) == added
        (root / "README.md").write_text("A changed implementation under the same version")
        lock(root)
        with pytest.raises(RecipeError, match="recipe_version_collision"):
            store.add(root)
        assert store.inspect(added["recipe_id"], added["version"])["digest"] == added["digest"]
        assert not list(store.root.glob("attempt-*"))


def test_overlapping_variants_do_not_invalidate_explicit_recipe_pins(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        first = store.add(recipe(tmp_path))
        second = store.add(recipe(tmp_path, "variant", id="local.variant"))
        for entry in (first, second):
            store.enable(entry["recipe_id"], entry["version"])
            assert store.resolve(entry["recipe_id"], entry["version"], entry["digest"])["recipe_id"] == entry["recipe_id"]
        with pytest.raises(RecipeError, match="recipe_digest_mismatch"):
            store.resolve(first["recipe_id"], first["version"], second["digest"])


@pytest.mark.parametrize("field,value", [
    ("gate_evidence", {"outcome": "passed"}), ("script", "worker.py"), ("schema_version", True),
    ("worker_api_version", 2), ("version", "01.0.0"),
])
def test_bundle_cannot_assert_evidence_or_add_unsupported_contract_fields(tmp_path, field, value):
    root = recipe(tmp_path, **{field: value})
    with pytest.raises(PreparationError):
        validate_bundle(root)


@pytest.mark.parametrize("change", ["unlocked", "content", "symlink", "hardlink", "traversal", "notice"])
def test_closed_tree_and_license_hashes_reject_unsafe_or_changed_bytes(tmp_path, change):
    root = recipe(tmp_path)
    if change == "unlocked":
        (root / "extra.py").write_text("Unlisted source")
    elif change == "content":
        (root / "README.md").write_text("Tampered")
    elif change == "symlink":
        (root / "README.md").unlink()
        (root / "README.md").symlink_to(root / "LICENSE")
    elif change == "hardlink":
        os.link(root / "README.md", tmp_path / "linked")
    elif change == "traversal":
        path = root / "mcpme.recipe.lock.json"
        value = json.loads(path.read_bytes())
        value["artifacts"][0]["path"] = "../outside"
        path.write_text(json.dumps(value))
    elif change == "notice":
        (root / "LICENSE").write_text("")
        lock(root)
    with pytest.raises(PreparationError):
        validate_bundle(root)


@pytest.mark.parametrize("form", [
    [{"field": "label", "label": "<script>execute()</script>", "kind": "string"}],
    [{"field": "label", "label": "Tool label", "kind": "html"}],
    [{"field": "label", "label": "Tool label", "kind": "string", "script": "worker.py"}],
    [{"field": "label", "label": "Tool label", "kind": "integer"}],
])
def test_forms_are_bounded_typed_metadata_without_html_or_scripts(tmp_path, form):
    with pytest.raises(PreparationError):
        validate_bundle(recipe(tmp_path, form=form))


def test_external_schema_references_are_never_fetched(tmp_path):
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"label": {"$ref": "https://example.invalid/schema"}}}
    with pytest.raises(PreparationError, match="external_schema_reference"):
        validate_bundle(recipe(tmp_path, input_schema=schema))


def test_executable_bundle_is_static_proposed_and_cannot_enable(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(recipe(tmp_path, kind="executable"))
        assert added["state"] == "proposed" and added["blockers"] == ["unsupported_recipe_kind"]
        with pytest.raises(RecipeError, match="unsupported_recipe_kind"):
            store.enable(added["recipe_id"], added["version"])


@pytest.mark.parametrize("change,blocker", [
    ("component", "recipe_component_unavailable"), ("digest", "recipe_component_digest_mismatch"),
    ("config", "invalid_recipe_component_config"),
])
def test_component_resolution_requires_exact_known_pins_and_closed_config(tmp_path, change, blocker):
    root = recipe(tmp_path)
    manifest = json.loads((root / "mcpme.recipe.json").read_bytes())
    manifest["steps"][0][change] = {"shell": "arbitrary"} if change == "config" else (
        "a" * 64 if change == "digest" else "unknown-worker")
    change_manifest(root, steps=manifest["steps"])
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(root)
        assert blocker in added["blockers"]
        with pytest.raises(RecipeError, match=blocker):
            store.enable(added["recipe_id"], added["version"])


def test_composition_cycles_in_trusted_dependency_graph_block_enablement(tmp_path):
    root = recipe(tmp_path)
    manifest = json.loads((root / "mcpme.recipe.json").read_bytes())
    first = manifest["steps"][0]
    second = {"component": "existing-mcp-v1", "version": "1.0.0", "digest": "b" * 64}
    catalog = builtin_components()
    catalog["python-function-v1@1.0.0"]["dependencies"] = [second]
    catalog["existing-mcp-v1@1.0.0"].update(digest="b" * 64, dependencies=[first])
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports, component_catalog=catalog)
        added = store.add(root)
        assert "recipe_dependency_cycle" in added["blockers"]
        with pytest.raises(RecipeError, match="recipe_dependency_cycle"):
            store.enable(added["recipe_id"], added["version"])


def test_evidence_lifetime_binds_runner_and_component_catalog_identity(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(recipe(tmp_path))
        store.enable(added["recipe_id"], added["version"])
        upgraded = RecipeStore(imports, runner_identity="c" * 64)
        with pytest.raises(RecipeError, match="recipe_evidence_stale"):
            upgraded.assert_admissible(added["recipe_id"], added["version"], added["digest"])
        upgraded.enable(added["recipe_id"], added["version"])
        assert upgraded.assert_admissible(added["recipe_id"], added["version"], added["digest"])["evidence"]["identities"]["runner"] == "c" * 64


def test_admission_reads_current_recipe_state_inside_import_transaction(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(recipe(tmp_path))
        store.enable(added["recipe_id"], added["version"])
        with imports._transaction():
            record = store._get(added["recipe_id"], added["version"])
            record.update(state="revoked", revoked=True)
            store._save(record)
            with pytest.raises(RecipeError, match="recipe_revoked"):
                store.assert_admissible(added["recipe_id"], added["version"], added["digest"], existing_job=True)


def test_snapshot_tampering_and_limits_never_reenable_changed_recipe(tmp_path, monkeypatch):
    root = recipe(tmp_path)
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        added = store.add(root)
        (Path(added["path"]) / "README.md").write_text("Tampered snapshot")
        with pytest.raises(RecipeError, match="recipe_artifact_hash_mismatch"):
            store.enable(added["recipe_id"], added["version"])
    monkeypatch.setattr("mcpme.recipes.MAX_BUNDLE_BYTES", 1)
    with pytest.raises(RecipeError, match="recipe_size_limit"):
        validate_bundle(root)
