"""Pinned CPU runtime component, independent of the detector task adapter."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .preparation_components import (
    ComponentReference, PreparationDescriptorV2, SourceBinding, SourceFileBinding, read_snapshot,
)
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest
from .recipes import _read

COMPONENT_ID = "onnxruntime-cpu-v1"
COMPONENT_VERSION = "1.0.0"
INPUT = {"profile": "cpu-stdio"}
INPUT_SCHEMA = {"type": "object", "properties": {"profile": {"const": "cpu-stdio"}},
                "required": ["profile"], "additionalProperties": False}
ASSETS = ("pyproject.toml", "uv.lock", "LICENSE-FLATBUFFERS", "LICENSE-HTTPX", "THIRD_PARTY_NOTICES.md")


def component_catalog():
    root = Path(__file__).resolve().parents[2] / "templates/onnx-hotdog-v1"
    identity = contract_digest("mcpme-onnxruntime-cpu-component-v1", {
        "implementation": hashlib.sha256(_read(Path(__file__), 1024 * 1024)).hexdigest(),
        "assets": {name: hashlib.sha256(_read(root / name, 4 * 1024 * 1024)).hexdigest() for name in ASSETS}})
    return {COMPONENT_ID + "@" + COMPONENT_VERSION: {"digest": identity,
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}}


def component_reference():
    return ComponentReference(id=COMPONENT_ID, version=COMPONENT_VERSION,
        digest=component_catalog()[COMPONENT_ID + "@" + COMPONENT_VERSION]["digest"])


def source_files(draft, collection):
    """Original snapshot files and captured payloads have separate namespaces."""
    if draft["source"]["kind"] == "captured_artifact_collection":
        return tuple(SourceFileBinding(item.path, item.sha256, item.size) for item in collection.entries)
    snapshot = read_snapshot(draft)
    return tuple(SourceFileBinding(item.path, item.sha256, item.size) for item in snapshot.files)


class OnnxRuntimeCpuComponent:
    input_schema = INPUT_SCHEMA
    legacy_interface_kinds = frozenset()

    def __init__(self):
        self.reference = component_reference()

    def validate_source(self, descriptor, draft, recipe):
        from .preparation_onnx import captured_collection
        if not isinstance(descriptor, PreparationDescriptorV2):
            raise PreparationError("unsupported_component_descriptor")
        selected = [item for item in descriptor.components if item.component == self.reference]
        if len(selected) != 1 or selected[0].input != INPUT:
            raise PreparationError("component_input_mismatch")
        if descriptor.resources.gpu or descriptor.lifecycle.max_sessions != 1:
            raise PreparationError("onnx_resource_contract_mismatch")
        collection = captured_collection(draft)
        return SourceBinding(*self.reference.key, draft["source"]["revision"], source_files(draft, collection),
            contract_digest("onnxruntime-cpu-interface-v1", INPUT), None,
            canonical_bytes({"family": "onnx-runtime", "collection_digest": collection.digest,
                             "python": "3.12.13", "onnxruntime": "1.30.0", "profile": "cpu-stdio"}))
