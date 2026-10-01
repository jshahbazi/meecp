"""Static, closed-tree prepared package contract for the initial stdio profile."""
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

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

SCHEMA_VERSION = 1
MANIFEST_NAME = "mcpme.package.json"
LOCK_NAME = "mcpme.lock.json"
MAX_METADATA_BYTES = 1024 * 1024
MAX_ARTIFACTS = 20_000
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
Label = Annotated[str, Field(min_length=1, max_length=256, strict=True)]


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

    def mcp_schema(self) -> dict[str, Any]:
        result = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.output_schema is not None:
            result["outputSchema"] = self.output_schema
        return result


class Access(StrictModel):
    filesystem: list[Label] = Field(default_factory=list, max_length=128)
    network: list[Label] = Field(default_factory=list, max_length=128)
    secrets: list[Label] = Field(default_factory=list, max_length=0)
    writes: bool = False


class Resources(StrictModel):
    memory_mb: int = Field(ge=1, le=4096)
    cpu_only: Literal[True]


class PackageManifest(StrictModel):
    schema_version: Literal[1]
    host_api_version: Literal[1] = 1
    package_id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$", max_length=128)]
    version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")]
    source_revision: Label
    name: Label
    profile: Literal["mcp-stdio"]
    platform: Platform
    entrypoint: Entrypoint
    protocol: Protocol
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    tools: list[Tool] = Field(min_length=1, max_length=128)
    access: Access = Field(default_factory=Access)
    resources: Resources
    licenses: list[Label] = Field(min_length=1, max_length=128)


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
    data = _read_file(root / name, MAX_METADATA_BYTES)
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
        if (type(manifest_json.get("schema_version")) is not int or manifest_json.get("schema_version") != 1
                or type(lock_json.get("schema_version")) is not int or lock_json.get("schema_version") != 1):
            raise PackageError("unsupported_package_schema")
        if manifest_json.get("profile") in ("mcp-http", "model-service"):
            raise PackageError("unsupported_execution_profile")
        manifest = PackageManifest.model_validate(manifest_json)
        lock = PackageLock.model_validate(lock_json)
        if (type(manifest_json.get("host_api_version", 1)) is not int
                or manifest_json.get("resources", {}).get("cpu_only") is not True):
            raise PackageError("invalid_package_schema")
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
        entry = manifest.entrypoint
        package_relative_path(entry.executable)
        package_relative_path(entry.cwd, allow_dot=True)
        if entry.executable not in artifacts or not artifacts[entry.executable].executable:
            raise PackageError("entrypoint_not_executable")
        if entry.cwd not in directories or any("\x00" in arg for arg in entry.args):
            raise PackageError("invalid_entrypoint")
        if len({tool.name for tool in manifest.tools}) != len(manifest.tools):
            raise PackageError("duplicate_tool")
        for tool in manifest.tools:
            _check_tool_schema(tool.input_schema)
            if tool.output_schema is not None:
                _check_tool_schema(tool.output_schema)
        # Recheck tree and metadata so an artifact added/removed during hashing is rejected.
        if _tree(root) != (files, directories) or _read_file(root / MANIFEST_NAME, MAX_METADATA_BYTES) != manifest_bytes or _read_file(root / LOCK_NAME, MAX_METADATA_BYTES) != lock_bytes:
            raise PackageError("package_changed")
        # Bind both metadata byte sequences; the lock binds the full artifact closure.
        digest = hashlib.sha256(manifest_bytes + b"\x00" + lock_bytes).hexdigest()
        return ValidatedPackage(root=root, digest=digest, manifest=manifest, lock=lock)
    except ValidationError:
        raise PackageError("invalid_package_schema") from None
    except (OSError, RecursionError):
        raise PackageError("unsafe_package_file") from None
