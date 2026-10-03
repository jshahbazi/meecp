"""Generated CLI questions; registration waits for the original A3 gate."""
from __future__ import annotations

from pathlib import PurePosixPath

from .preparation_cli import CliJsonComponent, parse_cli_spec
from .preparation_components import read_snapshot
from .preparation_contracts import PreparationError
from .preparation_forms import _choice, _field


class CliJsonFormProvider:
    def accepts(self, candidate, recipe):
        steps = recipe["manifest"]["steps"]
        return candidate["kind"] == "cli-json" and len(steps) == 1 and steps[0]["component"] == "cli-json-v1"

    def fields(self, draft, candidate, recipe):
        fields, bindings = [], {}
        def add(key, **kwargs):
            field = _field("cli-" + key, **kwargs)
            fields.append(field)
            bindings[field.id] = (key,)
        add("name", label="App name", kind="string", value=draft["name"], constraints={"min_length": 1, "max_length": 256})
        add("memory", label="Memory budget (MiB)", kind="integer", value=128, constraints={"minimum": 128, "maximum": 4096})
        spec = candidate.get("spec")
        if spec is None:
            licenses = [item.path for item in read_snapshot(draft).files
                if PurePosixPath(item.path).name.lower().split(".")[0] in {"license", "licence", "copying"} and item.size]
            if not licenses:
                return fields, bindings, ["source_license_required", "cli_topology_acceptance_required"]
            choices = [_choice("cli-license", path, path) for path in licenses]
            license_field = _field("cli-license", label="Source license", kind="choice", choices=choices,
                value=choices[0].id if len(choices) == 1 else None)
            fields.append(license_field)
            bindings[license_field.id] = ("license", {choice.id: path for choice, path in zip(choices, licenses)})
            add("tool_name", label="Tool name", kind="string", value="run", constraints={"min_length": 1, "max_length": 128})
            add("description", label="Tool description", kind="string", value=draft.get("intent") or "Run the selected JSON command",
                constraints={"min_length": 1, "max_length": 4096})
            schema = {"type": "object"}
            add("input_schema", label="Tool input schema", kind="object", schema=schema,
                value={"type": "object", "additionalProperties": False, "properties": {"value": {"type": "string", "maxLength": 4096}}, "required": ["value"]})
            add("output_schema", label="Tool output schema", kind="object", schema=schema,
                value={"type": "object", "additionalProperties": False, "properties": {"value": {"type": "string"}}, "required": ["value"]})
            add("argv", label="Fixed command arguments", kind="object", schema={"type": "object", "additionalProperties": False,
                "properties": {"argv": {"type": "array", "maxItems": 128}}, "required": ["argv"]}, value={"argv": []},
                description="Each argument is a literal string or an explicitly selected input field. Shell expansion is unavailable.")
        add("arguments", label="Synthetic test input", kind="object", schema=spec["tool"]["input_schema"] if spec else {"type": "object"})
        add("expected", label="Expected test result", kind="object", schema=spec["tool"]["output_schema"] if spec else {"type": "object"})
        return fields, bindings, ["cli_topology_acceptance_required"]

    def resolve(self, context, values):
        answers = {}
        for identity, binding in context.bindings.items():
            value = values.get(identity)
            if binding[0] == "license" and value is not None:
                value = binding[1].get(value)
            answers[binding[0]] = value
        candidate = context.candidate
        raw = candidate.get("spec") or {"schema_version": 1, "runtime": "private-python-stdlib-v1",
            "script": candidate["script"], "source_license": answers.get("license"),
            "tool": {"name": answers.get("tool_name"), "description": answers.get("description"),
                     "input_schema": answers.get("input_schema"), "output_schema": answers.get("output_schema")},
            "argv": (answers.get("argv") or {}).get("argv", []),
            "topology": "macos-no-fork-v1", "effects": "package-read-only-no-network-v1"}
        contract = parse_cli_spec(raw)
        if answers.get("name") is None or answers.get("memory") is None:
            raise PreparationError("invalid_preparation_form_answer")
        return {"schema_version": 2, "recipe": context.form.recipe.model_dump(),
            "package": {"id": "local.app-" + context.form.import_id, "version": "1.0.0", "name": answers["name"]},
            "components": [{"component": CliJsonComponent().reference.model_dump(), "input": {"spec": contract.model_dump(exclude_none=True)}}],
            "tools": [contract.tool.model_dump(exclude_none=True)], "inputs": {}, "access": {},
            "resources": {"memory_mb": answers["memory"]},
            "validation": [{"id": "synthetic", "tool": contract.tool.name, "arguments": answers.get("arguments"),
                            "expected": answers.get("expected"), "synthetic": True}]}
