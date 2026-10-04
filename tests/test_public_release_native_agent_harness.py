"""Public default discovery and private request-only cases keep distinct scope."""
import copy
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_public_release_native_agent")


ENGINE = ("textgen-engine-v1", "1.0.10", "b" * 64)
TASK = ("text-generate-v1", "1.0.10", "c" * 64)


def capabilities(driver):
    return {"automatic_preparation": True, "recipe_execution": True,
        "package_schema_versions": [1, 2], "profiles": ["mcp-stdio", "mcp-http", "model-service", "host-command"],
        "preparation_resolution": {"execution": True, "candidate_kinds": ["python-callable", "python-mcp"],
            "producer_versions": ["1.0.1"]},
        "preparation_execution": {"admission_digest": "a" * 64,
            "families": [list(driver.public.RESOLVED), list(driver.public.HOTDOG), list(driver.command.COMPONENT), list(TASK)],
            "recipe_component_sets": [[list(driver.public.RESOLVED)], [list(driver.public.ONNX), list(driver.public.HOTDOG)],
                [list(driver.command.COMPONENT)], [list(ENGINE), list(TASK)]]}}


@pytest.mark.parametrize("damage", [None, "python", "resolution", "onnx", "command", "command-closure",
    "schema", "profile", "engine", "task", "agent-family", "agent-closure", "version"])
def test_combined_discovery_requires_all_exact_families_and_closed_agent(driver, damage):
    value = copy.deepcopy(capabilities(driver)); engine, task = ENGINE, TASK
    execution = value["preparation_execution"]
    if damage == "python": execution["families"][0][2] = "f" * 64
    elif damage == "resolution": value["preparation_resolution"]["producer_versions"] = ["1.0.0"]
    elif damage == "onnx": execution["recipe_component_sets"][1].pop(0)
    elif damage == "command": execution["families"][2][2] = "f" * 64
    elif damage == "command-closure": execution["recipe_component_sets"][2].append(list(TASK))
    elif damage == "schema": value["package_schema_versions"] = [1]
    elif damage == "profile": value["profiles"].pop()
    elif damage == "engine": execution["recipe_component_sets"][3][0][2] = "f" * 64
    elif damage == "task": execution["families"][3][2] = "f" * 64
    elif damage == "agent-family": execution["families"].append(["agent-python-adapter-v1", "1.0.0", "f" * 64])
    elif damage == "agent-closure": execution["recipe_component_sets"].append([["agent-python-adapter-v1", "1.0.0", "f" * 64]])
    elif damage == "version": engine = (ENGINE[0], "1.0.9", ENGINE[2])
    if damage is None:
        assert driver.combined_capabilities(value, engine, task) == "a" * 64
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.combined_capabilities(value, engine, task)


@pytest.mark.parametrize("enabled", [False, True])
async def test_only_explicit_private_case_start_uses_existing_factory(driver, monkeypatch, enabled):
    calls = []
    async def ordinary(*args, **kwargs): calls.append(("ordinary", kwargs)); return "owned"
    async def private(*args, **kwargs): calls.append(("private", kwargs)); return "owned"
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    monkeypatch.setattr(driver.agent.Harness, "start", private)
    harness = object.__new__(driver.PublicReleaseAgentHarness); harness.combined_report = {}
    assert await harness.start("state", {}, "home", enabled=enabled) == "owned"
    assert calls == [("private" if enabled else "ordinary", {"enabled": enabled})]
    assert harness.combined_report == {"private_embedding_starts" if enabled else "ordinary_public_cli_starts": 1}


@pytest.mark.parametrize("failure", [None, "providers", "live", "preparation", "resolution", "agent-job", "missing-ledger"])
async def test_defaults_use_only_public_read_routes_and_require_empty_ledgers(driver, tmp_path, monkeypatch, failure):
    root = tmp_path.resolve()
    harness = driver.PublicReleaseAgentHarness(root, root, root, {"safe_to_remove": True}, report={})
    calls, starts = [], []
    async def ordinary(*args, **kwargs): starts.append(kwargs); return "owned"
    async def shutdown(*args): pass
    async def cli(state, environment, home, *args):
        calls.append(args)
        if args == ("capabilities", "--json"): return capabilities(driver)
        if args == ("preparations", "list", "--json"): return {"preparations": ["bad"] if failure == "preparation" else []}
        assert args == ("resolutions", "list", "--json")
        return {"resolutions": ["bad"] if failure == "resolution" else []}
    async def get(state, route):
        assert route == "/v1/agent-providers"
        return {"providers": ["bad"] if failure == "providers" else [], "live_execution_enabled": failure == "live"}
    counts = dict.fromkeys(("preparation_jobs", "resolution_jobs", "agent_host_jobs", "attempts"), 0)
    if failure == "agent-job": counts["agent_host_jobs"] = 1
    elif failure == "missing-ledger": counts.pop("attempts")
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    monkeypatch.setattr(harness, "shutdown", shutdown); monkeypatch.setattr(harness, "cli", cli); monkeypatch.setattr(harness, "get", get)
    monkeypatch.setattr(driver, "textgen_keys", lambda: (ENGINE, TASK)); monkeypatch.setattr(driver.agent, "job_counts", lambda _: counts)
    if failure is None:
        assert await harness.defaults() is True
        assert harness.combined_report["public_defaults"]["execution_factory_injected"] is False
        assert harness.combined_report["public_defaults"]["review_created_jobs"] is False
    else:
        with pytest.raises(driver.common.AcceptanceError): await harness.defaults()
        assert "public_defaults" not in harness.combined_report
    assert starts == [{"enabled": False}]
    assert not any("start" in call or "apply" in call or "create" in call for call in calls)


