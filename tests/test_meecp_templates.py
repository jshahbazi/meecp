"""Authoring binds selected source identity and preserves callable MCP contracts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

from meecp.contracts import validate_package

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("meecp_template_prepare", ROOT / "templates/prepare.py")
preparation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparation)
TOOL = json.loads((ROOT / "templates/python-function-v1/tool.example.json").read_text())


def fixture_export(tmp_path: Path):
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "arithmetic.py").write_text("def add_numbers(left, right):\n    return {'total': left + right}\n")
    (source / "LICENSE").write_text("Synthetic fixture license\n")
    records = [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size} for path in sorted(source.iterdir())]
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    export = {"api_version": 1, "draft_id": "synthetic-draft", "revision": 3,
              "source": {"kind": "local_folder", "revision": "sha256:" + digest, "digest": digest,
                         "snapshot_path": str(source), "files": records},
              "requirements": {"tools": ["add_numbers"], "profile": "mcp-stdio",
                               "memory_mb": 128, "secret_references": []}}
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps(export))
    tool = tmp_path / "tool.json"
    tool.write_text(json.dumps(TOOL))
    return export, exported, tool


def stub_runtime(destination, dependency_project):
    """Static preparation tests do not copy/download an entire interpreter."""
    (destination / "python/bin").mkdir(parents=True)
    executable = destination / "python/bin/python3.12"
    executable.write_text("synthetic runtime artifact; never executed\n")
    executable.chmod(0o700)
    (destination / "licenses/python-LICENSE.txt").write_text("Synthetic Python license\n")
    (destination / "licenses/DEPENDENCIES.md").write_text("Synthetic dependency notice\n")
    (destination / "uv.lock").write_text("synthetic locked runtime\n")
    return "CPython synthetic static fixture"


def arguments(exported, tool, output):
    return preparation.parser().parse_args(["python-function", "--setup-export", str(exported),
        "--tool-spec", str(tool), "--module", "arithmetic", "--function", "add_numbers",
        "--package-id", "local.arithmetic", "--name", "Arithmetic", "--source-license", "LICENSE",
        "--output", str(output)])


def test_generator_binds_export_and_seals_exact_artifacts(tmp_path, monkeypatch):
    export, exported, tool = fixture_export(tmp_path)
    monkeypatch.setattr(preparation, "prepare_runtime", stub_runtime)
    package = preparation.prepare(arguments(exported, tool, tmp_path / "package"))
    assert package.manifest.source_revision == export["source"]["revision"]
    assert package.manifest.tools[0].mcp_schema()["inputSchema"] == TOOL["input_schema"]
    receipt = json.loads((package.root / "preparation.json").read_text())
    assert receipt["draft_id"] == "synthetic-draft"
    assert receipt["revision"] == 3
    assert receipt["template"] == "python-function-v1"
    assert "snapshot_path" not in receipt
    assert "-I -S" in (package.root / "bin/service").read_text()
    assert "PYTHONPATH" not in (package.root / "bin/service").read_text()
    original = Path(export["source"]["snapshot_path"]) / "arithmetic.py"
    original.write_text("raise RuntimeError('changed original')\n")
    assert validate_package(package.root).digest == package.digest
    assert not list(package.root.rglob("*.pyc"))


@pytest.mark.parametrize("change", ["content", "identity", "unselected", "symlink", "traversal"])
def test_source_snapshot_rejects_changed_or_unsafe_inputs(tmp_path, change):
    export, _, _ = fixture_export(tmp_path)
    snapshot = Path(export["source"]["snapshot_path"])
    if change == "content":
        (snapshot / "arithmetic.py").write_text("changed\n")
    elif change == "identity":
        export["source"]["revision"] = "sha256:" + "b" * 64
    elif change == "unselected":
        (snapshot / "unselected.py").write_text("private unrelated contents\n")
        preparation.source_snapshot(export, tmp_path / "copied")
        assert not (tmp_path / "copied/unselected.py").exists()
        return
    elif change == "symlink":
        source = snapshot / "arithmetic.py"
        source.unlink()
        source.symlink_to(snapshot / "LICENSE")
    else:
        export["source"]["files"][0]["path"] = "../LICENSE"
    with pytest.raises((preparation.PreparationError, OSError, ValueError)):
        preparation.source_snapshot(export, tmp_path / "copied")


def test_existing_native_generator_does_not_launch_server(tmp_path, monkeypatch):
    export, exported, tool = fixture_export(tmp_path)
    args = preparation.parser().parse_args(["existing-mcp", "--setup-export", str(exported),
        "--tool-spec", str(tool), "--executable", "arithmetic.py", "--source-license", "LICENSE",
        "--package-id", "local.existing", "--name", "Existing", "--output", str(tmp_path / "package")])
    def forbid(*args, **kwargs):
        pytest.fail("native packaging must not execute selected source")
    monkeypatch.setattr(preparation.subprocess, "run", forbid)
    package = preparation.prepare(args)
    assert package.manifest.entrypoint.executable == "source/arithmetic.py"
    assert package.manifest.source_revision == export["source"]["revision"]


@pytest.mark.asyncio
async def test_callable_adapter_real_sdk_schemas_results_and_sanitized_failures(tmp_path):
    shutil.copyfile(ROOT / "templates/python-function-v1/adapter.py", tmp_path / "adapter.py")
    (tmp_path / "callable.json").write_text(json.dumps({"module": "arithmetic", "function": "add_numbers", "tool": TOOL}))
    (tmp_path / "arithmetic.py").write_text(
        "import os,sys\nprint('private source marker', flush=True)\n"
        "os.write(2, b'private stderr marker')\n"
        "def add_numbers(left, right):\n"
        "    print('private request marker', flush=True)\n"
        "    if left == 99: raise ValueError('private exception marker')\n"
        "    if left == 98: return {'private': 'invalid result marker'}\n"
        "    return {'total': left + right}\n")
    parameters = StdioServerParameters(command=sys.executable, args=[str(tmp_path / "adapter.py")],
                                       env={"PYTHONPATH": str(tmp_path)})
    with (tmp_path / "stderr.txt").open("w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                assert set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}
                listed = await client.list_tools()
                assert listed.tools[0].inputSchema == TOOL["input_schema"]
                assert listed.tools[0].outputSchema == TOOL["output_schema"]
                result = await client.call_tool("add_numbers", {"left": 2, "right": 3})
                assert result.structuredContent == {"total": 5}
                assert not result.isError
                for inputs, expected in [({"left": "bad", "right": 1}, "invalid_tool_arguments"),
                                         ({"left": 99, "right": 1}, "tool_execution_failed"),
                                         ({"left": 98, "right": 1}, "invalid_tool_result")]:
                    error = await client.call_tool("add_numbers", inputs)
                    assert error.isError
                    assert [block.text for block in error.content] == [expected]
    assert (tmp_path / "stderr.txt").read_text() == ""
