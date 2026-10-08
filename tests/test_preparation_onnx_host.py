"""The cold host composes ONNX metadata using its real capture ownership store."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from meecp import preparation_onnx as onnx
from meecp.preparation_artifacts import AcquisitionRequestV1
from meecp.preparation_capture import AttachImportTargetV1, CapturePolicyV1, CreateImportTargetV1
from meecp.supervisor import Supervisor


@pytest.mark.parametrize("source_kind", ["captured", "folder"])
async def test_cold_host_captured_form_binds_original_files_and_separate_collection(tmp_path, monkeypatch, source_kind):
    selected = tmp_path.resolve() / "selected"
    selected.mkdir()
    model = b"Synthetic captured model; never parsed or executed.\n"
    (selected / "model.onnx").write_bytes(model)
    (selected / "LICENSE").write_bytes((Path(__file__).resolve().parents[1] /
        "templates/onnx-hotdog-v1/LICENSE-MODEL").read_bytes())
    monkeypatch.setattr(onnx, "MODEL_SHA256", hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(onnx, "MODEL_BYTES", len(model))
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("metadata executed selected source"))
    try:
        record = next(item for item in host.builtin_recipe_metadata["recipes"]
                      if item["recipe_id"] == "mcpme.onnx-hotdog")
        assert record["ready"] and record["recipe_state"] == "enabled"
        assert host.preparation_runner.enabled is False
        original_source = None
        if source_kind == "folder":
            folder = tmp_path.resolve() / "application"
            folder.mkdir()
            (folder / "README.md").write_text("Synthetic app directory.\n")
            draft = host.imports.create(folder, name="Directory detector")
            original_source = draft["source"]
            target = AttachImportTargetV1(import_id=draft["draft_id"], revision=draft["revision"])
        else:
            target = CreateImportTargetV1(name="Captured detector")
        request = AcquisitionRequestV1(entries=[
            {"name": "weights", "role": "model-weights", "format": "onnx",
             "path": str(selected / "model.onnx"), "licenses": ["license"]},
            {"name": "license", "role": "license", "format": "text", "path": str(selected / "LICENSE")}])
        job = await host.capture_runner.start(request, CapturePolicyV1(disk_ceiling_bytes=1024**2),
                                              target, "metadata-capture")
        await host.capture_runner.tasks[job["capture_id"]]
        completed = host.captures.get(job["capture_id"])
        assert completed["state"] == "succeeded" and completed["ownership_settled"]
        assert not host.capture_reservations
        draft = host.imports._get(completed["result"]["import_id"])
        if original_source is not None:
            assert draft["source"] == original_source
        for entry in request.entries:
            Path(entry.path).unlink()
        inspected = host.inspect_preparation_source(draft["draft_id"], draft["revision"])
        assert inspected["blockers"] == []
        assert inspected["execution_available"] is False and inspected["verified_capabilities"] == []
        assert inspected["candidates"][0]["verified"] is False
        assert [item["kind"] for item in inspected["candidates"]] == ["onnx-hotdog"]
        form = host.preparation_forms.form(draft["draft_id"], draft["revision"])
        assert not form["blockers"] and form["execution_available"] is False
        assert form["recipe"] == record["recipe"]
        descriptor, context = host.preparation_forms.resolve(draft["draft_id"], draft["revision"],
            candidate_id=form["candidate_id"], recipe=form["recipe"], form_digest=form["form_digest"], answers={})
        assert descriptor.package.name == draft["name"]
        revised = host.preparations.select_descriptor(draft["draft_id"], draft["revision"], descriptor,
            candidate_id=form["candidate_id"], form_digest=form["form_digest"])
        recipe = host.recipes.resolve(form["recipe"]["id"], form["recipe"]["version"], form["recipe"]["digest"])
        family = host.preparations.family_for(descriptor)
        bound = family.bound_inputs(descriptor, revised, recipe, host.preparations.components, "f" * 64)
        assert len(bound.components) == 2
        assert bound.captured_collection.digest == completed["collection_digest"]
        assert bound.source.revision == draft["source"]["revision"]
        assert len(bound.source_validations) == 2
        if original_source is not None:
            for validation in bound.source_validations:
                assert [{"path": item.path, "sha256": item.sha256, "size": item.size}
                        for item in validation.files] == original_source["files"]
        assert all(item["kind"] == "draft" for item in host.captures.references(completed["collection_digest"]))
    finally:
        await host.close()


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
async def test_added_onnx_seed_preserves_user_lifecycle_and_other_recipes_across_restart(tmp_path, action):
    state = tmp_path.resolve() / "state"
    host = Supervisor(state, tmp_path / "unused-reaper")
    try:
        legacy = {item["recipe_id"]: item for item in host.recipes.list() if item["recipe_id"] != "mcpme.onnx-hotdog"}
        before = getattr(host.recipes, action)("mcpme.onnx-hotdog", "1.0.0")
    finally:
        await host.close()
    restarted = Supervisor(state, tmp_path / "unused-reaper")
    try:
        assert restarted.recipes.inspect("mcpme.onnx-hotdog", "1.0.0") == before
        assert {item["recipe_id"]: item for item in restarted.recipes.list()
                if item["recipe_id"] != "mcpme.onnx-hotdog"} == legacy
        assert restarted.preparation_runner.enabled is False
        metadata = next(item for item in restarted.builtin_recipe_metadata["recipes"]
                        if item["recipe_id"] == "mcpme.onnx-hotdog")
        assert metadata["seed_state"] == "preserved" and metadata["ready"] is False
    finally:
        await restarted.close()
