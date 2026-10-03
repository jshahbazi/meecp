"""Cold-host text-generation admission tests use captured bytes only."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mcpme import preparation_textgen as textgen
from mcpme.preparation_artifacts import AcquisitionRequestV1
from mcpme.preparation_capture import AttachImportTargetV1, CapturePolicyV1, CreateImportTargetV1
from mcpme.supervisor import Supervisor


@pytest.mark.parametrize("source_kind", ["captured", "folder"])
async def test_cold_host_textgen_capture_preserves_source_and_form_identity(tmp_path, monkeypatch, source_kind):
    selected = tmp_path.resolve() / "selected"
    selected.mkdir()
    model = b"Synthetic text model bytes; never parsed or executed.\n"
    (selected / "model.gguf").write_bytes(model)
    (selected / "LICENSE").write_bytes((Path(__file__).resolve().parents[1] /
        "templates/text-generate-v1/LICENSE-MODEL").read_bytes())
    monkeypatch.setattr(textgen, "MODEL_SHA256", hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(textgen, "MODEL_BYTES", len(model))
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    try:
        record = next(item for item in host.builtin_recipe_metadata["recipes"]
                      if item["recipe_id"] == "mcpme.text-generation")
        assert record["ready"] and record["recipe_state"] == "enabled"
        assert host.preparation_runner.enabled is False
        original_source = None
        if source_kind == "folder":
            folder = tmp_path.resolve() / "application"
            folder.mkdir()
            (folder / "README.md").write_text("Synthetic text app directory.\n")
            draft = host.imports.create(folder, name="Text generation application")
            original_source = draft["source"]
            target = AttachImportTargetV1(import_id=draft["draft_id"], revision=draft["revision"])
        else:
            target = CreateImportTargetV1(name="Captured text generation")
        request = AcquisitionRequestV1(entries=[
            {"name": "weights", "role": "model-weights", "format": "gguf",
             "path": str(selected / "model.gguf"), "licenses": ["license"]},
            {"name": "license", "role": "license", "format": "text", "path": str(selected / "LICENSE")}])
        job = await host.capture_runner.start(request, CapturePolicyV1(disk_ceiling_bytes=1024**2),
                                              target, "metadata-textgen")
        await host.capture_runner.tasks[job["capture_id"]]
        completed = host.captures.get(job["capture_id"])
        assert completed["state"] == "succeeded" and completed["ownership_settled"]
        draft = host.imports._get(completed["result"]["import_id"])
        if original_source is not None:
            assert draft["source"] == original_source
        for entry in request.entries:
            Path(entry.path).unlink()
        inspected = host.inspect_preparation_source(draft["draft_id"], draft["revision"])
        assert inspected["blockers"] == []
        assert inspected["execution_available"] is False and inspected["verified_capabilities"] == []
        assert inspected["candidates"][0]["verified"] is False
        assert [item["kind"] for item in inspected["candidates"]] == ["llamacpp-text-generation"]
        form = host.preparation_forms.form(draft["draft_id"], draft["revision"])
        assert not form["blockers"] and form["execution_available"] is False
        assert form["recipe"] == record["recipe"]
        assert form["fields"][0]["value"] == draft["name"]
        descriptor, _ = host.preparation_forms.resolve(
            draft["draft_id"], draft["revision"], candidate_id=form["candidate_id"],
            recipe=form["recipe"], form_digest=form["form_digest"], answers={})
        assert descriptor.package.name == draft["name"]
        assert host.preparation_runner.execution_available_for_recipe(record) is False
        explicit = host.preparation_runner.enabled_families
        assert textgen.TextGenerationFamily(None).component.reference.key not in explicit
        host.preparation_runner.enabled = True
        form_enabled = lambda: host.preparation_forms.form(draft["draft_id"], draft["revision"], execution_available=True)
        assert form_enabled()["execution_available"] is False
        onnx_family = next(f for f in host.preparations.families.values()
                           if f.component.reference.id == "yolo11-coco-detect-v1")
        host.preparation_runner.enabled_families = frozenset({onnx_family.component.reference.key})
        assert form_enabled()["execution_available"] is False
        host.preparation_runner.enabled_families = frozenset({textgen.references()[1].key})
        assert form_enabled()["execution_available"] is True
        host.preparations.select_descriptor(draft["draft_id"], draft["revision"], descriptor,
            candidate_id=form["candidate_id"], form_digest=form["form_digest"])
    finally:
        await host.close()


async def test_textgen_explicit_family_admission_is_separate_from_global_runner(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    try:
        family = next(f for f in host.preparations.families.values()
                      if f.component.reference.id == "text-generate-v1")
        assert host.preparation_runner.enabled is False
        host.preparation_runner.enabled_families = frozenset({family.component.reference.key})
        assert host.preparation_runner.execution_available_for_recipe({
            "manifest": {"steps": [{"component": c.reference.id, "version": c.reference.version,
                                      "digest": c.reference.digest} for c in family.components]}}) is False
        host.preparation_runner.enabled = True
        assert host.preparation_runner.execution_available_for_recipe({
            "manifest": {"steps": [{"component": c.reference.id, "version": c.reference.version,
                                      "digest": c.reference.digest} for c in family.components]}}) is True
    finally:
        await host.close()


@pytest.mark.parametrize("action", ["disable", "revoke"])
async def test_textgen_recipe_lifecycle_survives_restart(tmp_path, action):
    state = tmp_path.resolve() / "state"
    host = Supervisor(state, tmp_path / "unused-reaper")
    try:
        recipe = next(item for item in host.recipes.list() if item["recipe_id"] == "mcpme.text-generation")
        changed = getattr(host.recipes, action)(recipe["recipe_id"], recipe["version"])
    finally:
        await host.close()
    restarted = Supervisor(state, tmp_path / "unused-reaper")
    try:
        assert restarted.recipes.inspect(recipe["recipe_id"], recipe["version"]) == changed
        assert restarted.recipes.inspect(recipe["recipe_id"], recipe["version"])["state"] in {"disabled", "revoked"}
        assert restarted.preparation_runner.enabled is False
    finally:
        await restarted.close()
