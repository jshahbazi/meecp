"""Versioned, code-free preparation and declarative recipe contracts.

These contracts authorize no execution. Runtime capability remains gated until
the independent builder, ownership and probe acceptance checks are implemented.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextvars import ContextVar
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, _WrappedReferencingError
from jsonschema.validators import extend
from pydantic import Field, ValidationError, model_validator

from .contracts import Access, Digest, Lifecycle, PackageError, StrictModel, Tool, package_relative_path

MAX_CONTRACT_BYTES = 512 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 16384
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_SCHEMA_EVALUATIONS = 32768
MAX_VALIDATION_NODES = 4096
MAX_VALIDATION_ITEMS = 128
RUNNER_IDENTITY = hashlib.sha256(b"mcpme-preparation-contract-runner-v1:metadata-only").hexdigest()
VERIFIER_IDENTITY = hashlib.sha256(b"mcpme-preparation-contract-verifier-v1:static-only").hexdigest()
Name = Annotated[str, Field(min_length=1, max_length=256)]
RecipeID = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", max_length=128)]
Version = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?$", max_length=64)]
SourceIdentity = Annotated[str, Field(pattern=r"^(?:sha256:[0-9a-f]{64}|[0-9a-f]{40})$")]
UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
_CREDENTIALS = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")


class PreparationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_bytes(value: Any) -> bytes:
    """Bound JSON before recursion/serialization; preserve array order."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise PreparationError("preparation_contract_limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise PreparationError("invalid_preparation_contract")
            pending.extend((child, depth + 1) for child in item.values())
            pending.extend((key, depth + 1) for key in item)
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > 16384 or any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise PreparationError("preparation_contract_limit")
            if _CREDENTIALS.search(item):
                raise PreparationError("preparation_credentials_detected")
        elif type(item) not in (int, float, bool, type(None)):
            raise PreparationError("invalid_preparation_contract")
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        raise PreparationError("invalid_preparation_contract") from None
    if len(data) > MAX_CONTRACT_BYTES:
        raise PreparationError("preparation_contract_limit")
    return data


def contract_digest(domain: str, value: Any) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}-v[1-9][0-9]*", domain):
        raise PreparationError("invalid_digest_domain")
    return hashlib.sha256(b"mcpme\0" + domain.encode("ascii") + b"\0" + canonical_bytes(value)).hexdigest()


