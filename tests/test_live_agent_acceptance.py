"""No network, credentials, state registry, or provider factory is used here."""
import copy
import importlib
import json
import os
import asyncio
import time
import uuid
import subprocess
from pathlib import Path

import pytest
from test_recipe_native_management_acceptance import native_recipe_driver, private_json, file_identity


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_live_agent")


@pytest.fixture
def policy():
    return json.loads((Path(__file__).resolve().parents[1] / "templates/release-policies/phase3-v1.json").read_bytes())


def test_review_binds_current_adapter_and_only_selected_model(driver, policy, monkeypatch):
    from mcpme.preparation_agent_providers import OpenAIResponsesAdapter
    from mcpme.preparation_release_policy import ReleasePolicy
    monkeypatch.setattr(OpenAIResponsesAdapter, "backend", lambda *_: pytest.fail("backend constructed"))
    before = copy.deepcopy(policy)
    review = driver.prepare_review(policy, "synthetic-explicit-model")
    assert policy == before
    preparation, agent, command = ReleasePolicy.model_validate(review["candidate_policy"]).admissions()
    assert agent.permits("openai-responses-v1", OpenAIResponsesAdapter().digest, "synthetic-explicit-model")
    assert not agent.permits("openai-responses-v1", OpenAIResponsesAdapter().digest, "another-model")
    assert agent.components <= preparation.families and agent.unlocked_resolution
    assert review["candidate_policy"]["command"] == before["command"]
    assert review["candidate_policy"]["preparation"]["resolution_candidate_kinds"] == before["preparation"]["resolution_candidate_kinds"]
    assert review["limits"] == driver.LIMITS
    assert review["maximum_reserved_output_tokens"] == 16384
    assert review["credential_reads"] == review["live_provider_calls"] == 0
    assert not review["dispatch_authorized"] and not review["live_acceptance_verified"]


@pytest.mark.parametrize("model", [None, "", "x\n", "x " , "https://example.invalid", "x" * 129])
def test_no_implicit_or_endpoint_model(driver, policy, model):
    with pytest.raises(ValueError):
        driver.prepare_review(policy, model)


def test_rejects_existing_agent_authority_and_unknown_configuration(driver, policy):
    active = driver.prepare_review(policy, "synthetic-explicit-model")["candidate_policy"]
    with pytest.raises(ValueError, match="live_agent_base_must_be_closed"):
        driver.prepare_review(active, "another-model")
    policy["agent"]["endpoint"] = "https://example.invalid"
    with pytest.raises(ValueError):
        driver.prepare_review(policy, "synthetic-explicit-model")


def test_output_exclusive_and_no_credential_environment_read(driver, monkeypatch, tmp_path):
    output = tmp_path / "review.json"
    monkeypatch.setattr("sys.argv", ["prepare", "--model", "synthetic-explicit-model", "--output", str(output)])
    original_getenv = os.getenv
    def checked_getenv(name, default=None):
        assert name not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"}
        return original_getenv(name, default)
    monkeypatch.setattr("os.getenv", checked_getenv)
    driver.main()
    assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        driver.main()
    assert output.read_bytes() == before


def test_closed_policy_denied_before_credentials(driver, policy, monkeypatch):
    monkeypatch.setattr(driver, "read_selected_credential", lambda *_: pytest.fail("credential read"))
    with pytest.raises(driver.common.AcceptanceError, match="live_agent_candidate_inactive"):
        driver.active_candidate(policy, "synthetic", "a" * 64)


@pytest.mark.parametrize("fault", ["wrong_model", "wrong_adapter", "wrong_digest", "extra_model", "locked_resolution"])
def test_exact_candidate_rejects_authority_substitution(driver, policy, fault):
    from mcpme.preparation_agent_providers import OpenAIResponsesAdapter
    candidate = driver.prepare_review(policy, "synthetic")["candidate_policy"]
    row = candidate["agent"]["providers"][0]
    if fault == "wrong_model": row["models"] = ["other"]
    elif fault == "wrong_adapter": row["adapter_id"] = "unregistered"
    elif fault == "wrong_digest": row["adapter_digest"] = "a" * 64
    elif fault == "extra_model": row["models"].append("other")
    else: candidate["agent"]["unlocked_resolution"] = False
    with pytest.raises(driver.common.AcceptanceError):
        driver.active_candidate(candidate, "synthetic", OpenAIResponsesAdapter().digest)


@pytest.mark.parametrize("fault", ["mode", "link", "hardlink", "newline", "oversized", "relative"])
def test_explicit_credential_boundary(driver, tmp_path, fault):
    path = tmp_path / "selected"
    path.write_bytes(b"synthetic-only")
    path.chmod(0o600)
    if fault == "mode": path.chmod(0o644)
    elif fault == "link":
        link = tmp_path / "link"; link.symlink_to(path); path = link
    elif fault == "hardlink": os.link(path, tmp_path / "other")
    elif fault == "newline": path.write_bytes(b"synthetic\n")
    elif fault == "oversized": path.write_bytes(b"x" * 4097)
    else: path = Path("relative-synthetic-key")
    with pytest.raises((driver.common.AcceptanceError, FileNotFoundError)):
        driver.read_selected_credential(path)


def test_explicit_synthetic_credential_no_key_digest(driver, tmp_path):
    path = tmp_path / "selected"; path.write_bytes(b"synthetic-only"); path.chmod(0o600)
    key, identity = driver.read_selected_credential(path)
    assert key == "synthetic-only" and identity.st_size == 14


