"""Common input identities preserve legacy objects and separate derivations."""
from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from mcpme.imports import ImportStore
from mcpme.preparation_components import ComponentRegistry, PythonSourceComponent, SourceBinding, SourceFileBinding
from mcpme.preparation_contracts import PreparationError, PreparationReceipt, canonical_bytes
from mcpme.preparation_inputs import (
    CollectionReference, DerivedInputReference, PreparationReceiptV2, RuntimeReference,
    SourceValidationReference, adapt_descriptor, bind_preparation_inputs,
    parse_bound_inputs, parse_preparation_receipt, validate_builder_result,
)
from test_preparation_components import FakeNativeComponent, source_binding, v2_descriptor  # noqa: F401


@pytest.fixture
def view(source_binding):
    reference, recipe, draft, descriptor = source_binding
    registry = ComponentRegistry((PythonSourceComponent(reference, interface_kind="python-callable"),))
    return adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry), draft


def collection(kind="captured_artifact_collection", digest="c" * 64):
    return CollectionReference(kind=kind, schema_version=1, digest=digest)


def bind(view, draft, **kwargs):
    return bind_preparation_inputs(view, draft=draft, captured_collection=collection(),
        expected_package_contract=view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio"), **kwargs)


def derived(view, *, name="resolved-lock", parents=None, **changes):
    binding = view.source_bindings[0]
    raw = {"name": name, "role": "dependency-lock", "collection_digest": "d" * 64,
        "sha256": "e" * 64, "size": 15,
        "producer": {"id": binding.component_id, "version": binding.component_version, "digest": binding.component_digest},
        "source_revision": binding.source_revision, "request_digest": "f" * 64, "policy_digest": "1" * 64,
        "parent_digests": parents or [binding.source_revision.removeprefix("sha256:")]}
    raw.update(changes)
    return DerivedInputReference.model_validate(raw)


def test_legacy_descriptor_golden_and_exact_object_preserved(source_binding):
    reference, recipe, draft, original = source_binding
    before = canonical_bytes(original.model_dump(exclude_none=True))
    assert original.digest == "01d2b4f0bf6ba3db1a13efc4ac2cc3122c2f5e66c6e6c1718cde2ee4c8c66d03"
    view = adapt_descriptor(original, draft=draft, recipe=recipe,
        registry=ComponentRegistry((PythonSourceComponent(reference, interface_kind="python-callable"),)))
    assert view.descriptor is original
    assert view.digest == original.digest
    assert view.tools[0] is original.interface.tools[0]
    assert view.package is original.package and view.access is original.access
    assert view.resources is original.resources and view.lifecycle is original.lifecycle
    assert view.validation == tuple(original.validation) and view.artifacts == tuple(original.artifacts)
    assert before == canonical_bytes(original.model_dump(exclude_none=True))
    assert view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio")["tools"] == [
        item.model_dump(exclude_none=True) for item in original.interface.tools]


def test_fake_native_normalizes_without_python_lock_or_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("adapter ran a subprocess"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.swift").write_text('fatalError("never execute")\n')
    component = FakeNativeComponent()
    raw = v2_descriptor(component.reference)
    recipe = {"digest": raw["recipe"]["digest"], "manifest": {"id": raw["recipe"]["id"], "version": "1.0.0",
        "steps": [{"component": component.reference.id, "version": component.reference.version, "digest": component.reference.digest}]}}
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source, name="Native")
        view = adapt_descriptor(canonical_bytes(raw), draft=draft, recipe=recipe, registry=ComponentRegistry((component,)))
        bound = bind(view, draft)
        assert view.tools[0].name == "calculate"
        assert bound.descriptor_version == 2
        assert bound.original_dependency_locks == () and bound.derived_inputs == ()
        assert bound.source_validations[0].dependency_lock_digest is None
        assert bound.runtimes == () and bound.capabilities == ()


def test_bound_inputs_retain_exact_original_source_and_are_deeply_immutable(view):
    view, draft = view
    before = copy.deepcopy(draft["source"])
    bound = bind(view, draft)
    assert draft["source"] == before
    assert bound.source.revision == before["revision"]
    assert bound.source_validations[0].digest == view.source_bindings[0].digest
    assert bound.original_dependency_locks[0].file.path == "uv.lock"
    assert parse_bound_inputs(canonical_bytes(bound.model_dump(mode="json"))) == bound
    assert isinstance(bound.source_validations, tuple) and isinstance(bound.source_validations[0].files, tuple)
    with pytest.raises(ValidationError):
        bound.source_validations[0].files[0].path = "changed"


