"""Bounded static proposals from exact imported source snapshots.

This is observation, not runtime discovery: ASTs and TOML never import source,
evaluate annotations/default expressions, run scripts, or establish capability.
Generated synthetic arguments still require a selected expected result.
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import PurePosixPath

from .preparation_components import SnapshotFile, read_snapshot
from .preparation_contracts import PreparationError, canonical_bytes, contract_digest

MAX_PROJECT_BYTES = 64 * 1024
MAX_PROJECTS = 32
MAX_PYTHON_FILES = 128
MAX_AST_BYTES = 256 * 1024
MAX_AST_TOTAL_BYTES = 2 * 1024 * 1024
MAX_AST_NODES = 10000
MAX_AST_DEPTH = 64
MAX_CANDIDATES = 128
MAX_PARAMETERS = 32
_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_PRIMITIVES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
_SYNTHETIC = {"string": "synthetic", "integer": 1, "number": 1.25, "boolean": True}


def _bound_tree(tree, *, nodes: int, depth: int) -> None:
    pending, visits = [(tree, 0)], 0
    while pending:
        item, level = pending.pop()
        visits += 1
        if visits > nodes or level > depth:
            raise PreparationError("preparation_inspection_limit")
        if isinstance(item, ast.AST):
            pending.extend((child, level + 1) for child in ast.iter_child_nodes(item))
        elif isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise PreparationError("invalid_project_metadata")
            pending.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, level + 1) for child in item)


def _evidence(file: SnapshotFile, *, line: int | None = None) -> dict:
    value = {"path": file.path, "sha256": file.sha256, "size": file.size}
    if line is not None:
        value["line"] = line
    return value


def _project(file: SnapshotFile) -> dict:
    if file.size > MAX_PROJECT_BYTES:
        raise PreparationError("preparation_inspection_limit")
    try:
        value = tomllib.loads(file.data.decode("utf-8"))
        _bound_tree(value, nodes=4096, depth=16)
        canonical_bytes(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, PreparationError):
            raise
        raise PreparationError("invalid_project_metadata") from None


def _annotation(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return _PRIMITIVES.get(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _PRIMITIVES.get(node.value)
    return None


def _dictionary_result(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "dict"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in {"dict", "dict[str, str]", "dict[str, int]", "dict[str, float]", "dict[str, bool]"}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "dict":
        subscript = node.slice
        return (isinstance(subscript, ast.Tuple) and len(subscript.elts) == 2
                and _annotation(subscript.elts[0]) == "string"
                and _annotation(subscript.elts[1]) is not None)
    return False


def _literal_default(node: ast.expr, kind: str):
    # Do not call literal_eval: only the single primitive AST node is accepted.
    if not isinstance(node, ast.Constant):
        raise PreparationError("dynamic_callable_signature")
    value = node.value
    valid = {"string": type(value) is str, "integer": type(value) is int,
             "number": type(value) in {int, float}, "boolean": type(value) is bool}[kind]
    if not valid:
        raise PreparationError("unsupported_callable_signature")
    canonical_bytes(value)
    return value


def _callable(node: ast.FunctionDef) -> tuple[dict, dict]:
    args = node.args
    if (node.decorator_list or args.posonlyargs or args.vararg or args.kwarg
            or not _dictionary_result(node.returns)):
        raise PreparationError("unsupported_callable_signature")
    parameters = args.args + args.kwonlyargs
    if len(parameters) > MAX_PARAMETERS:
        raise PreparationError("preparation_inspection_limit")
    defaults = [None] * (len(args.args) - len(args.defaults)) + args.defaults + args.kw_defaults
    properties, required, arguments = {}, [], {}
    for parameter, default in zip(parameters, defaults):
        kind = _annotation(parameter.annotation)
        if kind is None:
            raise PreparationError("unsupported_callable_signature")
        properties[parameter.arg] = {"type": kind}
        if default is None:
            required.append(parameter.arg)
        else:
            properties[parameter.arg]["default"] = _literal_default(default, kind)
        arguments[parameter.arg] = _SYNTHETIC[kind]
    schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    return schema, arguments


def _root_for(path: str, roots: list[str]) -> str:
    matches = [root for root in roots if root == "." or path.startswith(root + "/")]
    return max(matches, key=lambda root: 0 if root == "." else len(root)) if matches else "."


def _source_module(path: str) -> str | None:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    module = ".".join(parts)
    return module if _MODULE.fullmatch(module) and len(module) <= 256 else None


def _test_source(path: str) -> bool:
    """Test helpers are not inferred app entry points; explicit MCP declarations remain valid."""
    path = PurePosixPath(path)
    return (any(part in {"test", "tests"} for part in path.parts[:-1])
        or path.name == "conftest.py" or path.name.startswith("test_") or path.name.endswith("_test.py"))


def _bound_names(node: ast.AST) -> set[str]:
    """Find top-level bindings that make builtin annotation names ambiguous."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {item.asname or item.name.split(".")[0] for item in node.names}
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        pending, names = list(targets), set()
        while pending:
            target = pending.pop()
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                pending.extend(target.elts)
        return names
    return set()


