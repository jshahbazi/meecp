"""Historical textgen lifecycle choices survive a newer default recipe."""

from pathlib import Path

import pytest

from meecp import preparation_builtin_recipes as seeds
from meecp.imports import ImportStore
from meecp.preparation_textgen import RECIPE_VERSION, VERSION
from meecp.recipes import RecipeStore, builtin_components, validate_bundle


def versions(monkeypatch, old_version):
    root = seeds.CATALOG_ROOT
    old_directory = "text-generation-v" + old_version.replace(".", "-")
    old_bundle = validate_bundle(root / old_directory)
    current = next(item for item in seeds._catalog().recipes if item.recipe_id == "mcpme.text-generation")
    new_bundle = validate_bundle(root / current.directory)
    refs = {step["component"]: step for step in old_bundle["manifest"]["steps"]}
    components = builtin_components()
    # Metadata from the immutable previous bundle simulates the retained
    # previous release registry; no historical worker is executed.
    for identifier, step in refs.items():
        component_version = step["version"]
        components[identifier + "@" + component_version] = {
            "digest": step["digest"], "input_schema": {"type": "object", "additionalProperties": False},
            "dependencies": [] if identifier == "llamacpp-cpu-v1" else [
                {"component": "llamacpp-cpu-v1", "version": refs["llamacpp-cpu-v1"]["version"], "digest": refs["llamacpp-cpu-v1"]["digest"]}],
        }
    monkeypatch.setattr(seeds, "builtin_components", lambda: components)
    entries = []
    for version, directory, bundle in [(old_version, old_directory, old_bundle),
                                       (current.version, current.directory, new_bundle)]:
        entries.append(seeds.BuiltinRecipeEntry(recipe_id="mcpme.text-generation", version=version,
            directory=directory, digest=bundle["digest"]))
    return components, entries


def choose(monkeypatch, entry):
    monkeypatch.setattr(seeds, "_catalog", lambda: seeds.BuiltinRecipeCatalog(schema_version=1, recipes=[entry.model_dump(mode="json")]))


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
@pytest.mark.parametrize("old_version", ["1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25"])
def test_exact_textgen_upgrade_preserves_owner_opt_out_across_restart(tmp_path, monkeypatch, action, old_version):
    components, (old, new) = versions(monkeypatch, old_version)
    state = tmp_path.resolve() / "state"
    with ImportStore(state) as imports:
        store = RecipeStore(imports, component_catalog=components)
        choose(monkeypatch, old)
        initial = seeds.seed_builtin_recipes(store)["recipes"][0]
        assert initial["ready"] and initial["recipe_state"] == "enabled"
        getattr(store, action)(old.recipe_id, old.version)
        before = store.list()
        choose(monkeypatch, new)
        denied = seeds.seed_builtin_recipes(store)["recipes"][0]
        assert not denied["ready"] and denied["blockers"] == ["builtin_recipe_lifecycle_preserved"]
        assert store.list() == before
    with ImportStore(state) as imports:
        restarted = RecipeStore(imports, component_catalog=components)
        again = seeds.seed_builtin_recipes(restarted)["recipes"][0]
        assert not again["ready"] and restarted.list() == before
        assert restarted.inspect(old.recipe_id, old.version)["state"] == before[0]["state"]
        assert not any(item["version"] == new.version for item in restarted.list())


@pytest.mark.parametrize("old_version", ["1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25"])
def test_enabled_historical_recipe_and_exact_snapshot_survive_upgrade(tmp_path, monkeypatch, old_version):
    components, (old, new) = versions(monkeypatch, old_version)
    with ImportStore(tmp_path.resolve() / "state") as imports:
        store = RecipeStore(imports, component_catalog=components)
        choose(monkeypatch, old); assert seeds.seed_builtin_recipes(store)["recipes"][0]["ready"]
        before = store.inspect(old.recipe_id, old.version)
        old_digest = validate_bundle(Path(before["path"]))["digest"]
        choose(monkeypatch, new); assert seeds.seed_builtin_recipes(store)["recipes"][0]["ready"]
        assert len(store.list()) == 2 and store.inspect(old.recipe_id, old.version) == before
        assert store.resolve(old.recipe_id, old.version, old.digest, existing_job=True)["digest"] == old_digest == old.digest
        current = store.resolve(new.recipe_id, new.version, new.digest)
        assert current["runnable"] is False and current["evidence"]["runnable"] is False
        assert validate_bundle(Path(before["path"]))["digest"] == old_digest


@pytest.mark.parametrize("old_version", ["1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25"])
def test_collected_old_tombstone_prevents_new_default(tmp_path, monkeypatch, old_version):
    components, (old, new) = versions(monkeypatch, old_version)
    with ImportStore(tmp_path.resolve() / "state") as imports:
        store = RecipeStore(imports, component_catalog=components)
        choose(monkeypatch, old); assert seeds.seed_builtin_recipes(store)["recipes"][0]["ready"]
        store.remove(old.recipe_id, old.version)
        with imports._transaction():
            imports._connection.execute("DELETE FROM recipes")
        choose(monkeypatch, new)
        denied = seeds.seed_builtin_recipes(store)["recipes"][0]
        assert not denied["ready"] and denied["blockers"] == ["builtin_recipe_lifecycle_preserved"]
        assert store.list() == []


def test_public_provenance_and_new_component_bindings_are_exact():
    root = seeds.CATALOG_ROOT
    entry = next(item for item in seeds._catalog().recipes if item.recipe_id == "mcpme.text-generation")
    assert (root / "text-generation-v1-0-5/model-provenance.json").read_bytes() == (root / entry.directory / "model-provenance.json").read_bytes()
    bundle = validate_bundle(root / entry.directory)
    assert entry.version == RECIPE_VERSION and entry.digest == bundle["digest"]
    components = builtin_components()
    assert len(bundle["files"]) == 9
    for step in bundle["manifest"]["steps"]:
        assert step["version"] == VERSION and components[step["component"] + "@" + VERSION]["digest"] == step["digest"]
