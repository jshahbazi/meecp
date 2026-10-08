"""Synthetic private credentials only; no live provider/model calls."""
import copy
import importlib
import json
import os
import asyncio
import time
import uuid
import subprocess
import shutil
import sys
from pathlib import Path

import pytest
from test_recipe_native_management_acceptance import native_recipe_driver, private_json, file_identity


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_live_agent")


@pytest.fixture
def policy():
    # Fresh-proposal controls use an explicitly closed contract fixture rather
    # than requiring the distribution's current gate to remain closed.
    return {"schema_version": 1, "policy_id": "synthetic-closed-review-base", "preparation": {
        "families": [{"id": "resolved-python-v1", "version": "1.0.1",
            "digest": "3c24410b46415cb83b9d945ebf0049f552e9f7493943164f311939a79b49000e"}],
        "recipe_component_sets": [[{"id": "python-function-v1", "version": "1.0.0",
            "digest": "b28f98582aeff09e344b4a7049af7fdadbaf19436dc6724638ab64b6a9139d4e"}]],
        "resolution_candidate_kinds": ["python-callable", "python-mcp"]},
        "agent": {"components": [], "providers": [], "unlocked_resolution": False}, "command": None}


def test_review_binds_current_adapter_and_only_selected_model(driver, policy, monkeypatch):
    from meecp.preparation_agent_providers import OpenAIResponsesAdapter
    from meecp.preparation_release_policy import ReleasePolicy
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


def test_rejects_conflicting_agent_authority_and_unknown_configuration(driver, policy):
    active = driver.prepare_review(policy, "synthetic-explicit-model")["candidate_policy"]
    with pytest.raises(ValueError, match="live_agent_existing_authority_mismatch"):
        driver.prepare_review(active, "another-model")
    policy["agent"]["endpoint"] = "https://example.invalid"
    with pytest.raises(ValueError):
        driver.prepare_review(policy, "synthetic-explicit-model")


def test_exact_current_admission_review_is_idempotent(driver, policy, monkeypatch):
    from meecp.preparation_agent_providers import OpenAIResponsesAdapter
    monkeypatch.setattr(OpenAIResponsesAdapter, "backend", lambda *_: pytest.fail("backend constructed"))
    current = driver.prepare_review(policy, "synthetic-explicit-model")["candidate_policy"]
    current["policy_id"] = "synthetic-already-reviewed-policy"
    before = copy.deepcopy(current)
    review = driver.prepare_review(current, "synthetic-explicit-model")
    assert current == before and review["candidate_policy"] == before
    assert review["candidate_policy_changed"] is False
    assert review["credential_reads"] == review["live_provider_calls"] == 0
    assert not review["dispatch_authorized"] and not review["live_acceptance_verified"]


@pytest.mark.parametrize("fault", ["model", "digest", "adapter", "extra-model", "extra-provider",
    "extra-component", "locked", "missing-family", "missing-closure"])
def test_current_review_rejects_wrong_or_superset_authority(driver, policy, fault):
    current = driver.prepare_review(policy, "synthetic-explicit-model")["candidate_policy"]
    row = current["agent"]["providers"][0]
    if fault == "model": row["models"] = ["another-model"]
    elif fault == "digest": row["adapter_digest"] = "0" * 64
    elif fault == "adapter": row["adapter_id"] = "another-adapter"
    elif fault == "extra-model": row["models"].append("another-model")
    elif fault == "extra-provider":
        extra = copy.deepcopy(row); extra["adapter_id"] = "another-adapter"
        current["agent"]["providers"].append(extra)
    elif fault == "extra-component":
        extra = copy.deepcopy(current["preparation"]["families"][0])
        current["agent"]["components"].append(extra)
        current["preparation"]["recipe_component_sets"].append([extra])
    elif fault == "locked": current["agent"]["unlocked_resolution"] = False
    elif fault == "missing-family": current["preparation"]["families"].remove(driver.UNLOCKED)
    else: current["preparation"]["recipe_component_sets"].remove([driver.UNLOCKED])
    before = copy.deepcopy(current)
    with pytest.raises(ValueError): driver.prepare_review(current, "synthetic-explicit-model")
    assert current == before


