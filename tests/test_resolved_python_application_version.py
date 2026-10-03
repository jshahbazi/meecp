"""Exact current family authoring and immutable prior recipe lifecycle."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from mcpme.imports import ImportStore
from mcpme import preparation_builtin_recipes as seeds
from mcpme.preparation_forms import ResolvedPythonFormProvider
from mcpme.preparation_resolved_python import PUBLISHED_COMPONENTS, ResolvedPythonComponent, component_catalog
from mcpme.recipes import RecipeStore, validate_bundle
from test_mcpme_management_cli import execute, private_manager

ROOT = Path(__file__).resolve().parents[1]


async def test_actual_cli_scaffold_check_add_enable_binds_current_exact_resolved_family(tmp_path):
    bundle = tmp_path / "independent"
    created = await execute(tmp_path, "recipes", "scaffold", "--output", bundle,
        "--id", "local.current-resolved", "--component", "resolved-python-v1", "--json")
    checked = await execute(tmp_path, "recipes", "check", "--path", bundle, "--json")
    assert created["digest"] == checked["digest"] and checked["valid"]
    reference = ResolvedPythonComponent().reference
    step, = validate_bundle(bundle)["manifest"]["steps"]
    assert (step["component"], step["version"], step["digest"]) == reference.key
    assert reference.version == "1.0.1"
    async with private_manager(tmp_path) as host:
        added = await execute(host.root, "recipes", "add", "--path", bundle, "--json")
        enabled = await execute(host.root, "recipes", "enable", "--id", "local.current-resolved",
            "--version", created["version"], "--json")
        assert added["digest"] == enabled["digest"] == checked["digest"]
        assert enabled["state"] == "enabled" and not enabled["runnable"]


def test_old_component_metadata_and_bundle_remain_exact_but_forms_offer_only_current():
    old = validate_bundle(ROOT / "templates/builtin-recipes/resolved-python-v1")
    current = validate_bundle(ROOT / "templates/builtin-recipes/resolved-python-v1-0-1")
    old_step, = old["manifest"]["steps"]
    new_step, = current["manifest"]["steps"]
    reference = ResolvedPythonComponent().reference
    assert old_step["version"] == "1.0.0" and old_step["digest"] == PUBLISHED_COMPONENTS["1.0.0"]
    assert component_catalog()["resolved-python-v1@1.0.0"]["digest"] == old_step["digest"]
    assert (new_step["component"], new_step["version"], new_step["digest"]) == reference.key
    provider = ResolvedPythonFormProvider()
    candidate = {"_resolved_path": True}
    assert not provider.accepts(candidate, old)
    assert provider.accepts(candidate, current)
    for key in ("component", "version", "digest"):
        changed = {"manifest": {"steps": [{**new_step, key: "invalid"}]}}
        assert not provider.accepts(candidate, changed)


@pytest.mark.parametrize("action", ["disable", "deprecate", "revoke", "remove"])
def test_resolved_default_upgrade_preserves_explicit_old_lifecycle(tmp_path, monkeypatch, action):
    catalog_root = tmp_path / "catalog"
    shutil.copytree(ROOT / "templates/builtin-recipes", catalog_root)
    old = validate_bundle(catalog_root / "resolved-python-v1")
    current = validate_bundle(catalog_root / "resolved-python-v1-0-1")
    def catalog(bundle, directory):
        return {"schema_version": 1, "recipes": [{"recipe_id": bundle["manifest"]["id"],
            "version": bundle["manifest"]["version"], "digest": bundle["digest"], "directory": directory}]}
    path = catalog_root / "catalog.json"
    path.write_text(json.dumps(catalog(old, "resolved-python-v1")))
    monkeypatch.setattr(seeds, "CATALOG_ROOT", catalog_root)
    state = tmp_path / "state"
    with ImportStore(state) as imports:
        recipes = RecipeStore(imports)
        initial, = seeds.seed_builtin_recipes(recipes)["recipes"]
        assert initial["ready"]
        getattr(recipes, action)(initial["recipe_id"], initial["version"])
        previous = recipes.list()
    path.write_text(json.dumps(catalog(current, "resolved-python-v1-0-1")))
    with ImportStore(state) as imports:
        recipes = RecipeStore(imports)
        result, = seeds.seed_builtin_recipes(recipes)["recipes"]
        assert not result["ready"] and result["blockers"] == ["builtin_recipe_lifecycle_preserved"]
        assert recipes.list() == previous
        assert not any(item["version"] == "1.0.1" for item in recipes.list())


def test_new_resolved_seed_keeps_enabled_old_record_byte_equal(tmp_path, monkeypatch):
    catalog_root = tmp_path / "catalog"
    shutil.copytree(ROOT / "templates/builtin-recipes", catalog_root)
    old = validate_bundle(catalog_root / "resolved-python-v1")
    path = catalog_root / "catalog.json"
    original_catalog = json.loads(path.read_bytes())
    path.write_text(json.dumps({"schema_version": 1, "recipes": [{"recipe_id": "mcpme.resolved-python",
        "version": "1.0.0", "digest": old["digest"], "directory": "resolved-python-v1"}]}))
    monkeypatch.setattr(seeds, "CATALOG_ROOT", catalog_root)
    with ImportStore(tmp_path / "state") as imports:
        recipes = RecipeStore(imports)
        initial, = seeds.seed_builtin_recipes(recipes)["recipes"]
        previous = recipes.inspect(initial["recipe_id"], initial["version"])
        path.write_text(json.dumps(original_catalog))
        results = seeds.seed_builtin_recipes(recipes)["recipes"]
        current, = [item for item in results if item["recipe_id"] == "mcpme.resolved-python"]
        assert current["version"] == "1.0.1" and current["ready"]
        assert recipes.inspect(initial["recipe_id"], initial["version"]) == previous
