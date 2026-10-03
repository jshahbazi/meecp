"""Exercise the shipped Swift starter's syntax contract over actual MCP messages."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from mcpme.contracts import validate_package


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def starter(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("json_starter_preparer", ROOT / "scripts/prepare_json_package.py")
    preparer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparer)
    package = preparer.prepare(tmp_path_factory.mktemp("json-syntax").resolve() / "package")
    return validate_package(package)


def exchange(starter, requests):
    lines = [json.dumps(request, ensure_ascii=True) for request in requests]
    assert all(len(line.encode()) <= 1_048_576 for line in lines)
    executable = starter.root / starter.manifest.entrypoint.executable
    process = subprocess.run([str(executable)], input="\n".join(lines) + "\n", text=True,
                             capture_output=True, check=True, timeout=5)
    assert not process.stderr
    messages = process.stdout.splitlines()
    assert len(messages) == len(requests)
    results = []
    for request, message in zip(requests, messages, strict=True):
        response = json.loads(message)
        assert response["id"] == request["id"] and "error" not in response
        results.append(response["result"])
    return results


def validation_request(document, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": "validate_json", "arguments": {"document": document}}}


def call(starter, method="tools/call", *, document=None):
    request = (validation_request(document) if method == "tools/call"
               else {"jsonrpc": "2.0", "id": 1, "method": method})
    return exchange(starter, [request])[0]


@pytest.mark.parametrize("document,kind", [
    ("{}", "object"), ("[]", "array"), ("true", "boolean"), ("false", "boolean"), ("null", "null"),
    ('"text"', "string"), ('"\u00e9\U0001d11e"', "string"),
    (r'"\"\\\/\b\f\n\r\t"', "string"), (r'"\uD834\uDD1E"', "string"),
    (r'"\uDEAD"', "string"), (r'"\uD800"', "string"), (r'"\u0000"', "string"),
    ("-0", "number"), ("0.0e+0", "number"), ("1e400", "number"), ("-1e-9999999999", "number"),
    ("1234567890" * 1000, "number"), ("1e" + "9" * 1000, "number"),
    (' \t\r\n {"a":1e400,"a":2,"escaped\\uDEAD": [null,false,"text"]} \n', "object"),
])
def test_actual_starter_accepts_json_syntax_without_materializing_numbers(starter, document, kind):
    result = call(starter, document=document)
    assert result["isError"] is False
    assert result["structuredContent"] == {"valid": True, "kind": kind}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


@pytest.mark.parametrize("document", [
    '{"a":1,}', "[1,]", "[,1]", '{"a":}', '{a:1}', '{"a" 1}', '{"a": [1,]}',
    "", " \t\n", "true false", "True", "nul", "[]{}", "/*comment*/null", "\ufeffnull",
    "+1", "01", "-01", "1.", ".1", "1e", "1e+", "--1", "NaN", "Infinity", "-Infinity",
    '"unterminated', '"line\nbreak"', '"control\x00"', r'"\q"', r'"\u12G4"', r'"\u123"',
    '["a" "b"]', '[1}', '{"a":1]',
])
def test_actual_starter_rejects_invalid_json_syntax(starter, document):
    result = call(starter, document=document)
    assert result["isError"] is False
    assert result["structuredContent"] == {"valid": False, "kind": "invalid"}


@pytest.mark.parametrize("container", ["array", "object", "mixed"])
def test_actual_starter_depth_limit_is_a_tool_error_not_invalid_syntax(starter, container):
    def nested(depth):
        document = "null"
        for level in range(depth):
            document = ("[" + document + "]" if container == "array" or container == "mixed" and level % 2
                        else '{"a":' + document + "}")
        return document

    allowed, denied, recovered = exchange(starter, [validation_request(document, index)
        for index, document in enumerate([nested(512), nested(513), "true"])])
    assert allowed["isError"] is False and allowed["structuredContent"]["valid"] is True
    assert denied == {"content": [{"type": "text", "text": "json_limit_exceeded"}], "isError": True}
    assert recovered["structuredContent"] == {"valid": True, "kind": "boolean"}


def test_actual_starter_utf8_byte_limit_and_manifest_description(starter):
    # Include multibyte characters so a character-count implementation cannot
    # accidentally admit an oversized UTF-8 document. Both requests fit 1 MiB.
    allowed_document = '"' + "\u00e9" * 131_071 + '"'
    assert len(allowed_document.encode()) == 262_144
    assert call(starter, document=allowed_document)["structuredContent"] == {"valid": True, "kind": "string"}
    denied = call(starter, document=allowed_document[:-1] + "a\"")
    assert denied == {"content": [{"type": "text", "text": "json_limit_exceeded"}], "isError": True}
    advertised = call(starter, "tools/list")["tools"][0]["description"]
    assert advertised == starter.manifest.tools[0].description
    assert "256 KiB" in advertised and "512 nested containers" in advertised and "json_limit_exceeded" in advertised
