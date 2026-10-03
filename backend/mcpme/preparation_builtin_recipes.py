"""Seed shipped declarative recipe metadata once, preserving user lifecycle state.

This module enables static metadata compatibility only. It never enables the
preparation runner, executes source, or adds executable recipe implementations.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .contracts import Digest
from .preparation_contracts import (
    Contract, PreparationError, RecipeID, Version, canonical_bytes, contract_digest, load_contract,
)
from .recipes import RecipeError, RecipeStore, _read, builtin_components, canonical_digest, validate_bundle

CATALOG_ROOT = Path(__file__).resolve().parents[2] / "templates/builtin-recipes"
MAX_CATALOG_BYTES = 32 * 1024
MAX_BUILTIN_BYTES = 256 * 1024
MAX_BUILTIN_FILES = 32


class BuiltinRecipeEntry(Contract):
    recipe_id: RecipeID
    version: Version
    directory: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    digest: Digest


class BuiltinRecipeCatalog(Contract):
    schema_version: Literal[1]
    recipes: list[BuiltinRecipeEntry] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique(self):
        keys = [(item.recipe_id, item.version) for item in self.recipes]
        if len(set(keys)) != len(keys) or len({item.directory for item in self.recipes}) != len(keys):
            raise PreparationError("duplicate_builtin_recipe")
        return self


def _catalog():
    try:
        raw = load_contract(_read(CATALOG_ROOT / "catalog.json", MAX_CATALOG_BYTES))
        return BuiltinRecipeCatalog.model_validate(raw)
    except (ValidationError, PreparationError, OSError):
        raise PreparationError("builtin_recipe_catalog_invalid") from None


def _checked_bundle(store: RecipeStore, entry: BuiltinRecipeEntry):
    bundle = validate_bundle(CATALOG_ROOT / entry.directory)
    manifest = bundle["manifest"]
    if ((manifest["id"], manifest["version"], bundle["digest"]) !=
            (entry.recipe_id, entry.version, entry.digest)):
        raise RecipeError("builtin_recipe_identity_mismatch")
    if len(bundle["files"]) > MAX_BUILTIN_FILES or sum(map(len, bundle["files"].values())) > MAX_BUILTIN_BYTES:
        raise RecipeError("builtin_recipe_size_limit")
    if manifest["kind"] != "declarative":
        raise RecipeError("unsupported_builtin_recipe_kind")
    trusted = builtin_components()
    for step in manifest["steps"]:
        component = trusted.get(step["component"] + "@" + step["version"])
        if component is None or component["digest"] != step["digest"]:
            raise RecipeError("builtin_recipe_component_mismatch")
    blockers = store._compatibility(manifest)
    if blockers:
        raise RecipeError(blockers[0])
    return bundle


def _record(store, entry):
    row = store.imports._connection.execute("SELECT data FROM recipes WHERE recipe_id=? AND version=?",
        (entry.recipe_id, entry.version)).fetchone()
    return json.loads(row["data"]) if row is not None else None


def _marker(store, entry):
    row = store.imports._connection.execute(
        "SELECT digest,state FROM builtin_recipe_seeds WHERE recipe_id=? AND version=?",
        (entry.recipe_id, entry.version)).fetchone()
    return dict(row) if row is not None else None


def _other_version_opted_out(store, entry):
    rows = store.imports._connection.execute("SELECT data FROM recipes WHERE recipe_id=? AND version<>?",
        (entry.recipe_id, entry.version)).fetchall()
    if any(json.loads(row["data"])["state"] in {"disabled", "revoked", "removed", "deprecated"} for row in rows):
        return True
    # A terminal seed marker survives later registry garbage collection.
    return store.imports._connection.execute("""SELECT 1 FROM builtin_recipe_seeds AS seeds
        LEFT JOIN recipes ON recipes.recipe_id=seeds.recipe_id AND recipes.version=seeds.version
        WHERE seeds.recipe_id=? AND seeds.version<>? AND seeds.state<>'pending' AND recipes.data IS NULL LIMIT 1""",
        (entry.recipe_id, entry.version)).fetchone() is not None


def _summary(entry, state, record=None, blockers=(), *, ready=False):
    return {"recipe_id": entry.recipe_id, "version": entry.version, "digest": entry.digest,
            "recipe": {"id": entry.recipe_id, "version": entry.version, "digest": entry.digest},
            "seed_state": state, "recipe_state": record["state"] if record else None,
            "ready": ready, "execution_available": False, "blockers": list(blockers)}


def _preserved(store, entry, record):
    blockers = (["builtin_recipe_record_missing"] if record is None else
                ["recipe_version_collision"] if record["digest"] != entry.digest else [])
    ready = False
    if not blockers and record["state"] == "enabled":
        try:
            store.assert_admissible(entry.recipe_id, entry.version, entry.digest)
            ready = True
        except PreparationError as error:
            blockers.append(error.code)
    return _summary(entry, "preserved", record, blockers, ready=ready)


def _untouched_proposal(record, entry):
    return (record is not None and record["digest"] == entry.digest and record["state"] == "proposed"
            and record["epoch"] == 1 and record["revoked"] is False and record["runnable"] is False
            and record["evidence"] is None)


def _finish_seed(store, entry, expected):
    """CAS the untouched first registration and durable marker in one commit.

