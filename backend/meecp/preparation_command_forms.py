"""Revision-bound questions for the trusted native JSON command component."""
from __future__ import annotations

from .preparation_command import VERSION, CommandInput, structural_schema, source_notices
from .preparation_components import parse_component_descriptor
from .preparation_contracts import PreparationError
from .preparation_forms import _choice, _field


class CommandFormProvider:
    def __init__(self, family):
        self.family = family

    def accepts(self, candidate, recipe):
        return candidate["kind"] == "native-json-command" and {
            (step["component"], step["version"], step["digest"]) for step in recipe["manifest"]["steps"]
        } == {self.family.component.reference.key}

    def fields(self, draft, candidate, recipe):
        snapshot = self.family.snapshot_reader(draft)
        notices = source_notices(snapshot)
        licenses = [_choice("command-license", path) for path in notices]
        effects = _choice("command-effects", "none", "No file writes, network access, or other external effects")
        protocol = _choice("command-protocol", "json-stdin-stdout-v1", "One JSON object on stdin; one JSON object on stdout")
        fields, bindings = [], {}
        def add(key, **kwargs):
            field = _field("command-" + key, **kwargs)
            fields.append(field)
            bindings[field.id] = (key,)
        add("name", label="App name", kind="string", value=draft.get("name", "Native command"),
            constraints={"min_length": 1, "max_length": 256})
        add("tool", label="Tool name", kind="string", value="run", constraints={"min_length": 1, "max_length": 64})
        add("description", label="Tool description", kind="string", value="Run the selected JSON command",
            constraints={"min_length": 1, "max_length": 1024})
        add("protocol", label="Confirm the documented command interface", kind="choice", choices=[protocol])
        add("effects", label="Confirm the documented command effects", kind="choice", choices=[effects])
        if licenses:
            add("license", label="Source license or notice", kind="choice", choices=licenses)
            bindings[fields[-1].id] = ("license", {choice.id: path for choice, path in zip(licenses, notices)})
        structural_schema(candidate["input_schema"])
        structural_schema(candidate["output_schema"])
        add("arguments", label="Synthetic test input", kind="object", schema=candidate["input_schema"])
        add("expected", label="Expected synthetic result", kind="object", schema=candidate["output_schema"])
        add("argv", label="Fixed command arguments", kind="object", value={"args": []}, schema={
            "type": "object", "additionalProperties": False, "required": ["args"], "properties": {
                "args": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 1024}}}})
        add("deadline", label="Command deadline (milliseconds)", kind="integer", value=1000,
            constraints={"minimum": 1, "maximum": 30000})
        ceiling = min(4096, draft.get("requirements", {}).get("memory_mb", 4096))
        add("memory", label="Reserved memory (MiB)", kind="integer", value=min(256, max(128, ceiling)),
            constraints={"minimum": 128, "maximum": max(128, ceiling)})
        requirements = draft.get("requirements", {})
        blockers = [] if licenses else ["source_license_required"]
        if ceiling < 128:
            blockers.append("resource_requirement_mismatch")
        if requirements.get("profile") not in (None, "host-command"):
            blockers.append("unsupported_execution_profile")
        if requirements.get("secret_references"):
            blockers.append("secret_reference_unavailable")
        return fields, bindings, blockers

    def resolve(self, context, values):
        selected = {binding[0]: values[field_id] for field_id, binding in context.bindings.items() if field_id in values}
        candidate, draft, recipe = context.candidate, context.draft, context.recipe_record
        license_binding = next(binding for binding in context.bindings.values() if binding[0] == "license")
        license_path = license_binding[1][selected["license"]]
        if (selected["protocol"] != _choice("command-protocol", "json-stdin-stdout-v1").id
                or selected["effects"] != _choice("command-effects", "none").id):
            raise PreparationError("command_authority_mismatch")
        command_input = CommandInput(candidate_id=candidate["candidate_id"], executable=candidate["module"],
            input_schema_path=candidate["input_schema_path"], output_schema_path=candidate["output_schema_path"],
            license_paths=[license_path, *(path for path in source_notices(self.family.snapshot_reader(draft)) if path != license_path)],
            fixed_args=selected["argv"]["args"], protocol="json-stdin-stdout-v1", effects="none",
            helper_sha256=self.family.helper_sha256, deadline_ms=selected["deadline"], input_bytes=16384, output_bytes=16384)
        value = parse_component_descriptor({"schema_version": 2,
            "recipe": {"id": recipe["manifest"]["id"], "version": recipe["manifest"]["version"], "digest": recipe["digest"]},
            "package": {"id": "local.app-" + draft["draft_id"], "version": VERSION, "name": selected["name"]},
            "components": [{"component": self.family.component.reference.model_dump(), "input": command_input.model_dump()}],
            "tools": [{"name": selected["tool"], "description": selected["description"],
                "input_schema": candidate["input_schema"], "output_schema": candidate["output_schema"]}],
            "resources": {"memory_mb": selected["memory"]},
            "lifecycle": {"max_request_bytes": 65536, "max_sessions": 1, "operation_timeout_seconds": 35,
                "startup_timeout_seconds": 35, "stop_grace_seconds": 3},
            "validation": [{"id": "command-test", "tool": selected["tool"], "arguments": selected["arguments"],
                "expected": selected["expected"], "synthetic": True}]})
        requirements = draft.get("requirements", {})
        if requirements.get("tools") and requirements["tools"] != [selected["tool"]]:
            raise PreparationError("tool_requirement_mismatch")
        self.family.validate_inputs(value, draft)
        return value.model_dump(exclude_none=True)

    def restore_fields(self, fields, bindings, descriptor):
        value = parse_component_descriptor(descriptor)
        selected = CommandInput.model_validate(value.components[0].input)
        fixture = value.validation[0]
        values = {"name": value.package.name, "tool": value.tools[0].name, "description": value.tools[0].description,
            "protocol": _choice("command-protocol", selected.protocol).id, "effects": _choice("command-effects", selected.effects).id,
            "license": _choice("command-license", selected.license_paths[0]).id,
            "arguments": fixture.arguments, "expected": fixture.expected, "argv": {"args": selected.fixed_args},
            "deadline": selected.deadline_ms, "memory": value.resources.memory_mb}
        return [field.model_copy(update={"value": values[bindings[field.id][0]]}) for field in fields]
