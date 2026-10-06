"""Generated app transport limits cover inline data independently of tiny fixtures."""
import base64
import json
from types import SimpleNamespace

import pytest

from mcpme.contracts import Tool
from mcpme.limits import DEFAULT_REQUEST_BYTES, MAX_REQUEST_BYTES
from mcpme.preparation_agent_adapter import descriptor_for as locked_descriptor
from mcpme.preparation_agent_unlocked import descriptor_for as unlocked_descriptor
from mcpme.preparation_components import ComponentReference
from mcpme.preparation_contracts import RecipeReference, canonical_bytes
from mcpme.preparation_inputs import DescriptorView


@pytest.mark.parametrize("factory, component", [
    (locked_descriptor, "agent-python-adapter-v1"),
    (unlocked_descriptor, "agent-unlocked-python-adapter-v1"),
])
@pytest.mark.parametrize("field_name, field_type, tiny_argument", [
    ("image_base64", "string", "AA=="),
    ("document", "string", "small fixture"),
    ("value", "string", "small fixture"),
    ("count", "integer", 1),
    ("confidence", "number", 0.5),
    ("enabled", "boolean", True),
])
def test_generated_descriptor_and_package_contract_reserve_bounded_inline_capacity(
        factory, component, field_name, field_type, tiny_argument):
    tool = Tool.model_validate({"name": "invoke", "description": "Process the supplied value.",
        "input_schema": {"type": "object", "properties": {field_name: {"type": field_type}},
            "required": [field_name], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False}})
    result = SimpleNamespace(digest="a" * 64,
        request=SimpleNamespace(recipe=RecipeReference(id="mcpme.test-adapter", version="1.0.2", digest="b" * 64),
            producer=ComponentReference(id=component, version="1.0.2", digest="c" * 64)),
        specification=SimpleNamespace(tool=tool, arguments={field_name: tiny_argument}, expected={"ok": True}))
    descriptor = factory(result, "11111111-1111-4111-8111-111111111111",
        {"draft_id": "22222222-2222-4222-8222-222222222222", "name": "Inline data app"})
    view = DescriptorView(descriptor, (), canonical_bytes(descriptor.model_dump(exclude_none=True)))
    installed_contract = view.expected_package_contract("sha256:" + "d" * 64, profile="mcp-stdio")
    limit = installed_contract["lifecycle"]["max_request_bytes"]
    assert limit == descriptor.lifecycle.max_request_bytes
    assert 1 <= limit <= MAX_REQUEST_BYTES
    if field_type == "string":
        # A 12 MiB image expands to 16 MiB of base64 plus the JSON-RPC envelope.
        # The tiny preparation fixture must not determine its installed limit.
        wire = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool.name, "arguments": {field_name:
                base64.b64encode(b"\0" * (12 * 1024 * 1024)).decode()}}}).encode()
        assert DEFAULT_REQUEST_BYTES < len(wire) < limit == MAX_REQUEST_BYTES
    else:
        assert limit == DEFAULT_REQUEST_BYTES


def test_published_adapter_component_identities_remain_available():
    from mcpme.preparation_agent_adapter import component_catalog as locked_catalog
    from mcpme.preparation_agent_unlocked import component_catalog as unlocked_catalog
    assert locked_catalog()["agent-python-adapter-v1@1.0.1"]["digest"] == (
        "d7eb6da84e83c3c3204fb92e17c467766c1c4d17b930417634f2c8d71338712d")
    assert unlocked_catalog()["agent-unlocked-python-adapter-v1@1.0.1"]["digest"] == (
        "7849a67cc096d0579a92bc910aef855d68945a63d37b54362aa9a5f2bf307726")
