"""Actual captured bytes and static proposal boundaries; no dependency run."""
import json
import tomllib
import uuid

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_agent_contracts import AgentProposalV1
from mcpme.preparation_agent_adapter_store import original_snapshot, CODE_PATH
from mcpme.preparation_agent_unlocked_contracts import (DEPENDENCY_NAMESPACE,
    canonical_dependency_project, checked_requirements, specification_from_proposal)
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_resolution_runner import MCP_VERSION, PYTHON_VERSION
from test_preparation_agent_adapter import TOOL, CODE, MODULE


def proposal(*, requirements=(), change=None):
    tasks = [
        {"role": "interface", "files": [{"path": "proposed/interface/interface.json", "content": json.dumps({"tool": TOOL})}], "unresolved": []},
        {"role": "dependencies", "files": [{"path": "proposed/dependencies/dependencies.json", "content": json.dumps({"requirements": list(requirements)})}], "unresolved": []},
        {"role": "fixtures", "files": [{"path": "proposed/fixtures/fixtures.json", "content": json.dumps({"arguments": {"n": 3}, "expected": {"value": 6}})}], "unresolved": []},
        {"role": "adapter", "files": [
            {"path": "proposed/adapter/adapter.json", "content": json.dumps({"schema_version": 1, "module": MODULE, "function": "invoke", "source_license": "LICENSE"})},
            {"path": "proposed/adapter/wrapper.py", "content": CODE},
            {"path": "proposed/adapter/LICENSE", "content": "Synthetic wrapper permission.\n"}], "unresolved": []},
    ]
    if change:
        change(tasks)
    return AgentProposalV1(request_digest="a"*64, attempt_id=str(uuid.uuid4()), generation=str(uuid.uuid4()), tasks=tasks)


def captured(tmp_path, *, files=None):
    source = tmp_path / "source"; source.mkdir()
    (source / "worker.py").write_text("raise RuntimeError('selected source must not execute')\ndef double(n): return n*2\n")
    (source / "LICENSE").write_text("Synthetic original permission.\n")
    (source / "README.md").write_text("Unfamiliar unlocked scalar application.\n")
    for name, content in (files or {}).items():
        if isinstance(content,bytes):
            (source/name).write_bytes(content)
        else:
            (source/name).write_text(content)
    return source


def test_unlocked_proposal_needs_no_original_project_lock_or_source_execution(tmp_path):
    source = captured(tmp_path)
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic")
        snapshot, baseline = original_snapshot(draft)
        selected, content = specification_from_proposal(proposal(requirements=["humanize==4.13.0"]), snapshot)
        generated = tomllib.loads(canonical_dependency_project(selected, snapshot).decode())
        assert generated["project"]["dependencies"] == ["humanize==4.13.0", "mcp==" + MCP_VERSION]
        assert generated["project"]["requires-python"] == "==" + PYTHON_VERSION
        assert set(content) == {"LICENSE", "adapter.json", CODE_PATH}
        assert original_snapshot(draft)[1] == baseline and not imports.inspect(draft["draft_id"])["attachment"]
        assert not (source / "pyproject.toml").exists() and not (source / "uv.lock").exists()


@pytest.mark.parametrize("requirement", [
    "humanize @ https://example.invalid/file.whl", "humanize @ file:///private/tmp/example.whl",
    "humanize @ git+https://example.invalid/repo", "./untrusted", "--index-url=https://example.invalid",
    "humanize --extra-index-url https://example.invalid", "", "x"*4097,
])
def test_requirement_proposal_cannot_select_external_authority(requirement):
    with pytest.raises(PreparationError, match="requirements_invalid"):
        checked_requirements([requirement])


@pytest.mark.parametrize("requirements", [["humanize", "Humanize>=1"], ["mcp"], ["mcp=="+MCP_VERSION], ["mcp[cli]=="+MCP_VERSION]])
def test_requirement_duplicates_and_host_sdk_proposals_are_closed(requirements):
    with pytest.raises(PreparationError):
        checked_requirements(requirements)


def test_static_original_requirements_are_preserved_and_hooks_excluded(tmp_path):
    source = captured(tmp_path, files={"pyproject.toml": "[project]\nname='original'\nversion='1.0.0'\ndependencies=['humanize==4.13.0']\n[build-system]\nbuild-backend='untrusted.hook'\nrequires=['untrusted-hook']\n"})
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic")
        snapshot, _ = original_snapshot(draft)
        selected, _ = specification_from_proposal(proposal(), snapshot)
        generated = tomllib.loads(canonical_dependency_project(selected, snapshot).decode())
        assert set(generated) == {"project"}
        assert generated["project"]["dependencies"] == ["humanize==4.13.0", "mcp=="+MCP_VERSION]
        changed = selected.model_copy(update={"requirements": ("humanize==4.12.0",)})
        with pytest.raises(PreparationError, match="original_requirement_changed"):
            canonical_dependency_project(changed, snapshot)


@pytest.mark.parametrize("metadata", [
    "[project]\ndynamic=['dependencies']\n", "[tool.uv]\nindex-url='https://example.invalid'\n",
    "[dependency-groups]\ndevelopment=['humanize']\n", "[project]\nrequires-python='>=99'\n",
])
def test_unsupported_original_dependency_metadata_is_a_blocker(tmp_path, metadata):
    source = captured(tmp_path, files={"pyproject.toml": metadata})
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic")
        snapshot, _ = original_snapshot(draft)
        with pytest.raises(PreparationError):
            specification_from_proposal(proposal(), snapshot)


@pytest.mark.parametrize("name", [CODE_PATH, DEPENDENCY_NAMESPACE])
def test_original_reserved_namespace_collision_is_rejected(tmp_path, name):
    source = captured(tmp_path, files={name: "reserved collision\n"})
    with ImportStore(tmp_path / "state") as imports:
        snapshot, _ = original_snapshot(imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic"))
        with pytest.raises(PreparationError, match="namespace_collision"):
            specification_from_proposal(proposal(), snapshot)


def test_proposal_extra_dependency_file_is_not_ignored(tmp_path):
    source = captured(tmp_path)
    with ImportStore(tmp_path / "state") as imports:
        snapshot, _ = original_snapshot(imports.create(source, name="Unlocked", intent="Wrap the scalar", source_mode="automatic"))
        def change(tasks):
            tasks[1]["files"].append({"path": "proposed/dependencies/uv.toml", "content": "untrusted index"})
        with pytest.raises(PreparationError, match="proposal_invalid"):
            specification_from_proposal(proposal(change=change), snapshot)