def load_contract(data: bytes) -> dict:
    if len(data) > MAX_CONTRACT_BYTES:
        raise PreparationError("preparation_contract_limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PreparationError("invalid_preparation_contract")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(PreparationError("invalid_preparation_contract")))
        if not isinstance(value, dict):
            raise PreparationError("invalid_preparation_contract")
        canonical_bytes(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError("invalid_preparation_contract") from None


def relative_path(value: str, *, allow_dot: bool = False) -> str:
    try:
        package_relative_path(value, allow_dot=allow_dot)
        return value
    except PackageError:
        raise PreparationError("unsafe_preparation_path") from None


def _schema_children(node):
    """Traverse subschemas, never keyword-like names inside instance literals."""
    if not isinstance(node, dict):
        return []
    children = []
    for keyword in ("$defs", "definitions", "properties", "dependentSchemas"):
        value = node.get(keyword)
        if isinstance(value, dict):
            children.extend(value.values())
    for keyword in ("additionalProperties", "unevaluatedProperties", "propertyNames", "items", "contains",
                    "unevaluatedItems", "not", "if", "then", "else"):
        if keyword in node:
            children.append(node[keyword])
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        value = node.get(keyword)
        if isinstance(value, list):
            children.extend(value)
    return children


def check_schema(schema: dict) -> None:
    canonical_bytes(schema)
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                # Static planning is bounded work. Regex evaluation and dynamic
                # schema recursion need a separately bounded validator service.
                if key in {"pattern", "patternProperties", "$dynamicRef", "$recursiveRef", "$id", "$anchor", "$dynamicAnchor"}:
                    raise PreparationError("unsupported_preparation_schema")
                if key in {"$ref", "$dynamicRef"} and (not isinstance(value, str) or not value.startswith("#/")):
                    raise PreparationError("external_schema_reference")
            pending.extend(_schema_children(node))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise PreparationError("invalid_preparation_schema") from None
    visits = 0
    def references(node, active, depth=0):
        nonlocal visits
        visits += 1
        if visits > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise PreparationError("preparation_contract_limit")
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref is not None:
                if ref in active:
                    raise PreparationError("recursive_preparation_schema")
                target = schema
                try:
                    for part in ref[2:].split("/"):
                        if re.search(r"~(?![01])", part):
                            raise KeyError()
                        part = part.replace("~1", "/").replace("~0", "~")
                        if isinstance(target, list):
                            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                                raise KeyError()
                            target = target[int(part)]
                        else:
                            target = target[part]
                except (KeyError, TypeError, IndexError):
                    raise PreparationError("invalid_preparation_schema_reference") from None
                if not isinstance(target, (dict, bool)):
                    raise PreparationError("invalid_preparation_schema_reference")
                references(target, active | {ref}, depth + 1)
            for child in _schema_children(node):
                references(child, active, depth + 1)
    try:
        references(schema, set())
    except RecursionError:
        raise PreparationError("preparation_contract_limit") from None


_schema_budget: ContextVar[int] = ContextVar("preparation_schema_budget", default=0)


def _bounded_keyword(check):
    def bounded(validator, value, instance, schema):
        budget = _schema_budget.get()
        if budget <= 0:
            raise PreparationError("preparation_schema_evaluation_limit")
        _schema_budget.set(budget - 1)
        yield from check(validator, value, instance, schema)
    return bounded


_BoundedValidator = extend(Draft202012Validator, {
    name: _bounded_keyword(check) for name, check in Draft202012Validator.VALIDATORS.items()
})


def schema_accepts(schema: dict, instance: Any, *, subschema: dict | None = None) -> bool:
    """Validate small static inputs with a shared budget across reference expansion.

    Container limits also bound single-keyword work such as uniqueItems, which
    does not recurse through the validator. No network or format hook is used.
    The containing schema remains the resolution root for form defaults.
    """
    canonical_bytes(instance)
    pending = [instance]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > MAX_VALIDATION_NODES:
            raise PreparationError("preparation_schema_evaluation_limit")
        if isinstance(item, (dict, list)):
            if len(item) > MAX_VALIDATION_ITEMS:
                raise PreparationError("preparation_schema_evaluation_limit")
            pending.extend(item.values() if isinstance(item, dict) else item)
    token = _schema_budget.set(MAX_SCHEMA_EVALUATIONS)
    try:
        validator = _BoundedValidator(schema)
        if subschema is not None:
            validator = validator.evolve(schema=subschema)
        return validator.is_valid(instance)
    except (RecursionError, OverflowError):
        raise PreparationError("preparation_schema_evaluation_limit") from None
    except _WrappedReferencingError:
        raise PreparationError("invalid_preparation_schema_reference") from None
    finally:
        _schema_budget.reset(token)


class Contract(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def strict_versions(cls, value):
        if isinstance(value, dict):
            for name in ("schema_version", "worker_api_version", "package_schema", "host_api_version"):
                if name in value and type(value[name]) is not int:
                    raise PreparationError("invalid_preparation_contract")
            for name in ("cpu_only", "gpu", "synthetic"):
                if name in value and type(value[name]) is not bool:
                    raise PreparationError("invalid_preparation_contract")
            canonical_bytes(value)
        return value


class RecipeReference(Contract):
    id: RecipeID
    version: Version
    digest: Digest


class PreparationPackage(Contract):
    id: RecipeID
    version: Version
    name: Name


class CallableInterface(Contract):
    kind: Literal["python-callable"]
    module: Annotated[str, Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", max_length=256)]
    function: Annotated[str, Field(pattern=r"^[A-Za-z_]\w*$", max_length=128)]
    tools: list[Tool] = Field(min_length=1, max_length=1)


class MCPInterface(Contract):
    kind: Literal["python-mcp"]
    module: Annotated[str, Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", max_length=256)]
    tools: list[Tool] = Field(min_length=1, max_length=128)


class Dependencies(Contract):
    project: Name
    lock_sha256: Digest
    python_version: Literal["3.12.13"]
    mcp_version: Literal["1.30.0"]

    @model_validator(mode="after")
    def path(self):
        relative_path(self.project, allow_dot=True)
        return self


class ArtifactSelection(Contract):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    role: Literal["model-weights", "runtime-file", "license", "validation"]
    sha256: Digest
    size: int = Field(ge=1, le=64 * 1024 * 1024 * 1024)
    origin: Name
    license: Name

    @model_validator(mode="after")
    def path(self):
        relative_path(self.license)
        return self


class PreparationResources(Contract):
    memory_mb: int = Field(ge=1, le=4096)
    cpu_only: Literal[True] = True
    gpu: Literal[False] = False


class SyntheticFixture(Contract):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    tool: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    arguments: dict[str, Any]
    expected: dict[str, Any]
    synthetic: Literal[True]


class PreparationDescriptor(Contract):
    schema_version: Literal[1]
    recipe: RecipeReference
    package: PreparationPackage
    interface: Annotated[CallableInterface | MCPInterface, Field(discriminator="kind")]
    dependencies: Dependencies
    source_license: Name
    artifacts: list[ArtifactSelection] = Field(default_factory=list, max_length=64)
    inputs: dict[str, Any] = Field(default_factory=dict)
    access: Access = Field(default_factory=Access)
    resources: PreparationResources
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    validation: list[SyntheticFixture] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def consistency(self):
        relative_path(self.source_license)
        names = [tool.name for tool in self.interface.tools]
        if len(set(names)) != len(names):
            raise PreparationError("duplicate_preparation_tool")
        for tool in self.interface.tools:
            check_schema(tool.input_schema)
            if tool.output_schema is not None:
                check_schema(tool.output_schema)
        for field in (self.artifacts, self.validation):
            keys = [item.name if isinstance(item, ArtifactSelection) else item.id for item in field]
            if len(set(keys)) != len(keys):
                raise PreparationError("duplicate_preparation_input")
        for fixture in self.validation:
            if fixture.tool not in names:
                raise PreparationError("unknown_fixture_tool")
            tool = next(tool for tool in self.interface.tools if tool.name == fixture.tool)
            if not schema_accepts(tool.input_schema, fixture.arguments):
                raise PreparationError("invalid_synthetic_fixture")
            if tool.output_schema is not None and not schema_accepts(tool.output_schema, fixture.expected):
                raise PreparationError("invalid_synthetic_fixture")
        return self

    @property
    def digest(self) -> str:
        return contract_digest("preparation-descriptor-v1", self.model_dump(exclude_none=True))


class RecipeRequirements(Contract):
    host_api_version: Literal[1] = 1
    package_schema: Literal[1] = 1
    protocol: Literal["2025-11-25"] = "2025-11-25"
    os: Literal["macos"] = "macos"
    architecture: Literal["arm64"] = "arm64"
    capabilities: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(default_factory=list, max_length=32)


class RecipeFormField(Contract):
    field: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    label: Name
    kind: Literal["string", "enum", "integer", "number", "boolean", "file"]
    options: list[Name] = Field(default_factory=list, max_length=64)
    default: str | int | float | bool | None = None


class RecipeStep(Contract):
    component: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    version: Version
    digest: Digest
    config: dict[str, Any] = Field(default_factory=dict)


class RecipeProvenance(Contract):
    origin: Name
    publisher: Name | None = None


class RecipeManifest(Contract):
    schema_version: Literal[1]
    id: RecipeID
    version: Version
    kind: Literal["declarative", "executable"]
    worker_api_version: Literal[1]
    input_schema: dict[str, Any]
    form: list[RecipeFormField] = Field(default_factory=list, max_length=64)
    steps: list[RecipeStep] = Field(min_length=1, max_length=32)
    requirements: RecipeRequirements = Field(default_factory=RecipeRequirements)
    licenses: list[Name] = Field(min_length=1, max_length=64)
    provenance: RecipeProvenance

    @model_validator(mode="after")
    def consistency(self):
        check_schema(self.input_schema)
        if self.input_schema.get("type") != "object" or self.input_schema.get("additionalProperties") is not False:
            raise PreparationError("closed_recipe_input_schema_required")
        properties = self.input_schema.get("properties", {})
        seen = set()
        for field in self.form:
            if field.field in seen or field.field not in properties:
                raise PreparationError("invalid_recipe_form")
            seen.add(field.field)
            schema = properties[field.field]
            expected_type = "string" if field.kind in {"enum", "file"} else field.kind
            if not isinstance(schema, dict) or schema.get("type") != expected_type:
                raise PreparationError("invalid_recipe_form")
            if field.kind == "enum" and (not field.options or field.options != schema.get("enum")):
                raise PreparationError("invalid_recipe_form")
            if field.kind != "enum" and field.options:
                raise PreparationError("invalid_recipe_form")
            if field.default is not None and not schema_accepts(self.input_schema, field.default, subschema=schema):
                raise PreparationError("invalid_recipe_form")
        for path in self.licenses:
            relative_path(path)
        return self


class RecipeArtifact(Contract):
    path: Name
    sha256: Digest
    size: int = Field(ge=0, le=16 * 1024 * 1024)
    license: Name
    origin: Name

    @model_validator(mode="after")
    def paths(self):
        relative_path(self.path)
        relative_path(self.license)
        return self


class RecipeLock(Contract):
    schema_version: Literal[1]
    manifest_sha256: Digest
    artifacts: list[RecipeArtifact] = Field(min_length=1, max_length=256)


class ExecutionPolicy(Contract):
    schema_version: Literal[1]
    operations: list[Literal["build", "probe"]] = Field(min_length=1, max_length=2)
    network: list[Name] = Field(default_factory=list, max_length=32)
    wall_seconds: int = Field(ge=1, le=3600)
    disk_bytes: int = Field(ge=1, le=64 * 1024 * 1024 * 1024)
    memory_mb: int = Field(ge=1, le=4096)

    @model_validator(mode="after")
    def unique_operations(self):
        if len(set(self.operations)) != len(self.operations):
            raise PreparationError("invalid_preparation_policy")
        return self

    @property
    def digest(self) -> str:
        return contract_digest("preparation-policy-v1", self.model_dump())


class JobBinding(Contract):
    schema_version: Literal[1]
    job_id: UUID
    import_id: UUID
    revision: int = Field(ge=1)
    source_revision: SourceIdentity
    descriptor_digest: Digest
    artifact_collection_digest: Digest
    recipe: RecipeReference
    policy_digest: Digest
    runner_digest: Digest
    verifier_digest: Digest


class PreparationReceipt(Contract):
    schema_version: Literal[1]
    binding: JobBinding
    dependency_lock_digest: Digest
    runtime_digest: Digest
    fixture_digest: Digest


def parse_descriptor(value: dict | bytes) -> PreparationDescriptor:
    try:
        return PreparationDescriptor.model_validate(load_contract(value) if isinstance(value, bytes) else value)
    except ValidationError as error:
        for item in error.errors():
            cause = item.get("ctx", {}).get("error")
            if isinstance(cause, PreparationError):
                raise PreparationError(cause.code) from None
        raise PreparationError("invalid_preparation_descriptor") from None
