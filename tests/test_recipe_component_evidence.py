"""Additive catalogs preserve exact enabled component closures without writes."""
import copy

import pytest

from mcpme.imports import ImportStore
from mcpme.recipes import RecipeStore, RecipeError, builtin_components, canonical_digest, LEGACY_COMPONENT_KEYS
from mcpme.preparation_builtin_recipes import seed_builtin_recipes
from test_mcpme_recipes import recipe


def extra():
    return {"digest": "d"*64, "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}


def test_legacy_shipped_catalog_evidence_survives_addition_without_restamping(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        current = builtin_components()
        legacy = {key: current[key] for key in LEGACY_COMPONENT_KEYS}
        store = RecipeStore(imports, component_catalog=legacy)
        item = store.add(recipe(tmp_path))
        enabled = store.enable(item["recipe_id"], item["version"])
        # Exact structure written by the prior host; it stored only a digest,
        # so migration recognizes the known shipped catalog, not arbitrary ones.
        enabled["evidence"] = {"schema_version": 1, "recipe_digest": item["digest"],
            "component_catalog_digest": canonical_digest("recipe-components-v1", legacy),
            "identities": store.identities, "gate": "static-declarative-compatibility-v1", "outcome": "passed", "runnable": False}
        with imports._transaction():
            store._save(enabled)
        before = copy.deepcopy(enabled)
        upgraded = RecipeStore(imports)
        assert upgraded.resolve(item["recipe_id"], item["version"], item["digest"]) == before
        seed_builtin_recipes(upgraded)
        assert upgraded.inspect(item["recipe_id"], item["version"]) == before
        damaged = copy.deepcopy(current)
        damaged["python-function-v1@1.0.0"]["digest"] = "e"*64
        changed = RecipeStore(imports, component_catalog=damaged)
        with pytest.raises(RecipeError, match="recipe_evidence_stale"):
            changed.assert_admissible(item["recipe_id"], item["version"], item["digest"])
        assert changed.inspect(item["recipe_id"], item["version"]) == before


def test_legacy_default_seed_keeps_exact_evidence_and_epoch_after_catalog_upgrade(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        current = builtin_components()
        legacy = {key: current[key] for key in LEGACY_COMPONENT_KEYS}
        store = RecipeStore(imports, component_catalog=legacy)
        seeded = seed_builtin_recipes(store)["recipes"][0]
        record = store.inspect(seeded["recipe_id"], seeded["version"])
        record["evidence"] = {"schema_version": 1, "recipe_digest": record["digest"],
            "component_catalog_digest": canonical_digest("recipe-components-v1", legacy),
            "identities": store.identities, "gate": "static-declarative-compatibility-v1", "outcome": "passed", "runnable": False}
        with imports._transaction():
            store._save(record)
        upgraded = RecipeStore(imports)
        result = seed_builtin_recipes(upgraded)
        assert all(item["ready"] for item in result["recipes"])
        assert result["recipes"][0]["seed_state"] == "preserved"
        assert upgraded.inspect(seeded["recipe_id"], seeded["version"]) == record


@pytest.mark.parametrize("mutation", ["implementation", "schema", "dependency"])
def test_new_evidence_binds_selected_and_transitive_closure_only(tmp_path, mutation):
    with ImportStore(tmp_path / "state") as imports:
        catalog = builtin_components()
        catalog["private-dependency@1.0.0"] = extra()
        catalog["python-function-v1@1.0.0"]["dependencies"] = [
            {"component": "private-dependency", "version": "1.0.0", "digest": "d"*64}]
        store = RecipeStore(imports, component_catalog=catalog)
        item = store.add(recipe(tmp_path))
        before = store.enable(item["recipe_id"], item["version"])
        assert before["evidence"]["schema_version"] == 2
        added = copy.deepcopy(catalog)
        added["unrelated-family@1.0.0"] = extra()
        added["existing-mcp-v1@1.0.0"]["digest"] = "f"*64
        upgraded = RecipeStore(imports, component_catalog=added)
        assert upgraded.assert_admissible(item["recipe_id"], item["version"], item["digest"]) == before
        damaged = copy.deepcopy(added)
        if mutation == "implementation":
            damaged["python-function-v1@1.0.0"]["digest"] = "e"*64
        elif mutation == "schema":
            damaged["python-function-v1@1.0.0"]["input_schema"]["properties"] = {"new": {"type": "string"}}
        else:
            damaged["private-dependency@1.0.0"]["input_schema"]["properties"] = {"changed": {"type": "integer"}}
        changed = RecipeStore(imports, component_catalog=damaged)
        with pytest.raises(RecipeError, match="recipe_evidence_stale"):
            changed.assert_admissible(item["recipe_id"], item["version"], item["digest"])
        assert changed.inspect(item["recipe_id"], item["version"]) == before
