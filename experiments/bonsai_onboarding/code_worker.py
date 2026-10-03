"""Frozen, in-memory semantic checks behind a restricted candidate source gate.

The caller additionally denies filesystem writes and networking with sandbox-exec.
The source gate is a capability restriction, not a general Python security sandbox.
Only fixed test identifiers and counts cross the worker's output boundary.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import copy
import io
import json
import math
import numbers
import os
import resource
import sys
import types
import typing
from typing import Any

_MAX_INPUT = 64 * 1024
_MAX_ADDRESS_SPACE = 1024 * 1024 * 1024
_MAX_CPU_SECONDS = 2
_ENTRY = {"validate_arguments", "invoke"}
_FROM_IMPORTS = {
    "os": {"path"},
    "os.path": {"isabs"},
    "math": {"isfinite", "isnan", "isinf"},
    "pathlib": {"Path", "PurePath", "PurePosixPath"},
    "contextlib": {"redirect_stdout", "redirect_stderr"},
    "io": {"StringIO"},
    "json": {"loads", "dumps"},
    "sys": {"stdout", "stderr"},
    "numbers": {"Number", "Real", "Integral"},
    "typing": {"Any", "Callable", "Dict", "List", "Optional", "Tuple", "Union",
               "Mapping", "MutableMapping", "Sequence", "Set"},
    "__future__": {"annotations"},
}
_ATTRIBUTES = {
    "path", "isabs", "Path", "PurePath", "PurePosixPath", "is_absolute",
    "isfinite", "isnan", "isinf", "redirect_stdout", "redirect_stderr",
    "StringIO", "getvalue", "write", "stdout", "stderr", "loads", "dumps",
    "Number", "Real", "Integral", "Any", "Callable", "Dict", "List", "Optional",
    "Tuple", "Union", "Mapping", "MutableMapping", "Sequence", "Set",
    "get", "keys", "items", "values", "copy", "pop", "clear", "update",
    "setdefault", "append", "extend", "startswith", "endswith", "strip",
    "lstrip", "rstrip", "replace", "lower", "upper", "isdigit", "format",
}
_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "int", "float", "bool", "str", "dict", "set", "frozenset", "tuple",
        "list", "object", "bytes", "isinstance", "issubclass", "type", "len",
        "any", "all", "abs", "min", "max", "round", "sum", "sorted", "range",
        "enumerate", "zip", "ValueError", "TypeError", "Exception", "RuntimeError",
        "ArithmeticError", "OverflowError", "KeyError", "IndexError",
    )
}


class _LexicalPath:
    """Expose a POSIX lexical absolute-path check without filesystem methods."""

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("invalid_path_type")
        self._value = value

    def is_absolute(self) -> bool:
        return self._value.startswith("/")


class _Streams:
    """Allow manual Python stream redirection without exposing the sys module."""

    @property
    def stdout(self) -> Any:
        return sys.stdout

    @stdout.setter
    def stdout(self, value: Any) -> None:
        sys.stdout = value

    @property
    def stderr(self) -> Any:
        return sys.stderr

    @stderr.setter
    def stderr(self, value: Any) -> None:
        sys.stderr = value


_PATH_MODULE = types.SimpleNamespace(isabs=os.path.isabs)
_MODULES = {
    "os": types.SimpleNamespace(path=_PATH_MODULE),
    "os.path": _PATH_MODULE,
    "math": types.SimpleNamespace(isfinite=math.isfinite, isnan=math.isnan, isinf=math.isinf),
    "pathlib": types.SimpleNamespace(Path=_LexicalPath, PurePath=_LexicalPath,
                                    PurePosixPath=_LexicalPath),
    "contextlib": types.SimpleNamespace(redirect_stdout=contextlib.redirect_stdout,
                                       redirect_stderr=contextlib.redirect_stderr),
    "io": types.SimpleNamespace(StringIO=io.StringIO),
    "json": types.SimpleNamespace(loads=json.loads, dumps=json.dumps),
    "sys": _Streams(),
    "numbers": types.SimpleNamespace(Number=numbers.Number, Real=numbers.Real,
                                    Integral=numbers.Integral),
    "typing": types.SimpleNamespace(**{
        name: getattr(typing, name) for name in _FROM_IMPORTS["typing"]
    }),
    "__future__": types.SimpleNamespace(annotations=__import__("__future__").annotations),
}


def validate_arguments(arguments: Any) -> dict[str, Any]:
    """Trusted oracle matching the frozen synthetic task, without path I/O."""
    if not isinstance(arguments, dict) or set(arguments) - {"image_path", "confidence"}:
        raise ValueError("invalid_tool_arguments")
    if "image_path" not in arguments:
        raise ValueError("invalid_tool_arguments")
    path = arguments["image_path"]
    if (not isinstance(path, str) or not path or not path.startswith("/")
            or "\x00" in path or len(path) > 4096):
        raise ValueError("invalid_tool_arguments")
    confidence = arguments.get("confidence", 0.35)
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0.05 <= confidence <= 0.95):
        raise ValueError("invalid_tool_arguments")
    return {"image_path": path, "confidence": float(confidence)}


# Freeze these vectors before the first model call. Paths are never opened.
_ARGUMENT_CASES = (
    ("valid_default", {"image_path": "/synthetic/no-such-image.png"}, True),
    ("valid_lower_bound", {"image_path": "/x", "confidence": 0.05}, True),
    ("valid_upper_bound", {"image_path": "/x", "confidence": 0.95}, True),
    ("valid_explicit_float", {"image_path": "/x", "confidence": 0.5}, True),
    ("valid_unicode_spaces", {"image_path": "/synthetic/猫 café image.png"}, True),
    ("valid_root", {"image_path": "/"}, True),
    ("valid_preserve_path_text", {"image_path": "//synthetic/a/../ b.png"}, True),
    ("valid_max_path_length", {"image_path": "/" + "a" * 4095}, True),
    ("invalid_nonobject_none", None, False),
    ("invalid_nonobject_list", [], False),
    ("invalid_nonobject_string", "/x", False),
    ("invalid_nonobject_number", 7, False),
    ("invalid_nonobject_bool", False, False),
    ("invalid_missing_path", {}, False),
    ("invalid_extra_key", {"image_path": "/x", "extra": 1}, False),
    ("invalid_base64_key", {"image_path": "/x", "image_base64": "fixture"}, False),
    ("invalid_path_none", {"image_path": None}, False),
    ("invalid_path_number", {"image_path": 1}, False),
    ("invalid_path_bool", {"image_path": True}, False),
    ("invalid_path_object", {"image_path": {"path": "/x"}}, False),
    ("invalid_path_list", {"image_path": ["/x"]}, False),
    ("invalid_path_empty", {"image_path": ""}, False),
    ("invalid_path_relative", {"image_path": "relative.png"}, False),
    ("invalid_path_nul", {"image_path": "/synthetic/a\x00b.png"}, False),
    ("invalid_path_too_long", {"image_path": "/" + "a" * 4096}, False),
    ("invalid_confidence_true", {"image_path": "/x", "confidence": True}, False),
    ("invalid_confidence_false", {"image_path": "/x", "confidence": False}, False),
    ("invalid_confidence_nan", {"image_path": "/x", "confidence": float("nan")}, False),
    ("invalid_confidence_pos_inf", {"image_path": "/x", "confidence": float("inf")}, False),
    ("invalid_confidence_neg_inf", {"image_path": "/x", "confidence": -float("inf")}, False),
    ("invalid_confidence_low", {"image_path": "/x", "confidence": 0.049}, False),
    ("invalid_confidence_high", {"image_path": "/x", "confidence": 0.951}, False),
    ("invalid_confidence_integer", {"image_path": "/x", "confidence": 1}, False),
    ("invalid_confidence_string", {"image_path": "/x", "confidence": "0.5"}, False),
    ("invalid_confidence_none", {"image_path": "/x", "confidence": None}, False),
    ("invalid_confidence_object", {"image_path": "/x", "confidence": {"value": 0.5}}, False),
)
_INVOKE_CASES = (
    "invoke_success_default", "invoke_success_unicode", "invoke_success_empty_dict",
    "invoke_success_dict_subclass", "invoke_uses_trusted_validation",
    "invoke_invalid_arguments", "invoke_validation_valueerror",
    "invoke_validation_other_exception", "invoke_detector_exception",
    "invoke_detector_valueerror",
    "invoke_result_none", "invoke_result_list", "invoke_result_string",
    "invoke_result_number",
)


def _resource_limits() -> bool:
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_CPU)
        limit = _MAX_CPU_SECONDS if hard == resource.RLIM_INFINITY else min(_MAX_CPU_SECONDS, hard)
        resource.setrlimit(resource.RLIMIT_CPU, (limit, limit))
    except (OSError, ValueError):
        return False
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = _MAX_ADDRESS_SPACE if hard == resource.RLIM_INFINITY else min(_MAX_ADDRESS_SPACE, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError):
        # Darwin aliases RLIMIT_AS to RSS and rejects this limit. The trusted
        # caller must terminate the worker above its 1 GiB RSS threshold.
        return sys.platform == "darwin"
    return True


def _annotation_safe(node: ast.expr | None) -> bool:
    if node is None:
        return True
    return all(isinstance(part, (
        ast.Name, ast.Attribute, ast.Subscript, ast.Tuple, ast.List, ast.Constant,
        ast.BinOp, ast.BitOr, ast.Load,
    )) for part in ast.walk(node))


def _safe_tree(source: str, task: str) -> ast.Module | None:
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return None
    body = list(tree.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body.pop(0)
    functions = [node for node in body if isinstance(node, ast.FunctionDef)]
    if (len(functions) != 1 or functions[0].name != task
            or any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))
                   for node in body)):
        return None
    function = functions[0]
    args = function.args
    if (function.decorator_list or function.type_comment or getattr(function, "type_params", [])
            or args.vararg or args.kwarg or args.kwonlyargs or args.defaults
            or len(args.posonlyargs + args.args) != (1 if task == "validate_arguments" else 2)
            or not _annotation_safe(function.returns)
            or any(not _annotation_safe(arg.annotation) for arg in args.posonlyargs + args.args)):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                if any(alias.name not in _FROM_IMPORTS for alias in node.names):
                    return None
            elif (node.level or node.module not in _FROM_IMPORTS
                  or any(alias.name not in _FROM_IMPORTS[node.module] for alias in node.names)):
                return None
            if isinstance(node, ast.ImportFrom) and node.module != "__future__":
                if any((alias.asname or alias.name).startswith("__")
                       or (alias.asname or alias.name) == "validate_arguments"
                       for alias in node.names):
                    return None
            if isinstance(node, ast.Import) and any(
                (alias.asname or alias.name.split(".")[0]).startswith("__")
                for alias in node.names
            ):
                return None
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                             ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom,
                             ast.Await, ast.Delete)):
            return None
        if isinstance(node, ast.FunctionDef) and node is not function:
            return None
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                return None
            if task == "invoke" and node.id == "validate_arguments" and isinstance(node.ctx, ast.Store):
                return None
        if isinstance(node, ast.arg) and node.arg.startswith("__"):
            return None
        if isinstance(node, ast.Attribute):
            if node.attr not in _ATTRIBUTES:
                return None
            if isinstance(node.ctx, ast.Store) and node.attr not in {"stdout", "stderr"}:
                return None
    return ast.Module(body=body, type_ignores=[])


def _restricted_import(name: str, globals: Any = None, locals: Any = None,
                       fromlist: tuple[str, ...] = (), level: int = 0) -> Any:
    if level or name not in _MODULES:
        raise ImportError("module_not_allowed")
    if fromlist:
        if any(item not in _FROM_IMPORTS[name] for item in fromlist):
            raise ImportError("member_not_allowed")
        return _MODULES[name]
    return _MODULES[name.split(".")[0]]


def _capture_call(function: Any, *arguments: Any) -> tuple[Any, BaseException | None, bool]:
    """Observe the candidate's output and stream restoration independently."""
    previous_out, previous_err = sys.stdout, sys.stderr
    captured_out, captured_err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = captured_out, captured_err
    result, error, restored = None, None, False
    try:
        result = function(*arguments)
    except BaseException as exc:
        error = exc
    finally:
        restored = sys.stdout is captured_out and sys.stderr is captured_err
        sys.stdout, sys.stderr = previous_out, previous_err
    try:
        quiet = captured_out.getvalue() == "" and captured_err.getvalue() == ""
    except ValueError:
        quiet = False
    return result, error, restored and quiet