def test_compiled_live_selection_review_and_legacy_simulation(native_recipe_driver, tmp_path):
    result = subprocess.run([str(native_recipe_driver), "--agent-live-acceptance-self-test"],
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=15)
    assert result.returncode == 0 and result.stdout == b"agent_live_acceptance_contracts_passed\n" and not result.stderr


@pytest.mark.parametrize("fault", ["missing_generation", "invalid_generation", "extra_secret", "simulation_marker", "expired", "replaced_manager"])
def test_compiled_live_fixture_denied_before_claim(native_recipe_driver, tmp_path, driver, fault):
    workspace = tmp_path.resolve() / ("mcpme-native-onboarding-" + str(uuid.uuid4()))
    base = workspace / "live-agent"
    state, home, temporary = (base / name for name in ("state", "home", "tmp"))
    for path in (workspace, base, state, home, temporary): path.mkdir(mode=0o700)
    manager = state / "manager-connection.json"; private_json(manager, {"synthetic": True})
    provider = {"provider_id": str(uuid.uuid4()), "generation": str(uuid.uuid4())}
    recipe = {"id": driver.RECIPE, "version": "1.0.0", "digest": "a" * 64}
    fixture, output = driver.live_fixture(base, state, str(uuid.uuid4()), recipe, provider, "synthetic")
    value = json.loads(fixture.read_bytes()); nonce = value["fixture_nonce"]
    marker = state / ("native-agent-live-" + nonce + ".json")
    if fault == "missing_generation": del value["agent_scope"]["provider_generation"]
    elif fault == "invalid_generation": value["agent_scope"]["provider_generation"] = "invalid"
    elif fault == "extra_secret": value["agent_scope"]["api_key"] = "synthetic-only"
    elif fault == "simulation_marker": marker.rename(state / ("native-agent-" + nonce + ".json"))
    elif fault == "expired":
        proof = json.loads(marker.read_bytes()); proof["expires_at"] = int(time.time()) - 1
        marker.write_text(json.dumps(proof))
    else:
        replacement = state / "replacement"; private_json(replacement, {"synthetic": True}); replacement.replace(manager)
    fixture.write_text(json.dumps(value))
    result = subprocess.run([str(native_recipe_driver), "--agent-live-native-acceptance", str(fixture)],
        env={"HOME": str(home), "TMPDIR": str(temporary), "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=10)
    assert result.returncode == 1 and result.stdout == b"preparation_native_acceptance_failed\n" and not result.stderr
    assert not output.exists() and not marker.with_suffix(".claimed").exists()


async def test_real_owner_parent_refuses_closed_live_profile_before_provider_call(native_recipe_driver, tmp_path, driver):
    import uvicorn
    from mcpme.supervisor import Supervisor, management_app, HostServer, bind_listener
    from mcpme_local_transport import bind_private_listener
    workspace = tmp_path.resolve() / ("mcpme-native-onboarding-" + str(uuid.uuid4()))
    base = workspace / "live-agent"
    state, home, temporary = (base / name for name in ("state", "home", "tmp"))
    for path in (workspace, base, state, home, temporary): path.mkdir(mode=0o700)
    host = Supervisor(state, tmp_path / "unused-reaper")
    listener = bind_listener(); private = bind_private_listener(state, "manager")
    port = listener.getsockname()[1]; token = "synthetic-owner-token-only-for-private-test"
    server = HostServer(uvicorn.Config(management_app(host, token, port), access_log=False,
        log_config=None, log_level="critical", timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[listener, private])); child = None
    try:
        async with asyncio.timeout(5):
            while not server.started: await asyncio.sleep(0.01)
        source = base / "source"; source.mkdir(mode=0o700)
        (source / "worker.py").write_text("def double(n):\n return n*2\n")
        (source / "LICENSE").write_text("Synthetic permission.\n")
        draft = host.imports.create(source, name="Synthetic", intent="Wrap double", source_mode="automatic")
        provider = host.agent_provider_registry.create({"schema_version": 1, "adapter_id": "openai-responses-v1",
            "name": "Synthetic", "models": ["synthetic"], "configuration": {}, "credentials": {"api_key": "synthetic-only"}})["provider"]
        # The ordinary source host remains closed despite a configured profile.
        assert provider["execution_available"] is False
        private_json(state / "manager-connection.json", {"api_version": 1, "endpoint": f"http://127.0.0.1:{port}",
            "token": token, "supervisor_pid": os.getpid()})
        options = host.agent_preparations.options(draft["draft_id"], draft["revision"])
        recipe = next(row for row in options["recipes"] if row["id"] == driver.RECIPE)
        fixture, output = driver.live_fixture(base, state, draft["draft_id"], recipe, provider, "synthetic")
        child = await asyncio.create_subprocess_exec(str(native_recipe_driver), "--agent-live-native-acceptance", str(fixture),
            env={"HOME": str(home), "TMPDIR": str(temporary), "PATH": "/usr/bin:/bin"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(30): printed, error = await child.communicate()
        assert child.returncode == 1 and not error
        assert printed.endswith(b"preparation_native_acceptance_failed\n")
        receipt = json.loads(output.read_bytes())
        assert receipt["passed"] is False and receipt["stage"] == "agent_options"
        assert "POST v1/agent-preparations/start" not in receipt["routes"]
        assert all(value == 0 for value in driver.native_common.job_counts(state).values())
    finally:
        if child is not None and child.returncode is None: child.kill(); await child.wait()
        server.should_exit = True; await task; await host.close(); listener.close(); private.close()
