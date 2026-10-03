"""Default recipe metadata is immutable, restart-safe and subordinate to users."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcpme import preparation_builtin_recipes as seeds
from mcpme.imports import ImportStore
from mcpme.preparation_contracts import PreparationError, schema_accepts
from mcpme.recipes import RecipeStore, builtin_components, validate_bundle


@pytest.fixture
def store(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        yield RecipeStore(imports)


def only(store):
    result = seeds.seed_builtin_recipes(store)
    assert result["execution_available"] is False
    assert len(result["recipes"]) == 1
    return result["recipes"][0]


def marker(store):
    return dict(store.imports._connection.execute("SELECT * FROM builtin_recipe_seeds").fetchone())


def copy_catalog(tmp_path):
    root = tmp_path / "catalog"
    shutil.copytree(seeds.CATALOG_ROOT, root)
    return root


def update_bundle(root, *, version=None, readme=None, component_digest=None):
    catalog = json.loads((root / "catalog.json").read_bytes())
    entry = catalog["recipes"][0]
    bundle = root / entry["directory"]
    manifest = json.loads((bundle / "mcpme.recipe.json").read_bytes())
    if version is not None:
        manifest["version"] = entry["version"] = version
    if component_digest is not None:
        manifest["steps"][0]["digest"] = component_digest
    manifest_data = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    (bundle / "mcpme.recipe.json").write_bytes(manifest_data)
    if readme is not None:
        (bundle / "README.md").write_text(readme)
    lock = json.loads((bundle / "mcpme.recipe.lock.json").read_bytes())
    lock["manifest_sha256"] = hashlib.sha256(manifest_data).hexdigest()
    for item in lock["artifacts"]:
        data = (bundle / item["path"]).read_bytes()
        item.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    (bundle / "mcpme.recipe.lock.json").write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n")
    entry["digest"] = validate_bundle(bundle)["digest"]
    (root / "catalog.json").write_text(json.dumps(catalog, sort_keys=True, indent=2) + "\n")
    return entry, bundle


def test_cold_start_seeds_exact_empty_input_metadata_without_execution(store, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("seed executed a process"))
    result = only(store)
    assert result["seed_state"] == "seeded" and result["ready"] is True
    assert result["recipe_state"] == "enabled" and result["blockers"] == []
    record = store.resolve(result["recipe_id"], result["version"], result["digest"])
    assert result["recipe"] == {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]}
    assert record["runnable"] is False and record["evidence"]["runnable"] is False
    assert record["evidence"]["gate"] == "static-declarative-compatibility-v1"
    assert record["manifest"]["form"] == []
    assert schema_accepts(record["manifest"]["input_schema"], {})
    assert not schema_accepts(record["manifest"]["input_schema"], {"label": "irrelevant"})
    component = record["manifest"]["steps"][0]
    assert component["digest"] == builtin_components()[component["component"] + "@" + component["version"]]["digest"]
    bundle = validate_bundle(Path(record["path"]))
    assert bundle["digest"] == result["digest"]
    assert marker(store)["state"] == "seeded"


def test_restart_is_idempotent_and_does_not_refresh_epoch_or_evidence(tmp_path):
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        result = only(store)
        before, stamp = store.list(), marker(store)
        assert only(store)["ready"] is True
        assert store.list() == before and marker(store) == stamp
    with ImportStore(tmp_path / "state") as imports:
        store = RecipeStore(imports)
        after = only(store)
        assert after["ready"] is True and after["seed_state"] == "preserved"
        assert after["recipe"] == result["recipe"]
        assert store.list() == before and marker(store) == stamp


def test_concurrent_cold_starts_enable_once_across_connections(tmp_path):
    root = tmp_path / "state"
    with ImportStore(root) as first, ImportStore(root) as second:
        stores = RecipeStore(first), RecipeStore(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(only, stores))
        assert all(item["ready"] for item in results)
        assert stores[0].list() == stores[1].list()
        assert len(stores[0].list()) == 1 and stores[0].list()[0]["epoch"] == 2


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
def test_explicit_lifecycle_decision_is_never_undone(store, action):
    result = only(store)
    before = getattr(store, action)(result["recipe_id"], result["version"])
    after = only(store)
    assert after["ready"] is False and after["seed_state"] == "preserved"
    assert store.inspect(result["recipe_id"], result["version"]) == before


@pytest.mark.parametrize("state", ["proposed", "enabled", "disabled", "removed", "revoked"])
def test_preexisting_exact_user_record_is_never_reenabled_or_rewritten(store, state):
    entry = seeds._catalog().recipes[0]
    record = store.add(seeds.CATALOG_ROOT / entry.directory)
    if state != "proposed":
        action = {"enabled": "enable", "disabled": "disable", "removed": "remove", "revoked": "revoke"}[state]
        record = getattr(store, action)(entry.recipe_id, entry.version)
    result = only(store)
    assert result["seed_state"] == "preserved" and result["ready"] is (state == "enabled")
    assert store.inspect(entry.recipe_id, entry.version) == record
    assert marker(store)["state"] == "preserved"


def test_user_version_collision_is_preserved_without_overwrite(store, tmp_path):
    root = copy_catalog(tmp_path)
    entry, path = update_bundle(root, readme="User-owned variant of this reserved identity.\n")
    before = store.add(path)
    result = only(store)
    assert result["ready"] is False and result["blockers"] == ["recipe_version_collision"]
    assert store.inspect(entry["recipe_id"], entry["version"]) == before
    assert validate_bundle(Path(before["path"]))["digest"] == entry["digest"]


def test_terminal_marker_prevents_recreation_after_registry_collection(store):
    result = only(store)
    store.remove(result["recipe_id"], result["version"])
    with store.imports._transaction():
        store.imports._connection.execute("DELETE FROM recipes")
    refreshed = only(store)
    assert refreshed["ready"] is False and refreshed["blockers"] == ["builtin_recipe_record_missing"]
    assert store.list() == [] and marker(store)["state"] == "seeded"


def test_changed_bundle_under_existing_seed_version_is_not_replaced(store, tmp_path, monkeypatch):
    result = only(store)
    before = store.list()
    root = copy_catalog(tmp_path)
    update_bundle(root, readme="Changed shipped bytes require a new version.\n")
    monkeypatch.setattr(seeds, "CATALOG_ROOT", root)
    changed = only(store)
    assert changed["ready"] is False and changed["blockers"] == ["builtin_recipe_seed_identity_mismatch"]
    assert store.list() == before and marker(store)["digest"] == result["digest"]


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
def test_catalog_upgrade_respects_opt_out_of_previous_version(store, tmp_path, monkeypatch, action):
    old = only(store)
    getattr(store, action)(old["recipe_id"], old["version"])
    before = store.list()
    root = copy_catalog(tmp_path)
    update_bundle(root, version="2.0.0")
    monkeypatch.setattr(seeds, "CATALOG_ROOT", root)
    new = only(store)
    assert new["ready"] is False and new["blockers"] == ["builtin_recipe_lifecycle_preserved"]
    assert store.list() == before
    assert only(store)["ready"] is False


def test_new_immutable_catalog_version_can_seed_when_older_version_stays_enabled(store, tmp_path, monkeypatch):
    old = only(store)
    before = store.inspect(old["recipe_id"], old["version"])
    root = copy_catalog(tmp_path)
    update_bundle(root, version="2.0.0")
    monkeypatch.setattr(seeds, "CATALOG_ROOT", root)
    new = only(store)
    assert new["ready"] is True and new["recipe"]["version"] == "2.0.0"
    assert len(store.list()) == 2 and store.inspect(old["recipe_id"], old["version"]) == before


def test_lifecycle_change_between_add_and_enable_wins_across_connections(store, monkeypatch):
    add = store.add
    def interrupted(path):
        initial = add(path)
        with ImportStore(store.imports.root) as other_imports:
            other = RecipeStore(other_imports)
            other.disable(initial["recipe_id"], initial["version"])
        return initial
    monkeypatch.setattr(store, "add", interrupted)
    result = only(store)
    assert result["ready"] is False and result["recipe_state"] == "disabled"
    assert marker(store)["state"] == "preserved"
    assert only(store)["recipe_state"] == "disabled"


def test_enable_and_marker_are_one_transaction(store, monkeypatch):
    save = store._save
    def fail_enable(record):
        save(record)
        if record["state"] == "enabled":
            raise PreparationError("synthetic_seed_write_failure")
    monkeypatch.setattr(store, "_save", fail_enable)
    failed = only(store)
    assert failed["blockers"] == ["synthetic_seed_write_failure"]
    assert store.list()[0]["state"] == "proposed" and marker(store)["state"] == "pending"
    monkeypatch.setattr(store, "_save", save)
    assert only(store)["ready"] is True and marker(store)["state"] == "seeded"


@pytest.mark.parametrize("boundary", ["before_add", "after_add", "during_enable", "after_commit"])
def test_actual_process_exit_retries_initial_seed_safely(tmp_path, boundary):
    root = tmp_path / "state"
    pid = os.fork()
    if pid == 0:
        try:
            with ImportStore(root) as imports:
                store = RecipeStore(imports)
                add, save, finish = store.add, store._save, seeds._finish_seed
                if boundary in {"before_add", "after_add"}:
                    def stop_add(path):
                        if boundary == "after_add":
                            add(path)
                        os._exit(31)
                    store.add = stop_add
                elif boundary == "during_enable":
                    def stop_save(record):
                        save(record)
                        if record["state"] == "enabled":
                            os._exit(31)
                    store._save = stop_save
                else:
                    def stop_finish(*args):
                        finish(*args)
                        os._exit(31)
                    seeds._finish_seed = stop_finish
                seeds.seed_builtin_recipes(store)
        except BaseException:
            os._exit(32)
        os._exit(33)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 31
    with ImportStore(root) as imports:
        store = RecipeStore(imports)
        result = only(store)
        assert result["ready"] is True and result["recipe_state"] == "enabled"
        assert len(store.list()) == 1 and store.list()[0]["epoch"] == 2
        assert marker(store)["state"] == "seeded"


def test_component_hash_drift_blocks_seed_without_creating_registry_entry(store, tmp_path, monkeypatch):
    root = copy_catalog(tmp_path)
    update_bundle(root, component_digest="0" * 64)
    monkeypatch.setattr(seeds, "CATALOG_ROOT", root)
    result = only(store)
    assert result["ready"] is False and result["blockers"] == ["builtin_recipe_component_mismatch"]
    assert store.list() == []


def test_existing_enabled_seed_with_stale_evidence_is_not_restamped(store):
    only(store)
    before = store.list()
    store.identities = {**store.identities, "runner": "0" * 64}
    result = only(store)
    assert result["ready"] is False and result["blockers"] == ["recipe_evidence_stale"]
    assert store.list() == before


@pytest.mark.parametrize("change", [lambda x: x.update(schema_version=2), lambda x: x.update(schema_version=True),
    lambda x: x.update(worker="arbitrary"), lambda x: x["recipes"].append(x["recipes"][0]),
    lambda x: x["recipes"][0].update(directory="../outside")])
def test_catalog_is_closed_bounded_versioned_and_path_safe(store, tmp_path, monkeypatch, change):
    root = copy_catalog(tmp_path)
    raw = json.loads((root / "catalog.json").read_bytes())
    change(raw)
    (root / "catalog.json").write_text(json.dumps(raw))
    monkeypatch.setattr(seeds, "CATALOG_ROOT", root)
    with pytest.raises(PreparationError, match="builtin_recipe_catalog_invalid"):
        seeds.seed_builtin_recipes(store)
    assert store.list() == []


def test_default_is_usable_by_generated_questions_when_forms_are_integrated(store, tmp_path):
    forms = pytest.importorskip("mcpme.preparation_forms", reason="form integration is owned by the root checkout")
    from mcpme.preparations import PreparationStore
    only(store)
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n return {'value': n * 2}\n")
    (source / "LICENSE").write_text("Synthetic permission notice.\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    (source / "uv.lock").write_text("version=1\nrevision=3\n")
    draft = store.imports.create(source, name="Default callable")
    service = forms.PreparationForms(PreparationStore(store.imports, store))
    form = service.form(draft["draft_id"], draft["revision"])
    assert form["recipe"] is not None and form["blockers"] == []
    assert "Tool label" not in {field["label"] for field in form["fields"]}
    body = {key: form[key] for key in ("candidate_id", "recipe", "form_digest")}
    body["answers"] = {field["id"]: {"value": 2} for field in form["fields"] if field["label"] == "Expected test result"}
    value, _ = service.resolve(draft["draft_id"], draft["revision"], **body)
    assert value.inputs == {} and value.interface.function == "double"
