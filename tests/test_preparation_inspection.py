"""Static source proposals never execute code or manufacture verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_inspection import inspect_source


@pytest.fixture
def inspect_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with ImportStore(tmp_path / "state") as imports:
        def run(files):
            for name, content in files.items():
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            draft = imports.create(source, name="Static inspection fixture")
            return draft, inspect_source(draft)
        yield source, run


def test_supported_typed_function_generates_schema_and_incomplete_fixture(inspect_fixture, monkeypatch):
    source, run = inspect_fixture
    marker = source / "marker.txt"
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("inspection launched a process"))
    draft, result = run({
        "worker.py": f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
                     "def calculate(label: str, count: int, ratio: float, enabled: bool = False) -> dict[str, bool]:\n"
                     "    return {'ok': enabled}\n",
        "pyproject.toml": "[project]\nname='fixture'\nversion='1.0.0'\n",
        "uv.lock": "version=1\n", "LICENSE": "Fixture permission notice\n",
    })
    assert not marker.exists()
    candidate, = result["candidates"]
    assert candidate["kind"] == "python-callable" and candidate["status"] == "proposed" and not candidate["verified"]
    schema = candidate["tools"][0]["input_schema"]
    assert schema["required"] == ["label", "count", "ratio"]
    assert schema["properties"]["enabled"] == {"type": "boolean", "default": False}
    assert candidate["fixture"]["arguments"] == {"label": "synthetic", "count": 1, "ratio": 1.25, "enabled": True}
    assert candidate["fixture"]["expected"] is None and "expected_result_required" in candidate["unresolved"]
    assert result["source_revision"] == draft["source"]["revision"]
    assert result["verified_capabilities"] == [] and result["execution_available"] is False


@pytest.mark.parametrize("signature", [
    "value", "value: list[str]", "value: custom()", "value: str = dangerous()",
    "value: int = True", "value: bool = 1", "*value: str", "**value: str", "value: str, /",
])
def test_unsupported_or_dynamic_signatures_remain_observations(inspect_fixture, signature):
    _, run = inspect_fixture
    _, result = run({"worker.py": f"def calculate({signature}) -> dict:\n    return {{}}\n"})
    assert result["candidates"] == []
    assert result["observations"][0]["status"] == "observed"
    assert result["observations"][0]["blockers"][0] in {"unsupported_callable_signature", "dynamic_callable_signature"}


@pytest.mark.parametrize("source", [
    "@dangerous()\ndef calculate(value: str) -> dict:\n    return {}\n",
    "async def calculate(value: str) -> dict:\n    return {}\n",
    "class Worker:\n    def calculate(self, value: str) -> dict:\n        return {}\n",
    "def calculate(value: str) -> execute():\n    return {}\n",
])
def test_decorated_async_nested_and_evaluated_result_types_are_not_supported(inspect_fixture, source):
    _, run = inspect_fixture
    _, result = run({"worker.py": source})
    assert result["candidates"] == []


def test_string_annotations_do_not_evaluate_python(inspect_fixture):
    _, run = inspect_fixture
    _, result = run({"worker.py": "def calculate(value: 'str') -> 'dict[str, bool]':\n    return {}\n"})
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["tools"][0]["input_schema"]["properties"]["value"]["type"] == "string"


@pytest.mark.parametrize("source", [
    "str = int\ndef calculate(value: str) -> dict:\n    return {}\n",
    "from custom import dict\ndef calculate(value: str) -> dict:\n    return {}\n",
    "def calculate(value: str) -> dict:\n    return {}\ndef calculate(value: int) -> dict:\n    return {}\n",
])
def test_shadowed_annotation_names_and_duplicate_functions_require_resolution(inspect_fixture, source):
    _, run = inspect_fixture
    _, result = run({"worker.py": source})
    assert result["candidates"] == []
    assert all(item["blockers"] == ["ambiguous_callable_signature"] for item in result["observations"])


def test_missing_lock_and_license_are_resolution_requirements(inspect_fixture):
    _, run = inspect_fixture
    draft, result = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    candidate, = result["candidates"]
    assert {"dependency_resolution_required", "dependency_project_required", "source_license_required"} <= set(candidate["unresolved"])
    assert {item["path"] for item in draft["source"]["files"]} == {"worker.py"}
    assert "uv.lock" not in json.dumps(draft["source"]["files"])


def test_monorepo_roots_require_selection_and_nearest_root_is_preserved(inspect_fixture):
    _, run = inspect_fixture
    _, result = run({
        "pyproject.toml": "[project]\nname='parent'\nversion='1.0.0'\n",
        "services/a/pyproject.toml": "[project]\nname='service-a'\nversion='1.0.0'\n",
        "services/b/pyproject.toml": "[project]\nname='service-b'\nversion='1.0.0'\n",
        "services/a/worker.py": "def calculate(value: str) -> dict:\n    return {}\n",
        "services/b/worker.py": "def calculate(value: int) -> dict:\n    return {}\n",
    })
    assert "service_root_selection_required" in result["blockers"]
    assert result["roots"] == [".", "services/a", "services/b"]
    assert {item["root"] for item in result["candidates"]} == {"services/a", "services/b"}
    assert len({item["candidate_id"] for item in result["candidates"]}) == 2


def test_declared_mcp_script_requires_actual_discovery_and_launch_binding(inspect_fixture):
    _, run = inspect_fixture
    _, result = run({
        "pyproject.toml": "[project]\nname='fixture'\nversion='1.0.0'\ndependencies=['mcp>=1.30']\n"
                          "[project.scripts]\nserver='server:main'\n",
        "server.py": "raise RuntimeError('not a verified server')\ndef main(): pass\n",
    })
    candidate, = result["candidates"]
    assert candidate["kind"] == "python-mcp" and candidate["tools"] == []
    assert candidate["declared_entrypoint"] == "server:main"
    assert {"dynamic_mcp_discovery_required", "entrypoint_launch_binding_required", "dependency_resolution_required"} <= set(candidate["unresolved"])


def test_module_metadata_is_observed_without_guessing_tools(inspect_fixture):
    _, run = inspect_fixture
    _, result = run({"pyproject.toml": "[tool.mcpme]\nmcp_module='server'\n", "server.py": "raise RuntimeError('not executed')\n"})
    assert result["candidates"][0]["kind"] == "python-mcp"
    assert result["candidates"][0]["tools"] == []
    assert "dynamic_mcp_discovery_required" in result["candidates"][0]["unresolved"]


def test_non_mcp_project_script_is_not_assumed_to_be_mcp(inspect_fixture):
    _, run = inspect_fixture
    _, result = run({"pyproject.toml": "[project.scripts]\nserver='server:main'\n", "server.py": "def main(): pass\n"})
    assert not any(item["kind"] == "python-mcp" for item in result["candidates"])


def test_snapshot_mutation_during_parse_invalidates_inspection(inspect_fixture, monkeypatch):
    _, run = inspect_fixture
    draft, _ = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    import mcpme.preparation_inspection as inspection
    original = inspection.ast.parse
    def mutate(*args, **kwargs):
        tree = original(*args, **kwargs)
        (Path(draft["source"]["snapshot_path"]) / "worker.py").write_text("changed\n")
        return tree
    monkeypatch.setattr(inspection.ast, "parse", mutate)
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        inspect_source(draft)


def test_stale_recorded_hash_is_rejected(inspect_fixture):
    _, run = inspect_fixture
    draft, _ = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    draft["source"]["files"][0]["sha256"] = "f" * 64
    with pytest.raises(PreparationError, match="source_identity_mismatch"):
        inspect_source(draft)


def test_symlink_snapshot_is_rejected(inspect_fixture):
    source, run = inspect_fixture
    draft, _ = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    path = Path(draft["source"]["snapshot_path"]) / "worker.py"
    path.unlink()
    path.symlink_to(source / "worker.py")
    with pytest.raises(PreparationError, match="unsafe_preparation_source"):
        inspect_source(draft)


def test_ast_byte_and_node_limits_are_content_free(inspect_fixture, monkeypatch):
    _, run = inspect_fixture
    import mcpme.preparation_inspection as inspection
    monkeypatch.setattr(inspection, "MAX_AST_BYTES", 16)
    _, result = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    assert result["candidates"] == [] and "preparation_inspection_limit" in result["blockers"]
    monkeypatch.setattr(inspection, "MAX_AST_BYTES", 256 * 1024)
    monkeypatch.setattr(inspection, "MAX_AST_NODES", 2)
    _, result = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    assert result["candidates"] == [] and "preparation_inspection_limit" in result["blockers"]


def test_project_limits_and_malformed_toml_do_not_leak_content(inspect_fixture, monkeypatch):
    _, run = inspect_fixture
    import mcpme.preparation_inspection as inspection
    monkeypatch.setattr(inspection, "MAX_PROJECT_BYTES", 8)
    _, result = run({"pyproject.toml": "SECRET CONTENT = [\n", "worker.py": "pass\n"})
    assert result["observations"][0]["blockers"] == ["preparation_inspection_limit"]
    assert "SECRET CONTENT" not in json.dumps(result)


def test_candidate_identity_binds_exact_source_bytes(inspect_fixture):
    _, run = inspect_fixture
    draft, first = run({"worker.py": "def calculate(value: str) -> dict:\n    return {}\n"})
    _, second = run({"worker.py": "def calculate(value: str) -> dict:\n    return {'changed': True}\n"})
    assert first["candidates"][0]["candidate_id"] != second["candidates"][0]["candidate_id"]
    evidence = first["candidates"][0]["evidence"][0]
    content = (Path(draft["source"]["snapshot_path"]) / evidence["path"]).read_bytes()
    assert evidence["sha256"] == hashlib.sha256(content).hexdigest()