def test_output_exclusive_and_no_credential_environment_read(driver, policy, monkeypatch, tmp_path):
    # Output/privacy semantics do not require the tracked distribution policy
    # to remain closed or to admit this synthetic fresh-proposal model.
    source = tmp_path / "templates/release-policies/phase3-v1.json"
    source.parent.mkdir(parents=True); source.write_text(json.dumps(policy))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
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


def test_ordinary_default_entry_without_pytest_import_paths(tmp_path):
    root = Path(__file__).resolve().parents[1]
    policy_path = root / "templates/release-policies/phase3-v1.json"
    before = policy_path.read_bytes(); current = json.loads(before)
    providers = current["agent"]["providers"]
    model = providers[0]["models"][0] if providers else "synthetic-explicit-model"
    output = tmp_path / "review.json"
    uv = shutil.which("uv")
    assert uv is not None
    # No inherited PYTHONPATH, credentials or pytest import setup. Existing
    # locked environment only; offline mode prevents dependency acquisition.
    result = subprocess.run([uv, "run", "--frozen", "python",
        "scripts/verify_phase3_live_agent.py", "--model", model,
        "--output", str(output)], cwd=root,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UV_OFFLINE": "1",
            "UV_CACHE_DIR": str(tmp_path / "uv-cache")}, capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == b"live_agent_review_prepared_no_dispatch\n"
    assert not result.stderr and output.stat().st_mode & 0o777 == 0o600
    report = json.loads(output.read_bytes())
    assert report["credential_reads"] == report["live_provider_calls"] == 0
    assert report["dispatch_authorized"] is False and report["live_acceptance_verified"] is False
    assert policy_path.read_bytes() == before
    if providers:
        assert report["candidate_policy"] == current and report["candidate_policy_changed"] is False
    else:
        assert report["candidate_policy_changed"] is True


