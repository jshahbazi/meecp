"""Family-neutral preparation identities; these contracts confer no authority.

Legacy descriptors and receipts retain their existing parsers and serialization.
Collection references require independently validated, captured manifest bytes.
This module neither captures inputs nor enables any builder or source family.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, get_origin

from pydantic import Field, ValidationError, field_validator, model_validator

from .contracts import Digest, StrictModel
from .preparation_components import (
    ComponentReference, ComponentRegistry, PreparationDescriptorV2, SourceBinding,
    SourceFileBinding, parse_component_descriptor,
    original_file_records,
)
from .preparation_contracts import (
    JobBinding, Name, PreparationDescriptor, PreparationError, PreparationPackage,
    PreparationReceipt, RecipeReference, SourceIdentity, UUID, canonical_bytes,
    contract_digest, load_contract, parse_descriptor, relative_path,
)

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Capability = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class InputContract(StrictModel):
    """Closed, bounded JSON contracts with immutable tuple collections."""

    @model_validator(mode="before")
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            if any(name in value and type(value[name]) is not int for name in ("schema_version", "descriptor_version")):
                raise PreparationError("invalid_preparation_inputs")
            canonical_bytes(value)
        return value

    @field_validator("*", mode="before")
    @classmethod
    def tuple_fields(cls, value, info):
        if get_origin(cls.model_fields[info.field_name].annotation) is tuple and isinstance(value, list):
            return tuple(value)
        return value


class CollectionReference(InputContract):
    kind: Literal["captured_artifact_collection", "derived_input_collection"]
    schema_version: Literal[1]
    digest: Digest


class OriginalSourceReference(InputContract):
    kind: Literal["local_folder", "document", "artifact_collection", "captured_artifact_collection", "service_connection"]
    schema_version: Literal[1]
    revision: SourceIdentity


class OriginalFileReference(InputContract):
    path: Name
    sha256: Digest
    size: int = Field(ge=0, le=64 * 1024 * 1024 * 1024)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        return relative_path(value)


class SourceValidationReference(InputContract):
    component: ComponentReference
    source_revision: SourceIdentity
    files: tuple[OriginalFileReference, ...] = Field(max_length=2048)
    interface_digest: Digest
    dependency_lock_digest: Digest | None = None
    metadata_json: Annotated[str, Field(max_length=16384)] = "{}"

    @model_validator(mode="after")
    def valid_binding(self):
        self.as_binding()  # Reuse the original binding's exact checks and domain.
        return self

    def as_binding(self):
        return SourceBinding(*self.component.key, self.source_revision,
            tuple(SourceFileBinding(item.path, item.sha256, item.size) for item in self.files),
            self.interface_digest, self.dependency_lock_digest, self.metadata_json.encode("utf-8"))

    @property
    def digest(self):
        return self.as_binding().digest

    @classmethod
    def from_binding(cls, value: SourceBinding):
        return cls.model_validate({"component": {"id": value.component_id, "version": value.component_version,
            "digest": value.component_digest}, "source_revision": value.source_revision,
            "files": [{"path": item.path, "sha256": item.sha256, "size": item.size} for item in value.files],
            "interface_digest": value.interface_digest, "dependency_lock_digest": value.dependency_lock_digest,
            "metadata_json": value.metadata_bytes.decode("utf-8")})


class RuntimeReference(InputContract):
    kind: Literal["runtime"] = "runtime"
    name: Identifier
    digest: Digest


class DerivedInputReference(InputContract):
    kind: Literal["derived_input"] = "derived_input"
    name: Identifier
    role: Literal["dependency-lock", "dependency-project", "interface", "adapter", "runtime-config"]
    collection_digest: Digest
    sha256: Digest
    size: int = Field(ge=1, le=64 * 1024 * 1024 * 1024)
    producer: ComponentReference
    source_revision: SourceIdentity
    request_digest: Digest
    policy_digest: Digest
    parent_digests: tuple[Digest, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_parents(self):
        if len(set(self.parent_digests)) != len(self.parent_digests):
            raise PreparationError("duplicate_derived_parent")
        return self

    @property
    def digest(self):
        return contract_digest("preparation-derived-input-reference-v1", self.model_dump(mode="json"))


class OriginalLockReference(InputContract):
    kind: Literal["original_dependency_lock"] = "original_dependency_lock"
    component: ComponentReference
    file: OriginalFileReference


class BoundPreparationInputsV1(InputContract):
    schema_version: Literal[1]
    source: OriginalSourceReference
    descriptor_version: Literal[1, 2]
    descriptor_digest: Digest
    recipe: RecipeReference
    package: PreparationPackage
    components: tuple[ComponentReference, ...] = Field(min_length=1, max_length=32)
    source_validations: tuple[SourceValidationReference, ...] = Field(min_length=1, max_length=32)
    captured_collection: CollectionReference | None
    derived_collection: CollectionReference | None = None
    derived_inputs: tuple[DerivedInputReference, ...] = Field(default=(), max_length=128)
    original_dependency_locks: tuple[OriginalLockReference, ...] = Field(default=(), max_length=32)
    runtimes: tuple[RuntimeReference, ...] = Field(default=(), max_length=32)
    capabilities: tuple[Capability, ...] = Field(default=(), max_length=32)
    expected_package_contract_digest: Digest

    @model_validator(mode="after")
    def closure(self):
        keys = [item.key for item in self.components]
        bound_keys = [item.component.key for item in self.source_validations]
        if (len(set(keys)) != len(keys) or len({item.id for item in self.components}) != len(keys)
                or len(set(bound_keys)) != len(bound_keys) or set(keys) != set(bound_keys)):
            raise PreparationError("preparation_component_closure_mismatch")
        if any(item.source_revision != self.source.revision for item in self.source_validations):
            raise PreparationError("source_identity_mismatch")
        files = {}
        for binding in self.source_validations:
            for file in binding.files:
                if file.path in files and files[file.path] != file:
                    raise PreparationError("original_file_closure_mismatch")
                files[file.path] = file
        if self.captured_collection is not None and self.captured_collection.kind != "captured_artifact_collection":
            raise PreparationError("captured_collection_required")
        if self.captured_collection is None and self.source.kind not in {"local_folder", "document"}:
            raise PreparationError("captured_collection_required")
        if self.derived_collection is not None and self.derived_collection.kind != "derived_input_collection":
            raise PreparationError("derived_collection_required")
        if bool(self.derived_inputs) != (self.derived_collection is not None):
            raise PreparationError("derived_collection_mismatch")
        for items in (self.derived_inputs, self.runtimes):
            if len({item.name for item in items}) != len(items):
                raise PreparationError("duplicate_preparation_input")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise PreparationError("duplicate_preparation_capability")
        validations = {item.component.key: item for item in self.source_validations}
        lock_keys = set()
        for lock in self.original_dependency_locks:
            binding = validations.get(lock.component.key)
            if (binding is None or binding.dependency_lock_digest != lock.file.sha256
                    or lock.file not in binding.files or lock.component.key in lock_keys):
                raise PreparationError("original_dependency_lock_mismatch")
            lock_keys.add(lock.component.key)
        # Every original lock declaration must resolve to an original bound file.
        if lock_keys != {key for key, item in validations.items() if item.dependency_lock_digest is not None}:
            raise PreparationError("original_dependency_lock_mismatch")
        available = {self.source.revision.removeprefix("sha256:"), self.descriptor_digest,
                     *((self.captured_collection.digest,) if self.captured_collection else ()), *(item.digest for item in self.runtimes),
                     *(file.sha256 for binding in self.source_validations for file in binding.files)}
        for item in self.derived_inputs:
            if (item.source_revision != self.source.revision or item.collection_digest != self.derived_collection.digest
                    or not set(item.parent_digests) <= available):
                raise PreparationError("derived_input_closure_mismatch")
            available.add(item.digest)
        return self

    @property
    def digest(self):
        return contract_digest("preparation-bound-inputs-v1", self.model_dump(mode="json"))

    @property
    def consumed_input_digests(self):
        """Exact ordered identities a builder must acknowledge consuming."""
        return (self.digest, *(item.digest for item in self.source_validations),
                *((self.captured_collection.digest,) if self.captured_collection else ()),
                *((self.derived_collection.digest,) if self.derived_collection else ()),
                *(item.digest for item in self.derived_inputs), *(item.digest for item in self.runtimes))


@dataclass(frozen=True)
class DescriptorView:
    descriptor: PreparationDescriptor | PreparationDescriptorV2
    source_bindings: tuple[SourceBinding, ...]
    _canonical: bytes

    @property
    def digest(self):
        self.assert_unchanged()
        return self.descriptor.digest

    def assert_unchanged(self):
        if canonical_bytes(self.descriptor.model_dump(exclude_none=True)) != self._canonical:
            raise PreparationError("descriptor_identity_changed")

    @property
    def package(self):
        return self.descriptor.package

    @property
    def tools(self):
        return tuple(self.descriptor.interface.tools if isinstance(self.descriptor, PreparationDescriptor)
                     else self.descriptor.tools)

    @property
    def access(self):
        return self.descriptor.access

    @property
    def resources(self):
        return self.descriptor.resources

    @property
    def lifecycle(self):
        return self.descriptor.lifecycle

    @property
    def validation(self):
        return tuple(self.descriptor.validation)

    @property
    def artifacts(self):
        return tuple(self.descriptor.artifacts)

    def expected_package_contract(self, source_revision: str, *, profile: str):
        """Host-selected target profile; no inference from language or recipe ID."""
        self.assert_unchanged()
        if profile not in {"mcp-stdio", "mcp-http", "model-service"}:
            raise PreparationError("unsupported_execution_profile")
        if any(item.source_revision != source_revision for item in self.source_bindings):
            raise PreparationError("source_identity_mismatch")
        return {"package_id": self.package.id, "version": self.package.version, "name": self.package.name,
                "source_revision": source_revision, "profile": profile,
                "tools": [tool.model_dump(exclude_none=True) for tool in self.tools],
                "access": self.access.model_dump(), "resources": self.resources.model_dump(),
                "lifecycle": self.lifecycle.model_dump()}


def adapt_descriptor(value, *, draft: dict, recipe: dict, registry: ComponentRegistry) -> DescriptorView:
    """Parse and statically validate; preserve an existing descriptor object."""
    original = value if isinstance(value, (PreparationDescriptor, PreparationDescriptorV2)) else None
    raw = original.model_dump(exclude_none=True) if original is not None else load_contract(value) if isinstance(value, bytes) else value
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
        raise PreparationError("invalid_preparation_descriptor")
    parser = {1: parse_descriptor, 2: parse_component_descriptor}.get(raw["schema_version"])
    if parser is None:
        raise PreparationError("unsupported_preparation_descriptor_version")
    parsed = parser(raw)  # Revalidate even previously parsed, shallow-frozen models.
    descriptor = original if original is not None else parsed
    bindings = registry.validate_source(descriptor, draft, recipe)
    return DescriptorView(descriptor, bindings, canonical_bytes(descriptor.model_dump(exclude_none=True)))


def bind_preparation_inputs(view: DescriptorView, *, draft: dict, captured_collection: CollectionReference | None,
                            derived_collection: CollectionReference | None = None,
                            derived_inputs=(), runtimes=(), capabilities=(), expected_package_contract: dict):
    """Bind validated identities; callers own collection/runtime byte verification."""
    view.assert_unchanged()
    source = draft["source"]
    if any(item.source_revision != source["revision"] for item in view.source_bindings):
        raise PreparationError("source_identity_mismatch")
    expected = view.expected_package_contract(source["revision"], profile=expected_package_contract.get("profile"))
    if expected != expected_package_contract:
        raise PreparationError("expected_package_contract_mismatch")
    validations = [SourceValidationReference.from_binding(item) for item in view.source_bindings]
    records = {item["path"]: item for item in original_file_records(draft)}
    locks = []
    for binding in validations:
        for file in binding.files:
            if records.get(file.path) != file.model_dump():
                raise PreparationError("component_binding_mismatch")
        if binding.dependency_lock_digest is not None:
            matches = [item for item in binding.files if item.sha256 == binding.dependency_lock_digest]
            if len(matches) != 1:
                raise PreparationError("original_dependency_lock_mismatch")
            locks.append({"component": binding.component.model_dump(), "file": matches[0].model_dump()})
    value = {"schema_version": 1,
        "source": {"kind": source["kind"], "schema_version": source.get("contract_version", 1), "revision": source["revision"]},
        "descriptor_version": view.descriptor.schema_version, "descriptor_digest": view.digest,
        "recipe": view.descriptor.recipe.model_dump(), "package": view.package.model_dump(),
        "components": [item.component.model_dump() for item in validations],
        "source_validations": [item.model_dump(mode="json") for item in validations],
        "captured_collection": captured_collection.model_dump() if captured_collection else None,
        "derived_collection": derived_collection.model_dump() if derived_collection else None,
        "derived_inputs": [item.model_dump(mode="json") for item in derived_inputs],
        "original_dependency_locks": locks, "runtimes": [item.model_dump() for item in runtimes],
        "capabilities": list(capabilities),
        "expected_package_contract_digest": contract_digest("preparation-expected-package-v1", expected)}
    return _parse(BoundPreparationInputsV1, value)


class CandidatePackageReference(InputContract):
    candidate_id: UUID
    package: PreparationPackage
    digest: Digest


class BuilderResultV1(InputContract):
    schema_version: Literal[1]
    candidate: CandidatePackageReference
    bound_inputs_digest: Digest
    consumed_input_digests: tuple[Digest, ...] = Field(min_length=1, max_length=256)
    expected_package_contract_digest: Digest
    produced_derivations: tuple[DerivedInputReference, ...] = Field(default=(), max_length=128)

    @property
    def digest(self):
        return contract_digest("preparation-builder-result-v1", self.model_dump(mode="json"))


def validate_builder_result(value, inputs: BoundPreparationInputsV1) -> BuilderResultV1:
    result = _parse(BuilderResultV1, value)
    if (result.bound_inputs_digest != inputs.digest or result.consumed_input_digests != inputs.consumed_input_digests
            or result.expected_package_contract_digest != inputs.expected_package_contract_digest
            or result.candidate.package != inputs.package):
        raise PreparationError("builder_input_binding_mismatch")
    available = set(inputs.consumed_input_digests)
    names = {item.name for item in inputs.derived_inputs}
    for item in result.produced_derivations:
        if (item.name in names or item.source_revision != inputs.source.revision
                or item.producer.key not in {component.key for component in inputs.components}
                or not set(item.parent_digests) <= available):
            raise PreparationError("builder_derivation_mismatch")
        names.add(item.name)
        available.add(item.digest)
    return result


class PreparationReceiptV2(InputContract):
    schema_version: Literal[2]
    binding: JobBinding
    inputs: BoundPreparationInputsV1
    fixture_digest: Digest

    @model_validator(mode="after")
    def matches_job(self):
        if (self.binding.source_revision != self.inputs.source.revision
                or self.binding.descriptor_digest != self.inputs.descriptor_digest
                or self.binding.recipe != self.inputs.recipe
                or self.binding.artifact_collection_digest != (self.inputs.captured_collection.digest
                    if self.inputs.captured_collection else contract_digest("artifact-collection-v1", []))):
            raise PreparationError("receipt_input_binding_mismatch")
        return self

    @property
    def digest(self):
        return contract_digest("preparation-receipt-v2", self.model_dump(mode="json"))


def _parse(model, value):
    raw = value.model_dump(mode="json") if isinstance(value, model) else load_contract(value) if isinstance(value, bytes) else value
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        for item in error.errors():
            cause = item.get("ctx", {}).get("error")
            if isinstance(cause, PreparationError):
                raise PreparationError(cause.code) from None
        raise PreparationError("invalid_preparation_inputs") from None


def parse_bound_inputs(value) -> BoundPreparationInputsV1:
    return _parse(BoundPreparationInputsV1, value)


def parse_preparation_descriptor(value):
    """Version dispatch without normalizing legacy objects or identities."""
    original = value if isinstance(value, (PreparationDescriptor, PreparationDescriptorV2)) else None
    raw = original.model_dump(exclude_none=True) if original is not None else load_contract(value) if isinstance(value, bytes) else value
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
        raise PreparationError("invalid_preparation_descriptor")
    parser = {1: parse_descriptor, 2: parse_component_descriptor}.get(raw["schema_version"])
    if parser is None:
        raise PreparationError("unsupported_preparation_descriptor_version")
    checked = parser(raw)
    return original if original is not None else checked


def descriptor_tools(value):
    return value.interface.tools if value.schema_version == 1 else value.tools


def parse_preparation_receipt(value) -> PreparationReceipt | PreparationReceiptV2:
    """Separate dispatch; the original ReceiptV1 model and bytes are untouched."""
    raw = value.model_dump(mode="json") if isinstance(value, (PreparationReceipt, PreparationReceiptV2)) else load_contract(value) if isinstance(value, bytes) else value
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
        raise PreparationError("invalid_preparation_receipt")
    model = {1: PreparationReceipt, 2: PreparationReceiptV2}.get(raw["schema_version"])
    if model is None:
        raise PreparationError("unsupported_preparation_receipt_version")
    return _parse(model, raw)
