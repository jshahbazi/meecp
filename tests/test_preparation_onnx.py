from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid
from dataclasses import dataclass

import pytest

from mcpme import preparation_onnx as onnx
from mcpme.contracts import Access
from mcpme.preparation_artifacts import CapturedArtifactCollectionV1
from mcpme.preparation_components import parse_component_descriptor
from mcpme.preparation_contracts import PreparationError


ROOT = Path(__file__).resolve().parents[1]


def collection(**model_changes):
    license_hash = onnx.MODEL_LICENSE_SHA256
    model = {"name": "weights", "path": "files/weights", "role": "model-weights", "format": "onnx",
             "sha256": onnx.MODEL_SHA256, "size": onnx.MODEL_BYTES, "origin": {},
             "licenses": [{"name": "license", "sha256": license_hash}]}
    model.update(model_changes)
    return CapturedArtifactCollectionV1(entries=[
        {"name": "license", "path": "files/license", "role": "license", "format": "text",
         "sha256": license_hash, "size": 34523, "origin": {}}, model])


def source(value):
    return {"draft_id": str(uuid.uuid4()), "revision": 1, "requirements": {}, "source": {
        "kind": "captured_artifact_collection", "revision": "sha256:" + value.digest,
        "digest": value.digest, "collection": value.model_dump(mode="json"), "files": []}}


def descriptor(draft):
    return parse_component_descriptor(onnx.onnx_hotdog_descriptor(draft, {
        "manifest": {"id": "test.onnx", "version": "1.0.0"}, "digest": "a" * 64}))


def test_pinned_source_binds_original_collection_without_inventing_source_lock():
    value = collection()
    draft = source(value)
    binding = onnx.OnnxHotdogComponent().validate_source(descriptor(draft), draft, {})
    assert binding.source_revision == "sha256:" + value.digest
    assert binding.dependency_lock_digest is None
    assert [(item.path, item.sha256, item.size) for item in binding.files] == [
        (item.path, item.sha256, item.size) for item in value.entries]
    assert json.loads(binding.metadata_bytes)["publisher_trust"] == "publisher_unverified"
    assert descriptor(draft).access == Access()


@pytest.mark.parametrize("change", [{"sha256": "0" * 64}, {"size": onnx.MODEL_BYTES - 1}, {"format": "binary"}])
def test_wrong_export_fails_eligibility_without_host_onnx_import(change):
    with pytest.raises(PreparationError, match="onnx_model_identity_unsupported"):
        onnx.validate_captured_model(collection(**change))


def test_matching_model_without_exact_agpl_license_is_rejected():
    value = collection().model_dump(mode="json")
    value["entries"][0]["sha256"] = "0" * 64
    value["entries"][1]["licenses"][0]["sha256"] = "0" * 64
    with pytest.raises(PreparationError, match="onnx_model_license_required"):
        onnx.validate_captured_model(CapturedArtifactCollectionV1.model_validate(value))


def test_manifest_labels_are_not_model_proof():
    value = collection().model_dump(mode="json")
    value["entries"][1]["role"] = "runtime-file"
    with pytest.raises(PreparationError, match="onnx_artifact_selection_unsupported"):
        onnx.validate_captured_model(CapturedArtifactCollectionV1.model_validate(value))


@pytest.mark.parametrize("mutation", ["digest", "revision", "kind"])
def test_source_collection_binding_rejects_tampered_reference(mutation):
    draft = source(collection())
    draft["source"][mutation] = "0" * 64
    with pytest.raises(PreparationError):
        onnx.captured_collection(draft)


def test_folder_supplement_preserves_original_folder_revision():
    value = collection(); draft = source(value)
    draft["captured_artifacts"] = {"kind": "captured_artifact_collection", "digest": value.digest,
                                  "collection": value.model_dump(mode="json")}
    draft["source"] = {"kind": "local_folder", "revision": "sha256:" + "b" * 64, "files": []}
    binding = onnx.OnnxHotdogComponent().validate_source(descriptor(draft), draft, {})
    assert binding.source_revision == "sha256:" + "b" * 64
    assert json.loads(binding.metadata_bytes)["collection_digest"] == value.digest


