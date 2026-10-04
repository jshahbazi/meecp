"""Static untrusted proposals for a separately authorized dependency job.

Parsing these contracts cannot resolve dependencies, import selected modules,
start a process, enable a recipe or install a package.
"""
from __future__ import annotations

import ast
import json
import tomllib
from typing import Literal

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from pydantic import Field, field_validator

from .contracts import Tool
from .preparation_agent_contracts import AgentProposalV1
from .preparation_agent_adapter_store import CODE_PATH, MODULE, _json_file
from .preparation_contracts import (PreparationError, canonical_bytes, check_schema,
    contract_digest, load_contract, relative_path, schema_accepts)
from .preparation_inputs import InputContract
from .preparation_inspection import _bound_tree, _callable
from .preparation_resolution_runner import MCP_VERSION, PYTHON_VERSION

DEPENDENCY_NAMESPACE = "mcpme_generated_agent_dependencies"
MAX_PROPOSED_REQUIREMENTS = 64


def checked_requirements(values, *, original=False):
    if not isinstance(values, (list, tuple)) or len(values) > (128 if original else MAX_PROPOSED_REQUIREMENTS):
        raise PreparationError("agent_dependency_requirements_invalid")
    seen, result = set(), []
    for raw in values:
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            raise PreparationError("agent_dependency_requirements_invalid")
        try:
            value = Requirement(raw)
        except ValueError:
            raise PreparationError("agent_dependency_requirements_invalid") from None
        name = canonicalize_name(value.name)
        if value.url or name in seen:
            raise PreparationError("agent_dependency_requirements_invalid")
        seen.add(name)
        if name == "mcp":
            if not original or value.extras or MCP_VERSION not in value.specifier:
                raise PreparationError("agent_dependency_sdk_host_owned")
            continue
        result.append(raw)
    return tuple(result)


class UnlockedAdapterSpecificationV1(InputContract):
    schema_version: Literal[1]
    module: Literal["mcpme_generated_agent_adapter"]
    function: Literal["invoke"]
    source_license: str = Field(min_length=1, max_length=256)
    requirements: tuple[str, ...] = Field(max_length=MAX_PROPOSED_REQUIREMENTS)
    tool: Tool
    arguments: dict
    expected: dict

    @field_validator("requirements")
    @classmethod
    def requirements_closed(cls, values):
        return checked_requirements(values)

    @property
    def digest(self):
        return contract_digest("agent-unlocked-adapter-specification-v1", self.model_dump(mode="json", exclude_none=True))


def specification_from_proposal(proposal, snapshot):
    """Consume each role/file without authorizing its source or dependencies."""
    value = AgentProposalV1.model_validate(proposal.model_dump(mode="json"))
    if any(item.unresolved for item in value.tasks):
        raise PreparationError("agent_candidate_unresolved")
    interface_task, dependency_task, fixture_task, adapter_task = value.tasks
    interface = _json_file(interface_task, "proposed/interface/interface.json")
    dependencies = _json_file(dependency_task, "proposed/dependencies/dependencies.json")
    fixtures = _json_file(fixture_task, "proposed/fixtures/fixtures.json")
    content = {item.path: item.content.encode() for item in adapter_task.files}
    if (set(interface) != {"tool"} or set(dependencies) != {"requirements"}
            or set(fixtures) != {"arguments", "expected"}
            or set(content) != {"proposed/adapter/adapter.json", "proposed/adapter/wrapper.py", "proposed/adapter/LICENSE"}):
        raise PreparationError("agent_adapter_proposal_invalid")
    metadata = load_contract(content["proposed/adapter/adapter.json"])
    if set(metadata) != {"schema_version", "module", "function", "source_license"}:
        raise PreparationError("agent_adapter_proposal_invalid")
    specification = UnlockedAdapterSpecificationV1.model_validate({**metadata, **dependencies,
        "tool": interface["tool"], "arguments": fixtures["arguments"], "expected": fixtures["expected"]})
    relative_path(specification.source_license)
    original = snapshot.by_path()
    if specification.source_license not in original or not original[specification.source_license].size:
        raise PreparationError("source_license_required")
    reserved = {MODULE, CODE_PATH, DEPENDENCY_NAMESPACE}
    if any(path.split("/")[0] in reserved for path in original):
        raise PreparationError("agent_adapter_namespace_collision")
    code, license_text = content["proposed/adapter/wrapper.py"], content["proposed/adapter/LICENSE"]
    if not code or not license_text or len(code) > 16384 or len(license_text) > 16384:
        raise PreparationError("agent_adapter_proposal_invalid")
    try:
        tree = ast.parse(code)
        _bound_tree(tree, nodes=2048, depth=64)
        if any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)) for node in tree.body):
            raise PreparationError("agent_adapter_static_eligibility_required")
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        if len(functions) != 1 or functions[0].name != "invoke":
            raise PreparationError("agent_adapter_static_eligibility_required")
        schema, _ = _callable(functions[0])
        if schema != specification.tool.input_schema or specification.tool.output_schema is None:
            raise PreparationError("agent_adapter_interface_mismatch")
    except (SyntaxError, UnicodeError, RecursionError):
        raise PreparationError("agent_adapter_static_eligibility_required") from None
    check_schema(specification.tool.input_schema); check_schema(specification.tool.output_schema)
    if (not schema_accepts(specification.tool.input_schema, specification.arguments)
            or not schema_accepts(specification.tool.output_schema, specification.expected)):
        raise PreparationError("agent_adapter_fixture_mismatch")
    # Bind the host's static dependency interpretation before staging. No
    # lock, network request or subprocess is created here.
    canonical_dependency_project(specification, snapshot)
    return specification, {"adapter.json": canonical_bytes(specification.model_dump(mode="json", exclude_none=True)),
        CODE_PATH: code, "LICENSE": license_text}


def canonical_dependency_project(specification, snapshot):
    """Preserve static original requirements and pin the host SDK privately."""
    existing = snapshot.by_path().get("pyproject.toml")
    try:
        metadata = tomllib.loads(existing.data.decode()) if existing else {}
        project = metadata.get("project", {})
        if (not isinstance(project, dict) or project.get("dynamic") or metadata.get("tool", {}).get("uv")
                or metadata.get("dependency-groups")):
            raise PreparationError("unsupported_resolution_project")
        requires_python = project.get("requires-python", ">=3.12")
        if not isinstance(requires_python, str) or PYTHON_VERSION not in SpecifierSet(requires_python):
            raise PreparationError("unsupported_resolution_python")
        original = checked_requirements(project.get("dependencies", []), original=True)
        # A provider cannot replace a captured requirement with a conflicting
        # spelling, version constraint or environment marker.
        combined = {canonicalize_name(Requirement(item).name): item for item in original}
        for item in checked_requirements(specification.requirements):
            name = canonicalize_name(Requirement(item).name)
            if name in combined and item != combined[name]:
                raise PreparationError("agent_dependency_original_requirement_changed")
            combined[name] = item
        requirements = [combined[name] for name in sorted(combined)] + ["mcp==" + MCP_VERSION]
        return ('[project]\nname = "mcpme-agent-derived-preparation"\nversion = "1.0.0"\n'
            'requires-python = "==' + PYTHON_VERSION + '"\ndependencies = ' + json.dumps(requirements) + '\n').encode()
    except PreparationError:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise PreparationError("unsupported_resolution_project") from None
