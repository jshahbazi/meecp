import copy
import hashlib
import json
from pathlib import Path
import uuid
from types import SimpleNamespace

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


@pytest.mark.parametrize("name", ["preparation_textgen_verifier.py", "preparation_contracts.py", "recipes.py",
                                   "imports.py", "ownership_receipts.py", "mcpme_local_transport.py"])
def test_verifier_implementation_changes_component_identity(tmp_path, monkeypatch, name):
    before = textgen.references()
    original = textgen._read
    def changed(path, limit):
        data = original(path, limit)
        return data + b"\n# identity test\n" if path.name == name else data
    monkeypatch.setattr(textgen, "_read", changed)
    assert all(a.digest != b.digest for a, b in zip(before, textgen.references()))


def test_generic_store_selects_full_text_composition_and_static_candidate(tmp_path, monkeypatch):
    from mcpme.imports import ImportStore
    from mcpme.preparations import PreparationStore
    from mcpme.recipes import RecipeStore
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("static inspection launched a process"))
    family = textgen.TextGenerationFamily(None)
    with ImportStore(tmp_path / "state") as imports:
        store = PreparationStore(imports, RecipeStore(imports), families=(family,))
        source = draft(); value = descriptor(source)
        assert store.family_for(value) is family
        inspection = store.inspect_source(source)
        assert not inspection["blockers"]
        assert [c["kind"] for c in inspection["candidates"]] == ["llamacpp-text-generation"]
        candidate = inspection["candidates"][0]
        assert family.form_provider.accepts(candidate, recipe())
        fields, bindings, blockers = family.form_provider.fields(source, candidate, recipe())
        assert not blockers and len(fields) == len(bindings) == 2
        raw = value.model_dump(exclude_none=True)
        raw["components"].pop()
        with pytest.raises(PreparationError, match="unknown_preparation_component"):
            store.family_for(parse_component_descriptor(raw))


def candidate_metadata(tmp_path):
    """Extra family-contract checks; package byte hashing is tested separately."""
    from mcpme.contracts import ModelArtifact, ManagedProcess
    source = draft(); value = descriptor(source)
    manifest = SimpleNamespace(profile="model-service", adapter_process="adapter",
        model=ModelArtifact.model_validate(textgen.model_contract()),
        processes=[ManagedProcess.model_validate(p) for p in textgen.PROCESS_GRAPH],
        tools=value.tools, lifecycle=value.lifecycle, access=value.access, resources=value.resources,
        package_id=value.package.id, version=value.package.version, name=value.package.name,
        source_revision=source["source"]["revision"])
    closure = json.loads(textgen.assets()["engine-closure.json"])
    records = {name: SimpleNamespace(path=name, sha256=entry["sha256"], size=entry["bytes"])
               for name, entry in closure.items() if name != "llama/llama-server"}
    records["models/" + textgen.MODEL_FILENAME] = SimpleNamespace(path="models/" + textgen.MODEL_FILENAME,
        sha256=textgen.MODEL_SHA256, size=textgen.MODEL_BYTES)
    records["licenses/Qwen-LICENSE"] = SimpleNamespace(path="licenses/Qwen-LICENSE", sha256=textgen.LICENSE_SHA256)
    backend = Path(textgen.__file__).resolve().parents[1]
    paths = [*sorted((backend / "mcpme_textgen").glob("*.py")),
             *[backend / "jevk5_app" / name for name in ("__init__.py", "server.py", "contracts.py", "state.py")]]
    for path in paths:
        name = "backend/" + path.relative_to(backend).as_posix()
        records[name] = SimpleNamespace(path=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (tmp_path / "capture.json").write_text(json.dumps(source["source"]["collection"]))
    (tmp_path / "origin.json").write_text(json.dumps({"engine_closure":closure,
        "launcher_source_sha256":hashlib.sha256(textgen.assets()["launcher.c"]).hexdigest()}))
    return SimpleNamespace(root=tmp_path, manifest=manifest, lock=SimpleNamespace(artifacts=list(records.values()))), value


def test_exact_extra_model_service_contract_is_accepted(tmp_path):
    package, value = candidate_metadata(tmp_path)
    textgen.validate_model_package(package, value)


@pytest.mark.parametrize("change", ["graph", "model", "engine", "extra_plugin", "adapter_code", "extra_code", "capture", "launcher"])
def test_base_manifest_match_cannot_replace_model_engine_adapter_or_capture(tmp_path, change):
    package, value = candidate_metadata(tmp_path)
    if change == "graph": package.manifest.processes.pop()
    if change == "model": package.manifest.model = package.manifest.model.model_copy(update={"context_tokens": 1024})
    if change == "engine":
        next(a for a in package.lock.artifacts if a.path == "llama/llama-server.real").sha256 = "0" * 64
    if change == "extra_plugin":
        package.lock.artifacts.append(SimpleNamespace(path="llama/plugins/extra.so", sha256="0" * 64, size=1))
    if change == "adapter_code":
        next(a for a in package.lock.artifacts if a.path == "backend/mcpme_textgen/adapter.py").sha256 = "0" * 64
    if change == "extra_code":
        package.lock.artifacts.append(SimpleNamespace(path="backend/extra.py", sha256="0" * 64, size=1))
    if change == "capture":
        content = json.loads((tmp_path / "capture.json").read_text())
        content["entries"][1]["sha256"] = "0" * 64
        (tmp_path / "capture.json").write_text(json.dumps(content))
    if change == "launcher":
        content = json.loads((tmp_path / "origin.json").read_text())
        content["launcher_source_sha256"] = "0" * 64
        (tmp_path / "origin.json").write_text(json.dumps(content))
    with pytest.raises(PreparationError): textgen.validate_model_package(package, value)
