"""Actual cold inventory and trusted recipe lifecycle; no source execution."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from meecp.preparation_agent_contracts import AnalysisGuideV1
from meecp.preparation_agent_guides import AnalysisGuideRegistry, builtin_analysis_guides
from meecp.preparation_agent_recipe_choices import AgentRecipeChoiceRegistry, builtin_recipe_choice_rules
from meecp.preparation_contracts import PreparationError, canonical_bytes
from meecp.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[1]
LOCKED = "mcpme.agent-python-adapter"
UNLOCKED = "mcpme.agent-unlocked-python-adapter"
WORKER = b"raise RuntimeError('source was executed')\ndef double(n: int) -> int: return n*2\n"
PROJECT = b"[project]\nname='synthetic'\nversion='1.0.0'\n"
LOCK = b"version=1\nrevision=3\n"


def capture(host, root, files):
    root.mkdir()
    for name, data in {"README.md": b"Nonsecret source fixture.\n", "LICENSE": b"Synthetic permission notice.\n",
                       "payload.bin": b"\x00\xffopaque asset", **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    draft = host.imports.create(root, name="Synthetic app", intent="Expose a typed callable", source_mode="automatic")
    assert draft["source"]["kind"] == "application_directory"
    return draft


def options(host, draft):
    return host.agent_preparations.options(draft["draft_id"], draft["revision"])


def recommended(value):
    return [item["recipe"]["id"] for item in value["recipe_choices"] if item["recommended"]]


def add_alias(host, root, recipe_id, version="1.0.2"):
    shutil.copytree(ROOT / "templates/builtin-recipes/agent-unlocked-python-adapter-v1-0-2", root)
    manifest = json.loads((root / "meecp.recipe.json").read_bytes())
    manifest.update(id=recipe_id, version=version)
    raw = canonical_bytes(manifest)
    lock = json.loads((root / "meecp.recipe.lock.json").read_bytes())
    lock["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    (root / "meecp.recipe.json").write_bytes(raw)
    (root / "meecp.recipe.lock.json").write_bytes(canonical_bytes(lock))
    record = host.recipes.add(root)
    return host.recipes.enable(record["recipe_id"], record["version"])


@pytest.mark.parametrize("files, expected", [
    ({"worker.py": WORKER}, UNLOCKED),
    ({"worker.py": WORKER, "pyproject.toml": PROJECT, "uv.lock": LOCK}, LOCKED),
    ({"app/worker.py": WORKER, "app/pyproject.toml": PROJECT, "app/uv.lock": LOCK}, LOCKED),
    ({"worker.py": WORKER, "pyproject.toml": PROJECT}, UNLOCKED),
    ({"one/worker.py": WORKER, "one/pyproject.toml": PROJECT, "one/uv.lock": LOCK,
      "two/worker.py": WORKER, "two/pyproject.toml": PROJECT, "two/uv.lock": LOCK}, None),
    ({"worker.py": b"\x00\xffbinary Python name"}, None),
    ({"worker.py": b"#" + b"x" * 16384}, None),
    ({"worker.js": b"export function double(n) {return n*2;}\n"}, None),
    ({"worker.py": WORKER, "pyproject.toml": PROJECT, "uv.lock": b"\x00binary lock"}, None),
    ({"worker.py": WORKER, "tools/pyproject.toml": PROJECT, "tools/uv.lock": LOCK}, None),
])
async def test_cold_metadata_recommends_only_an_exact_supported_path(tmp_path, files, expected):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", files)
        value = options(host, draft)
        assert recommended(value) == ([] if expected is None else [expected])
        assert value["recipes_truncated"] is False
        assert [item["recipe"] for item in value["recipe_choices"]] == value["recipes"]
        assert "payload.bin" not in {item["path"] for item in value["files"]}
        assert "payload.bin" in {item["path"] for item in value["metadata_files"]}
        assert all(item["compatibility"] == "compatible" for item in value["recipe_choices"] if item["recommended"])
        assert host.agent_preparations.tasks == {} and host.preparations.list() == []
        assert host.preparation_runner.enabled is False and host.agent_preparations.live_enabled is False
    finally:
        await host.close()


async def test_options_never_read_source_recipe_bytes_launch_or_resolve_credentials(tmp_path, monkeypatch):
    import httpx
    from meecp.preparation_agent_backend import OpenAIResponsesBackend
    from meecp.preparation_agent_host import AgentProviderConfiguration
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        def forbidden(*args, **kwargs):
            raise AssertionError("metadata inventory attempted external work")
        configured = AgentProviderConfiguration("synthetic-discovery", OpenAIResponsesBackend(forbidden,
            transport=httpx.MockTransport(forbidden)), ("explicit-synthetic-model",), "synthetic-reference")
        host.agent_preparations.providers[configured.provider_id] = configured
        with monkeypatch.context() as blocked:
            for method in ("read_bytes", "read_text", "open"):
                blocked.setattr(Path, method, forbidden)
            blocked.setattr(subprocess, "Popen", forbidden)
            blocked.setattr(host.recipes, "resolve", forbidden)
            blocked.setattr("meecp.preparation_agent_host._read_file", forbidden)
            value = options(host, draft)
            assert recommended(value) == [UNLOCKED]
            assert value["providers"][0]["provider_id"] == configured.provider_id
            assert value["providers"][0]["execution_available"] is False
        assert not host.agent_preparations.tasks
    finally:
        await host.close()


@pytest.mark.parametrize("action", ["disable", "revoke", "remove", "deprecate"])
async def test_unavailable_recipe_is_never_selected_or_reenabled(tmp_path, action):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        getattr(host.recipes, action)(UNLOCKED, "1.0.2")
        value = options(host, draft)
        assert recommended(value) == []
        assert UNLOCKED not in {item["id"] for item in value["recipes"]}
        assert host.recipes.inspect(UNLOCKED, "1.0.2")["state"] != "enabled"
    finally:
        await host.close()


async def test_component_or_guide_change_cannot_inherit_a_recommendation(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        guides = [guide.model_dump() for guide in builtin_analysis_guides()]
        guides[1]["roles"][0]["instructions"] += " A different trusted protocol."
        host.agent_preparations.guides = AnalysisGuideRegistry(tuple(AnalysisGuideV1.model_validate(item) for item in guides))
        assert recommended(options(host, draft)) == []
        host.agent_preparations.guides = AnalysisGuideRegistry(builtin_analysis_guides())
        host.recipes.component_catalog["agent-unlocked-python-adapter-v1@1.0.0"]["digest"] = "0" * 64
        value = options(host, draft)
        assert recommended(value) == []
        assert next(item for item in value["recipe_choices"] if item["recipe"]["id"] == UNLOCKED)["compatibility"] == "unresolved"
    finally:
        await host.close()


async def test_independent_exact_recipe_alias_can_be_chosen_without_dispatcher_changes(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        add_alias(host, tmp_path / "alias-a", "test.independent-adapter")
        assert recommended(options(host, draft)) == [UNLOCKED]
        host.recipes.disable(UNLOCKED, "1.0.2")
        assert recommended(options(host, draft)) == ["test.independent-adapter"]
        add_alias(host, tmp_path / "alias-b", "test.second-adapter")
        assert recommended(options(host, draft)) == []
    finally:
        await host.close()


async def test_multiple_builtin_versions_require_explicit_selection(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        add_alias(host, tmp_path / "alias", UNLOCKED, "1.0.3")
        assert recommended(options(host, draft)) == []
    finally:
        await host.close()


async def test_overlapping_trusted_guide_rules_require_an_explicit_choice(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        locked, unlocked = builtin_recipe_choice_rules()
        overlapping = type(locked).model_validate({**locked.model_dump(), "pair_requirement": "absent"})
        host.agent_preparations.recipe_choices = AgentRecipeChoiceRegistry((overlapping, unlocked))
        value = options(host, draft)
        assert {item["recipe"]["id"] for item in value["recipe_choices"] if item["compatibility"] == "compatible"} == {LOCKED, UNLOCKED}
        assert recommended(value) == []
    finally:
        await host.close()


async def test_real_inventory_over_limit_is_bounded_and_cannot_default(tmp_path):
    from test_meecp_preparations import recipe_bundle
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft = capture(host, tmp_path / "source", {"worker.py": WORKER})
        for index in range(128):
            record = host.recipes.add(recipe_bundle(tmp_path / f"extra-{index}", recipe_id=f"test.choice-{index:03}"))
            host.recipes.enable(record["recipe_id"], record["version"])
        value = options(host, draft)
        assert value["recipes_truncated"] is True and len(value["recipes"]) == len(value["recipe_choices"]) == 128
        assert recommended(value) == []
        assert [item["recipe"] for item in value["recipe_choices"]] == value["recipes"]
    finally:
        await host.close()


@pytest.mark.parametrize("hidden", ["python", "lock"])
async def test_a_recommendation_never_depends_on_undisplayed_source_records(tmp_path, hidden):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        files = {f"a/{index:03}.txt": b"Synthetic text.\n" for index in range(256)}
        files.update({"z/worker.py": WORKER} if hidden == "python" else {
            "a/000-worker.py": WORKER, "pyproject.toml": PROJECT, "uv.lock": LOCK})
        draft = capture(host, tmp_path / "source", files)
        value = options(host, draft)
        assert value["files_truncated"] and value["metadata_files_truncated"]
        if hidden == "lock":
            assert "a/000-worker.py" in {item["path"] for item in value["files"]}
            assert not {"pyproject.toml", "uv.lock"} & {item["path"] for item in value["metadata_files"]}
        assert recommended(value) == []
    finally:
        await host.close()


def test_rules_are_closed_bounded_and_registered_against_exact_guides():
    rule = builtin_recipe_choice_rules()[0]
    with pytest.raises(ValueError):
        type(rule).model_validate({**rule.model_dump(), "source_reader": "arbitrary"})
    with pytest.raises(PreparationError, match="agent_recipe_choice_registration_invalid"):
        AgentRecipeChoiceRegistry((rule, copy.deepcopy(rule)))
