"""Host-owned source validation components, independent of source language.

Source and recipe bundles are data. They cannot register inspection hooks. The
registry is constructed by the host from implementations it already owns; every
selection binds an exact implementation identity. This module does not execute
selected source or enable a build capability.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from .contracts import Access, Digest, Lifecycle, Tool
from .imports import ImportError as SourceError, MAX_SOURCE_BYTES, MAX_SOURCE_FILES, _read_source
from .preparation_contracts import (
    ArtifactSelection, Contract, PreparationDescriptor, PreparationError,
    PreparationPackage, PreparationResources, RecipeReference, SyntheticFixture,
    Version, canonical_bytes, check_schema, contract_digest, load_contract,
    relative_path, schema_accepts,
)


class ComponentReference(Contract):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    version: Version
    digest: Digest

    @property
    def key(self) -> tuple[str, str, str]:
        return self.id, self.version, self.digest


class ComponentSelection(Contract):
    component: ComponentReference
    input: dict[str, Any] = Field(default_factory=dict)


class PreparationDescriptorV2(Contract):
    """Closed common envelope; each host component owns its input schema.

    V1 is deliberately not normalized to V2: its canonical digest and required
    Python fields remain defined by preparation_contracts.PreparationDescriptor.
    Parsing this envelope does not establish component admission or execution.
    """
    schema_version: Literal[2]
    recipe: RecipeReference
    package: PreparationPackage
    components: list[ComponentSelection] = Field(min_length=1, max_length=32)
    tools: list[Tool] = Field(min_length=1, max_length=128)
    artifacts: list[ArtifactSelection] = Field(default_factory=list, max_length=64)
    inputs: dict[str, Any] = Field(default_factory=dict)
    access: Access = Field(default_factory=Access)
    resources: PreparationResources
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    validation: list[SyntheticFixture] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def consistency(self):
        if any(selection.component.id == "host-command-json-v1" for selection in self.components):
            # Reject costly selected validation keywords before general fixture
            # evaluation. Component selection grants no execution capability.
            from .preparation_command import structural_schema
            for tool in self.tools:
                structural_schema(tool.input_schema)
                structural_schema(tool.output_schema)
        keys = [selection.component.key for selection in self.components]
        if len(set(keys)) != len(keys) or len({key[0] for key in keys}) != len(keys):
            raise PreparationError("duplicate_preparation_component")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise PreparationError("duplicate_preparation_tool")
        for tool in self.tools:
            check_schema(tool.input_schema)
            if tool.output_schema is not None:
                check_schema(tool.output_schema)
        if len({item.name for item in self.artifacts}) != len(self.artifacts):
            raise PreparationError("duplicate_preparation_input")
        if len({item.id for item in self.validation}) != len(self.validation):
            raise PreparationError("duplicate_preparation_input")
        for fixture in self.validation:
            tool = next((item for item in self.tools if item.name == fixture.tool), None)
            if tool is None:
                raise PreparationError("unknown_fixture_tool")
            if not schema_accepts(tool.input_schema, fixture.arguments):
                raise PreparationError("invalid_synthetic_fixture")
            if tool.output_schema is not None and not schema_accepts(tool.output_schema, fixture.expected):
                raise PreparationError("invalid_synthetic_fixture")
        return self

    @property
    def digest(self) -> str:
        return contract_digest("preparation-descriptor-v2", self.model_dump(exclude_none=True))


def parse_component_descriptor(value: dict | bytes) -> PreparationDescriptorV2:
    try:
        return PreparationDescriptorV2.model_validate(load_contract(value) if isinstance(value, bytes) else value)
    except ValidationError as error:
        for item in error.errors():
            cause = item.get("ctx", {}).get("error")
            if isinstance(cause, PreparationError):
                raise PreparationError(cause.code) from None
        raise PreparationError("invalid_preparation_descriptor") from None


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    sha256: str
    size: int
    data: bytes


@dataclass(frozen=True)
class SourceSnapshot:
    revision: str
    files: tuple[SnapshotFile, ...]

    def by_path(self) -> dict[str, SnapshotFile]:
        return {item.path: item for item in self.files}


def source_records(draft: dict) -> tuple[dict, ...]:
    """Validate the original folder identity without changing its serialization."""
    try:
        source = draft["source"]
        if source["kind"] not in {"local_folder", "document"}:
            raise PreparationError("unsupported_preparation_source")
        raw = source["files"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SOURCE_FILES:
            raise PreparationError("invalid_source_identity")
        records = []
        for item in raw:
            if (not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}
                    or not isinstance(item["path"], str)
                    or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    or type(item["size"]) is not int or not 0 <= item["size"] <= 16 * 1024 * 1024):
                raise PreparationError("invalid_source_identity")
            relative_path(item["path"])
            records.append(dict(item))
        if len({item["path"] for item in records}) != len(records):
            raise PreparationError("invalid_source_identity")
        if records != sorted(records, key=lambda item: item["path"]) or sum(item["size"] for item in records) > MAX_SOURCE_BYTES:
            raise PreparationError("invalid_source_identity")
        # This matches ImportStore's existing v1 folder digest exactly, including
        # ensure_ascii=True. Never substitute the new contract canonicalizer.
        digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if source.get("digest") != digest or source.get("revision") != "sha256:" + digest:
            raise PreparationError("source_identity_mismatch")
        return tuple(records)
    except (KeyError, TypeError):
        raise PreparationError("invalid_source_identity") from None


def original_file_records(draft):
    """Original file metadata for either existing snapshot or captured source."""
    source = draft["source"]
    if source["kind"] == "application_directory":
        from .preparation_application_source import application_source_records
        try:
            return application_source_records(draft)
        except SourceError as error:
            raise PreparationError(error.code) from None
    if source["kind"] != "captured_artifact_collection":
        return tuple(source.get("files", []))
    from .preparation_artifacts import CapturedArtifactCollectionV1
    collection = CapturedArtifactCollectionV1.model_validate(source["collection"])
    if source.get("contract_version") != 1 or (source["digest"], source["revision"]) != (collection.digest, "sha256:" + collection.digest):
        raise PreparationError("source_identity_mismatch")
    return tuple({"path": item.path, "sha256": item.sha256, "size": item.size} for item in collection.entries)


def read_snapshot(draft: dict) -> SourceSnapshot:
    """Read and recheck the exact bounded snapshot tree without importing it."""
    records = source_records(draft)
    try:
        root = Path(draft["source"]["snapshot_path"])
        if not root.is_absolute() or root.resolve() != root or root.is_symlink() or not root.is_dir():
            raise PreparationError("unsafe_preparation_source")
        pending, visible, scanned = [root], set(), 0
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                scanned += 1
                if scanned > 10000:
                    raise PreparationError("preparation_inspection_limit")
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    pending.append(path)
                elif stat.S_ISREG(mode):
                    visible.add(path.relative_to(root).as_posix())
                else:
                    raise PreparationError("unsafe_preparation_source")
        if visible != {item["path"] for item in records}:
            raise PreparationError("source_identity_mismatch")
        files = []
        for record in records:
            data = _read_source(root / record["path"])
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise PreparationError("source_identity_mismatch")
            files.append(SnapshotFile(record["path"], record["sha256"], record["size"], data))
        for item in files:
            if _read_source(root / item.path) != item.data:
                raise PreparationError("source_identity_mismatch")
        return SourceSnapshot(draft["source"]["revision"], tuple(files))
    except SourceError as error:
        raise PreparationError(error.code) from None
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError("unsafe_preparation_source") from None


@dataclass(frozen=True)
class SourceFileBinding:
    path: str
    sha256: str
    size: int

    def __post_init__(self):
        relative_path(self.path)
        if (not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256)
                or type(self.size) is not int or self.size < 0):
            raise PreparationError("component_binding_mismatch")


@dataclass(frozen=True)
class SourceBinding:
    """Immutable, content-free data returned by a host-owned validator."""
    component_id: str
    component_version: str
    component_digest: str
    source_revision: str
    files: tuple[SourceFileBinding, ...]
    interface_digest: str
    dependency_lock_digest: str | None = None
    metadata_bytes: bytes = b"{}"

    def __post_init__(self):
        try:
            ComponentReference(id=self.component_id, version=self.component_version, digest=self.component_digest)
        except ValidationError:
            raise PreparationError("component_binding_mismatch") from None
        if (not isinstance(self.source_revision, str)
                or not re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[0-9a-f]{40})", self.source_revision)
                or type(self.files) is not tuple or len(self.files) > MAX_SOURCE_FILES
                or any(not isinstance(item, SourceFileBinding) for item in self.files)
                or len({item.path for item in self.files}) != len(self.files)
                or not isinstance(self.interface_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.interface_digest)
                or (self.dependency_lock_digest is not None and (not isinstance(self.dependency_lock_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", self.dependency_lock_digest)))
                or type(self.metadata_bytes) is not bytes):
            raise PreparationError("component_binding_mismatch")
        if canonical_bytes(load_contract(self.metadata_bytes)) != self.metadata_bytes:
            raise PreparationError("component_binding_mismatch")

    @property
    def component_key(self) -> tuple[str, str, str]:
        return self.component_id, self.component_version, self.component_digest

    @property
    def digest(self) -> str:
        return contract_digest("preparation-source-binding-v1", {
            "component": {"id": self.component_id, "version": self.component_version, "digest": self.component_digest},
            "source_revision": self.source_revision,
            "files": [{"path": item.path, "sha256": item.sha256, "size": item.size} for item in self.files],
            "interface_digest": self.interface_digest, "dependency_lock_digest": self.dependency_lock_digest,
            "metadata": load_contract(self.metadata_bytes),
        })


class ComponentProtocol(Protocol):
    """Implemented only by admitted host code, never source/recipe modules."""
    reference: ComponentReference
    input_schema: dict
    legacy_interface_kinds: frozenset[str]

    def validate_source(self, descriptor: PreparationDescriptor | PreparationDescriptorV2,
                        draft: dict, recipe: dict) -> SourceBinding: ...


class ComponentRegistry:
    def __init__(self, components: tuple[ComponentProtocol, ...] = ()):
        self._components: dict[tuple[str, str, str], ComponentProtocol] = {}
        for component in components:
            self.register(component)

    def register(self, component: ComponentProtocol) -> None:
        reference = ComponentReference.model_validate(component.reference.model_dump())
        check_schema(component.input_schema)
        if (component.input_schema.get("type") != "object"
                or component.input_schema.get("additionalProperties") is not False):
            raise PreparationError("closed_component_input_schema_required")
        if reference.key in self._components:
            raise PreparationError("duplicate_preparation_component")
        self._components[reference.key] = component

    def validate_source(self, descriptor: PreparationDescriptor | PreparationDescriptorV2,
                        draft: dict, recipe: dict) -> tuple[SourceBinding, ...]:
        manifest = recipe["manifest"]
        recipe_reference = descriptor.recipe
        if (manifest.get("id") != recipe_reference.id or manifest.get("version") != recipe_reference.version
                or recipe.get("digest") != recipe_reference.digest):
            raise PreparationError("recipe_identity_mismatch")
        selected = {}
        for step in manifest["steps"]:
            reference = ComponentReference(id=step["component"], version=step["version"], digest=step["digest"])
            if reference.key not in self._components:
                raise PreparationError("unknown_preparation_component")
            if reference.key in selected:
                raise PreparationError("duplicate_preparation_component")
            selected[reference.key] = self._components[reference.key]
        if isinstance(descriptor, PreparationDescriptorV2):
            if set(selected) != {item.component.key for item in descriptor.components}:
                raise PreparationError("recipe_component_mismatch")
            for item in descriptor.components:
                if not schema_accepts(selected[item.component.key].input_schema, item.input):
                    raise PreparationError("component_input_mismatch")
        elif isinstance(descriptor, PreparationDescriptor):
            if any(descriptor.interface.kind not in item.legacy_interface_kinds for item in selected.values()):
                raise PreparationError("recipe_interface_mismatch")
        else:
            raise PreparationError("invalid_preparation_descriptor")
        bindings = []
        for key, component in selected.items():
            binding = component.validate_source(descriptor, draft, recipe)
            if (not isinstance(binding, SourceBinding) or binding.component_key != key
                    or binding.source_revision != draft["source"]["revision"]):
                raise PreparationError("component_binding_mismatch")
            records = {item["path"]: item for item in original_file_records(draft)}
            if any(item.path not in records or (item.sha256, item.size) !=
                   (records[item.path]["sha256"], records[item.path]["size"]) for item in binding.files):
                raise PreparationError("component_binding_mismatch")
            binding.digest  # Also bounds the immutable result before admitting it.
            bindings.append(binding)
        return tuple(bindings)


class PythonSourceComponent:
    """V1 Python validation moved behind the component seam, without execution."""
    input_schema = {"type": "object", "additionalProperties": False}

    def __init__(self, reference: ComponentReference, *, interface_kind: Literal["python-callable", "python-mcp"]):
        self.reference = reference
        self.legacy_interface_kinds = frozenset({interface_kind})

    def validate_source(self, descriptor, draft, recipe) -> SourceBinding:
        if not isinstance(descriptor, PreparationDescriptor) or descriptor.interface.kind not in self.legacy_interface_kinds:
            raise PreparationError("unsupported_component_descriptor")
        files = {item["path"]: item for item in source_records(draft)}
        # The planner can validate legacy metadata-only fixtures. Actual intake
        # has a snapshot path and must bind its current bytes as well.
        if "snapshot_path" in draft["source"]:
            read_snapshot(draft)
        prefix = "" if descriptor.dependencies.project == "." else descriptor.dependencies.project + "/"
        lock_path = prefix + "uv.lock"
        lock = files.get(lock_path)
        if lock is None or lock["sha256"] != descriptor.dependencies.lock_sha256 or prefix + "pyproject.toml" not in files:
            raise PreparationError("dependency_lock_mismatch")
        if descriptor.source_license not in files or not files[descriptor.source_license]["size"]:
            raise PreparationError("source_license_required")
        module = descriptor.interface.module.replace(".", "/")
        entrypoint = module + ".py" if module + ".py" in files else module + "/__init__.py"
        if entrypoint not in files:
            raise PreparationError("entrypoint_source_required")
        bound_paths = sorted({lock_path, prefix + "pyproject.toml", descriptor.source_license, entrypoint})
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
            tuple(SourceFileBinding(path, files[path]["sha256"], files[path]["size"]) for path in bound_paths),
            contract_digest("preparation-interface-v1", descriptor.interface.model_dump(exclude_none=True)),
            descriptor.dependencies.lock_sha256,
            canonical_bytes({"family": "python", "project": descriptor.dependencies.project}))


def builtin_component_registry() -> ComponentRegistry:
    # Catalog identities are the existing pinned builder implementation bytes.
    from .recipes import builtin_components
    catalog = builtin_components()
    components = []
    for name, kind in (("python-function-v1", "python-callable"), ("existing-mcp-v1", "python-mcp")):
        record = catalog[name + "@1.0.0"]
        components.append(PythonSourceComponent(ComponentReference(id=name, version="1.0.0", digest=record["digest"]),
                                                interface_kind=kind))
    from .preparation_resolved_python import ResolvedPythonComponent
    components.append(ResolvedPythonComponent())
    return ComponentRegistry(tuple(components))
