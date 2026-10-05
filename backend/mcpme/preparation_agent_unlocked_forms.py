"""Generated questions after explicit staged-adapter dependency resolution."""
from .preparation_agent_unlocked import descriptor_for
from .preparation_forms import _field


class AgentUnlockedPythonFormProvider:
    def __init__(self, family):
        self.family = family

    def accepts(self, candidate, recipe):
        steps = recipe["manifest"]["steps"]
        return candidate["kind"] == "agent-unlocked-python-adapter" and len(steps) == 1 and tuple(
            steps[0][key] for key in ("component", "version", "digest")) == self.family.component.reference.key

    def result(self, draft, candidate):
        item = self.family.inputs.for_job(candidate["_agent_job_id"], "completed")
        if item is None or item["id"] != candidate["_agent_completed_id"]:
            raise ValueError("agent_dependency_resolution_required")
        result, _ = self.family.inputs.resolve(item["id"], item["result_digest"], draft)
        self.family.component.result(descriptor_for(result, item["id"], draft), draft)
        return result, item

    def fields(self, draft, candidate, recipe):
        if candidate.get("_resolved") is not True or candidate["_agent_completed_id"] is None:
            return [], {}, ["dependency_resolution_required"]
        result, _ = self.result(draft, candidate)
        ceiling = min(4096, draft["requirements"].get("memory_mb", 4096))
        fields = [_field("agent-unlocked-name", label="App name", kind="string", value=draft["name"], constraints={"min_length": 1, "max_length": 256}),
            _field("agent-unlocked-memory", label="Memory budget (MiB)", kind="integer", value=min(128,max(64,ceiling)), constraints={"minimum": 64, "maximum": max(64,ceiling)}),
            _field("agent-unlocked-arguments", label="Test input", kind="object", value=result.specification.arguments, schema=result.specification.tool.input_schema),
            _field("agent-unlocked-expected", label="Expected test result", kind="object", value=result.specification.expected, schema=result.specification.tool.output_schema)]
        return fields, {item.id: (index,) for index,item in enumerate(fields)}, ([] if ceiling >= 64 else ["unsupported_resource_requirement"])

    def restore_fields(self, fields, bindings, descriptor):
        values = [descriptor["package"]["name"], descriptor["resources"]["memory_mb"], descriptor["validation"][0]["arguments"], descriptor["validation"][0]["expected"]]
        return [field.model_copy(update={"value": values[bindings[field.id][0]]}) for field in fields]

    def resolve(self, context, values):
        result, item = self.result(context.draft, context.candidate)
        if context.form.resolution_digest != result.request.resolution_result_digest:
            raise ValueError("agent_unlocked_resolution_mismatch")
        descriptor = descriptor_for(result,item["id"],context.draft).model_dump(mode="json",exclude_none=True)
        supplied = {binding[0]: values[key] for key,binding in context.bindings.items()}
        descriptor["package"]["name"], descriptor["resources"]["memory_mb"] = supplied[0],supplied[1]
        descriptor["validation"][0].update(arguments=supplied[2],expected=supplied[3])
        return descriptor
