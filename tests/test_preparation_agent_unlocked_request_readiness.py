"""Request-only simulation for the controlled scalar/humanize fixture."""
import ast
import hashlib
import json
from pathlib import PurePosixPath

import pytest

from meecp.preparation_agent_guides import python_adapter_guide
from test_preparation_agent import backend, output, request_for, response


def request_only_unlocked_handler(request):
    """No captured fixture data, pinned dependency, ambient files or execution."""
    payload = json.loads(request.content)
    material = json.loads(payload["input"][0]["content"][0]["text"])
    role = material["role"]
    guidance = payload["instructions"]
    contract = json.loads(guidance[guidance.index('{"component"'):])["role_contract"]
    assert contract["role"] == role
    assert [item["role"] for item in material["prior_proposals"]] == contract["depends_on"]
    assert len(material["selected_source"]) == 1
    records = {row["path"]: row for row in material["selected_files"] + material["selected_metadata"]}
    path, source = next(iter(material["selected_source"].items()))
    assert hashlib.sha256(source.encode()).hexdigest() == records[path]["sha256"]
    assert len(source.encode()) == records[path]["bytes"]
    tree = ast.parse(source)
    requirements = sorted({alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names})
    # This narrow simulation recognizes the distribution corresponding to the
    # fixture's reviewed import. Only the ordinary resolver chooses a version.
    assert set(requirements) <= {"humanize"}
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    argument = function.args.args[0].arg
    returned = function.body[0].value
    multiplication = next(node for node in ast.walk(returned) if isinstance(node, ast.BinOp))
    assert isinstance(multiplication.op, ast.Mult) and isinstance(multiplication.left, ast.Name)
    assert multiplication.left.id == argument and type(multiplication.right.value) is int
    factor = multiplication.right.value
    expression = f"{argument} * {factor}"
    if requirements:
        expression = f"int({requirements[0]}.intcomma({expression}).replace(',', ''))"
    assert ast.dump(returned) == ast.dump(ast.parse(expression, mode="eval").body)
    tool = {"name": function.name, "description": "Synthetic scalar wrapper",
        "input_schema": {"type": "object", "properties": {argument: {"type": "integer"}}, "required": [argument], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}}
    unresolved = []
    if role == "interface":
        files = [{"path": "proposed/interface/interface.json", "content": json.dumps({"tool": tool})}]
    elif role == "dependencies":
        files = [{"path": "proposed/dependencies/dependencies.json", "content": json.dumps({"requirements": requirements})}]
    elif role == "fixtures":
        assert json.loads(material["prior_proposals"][0]["files"][0]["content"])["tool"] == tool
        files = [{"path": "proposed/fixtures/fixtures.json", "content": json.dumps({
            "arguments": {argument: 3}, "expected": {"value": 3 * factor}})}]
    else:
        licenses = [name for name in records if PurePosixPath(name).name == "LICENSE"]
        if len(licenses) != 1:
            unresolved = ["An exact original license identity must be selected for review."]
        files = [
            {"path": "proposed/adapter/adapter.json", "content": json.dumps({"schema_version": 1,
                "module": "meecp_generated_agent_adapter", "function": "invoke", "source_license": licenses[0] if len(licenses) == 1 else "LICENSE"})},
            {"path": "proposed/adapter/wrapper.py", "content": f"from {PurePosixPath(path).stem} import {function.name}\ndef invoke({argument}: int) -> dict[str, int]:\n return {{'value': {function.name}({argument})}}\n"},
            {"path": "proposed/adapter/LICENSE", "content": "Permission to use, copy and modify this synthetic generated wrapper is granted.\n"}]
    assert all(item["path"] in contract["instructions"] for item in files)
    return response(role, output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text",
        "text": json.dumps(output(role, files=files, unresolved=unresolved))}]}])


@pytest.mark.parametrize("source,expected", [
    (b"def double(n):\n return n*2\n", []),
    (b"import humanize\ndef triple(n):\n return int(humanize.intcomma(n*3).replace(',', ''))\n", ["humanize"]),
], ids=["scalar-no-import", "reviewed-humanize-import"])
@pytest.mark.parametrize("license_reviewed", [False, True])
async def test_unlocked_transport_derives_requirements_and_fixture_from_request(source, expected, license_reviewed):
    reference = {"id": "agent-unlocked-python-adapter-v1", "version": "1.0.0",
        "digest": "30a6e459f76f4feabff58ff5481b5cc95f9589e03436a8aac109583a4cbfa26c"}
    guide = python_adapter_guide(reference, unlocked=True)
    license_bytes = b"Synthetic original permission\n"
    request, captured = request_for(source={"worker.py": source}, recipe={
        "id": "mcpme.agent-unlocked-python-adapter", "version": "1.0.1", "digest": "a" * 64},
        guide=guide.model_dump(), selected_metadata=[{"path": "LICENSE", "bytes": len(license_bytes),
            "sha256": hashlib.sha256(license_bytes).hexdigest()}] if license_reviewed else [])
    transport = backend(request_only_unlocked_handler)
    proposals = {}
    for contract in guide.roles:
        body = transport.encode(request, contract.role, request.selected_source(captured),
            [proposals[role] for role in contract.depends_on])
        proposals[contract.role] = (await transport.complete(request, contract.role, body)).proposal
    dependencies = json.loads(proposals["dependencies"].files[0].content)
    assert dependencies == {"requirements": expected}
    assert bool(proposals["adapter"].unresolved) is not license_reviewed
    fixture = json.loads(proposals["fixtures"].files[0].content)
    assert fixture == {"arguments": {"n": 3}, "expected": {"value": 9 if expected else 6}}
