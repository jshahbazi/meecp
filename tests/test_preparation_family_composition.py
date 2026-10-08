"""Trusted composite families route only complete identities; inspection is static."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from meecp.imports import ImportStore
from meecp.preparation_components import ComponentReference, parse_component_descriptor
from meecp.preparation_contracts import PreparationError
from meecp.preparations import PreparationStore
from meecp.preparation_runner import PreparationRunner
from meecp.preparation_verifier import VerificationReport
from meecp.recipes import RecipeStore
from test_preparation_components import FakeNativeComponent, v2_descriptor
from test_preparation_processes import group_reaper


class FakeTaskComponent(FakeNativeComponent):
    reference = ComponentReference(id="task-fixture-v1", version="1.0.0", digest="e" * 64)


@pytest.fixture
def composite(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "worker.swift").write_text("fatalError(\"must never execute\")\n")
    (folder / "worker.py").write_text("raise RuntimeError('must never execute')\ndef echo(text: str) -> dict: return {'text': text}\n")
    engine, task = FakeNativeComponent(), FakeTaskComponent()
    family = SimpleNamespace(component=task, components=(engine, task), source_kind=None,
                             supports_source=lambda draft: draft["source"]["kind"] == "local_folder")
    family.inspect_source = lambda draft: {"schema_version": 1, "source_revision": draft["source"]["revision"],
        "candidates": [{"candidate_id": "f" * 64, "kind": "native-contract-fixture", "module": "worker.swift",
                        "tools": [], "unresolved": []}], "blockers": []}
    with ImportStore(tmp_path / "state") as imports:
        store = PreparationStore(imports, RecipeStore(imports), families=(family,))
        draft = imports.create(folder, name="Composite fixture")
        raw = v2_descriptor(engine.reference)
        raw["components"].append({"component": task.reference.model_dump(), "input": {"entrypoint": "worker.swift"}})
        yield store, draft, family, raw


def test_composite_family_requires_exact_complete_component_set(composite):
    store, draft, family, raw = composite
    value = parse_component_descriptor(raw)
    assert store.family_for(value) is family
    reversed_value = parse_component_descriptor({**raw, "components": list(reversed(raw["components"]))})
    assert store.family_for(reversed_value) is family
    recipe = {"digest": value.recipe.digest, "manifest": {"id": value.recipe.id, "version": value.recipe.version,
        "steps": [{"component": item.component.id, "version": item.component.version,
                   "digest": item.component.digest} for item in value.components]}}
    bindings = store.components.validate_source(value, draft, recipe)
    assert {item.component_key for item in bindings} == {item.component.key for item in value.components}


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra"])
def test_partial_changed_or_expanded_component_sets_cannot_select_family(composite, mutation):
    store, draft, family, original = composite
    raw = copy.deepcopy(original)
    if mutation == "missing":
        raw["components"].pop()
    elif mutation == "changed":
        raw["components"][0]["component"]["version"] = "2.0.0"
    else:
        raw["components"].append({"component": {"id": "extra-fixture-v1", "version": "1.0.0", "digest": "a" * 64},
                                  "input": {}})
    with pytest.raises(PreparationError, match="^unknown_preparation_component$"):
        store.family_for(parse_component_descriptor(raw))


def test_directory_inspection_preserves_python_and_distinct_family_candidates(composite, monkeypatch):
    store, draft, family, raw = composite
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("inspection executed selected source"))
    result = store.inspect_source(draft)
    assert result["source_revision"] == draft["source"]["revision"]
    assert {item["kind"] for item in result["candidates"]} == {"python-callable", "native-contract-fixture"}
    assert result["blockers"] == []


def test_duplicate_candidate_identity_or_foreign_source_revision_fails_closed(composite):
    store, draft, family, raw = composite
    candidate = store.inspect_source(draft)["candidates"][0]
    family.inspect_source = lambda source: {"schema_version": 1, "source_revision": source["source"]["revision"],
                                            "candidates": [candidate, candidate], "blockers": []}
    with pytest.raises(PreparationError, match="^ambiguous_preparation_provider$"):
        store.inspect_source(draft)
    family.inspect_source = lambda source: {"schema_version": 1, "source_revision": "sha256:" + "0" * 64,
                                            "candidates": [], "blockers": []}
    with pytest.raises(PreparationError, match="^source_identity_mismatch$"):
        store.inspect_source(draft)


async def test_global_runner_switch_does_not_admit_composite_family(composite, tmp_path, monkeypatch):
    store, draft, family, raw = composite
    value = parse_component_descriptor(raw)
    monkeypatch.setattr(store, "_bound_descriptor", lambda *args: (value, draft, {}))
    monkeypatch.setattr("meecp.preparation_runner.load_private_toolchain",
                        lambda *args: pytest.fail("denied family loaded execution inputs"))
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True)
    with pytest.raises(PreparationError, match="^preparation_family_unavailable$"):
        await runner.plan(draft["draft_id"], draft["revision"], raw)
    assert store.list() == []
    assert not runner.execution_available_for_recipe({"manifest": {"steps": [
        {"component": c.reference.id, "version": c.reference.version, "digest": c.reference.digest}
        for c in family.components]}})
    await runner.close()


async def test_revoked_family_admission_cannot_consume_previously_reviewed_plan(composite, tmp_path, monkeypatch):
    store, draft, family, raw = composite
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True,
                               enabled_families=(family.component.reference.key,))
    monkeypatch.setattr(store, "_execution_plan", lambda *args: {"descriptor": raw})
    monkeypatch.setattr(store, "authorize_start", lambda *args: pytest.fail("denied family consumed authority"))
    runner.enabled_families = frozenset()
    with pytest.raises(PreparationError, match="^preparation_family_unavailable$"):
        await runner.start("unused", "a" * 64, "unused")
    assert not runner.tasks and not runner.attempts and store.list() == []
    await runner.close()


async def test_specialist_verifier_receives_same_owned_process_boundary(composite, tmp_path, monkeypatch):
    store, draft, family, raw = composite
    family.target_profile = "model-service"
    owner = object()
    report = VerificationReport("a" * 64, 1, 1, "2025-11-25", 4096)
    async def specialist(root, descriptor, reaper, workspace, *, timeout_seconds, owner):
        assert (root, reaper, workspace, timeout_seconds, owner) == (
            tmp_path / "candidate", tmp_path / "reaper", tmp_path / "probe", 7, expected_owner)
        assert {item.component.key for item in descriptor.components} == {c.reference.key for c in family.components}
        return report
    expected_owner = owner
    family.verify = specialist
    monkeypatch.setattr("meecp.preparation_runner.verify_prepared_package",
                        lambda *args, **kwargs: pytest.fail("specialist fell through to stdio probe"))
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True,
                               enabled_families=(family.component.reference.key,))
    assert await runner._verify(tmp_path / "candidate", parse_component_descriptor(raw), tmp_path / "probe",
                                timeout_seconds=7, owner=owner) is report
    await runner.close()


async def test_nonstdio_family_requires_specialist_and_stdio_keeps_existing_probe(composite, tmp_path, monkeypatch):
    store, draft, family, raw = composite
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True,
                               enabled_families=(family.component.reference.key,))
    family.target_profile = "model-service"
    with pytest.raises(PreparationError, match="^independent_runtime_verifier_unavailable$"):
        await runner._verify(tmp_path, parse_component_descriptor(raw), tmp_path, timeout_seconds=1, owner=None)
    family.target_profile = "mcp-stdio"
    async def stdio(*args, **kwargs):
        return "stdio-verifier"
    monkeypatch.setattr("meecp.preparation_runner.verify_prepared_package", stdio)
    assert await runner._verify(tmp_path, parse_component_descriptor(raw), tmp_path,
                                timeout_seconds=1, owner=None) == "stdio-verifier"
    await runner.close()


def test_family_admission_rejects_unknown_version_and_partial_recipe(composite, tmp_path):
    store, draft, family, raw = composite
    with pytest.raises(PreparationError, match="^unknown_preparation_component$"):
        PreparationRunner(store, tmp_path, tmp_path, enabled=True,
                          enabled_families=((family.component.reference.id, "2.0.0", family.component.reference.digest),))
    runner = PreparationRunner(store, tmp_path, tmp_path, enabled=True,
                               enabled_families=(family.component.reference.key,))
    record = {"manifest": {"steps": [
        {"component": c.reference.id, "version": c.reference.version, "digest": c.reference.digest}
        for c in family.components]}}
    assert runner.execution_available_for_recipe(record)
    record["manifest"]["steps"].pop()
    assert not runner.execution_available_for_recipe(record)


async def test_real_composite_implementation_identities_are_canonical_and_bind_verifier_profile(composite, tmp_path, group_reaper):
    store, _, family, raw = composite
    runner = PreparationRunner(store, tmp_path, group_reaper, enabled=True,
                               enabled_families=(family.component.reference.key,))
    value = parse_component_descriptor(raw)
    try:
        first = await runner._implementation_identities(value, "d" * 64)
        assert all(len(identity) == 64 and int(identity, 16) >= 0 for identity in first)
        family.components = tuple(reversed(family.components))
        assert await runner._implementation_identities(value, "d" * 64) == first
        family.target_profile = "model-service"
        second = await runner._implementation_identities(value, "d" * 64)
        assert second[0] == first[0] and second[1] != first[1]
        async def specialist(*args, **kwargs):
            raise AssertionError("identity calculation must not execute a verifier")
        family.verify = specialist
        third = await runner._implementation_identities(value, "d" * 64)
        assert third[0] == first[0] and third[1] != second[1]
    finally:
        await runner.close()
