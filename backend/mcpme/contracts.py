"""Static, closed-tree prepared package contracts for owned MCP services."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationError, field_validator,
                      model_serializer, model_validator)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .limits import DEFAULT_REQUEST_BYTES, MAX_REQUEST_BYTES

SCHEMA_VERSION = 1
MANIFEST_NAME = "mcpme.package.json"
LOCK_NAME = "mcpme.lock.json"
MAX_METADATA_BYTES = 1024 * 1024
MAX_LOCK_METADATA_BYTES = 16 * 1024 * 1024
MAX_ARTIFACTS = 20_000
MAX_PACKAGE_BYTES = 64 * 1024 * 1024 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
Label = Annotated[str, Field(min_length=1, max_length=256, strict=True)]
SettingName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$", strict=True)]
MAX_CONFIGURATION_INTEGER = 2 ** 53


class PackageError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Platform(StrictModel):
    os: Literal["macos"]
    architecture: Literal["arm64"]
    minimum_version: Literal["26.0"]


class Entrypoint(StrictModel):
    executable: Label
    args: list[Annotated[str, Field(max_length=4096)]] = Field(default_factory=list, max_length=128)
    cwd: Label = "."


class Protocol(StrictModel):
    version: Literal["2025-11-25"]
    capabilities: list[Literal["tools"]] = Field(min_length=1, max_length=1)


class Lifecycle(StrictModel):
    max_request_bytes: int = Field(default=DEFAULT_REQUEST_BYTES, ge=1, le=MAX_REQUEST_BYTES)
    max_sessions: int = Field(default=8, ge=1, le=64)
    idle_timeout_seconds: int = Field(default=300, ge=1, le=300)
    operation_timeout_seconds: int = Field(default=60, ge=1, le=300)
    startup_timeout_seconds: int = Field(default=30, ge=1, le=300)
    stop_grace_seconds: int = Field(default=2, ge=1, le=10)


class Tool(StrictModel):
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    description: Annotated[str, Field(min_length=1, max_length=4096)]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, bool | str] | None = None

    def mcp_schema(self) -> dict[str, Any]:
        result = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.output_schema is not None:
            result["outputSchema"] = self.output_schema
        if self.annotations is not None:
            result["annotations"] = self.annotations
        return result


class Access(StrictModel):
    filesystem: list[Label] = Field(default_factory=list, max_length=128)
    network: list[Label] = Field(default_factory=list, max_length=128)
    secrets: list[SettingName] = Field(default_factory=list, max_length=16)
    writes: bool = False

    @field_validator("secrets")
    @classmethod
    def unique_secrets(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("invalid_secret_declaration")
        return value


class ConfigurationField(StrictModel):
    """A bounded primitive setting; package schemas cannot select arbitrary validators."""

    type: Literal["string", "integer", "boolean"]
    required: bool = False
    default: str | int | bool | None = None
    enum: list[str | int | bool] | None = Field(default=None, min_length=1, max_length=32)
    min_length: int | None = Field(default=None, ge=0, le=4096)
    max_length: int | None = Field(default=None, ge=0, le=4096)
    minimum: int | None = Field(default=None, ge=-MAX_CONFIGURATION_INTEGER, le=MAX_CONFIGURATION_INTEGER)
    maximum: int | None = Field(default=None, ge=-MAX_CONFIGURATION_INTEGER, le=MAX_CONFIGURATION_INTEGER)

    @model_serializer(mode="wrap")
    def serialize_declaration(self, handler):
        result = handler(self)
        # Omitted constraints/defaults stay omitted when a manifest is exported.
        # Explicit null is invalid, so a model dump must never invent it.
        for name in ("default", "enum", "min_length", "max_length", "minimum", "maximum"):
            if name not in self.model_fields_set:
                result.pop(name, None)
        return result

    def accepts(self, value: Any) -> bool:
        expected = {"string": str, "integer": int, "boolean": bool}[self.type]
        if type(value) is not expected:
            return False
        if self.type == "string":
            try:
                value.encode("utf-8")
            except UnicodeError:
                return False
            if "\x00" in value or not (self.min_length or 0) <= len(value) <= (
                    self.max_length if self.max_length is not None else 4096):
                return False
        elif self.type == "integer":
            if not (self.minimum if self.minimum is not None else -MAX_CONFIGURATION_INTEGER) <= value <= (
                    self.maximum if self.maximum is not None else MAX_CONFIGURATION_INTEGER):
                return False
        if self.enum is not None and not any(type(item) is type(value) and item == value for item in self.enum):
            return False
        return True

    @model_validator(mode="after")
    def closed_constraints(self) -> ConfigurationField:
        supplied = self.model_fields_set
        optional = {"default", "enum", "min_length", "max_length", "minimum", "maximum"}
        if any(getattr(self, name) is None for name in supplied & optional):
            raise ValueError("invalid_configuration_declaration")
        if ({"minimum", "maximum"} & supplied and self.type != "integer"
                or {"min_length", "max_length"} & supplied and self.type != "string"):
            raise ValueError("invalid_configuration_declaration")
        if (self.min_length is not None and self.max_length is not None and self.min_length > self.max_length
                or self.minimum is not None and self.maximum is not None and self.minimum > self.maximum):
            raise ValueError("invalid_configuration_declaration")
        if self.enum is not None:
            expected = {"string": str, "integer": int, "boolean": bool}[self.type]
            if any(type(item) is not expected for item in self.enum) or len(set(self.enum)) != len(self.enum):
                raise ValueError("invalid_configuration_declaration")
            # The declared choices must satisfy the same bounds as supplied values.
            if any(not self.accepts(item) for item in self.enum):
                raise ValueError("invalid_configuration_declaration")
        if "default" in supplied and not self.accepts(self.default):
            raise ValueError("invalid_configuration_declaration")
        return self


class Configuration(StrictModel):
    schema_version: Literal[1] = 1
    fields: dict[SettingName, ConfigurationField] = Field(default_factory=dict, max_length=32)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("invalid_configuration_declaration")
        return value


class Resources(StrictModel):
    memory_mb: int = Field(ge=1, le=262144)
    cpu_only: bool
    gpu: bool = False


class Readiness(StrictModel):
    path: Annotated[str, Field(pattern=r"^/[A-Za-z0-9_/-]{1,127}$")]
    echo_generation: bool = True


class ManagedProcess(StrictModel):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    role: Literal["engine", "adapter"]
    entrypoint: Entrypoint
    depends_on: list[Label] = Field(default_factory=list, max_length=16)
    readiness: Readiness


class ModelArtifact(StrictModel):
    artifact: Label
    model_id: Label
    sha256: Digest
    profile: Label
    context_tokens: int = Field(ge=128, le=1048576)
    engine_identity: dict[Label, Label]
    parameters: dict[Label, str | int | float | bool] = Field(default_factory=dict)


class HTTPService(StrictModel):
    configuration: Literal["descriptor-v1"]
    session_termination: Literal["delete"]


class SmokeFixture(StrictModel):
    tool: Label
    arguments: dict[str, Any]
    expected: dict[str, Any]


class Validation(StrictModel):
    fixtures: list[SmokeFixture] = Field(default_factory=list, max_length=4)


class PackageManifest(StrictModel):
    schema_version: Literal[1]
    host_api_version: Literal[1] = 1
    package_id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", max_length=128)]
    version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")]
    source_revision: Label
    name: Label
    profile: Literal["mcp-stdio", "mcp-http", "model-service"]
    platform: Platform
    entrypoint: Entrypoint | None = None
    processes: list[ManagedProcess] = Field(default_factory=list, max_length=16)
    adapter_process: Label | None = None
    model: ModelArtifact | None = None
    http: HTTPService | None = None
    validation: Validation = Field(default_factory=Validation)
    protocol: Protocol
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    tools: list[Tool] = Field(min_length=1, max_length=128)
    access: Access = Field(default_factory=Access)
    configuration: Configuration = Field(default_factory=Configuration)
    resources: Resources
    licenses: list[Label] = Field(min_length=1, max_length=128)


class HostCommand(StrictModel):
    """Versioned fixed argv / JSON stdin contract; selected code has no host API."""
    version: Literal[1]
    component: Literal["macos26-arm64-command-v3"]
    helper_sha256: Digest
    commands: dict[SettingName, Entrypoint] = Field(min_length=1, max_length=16)
    deadline_ms: int = Field(ge=1, le=30000)
    input_bytes: int = Field(ge=1, le=65536)
    output_bytes: int = Field(ge=1, le=65536)
    queue_limit: int = Field(default=4, ge=1, le=8)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid_host_command_contract")
        return value


class HostCommandManifest(PackageManifest):
    schema_version: Literal[2]
    profile: Literal["host-command"]
    command: HostCommand


class Artifact(StrictModel):
    path: Label
    sha256: Digest
    size: int = Field(ge=0, le=MAX_PACKAGE_BYTES)
    executable: bool = False
    source: Label
    provenance: Label
    license: Label


class Build(StrictModel):
    recipe: Label
    builder: Label
    toolchain: Label


class PackageLock(StrictModel):
    schema_version: Literal[1]
    package_id: Label
    version: Label
    manifest_sha256: Digest
    artifacts: list[Artifact] = Field(min_length=1, max_length=MAX_ARTIFACTS)
    build: Build


@dataclass(frozen=True)
class ValidatedPackage:
    root: Path
    digest: str
    manifest: PackageManifest
    lock: PackageLock


def package_relative_path(value: str, *, allow_dot: bool = False) -> PurePosixPath:
    if allow_dot and value == ".":
        return PurePosixPath(".")
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value or path.is_absolute()
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(char) < 32 for char in value) or str(path) != value):
        raise PackageError("unsafe_package_path")
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("invalid_package_json")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise PackageError("invalid_package_json")
    return result


def _read_file(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PackageError("unsafe_package_file")
            if before.st_size > limit:
                raise PackageError("package_size_exceeded")
            data = source.read(limit + 1)
            after = os.fstat(source.fileno())
            current = path.lstat()
            attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if len(data) > limit or any(getattr(before, key) != getattr(after, key)
                                       or getattr(after, key) != getattr(current, key) for key in attrs):
                raise PackageError("package_changed")
            return data
    except OSError:
        raise PackageError("unsafe_package_file") from None


def _metadata(root: Path, name: str) -> tuple[bytes, dict[str, Any]]:
    data = _read_file(root / name, MAX_LOCK_METADATA_BYTES if name == LOCK_NAME else MAX_METADATA_BYTES)
    try:
        value = json.loads(data, object_pairs_hook=_unique_object,
                           parse_float=_finite_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(PackageError("invalid_package_json")))
        if not isinstance(value, dict):
            raise PackageError("invalid_package_schema")
        return data, value
    except (ValueError, UnicodeError, RecursionError):
        raise PackageError("invalid_package_json") from None


def _verify_artifact(path: Path, artifact: Artifact) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PackageError("unsafe_package_file")
            if before.st_size != artifact.size:
                raise PackageError("artifact_size_mismatch")
            if bool(before.st_mode & 0o111) != artifact.executable:
                raise PackageError("artifact_mode_mismatch")
            actual = hashlib.sha256()
            read = 0
            while chunk := source.read(min(1024 * 1024, artifact.size - read + 1)):
                read += len(chunk)
                if read > artifact.size:
                    raise PackageError("package_changed")
                actual.update(chunk)
            after = os.fstat(source.fileno())
            current = path.lstat()
            attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if any(getattr(before, key) != getattr(after, key)
                   or getattr(after, key) != getattr(current, key) for key in attrs):
                raise PackageError("package_changed")
            if read != artifact.size or actual.hexdigest() != artifact.sha256:
                raise PackageError("artifact_hash_mismatch")
    except OSError:
        raise PackageError("unsafe_package_file") from None


def _check_tool_schema(schema: dict[str, Any]) -> None:
    if schema.get("type") != "object":
        raise PackageError("invalid_tool_schema")
    # Package contracts remain self-contained and never trigger reference retrieval.
    pending: list[Any] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise PackageError("external_schema_reference")
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise PackageError("invalid_tool_schema") from None


def _check_host_command_schema(schema: dict[str, Any], byte_limit: int) -> None:
    """Bound trusted-host validation work; never evaluate selected regex/refs."""
    unsupported = "unsupported_host_command_schema"
    def size(value):
        try:
            return len(value.encode())
        except UnicodeError:
            raise PackageError(unsupported) from None
    pending = [(schema, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if type(value) is not dict or nodes > 128 or depth > 8:
            raise PackageError(unsupported)
        kind = value.get("type")
        common = {"type", "title", "description"}
        allowed = {"object": {"properties", "required", "additionalProperties"},
                   "array": {"items", "minItems", "maxItems"},
                   "string": {"minLength", "maxLength", "enum"},
                   "integer": {"minimum", "maximum", "enum"},
                   "number": {"minimum", "maximum", "enum"},
                   "boolean": {"enum"}, "null": {"enum"}}
        if type(kind) is not str or kind not in allowed or set(value) - common - allowed[kind]:
            raise PackageError(unsupported)
        for key in ("title", "description"):
            if key in value and (type(value[key]) is not str or size(value[key]) > min(byte_limit, 4096)):
                raise PackageError(unsupported)
        if kind == "object":
            properties = value.get("properties")
            required = value.get("required", [])
            if (type(properties) is not dict or len(properties) > 32 or value.get("additionalProperties") is not False
                    or any(type(key) is not str or not 1 <= size(key) <= min(byte_limit, 256) for key in properties)
                    or type(required) is not list or len(required) > 32
                    or any(type(key) is not str or key not in properties for key in required)
                    or len(set(required)) != len(required)):
                raise PackageError(unsupported)
            pending.extend((child, depth + 1) for child in properties.values())
        elif kind in {"array", "string"}:
            lower, upper = ("minItems", "maxItems") if kind == "array" else ("minLength", "maxLength")
            maximum = value.get(upper)
            minimum = value.get(lower, 0)
            ceiling = min(byte_limit, 64 if kind == "array" else 16384)
            if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= ceiling:
                raise PackageError(unsupported)
            if kind == "array":
                pending.append((value.get("items"), depth + 1))
        if kind in {"integer", "number"}:
            for key in ("minimum", "maximum"):
                if key in value and (type(value[key]) not in {int, float}
                                     or type(value[key]) is float and not math.isfinite(value[key])):
                    raise PackageError(unsupported)
            if "minimum" in value and "maximum" in value and value["minimum"] > value["maximum"]:
                raise PackageError(unsupported)
        if "enum" in value:
            enum = value["enum"]
            types = {"string": {str}, "integer": {int}, "number": {int, float}, "boolean": {bool}, "null": {type(None)}}
            if (type(enum) is not list or not 1 <= len(enum) <= 32
                    or any(type(item) not in types[kind] or type(item) is float and not math.isfinite(item) for item in enum)
                    or size(json.dumps(enum, ensure_ascii=False, allow_nan=False)) > byte_limit
                    or kind == "string" and any(size(item) > min(byte_limit, 16384) for item in enum)):
                raise PackageError(unsupported)


def _tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = {"."}
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            for item in directory.iterdir():
                relative = str(item.relative_to(root))
                package_relative_path(relative)
                details = item.lstat()
                if details.st_mode & 0o7022:
                    raise PackageError("unsafe_package_file")
                if stat.S_ISDIR(details.st_mode):
                    directories.add(relative)
                    pending.append(item)
                elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                    files.add(relative)
                else:
                    raise PackageError("unsafe_package_file")
                if len(files) + len(directories) > MAX_ARTIFACTS * 2:
                    raise PackageError("package_size_exceeded")
        return files, directories
    except OSError:
        raise PackageError("unsafe_package_file") from None


def validate_package(root: Path) -> ValidatedPackage:
    """Read and verify metadata/artifacts without importing or executing package code."""
    root = Path(root).absolute()
    try:
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise PackageError("unsafe_package_path")
        manifest_bytes, manifest_json = _metadata(root, MANIFEST_NAME)
        lock_bytes, lock_json = _metadata(root, LOCK_NAME)
        if (type(manifest_json.get("schema_version")) is not int or manifest_json.get("schema_version") not in (1, 2)
                or type(lock_json.get("schema_version")) is not int or lock_json.get("schema_version") != 1):
            raise PackageError("unsupported_package_schema")
        if manifest_json["schema_version"] == 2 and manifest_json.get("profile") != "host-command":
            raise PackageError("unsupported_package_schema")
        manifest = (PackageManifest if manifest_json["schema_version"] == 1 else HostCommandManifest).model_validate(manifest_json)
        lock = PackageLock.model_validate(lock_json)
        if type(manifest_json.get("host_api_version", 1)) is not int:
            raise PackageError("invalid_package_schema")
        if manifest.resources.cpu_only and manifest.resources.gpu:
            raise PackageError("invalid_resource_declaration")
        if manifest.profile == "host-command":
            if (manifest.entrypoint is not None or manifest.processes or manifest.model is not None
                    or manifest.http is not None or manifest.adapter_process is not None
                    or not manifest.resources.cpu_only or manifest.resources.gpu or manifest.resources.memory_mb > 4096
                    or manifest.access != Access() or manifest.configuration.fields
                    or set(manifest.command.commands) != {tool.name for tool in manifest.tools}
                    or manifest.command.deadline_ms > manifest.lifecycle.operation_timeout_seconds * 1000
                    or any(tool.output_schema is None or tool.input_schema.get("additionalProperties") is not False
                           or tool.output_schema.get("additionalProperties") is not False for tool in manifest.tools)):
                raise PackageError("invalid_host_command_contract")
            for tool in manifest.tools:
                _check_host_command_schema(tool.input_schema, manifest.command.input_bytes)
                _check_host_command_schema(tool.output_schema, manifest.command.output_bytes)
        elif manifest.profile == "mcp-stdio":
            if (manifest.entrypoint is None or manifest.processes or manifest.model is not None
                    or manifest.http is not None or manifest.adapter_process is not None):
                raise PackageError("invalid_package_schema")
            if manifest.resources.gpu and manifest.lifecycle.max_sessions != 1:
                raise PackageError("invalid_resource_declaration")
        elif manifest.profile == "mcp-http":
            if manifest.http is None:
                raise PackageError("http_configuration_required")
            if (manifest.entrypoint is not None or manifest.model is not None or len(manifest.processes) != 1
                    or not manifest.resources.cpu_only or manifest.resources.gpu or manifest.resources.memory_mb > 4096):
                raise PackageError("unsupported_process_topology")
            service = manifest.processes[0]
            if (manifest.adapter_process != service.id or service.role != "adapter" or service.depends_on
                    or service.readiness.path != "/health" or not service.readiness.echo_generation):
                raise PackageError("http_identity_contract_required")
        else:
            if manifest.entrypoint is not None or not manifest.processes or manifest.model is None or manifest.http is not None:
                raise PackageError("invalid_package_schema")
            processes = {process.id: process for process in manifest.processes}
            if len(processes) != len(manifest.processes) or manifest.adapter_process not in processes:
                raise PackageError("invalid_process_graph")
            if sum(process.role == "adapter" for process in manifest.processes) != 1:
                raise PackageError("invalid_process_graph")
            adapter = processes[manifest.adapter_process]
            if adapter.role != "adapter" or not adapter.readiness.echo_generation:
                raise PackageError("invalid_process_graph")
            visited: set[str] = set()
            remaining = list(manifest.processes)
            while remaining:
                ready = [process for process in remaining if set(process.depends_on) <= visited]
                if not ready:
                    raise PackageError("invalid_process_graph")
                for process in ready:
                    if len(set(process.depends_on)) != len(process.depends_on) or process.id in process.depends_on:
                        raise PackageError("invalid_process_graph")
                    visited.add(process.id)
                    remaining.remove(process)
            # The public adapter must depend on all resident engine processes.
            dependencies = set(adapter.depends_on)
            pending = list(dependencies)
            while pending:
                item = pending.pop()
                for dependency in processes[item].depends_on:
                    if dependency not in dependencies:
                        dependencies.add(dependency)
                        pending.append(dependency)
            if set(processes) - {adapter.id} != dependencies:
                raise PackageError("invalid_process_graph")
            if any(isinstance(value, float) and not math.isfinite(value) for value in manifest.model.parameters.values()):
                raise PackageError("invalid_model_profile")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})", manifest.source_revision):
            raise PackageError("source_unresolved")
        if manifest.package_id != lock.package_id or manifest.version != lock.version:
            raise PackageError("package_lock_mismatch")
        if hashlib.sha256(manifest_bytes).hexdigest() != lock.manifest_sha256:
            raise PackageError("manifest_hash_mismatch")
        artifacts = {artifact.path: artifact for artifact in lock.artifacts}
        if len(artifacts) != len(lock.artifacts):
            raise PackageError("duplicate_artifact")
        for name in artifacts:
            package_relative_path(name)
            if name in (MANIFEST_NAME, LOCK_NAME):
                raise PackageError("invalid_artifact_path")
        files, directories = _tree(root)
        if files != set(artifacts) | {MANIFEST_NAME, LOCK_NAME}:
            raise PackageError("artifact_tree_mismatch")
        required_dirs = {"."}
        for name in files:
            required_dirs.update(str(parent) for parent in PurePosixPath(name).parents)
        if directories != required_dirs:
            raise PackageError("artifact_tree_mismatch")
        if sum(artifact.size for artifact in artifacts.values()) > MAX_PACKAGE_BYTES:
            raise PackageError("package_size_exceeded")
        for artifact in artifacts.values():
            package_relative_path(artifact.license)
            if artifact.license not in manifest.licenses:
                raise PackageError("license_required")
            _verify_artifact(root / artifact.path, artifact)
        for license_path in manifest.licenses:
            package_relative_path(license_path)
            if license_path not in artifacts or artifacts[license_path].size == 0:
                raise PackageError("license_required")
        entries = ([manifest.entrypoint] if manifest.profile == "mcp-stdio" else
                   list(manifest.command.commands.values()) if manifest.profile == "host-command" else
                   [process.entrypoint for process in manifest.processes])
        for entry in entries:
            package_relative_path(entry.executable)
            package_relative_path(entry.cwd, allow_dot=True)
            if entry.executable not in artifacts or not artifacts[entry.executable].executable:
                raise PackageError("entrypoint_not_executable")
            if entry.cwd not in directories or any("\x00" in arg for arg in entry.args):
                raise PackageError("invalid_entrypoint")
            if manifest.profile in {"model-service", "mcp-http"} and any(arg == "--mcpme-config-fd" for arg in entry.args):
                raise PackageError("invalid_entrypoint")
        if manifest.model is not None:
            package_relative_path(manifest.model.artifact)
            model_artifact = artifacts.get(manifest.model.artifact)
            if model_artifact is None or model_artifact.sha256 != manifest.model.sha256:
                raise PackageError("model_identity_mismatch")
        if len({tool.name for tool in manifest.tools}) != len(manifest.tools):
            raise PackageError("duplicate_tool")
        for tool in manifest.tools:
            if tool.annotations is not None:
                if set(tool.annotations) - {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}:
                    raise PackageError("invalid_tool_schema")
                if any((not isinstance(value, str) or not 1 <= len(value) <= 256) if key == "title"
                       else type(value) is not bool for key, value in tool.annotations.items()):
                    raise PackageError("invalid_tool_schema")
            _check_tool_schema(tool.input_schema)
            if tool.output_schema is not None:
                _check_tool_schema(tool.output_schema)
        tools = {tool.name: tool for tool in manifest.tools}
        for fixture in manifest.validation.fixtures:
            if len(json.dumps(fixture.model_dump(), separators=(",", ":"), allow_nan=False).encode()) > 16384:
                raise PackageError("smoke_fixture_limit")
            tool = tools.get(fixture.tool)
            if (tool is None or not Draft202012Validator(tool.input_schema).is_valid(fixture.arguments)
                    or tool.output_schema is not None and not Draft202012Validator(tool.output_schema).is_valid(fixture.expected)):
                raise PackageError("invalid_smoke_fixture")
        # Recheck tree and metadata so an artifact added/removed during hashing is rejected.
        if _tree(root) != (files, directories) or _read_file(root / MANIFEST_NAME, MAX_METADATA_BYTES) != manifest_bytes or _read_file(root / LOCK_NAME, MAX_LOCK_METADATA_BYTES) != lock_bytes:
            raise PackageError("package_changed")
        # Bind both metadata byte sequences; the lock binds the full artifact closure.
        digest = hashlib.sha256(manifest_bytes + b"\x00" + lock_bytes).hexdigest()
        return ValidatedPackage(root=root, digest=digest, manifest=manifest, lock=lock)
    except ValidationError:
        raise PackageError("invalid_package_schema") from None
    except (OSError, RecursionError):
        raise PackageError("unsafe_package_file") from None
