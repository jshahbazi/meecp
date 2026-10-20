"""Exercise the entire shipped catalog with real current producer identities."""
import json
from pathlib import Path

from meecp.imports import ImportStore
from meecp.preparation_builtin_recipes import seed_builtin_recipes
from meecp.preparation_release_policy import ReleasePolicy, POLICY_PATH, validate_policy_implementations
from meecp.recipes import RecipeStore


def test_every_shipped_recipe_seeds_and_matches_current_release_authority(tmp_path):
    root = Path(__file__).resolve().parents[1]
    policy = ReleasePolicy.model_validate(json.loads((root / POLICY_PATH).read_bytes()))
    validate_policy_implementations(policy)
    catalog = json.loads((root / "templates/builtin-recipes/catalog.json").read_bytes())
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        result = seed_builtin_recipes(store)
        assert len(result["recipes"]) == len(catalog["recipes"])
        assert all(item["ready"] and not item["blockers"] for item in result["recipes"])
        assert {(item["recipe_id"], item["version"], item["digest"]) for item in store.list()} == {
            (item["recipe_id"], item["version"], item["digest"]) for item in catalog["recipes"]}
