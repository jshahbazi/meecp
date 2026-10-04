"""An unaccepted command platform cannot disable independent families."""
import hashlib
import json

import pytest

from mcpme import preparation_release_artifact as artifact_module
from mcpme import preparation_release_command_admission as command_module
from mcpme import preparation_release_policy as policy_module
from mcpme import recipes
from mcpme.preparation_contracts import PreparationError, contract_digest
from test_preparation_release_policy import KEY, closed, write_policy


@pytest.fixture
def composed_policy(tmp_path, monkeypatch):
    command_key = command_module.COMMAND_COMPONENT
    reference = lambda key: dict(zip(("id", "version", "digest"), key))
    value = closed()
    value["preparation"].update(families=[reference(KEY), reference(command_key)],
        recipe_component_sets=[[reference(KEY)], [reference(command_key)], [reference(KEY), reference(command_key)]],
        resolution_candidate_kinds=["python-callable", "python-mcp"])
    value["agent"].update(components=[reference(KEY)], unlocked_resolution=True)
    value["command"] = {"component": list(command_key),
        "installed_platform_policy": command_module.INSTALLED_PLATFORM_POLICY,
        "resource_platform_policy": command_module.RESOURCE_PLATFORM_POLICY}
    path = write_policy(tmp_path, value)
    data = path.read_bytes()
    build = {"source_commit": "a" * 40, "source_tree_dirty": False,
        "artifact_sha256": {policy_module.POLICY_PATH: hashlib.sha256(data).hexdigest()}}
    build_data = json.dumps(build).encode()
    (tmp_path / "build.json").write_bytes(build_data)
    proof = artifact_module.ReleaseArtifactProof((), frozenset(), path, data,
        hashlib.sha256(build_data).hexdigest())
    monkeypatch.setattr(artifact_module, "verify_release_artifact", lambda *args: proof)
    monkeypatch.setattr(recipes, "builtin_components", lambda: {
        key[0] + "@" + key[1]: {"digest": key[2], "dependencies": []} for key in (KEY, command_key)})
    return tmp_path, value, proof


def test_unaccepted_command_platform_retains_independent_families_and_full_identity(composed_policy, monkeypatch):
    root, value, proof = composed_policy
    calls = []
    def unavailable(command, host_root, *, artifact):
        calls.append((command, host_root, artifact))
        raise PreparationError("command_release_platform_unavailable")
    monkeypatch.setattr(command_module, "command_release_admission", unavailable)
    public, agent, commands = policy_module._load_policy(root)
    assert len(calls) == 1 and calls[0][1] == root and calls[0][2] is proof
    assert public.families == frozenset({KEY})
    assert public.recipe_component_sets == frozenset({frozenset({KEY})})
    assert public.resolution_candidate_kinds == frozenset({"python-callable", "python-mcp"})
    assert commands == (None, None)
    assert agent.components == frozenset({KEY}) and agent.unlocked_resolution
    assert public.artifact is agent.artifact is proof
    expected = contract_digest("release-policy-authority-v1", value)
    assert public.release_policy_digest == agent.release_policy_digest == expected
    original, _, _ = policy_module.ReleasePolicy.model_validate(value).admissions()
    assert original.digest != public.digest
    assert public.digest != policy_module.replace(public, artifact=None).digest


@pytest.mark.parametrize("code", ["command_release_not_admitted", "invalid_release_artifact",
    "command_platform_unsupported", "command_release_platform_unavailable_extra"])
def test_other_command_errors_never_downgrade_release(composed_policy, monkeypatch, code):
    root, _, _ = composed_policy
    def denied(*args, **kwargs):
        raise PreparationError(code)
    monkeypatch.setattr(command_module, "command_release_admission", denied)
    with pytest.raises(PreparationError, match="^invalid_release_policy$"):
        policy_module._load_policy(root)


def test_platform_unavailable_does_not_recapture_replaced_policy(composed_policy, monkeypatch):
    root, _, proof = composed_policy
    def unavailable(*args, **kwargs):
        proof.policy.write_bytes(proof.policy_bytes + b" ")
        raise PreparationError("command_release_platform_unavailable")
    monkeypatch.setattr(command_module, "command_release_admission", unavailable)
    with pytest.raises(PreparationError, match="^invalid_release_policy$"):
        policy_module._load_policy(root)


def test_accepted_command_platform_keeps_exact_family_and_closures(composed_policy, monkeypatch):
    root, value, proof = composed_policy
    pair = (object(), object())
    monkeypatch.setattr(command_module, "command_release_admission", lambda *args, **kwargs: pair)
    public, agent, commands = policy_module._load_policy(root)
    original, _, _ = policy_module.ReleasePolicy.model_validate(value).admissions()
    assert public.families == original.families
    assert public.recipe_component_sets == original.recipe_component_sets
    assert public.artifact is agent.artifact is proof
    assert commands is pair

# Uses the peer's constructor fixture with explicitly synthetic signatures.
# Both production loader and production command constructor execute below.
from test_preparation_release_command_admission import packaged


@pytest.mark.parametrize("outcome", ["accepted", "unsupported", "changed-helper", "signature-denied"])
def test_real_command_constructor_and_loader_isolate_only_platform(packaged, monkeypatch, outcome):
    command_key = command_module.COMMAND_COMPONENT
    reference = lambda key: dict(zip(("id", "version", "digest"), key))
    value = closed()
    value["preparation"].update(families=[reference(KEY), reference(command_key)],
        recipe_component_sets=[[reference(KEY)], [reference(command_key)]],
        resolution_candidate_kinds=["python-callable"])
    value["command"] = {"component": list(command_key),
        "installed_platform_policy": command_module.INSTALLED_PLATFORM_POLICY,
        "resource_platform_policy": command_module.RESOURCE_PLATFORM_POLICY}
    write_policy(packaged.host, value)
    packaged.refresh()
    proof = packaged.capture()
    monkeypatch.setattr(artifact_module, "verify_release_artifact", lambda *args: proof)
    monkeypatch.setattr(recipes, "builtin_components", lambda: {
        key[0] + "@" + key[1]: {"digest": key[2], "dependencies": []} for key in (KEY, command_key)})
    if outcome != "accepted":
        monkeypatch.setattr(command_module, "installed_platform_policy", lambda: "e" * 64)
    if outcome == "changed-helper":
        packaged.helpers[0].write_bytes(b"Changed helper")
    elif outcome == "signature-denied":
        def invalid(*args, **kwargs):
            raise PreparationError("command_release_platform_unavailable")
        monkeypatch.setattr(command_module, "_signature", invalid)
    if outcome in {"changed-helper", "signature-denied"}:
        with pytest.raises(PreparationError, match="^invalid_release_policy$"):
            policy_module._load_policy(packaged.host)
    else:
        public, agent, commands = policy_module._load_policy(packaged.host)
        assert agent is None
        assert public.artifact is proof
        assert public.resolution_candidate_kinds == frozenset({"python-callable"})
        assert public.release_policy_digest == contract_digest("release-policy-authority-v1", value)
        assert len(packaged.checks) == 3
        if outcome == "accepted":
            assert public.families == frozenset({KEY, command_key})
            assert type(commands[0]) is command_module.HostCommandAdmission
            assert type(commands[1]) is command_module.CommandPreparationResourceAdmission
        else:
            assert public.families == frozenset({KEY})
            assert public.recipe_component_sets == frozenset({frozenset({KEY})})
            assert commands == (None, None)