def test_mutation_after_adaptation_is_detected(view):
    view, draft = view
    view.descriptor.interface.tools[0].input_schema["title"] = "later mutation"
    with pytest.raises(PreparationError, match="descriptor_identity_changed"):
        bind(view, draft)


def test_derived_lock_has_separate_identity_and_original_source_is_unchanged(view):
    view, draft = view
    before = copy.deepcopy(draft["source"])
    output = derived(view)
    bound = bind(view, draft, derived_collection=collection("derived_input_collection", "d" * 64), derived_inputs=(output,))
    assert bound.derived_inputs[0].role == "dependency-lock"
    assert bound.original_dependency_locks[0].file.sha256 != output.sha256
    assert bound.source.revision == draft["source"]["revision"] and before == draft["source"]
    assert output.digest in bound.consumed_input_digests
    assert bind(view, draft).digest != bound.digest


@pytest.mark.parametrize("changes", [
    {"collection_digest": "0" * 64}, {"source_revision": "sha256:" + "0" * 64}, {"parent_digests": ["0" * 64]},
])
def test_derivation_closure_mismatch_fails(view, changes):
    view, draft = view
    with pytest.raises(PreparationError, match="derived_input_closure_mismatch"):
        bind(view, draft, derived_collection=collection("derived_input_collection", "d" * 64),
             derived_inputs=(derived(view, **changes),))


def test_derivation_order_closes_dependencies_and_rejects_forward_references(view):
    view, draft = view
    first = derived(view)
    second = derived(view, name="adapter", role="adapter", parents=[first.digest])
    params = {"derived_collection": collection("derived_input_collection", "d" * 64)}
    assert len(bind(view, draft, derived_inputs=(first, second), **params).derived_inputs) == 2
    with pytest.raises(PreparationError, match="derived_input_closure_mismatch"):
        bind(view, draft, derived_inputs=(second, first), **params)


def test_derivation_cannot_masquerade_as_original_file(view):
    view, draft = view
    original = view.source_bindings[0]
    forged = SourceBinding(*original.component_key, original.source_revision,
        original.files + (SourceFileBinding("generated/uv.lock", "e" * 64, 15),),
        original.interface_digest, original.dependency_lock_digest, original.metadata_bytes)
    with pytest.raises(PreparationError, match="component_binding_mismatch"):
        bind(replace(view, source_bindings=(forged,)), draft)
    raw = bind(view, draft).model_dump(mode="json")
    raw["original_dependency_locks"][0]["file"] = {"path": "generated/uv.lock", "sha256": "e" * 64, "size": 15}
    with pytest.raises(PreparationError, match="original_dependency_lock_mismatch"):
        parse_bound_inputs(raw)


@pytest.mark.parametrize("change", [
    lambda x: x.update(schema_version=2), lambda x: x.update(schema_version=True),
    lambda x: x.update(descriptor_version=True), lambda x: x.update(execution_authority=True),
    lambda x: x["captured_collection"].update(schema_version=2),
    lambda x: x["captured_collection"].update(locator="/mutable/file"),
    lambda x: x.update(capabilities=["native-execute"] * 33),
])
def test_bound_contract_is_closed_bounded_and_versioned(view, change):
    view, draft = view
    raw = bind(view, draft).model_dump(mode="json")
    change(raw)
    with pytest.raises(PreparationError):
        parse_bound_inputs(raw)