@pytest.mark.parametrize("field,value", [("memory_mb", 511), ("max_sessions", 2), ("max_request_bytes", 1024),
                                        ("operation_timeout_seconds", 59)])
def test_reduced_measured_resource_contract_is_mandatory(field, value):
    draft = source(collection()); raw = descriptor(draft).model_dump(exclude_none=True)
    raw["resources" if field == "memory_mb" else "lifecycle"][field] = value
    with pytest.raises(PreparationError, match="onnx_resource_contract_mismatch"):
        onnx.OnnxHotdogComponent().validate_source(parse_component_descriptor(raw), draft, {})


def test_schema_and_fixtures_are_exact_host_choices():
    draft = source(collection()); raw = descriptor(draft).model_dump(exclude_none=True)
    raw["tools"][0]["description"] = "Changed unreviewed surface"
    with pytest.raises(PreparationError, match="onnx_task_schema_mismatch"):
        onnx.OnnxHotdogComponent().validate_source(parse_component_descriptor(raw), draft, {})
    raw = descriptor(draft).model_dump(exclude_none=True)
    raw["validation"][0]["expected"]["threshold"] = .4
    with pytest.raises(PreparationError, match="onnx_fixture_contract_mismatch"):
        onnx.OnnxHotdogComponent().validate_source(parse_component_descriptor(raw), draft, {})


def test_task_ref_identity_binds_all_actual_implementation_assets(tmp_path, monkeypatch):
    before = onnx.component_reference()
    from shutil import copytree
    copied = tmp_path / "assets"
    copytree(ROOT / "templates/onnx-hotdog-v1", copied, ignore=lambda _, names: [name for name in names if name == ".venv"])
    (copied / "detector.py").write_bytes((copied / "detector.py").read_bytes() + b"\n# changed fixture\n")
    monkeypatch.setattr(onnx, "_asset_root", lambda: copied)
    assert onnx.component_reference().digest != before.digest


def test_pinned_notices_and_model_provenance_match_current_export():
    root = ROOT / "templates/onnx-hotdog-v1"
    assert hashlib.sha256((root / "LICENSE-MODEL").read_bytes()).hexdigest() == onnx.MODEL_LICENSE_SHA256
    info = json.loads((root / "model-provenance.json").read_bytes())
    assert (info["onnx"]["sha256"], info["onnx"]["bytes"]) == (onnx.MODEL_SHA256, onnx.MODEL_BYTES)
    assert json.loads((root / "model-metadata.json").read_bytes())["metadata"]["task"] == "detect"
    assert "mcp==1.30.0" in (root / "pyproject.toml").read_text()


def test_immutable_builtin_recipe_uses_exact_host_component(tmp_path):
    bundle = onnx.write_onnx_recipe(tmp_path.resolve() / "recipe")
    assert bundle["manifest"]["id"] == "mcpme.onnx-hotdog"
    assert bundle["manifest"]["steps"] == [{"component":"onnx-hotdog-v1","version":"1.0.0",
                                           "digest":onnx.component_reference().digest,"config":{}}]
    assert bundle["manifest"]["input_schema"] == {"type":"object","additionalProperties":False}
    with pytest.raises(PreparationError, match="recipe_destination_exists"):
        onnx.write_onnx_recipe(tmp_path.resolve() / "recipe")


def test_job_reference_release_is_explicit_separate_from_builder():
    recorded = []
    class Ledger:
        def release_reference(self, digest, reference):
            recorded.append((digest, reference))
    job = str(uuid.uuid4())
    onnx.release_onnx_inputs(Ledger(), job, "a" * 64)
    assert recorded[0][1].kind == "job" and recorded[0][1].owner_id == job