RecipeStore.enable deliberately supports explicit user re-enablement. Startup
seeding must instead preserve any intervening lifecycle change, so it performs
only this narrower initial-proposal transition under the shared transaction.
"""
    with store.imports._transaction():
        marker, current = _marker(store, entry), _record(store, entry)
        if marker is None or marker["digest"] != entry.digest:
            return _summary(entry, "blocked", current, ["builtin_recipe_seed_identity_mismatch"])
        if marker["state"] != "pending":
            return _preserved(store, entry, current)
        opted_out = _other_version_opted_out(store, entry)
        untouched = not opted_out and current == expected and _untouched_proposal(current, entry)
        if not untouched:
            store.imports._connection.execute(
                "UPDATE builtin_recipe_seeds SET state='preserved' WHERE recipe_id=? AND version=?",
                (entry.recipe_id, entry.version))
            return (_summary(entry, "preserved", current, ["builtin_recipe_lifecycle_preserved"]) if opted_out
                    else _preserved(store, entry, current))
        current.update(state="enabled", epoch=2, updated_at=time.time(), blockers=[],
            evidence={"schema_version": 1, "recipe_digest": entry.digest,
                "component_catalog_digest": canonical_digest("recipe-components-v1", store.component_catalog),
                "identities": store.identities, "gate": "static-declarative-compatibility-v1",
                "outcome": "passed", "runnable": False})
        store._save(current)
        store.imports._connection.execute(
            "UPDATE builtin_recipe_seeds SET state='seeded' WHERE recipe_id=? AND version=?",
            (entry.recipe_id, entry.version))
        return _summary(entry, "seeded", current, ready=True)


def _seed_one(store, entry):
    with store.imports._transaction():
        marker, existing = _marker(store, entry), _record(store, entry)
        if marker is not None:
            if marker["digest"] != entry.digest:
                return _summary(entry, "blocked", existing, ["builtin_recipe_seed_identity_mismatch"])
            if marker["state"] != "pending":
                return _preserved(store, entry, existing)
            if existing is not None and not _untouched_proposal(existing, entry):
                store.imports._connection.execute(
                    "UPDATE builtin_recipe_seeds SET state='preserved' WHERE recipe_id=? AND version=?",
                    (entry.recipe_id, entry.version))
                return _preserved(store, entry, existing)
        else:
            opted_out = _other_version_opted_out(store, entry)
            state = "pending" if existing is None and not opted_out else "preserved"
            store.imports._connection.execute("INSERT INTO builtin_recipe_seeds VALUES (?,?,?,?,?)",
                (entry.recipe_id, entry.version, entry.digest, state, time.time()))
            if existing is not None:
                return _preserved(store, entry, existing)
            if opted_out:
                return _summary(entry, "preserved", blockers=["builtin_recipe_lifecycle_preserved"])
    # A pending marker can survive a crash before or after the static add.
    # add() is immutable/idempotent; changed lifecycle epochs cannot be enabled.
    record = store.add(CATALOG_ROOT / entry.directory)
    if record["digest"] != entry.digest:
        raise RecipeError("builtin_recipe_identity_mismatch")
    # Verify the captured bytes, not merely the installation's original files.
    if validate_bundle(Path(record["path"]))["digest"] != entry.digest:
        raise RecipeError("recipe_changed")
    return _finish_seed(store, entry, record)


def seed_builtin_recipes(store: RecipeStore) -> dict:
    """Seed host-owned metadata once; preserve every existing user decision.

Returns bounded statuses for startup/UI diagnostics. Per-recipe compatibility
failures do not overwrite the registry. A corrupt/missing shipped catalog raises
a fixed error. Call before presenting initial onboarding questions.
"""
    catalog = _catalog()
    with store.imports._transaction():
        store.imports._connection.execute("""CREATE TABLE IF NOT EXISTS builtin_recipe_seeds (
            recipe_id TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','seeded','preserved')),
            created_at REAL NOT NULL, PRIMARY KEY(recipe_id,version))""")
    results = []
    for entry in catalog.recipes:
        try:
            _checked_bundle(store, entry)
            results.append(_seed_one(store, entry))
        except PreparationError as error:
            results.append(_summary(entry, "blocked", blockers=[error.code]))
    result = {"schema_version": 1, "catalog_digest": contract_digest("builtin-recipe-catalog-v1", catalog.model_dump()),
              "execution_available": False, "recipes": results}
    canonical_bytes(result)
    return result