def test_component_and_collection_closure_are_checked(view):
    view, draft = view
    raw = bind(view, draft).model_dump(mode="json")
    raw["components"][0]["digest"] = "0" * 64
    with pytest.raises(PreparationError, match="preparation_component_closure_mismatch"):
        parse_bound_inputs(raw)
    with pytest.raises(PreparationError, match="captured_collection_required"):
        bind_preparation_inputs(view, draft=draft, captured_collection=collection("derived_input_collection"),
            expected_package_contract=view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio"))


def test_multiple_components_cannot_disagree_on_original_file_bytes(view):
    view, draft = view
    raw = bind(view, draft).model_dump(mode="json")
    component = {**raw["components"][0], "id": "other-source-component"}
    binding = copy.deepcopy(raw["source_validations"][0])
    binding["component"] = component
    binding["dependency_lock_digest"] = None
    binding["files"][0]["size"] += 1
    raw["components"].append(component)
    raw["source_validations"].append(binding)
    with pytest.raises(PreparationError, match="original_file_closure_mismatch"):
        parse_bound_inputs(raw)


def test_expected_package_tools_effects_and_profile_are_exact(view):
    view, draft = view
    expected = view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio")
    expected["tools"] = []
    with pytest.raises(PreparationError, match="expected_package_contract_mismatch"):
        bind_preparation_inputs(view, draft=draft, captured_collection=collection(), expected_package_contract=expected)
    with pytest.raises(PreparationError, match="unsupported_execution_profile"):
        view.expected_package_contract(draft["source"]["revision"], profile="arbitrary")


def result(bound):
    return {"schema_version": 1, "candidate": {"candidate_id": "12345678-1234-1234-1234-123456789012",
        "package": bound.package.model_dump(), "digest": "a" * 64}, "bound_inputs_digest": bound.digest,
        "consumed_input_digests": list(bound.consumed_input_digests),
        "expected_package_contract_digest": bound.expected_package_contract_digest}


def test_builder_result_requires_exact_consumed_set_and_has_no_admission_authority(view):
    view, draft = view
    bound = bind(view, draft, runtimes=(RuntimeReference(name="private-python", digest="9" * 64),))
    valid = validate_builder_result(result(bound), bound)
    assert valid.bound_inputs_digest == bound.digest
    for change in (lambda x: x["consumed_input_digests"].pop(),
                   lambda x: x["consumed_input_digests"].reverse(),
                   lambda x: x.update(bound_inputs_digest="0" * 64),
                   lambda x: x["candidate"]["package"].update(id="another.package")):
        raw = result(bound)
        change(raw)
        with pytest.raises(PreparationError, match="builder_input_binding_mismatch"):
            validate_builder_result(raw, bound)
    raw = result(bound)
    raw["attach"] = True
    with pytest.raises(PreparationError, match="invalid_preparation_inputs"):
        validate_builder_result(raw, bound)


def test_builder_produced_derivations_bind_producer_and_consumed_inputs(view):
    view, draft = view
    bound = bind(view, draft)
    output = derived(view, name="generated-adapter", role="adapter", parents=[bound.digest])
    raw = {**result(bound), "produced_derivations": [output.model_dump(mode="json")]}
    assert validate_builder_result(raw, bound).produced_derivations == (output,)
    raw["produced_derivations"][0]["producer"]["digest"] = "0" * 64
    with pytest.raises(PreparationError, match="builder_derivation_mismatch"):
        validate_builder_result(raw, bound)


def job_binding(bound):
    return {"schema_version": 1, "job_id": "12345678-1234-1234-1234-123456789012",
        "import_id": "22345678-1234-1234-1234-123456789012", "revision": 1,
        "source_revision": bound.source.revision, "descriptor_digest": bound.descriptor_digest,
        "artifact_collection_digest": bound.captured_collection.digest, "recipe": bound.recipe.model_dump(),
        "policy_digest": "1" * 64, "runner_digest": "2" * 64, "verifier_digest": "3" * 64}


def test_receipt_dispatch_preserves_v1_bytes_and_v2_exact_identity(view):
    view, draft = view
    bound = bind(view, draft)
    old = {"schema_version": 1, "binding": job_binding(bound), "dependency_lock_digest": "4" * 64,
           "runtime_digest": "5" * 64, "fixture_digest": "6" * 64}
    old_bytes = canonical_bytes(PreparationReceipt.model_validate(old).model_dump())
    selected = parse_preparation_receipt(old_bytes)
    assert type(selected) is PreparationReceipt
    assert canonical_bytes(selected.model_dump()) == old_bytes
    current = {"schema_version": 2, "binding": job_binding(bound), "inputs": bound.model_dump(mode="json"), "fixture_digest": "6" * 64}
    selected = parse_preparation_receipt(current)
    assert isinstance(selected, PreparationReceiptV2) and selected.inputs == bound
    assert parse_preparation_receipt(canonical_bytes(selected.model_dump(mode="json"))) == selected
    current["binding"]["artifact_collection_digest"] = "0" * 64
    with pytest.raises(PreparationError, match="receipt_input_binding_mismatch"):
        parse_preparation_receipt(current)


@pytest.mark.parametrize("raw", [{"schema_version": 3}, {"schema_version": True}, {"schema_version": "1"}])
def test_receipt_unknown_or_noninteger_version_fails(raw):
    with pytest.raises(PreparationError):
        parse_preparation_receipt(raw)


def test_source_binding_reference_keeps_original_binding_digest(view):
    view, _ = view
    original = view.source_bindings[0]
    reference = SourceValidationReference.from_binding(original)
    assert reference.as_binding() == original and reference.digest == original.digest
    raw = reference.model_dump(mode="json")
    raw["metadata_json"] = '{ "mutable": true }'
    with pytest.raises(ValidationError, match="component_binding_mismatch"):
        SourceValidationReference.model_validate(raw)