def test_ordinary_active_default_entry_preserves_exact_policy(driver, policy, tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "owned-source-fixture"
    # This nondispatch fixture copies exact source bytes; it creates no host,
    # signed artifact, credential or execution admission.
    for name in ("backend", "scripts"):
        shutil.copytree(root / name, fixture / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    candidate = driver.prepare_review(policy, "synthetic-explicit-model")["candidate_policy"]
    candidate["policy_id"] = "synthetic-existing-current-policy"
    path = fixture / "templates/release-policies/phase3-v1.json"
    path.parent.mkdir(parents=True); path.write_text(json.dumps(candidate))
    before = path.read_bytes(); output = tmp_path / "active-review.json"
    uv = shutil.which("uv"); assert uv is not None
    result = subprocess.run([uv, "run", "--frozen", "python",
        str(fixture / "scripts/verify_phase3_live_agent.py"), "--model", "synthetic-explicit-model",
        "--output", str(output)], cwd=root,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UV_OFFLINE": "1",
            "UV_CACHE_DIR": str(tmp_path / "uv-cache")}, capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == b"live_agent_review_prepared_no_dispatch\n" and not result.stderr
    review = json.loads(output.read_bytes())
    assert path.read_bytes() == before and review["candidate_policy"] == candidate
    assert review["candidate_policy_changed"] is False
    assert review["credential_reads"] == review["live_provider_calls"] == 0 and not review["dispatch_authorized"]


def test_live_entry_never_bootstraps_source_backend(driver, monkeypatch, tmp_path):
    output = tmp_path / "live.json"
    before = list(sys.path)
    entered = []
    async def verify(*args):
        entered.append(True)
        assert sys.path == before
        raise driver.common.AcceptanceError("live_agent_test_blocked")
    monkeypatch.setattr(driver, "verify_live", verify)
    monkeypatch.setattr(driver.logging, "disable", lambda _: None)
    monkeypatch.setattr(driver.os, "umask", lambda _: 0o077)
    monkeypatch.setattr(sys, "argv", ["verify", "--run-live", "--model", "synthetic-explicit-model",
        "--host", str(tmp_path), "--native", str(tmp_path), "--credential-file", str(tmp_path / "unread"),
        "--output", str(output)])
    with pytest.raises(SystemExit) as exited:
        driver.main()
    assert exited.value.code == 1 and entered == [True] and sys.path == before
    report = json.loads(output.read_bytes())
    assert report["credential_read_attempted"] is report["credential_read"] is report["credential_accepted"] is False
    assert report["live_dispatch_attempted"] is False


def test_closed_policy_denied_before_credentials(driver, policy, monkeypatch):
    monkeypatch.setattr(driver, "read_selected_credential", lambda *_: pytest.fail("credential read"))
    with pytest.raises(driver.common.AcceptanceError, match="live_agent_candidate_inactive"):
        driver.active_candidate(policy, "synthetic", "a" * 64)


@pytest.mark.parametrize("fault", ["wrong_model", "wrong_adapter", "wrong_digest", "extra_model", "locked_resolution"])
def test_exact_candidate_rejects_authority_substitution(driver, policy, fault):
    from meecp.preparation_agent_providers import OpenAIResponsesAdapter
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
    elif fault == "newline": path.write_bytes(b"synthetic\nembedded")
    elif fault == "oversized": path.write_bytes(b"x" * 4099)
    else: path = Path("relative-synthetic-key")
    with pytest.raises((driver.common.AcceptanceError, FileNotFoundError)):
        driver.read_selected_credential(path)


def test_explicit_synthetic_credential_no_key_digest(driver, tmp_path):
    path = tmp_path / "selected"; path.write_bytes(b"synthetic-only"); path.chmod(0o600)
    key, identity = driver.read_selected_credential(path)
    assert key == "synthetic-only" and identity.st_size == 14


@pytest.mark.parametrize("size", [1, 4096])
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"], ids=["none", "LF", "CRLF"])
def test_one_conventional_credential_terminator_retains_raw_identity(driver, tmp_path, size, ending):
    path = tmp_path / "selected"; raw = b"x" * size + ending
    path.write_bytes(raw); path.chmod(0o600)
    events = []
    original = driver.os.read
    def observed(fd, maximum):
        assert events == ["attempt"] and maximum == 4099
        return original(fd, maximum)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(driver.os, "read", observed)
        key, identity = driver.read_selected_credential(path,
            on_read_attempt=lambda: events.append("attempt"), on_read_complete=lambda: events.append("complete"))
    assert key == "x" * size and identity.st_size == len(raw) and path.read_bytes() == raw
    assert events == ["attempt", "complete"]


@pytest.mark.parametrize("raw", [b"\n", b"\r\n", b"x\r", b"x\n\n", b"x\r\n\r\n",
    b"x\ny", b"x\ry", b" x", b"x ", b"x\t", b"x\xff", b"x" * 4097, b"x" * 4098],
    ids=["empty-LF", "empty-CRLF", "lone-CR", "extra-LF", "extra-CRLF", "embedded-LF", "embedded-CR",
        "leading-space", "trailing-space", "tab", "non-ASCII", "normalized-overflow-1", "normalized-overflow-2"])
def test_invalid_bounded_credential_bytes_record_physical_read(driver, tmp_path, raw):
    path = tmp_path / "selected"; path.write_bytes(raw); path.chmod(0o600)
    events = []
    with pytest.raises(driver.common.AcceptanceError, match="^live_agent_credential_file_invalid$"):
        driver.read_selected_credential(path, on_read_attempt=lambda: events.append("attempt"),
            on_read_complete=lambda: events.append("complete"))
    assert events == ["attempt", "complete"] and path.read_bytes() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["format", "read-error", "identity", "preflight"])
async def test_failed_configuration_receipt_distinguishes_attempt_read_and_acceptance(driver, tmp_path, monkeypatch, fault):
    path = tmp_path / "selected"; path.write_bytes(b"synthetic\nembedded" if fault == "format" else b"synthetic-only\n")
    path.chmod(0o600)
    report = {"credential_read_attempted": False, "credential_read": False, "credential_accepted": False}
    harness = driver.LiveHarness(tmp_path, tmp_path / "unused-host", tmp_path / "unused-native", {},
        model="synthetic", credential_file=path, report=report)
    async def get(*args): return {"providers": [], "live_execution_enabled": fault != "preflight"}
    async def forbidden(*args): pytest.fail("invalid credential reached provider registration")
    monkeypatch.setattr(harness, "get", get); monkeypatch.setattr(harness, "manager", forbidden)
    monkeypatch.setattr(driver.native_common, "job_counts", lambda *args: {"agent": 0})
    original = driver.os.read
    def observed(fd, maximum):
        assert report["credential_read_attempted"] and not report["credential_read"]
        if fault == "read-error": raise OSError("synthetic read failure")
        result = original(fd, maximum)
        if fault == "identity": os.fchmod(fd, 0o400)
        return result
    monkeypatch.setattr(driver.os, "read", observed)
    with pytest.raises((driver.common.AcceptanceError, OSError)): await harness.configure(tmp_path)
    assert report["credential_read_attempted"] is (fault != "preflight")
    assert report["credential_read"] is (fault in {"format", "identity"})
    assert report["credential_accepted"] is False and not harness.children


@pytest.mark.asyncio
async def test_normalized_synthetic_credential_is_accepted_without_key_metadata(driver, tmp_path, monkeypatch):
    path = tmp_path / "selected"; path.write_bytes(b"synthetic-only\r\n"); path.chmod(0o600)
    before = driver.credential_identity(path.lstat())
    report = {"credential_read_attempted": False, "credential_read": False, "credential_accepted": False}
    harness = driver.LiveHarness(tmp_path, tmp_path / "unused-host", tmp_path / "unused-native", {},
        model="synthetic", credential_file=path, report=report)
    async def get(*args): return {"providers": [], "live_execution_enabled": True}
    requests = []
    async def manager(state, route, body):
        assert route == "/v1/agent-providers" and body["credentials"] == {"api_key": "synthetic-only"}
        requests.append(route)
        return {"provider": {"execution_available": True, "models": ["synthetic"],
            "destination": driver.ENDPOINT, "credential_status": "configured"}}
    monkeypatch.setattr(harness, "get", get); monkeypatch.setattr(harness, "manager", manager)
    monkeypatch.setattr(driver.native_common, "job_counts", lambda *args: {"agent": 0})
    await harness.configure(tmp_path)
    assert report == {"credential_read_attempted": True, "credential_read": True, "credential_accepted": True}
    assert driver.credential_identity(path.lstat()) == before and requests == ["/v1/agent-providers"]
    assert "synthetic-only" not in json.dumps(report) and not harness.children


def test_terminal_live_receipt_retains_completed_read_before_format_rejection(driver, tmp_path, monkeypatch):
    path = tmp_path / "selected"; path.write_bytes(b"synthetic\nembedded"); path.chmod(0o600)
    output = tmp_path / "failed-live.json"
    async def verify(host, native, model, credential, report):
        harness = driver.LiveHarness(tmp_path, host, native, {}, model=model, credential_file=credential, report=report)
        async def get(*args): return {"providers": [], "live_execution_enabled": True}
        async def forbidden(*args): pytest.fail("invalid file reached registration")
        harness.get, harness.manager = get, forbidden
        await harness.configure(tmp_path)
    monkeypatch.setattr(driver, "verify_live", verify)
    monkeypatch.setattr(driver.native_common, "job_counts", lambda *args: {"agent": 0})
    monkeypatch.setattr(driver.logging, "disable", lambda _: None)
    monkeypatch.setattr(driver.os, "umask", lambda _: 0o077)
    monkeypatch.setattr(sys, "argv", ["verify", "--run-live", "--model", "synthetic", "--host", str(tmp_path),
        "--native", str(tmp_path), "--credential-file", str(path), "--output", str(output)])
    with pytest.raises(SystemExit) as exited: driver.main()
    assert exited.value.code == 1
    report = json.loads(output.read_bytes())
    assert report["diagnostic"]["code"] == "live_agent_credential_file_invalid"
    assert report["credential_read_attempted"] is report["credential_read"] is True
    assert report["credential_accepted"] is report["live_dispatch_attempted"] is False
    assert b"synthetic" not in output.read_bytes() and output.stat().st_mode & 0o777 == 0o600


def test_compiled_live_selection_review_and_legacy_simulation(native_recipe_driver, tmp_path):
    result = subprocess.run([str(native_recipe_driver), "--agent-live-acceptance-self-test"],
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=15)
    assert result.returncode == 0 and result.stdout == b"agent_live_acceptance_contracts_passed\n" and not result.stderr


def test_compiled_handoff_observation_has_no_native_startup(native_recipe_driver, tmp_path):
    result = subprocess.run([str(native_recipe_driver), "--agent-handoff-observation-self-test"],
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=15)
    assert result.returncode == 0 and result.stdout == b"agent_handoff_observation_contracts_passed\n" and not result.stderr


def test_compiled_form_error_current_stale_malformed_and_recovery(native_recipe_driver, tmp_path):
    result = subprocess.run([str(native_recipe_driver), "--preparation-form-error-self-test"],
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, timeout=15)
    assert result.returncode == 0 and result.stdout == b"preparation_form_error_contracts_passed\n" and not result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_stage", ["source_inspection", "native_run"])
async def test_secondary_observation_failure_cannot_replace_original_case_error(driver, tmp_path, monkeypatch, fault_stage):
    harness = driver.LiveHarness(tmp_path, tmp_path, tmp_path, None, model="synthetic", credential_file=tmp_path / "not-read", report={})
    original = driver.common.AcceptanceError("synthetic_original_failure")
    joined = asyncio.Event()
    provider = {"provider_id": str(uuid.uuid4()), "generation": str(uuid.uuid4())}
    async def start(state, *_, **kwargs):
        assert kwargs == {"enabled": False}
        driver.common.write_private_json(state / "manager-connection.json", {"synthetic": True})
        return object()
    async def configure(*_): return provider
    async def cli(_state, _environment, _home, *argv):
        if argv[:2] == ("imports", "create"):
            source = tmp_path / "scalar" / "unfamiliar source"
            return {"draft_id": str(uuid.uuid4()), "revision": 1,
                "source": {"revision": "a" * 64, "files": [{"path": path.name, "sha256": driver.common.digest(path)} for path in source.iterdir()]}}
        if fault_stage == "source_inspection": raise original
        return {"candidates": []}
    async def manager(*_):
        return {"recipes": [{"id": driver.RECIPE, "version": "1.0.0", "digest": "a" * 64}],
            "providers": [dict(provider, simulated=False)]}
    async def run(*_, **_kwargs): raise original
    def failed_count(*_): raise OSError("synthetic unavailable ledger")
    async def observe(*_): raise RuntimeError("synthetic secondary failure")
    async def shutdown(*_): joined.set()
    harness.start, harness.configure, harness.cli = start, configure, cli
    harness.manager, harness.run = manager, run
    harness.observe_failure, harness.shutdown = observe, shutdown
    monkeypatch.setattr(driver.native_common, "job_counts", failed_count)
    with pytest.raises(driver.common.AcceptanceError) as raised:
        await harness.case("scalar")
    assert raised.value is original and joined.is_set()
    assert harness.report["live_failure_observation"] == {"observation_available": False,
        "call_accounting": {"known": False, "remote_calls_after_cancel": "unknown"}}
    assert not harness.credential_file.exists() and not harness.children


@pytest.mark.parametrize("fault", ["duplicate_role", "unknown_role", "extra_call_key", "bool_transfer", "negative_transfer",
    "oversized_transfer", "bool_reservation", "wrong_reservation", "extra_usage_key", "bool_usage", "negative_usage", "oversized_usage", "missing_source"])
def test_malformed_durable_call_records_remain_unknown(driver, fault):
    job_id, import_id = str(uuid.uuid4()), str(uuid.uuid4())
    draft = {"draft_id": import_id, "revision": 1, "source": {"revision": "sha256:" + "a" * 64}}
    call = {"role": "interface", "transfer_bytes": 32, "reserved_output_tokens": driver.LIMITS["max_output_tokens"],
        "usage": {"input_tokens": 1, "output_tokens": 2}}
    job = {"job_id": job_id, "import_id": import_id, "revision": 1, "source_revision": draft["source"]["revision"],
        "state": "failed", "local_io_settled": True, "calls": [call]}
    assert driver.agent_call_metadata(job, draft, job_id)["known"] is True
    if fault == "duplicate_role": job["calls"].append(copy.deepcopy(call))
    elif fault == "unknown_role": call["role"] = "synthetic-unretained-content"
    elif fault == "extra_call_key": call["synthetic-unretained-content"] = True
    elif fault == "bool_transfer": call["transfer_bytes"] = True
    elif fault == "negative_transfer": call["transfer_bytes"] = -1
    elif fault == "oversized_transfer": call["transfer_bytes"] = driver.LIMITS["max_transfer_bytes"] + 1
    elif fault == "bool_reservation": call["reserved_output_tokens"] = True
    elif fault == "wrong_reservation": call["reserved_output_tokens"] += 1
    elif fault == "extra_usage_key": call["usage"]["synthetic-unretained-content"] = 1
    elif fault == "bool_usage": call["usage"]["input_tokens"] = True
    elif fault == "negative_usage": call["usage"]["input_tokens"] = -1
    elif fault == "oversized_usage": call["usage"]["input_tokens"] = 10**9 + 1
    else: draft.pop("source")
    assert driver.agent_call_metadata(job, draft, job_id) == {"known": False, "remote_calls_after_cancel": "unknown"}


@pytest.mark.parametrize("fault", ["missing_source_revision", "none_source_revision", "malformed_source_revision",
    "missing_import", "none_import", "malformed_import", "malformed_job_id", "nonpositive_revision", "bool_revision",
    "unknown_state", "missing_state", "none_state", "nonbool_settlement", "missing_settlement", "none_settlement", "bare_digest"])
def test_paired_malformed_bindings_cannot_claim_known_call_accounting(driver, fault):
    job_id, import_id = str(uuid.uuid4()), str(uuid.uuid4())
    draft = {"draft_id": import_id, "revision": 1, "source": {"revision": "sha256:" + "a" * 64}}
    job = {"job_id": job_id, "import_id": import_id, "revision": 1, "source_revision": draft["source"]["revision"],
        "state": "failed", "local_io_settled": True, "calls": []}
    assert driver.agent_call_metadata(job, draft, job_id)["known"] is True
    # Both sides are deliberately corrupted alike: equality alone must never
    # establish a valid original-source binding or metadata certainty.
    if fault == "missing_source_revision": draft["source"].pop("revision"); job.pop("source_revision")
    elif fault == "none_source_revision": draft["source"]["revision"] = job["source_revision"] = None
    elif fault == "malformed_source_revision": draft["source"]["revision"] = job["source_revision"] = "not-a-digest"
    elif fault == "missing_import": draft.pop("draft_id"); job.pop("import_id")
    elif fault == "none_import": draft["draft_id"] = job["import_id"] = None
    elif fault == "malformed_import": draft["draft_id"] = job["import_id"] = "not-a-uuid"
    elif fault == "malformed_job_id": job_id = job["job_id"] = "not-a-uuid"
    elif fault == "nonpositive_revision": draft["revision"] = job["revision"] = 0
    elif fault == "bool_revision": draft["revision"] = job["revision"] = True
    elif fault == "unknown_state": job["state"] = "not-a-state"
    elif fault == "missing_state": job.pop("state")
    elif fault == "none_state": job["state"] = None
    elif fault == "nonbool_settlement": job["local_io_settled"] = 1
    elif fault == "missing_settlement": job.pop("local_io_settled")
    elif fault == "bare_digest": draft["source"]["revision"] = job["source_revision"] = "a" * 64
    else: job["local_io_settled"] = None
    assert driver.agent_call_metadata(job, draft, job_id) == {"known": False, "remote_calls_after_cancel": "unknown"}


@pytest.mark.parametrize("source_revision", ["sha256:" + "a" * 64, "a" * 40])
def test_call_accounting_accepts_exact_production_source_revision_domains(driver, source_revision):
    from test_preparation_agent import request_for
    request, _ = request_for(source_revision=source_revision)
    job_id = str(uuid.uuid4())
    draft = {"draft_id": request.import_id, "revision": request.revision, "source": {"revision": request.source_revision}}
    job = {"job_id": job_id, "import_id": request.import_id, "revision": request.revision,
        "source_revision": request.source_revision, "state": "analyzing", "local_io_settled": False, "calls": []}
    assert driver.agent_call_metadata(job, draft, job_id) == {"known": True, "state": "analyzing",
        "local_io_settled": False, "reserved_calls": 0, "usage_recorded_calls": 0, "remote_calls_after_cancel": "unknown"}


@pytest.mark.parametrize("damage", ["absent", "unknown_code", "unknown_stage", "extra_condition", "integer_condition"])
def test_failure_projection_never_copies_unknown_native_content(driver, damage):
    secret = "synthetic-unretained-content"
    value = {"stage": "agent_handoff", "error_code": "preparation_form_unavailable",
        "handoff_postconditions": dict.fromkeys(driver.HANDOFF_POSTCONDITIONS, True),
        "routes": ["POST v1/preparations/form"], "options": secret, "response": secret}
    if damage == "absent": value = None
    elif damage == "unknown_code": value["error_code"] = secret
    elif damage == "unknown_stage": value["stage"] = secret
    elif damage == "extra_condition": value["handoff_postconditions"][secret] = True
    else: value["handoff_postconditions"]["form_present"] = 1
    projected = driver.native_failure_metadata(value)
    assert secret not in json.dumps(projected)
    if damage == "absent": assert projected == {"receipt_available": False}
    elif damage == "unknown_code":
        assert projected["native_error_present"] and not projected["native_error_allowed"]
        assert projected["native_error_code"] == "native_live_agent_acceptance_failed"
    elif damage == "unknown_stage": assert projected["native_stage"] == "unknown"
    else: assert projected["handoff_postconditions_available"] is False


def test_failure_projection_preserves_exact_native_handoff_conditions(driver):
    value = {"stage": "agent_handoff", "error_code": "preparation_form_unavailable",
        "handoff_postconditions": dict.fromkeys(driver.HANDOFF_POSTCONDITIONS, True),
        "routes": ["POST v1/agent-preparations/start", "POST v1/preparations/form"]}
    value["handoff_postconditions"].update(form_present=False, ordinary_error_absent=False)
    projected = driver.native_failure_metadata(value)
    assert projected["native_error_code"] == "preparation_form_unavailable"
    assert projected["native_stage"] == "agent_handoff"
    assert projected["handoff_postconditions"] == value["handoff_postconditions"]
    assert projected["agent_start_requested"] and projected["form_requested"]
    assert not projected["resolution_start_requested"] and not projected["preparation_start_requested"]


@pytest.mark.asyncio
async def test_failure_observation_reads_production_ledger_before_cleanup(driver, tmp_path, monkeypatch):
    from test_preparation_agent import request_for
    from meecp.preparation_agent_contracts import AgentUsageV1
    from meecp.preparation_agent_coordinator import AgentAttemptStore
    from meecp.preparation_agent_host import AgentPreparationHost
    from meecp.preparation_contracts import canonical_bytes
    request, _ = request_for(source_revision="sha256:" + "a" * 64)
    store = AgentAttemptStore(tmp_path / "journal")
    try:
        attempt = store.create(request); attempt.update(state="running", local_io_settled=False); store.save(attempt)
        store.reserve_call(attempt["attempt_id"], attempt["generation"], request, "interface", 32)
        store.account(attempt["attempt_id"], attempt["generation"], request, "interface", AgentUsageV1(input_tokens=1, output_tokens=2))
        store.reserve_call(attempt["attempt_id"], attempt["generation"], request, "dependencies", 32)
        store.finish(attempt["attempt_id"], attempt["generation"], "failed", "agent_provider_failed")
        job_id = str(uuid.uuid4())
        job = {"job_id": job_id, "import_id": request.import_id, "revision": request.revision,
            "source_revision": request.source_revision, "state": "failed", "local_io_settled": True,
            "attempt_id": attempt["attempt_id"], "candidate_metadata": None, "candidate_blocker": None}
        # Exercise the production SQLite/get projection; no host executor or
        # provider is constructed by this ledger-only fixture.
        store.db.execute("CREATE TABLE agent_host_jobs (id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, data TEXT NOT NULL)")
        store.db.execute("INSERT INTO agent_host_jobs VALUES (?,?,?)", (job_id, str(uuid.uuid4()), canonical_bytes(job).decode()))
        host = object.__new__(AgentPreparationHost); host.db, host.store = store.db, store
        draft = {"draft_id": request.import_id, "revision": request.revision, "source": {"revision": request.source_revision}}
        harness = driver.LiveHarness(tmp_path, tmp_path, tmp_path, None, model="synthetic", credential_file=tmp_path / "not-read", report={})
        seen = []
        async def get(state, route):
            seen.append(route); return host.get(job_id)
        harness.get = get
        monkeypatch.setattr(driver.native_common, "job_counts", lambda _state: {"agent_host_jobs": 1, "attempts": 1})
        await harness.observe_failure(tmp_path, draft, {"agent_job_id": job_id, "stage": "agent_handoff"})
        assert seen == ["/v1/agent-preparations/jobs/" + job_id]
        observed = harness.report["live_failure_observation"]
        assert observed["journal_counts"] == {"agent_host_jobs": 1, "attempts": 1}
        assert observed["call_accounting"] == {"known": True, "state": "failed", "local_io_settled": True,
            "reserved_calls": 2, "usage_recorded_calls": 1, "remote_calls_after_cancel": "unknown"}
        mismatch = dict(draft, revision=draft["revision"] + 1)
        assert driver.agent_call_metadata(host.get(job_id), mismatch, job_id)["known"] is False
        assert not harness.children and not harness.credential_file.exists()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failure_observation_missing_or_silent_owner_is_unknown_and_joined(driver, tmp_path, monkeypatch):
    harness = driver.LiveHarness(tmp_path, tmp_path, tmp_path, None, model="synthetic", credential_file=tmp_path / "not-read", report={})
    draft = {"draft_id": str(uuid.uuid4()), "revision": 1, "source": {"revision": "a" * 40}}
    monkeypatch.setattr(driver.native_common, "job_counts", lambda _state: {})
    completed = asyncio.Event()
    async def silent(*_):
        try: await asyncio.Future()
        finally: completed.set()
    harness.get = silent
    actual_wait = driver.asyncio.wait_for
    async def short_wait(value, timeout):
        assert timeout == 5
        return await actual_wait(value, 0.02)
    monkeypatch.setattr(driver.asyncio, "wait_for", short_wait)
    await harness.observe_failure(tmp_path, draft, {"agent_job_id": str(uuid.uuid4()), "stage": "agent_handoff"})
    assert completed.is_set() and harness.report["live_failure_observation"]["call_accounting_available"] is False
    assert harness.report["live_failure_observation"]["call_accounting"]["known"] is False
    seen = []
    async def forbidden(*_): seen.append(True); pytest.fail("invalid job ID reached owner route")
    harness.get = forbidden
    await harness.observe_failure(tmp_path, draft, {"agent_job_id": "../not-owned"})
    assert not seen and harness.report["live_failure_observation"]["call_accounting"]["known"] is False


def test_main_preserves_fixed_native_fallback_and_metadata(driver, tmp_path, monkeypatch):
    output = tmp_path / "receipt.json"
    async def verify(_host, _native, _model, _credential, report):
        report["live_failure_observation"] = driver.native_failure_metadata({"stage": "agent_handoff"})
        error = driver.common.AcceptanceError("native_live_agent_acceptance_failed"); error.native_stage = "agent_handoff"
        raise error
    monkeypatch.setattr(driver, "verify_live", verify)
    monkeypatch.setattr(driver.logging, "disable", lambda _: None)
    monkeypatch.setattr(driver.os, "umask", lambda _: 0o077)
    monkeypatch.setattr(sys, "argv", ["verify", "--run-live", "--model", "synthetic", "--host", str(tmp_path),
        "--native", str(tmp_path), "--credential-file", str(tmp_path), "--output", str(output)])
    with pytest.raises(SystemExit) as exited: driver.main()
    assert exited.value.code == 1
    report = json.loads(output.read_bytes())
    assert report["diagnostic"] == {"code": "native_live_agent_acceptance_failed", "stage": "agent_handoff"}
    assert not report["credential_read_attempted"] and not report["credential_read"]
    assert report["live_failure_observation"]["receipt_available"] is True


@pytest.mark.parametrize("fault", ["missing_generation", "invalid_generation", "extra_secret", "simulation_marker", "expired", "replaced_manager"])
def test_compiled_live_fixture_denied_before_claim(native_recipe_driver, tmp_path, driver, fault):
    workspace = tmp_path.resolve() / ("meecp-native-onboarding-" + str(uuid.uuid4()))
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
    from meecp.supervisor import Supervisor, management_app, HostServer, bind_listener
    from meecp_local_transport import bind_private_listener
    workspace = tmp_path.resolve() / ("meecp-native-onboarding-" + str(uuid.uuid4()))
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
