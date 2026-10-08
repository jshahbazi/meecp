"""Generated owner questions for exact frozen adapter inputs."""
from .preparation_agent_adapter import COMPONENT_ID, VERSION, descriptor_for
from .preparation_forms import _field


class AgentPythonAdapterFormProvider:
    def __init__(self, family):
        self.family = family

    def accepts(self, candidate, recipe):
        steps = recipe["manifest"]["steps"]
        return (candidate["kind"] == "agent-python-adapter" and len(steps) == 1
            and (steps[0]["component"], steps[0]["version"], steps[0]["digest"]) == self.family.component.reference.key)

    def fields(self, draft, candidate, recipe):
        result, _ = self.family.derivations.resolve(candidate["_agent_derivation_id"], candidate["_agent_derivation_digest"], draft)
        ceiling = min(4096, draft["requirements"].get("memory_mb", 4096))
        fields = [_field("agent-adapter-name", label="App name", kind="string", value=draft["name"],
                    constraints={"min_length": 1, "max_length": 256}),
            _field("agent-adapter-memory", label="Memory budget (MiB)", kind="integer", value=min(128, max(64, ceiling)),
                constraints={"minimum": 64, "maximum": max(64, ceiling)}),
            _field("agent-adapter-arguments", label="Test input", kind="object", value=result.specification.arguments,
                schema=result.specification.tool.input_schema),
            _field("agent-adapter-expected", label="Expected test result", kind="object", value=result.specification.expected,
                schema=result.specification.tool.output_schema)]
        return fields, {item.id: (index,) for index, item in enumerate(fields)}, ([] if ceiling >= 64 else ["unsupported_resource_requirement"])

    def restore_fields(self, fields, bindings, descriptor):
        values = [descriptor["package"]["name"], descriptor["resources"]["memory_mb"],
            descriptor["validation"][0]["arguments"], descriptor["validation"][0]["expected"]]
        return [field.model_copy(update={"value": values[bindings[field.id][0]]}) for field in fields]

    def resolve(self, context, values):
        candidate = context.candidate
        result, _ = self.family.derivations.resolve(candidate["_agent_derivation_id"], candidate["_agent_derivation_digest"], context.draft)
        descriptor = descriptor_for(result, candidate["_agent_derivation_id"], context.draft).model_dump(mode="json", exclude_none=True)
        supplied = {binding[0]: values[key] for key, binding in context.bindings.items()}
        descriptor["package"]["name"], descriptor["resources"]["memory_mb"] = supplied[0], supplied[1]
        descriptor["validation"][0].update(arguments=supplied[2], expected=supplied[3])
        return descriptor
