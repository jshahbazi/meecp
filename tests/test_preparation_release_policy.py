"""Distribution policy is inert until an exact trusted artifact admits it."""
import copy
import asyncio
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from meecp.preparation_contracts import PreparationError
from meecp.preparation_release_policy import AgentExecutionAdmission, ReleasePolicy, POLICY_PATH, _load_policy, release_admissions
from meecp.preparation_agent_providers import AgentProviderRegistry, OpenAIResponsesAdapter
from test_preparation_agent_providers import choices, update, SYNTHETIC


KEY = ("agent-unlocked-python-adapter-v1", "1.0.0", "3850d78dc24ab286cd4bb40825e0d52a83869c91c4278c525cfd6462ee822b49")


def closed():
    return {"schema_version": 1, "policy_id": "closed", "preparation": {"families": [],
        "recipe_component_sets": [], "resolution_candidate_kinds": []}, "agent": {
        "components": [], "providers": [], "unlocked_resolution": False}, "command": None}


def write_policy(root, value):
    path = root / POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def admitted():
    adapter = OpenAIResponsesAdapter()
    return AgentExecutionAdmission(frozenset({KEY}), frozenset({(adapter.adapter_id, adapter.digest, "accepted-model")}), True)


def test_shipped_policy_closed_and_environment_cannot_activate(monkeypatch):
    monkeypatch.setenv("MCPME_RELEASE_POLICY", "/arbitrary/policy")
    monkeypatch.setenv("MCPME_AGENT_EXECUTION", "true")
    assert release_admissions() == (None, None, (None, None))


def test_shipped_agent_policy_tuples_match_registered_implementations():
    from meecp.recipes import builtin_components
    from meecp.preparation_agent_guides import builtin_analysis_guides

    root = Path(__file__).parents[1]
    policy = ReleasePolicy.model_validate(json.loads((root / POLICY_PATH).read_bytes()))
    preparation, agent, _ = policy.admissions()
    catalog = builtin_components()
    assert agent.components <= preparation.families
    for identifier, version, digest in agent.components:
        assert catalog[identifier + "@" + version]["digest"] == digest
    assert agent.components <= frozenset(
        (guide.component.id, guide.component.version, guide.component.digest)
        for guide in builtin_analysis_guides())


def test_whole_external_policy_changes_reviewed_producer_identity():
    value = closed(); value["preparation"]["resolution_candidate_kinds"] = ["python-callable"]
    original = ReleasePolicy.model_validate(value).admissions()[0]
    value["policy_id"] = "separately-reviewed-acceptance"
    changed = ReleasePolicy.model_validate(value).admissions()[0]
    assert original.families == changed.families and original.resolution_candidate_kinds == changed.resolution_candidate_kinds
    assert original.digest != changed.digest and original.release_policy_digest != changed.release_policy_digest


@pytest.mark.parametrize("damage", ["unknown", "boolean_schema", "integer_flag", "missing_command", "duplicate_kinds",
    "unknown_kind", "providers_without_components", "unlocked_without_components", "overlarge"])
def test_policy_rejects_unbounded_or_ambiguous_authority(tmp_path, damage):
    value = closed()
    if damage == "unknown": value["path"] = "/arbitrary"
    elif damage == "boolean_schema": value["schema_version"] = True
    elif damage == "integer_flag": value["agent"]["unlocked_resolution"] = 1
    elif damage == "missing_command": del value["command"]
    elif damage == "duplicate_kinds": value["preparation"]["resolution_candidate_kinds"] = ["python-callable"] * 2
    elif damage == "unknown_kind": value["preparation"]["resolution_candidate_kinds"] = ["agent-adapter"]
    elif damage == "providers_without_components": value["agent"]["providers"] = [{"adapter_id": "openai-responses-v1", "adapter_digest": "a"*64, "models": ["model"]}]
    elif damage == "unlocked_without_components": value["agent"]["unlocked_resolution"] = True
    else: value["policy_id"] = "x" * 65536
    write_policy(tmp_path, value)
    with pytest.raises(PreparationError): _load_policy(tmp_path)


@pytest.mark.parametrize("damage", ["symlink", "fifo", "hardlink", "ancestor"])
def test_fixed_policy_requires_safe_exact_file(tmp_path, damage):
    path = write_policy(tmp_path, closed())
    if damage == "symlink":
        path.rename(tmp_path / "held"); path.symlink_to(tmp_path / "held")
    elif damage == "fifo": path.unlink(); os.mkfifo(path)
    elif damage == "hardlink": os.link(path, tmp_path / "alias")
    else:
        parent = path.parent; parent.rename(tmp_path / "held"); parent.symlink_to(tmp_path / "held")
    with pytest.raises(PreparationError): _load_policy(tmp_path)


def test_active_policy_cannot_authorize_itself_with_source_config(tmp_path):
    value = closed(); value["preparation"]["resolution_candidate_kinds"] = ["python-callable"]
    path = write_policy(tmp_path, value)
    with pytest.raises(PreparationError): _load_policy(tmp_path)
    # Even plausible host metadata must still pass the distribution boundary.
    (tmp_path / "build.json").write_text(json.dumps({"source_commit": "a"*40, "source_tree_dirty": False,
        "artifact_sha256": {POLICY_PATH: hashlib.sha256(path.read_bytes()).hexdigest()}}))
    with pytest.raises(PreparationError): _load_policy(tmp_path)


