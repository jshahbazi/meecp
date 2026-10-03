"""Static CLI source binding. The full A3 capability remains unregistered."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Tool
from .preparation_contracts import Contract, PreparationError, canonical_bytes, check_schema, contract_digest, load_contract, relative_path

COMPONENT_ID = "cli-json-v1"
VERSION = "1.0.0"


class ArgField(Contract):
    field: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]


class CliJsonSpec(Contract):
    schema_version: Literal[1]
    runtime: Literal["private-python-stdlib-v1"]
    script: str = Field(min_length=1, max_length=4096)
    source_license: str = Field(min_length=1, max_length=4096)
    tool: Tool
    argv: list[Annotated[str, Field(max_length=4096)] | ArgField] = Field(default_factory=list, max_length=128)
    stdin_limit: int = Field(default=65536, ge=1, le=1024 * 1024)
    stdout_limit: int = Field(default=65536, ge=1, le=1024 * 1024)
    argument_limit: int = Field(default=16384, ge=1, le=65536)
    deadline_seconds: int = Field(default=30, ge=1, le=300)
    exit_errors: dict[str, str] = Field(default_factory=dict, max_length=32)
    topology: Literal["macos-no-fork-v1"]
    effects: Literal["package-read-only-no-network-v1"]

    @model_validator(mode="after")
    def closed_contract(self):
        relative_path(self.script)
        relative_path(self.source_license)
        if not self.script.endswith(".py") or self.tool.output_schema is None:
            raise PreparationError("cli_json_contract_required")
        for schema in (self.tool.input_schema, self.tool.output_schema):
            check_schema(schema)
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise PreparationError("cli_closed_object_schema_required")
        properties = self.tool.input_schema.get("properties", {})
        required = self.tool.input_schema.get("required", [])
        for argument in self.argv:
            if isinstance(argument, ArgField):
                if (argument.field not in required or properties.get(argument.field, {}).get("type") != "string"
                        or type(properties[argument.field].get("maxLength")) is not int
                        or not 0 <= properties[argument.field]["maxLength"] <= 4096):
                    raise PreparationError("cli_argument_field_unbounded")
            elif "\0" in argument:
                raise PreparationError("invalid_cli_argument")
        for code, failure in self.exit_errors.items():
            if (not re.fullmatch(r"[1-9][0-9]{0,2}", code) or int(code) > 255
                    or not re.fullmatch(r"cli_exit_[a-z][a-z0-9_]{0,39}", failure)):
                raise PreparationError("invalid_cli_exit_mapping")
        return self

    @property
    def digest(self):
        return contract_digest("cli-json-contract-v1", self.model_dump(exclude_none=True))


def parse_cli_spec(value):
    from pydantic import ValidationError
    try:
        return CliJsonSpec.model_validate(load_contract(value) if type(value) is bytes else value)
    except ValidationError:
        raise PreparationError("invalid_cli_json_contract") from None


def component_catalog():
    root = Path(__file__).resolve().parents[2]
    files = ["backend/mcpme/preparation_cli.py", "backend/mcpme/cli_execution.py", "scripts/mcpme-cli-launch.c",
             "backend/mcpme/preparation_cli_forms.py", "templates/cli-json-v1/adapter.py", "templates/cli-json-v1/prepare.py"]
    digest = contract_digest("cli-json-component-v1", [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in files])
    return {COMPONENT_ID + "@" + VERSION: {"digest": digest,
        "input_schema": {"type": "object", "additionalProperties": False}, "dependencies": []}}


def inspect_cli_directory(draft):
    """Generate a default recipe candidate without executing selected source."""
    from .preparation_components import read_snapshot
    snapshot = read_snapshot(draft)
    files = snapshot.by_path()
    declaration = files.get("mcpme.cli.json")
    if declaration is None:
        scripts = sorted(path for path in files if path.rsplit("/", 1)[-1] in {"cli.py", "__main__.py"})
        return [{"kind": "cli-json", "script": script, "default_recipe": "mcpme.cli-json",
            "candidate_id": contract_digest("cli-json-candidate-v1", {"source": snapshot.revision, "script": script}),
            "tools": [], "unresolved": ["cli_json_contract_required", "cli_topology_acceptance_required"],
            "questions": ["tool_input_schema", "tool_output_schema", "synthetic_arguments", "synthetic_expected",
                          "source_license", "fixed_argv"], "execution_available": False} for script in scripts]
    spec = parse_cli_spec(declaration.data)
    if spec.script not in files or spec.source_license not in files or not files[spec.source_license].size:
        raise PreparationError("cli_source_binding_required")
    return [{"kind": "cli-json", "script": spec.script, "spec": spec.model_dump(exclude_none=True),
        "default_recipe": "mcpme.cli-json", "candidate_id": contract_digest("cli-json-candidate-v1",
            {"source": snapshot.revision, "contract": spec.digest}),
        "tools": [spec.tool.model_dump(exclude_none=True)], "unresolved": ["cli_topology_acceptance_required"],
        "questions": ["synthetic_arguments", "synthetic_expected"], "execution_available": False}]


class CliJsonComponent:
    input_schema = {"type": "object", "additionalProperties": False,
        "properties": {"contract_path": {"type": "string", "minLength": 1, "maxLength": 4096},
                       "spec": {"type": "object"}}}
    legacy_interface_kinds = frozenset()

    def __init__(self):
        from .preparation_components import ComponentReference
        self.reference = ComponentReference(id=COMPONENT_ID, version=VERSION,
            digest=component_catalog()[COMPONENT_ID + "@" + VERSION]["digest"])

    def validate_source(self, descriptor, draft, recipe):
        from .preparation_components import PreparationDescriptorV2, SourceBinding, SourceFileBinding, read_snapshot
        if not isinstance(descriptor, PreparationDescriptorV2) or len(descriptor.components) != 1:
            raise PreparationError("unsupported_component_descriptor")
        selected = descriptor.components[0]
        if selected.component.key != self.reference.key or set(selected.input) not in ({"contract_path"}, {"spec"}):
            raise PreparationError("component_binding_mismatch")
        files = read_snapshot(draft).by_path()
        if "spec" in selected.input:
            spec = parse_cli_spec(selected.input["spec"])
        else:
            path = selected.input["contract_path"]
            relative_path(path)
            if path not in files:
                raise PreparationError("cli_json_contract_required")
            spec = parse_cli_spec(files[path].data)
        if (descriptor.tools != [spec.tool] or descriptor.artifacts or descriptor.access.filesystem
                or descriptor.access.network or descriptor.access.secrets or descriptor.access.writes):
            raise PreparationError("cli_effects_contract_mismatch")
        if spec.script not in files or spec.source_license not in files or not files[spec.source_license].size:
            raise PreparationError("cli_source_binding_required")
        return SourceBinding(*self.reference.key, draft["source"]["revision"],
            tuple(SourceFileBinding(item.path, item.sha256, item.size) for item in sorted(files.values(), key=lambda item: item.path)),
            spec.digest, None, canonical_bytes({"family": "cli", "topology": spec.topology,
                "effects": spec.effects, "release_gate": "A3-open"}))
