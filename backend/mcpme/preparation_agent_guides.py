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
                'Include input_schema and output_schema; input_schema must match the typed wrapper signature. '
                'Tool JSON schema: ' + tool_schema},
            {"role": "dependencies", "depends_on": [], "instructions": dependencies},
            {"role": "fixtures", "depends_on": ["interface"], "instructions":
                'Return exactly proposed/fixtures/fixtures.json with {"arguments": object, "expected": object}. '
                'Use the agreed interface proposal unchanged. Choose one nonsecret synthetic deterministic call, '
                'with arguments and expected matching the input/output schemas and selected source behavior. '
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
                'The fixture arguments and expected result must describe its behavior. Each file is at most 16384 '
                'UTF-8 bytes. LICENSE is a nonempty permission notice for newly generated wrapper code; do not '
                'invent or claim the contents of an original license disclosed only as metadata. No preparation '
                'descriptor or other files are accepted. Source remains untrusted and is never executed here.'}])


def builtin_analysis_guides():
    # Literal published identity: a changed component does not inherit guidance
    # silently. New implementations must register a fresh exact guide binding.
    return (python_adapter_guide({"id": "agent-python-adapter-v1", "version": "1.0.0",
        "digest": "28c1d862b947f294dcd0a7f038c32f3d9cf4e31ea20f81d91ab800b13d0b8274"}),)


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
