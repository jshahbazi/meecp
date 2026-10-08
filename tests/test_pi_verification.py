from __future__ import annotations

import json

from mcp.types import CallToolResult
import pytest

from meecp.pi_verification import PiVerificationError, validate_fixtures, verify_result


def fixture(*assertions, expected=None, with_expected=False):
    item = {"tool": "example", "arguments": {}}
    if assertions:
        item["assertions"] = list(assertions)
    if with_expected:
        item["expected"] = expected
    return validate_fixtures(json.dumps([item]).encode(), ["example"])[0]


def assertion(path, operator, value=None, with_value=True):
    item = {"path": path, "operator": operator}
    if with_value:
        item["value"] = value
    return item


@pytest.mark.parametrize("expected,actual,success", [
    ({"ok": True}, {"ok": True}, True), ({"ok": True}, {"ok": 1}, False),
    ({"ok": 1}, {"ok": 1.0}, False), ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
    ({"nested": [True, 2]}, {"nested": [1, 2]}, False),
])
def test_exact_structured_result_retains_strict_json_values(expected, actual, success):
    item = fixture(expected=expected, with_expected=True)
    result = CallToolResult(content=[], structuredContent=actual)
    assert verify_result(item, result) is success


def test_text_content_can_be_checked_without_structured_result():
    item = fixture(assertion("/content/0/type", "equals", "text"),
                   assertion("/content/0/text", "type", "string"),
                   assertion("/content/0/text", "contains", "finished"),
                   assertion("/content/0/text", "min_length", 8))
    assert verify_result(item, CallToolResult(content=[{"type": "text", "text": "Task finished."}]))
    assert not verify_result(item, CallToolResult(content=[{"type": "text", "text": "Pending"}]))


@pytest.mark.parametrize("kind,mime", [("image", "image/png"), ("audio", "audio/wav")])
def test_media_content_can_be_verified_generically(kind, mime):
    item = fixture(assertion("/content/0/type", "equals", kind),
                   assertion("/content/0/mimeType", "equals", mime),
                   assertion("/content/0/data", "type", "string"),
                   assertion("/content/0/data", "min_length", 4))
    result = CallToolResult(content=[{"type": kind, "mimeType": mime, "data": "c3ludGhldGlj"}])
    assert verify_result(item, result)
    assert not verify_result(item, CallToolResult(content=[]))


def test_structured_and_content_assertions_can_both_be_required():
    item = fixture(assertion("/content/0/text", "equals", "Done"), expected={"count": 3}, with_expected=True)
    assert verify_result(item, CallToolResult(content=[{"type": "text", "text": "Done"}], structuredContent={"count": 3}))
    assert not verify_result(item, CallToolResult(content=[{"type": "text", "text": "Done"}], structuredContent={"count": 4}))
    assert not verify_result(item, CallToolResult(content=[{"type": "text", "text": "Pending"}], structuredContent={"count": 3}))


def test_pointer_supports_rfc6901_escapes_empty_keys_and_arrays():
    item = fixture(assertion("/structuredContent/a~1b/~0/0/", "equals", "found"))
    result = CallToolResult(content=[], structuredContent={"a/b": {"~": [{"": "found"}]}})
    assert verify_result(item, result)
    assert verify_result(fixture(assertion("", "type", "object")), result)
    assert not verify_result(fixture(assertion("/structuredContent/a~1b/~0/00/", "exists", with_value=False)), result)


def test_exists_distinguishes_missing_from_present_null():
    result = CallToolResult(content=[], structuredContent={"empty": None})
    assert verify_result(fixture(assertion("/structuredContent/empty", "exists", with_value=False)), result)
    assert verify_result(fixture(assertion("/structuredContent/missing", "exists", False)), result)
    assert not verify_result(fixture(assertion("/structuredContent/missing", "exists", True)), result)
    assert verify_result(fixture(assertion("/structuredContent/empty", "type", "null")), result)
    assert not verify_result(fixture(assertion("/structuredContent/missing", "type", "null")), result)


