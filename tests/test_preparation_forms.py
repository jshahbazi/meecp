"""Generated form fidelity, static inspection and stale-choice boundaries."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcpme.imports import ImportError, ImportStore
from mcpme.preparation_contracts import PreparationError, contract_digest
from mcpme.preparation_forms import FormField, OnboardingFormV1, PreparationForms
from mcpme.preparations import PreparationStore
from mcpme.recipe_sdk import relock_recipe, scaffold_recipe
from mcpme.recipes import RecipeStore


@pytest.fixture
def form_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "unexpected-execution"
    (source / "worker.py").write_text("from pathlib import Path\n" + f"Path({str(marker)!r}).touch()\n"
        "def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n")
    (source / "LICENSE").write_text("Synthetic permission notice.\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    (source / "uv.lock").write_text("version=1\nrevision=3\n")
    with ImportStore(tmp_path / "state") as imports:
        recipes = RecipeStore(imports)
        scaffold_recipe(tmp_path / "recipe", "test.generated")
        record = recipes.add(tmp_path / "recipe")
        recipes.enable(record["recipe_id"], record["version"])
        draft = imports.create(source, name="Synthetic double", intent="Expose the selected integer transformation")
        jobs = PreparationStore(imports, recipes)
        yield PreparationForms(jobs), draft, marker


def answers(form, **by_label):
    return {field["id"]: by_label[field["label"]] for field in form["fields"] if field["label"] in by_label}


def resolve(service, form, values):
    return service.resolve(form["import_id"], form["revision"], candidate_id=form["candidate_id"],
        recipe=form["recipe"], form_digest=form["form_digest"], answers=values)


def test_generated_answers_create_complete_descriptor_without_source_execution(form_source):
    service, draft, marker = form_source
    form = service.form(draft["draft_id"], 1)
    assert form["source_revision"] == draft["source"]["revision"]
    assert form["execution_available"] is False and form["blockers"] == []
    assert form["candidate_id"] and form["recipe"]["id"] == "test.generated"
    assert service.form(draft["draft_id"], 1) == form
    assert all("schema" in item and "schema_" not in item for item in form["fields"])
    descriptor, context = resolve(service, form, answers(form, **{"Expected test result": {"value": 2}}))
    assert descriptor.interface.module == "worker" and descriptor.interface.function == "double"
    assert descriptor.interface.tools[0].input_schema["properties"]["n"]["type"] == "integer"
    assert descriptor.validation[0].expected == {"value": 2}
    assert descriptor.inputs == {"label": "Synthetic tool", "precision": 2}
    assert descriptor.source_license == "LICENSE"
    assert context.form.form_digest == form["form_digest"]
    assert service.store.list() == [] and draft == service.imports.inspect(draft["draft_id"])
    assert not marker.exists()


def test_required_expected_result_and_opaque_unknown_fields_fail(form_source):
    service, draft, _ = form_source
    form = service.form(draft["draft_id"], 1)
    with pytest.raises(PreparationError, match="preparation_form_answer_required"):
        resolve(service, form, {})
    with pytest.raises(PreparationError, match="unknown_preparation_form_field"):
        resolve(service, form, {"interface.module": "other"})
    with pytest.raises(PreparationError, match="invalid_preparation_form_answer"):
        resolve(service, form, answers(form, **{"Expected test result": {"value": 2}, "Memory budget (MiB)": True}))


def test_stale_form_revision_recipe_and_source_close_normalization(form_source):
    service, draft, _ = form_source
    form = service.form(draft["draft_id"], 1)
    values = answers(form, **{"Expected test result": {"value": 2}})
    tampered = {**form, "form_digest": "f" * 64}
    with pytest.raises(PreparationError, match="stale_preparation_form"):
        resolve(service, tampered, values)
    service.recipes.disable(form["recipe"]["id"], form["recipe"]["version"])
    with pytest.raises(PreparationError, match="preparation_recipe_mismatch"):
        resolve(service, form, values)
    service.recipes.enable(form["recipe"]["id"], form["recipe"]["version"])
    service.imports.revise(draft["draft_id"], 1, name="Changed app")
    with pytest.raises(ImportError, match="stale_import_revision"):
        resolve(service, form, values)


def test_source_mutation_is_not_hidden_by_cached_fields(form_source):
    service, draft, _ = form_source
    form = service.form(draft["draft_id"], 1)
    (Path(draft["source"]["snapshot_path"]) / "worker.py").write_text("def other(): pass\n")
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        resolve(service, form, answers(form, **{"Expected test result": {"value": 2}}))


def test_memory_questions_obey_existing_draft_requirements(form_source):
    service, draft, _ = form_source
    updated = service.imports.revise(draft["draft_id"], 1, requirements={"memory_mb": 64})
    form = service.form(updated["draft_id"], 2)
    memory = next(field for field in form["fields"] if field["label"] == "Memory budget (MiB)")
    assert memory["value"] == 64 and memory["constraints"] == {"minimum": 64, "maximum": 64}
    value, _ = resolve(service, form, answers(form, **{"Expected test result": {"value": 2}}))
    assert value.resources.memory_mb == 64
    service.imports.revise(draft["draft_id"], 2, requirements={"memory_mb": 32})
    unsupported = service.form(draft["draft_id"], 3)
    assert "unsupported_resource_requirement" in unsupported["blockers"]


def test_required_recipe_schema_without_editable_field_has_explicit_blocker(form_source, tmp_path):
    service, draft, _ = form_source
    scaffold_recipe(tmp_path / "incomplete", "test.incomplete")
    path = tmp_path / "incomplete" / "mcpme.recipe.json"
    manifest = json.loads(path.read_text())
    manifest["input_schema"]["properties"]["mode"] = {"type": "string"}
    manifest["input_schema"]["required"].append("mode")
    path.write_text(json.dumps(manifest))
    relock_recipe(tmp_path / "incomplete")
    record = service.recipes.add(tmp_path / "incomplete")
    service.recipes.enable(record["recipe_id"], record["version"])
    form = service.form(draft["draft_id"], 1, recipe={"id": record["recipe_id"],
        "version": record["version"], "digest": record["digest"]})
    assert "recipe_form_incomplete" in form["blockers"]
    with pytest.raises(PreparationError, match="recipe_form_incomplete"):
        resolve(service, form, answers(form, **{"Expected test result": {"value": 2}}))


def test_recipe_addition_is_form_additive_without_provider_or_dispatch_edits(form_source, tmp_path):
    service, draft, _ = form_source
    first = service.form(draft["draft_id"], 1)
    scaffold_recipe(tmp_path / "new-recipe", "independent.variant")
    second = service.recipes.add(tmp_path / "new-recipe")
    service.recipes.enable(second["recipe_id"], second["version"])
    ambiguous = service.form(draft["draft_id"], 1)
    assert ambiguous["recipe"] is None and "recipe_selection_required" in ambiguous["blockers"]
    assert len(ambiguous["recipe_choices"]) == 2
    selection = {"id": second["recipe_id"], "version": second["version"], "digest": second["digest"]}
    selected = service.form(draft["draft_id"], 1, recipe=selection)
    value, _ = resolve(service, selected, answers(selected, **{"Expected test result": {"value": 2}}))
    with pytest.raises(PreparationError, match="stale_preparation_form"):
        resolve(service, first, answers(first, **{"Expected test result": {"value": 2}}))
    refreshed = service.form(draft["draft_id"], 1, recipe=first["recipe"])
    assert value.recipe.id == "independent.variant" and value.digest != resolve(service, refreshed,
        answers(refreshed, **{"Expected test result": {"value": 2}}))[0].digest


def test_saved_candidate_and_recipe_remain_selected_among_multiple_choices(form_source, tmp_path):
    service, draft, _ = form_source
    source = Path(draft["source"]["locator"])
    with (source / "worker.py").open("a") as stream:
        stream.write("def triple(n: int) -> dict[str, int]:\n    return {'value': n * 3}\n")
    multi = service.imports.create(source, name="Multiple choices", intent="Choose a transformation")
    scaffold_recipe(tmp_path / "variant", "test.variant")
    record = service.recipes.add(tmp_path / "variant")
    service.recipes.enable(record["recipe_id"], record["version"])
    ambiguous = service.form(multi["draft_id"], 1)
    assert ambiguous["candidate_id"] is None and len(ambiguous["candidate_choices"]) == 2
    selected = service.form(multi["draft_id"], 1, candidate_id=ambiguous["candidate_choices"][0]["id"],
        recipe={"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]})
    value, _ = resolve(service, selected, answers(selected, **{"Expected test result": {"value": 2}}))
    updated = service.store.select_descriptor(multi["draft_id"], 1, value.model_dump(exclude_none=True),
        candidate_id=selected["candidate_id"], form_digest=selected["form_digest"])
    reloaded = service.form(multi["draft_id"], updated["revision"])
    assert reloaded["candidate_id"] == selected["candidate_id"] and reloaded["recipe"] == selected["recipe"]
    assert reloaded["blockers"] == [] and len(reloaded["recipe_choices"]) == 2


@pytest.mark.parametrize("kind,value", [("integer", True), ("number", False), ("boolean", 1),
    ("string", 1), ("object", []), ("choice", "unlisted")])
def test_widget_types_never_coerce_json_primitives(kind, value):
    fields = {"id": "a" * 64, "label": "Synthetic", "kind": kind, "required": True}
    if kind == "object":
        fields["schema"] = {"type": "object"}
    if kind == "choice":
        fields["choices"] = [{"id": "b" * 64, "label": "Only choice"}]
    field = FormField.model_validate(fields)
    with pytest.raises(PreparationError, match="invalid_preparation_form_answer"):
        field.accept(value)


def test_object_byte_bounds_and_file_selection_semantics():
    field = FormField.model_validate({"id": "a" * 64, "label": "Synthetic", "kind": "object",
        "required": True, "schema": {"type": "object"}})
    exact = {"v": "x" * (16384 - len(b'{"v":""}'))}
    assert field.accept(exact) == exact
    with pytest.raises(PreparationError, match="preparation_form_limit"):
        field.accept({"v": exact["v"] + "x"})
    selected = FormField.model_validate({"id": "b" * 64, "label": "Local file", "kind": "file", "required": True})
    assert selected.accept("/synthetic/weights.onnx") == "/synthetic/weights.onnx"
    with pytest.raises(PreparationError, match="invalid_preparation_form_answer"):
        selected.accept("weights.onnx")


def test_form_shape_rejects_unknown_fields_versions_and_duplicate_ids(form_source):
    service, draft, _ = form_source
    form = service.form(draft["draft_id"], 1)
    for change in ({"schema_version": 2}, {"executable_ui": "hook"}):
        with pytest.raises((ValidationError, PreparationError)):
            OnboardingFormV1.model_validate({**form, **change})
    bad = copy.deepcopy(form)
    bad["fields"].append(bad["fields"][0])
    with pytest.raises((ValidationError, PreparationError), match="invalid_preparation_form"):
        OnboardingFormV1.model_validate(bad)
    identity = contract_digest("preparation-onboarding-form-v1", {key: value for key, value in form.items() if key != "form_digest"})
    assert identity == form["form_digest"]