def _candidate(value: dict, source_revision: str) -> dict:
    value["status"] = "proposed"
    value["verified"] = False
    value["candidate_id"] = contract_digest("preparation-inspection-candidate-v1", {"source_revision": source_revision, **value})
    return value


def inspect_source(draft: dict) -> dict:
    """Return only content-free observations and generated synthetic fixtures.

    Every selected file is hash checked twice before parsing. Missing locks are
    resolution requirements; they are never synthesized into original identity.
    Consumers must revalidate the selected source/revision before execution.
    """
    snapshot = read_snapshot(draft)
    files = snapshot.by_path()
    project_files = [file for file in snapshot.files if PurePosixPath(file.path).name == "pyproject.toml"]
    if len(project_files) > MAX_PROJECTS:
        raise PreparationError("preparation_inspection_limit")
    roots = [str(PurePosixPath(file.path).parent) for file in project_files] or ["."]
    observations, candidates, blockers, projects = [], [], [], {}
    if len(roots) > 1:
        blockers.append("service_root_selection_required")
    for file in project_files:
        root = str(PurePosixPath(file.path).parent)
        try:
            metadata = _project(file)
            projects[root] = metadata
            observations.append({"kind": "python_project", "status": "observed", "root": root, "evidence": [_evidence(file)]})
        except PreparationError as error:
            observations.append({"kind": "python_project", "status": "observed", "root": root,
                                 "evidence": [_evidence(file)], "blockers": [error.code]})
        prefix = "" if root == "." else root + "/"
        project = projects.get(root, {}).get("project", {})
        if not isinstance(project, dict):
            continue
        dependencies = project.get("dependencies", [])
        mcp_declared = (isinstance(dependencies, list) and any(isinstance(item, str)
                        and re.match(r"(?i)^mcp(?:\s|[<>=!~;\[]|$)", item) for item in dependencies))
        declarations = []
        scripts = project.get("scripts", {})
        if mcp_declared and isinstance(scripts, dict):
            declarations.extend((name, target) for name, target in scripts.items())
        tool = projects.get(root, {}).get("tool", {})
        mcpme = tool.get("mcpme", {}) if isinstance(tool, dict) else {}
        if isinstance(mcpme, dict) and isinstance(mcpme.get("mcp_module"), str):
            declarations.append(("declared-mcp-module", mcpme["mcp_module"]))
        for name, target in declarations:
            if not isinstance(name, str) or not isinstance(target, str) or len(name) > 128:
                continue
            pieces = target.split(":")
            if len(pieces) > 2 or not _MODULE.fullmatch(pieces[0]) or len(pieces[0]) > 256:
                continue
            module = pieces[0]
            local_path = prefix + module.replace(".", "/")
            entrypoint = files.get(local_path + ".py") or files.get(local_path + "/__init__.py")
            if entrypoint is None:
                continue
            unresolved = ["dynamic_mcp_discovery_required"]
            if len(pieces) == 2:
                if not re.fullmatch(r"[A-Za-z_]\w*", pieces[1]):
                    continue
                unresolved.append("entrypoint_launch_binding_required")
            if prefix + "uv.lock" not in files:
                unresolved.append("dependency_resolution_required")
            if len(roots) > 1:
                unresolved.append("service_root_selection_required")
            candidates.append(_candidate({"kind": "python-mcp", "root": root,
                "module": _source_module(entrypoint.path), "declared_module": module, "declared_entrypoint": target, "script": name,
                "tools": [], "evidence": [_evidence(file), _evidence(entrypoint)],
                "unresolved": unresolved}, snapshot.revision))
            if len(candidates) > MAX_CANDIDATES:
                raise PreparationError("preparation_inspection_limit")
    python_files = [file for file in snapshot.files if file.path.endswith(".py") and not _test_source(file.path)]
    total_bytes = 0
    for index, file in enumerate(python_files):
        if index >= MAX_PYTHON_FILES or file.size > MAX_AST_BYTES or total_bytes + file.size > MAX_AST_TOTAL_BYTES:
            blockers.append("preparation_inspection_limit")
            continue
        total_bytes += file.size
        module = _source_module(file.path)
        if module is None:
            continue
        try:
            tree = ast.parse(file.data.decode("utf-8"), mode="exec")
            _bound_tree(tree, nodes=MAX_AST_NODES, depth=MAX_AST_DEPTH)
        except (SyntaxError, UnicodeError, RecursionError, MemoryError):
            observations.append({"kind": "python_source", "status": "observed", "evidence": [_evidence(file)],
                                 "blockers": ["unresolved_python_syntax"]})
            continue
        except PreparationError as error:
            blockers.append(error.code)
            continue
        root = _root_for(file.path, roots)
        prefix = "" if root == "." else root + "/"
        top_level_names = [_bound_names(node) for node in tree.body]
        shadowed = set().union(*top_level_names) & {*_PRIMITIVES, "dict"}
        function_names = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            try:
                if shadowed or function_names.count(node.name) != 1 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", node.name):
                    raise PreparationError("ambiguous_callable_signature")
                input_schema, arguments = _callable(node)
            except PreparationError as error:
                observations.append({"kind": "python_callable", "status": "observed", "module": module,
                    "function": node.name, "evidence": [_evidence(file, line=node.lineno)], "blockers": [error.code]})
                continue
            unresolved = ["expected_result_required", "effects_declaration_required"]
            if len(roots) > 1:
                unresolved.append("service_root_selection_required")
            if prefix + "uv.lock" not in files:
                unresolved.append("dependency_resolution_required")
            if prefix + "pyproject.toml" not in files:
                unresolved.append("dependency_project_required")
            licenses = [item.path for item in snapshot.files if PurePosixPath(item.path).name.lower() in {"license", "copying"} and item.size]
            if not licenses:
                unresolved.append("source_license_required")
            tool = {"name": node.name, "description": "Call the selected function with typed arguments.",
                    "input_schema": input_schema, "output_schema": {"type": "object"}}
            candidates.append(_candidate({"kind": "python-callable", "root": root, "module": module,
                "function": node.name, "tools": [tool], "evidence": [_evidence(file, line=node.lineno)],
                "fixture": {"id": "synthetic", "tool": node.name, "arguments": arguments, "expected": None, "synthetic": True},
                "license_candidates": licenses, "unresolved": unresolved}, snapshot.revision))
            if len(candidates) > MAX_CANDIDATES:
                raise PreparationError("preparation_inspection_limit")
    if len(candidates) > MAX_CANDIDATES:
        raise PreparationError("preparation_inspection_limit")
    if not candidates:
        blockers.append("supported_interface_selection_required")
    elif len(candidates) > 1:
        blockers.append("interface_selection_required")
    result = {"schema_version": 1, "source_revision": snapshot.revision, "execution_available": False,
              "verified_capabilities": [], "roots": roots, "observations": observations,
              "candidates": candidates, "blockers": sorted(set(blockers))}
    canonical_bytes(result)
    # Detect changes while AST/TOML parsing ran, not only during initial copying.
    if read_snapshot(draft) != snapshot:
        raise PreparationError("source_identity_mismatch")
    return result