@pytest.mark.parametrize("actual,kind,success", [
    (True, "boolean", True), (1, "boolean", False), (True, "integer", False), (1.0, "integer", False),
    (1, "integer", True), (1, "number", True), (1.0, "number", True), (False, "number", False),
    ([], "array", True), ({}, "object", True), ("value", "string", True),
])
def test_json_type_checks(actual, kind, success):
    result = CallToolResult(content=[], structuredContent={"value": actual})
    assert verify_result(fixture(assertion("/structuredContent/value", "type", kind)), result) is success


@pytest.mark.parametrize("actual,expected,success", [
    ([1], True, False), ([1], 1.0, False), ([1], 1, True), ([{"a": True}], {"a": True}, True),
    ({"a": True, "b": 1}, {"a": 1}, False), ({"a": True, "b": 1}, {"a": True}, True),
    ({"a": False}, "a", True), ("A completed task", "completed", True), ("text", 1, False),
])
def test_contains_has_strict_container_values(actual, expected, success):
    result = CallToolResult(content=[], structuredContent={"value": actual})
    assert verify_result(fixture(assertion("/structuredContent/value", "contains", expected)), result) is success


def test_errors_and_invalid_response_values_always_fail():
    item = fixture(assertion("/content", "exists", with_value=False))
    assert not verify_result(item, CallToolResult(content=[], isError=True))
    assert not verify_result(item, {"content": []})
    assert not verify_result(item, CallToolResult(content=[], structuredContent={"number": float("inf")}))
    assert not verify_result({"tool": "example", "arguments": {}}, CallToolResult(content=[]))


@pytest.mark.parametrize("data", [
    b"", b"{", b"{}", b"[]", b'[{"tool":"unknown","arguments":{},"expected":{}}]',
    b'[{"tool":"example","arguments":[],"expected":{}}]',
    b'[{"tool":"example","arguments":{},"expected":{},"unknown":true}]',
    b'[{"tool":"example","arguments":{},"expected":{},"expected":null}]',
    b'[{"tool":"example","arguments":{},"expected":NaN}]',
    b'[{"tool":"example","arguments":{},"expected":1e10000}]',
    b'[{"tool":"example","arguments":{},"assertions":[]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"text","operator":"exists"}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"/~2","operator":"exists"}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"/~","operator":"exists"}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"","operator":"regex","value":".*"}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"","operator":"type","value":"float"}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"","operator":"exists","value":1}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"","operator":"min_length","value":true}]}]',
    b'[{"tool":"example","arguments":{},"assertions":[{"path":"","operator":"equals"}]}]',
])
def test_malformed_fixtures_raise_only_fixed_error(data):
    with pytest.raises(PiVerificationError, match="^pi_fixture_required$"):
        validate_fixtures(data, ["example"])


def test_all_declared_tools_must_be_covered():
    data = json.dumps([{"tool": "example", "arguments": {}, "expected": {}}]).encode()
    with pytest.raises(PiVerificationError, match="^pi_fixture_required$"):
        validate_fixtures(data, ["example", "second"])
    data = json.dumps([{"tool": name, "arguments": {}, "expected": {}} for name in ["example", "second"]]).encode()
    assert len(validate_fixtures(data, ["example", "second"])) == 2


@pytest.mark.parametrize("item", [
    {"path": "/" + "a" * 512, "operator": "exists"},
    {"path": "", "operator": "min_length", "value": -1},
    {"path": "", "operator": "exists", "unknown": "value"},
])
def test_assertion_shapes_and_bounds_are_closed(item):
    with pytest.raises(PiVerificationError, match="^pi_fixture_required$"):
        fixture(item)


def test_fixture_and_assertion_counts_are_bounded():
    item = {"tool": "example", "arguments": {}, "assertions": [assertion("", "exists", with_value=False)] * 65}
    with pytest.raises(PiVerificationError, match="^pi_fixture_required$"):
        validate_fixtures(json.dumps([item]).encode(), ["example"])
    item = {"tool": "example", "arguments": {}, "expected": {}}
    with pytest.raises(PiVerificationError, match="^pi_fixture_required$"):
        validate_fixtures(json.dumps([item] * 129).encode(), ["example"])
    with pytest.raises(PiVerificationError, match="^pi_fixture_required$"):
        validate_fixtures(b" " * (1024 * 1024 + 1), ["example"])