@pytest.mark.parametrize("damage", [None, "calls", "live", "http", "source", "relocation"])
async def test_private_cases_keep_exact_simulation_and_cleanup_scope(driver, monkeypatch, damage):
    result = {"passed": True, "simulated_provider": True, "provider_calls": 4, "live_provider_calls": 0,
        "http_calls": 2, "stdio_calls": 2, "pending_proposal_handoff": True, "source_removed": True,
        "captured_source_removed": True, "restart_invoked": True, "relocated_confined_probe": True}
    if damage == "calls": result["provider_calls"] = 5
    elif damage == "live": result["live_provider_calls"] = 1
    elif damage == "http": result["http_calls"] = 1
    elif damage == "source": result["captured_source_removed"] = False
    elif damage == "relocation": result["relocated_confined_probe"] = False
    async def private(self, label): return result
    monkeypatch.setattr(driver.agent.Harness, "case", private)
    harness = object.__new__(driver.PublicReleaseAgentHarness)
    if damage is None:
        observed = await harness.case("scalar")
        assert observed["execution_authority"] == "trusted_private_embedding"
        assert observed["public_agent_execution_verified"] is False and observed["execution_factory_injected"] is True
        assert observed["captured_state_retained_until_owned_cleanup"] is True
    else:
        with pytest.raises(driver.common.AcceptanceError): await harness.case("scalar")


@pytest.mark.parametrize("damage", [None, "bytes", "mode", "symlink", "hardlink"])
def test_fixture_source_inputs_are_bounded_regular_owned_bytes(driver, tmp_path, damage):
    path = tmp_path.resolve() / "fixture.py"; path.write_bytes(b"Synthetic fixture source"); path.chmod(0o600)
    before = driver.input_file(path)
    if damage == "bytes": path.write_bytes(b"Changed synthetic source")
    elif damage == "mode": path.chmod(0o666)
    elif damage == "symlink": original = path.with_suffix(".held"); path.rename(original); path.symlink_to(original)
    elif damage == "hardlink": (path.with_suffix(".alias")).hardlink_to(path)
    if damage == "bytes": assert driver.input_file(path) != before
    elif damage is None: assert driver.input_file(path) == before
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.input_file(path)


@pytest.mark.parametrize("fault", [None, "open-agent", "app-change", "fixture-change", "ownership", "missing-case", "missing-defaults"])
async def test_signed_composition_preserves_inputs_and_cannot_project_incomplete_cases(driver, tmp_path, monkeypatch, fault):
    app = tmp_path.resolve() / "Synthetic.app"
    host = app / "Contents/Resources/host"
    native = app / "Contents/MacOS/mcpme"
    native.parent.mkdir(parents=True); native.write_bytes(b"Synthetic native metadata"); native.chmod(0o700)
    policy = host / driver.command.POLICY_PATH; policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"agent": {} if fault == "open-agent" else None}))
    (host / "build.json").write_bytes(b"Synthetic build receipt metadata")
    scripts = tmp_path / "scripts"; scripts.mkdir()
    old = scripts / "old.py"; old.write_bytes(b"Synthetic old driver source")
    fixture = scripts / "agent_acceptance_fixture.py"; fixture.write_bytes(b"Synthetic request fixture source")
    monkeypatch.setattr(driver.agent, "__file__", str(old))
    monkeypatch.setattr(driver.common, "_host_paths", lambda _: None)
    monkeypatch.setattr(driver.common, "verify_host_payload", lambda _: {"source_commit": "a" * 40})
    signatures, runs = [], []
    monkeypatch.setattr(driver.command, "signature", lambda path, **kw: signatures.append(path))
    monkeypatch.setattr(driver.common, "verify_native_app_signature", lambda path: signatures.append(path))
    async def existing(_host, _native, report, *, harness_factory, cases):
        runs.append(cases)
        harness = harness_factory(tmp_path, host, native, {"safe_to_remove": True})
        assert type(harness) is driver.PublicReleaseAgentHarness and harness.combined_report is report
        report.update(verified=True, owned_children_joined=fault != "ownership", production_defaults_disabled=True,
            cases={name: {"passed": True, "provider_calls": 4} for name in cases if fault != "missing-case" or name == "scalar"})
        if fault != "missing-defaults": report["public_defaults"] = {"passed": True}
        if fault == "app-change": (host / "build.json").write_bytes(b"Changed synthetic app metadata")
        elif fault == "fixture-change": fixture.write_bytes(b"Changed synthetic request fixture source")
    monkeypatch.setattr(driver.common, "verify", existing)
    report = {"verified": False, "cases": {}}
    if fault in {"open-agent", "app-change", "fixture-change"}:
        with pytest.raises(driver.common.AcceptanceError): await driver.verify(host, native, report)
        assert report["verified"] is False
    else:
        await driver.verify(host, native, report)
        assert report["verified"] is (fault is None)
        assert report["public_agent_execution_verified"] is False and report["model_inference_calls"] == 0
        assert report["original_signed_app_preserved"] and report["original_agent_fixture_and_driver_preserved"]
        assert "production_defaults_disabled" not in report
        assert report["public_release_defaults_and_closed_agent_verified"] is True
    if fault == "open-agent": assert not runs and not signatures
    else: assert runs == [driver.agent.CASES]