@pytest.mark.parametrize("damage", [None, "missing_dependency", "wrong_component", "missing_agent_family", "missing_agent_closure", "wrong_adapter", "changed_policy"])
def test_accepted_artifact_still_requires_exact_registered_closures(tmp_path, monkeypatch, damage):
    from meecp import recipes, preparation_release_artifact
    adapter = OpenAIResponsesAdapter()
    ref = dict(zip(("id", "version", "digest"), KEY))
    value = closed()
    value["preparation"].update(families=[ref], recipe_component_sets=[[ref]])
    value["agent"].update(components=[ref], unlocked_resolution=True, providers=[{
        "adapter_id": adapter.adapter_id, "adapter_digest": adapter.digest, "models": ["accepted-model"]}])
    catalog = {KEY[0] + "@" + KEY[1]: {"digest": KEY[2], "dependencies": []}}
    if damage == "missing_dependency": catalog[next(iter(catalog))]["dependencies"] = [{"component": "missing", "version": "1.0.0", "digest": "a"*64}]
    elif damage == "wrong_component": catalog[next(iter(catalog))]["digest"] = "0"*64
    elif damage == "missing_agent_family": value["preparation"]["families"] = []
    elif damage == "missing_agent_closure": value["preparation"]["recipe_component_sets"] = []
    elif damage == "wrong_adapter": value["agent"]["providers"][0]["adapter_digest"] = "0"*64
    path = write_policy(tmp_path, value)
    (tmp_path / "build.json").write_text(json.dumps({"source_commit": "a"*40, "source_tree_dirty": False,
        "artifact_sha256": {POLICY_PATH: hashlib.sha256(path.read_bytes()).hexdigest()}}))
    monkeypatch.setattr(preparation_release_artifact, "verify_release_artifact", lambda root, path, data:
        preparation_release_artifact.ReleaseArtifactProof((), frozenset(), root/path, data))
    def components():
        if damage == "changed_policy": path.write_text(json.dumps(closed()))
        return catalog
    monkeypatch.setattr(recipes, "builtin_components", components)
    if damage:
        with pytest.raises(PreparationError): _load_policy(tmp_path)
    else:
        public, agent, command = _load_policy(tmp_path)
        assert public.families == agent.components == frozenset({KEY})
        assert agent.permits(adapter.adapter_id, adapter.digest, "accepted-model") and not agent.permits(adapter.adapter_id, adapter.digest, "other-model")
        assert command == (None, None) and len(agent.digest) == 64


@pytest.mark.parametrize("damage", ["mutable", "unknown_bool", "bad_digest", "wrong_component"])
def test_agent_admission_is_typed_and_closed(damage):
    values = {"components": frozenset({KEY}), "providers": frozenset(), "unlocked_resolution": True}
    if damage == "mutable": values["components"] = set(values["components"])
    elif damage == "unknown_bool": values["unlocked_resolution"] = 1
    elif damage == "bad_digest": values["providers"] = frozenset({("openai-responses-v1", "bad", "model")})
    else: values["components"] = frozenset({("other-component", "1.0.0", "a"*64)})
    with pytest.raises(PreparationError): AgentExecutionAdmission(**values)


def test_provider_configuration_does_not_grant_model_or_adapter_authority(tmp_path):
    admission = admitted()
    registry = AgentProviderRegistry(tmp_path / "providers", execution_admission=admission)
    row = registry.create(choices(models=["unaccepted-model"]))["provider"]
    assert not row["execution_available"]
    changed = registry.update(row["provider_id"], update(row, models=["accepted-model", "unaccepted-model"]))["provider"]
    assert changed["execution_available"]
    assert registry.list()["live_execution_enabled"]
    assert SYNTHETIC not in json.dumps(registry.list())
    reopened = AgentProviderRegistry(registry.root, execution_admission=admission)
    assert reopened.list()["providers"] == [changed]
    closed_registry = AgentProviderRegistry(registry.root)
    assert not closed_registry.list()["providers"][0]["execution_available"]
    assert not closed_registry.list()["live_execution_enabled"]
    removed = reopened.remove(row["provider_id"], {"schema_version": 1, "expected_generation": changed["generation"]})["provider"]
    assert not removed["execution_available"]


def test_revoked_credential_and_changed_adapter_stop_advertised_execution(tmp_path):
    registry = AgentProviderRegistry(tmp_path / "providers", execution_admission=admitted())
    row = registry.create(choices(models=["accepted-model"]))["provider"]
    registry.secrets.revoke(registry._read().providers[0].credential_references["api_key"])
    assert not registry.list()["providers"][0]["execution_available"]
    other = AgentExecutionAdmission(frozenset({KEY}), frozenset({("openai-responses-v1", "a"*64, "accepted-model")}))
    reopened = AgentProviderRegistry(registry.root, execution_admission=other)
    assert not reopened.list()["providers"][0]["execution_available"]


async def test_canceled_startup_joins_held_artifact_inspection(monkeypatch):
    from meecp import preparation_release_policy as module
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def held():
        entered.set()
        try: assert release.wait(3)
        finally: exited.set()
        return None, None, (None, None)
    monkeypatch.setattr(module, "release_admissions", held)
    task = asyncio.create_task(module.load_release_admissions())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel(); await asyncio.sleep(.01); task.cancel(); await asyncio.sleep(.01)
        assert not task.done() and not exited.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert exited.is_set() and task.cancelled()
