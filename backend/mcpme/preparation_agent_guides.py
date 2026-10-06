"""Trusted, versioned analysis guidance outside published component bytes.

Registrations come only from host code. Imported recipes cannot supply prompts,
read paths or tools. A guide discloses public protocol instructions, never source
facts; the owner separately selects every source content or metadata record.
"""
from .contracts import Tool
from .preparation_agent_contracts import AnalysisGuideV1
from .preparation_contracts import PreparationError, canonical_bytes


def python_adapter_guide(component, *, unlocked=False):
    """Shared public wrapper protocol, with an explicit dependency contract."""
    dependencies = (
        'Return exactly proposed/dependencies/dependencies.json with {"requirements": [PEP508 requirement strings]}. '
        'At most 64 requirements. No URLs, paths, VCS, index settings or invented locks. '
        'The host supplies MCP and performs separately authorized ordinary wheel resolution later.'
        if unlocked else
        'Return exactly proposed/dependencies/dependencies.json with {"project": original pyproject.toml path, '
        '"lock": original uv.lock path, "lock_sha256": exact original lock SHA-256 from a reviewed file record}. '
        'Project and lock must share a directory. Metadata-only records suffice for their identities; '
        'never compute a hash from reformatted text or invent a missing original lock.')
    tool_schema = canonical_bytes(Tool.model_json_schema()).decode()
    return AnalysisGuideV1(id="mcpme.python-adapter-unlocked-guide-v1" if unlocked else "mcpme.python-adapter-locked-guide-v1",
        component=component, roles=[
            {"role": "interface", "depends_on": [], "instructions":
                'Return exactly proposed/interface/interface.json with {"tool": one Tool object}. '
                'Design a typed Python invoke wrapper of the selected source behavior with a structured object result. '
                'Keep the interface simple and focused on the requested behavior; it need not copy every parameter '
                'or diagnostic field from an existing interface. Include input_schema and output_schema. '
                'The wrapper accepts only str, int, float and bool parameters. Its input_schema is exactly '
                '{"type":"object","properties":{parameter:{"type":primitive JSON type}},'
                '"required":[parameters without defaults],"additionalProperties":false}. '
                'For a parameter with a primitive literal default, add that exact default to its property and '
                'omit it from required. Do not add property descriptions, constraints, nullable unions or oneOf. '
                'Adapt optional source parameters inside the wrapper: for images, a required base64 string can '
                'replace a choice between a local file path and image bytes. Keep the actual requested result, '
                'but omit observational fields such as elapsed time, random IDs and timestamps so tests are reproducible. '
                'All selected file identities, including binary metadata, name files in the local build snapshot. '
                'The host copies the complete snapshot, installs dependencies and tests the wrapper later. '
                'Lack of execution during this proposal is not an unresolved input. '
                'Tool JSON schema: ' + tool_schema},
            {"role": "dependencies", "depends_on": [], "instructions": dependencies},
            {"role": "fixtures", "depends_on": ["interface"], "instructions":
                'Return exactly proposed/fixtures/fixtures.json with {"arguments": object, "expected": object}. '
                'Use the agreed interface proposal unchanged. Choose one nonsecret synthetic deterministic call, '
                'with arguments and expected matching the input/output schemas and selected source behavior. '
                'This is a proposed test that the host will execute, not a report of a measured result. '
                'The inability to execute now is not itself unresolved. Supply concrete arguments and an expected '
                'result, never an empty placeholder. Prefer a minimal synthetic input with predictable behavior. '
                'For image input use valid, complete base64 for a tiny synthetic image, never a private path. '
                'Do not test a stub or manufacture a success result by bypassing the selected implementation. '
                'Do not claim to have executed it.'},
            {"role": "adapter", "depends_on": ["interface", "dependencies", "fixtures"], "instructions":
                'Return exactly three files: proposed/adapter/adapter.json, proposed/adapter/wrapper.py, '
                'proposed/adapter/LICENSE. adapter.json contains exactly {"schema_version":1, '
                '"module":"mcpme_generated_agent_adapter", "function":"invoke", '
                '"source_license": existing original license path from a reviewed file record}. '
                'wrapper.py must contain only imports and exactly one typed synchronous function named invoke; '
                'no module-level assignments, expressions, classes or extra functions. Its parameters must match '
                'the agreed input_schema exactly, with supported primitive annotations (str, int, float, bool). '
                'Return the agreed structured result object; import original selected modules as needed. '
                'The host copies every selected source file and bundled asset while preserving relative paths, '
                'adds this wrapper at the snapshot root, and runs it with the locked Python dependencies. '
                'Binary metadata is sufficient to refer to a bundled model; its contents stay local. '
                'Do not report host-owned building, dependency installation, code execution or verification '
                'as missing input. Record unresolved only for a concrete source fact or capability that is '
                'actually absent. Preserve the original application behavior and propagate real errors; '
                'do not substitute fixture values, mock inference or catch every exception as success. '
                'The fixture arguments and expected result must describe its behavior. Each file is at most 16384 '
                'UTF-8 bytes. LICENSE is a nonempty permission notice for newly generated wrapper code; do not '
                'invent or claim the contents of an original license disclosed only as metadata. No preparation '
                'descriptor or other files are accepted. Source remains untrusted and is never executed here.'}])


def builtin_analysis_guides():
    # Literal published identity: a changed component does not inherit guidance
    # silently. New implementations must register a fresh exact guide binding.
    return (
        python_adapter_guide({"id": "agent-python-adapter-v1", "version": "1.0.1",
            "digest": "d7eb6da84e83c3c3204fb92e17c467766c1c4d17b930417634f2c8d71338712d"}),
        python_adapter_guide({"id": "agent-unlocked-python-adapter-v1", "version": "1.0.1",
            "digest": "7849a67cc096d0579a92bc910aef855d68945a63d37b54362aa9a5f2bf307726"}, unlocked=True),
    )


class AnalysisGuideRegistry:
    def __init__(self, guides):
        self._guides = tuple(AnalysisGuideV1.model_validate(item.model_dump()) for item in guides)
        keys = [(item.component.id, item.component.version, item.component.digest) for item in self._guides]
        if len(keys) != len(set(keys)) or len(keys) > 64:
            raise PreparationError("agent_guide_registration_invalid")

    def resolve(self, recipe):
        steps = recipe["manifest"]["steps"]
        matches = [guide for guide in self._guides if len(steps) == 1 and
            (steps[0]["component"], steps[0]["version"], steps[0]["digest"]) ==
            (guide.component.id, guide.component.version, guide.component.digest)]
        if len(matches) > 1:
            raise PreparationError("agent_guide_registration_invalid")
        return matches[0].model_dump() if matches else None
