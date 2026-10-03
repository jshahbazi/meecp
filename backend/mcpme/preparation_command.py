"""Trusted native JSON command family; directory bytes remain inert until probe.

The initial interface is one fixed executable/argv with JSON stdin/stdout and
system-only native loader dependencies. Schemas and synthetic fixtures are
explicit inputs; unknown interfaces and effects are never inferred as safe.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from typing import Literal

from pydantic import Field, field_validator

from .contracts import Access, Digest, HostCommand, HostCommandManifest, PackageError, Tool, _check_host_command_schema, validate_package
from .preparation_components import ComponentReference, PreparationDescriptorV2, SnapshotFile, SourceSnapshot, SourceBinding, SourceFileBinding
from .preparation_contracts import Contract, PreparationError, canonical_bytes, contract_digest, load_contract, relative_path
from .preparation_command_source import inspect_native_executable

COMPONENT_ID = "host-command-json-v1"
VERSION = "1.0.0"
PROVENANCE = "Exact original application directory; trusted fixed JSON command component"
MAX_SCHEMAS = 8
MAX_EXECUTABLES = 8


class CommandInput(Contract):
    candidate_id: Digest
    executable: str = Field(min_length=1, max_length=1024)
    input_schema_path: str = Field(min_length=1, max_length=1024)
    output_schema_path: str = Field(min_length=1, max_length=1024)
    license_paths: list[str] = Field(min_length=1, max_length=16)
    fixed_args: list[str] = Field(default_factory=list, max_length=16)
    protocol: Literal["json-stdin-stdout-v1"]
    effects: Literal["none"]
    helper_sha256: Digest
    deadline_ms: int = Field(ge=1, le=30000)
    input_bytes: int = Field(ge=1, le=16384)
    output_bytes: int = Field(ge=1, le=16384)

    @field_validator("executable", "input_schema_path", "output_schema_path")
    @classmethod
    def path(cls, value):
        return relative_path(value)

    @field_validator("license_paths")
    @classmethod
    def licenses(cls, values):
        if len(set(values)) != len(values):
            raise PreparationError("source_license_required")
        return [relative_path(value) for value in values]

    @field_validator("fixed_args")
    @classmethod
    def args(cls, values):
        if any("\0" in value or len(value.encode()) > 1024 for value in values):
            raise PreparationError("invalid_command_arguments")
        return values


def component_catalog():
    names = ("preparation_command.py", "preparation_command_source.py", "preparation_command_forms.py",
             "preparation_command_owner.py", "preparation_command_verifier.py", "command_execution_v2.py", "host_command.py")
    root = Path(__file__).parent
    digest = contract_digest("preparation-host-command-component-v1", [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in names])
    return {COMPONENT_ID + "@" + VERSION: {"digest": digest,
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": [], "package_schemas": [2]}}


def structural_schema(value, limit=16384):
    try:
        if type(value) is not dict or len(canonical_bytes(value)) > 16384 or value.get("type") != "object":
            raise ValueError()
        _check_host_command_schema(value, byte_limit=limit)
        return value
    except (ValueError, UnicodeError, PackageError):
        raise PreparationError("unsupported_host_command_schema") from None


def source_notices(snapshot):
    notices = [item.path for item in snapshot.files if item.size and item.size <= 65536
        and PurePosixPath(item.path).name.lower().startswith(("license", "licence", "copying", "notice"))]
    if len(notices) > 16:
        raise PreparationError("preparation_form_limit")
    return notices


def write_command_recipe(destination):
    """Emit a fresh immutable declarative recipe; no execution admission."""
    from .preparation_contracts import RecipeManifest
    from .recipes import validate_bundle
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent or destination.exists():
        raise PreparationError("recipe_destination_exists")
    component = component_catalog()[COMPONENT_ID + "@" + VERSION]
    manifest = RecipeManifest.model_validate({"schema_version": 1, "id": "mcpme.native-command", "version": VERSION,
        "kind": "declarative", "worker_api_version": 1,
        "input_schema": {"type": "object", "additionalProperties": False}, "form": [],
        "steps": [{"component": COMPONENT_ID, "version": VERSION, "digest": component["digest"]}],
        "requirements": {"package_schema": 2, "capabilities": ["declarative-composition"]}, "licenses": ["LICENSE"],
        "provenance": {"origin": "mcpme fixed native JSON command component", "publisher": "mcpme contributors"}})
    license_data = (Path(__file__).resolve().parents[2] / "templates/builtin-recipes/resolved-python-v1/LICENSE").read_bytes()
    files = {"LICENSE": license_data, "mcpme.recipe.json": canonical_bytes(manifest.model_dump(exclude_none=True))}
    files["mcpme.recipe.lock.json"] = canonical_bytes({"schema_version": 1,
        "manifest_sha256": hashlib.sha256(files["mcpme.recipe.json"]).hexdigest(),
        "artifacts": [{"path": "LICENSE", "sha256": hashlib.sha256(license_data).hexdigest(), "size": len(license_data),
            "license": "LICENSE", "origin": "mcpme repository license"}]})
    destination.mkdir(mode=0o700)
    for name, content in files.items():
        with (destination / name).open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        (destination / name).chmod(0o600)
    return validate_bundle(destination)


class CommandComponent:
    input_schema = CommandInput.model_json_schema()
    legacy_interface_kinds = frozenset()

    def __init__(self, family):
        self.family = family
        self.reference = ComponentReference(id=COMPONENT_ID, version=VERSION,
            digest=component_catalog()[COMPONENT_ID + "@" + VERSION]["digest"])

    def validate_source(self, descriptor, draft, recipe):
        selected, candidate, snapshot = self.family.validate_inputs(descriptor, draft)
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
            tuple(SourceFileBinding(file.path, file.sha256, file.size) for file in snapshot.files),
            contract_digest("preparation-command-interface-v1", {
                "input": selected.model_dump(), "tools": [tool.model_dump(exclude_none=True) for tool in descriptor.tools]}),
            None, canonical_bytes({"source_contract_version": 2, "source_identity": draft["source"]["digest"],
                "executable": selected.executable,
                "runtime_digest": contract_digest("preparation-command-runtime-v1", candidate["runtime"]),
                "file_metadata_digest": contract_digest("preparation-command-file-metadata-v1", draft["source"]["file_metadata"])}))


class CommandFamily:
    source_kind = "application_directory"
    target_profile = "host-command"
    preparation_owner_kind = "command-v3"

    def __init__(self, helper_sha256, *, snapshot_reader=None):
        # Constructor capabilities are supplied only by trusted host wiring.
        if snapshot_reader is None:
            from .preparation_application_source import read_application_snapshot
            snapshot_reader = read_application_snapshot
        if not re.fullmatch(r"[0-9a-f]{64}", helper_sha256) or not callable(snapshot_reader):
            raise PreparationError("command_preparation_not_admitted")
        self.helper_sha256, self._snapshot_reader = helper_sha256, snapshot_reader
        self.component = CommandComponent(self)
        from .preparation_command_forms import CommandFormProvider
        self.form_provider = CommandFormProvider(self)

    def snapshot_reader(self, draft):
        records = self._snapshot_reader(draft)
        return SourceSnapshot(draft["source"]["revision"], tuple(SnapshotFile(
            item.path, item.sha256, item.size, item.data) for _, item in sorted(records.items())))

    def supports_source(self, draft):
        return draft.get("source", {}).get("kind") == self.source_kind and draft["source"].get("contract_version") == 2

    def inspect_source(self, draft):
        result = {"schema_version": 1, "source_revision": draft["source"]["revision"], "candidates": [], "blockers": []}
        if not self.supports_source(draft):
            result["blockers"] = ["unsupported_command_source"]
            return result
        snapshot = self.snapshot_reader(draft)
        files = snapshot.by_path()
        metadata = {item["path"]: item for item in draft["source"]["file_metadata"]}
        executables = [name for name in files if metadata[name]["executable"] is True]
        schemas = [name for name in files if name.endswith(".input.schema.json")]
        if len(executables) > MAX_EXECUTABLES or len(schemas) > MAX_SCHEMAS:
            raise PreparationError("command_inspection_limit")
        if not executables:
            result["blockers"].append("command_executable_required")
        if not schemas:
            result["blockers"].append("command_interface_required")
        for executable in executables:
            try:
                runtime = inspect_native_executable(files[executable].data)
            except PreparationError as failure:
                result["blockers"].append(failure.code)
                continue
            for input_path in schemas:
                output_path = input_path.removesuffix(".input.schema.json") + ".output.schema.json"
                try:
                    if output_path not in files:
                        raise PreparationError("command_interface_required")
                    input_schema = structural_schema(load_contract(files[input_path].data))
                    output_schema = structural_schema(load_contract(files[output_path].data))
                except PreparationError as failure:
                    result["blockers"].append(failure.code)
                    continue
                candidate = {"kind": "native-json-command", "module": executable, "declared_entrypoint": executable,
                    "unresolved": [], "input_schema_path": input_path, "output_schema_path": output_path,
                    "input_schema": input_schema, "output_schema": output_schema, "runtime": runtime}
                candidate["candidate_id"] = contract_digest("preparation-command-source-candidate-v1", {
                    "source_revision": snapshot.revision, "component": self.component.reference.model_dump(), "candidate": candidate})
                result["candidates"].append(candidate)
        result["blockers"] = sorted(set(result["blockers"])) if not result["candidates"] else []
        return result

    def validate_inputs(self, descriptor, draft):
        if (not isinstance(descriptor, PreparationDescriptorV2) or len(descriptor.components) != 1
                or descriptor.components[0].component != self.component.reference):
            raise PreparationError("unsupported_component_descriptor")
        selected = CommandInput.model_validate(descriptor.components[0].input)
        candidate = next((item for item in self.inspect_source(draft)["candidates"] if item["candidate_id"] == selected.candidate_id), None)
        if (candidate is None or selected.helper_sha256 != self.helper_sha256
                or selected.executable != candidate["module"] or selected.input_schema_path != candidate["input_schema_path"]
                or selected.output_schema_path != candidate["output_schema_path"]):
            raise PreparationError("command_candidate_mismatch")
        if (len(descriptor.tools) != 1 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", descriptor.tools[0].name)
                or descriptor.tools[0].input_schema != candidate["input_schema"]
                or descriptor.tools[0].output_schema != candidate["output_schema"]):
            raise PreparationError("command_interface_mismatch")
        structural_schema(descriptor.tools[0].input_schema, selected.input_bytes)
        structural_schema(descriptor.tools[0].output_schema, selected.output_bytes)
        if (descriptor.artifacts or descriptor.inputs or descriptor.access != Access() or descriptor.resources.gpu
                or not 1 <= len(descriptor.validation) <= 4
                or any(item.tool != descriptor.tools[0].name or len(canonical_bytes(item.arguments)) > selected.input_bytes
                       or len(canonical_bytes(item.expected)) > selected.output_bytes for item in descriptor.validation)):
            raise PreparationError("command_authority_mismatch")
        snapshot = self.snapshot_reader(draft)
        if set(selected.license_paths) != set(source_notices(snapshot)):
            raise PreparationError("source_license_required")
        for name in selected.license_paths:
            item = snapshot.by_path().get(name)
            if item is None or not item.data or len(item.data) > 65536:
                raise PreparationError("source_license_required")
            try:
                item.data.decode("utf-8")
            except UnicodeError:
                raise PreparationError("source_license_required") from None
        return selected, candidate, snapshot

    def command(self, descriptor, draft):
        selected, _, _ = self.validate_inputs(descriptor, draft)
        return HostCommand(version=1, component="macos26-arm64-command-v3", helper_sha256=self.helper_sha256,
            commands={descriptor.tools[0].name: {"executable": "source/" + selected.executable,
                "args": selected.fixed_args, "cwd": "source"}}, deadline_ms=selected.deadline_ms,
            input_bytes=selected.input_bytes, output_bytes=selected.output_bytes, queue_limit=1)

    def source_artifacts(self, descriptor, draft):
        selected, _, snapshot = self.validate_inputs(descriptor, draft)
        return [{"path": "source/" + item.path, "sha256": item.sha256, "size": item.size,
            "executable": item.path == selected.executable, "source": draft["source"]["revision"],
            "provenance": PROVENANCE, "license": "source/" + (item.path if item.path in selected.license_paths
                else selected.license_paths[0])} for item in snapshot.files]

    def expected_package_contract(self, descriptor, draft):
        selected, _, _ = self.validate_inputs(descriptor, draft)
        return {"package_id": descriptor.package.id, "version": descriptor.package.version, "name": descriptor.package.name,
            "source_revision": draft["source"]["revision"], "profile": "host-command", "schema_version": 2,
            "tools": [tool.model_dump(exclude_none=True) for tool in descriptor.tools],
            "access": descriptor.access.model_dump(), "resources": descriptor.resources.model_dump(),
            "lifecycle": descriptor.lifecycle.model_dump(), "command": self.command(descriptor, draft).model_dump(),
            "licenses": sorted("source/" + path for path in selected.license_paths),
            "source_artifacts": self.source_artifacts(descriptor, draft)}

    def bound_inputs(self, descriptor, draft, recipe, registry, runtime_digest):
        from .preparation_inputs import RuntimeReference, adapt_descriptor, bind_preparation_inputs
        view = adapt_descriptor(descriptor, draft=draft, recipe=recipe, registry=registry)
        return bind_preparation_inputs(view, draft=draft, captured_collection=None,
            runtimes=(RuntimeReference(name="command-helper-v3", digest=self.helper_sha256),), capabilities=("build", "probe"),
            expected_package_contract=self.expected_package_contract(descriptor, draft))

    def prepare(self, descriptor, draft, toolchain, output, run, check, *, acquire_wheels, job_id):
        selected, _, snapshot = self.validate_inputs(descriptor, draft)
        if not output.is_absolute() or output.exists() or output.parent.resolve() != output.parent:
            raise PreparationError("private_preparation_workspace_required")
        output.mkdir(mode=0o700)
        for item in snapshot.files:
            check()
            target = output / "source" / item.path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700 if item.path == selected.executable else 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(item.data)
                stream.flush()
                os.fsync(stream.fileno())
        expected = self.expected_package_contract(descriptor, draft)
        manifest = HostCommandManifest.model_validate({**{k: v for k, v in expected.items() if k != "source_artifacts"},
            "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
            "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
            "validation": {"fixtures": [{"tool": item.tool, "arguments": item.arguments, "expected": item.expected}
                for item in descriptor.validation]}})
        data = canonical_bytes(manifest.model_dump(exclude_none=True))
        lock = {"schema_version": 1, "package_id": manifest.package_id, "version": manifest.version,
            "manifest_sha256": hashlib.sha256(data).hexdigest(), "artifacts": expected["source_artifacts"],
            "build": {"recipe": descriptor.recipe.id + " " + descriptor.recipe.version,
                "builder": "mcpme host command copy v1", "toolchain": "native-system-loader-v1"}}
        for name, content in (("mcpme.package.json", data), ("mcpme.lock.json", canonical_bytes(lock))):
            with (output / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            (output / name).chmod(0o600)
        check()
        return validate_package(output)

    async def verify(self, *args, **kwargs):
        from .preparation_command_verifier import verify_command_package
        return await verify_command_package(*args, **kwargs)

    def release(self, job_id, descriptor=None):
        pass
