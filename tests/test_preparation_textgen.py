import copy
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from mcpme import preparation_textgen as textgen
from mcpme.preparation_artifacts import CapturedArtifactCollectionV1
from mcpme.preparation_components import ComponentRegistry, parse_component_descriptor
from mcpme.preparation_contracts import PreparationError


def collection(**changes):
    model = {"name": "weights", "path": "files/weights", "role": "model-weights", "format": "gguf",
        "sha256": textgen.MODEL_SHA256, "size": textgen.MODEL_BYTES, "origin": {},
        "licenses": [{"name": "license", "sha256": textgen.LICENSE_SHA256}]}
    model.update(changes)
    return CapturedArtifactCollectionV1(entries=[
        {"name": "license", "path": "files/license", "role": "license", "format": "text",
         "sha256": textgen.LICENSE_SHA256, "size": len(textgen.assets()["LICENSE-MODEL"]), "origin": {}}, model])


def draft(value=None):
    value = value or collection()
    return {"draft_id": str(uuid.uuid4()), "revision": 1, "requirements": {}, "source": {
        "kind": "captured_artifact_collection", "contract_version": 1, "revision": "sha256:" + value.digest,
        "digest": value.digest, "collection": value.model_dump(mode="json"), "files": []}}


def recipe():
    return {"manifest": {"id": "test.textgen", "version": "1.0.0", "steps": [
        {"component": ref.id, "version": ref.version, "digest": ref.digest} for ref in textgen.references()]}, "digest": "a" * 64}


def descriptor(source):
    return parse_component_descriptor(textgen.textgen_descriptor(source, recipe()))


def test_component_composition_binds_two_exact_distinct_identities():
    source = draft(); family = textgen.TextGenerationFamily(None)
    bindings = ComponentRegistry(family.components).validate_source(descriptor(source), source, recipe())
    assert len(bindings) == 2 and bindings[0].component_key != bindings[1].component_key
    assert {b.component_key for b in bindings} == {ref.key for ref in textgen.references()}
    for binding in bindings:
        assert binding.dependency_lock_digest is None
        assert [b.path for b in binding.files] == [e.path for e in collection().entries]
        assert json.loads(binding.metadata_bytes)["collection_digest"] == collection().digest


@pytest.mark.parametrize("change", [{"sha256": "0" * 64}, {"format": "binary"}, {"size": 100}])
def test_exact_model_identity_required(change):
    with pytest.raises(PreparationError, match="textgen_model_identity_unsupported"):
        textgen.model_selection(collection(**change))


def test_license_and_closed_selection_are_required():
    raw = collection().model_dump(mode="json")
    raw["entries"][0]["sha256"] = raw["entries"][1]["licenses"][0]["sha256"] = "0" * 64
    with pytest.raises(PreparationError, match="textgen_model_identity_unsupported"):
        textgen.model_selection(CapturedArtifactCollectionV1.model_validate(raw))


@pytest.mark.parametrize("field", ["digest", "revision", "kind", "contract_version"])
def test_tampered_capture_reference_rejected(field):
    source = draft(); source["source"][field] = "invalid"
    with pytest.raises(PreparationError):
        textgen.captured_collection(source)


@pytest.mark.parametrize("change", ["component", "model", "bound", "tool", "fixture", "memory", "gpu", "lifecycle"])
def test_descriptor_cannot_broaden_reviewed_profile(change):
    source = draft(); value = descriptor(source).model_dump(exclude_none=True)
    if change == "component": value["components"].pop()
    if change == "model": value["components"][0]["input"]["model_name"] = "another"
    if change == "bound": value["components"][1]["input"]["max_output_tokens"] = 65
    if change == "tool": value["tools"][0]["description"] = "another"
    if change == "fixture": value["validation"][0]["expected"]["input_tokens"] += 1
    if change == "memory": value["resources"]["memory_mb"] = 1023
    if change == "gpu": value["resources"]["gpu"] = True
    if change == "lifecycle": value["lifecycle"]["startup_timeout_seconds"] = 30
    with pytest.raises((PreparationError, ValueError)):
        textgen.validate_descriptor(parse_component_descriptor(value), source)


def test_folder_supplement_binds_actual_original_snapshot(tmp_path):
    source = draft(); captured = source["source"]
    (tmp_path / "README.md").write_text("Synthetic model instructions.\n")
    data = (tmp_path / "README.md").read_bytes()
    records = [{"path": "README.md", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    source["source"] = {"kind": "local_folder", "snapshot_path": str(tmp_path), "files": records,
                        "digest": digest, "revision": "sha256:" + digest}
    source["captured_artifacts"] = {"schema_version": 1, "kind": captured["kind"], "digest": captured["digest"],
                                    "collection": captured["collection"]}
    family = textgen.TextGenerationFamily(None)
    assert family.supports_source(source)
    bindings = ComponentRegistry(family.components).validate_source(descriptor(source), source, recipe())
    assert all([item.path for item in binding.files] == ["README.md"] for binding in bindings)
    assert all(json.loads(binding.metadata_bytes)["collection_digest"] == captured["digest"] for binding in bindings)
    (tmp_path / "README.md").write_text("changed")
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        textgen.validate_descriptor(descriptor(source), source)


def test_recipe_has_only_the_two_pinned_components(tmp_path):
    bundle = textgen.write_textgen_recipe(tmp_path / "recipe")
    assert bundle["manifest"]["kind"] == "declarative"
    assert {step["component"] for step in bundle["manifest"]["steps"]} == {textgen.ENGINE_ID, textgen.TASK_ID}


def test_host_assets_bind_model_template_tokenizer_and_schema():
    from mcpme_textgen.service import model_declaration
    assert textgen.model_contract() == model_declaration()
    assert len(textgen.task_tools()) == 2
    assert len(textgen.task_fixtures()) == 1


def test_verifier_implementation_changes_component_identity(tmp_path, monkeypatch):
    before = textgen.references()
    original = textgen._read
    def changed(path, limit):
        data = original(path, limit)
        return data + b"\n# identity test\n" if path.name == "preparation_textgen_verifier.py" else data
    monkeypatch.setattr(textgen, "_read", changed)
    assert all(a.digest != b.digest for a, b in zip(before, textgen.references()))