def _load(source: str, task: str) -> dict[str, Any] | None:
    tree = _safe_tree(source, task)
    if tree is None:
        return None
    scope: dict[str, Any] = {
        "__builtins__": {**_BUILTINS, "__import__": _restricted_import},
    }
    if task == "invoke":
        scope["validate_arguments"] = validate_arguments
    try:
        _, error, quiet = _capture_call(
            lambda: exec(compile(tree, "<candidate>", "exec"), scope, scope)
        )
        if error is not None or not quiet:
            return None
    except BaseException:
        return None
    return scope


def _fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        return ("dict", tuple((_fingerprint(key), _fingerprint(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_fingerprint(item) for item in value))
    if isinstance(value, float):
        return ("float", value.hex())
    return (type(value).__name__, value)


def _argument_test(function: Any, original: Any, valid: bool) -> bool:
    argument = copy.deepcopy(original)
    before = _fingerprint(argument)
    expected = validate_arguments(argument) if valid else None
    result, error, quiet = _capture_call(function, argument)
    if not quiet or _fingerprint(argument) != before:
        return False
    if valid:
        return (error is None and type(result) is dict and result is not argument
                and result == expected and type(result.get("confidence")) is float
                and type(result.get("image_path")) is str)
    return type(error) is ValueError and str(error) == "invalid_tool_arguments"


class _ResultDict(dict):
    pass


def _fixture_noise() -> None:
    # Fixed synthetic fixtures, never candidate text or exception contents.
    print("fixture_stdout")
    print("fixture_stderr", file=sys.stderr)


def _invoke_test(function: Any, scope: dict[str, Any], case: str) -> bool:
    argument = {"image_path": "/synthetic/a.png"}
    if case == "invoke_success_unicode":
        argument = {"image_path": "/synthetic/猫 café image.png", "confidence": 0.05}
    elif case == "invoke_invalid_arguments":
        argument = {"image_path": "relative.png"}
    before = _fingerprint(argument)
    validation_calls: list[Any] = []
    detector_calls: list[Any] = []
    expected_kwargs = (
        {"image_path": "/trusted/ normalized.png", "confidence": 0.6}
        if case == "invoke_uses_trusted_validation"
        else (None if case == "invoke_invalid_arguments" else validate_arguments(argument))
    )
    result_object: Any = {"ok": True, "nested": {"items": [1, 2]}}
    if case == "invoke_success_empty_dict":
        result_object = {}
    elif case == "invoke_success_dict_subclass":
        result_object = _ResultDict(ok=True)
    elif case == "invoke_result_none":
        result_object = None
    elif case == "invoke_result_list":
        result_object = []
    elif case == "invoke_result_string":
        result_object = "fixture"
    elif case == "invoke_result_number":
        result_object = 1

    def checked_validation(value: Any) -> dict[str, Any]:
        validation_calls.append(value)
        _fixture_noise()
        if case == "invoke_validation_valueerror":
            raise ValueError("fixture_exception")
        if case == "invoke_validation_other_exception":
            raise TypeError("fixture_exception")
        if case == "invoke_uses_trusted_validation":
            return dict(expected_kwargs)
        return validate_arguments(value)

    def checked_detector(**kwargs: Any) -> Any:
        detector_calls.append(kwargs)
        _fixture_noise()
        if case == "invoke_detector_exception":
            raise RuntimeError("fixture_exception")
        if case == "invoke_detector_valueerror":
            raise ValueError("fixture_exception")
        return result_object

    scope["validate_arguments"] = checked_validation
    returned, error, quiet = _capture_call(function, argument, checked_detector)
    if (not quiet or error is not None or _fingerprint(argument) != before
            or len(validation_calls) != 1 or validation_calls[0] is not argument):
        return False
    validation_failed = case in {
        "invoke_invalid_arguments", "invoke_validation_valueerror",
        "invoke_validation_other_exception",
    }
    if len(detector_calls) != (0 if validation_failed else 1):
        return False
    if detector_calls and (detector_calls[0] != expected_kwargs
                           or type(detector_calls[0].get("confidence")) is not float):
        return False
    if case in {"invoke_invalid_arguments", "invoke_validation_valueerror"}:
        return type(returned) is dict and returned == {"error": "invalid_tool_arguments"}
    if case in {"invoke_validation_other_exception", "invoke_detector_exception",
                "invoke_detector_valueerror"}:
        return type(returned) is dict and returned == {"error": "app_execution_failed"}
    if case.startswith("invoke_result_"):
        return type(returned) is dict and returned == {"error": "invalid_tool_result"}
    return returned is result_object


def _run_tests(task: str, scope: dict[str, Any]) -> tuple[int, list[str]]:
    cases = _ARGUMENT_CASES if task == "validate_arguments" else _INVOKE_CASES
    function = scope.get(task)
    if not callable(function):
        return len(cases), [item[0] if task == "validate_arguments" else item for item in cases]
    failures: list[str] = []
    for item in cases:
        case_id = item[0] if task == "validate_arguments" else item
        try:
            passed = (_argument_test(function, item[1], item[2])
                      if task == "validate_arguments"
                      else _invoke_test(function, scope, item))
        except BaseException:
            passed = False
        if not passed:
            failures.append(case_id)
    return len(cases), failures


_CORRECT_VALIDATOR = """import os
from math import isfinite as finite
from typing import Any
def validate_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {"image_path", "confidence"}:
        raise ValueError("invalid_tool_arguments")
    path = arguments.get("image_path")
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\\x00" in path or len(path) > 4096:
        raise ValueError("invalid_tool_arguments")
    value = arguments.get("confidence", 0.35)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite(value) or not 0.05 <= value <= 0.95:
        raise ValueError("invalid_tool_arguments")
    return {"image_path": path, "confidence": float(value)}
"""
_CORRECT_INVOKE = """import contextlib as streams
from io import StringIO
def invoke(arguments, detect):
    with streams.redirect_stdout(StringIO()), streams.redirect_stderr(StringIO()):
        try:
            validated = validate_arguments(arguments)
        except ValueError:
            return {"error": "invalid_tool_arguments"}
        except Exception:
            return {"error": "app_execution_failed"}
        try:
            result = detect(**validated)
        except Exception:
            return {"error": "app_execution_failed"}
    return result if isinstance(result, dict) else {"error": "invalid_tool_result"}
"""
_BAD_VALIDATOR = """def validate_arguments(arguments):
    return {"image_path": "/synthetic/a.png", "confidence": 0.35}
"""
_BAD_NOISY_INVOKE = """def invoke(arguments, detect):
    try:
        validated = validate_arguments(arguments)
    except ValueError:
        return {"error": "invalid_tool_arguments"}
    except Exception:
        return {"error": "app_execution_failed"}
    try:
        result = detect(**validated)
    except Exception:
        return {"error": "app_execution_failed"}
    return result if isinstance(result, dict) else {"error": "invalid_tool_result"}
"""
_USER_CORRECT_INVOKE = """import contextlib
import io
def invoke(arguments, detect):
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            validated = validate_arguments(arguments)
        except ValueError:
            return {"error": "invalid_tool_arguments"}
        except Exception:
            return {"error": "app_execution_failed"}
        try:
            result = detect(**validated)
        except Exception:
            return {"error": "app_execution_failed"}
    if not isinstance(result, dict):
        return {"error": "invalid_tool_result"}
    return result
"""
_BAD_COMBINED_INVOKE = """import contextlib
import io
def invoke(arguments, detect):
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            validated = validate_arguments(arguments)
            result = detect(**validated)
        except ValueError:
            return {"error": "invalid_tool_arguments"}
        except Exception:
            return {"error": "app_execution_failed"}
    if not isinstance(result, dict):
        return {"error": "invalid_tool_result"}
    return result
"""


def _report(passed: int, total: int, failed_tests: list[str], failure: str | None) -> None:
    print(json.dumps({
        "pass": failure is None and not failed_tests and passed == total,
        "passed": passed, "total": total, "failed_tests": failed_tests, "failure": failure,
    }, separators=(",", ":"), allow_nan=False))


def _self_test() -> int:
    failures: list[str] = []
    fixtures = (
        ("selftest_correct_validator", _CORRECT_VALIDATOR, "validate_arguments", True),
        ("selftest_correct_path_validator",
         _CORRECT_VALIDATOR.replace("import os", "from pathlib import Path")
         .replace("os.path.isabs(path)", "Path(path).is_absolute()"),
         "validate_arguments", True),
        ("selftest_correct_invoke", _CORRECT_INVOKE, "invoke", True),
        ("selftest_user_correct_invoke", _USER_CORRECT_INVOKE, "invoke", True),
        ("selftest_correct_local_import_validator",
         _CORRECT_VALIDATOR.replace(
             "import os\nfrom math import isfinite as finite\nfrom typing import Any\n",
             "from __future__ import annotations\n")
         .replace(
             "def validate_arguments(arguments: Any) -> dict[str, Any]:",
             "def validate_arguments(arguments: Any) -> dict[str, Any]:\n"
             "    import os\n    from math import isfinite as finite\n    from typing import Any"),
         "validate_arguments", True),
        ("selftest_correct_local_import_invoke",
         _CORRECT_INVOKE.replace("import contextlib as streams\nfrom io import StringIO\n", "")
         .replace("def invoke(arguments, detect):",
                  "def invoke(arguments, detect):\n"
                  "    import contextlib as streams\n    from io import StringIO"),
         "invoke", True),
        ("selftest_bad_validator", _BAD_VALIDATOR, "validate_arguments", False),
        ("selftest_mutating_validator",
         _CORRECT_VALIDATOR.replace(
             '    return {"image_path": path, "confidence": float(value)}',
             '    arguments["confidence"] = value\n    return {"image_path": path, "confidence": float(value)}'),
         "validate_arguments", False),
        ("selftest_bad_noisy_invoke", _BAD_NOISY_INVOKE, "invoke", False),
        ("selftest_bad_combined_try_invoke", _BAD_COMBINED_INVOKE, "invoke", False),
        ("selftest_bad_stream_restoration",
         _BAD_NOISY_INVOKE.replace(
             "def invoke(arguments, detect):",
             "import sys\nfrom io import StringIO\ndef invoke(arguments, detect):\n"
             "    sys.stdout = StringIO()\n    sys.stderr = StringIO()"),
         "invoke", False),
    )
    for case_id, source, task, good in fixtures:
        scope = _load(source, task)
        if scope is None:
            failures.append(case_id)
            continue
        _, failed = _run_tests(task, scope)
        if ((not failed) != good or (case_id == "selftest_bad_combined_try_invoke"
                                    and failed != ["invoke_detector_valueerror"])):
            failures.append(case_id)
    forbidden = (
        ("selftest_path_io_gate", "from pathlib import Path\ndef validate_arguments(a):\n return Path('/x').read_text()\n"),
        ("selftest_import_gate", "import subprocess\ndef validate_arguments(a):\n return {}\n"),
        ("selftest_dunder_gate", "def validate_arguments(a):\n return a.__class__\n"),
        ("selftest_local_unsafe_import_gate", "def validate_arguments(a):\n import subprocess\n return {}\n"),
        ("selftest_extra_function_gate", "def helper(a):\n return a\ndef validate_arguments(a):\n return {}\n"),
    )
    for case_id, source in forbidden:
        if _load(source, "validate_arguments") is not None:
            failures.append(case_id)
    total = len(fixtures) + len(forbidden)
    _report(total - len(failures), total, failures, None if not failures else "self_test_failed")
    return 0 if not failures else 1


def main() -> int:
    if not _resource_limits():
        _report(0, 0, ["resource_limits"], "resource_limits")
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        return _self_test()
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            _report(0, 0, ["input_too_large"], "input_too_large")
            return 0
        request = json.loads(raw)
    except (ValueError, UnicodeError, MemoryError, RecursionError):
        _report(0, 0, ["invalid_request"], "invalid_request")
        return 0
    if (type(request) is not dict or type(request.get("task_id")) is not str
            or request["task_id"] not in _ENTRY
            or not isinstance(request.get("code"), str)):
        _report(0, 0, ["invalid_request"], "invalid_request")
        return 0
    try:
        scope = _load(request["code"], request["task_id"])
        if scope is None:
            _report(0, 0, ["source_gate"], "source_rejected")
            return 0
        total, failures = _run_tests(request["task_id"], scope)
        _report(total - len(failures), total, failures, None if not failures else "semantic_tests")
    except BaseException:
        _report(0, 0, ["worker_check"], "worker_check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
