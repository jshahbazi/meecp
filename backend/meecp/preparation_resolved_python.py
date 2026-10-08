"""Host-owned Python component consuming separately authorized derived inputs."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field

from .contracts import Digest
from .preparation_components import ComponentReference, PreparationDescriptorV2, SourceBinding, SourceFileBinding
from .preparation_contracts import Contract, PreparationError, canonical_bytes, contract_digest, relative_path
from .preparation_inspection import inspect_source


class ResolvedPythonInput(Contract):
    candidate_id: Digest
    resolution_digest: Digest
    source_license: str = Field(min_length=1, max_length=4096)


COMPONENT_ID = "resolved-python-v1"
VERSION = "1.0.1"
# Published component metadata remains immutable when a new host implementation
# is minted. It is retained for saved recipe records, not implicit admission.
PUBLISHED_COMPONENTS = {"1.0.0": "876ec0a299ff6b028ac8b747f72c610fc7f53c119cac4bf00de1e2405558f04f"}
INPUT_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["candidate_id", "resolution_digest", "source_license"],
    "properties": {"candidate_id": {"type": "string", "minLength": 64, "maxLength": 64},
                   "resolution_digest": {"type": "string", "minLength": 64, "maxLength": 64},
                   "source_license": {"type": "string", "minLength": 1, "maxLength": 4096}}}


def component_catalog():
    # Identity changes when the trusted component or build implementation does.
    root = Path(__file__).parent
    files = ["preparation_resolved_python.py", "preparation_builder.py",
        "preparation_application_builder.py", "preparation_application_source.py"]
    digest = contract_digest("preparation-resolved-python-component-v1", [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in files])
    # Recipe config is empty. Descriptor input belongs to the component below.
    identities = {**PUBLISHED_COMPONENTS, VERSION: digest}
    return {COMPONENT_ID + "@" + version: {"digest": identity,
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}
        for version, identity in identities.items()}


def resolved_input(descriptor):
    if not isinstance(descriptor, PreparationDescriptorV2) or len(descriptor.components) != 1:
        raise PreparationError("unsupported_component_descriptor")
    selected = descriptor.components[0]
    if selected.component.id != COMPONENT_ID:
        raise PreparationError("unsupported_component_descriptor")
    value = ResolvedPythonInput.model_validate(selected.input)
    relative_path(value.source_license)
    return value


def source_candidate(descriptor, draft):
    selected = resolved_input(descriptor)
    candidate = next((item for item in inspect_source(draft)["candidates"]
        if item["candidate_id"] == selected.candidate_id), None)
    if candidate is None or candidate["kind"] not in {"python-callable", "python-mcp"}:
        raise PreparationError("resolution_candidate_mismatch")
    files = {item["path"]: item for item in draft["source"]["files"]}
    if selected.source_license not in files or not files[selected.source_license]["size"]:
        raise PreparationError("source_license_required")
    return candidate


def validate_resolution(descriptor, draft, result):
    selected = resolved_input(descriptor)
    candidate = source_candidate(descriptor, draft)
    if (result.digest != selected.resolution_digest or result.request.source_revision != draft["source"]["revision"]
            or result.request.candidate_id != selected.candidate_id
            or result.request.candidate_digest != contract_digest("preparation-resolution-candidate-v1", candidate)):
        raise PreparationError("resolution_result_mismatch")
    tools = candidate["tools"] if candidate["kind"] == "python-callable" else [item.model_dump(exclude_none=True) for item in result.tools]
    from .contracts import Tool
    if descriptor.tools != [Tool.model_validate(item) for item in tools]:
        raise PreparationError("resolution_interface_mismatch")
    if candidate["kind"] == "python-mcp" and result.protocol_version != "2025-11-25":
        raise PreparationError("resolution_interface_required")
    return candidate


class ResolvedPythonComponent:
    input_schema = INPUT_SCHEMA
    legacy_interface_kinds = frozenset()

    def __init__(self):
        self.reference = ComponentReference(id=COMPONENT_ID, version=VERSION,
            digest=component_catalog()[COMPONENT_ID + "@" + VERSION]["digest"])

    def validate_source(self, descriptor, draft, recipe):
        candidate = source_candidate(descriptor, draft)
        # Bind only original captured files. The generated project, lock and
        # dynamic interface are separately checked resolution result inputs.
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
            tuple(SourceFileBinding(item["path"], item["sha256"], item["size"]) for item in draft["source"]["files"]),
            contract_digest("preparation-resolution-candidate-v1", candidate), None,
            canonical_bytes({"family": "python", "kind": candidate["kind"], "root": candidate["root"]}))


class ResolvedPythonFamily:
    """Reviewed dispatch implementation. Bundles cannot register families."""
    source_kind = None  # Existing folder inspection also supports descriptor v1.

    def __init__(self, resolutions):
        self.component, self.resolutions = ResolvedPythonComponent(), resolutions

    def validate_inputs(self, descriptor, draft):
        selected = resolved_input(descriptor)
        if self.resolutions is None:
            raise PreparationError("resolution_result_unavailable")
        result, path = self.resolutions.resolve_result(draft["draft_id"], draft["revision"],
            selected.candidate_id, selected.resolution_digest)
        validate_resolution(descriptor, draft, result)
        return result, path

    def resolution_binding(self, descriptor):
        selected = resolved_input(descriptor)
        return selected.candidate_id, selected.resolution_digest

    def bound_inputs(self, descriptor, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import adapt_descriptor, bind_preparation_inputs
        result, _ = self.validate_inputs(descriptor, draft)
        if runtime_digest != result.request.runtime.digest or descriptor.artifacts:
            raise PreparationError("resolution_runtime_mismatch")
        view = adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry)
        return bind_preparation_inputs(view, draft=draft, captured_collection=None,
            derived_collection=result.collection, derived_inputs=result.derived_inputs,
            runtimes=(result.request.runtime,), capabilities=("tools",),
            expected_package_contract=view.expected_package_contract(draft["source"]["revision"], profile="mcp-stdio"))

    def prepare(self, descriptor, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        from .preparation_builder import prepare_bound
        return prepare_bound(descriptor, draft, toolchain, output, run, check,
            acquire_wheels=acquire_wheels, resolved=self.validate_inputs(descriptor, draft))

    def release(self, job_id, descriptor):
        pass  # Resolution results remain durable immutable inputs.