@pytest.mark.asyncio
async def test_actual_captured_fd_copy_build_and_job_pin_survive_original_removal(tmp_path, monkeypatch):
    from mcpme.imports import ImportStore
    from mcpme.preparation_artifacts import AcquisitionRequestV1, ArtifactStore
    from mcpme.preparation_capture import (ArtifactCaptureCoordinator, ArtifactCaptureStore,
        CapturePolicyV1, CreateImportTargetV1)
    directory = tmp_path.resolve()
    original = directory / "selected"; original.mkdir()
    model = b"Synthetic ONNX bytes, never parsed or executed.\n"
    (original / "weights.onnx").write_bytes(model)
    (original / "LICENSE").write_bytes((ROOT / "templates/onnx-hotdog-v1/LICENSE-MODEL").read_bytes())
    monkeypatch.setattr(onnx, "MODEL_SHA256", hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(onnx, "MODEL_BYTES", len(model))
    imports = ImportStore(directory / "state")
    ledger = ArtifactCaptureStore(imports, ArtifactStore(directory / "objects"))
    def admit(target, collection, capture_id):
        value = imports._admit_captured(collection, choices=target.model_dump(mode="json", exclude={"kind"}))
        return {"import_id": value["draft_id"], "revision": value["revision"]}
    coordinator = ArtifactCaptureCoordinator(ledger, admit_target=admit)
    request = AcquisitionRequestV1(entries=[
        {"name":"weights","role":"model-weights","path":str(original / "weights.onnx"),"format":"onnx","licenses":["license"]},
        {"name":"license","role":"license","path":str(original / "LICENSE"),"format":"text"}])
    queued = await coordinator.start(request, CapturePolicyV1(disk_ceiling_bytes=1024*1024),
                                     CreateImportTargetV1(name="Synthetic detector"), "capture-synthetic")
    await coordinator.tasks[queued["capture_id"]]
    completed = ledger.get(queued["capture_id"])
    assert completed["state"] == "succeeded"
    draft = imports._get(completed["result"]["import_id"])
    from shutil import rmtree
    rmtree(original)
    class Private:
        root = directory / "host/preparation-toolchain"
        identity = "f" * 64
    from shutil import copytree
    copytree(ROOT / "templates/onnx-hotdog-v1", Private.root / "templates/onnx-hotdog-v1",
             ignore=lambda _, names: [name for name in names if name == ".venv"])
    monkeypatch.setattr(onnx, "load_private_toolchain", lambda *args: Private())
    def runtime(destination, *args):
        path = destination / "python/bin/python3.12"; path.parent.mkdir(parents=True)
        path.write_bytes(b"Synthetic executable, never launched"); path.chmod(0o700)
        (destination / "licenses/python-LICENSE.txt").write_text("Synthetic PSF notice\n")
        (destination / "licenses/DEPENDENCIES.md").write_text("Synthetic dependency notice\n")
        return "Python 3.12.13"
    monkeypatch.setattr(onnx, "_runtime", runtime)
    @dataclass
    class SyntheticSpec:
        argv: tuple; cwd: Path; env: dict; timeout_seconds: int; operation: str; memory_mb: int
    monkeypatch.setattr(onnx, "CommandSpec", SyntheticSpec)
    job = str(uuid.uuid4()); calls = []
    def run(command):
        calls.append(command)
        assert command.operation == "onnx_model_validation"
        assert command.argv[0] == str(directory / "package/python/bin/python3.12")
        assert command.env["UV_PYTHON_DOWNLOADS"] == "never"
        assert ledger.references(completed["collection_digest"]) == [
            {"kind":"draft","owner_id":draft["draft_id"],"owner_revision":1},
            {"kind":"job","owner_id":job,"owner_revision":0}]
        return json.dumps({"schema_version":1,"model_valid":True,"blank_images":3,"controlled_tensor_cases":6,
            "max_image_cases":7,"max_input_bytes":1024*1024,"max_pixels":1024*1024,"boundary_rejections":3,
            "peak_rss_bytes":100,"max_inference_seconds":.001})
    package = onnx.prepare_onnx_bound(descriptor(draft), draft, Private(), directory / "package", run, lambda: None,
                                    capture_store=ledger, job_id=job, acquire_wheels=lambda *args: None)
    assert len(calls) == 1
    assert (package.root / "captured/files/weights").read_bytes() == model
    assert (package.root / "source/model.onnx").read_bytes() == model
    assert {entry.sha256 for entry in onnx.captured_collection(draft).entries} <= {item.sha256 for item in package.lock.artifacts}
    family = onnx.OnnxPreparationFamily(ledger)
    assert family.validate_inputs(descriptor(draft), draft).digest == completed["collection_digest"]
    from mcpme.recipes import RecipeStore, builtin_components
    from mcpme.preparations import PreparationStore
    from mcpme.preparation_forms import PreparationForms
    recipes = RecipeStore(imports, component_catalog={**builtin_components(), **onnx.builtin_onnx_components()})
    onnx.write_onnx_recipe(directory / "recipe")
    recipe = recipes.add(directory / "recipe")
    recipe = recipes.enable(recipe["recipe_id"],recipe["version"])
    assert recipe["blockers"] == []
    store = PreparationStore(imports,recipes,families=(family,))
    forms = PreparationForms(store)
    form = forms.form(draft["draft_id"],1)
    assert form["blockers"] == [] and form["execution_available"] is False
    resolved,context = forms.resolve(draft["draft_id"],1,candidate_id=form["candidate_id"],recipe=form["recipe"],
                                    form_digest=form["form_digest"],answers={})
    current = store.select_descriptor(draft["draft_id"],1,resolved,candidate_id=form["candidate_id"],form_digest=form["form_digest"])
    assert current["revision"] == 2
    bound = family.bound_inputs(resolved,current,recipe,store.components,"c"*64)
    assert bound.descriptor_version == 2 and bound.captured_collection.digest == completed["collection_digest"]
    assert bound.original_dependency_locks == () and bound.runtimes[0].digest == "c"*64
    family.release(job, resolved)
    assert all(row["kind"] != "job" for row in ledger.references(completed["collection_digest"]))
    await coordinator.close(); imports.close()


def test_form_respects_declared_memory_requirement_and_rejects_impossible_budget():
    draft = source(collection()); draft["requirements"] = {"memory_mb":1024}
    fields,bindings,blockers = onnx.OnnxHotdogFormProvider().fields(draft,{}, {})
    memory = next(item for item in fields if bindings[item.id][0] == "memory")
    assert memory.constraints == {"minimum":512,"maximum":1024} and not blockers
    draft["requirements"]["memory_mb"] = 128
    _,_,blockers = onnx.OnnxHotdogFormProvider().fields(draft,{}, {})
    assert blockers == ["resource_requirement_mismatch"]


def test_captured_copy_cancellation_checks_each_chunk_and_keeps_content_identity(tmp_path):
    import contextlib, os
    data = b"a" * (2 * 1024 * 1024)
    path = tmp_path / "source"; path.write_bytes(data)
    class Reader:
        @contextlib.contextmanager
        def open_artifact(self, *args):
            descriptor = os.open(path, os.O_RDONLY)
            try: yield descriptor
            finally: os.close(descriptor)
    calls = 0
    def canceled():
        nonlocal calls
        calls += 1
        if calls == 2: raise PreparationError("preparation_canceled")
    from types import SimpleNamespace
    entry = SimpleNamespace(name="weights",size=len(data),sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(PreparationError, match="preparation_canceled"):
        onnx._captured_copy(Reader(), SimpleNamespace(digest="a"*64), entry, tmp_path / "target", canceled)
    assert (tmp_path / "target").stat().st_size == 1024 * 1024


def test_family_inspection_reports_wrong_export_blocker_without_runtime_execution():
    from types import SimpleNamespace
    value = collection(sha256="0"*64)
    inspected = onnx.OnnxPreparationFamily(SimpleNamespace()).inspect_source(source(value))
    assert inspected["candidates"] == []
    assert inspected["blockers"] == ["onnx_model_identity_unsupported"]
