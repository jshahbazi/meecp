"""Generic deterministic assertions for independently exercised MCP tools."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from mcp.types import CallToolResult


MAX_FIXTURE_BYTES = 1024 * 1024
MAX_FIXTURES = 128
MAX_ASSERTIONS = 64
_MISSING = object()
_OPERATORS = {"equals", "type", "contains", "exists", "min_length"}
_TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}


class PiVerificationError(ValueError):
    def __init__(self):
        self.code = "pi_fixture_required"
        super().__init__(self.code)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PiVerificationError()
        result[key] = value
    return result


def _json_value(value: Any) -> None:
    """Reject non-JSON values and bound traversal without evaluating expressions."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 100_000 or depth > 64:
            raise PiVerificationError()
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise PiVerificationError()
                key.encode("utf-8")
                pending.append((child, depth + 1))
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item.encode("utf-8")
        elif type(item) is float:
            if not math.isfinite(item):
                raise PiVerificationError()
        elif item is not None and type(item) not in (bool, int):
            raise PiVerificationError()


def _assertion(value: Any) -> None:
    if (type(value) is not dict or set(value) - {"path", "operator", "value"}
            or not {"path", "operator"} <= set(value)):
        raise PiVerificationError()
    path, operator = value["path"], value["operator"]
    if (type(path) is not str or len(path.encode("utf-8")) > 512
            or path and not path.startswith("/") or re.search(r"~(?:[^01]|$)", path)
            or type(operator) is not str or operator not in _OPERATORS):
        raise PiVerificationError()
    if operator == "exists":
        if "value" in value and type(value["value"]) is not bool:
            raise PiVerificationError()
    elif "value" not in value:
        raise PiVerificationError()
    elif operator == "type":
        if type(value["value"]) is not str or value["value"] not in _TYPES:
            raise PiVerificationError()
    elif operator == "min_length":
        if type(value["value"]) is not int or not 0 <= value["value"] <= MAX_FIXTURE_BYTES:
            raise PiVerificationError()


def validate_fixtures(data: bytes, tools: Iterable[str]) -> list[dict[str, Any]]:
    """Load a closed fixture shape and require coverage of every declared tool.

    `expected` compares structuredContent exactly. `assertions` inspect the full
    SDK CallToolResult using RFC 6901 pointers, including text/image/audio blocks.
    Both can be supplied when a tool has structured and unstructured outputs.
    """
    try:
        names = list(tools)
        if (type(data) is not bytes or not 0 < len(data) <= MAX_FIXTURE_BYTES
                or not names or any(type(name) is not str or not name for name in names)
                or len(set(names)) != len(names)):
            raise PiVerificationError()
        fixtures = json.loads(data, object_pairs_hook=_unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(PiVerificationError()))
        _json_value(fixtures)
        if type(fixtures) is not list or not 1 <= len(fixtures) <= MAX_FIXTURES:
            raise PiVerificationError()
        for fixture in fixtures:
            if (type(fixture) is not dict or set(fixture) - {"tool", "arguments", "expected", "assertions"}
                    or not {"tool", "arguments"} <= set(fixture)
                    or type(fixture["tool"]) is not str or type(fixture["arguments"]) is not dict
                    or not ({"expected", "assertions"} & set(fixture))):
                raise PiVerificationError()
            if "assertions" in fixture:
                assertions = fixture["assertions"]
                if type(assertions) is not list or not 1 <= len(assertions) <= MAX_ASSERTIONS:
                    raise PiVerificationError()
                for assertion in assertions:
                    _assertion(assertion)
        if {fixture["tool"] for fixture in fixtures} != set(names):
            raise PiVerificationError()
        return fixtures
    except PiVerificationError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise PiVerificationError() from None


def _equal(left: Any, right: Any) -> bool:
    # Canonical JSON retains the existing distinction between booleans, integer
    # numbers, and floating-point numbers, including those nested in containers.
    return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
        right, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pointer(value: Any, path: str) -> Any:
    if not path:
        return value
    for token in path[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if type(value) is dict:
            value = value.get(token, _MISSING)
        elif type(value) is list and re.fullmatch(r"0|[1-9][0-9]*", token):
            index = int(token)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
        if value is _MISSING:
            break
    return value


def _matches(actual: Any, assertion: dict[str, Any]) -> bool:
    operator = assertion["operator"]
    expected = assertion.get("value", True)
    if operator == "exists":
        return (actual is not _MISSING) == expected
    if actual is _MISSING:
        return False
    if operator == "equals":
        return _equal(actual, expected)
    if operator == "type":
        return {"null": actual is None, "boolean": type(actual) is bool, "integer": type(actual) is int,
                "number": type(actual) in (int, float), "string": type(actual) is str,
                "array": type(actual) is list, "object": type(actual) is dict}[expected]
    if operator == "min_length":
        return type(actual) in (str, list, dict) and len(actual) >= expected
    if operator == "contains":
        if type(actual) is str:
            return type(expected) is str and expected in actual
        if type(actual) is list:
            return any(_equal(item, expected) for item in actual)
        if type(actual) is dict:
            if type(expected) is str:
                return expected in actual
            if type(expected) is dict:
                return all(key in actual and _equal(actual[key], item) for key, item in expected.items())
    return False


def verify_result(fixture: dict[str, Any], result: CallToolResult) -> bool:
    """Return only a verdict; no result values enter diagnostics or exceptions."""
    try:
        if not isinstance(result, CallToolResult) or result.isError or not ({"expected", "assertions"} & set(fixture)):
            return False
        value = result.model_dump(by_alias=True, exclude_none=True)
        _json_value(value)
        if "expected" in fixture and not _equal(result.structuredContent, fixture["expected"]):
            return False
        return all(_matches(_pointer(value, assertion["path"]), assertion) for assertion in fixture.get("assertions", []))
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError, KeyError):
        return False
