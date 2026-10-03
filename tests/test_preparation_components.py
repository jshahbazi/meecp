"""Component routing is host-owned, identity-bound and language independent."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import FrozenInstanceError

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_components import (
    ComponentReference, ComponentRegistry, PythonSourceComponent, SourceBinding,
    SourceFileBinding, parse_component_descriptor, read_snapshot,
)
from mcpme.preparation_contracts import PreparationError, canonical_bytes, contract_digest, parse_descriptor
from test_mcpme_preparations import descriptor


@pytest.fixture
def source_binding(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "validator.py").write_text("raise RuntimeError('source was executed')\ndef validate_json(document: str) -> dict: return {'valid': True}\n")
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1.0.0'\n")
    lock = b"version = 1\nrevision = 3\n"
    (source / "uv.lock").write_bytes(lock)
    (source / "LICENSE").write_text("Fixture permission notice.\n")
    component = ComponentReference(id="python-function-v1", version="1.0.0", digest="a" * 64)
    recipe = {"recipe_id": "test.synthetic", "version": "1.0.0", "digest": "b" * 64,
              "manifest": {"id": "test.synthetic", "version": "1.0.0", "steps": [{
                  "component": component.id, "version": component.version, "digest": component.digest}]}}
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source, name="Fixture")
        value = parse_descriptor(descriptor(recipe, hashlib.sha256(lock).hexdigest()))
        yield component, recipe, draft, value


def test_v1_identity_and_static_checks_are_preserved(source_binding, monkeypatch):
    reference, recipe, draft, value = source_binding
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("static validation executed a process"))
    before = value.digest
    registry = ComponentRegistry((PythonSourceComponent(reference, interface_kind="python-callable"),))
    binding, = registry.validate_source(value, draft, recipe)
    assert value.schema_version == 1 and value.digest == before
    assert binding.source_revision == draft["source"]["revision"]
    assert binding.dependency_lock_digest == value.dependencies.lock_sha256
    assert {item.path for item in binding.files} == {"uv.lock", "pyproject.toml", "LICENSE", "validator.py"}
    with pytest.raises(FrozenInstanceError):
        binding.source_revision = "changed"
    with pytest.raises(FrozenInstanceError):
        binding.files[0].size = 1


@pytest.mark.parametrize("change,error", [
    (lambda value: value["dependencies"].update(lock_sha256="c" * 64), "dependency_lock_mismatch"),
    (lambda value: value.update(source_license="missing"), "source_license_required"),
    (lambda value: value["interface"].update(module="missing"), "entrypoint_source_required"),
])
def test_python_component_derives_existing_checks(source_binding, change, error):
    reference, recipe, draft, value = source_binding
    raw = value.model_dump(exclude_none=True)
    change(raw)
    with pytest.raises(PreparationError, match=error):
        ComponentRegistry((PythonSourceComponent(reference, interface_kind="python-callable"),)).validate_source(
            parse_descriptor(raw), draft, recipe)


@pytest.mark.parametrize("field", ["id", "version", "digest"])
def test_unknown_exact_component_identity_is_rejected(source_binding, field):
    reference, recipe, draft, value = source_binding
    altered = copy.deepcopy(recipe)
    altered["manifest"]["steps"][0]["component" if field == "id" else field] = {
        "id": "unknown-component", "version": "2.0.0", "digest": "f" * 64}[field]
    with pytest.raises(PreparationError, match="unknown_preparation_component"):
        ComponentRegistry((PythonSourceComponent(reference, interface_kind="python-callable"),)).validate_source(value, draft, altered)


def test_source_changes_and_unbound_tree_entries_are_rejected(source_binding):
    reference, recipe, draft, value = source_binding
    from pathlib import Path
    root = Path(draft["source"]["snapshot_path"])
    (root / "validator.py").write_text("changed bytes\n")
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        read_snapshot(draft)
    (root / "unbound.py").write_text("unbound source\n")
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        read_snapshot(draft)


def test_original_locator_mutation_does_not_change_snapshot_binding(source_binding):
    reference, recipe, draft, value = source_binding
    from pathlib import Path
    (Path(draft["source"]["locator"]) / "validator.py").write_text("raise RuntimeError('later original source')\n")
    binding, = ComponentRegistry((PythonSourceComponent(reference, interface_kind="python-callable"),)).validate_source(value, draft, recipe)
    assert binding.source_revision == draft["source"]["revision"]


def v2_descriptor(reference):
    return {"schema_version": 2, "recipe": {"id": "test.native", "version": "1.0.0", "digest": "b" * 64},
            "package": {"id": "test.native", "version": "1.0.0", "name": "Native fixture"},
            "components": [{"component": reference.model_dump(), "input": {"entrypoint": "worker.swift"}}],
            "tools": [{"name": "calculate", "description": "Synthetic native fixture", "input_schema": {"type": "object"},
                       "output_schema": {"type": "object"}}], "resources": {"memory_mb": 128},
            "validation": [{"id": "synthetic", "tool": "calculate", "arguments": {}, "expected": {}, "synthetic": True}]}


class FakeNativeComponent:
    """Contract-only native fixture; no compiler/runtime capability is claimed."""
    reference = ComponentReference(id="native-fixture-v1", version="1.0.0", digest="d" * 64)
    input_schema = {"type": "object", "properties": {"entrypoint": {"type": "string"}},
                    "required": ["entrypoint"], "additionalProperties": False}
    legacy_interface_kinds = frozenset()

    def validate_source(self, descriptor, draft, recipe):
        entrypoint = descriptor.components[0].input["entrypoint"]
        file = read_snapshot(draft).by_path()[entrypoint]
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
                             (SourceFileBinding(file.path, file.sha256, file.size),),
                             contract_digest("native-interface-v1", [tool.model_dump() for tool in descriptor.tools]),
                             metadata_bytes=canonical_bytes({"family": "native-contract-fixture"}))


def test_non_python_component_has_no_python_module_lock_or_license_assumptions(tmp_path):
    source = tmp_path / "native"
    source.mkdir()
    (source / "worker.swift").write_text("fatalError(\"must never execute\")\n")
    component = FakeNativeComponent()
    raw = v2_descriptor(component.reference)
    assert all(field not in raw for field in ("interface", "dependencies", "source_license"))
    value = parse_component_descriptor(raw)
    recipe = {"digest": value.recipe.digest, "manifest": {"id": value.recipe.id, "version": value.recipe.version,
              "steps": [{"component": component.reference.id, "version": component.reference.version,
                         "digest": component.reference.digest}]}}
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source, name="Native fixture")
        binding, = ComponentRegistry((component,)).validate_source(value, draft, recipe)
        assert binding.dependency_lock_digest is None
        assert binding.files[0].path == "worker.swift"
        assert value.digest == contract_digest("preparation-descriptor-v2", value.model_dump(exclude_none=True))


@pytest.mark.parametrize("change,error", [
    (lambda raw: raw.update(arbitrary_shell="echo unsafe"), "invalid_preparation_descriptor"),
    (lambda raw: raw["components"].append(copy.deepcopy(raw["components"][0])), "duplicate_preparation_component"),
])
def test_v2_envelope_is_closed_and_exact(change, error):
    raw = v2_descriptor(FakeNativeComponent.reference)
    change(raw)
    with pytest.raises(PreparationError, match=error):
        parse_component_descriptor(raw)


def test_component_input_schema_rejects_unknown_input(tmp_path):
    component = FakeNativeComponent()
    raw = v2_descriptor(component.reference)
    raw["components"][0]["input"]["shell"] = "arbitrary"
    value = parse_component_descriptor(raw)
    recipe = {"digest": value.recipe.digest, "manifest": {"id": value.recipe.id, "version": value.recipe.version,
              "steps": [{"component": component.reference.id, "version": component.reference.version,
                         "digest": component.reference.digest}]}}
    with pytest.raises(PreparationError, match="component_input_mismatch"):
        ComponentRegistry((component,)).validate_source(value, {"source": {"revision": "sha256:" + "e" * 64}}, recipe)


def test_binding_cannot_hide_mutable_or_noncanonical_data():
    with pytest.raises(PreparationError, match="component_binding_mismatch"):
        SourceBinding("native-fixture-v1", "1.0.0", "d" * 64, "sha256:" + "e" * 64, [], "f" * 64)
    with pytest.raises(PreparationError, match="component_binding_mismatch"):
        SourceBinding("native-fixture-v1", "1.0.0", "d" * 64, "sha256:" + "e" * 64, (), "f" * 64,
                      metadata_bytes=b'{ "family": "native" }')
