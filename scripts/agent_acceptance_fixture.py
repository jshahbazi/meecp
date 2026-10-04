"""Closed request-only provider simulation for developer acceptance; no live transport."""
import ast
import hashlib
import json
from pathlib import PurePosixPath

import httpx

def output(role, **changes):
    return {"role": role, "files": changes["files"], "unresolved": changes["unresolved"]}

def response(role, **changes):
    return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 40}, **changes})


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
                "module": "mcpme_generated_agent_adapter", "function": "invoke", "source_license": licenses[0] if len(licenses) == 1 else "LICENSE"})},
            {"path": "proposed/adapter/wrapper.py", "content": f"from {PurePosixPath(path).stem} import {function.name}\ndef invoke({argument}: int) -> dict[str, int]:\n return {{'value': {function.name}({argument})}}\n"},
            {"path": "proposed/adapter/LICENSE", "content": "Permission to use, copy and modify this synthetic generated wrapper is granted.\n"}]
    assert all(item["path"] in contract["instructions"] for item in files)
    return response(role, output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text",
        "text": json.dumps(output(role, files=files, unresolved=unresolved))}]}])

