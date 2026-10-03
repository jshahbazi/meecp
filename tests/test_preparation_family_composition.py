"""Trusted composite families route only complete identities; inspection is static."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_components import ComponentReference, parse_component_descriptor
from mcpme.preparation_contracts import PreparationError
from mcpme.preparations import PreparationStore
from mcpme.recipes import RecipeStore
from test_preparation_components import FakeNativeComponent, v2_descriptor


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
