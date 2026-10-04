"""Metadata/transport controls for the public driver; no released-host workloads."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from test_management_workflow_grants import workflow, issued, start
from test_mcpme_preparations import foundation


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_public_management")


@pytest.fixture
def harness(driver, tmp_path):
    return driver.ManagementHarness(tmp_path, tmp_path / "host", tmp_path / "native", {"safe_to_remove": True},
        report={"public_cli_starts": 0, "sdk_sessions_exited": 0, "cases": {}})


@pytest.mark.asyncio
async def test_every_start_uses_ordinary_cli_even_if_enabled_requested(driver, harness, monkeypatch):
    calls = []
    async def ordinary(self, *args, enabled):
        calls.append(enabled); return object()
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    for enabled in (True, False, True):
        await harness.start("state", {}, "home", enabled=enabled)
    assert calls == [False] * 3 and harness.report["public_cli_starts"] == 3


@pytest.fixture
def policy(driver, tmp_path):
    source = Path(__file__).resolve().parents[1] / "templates/release-policies/phase3-v1.json"
    value = json.loads(source.read_bytes())
    target = tmp_path / driver.POLICY_PATH
    target.parent.mkdir(parents=True)
    def write(): target.write_text(json.dumps(value))
    write()
    return SimpleNamespace(host=tmp_path, value=value, path=target, write=write)


def test_actual_current_policy_has_exact_python_closure_and_closed_agent(driver, policy):
    assert driver.active_policy(policy.host) == policy.path.read_bytes()


@pytest.mark.parametrize("fault", ["schema", "unknown", "closure", "digest", "family", "agent-extra", "agent-none",
    "agent-boolean", "agent-live", "duplicate"])
def test_policy_cannot_enable_closed_or_invented_admission(driver, policy, fault):
    value = policy.value
    if fault == "schema": value["schema_version"] = True
    elif fault == "unknown": value["enable"] = True
    elif fault == "closure": value["preparation"]["recipe_component_sets"] = []
    elif fault == "digest":
        for group in value["preparation"]["recipe_component_sets"]:
            for component in group:
                if component["id"] == driver.PYTHON[0]: component["digest"] = "a" * 64
    elif fault == "family": value["preparation"]["families"] = []
    elif fault == "agent-extra": value["agent"]["enable"] = False
    elif fault == "agent-none": value["agent"] = None
    elif fault == "agent-boolean": value["agent"]["unlocked_resolution"] = 0
    elif fault == "agent-live": value["agent"]["providers"] = [{"adapter_id": "openai-responses-v1"}]
    policy.write()
    if fault == "duplicate": policy.path.write_text('{"schema_version":1,"schema_version":1}')
    before = policy.path.read_bytes()
    with pytest.raises(driver.common.AcceptanceError): driver.active_policy(policy.host)
    assert policy.path.read_bytes() == before


def capability(driver):
    return {"automatic_preparation": True, "recipe_execution": True, "management_mcp": "scoped_stdio",
        "preparation_execution": {"families": [list(driver.RESOLVED)],
            "recipe_component_sets": [[list(driver.PYTHON)]], "admission_digest": "a" * 64}}


@pytest.mark.parametrize("fault", [None, "disabled", "management", "family", "closure", "digest"])
def test_capabilities_require_actual_public_host_admission(driver, fault):
    value = capability(driver)
    if fault == "disabled": value["automatic_preparation"] = False
    elif fault == "management": value["management_mcp"] = "private"
    elif fault == "family": value["preparation_execution"]["families"] = []
    elif fault == "closure": value["preparation_execution"]["recipe_component_sets"] = []
    elif fault == "digest": value["preparation_execution"]["admission_digest"] = "invented"
    if fault is None: assert driver.public_capabilities(value) == "a" * 64
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.public_capabilities(value)


@pytest.mark.parametrize("fault", [None, "command", "credential", "state", "environment", "extra-server"])
def test_export_pins_actual_shipped_command_state_and_credential(driver, tmp_path, fault):
    launcher, state, credential = (tmp_path / name for name in ("mcpme", "state", "credential"))
    configuration = {"command": str(launcher), "args": ["--state-dir", str(state), "--credential-file",
        str(credential), "management", "connect"]}
    value = {"configuration": {"mcpServers": {"mcpme-management": configuration}}}
    if fault == "command": configuration["command"] = "/usr/bin/python3"
    elif fault == "credential": configuration["args"][3] = str(tmp_path / "other")
    elif fault == "state": configuration["args"][1] = str(tmp_path / "other")
    elif fault == "environment": configuration["env"] = {"ALLOW_EXECUTION": "1"}
    elif fault == "extra-server": value["configuration"]["mcpServers"]["other"] = configuration
    if fault is None: assert driver.validate_export(value, launcher, state, credential) == configuration
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.validate_export(value, launcher, state, credential)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "overflow", "timeout"])
async def test_actual_bounded_ram_capture_and_cleanup(driver, harness, tmp_path, mode, monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail("raw protocol bytes written to a temporary file")
    monkeypatch.setattr(driver.common.tempfile, "TemporaryFile", forbidden)
    code = {"complete": "import sys;sys.stdout.buffer.write(b'x'*262144)",
        "overflow": "import sys;sys.stdout.buffer.write(b'x'*2097152)",
        "timeout": "import time;time.sleep(60)"}[mode]
    if mode == "complete":
        status, data = await harness.run([sys.executable, "-I", "-B", "-c", code], {"PATH": "/usr/bin:/bin"}, tmp_path, capture=True)
        assert status == 0 and data == b"x" * 262144
    else:
        expected = "acceptance_child_timeout" if mode == "timeout" else "acceptance_command_output_limit"
        with pytest.raises(driver.common.AcceptanceError, match="^" + expected + "$"):
            await harness.run([sys.executable, "-I", "-B", "-c", code], {"PATH": "/usr/bin:/bin"}, tmp_path,
                capture=True, timeout=.05 if mode == "timeout" else 5)
    assert not harness.children and list(tmp_path.iterdir()) == []


def test_form_answers_use_production_ids_and_reject_new_required_questions(driver):
    form = {"fields": [{"id": "input-real", "label": "Test input", "kind": "object", "required": True},
        {"id": "output-real", "label": "Expected test result", "kind": "object", "required": True},
        {"id": "license-real", "label": "Source license", "kind": "choice", "required": True, "value": "choice-real"}]}
    assert driver.answers(form) == {"input-real": {"n": 7}, "output-real": {"value": 14}, "license-real": "choice-real"}
    form["fields"].append({"id": "changed", "label": "New requirement", "kind": "string", "required": True})
    with pytest.raises(driver.common.AcceptanceError): driver.answers(form)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["terminal", "reservation"])
async def test_live_revoke_requires_actually_owned_verifying_work(driver, harness, tmp_path, monkeypatch, fault):
    async def cli(*args): return {"job_id": "job", "state": "succeeded" if fault == "terminal" else "verifying"}
    monkeypatch.setattr(harness, "cli", cli)
    monkeypatch.setattr(driver.common, "preparation_ownership_count", lambda *args: 1)
    with pytest.raises(driver.common.AcceptanceError):
        await harness.wait_job(tmp_path, {}, tmp_path, "job", owned=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "retained", "ownership", "attachment", "installed"])
async def test_settlement_requires_real_cleanup_and_zero_revoke_attachment(driver, harness, tmp_path, monkeypatch, fault):
    job = {"job_id": "job", "state": "canceled", "ownership_settled": fault != "ownership", "reservation_retained": fault == "retained"}
    async def cli(state, env, home, *args):
        if args[0] == "imports": return {"attachment": {} if fault == "attachment" else None}
        return {"slots": [{}] if fault == "installed" else []}
    monkeypatch.setattr(harness, "cli", cli)
    monkeypatch.setattr(driver.common, "preparation_ownership_count", lambda *args: 0)
    if fault is None: await harness.settled(tmp_path, {}, tmp_path, job, {"draft_id": "draft"}, canceled=True)
    else:
        with pytest.raises(driver.common.AcceptanceError):
            await harness.settled(tmp_path, {}, tmp_path, job, {"draft_id": "draft"}, canceled=True)


@pytest.mark.parametrize("fault", [None, "missing-association", "changed-association", "missing-kind", "changed-kind",
    "changed-plan", "missing-plan", "owner-mode", "changed-runtime", "changed-source", "changed-artifacts", "scope-digest"])
def test_real_persisted_binding_projection_never_accepts_owner_fallback_or_plan_drift(driver, workflow, fault):
    grant, key = issued(workflow)
    grants, store, plan, imports, *_ = workflow
    job = start(workflow, grant, key)
    state = imports.root
    assert (state / "imports.sqlite").is_file()
    before = driver.binding_snapshot(state, grant, job)
    if fault == "missing-association": store.db.execute("DELETE FROM workflow_management_jobs")
    elif fault == "changed-association": store.db.execute("UPDATE workflow_management_jobs SET binding_digest=?", ("a" * 64,))
    elif fault == "missing-kind": store.db.execute("DELETE FROM workflow_management_job_kinds")
    elif fault == "changed-kind": store.db.execute("UPDATE workflow_management_job_kinds SET authority='{}'")
    elif fault == "changed-plan":
        changed = store._execution_plan(plan["plan_id"]); changed["runtime_digest"] = "d" * 64
        store.db.execute("UPDATE preparation_execution_plans SET data=?", (json.dumps(changed),))
    elif fault == "missing-plan": store.db.execute("DELETE FROM preparation_execution_plans")
    elif fault == "owner-mode": job["management_authority"] = {"schema_version": 1, "kind": "owner"}
    elif fault == "changed-runtime": job["runtime_digest"] = "d" * 64
    elif fault == "changed-source": job["binding"]["source_revision"] = "d" * 64
    elif fault == "changed-artifacts": job["binding"]["artifact_collection_digest"] = "d" * 64
    elif fault == "scope-digest":
        scope = json.loads(store.db.execute("SELECT scope FROM workflow_management_grants").fetchone()[0])
        scope["expires_at"] += 1
        store.db.execute("UPDATE workflow_management_grants SET scope=?", (json.dumps(scope),))
    if fault is None:
        grants.revoke(grant["grant_id"])
        assert driver.binding_snapshot(state, grant, job) == before
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.binding_snapshot(state, grant, job)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "form", "resolution", "recipe", "component", "plan", "job", "unknown-tool"])
async def test_static_mcp_review_requires_real_shipped_recipe_shape_and_no_jobs(driver, harness, monkeypatch, fault):
    from mcpme.management import operations
    from mcpme.recipes import validate_bundle
    builtin = Path(__file__).resolve().parents[1] / "templates/builtin-recipes/python-callable-v1"
    checked = validate_bundle(builtin)
    recipe = {**checked, "recipe_id": checked["manifest"]["id"], "version": "1.0.0", "state": "enabled", "revoked": False}
    reference = {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]}
    form = {"execution_available": fault != "form", "resolution_required": fault == "resolution", "recipe": reference,
        "candidate_id": "a" * 64, "form_digest": "b" * 64, "fields": []}
    if fault == "recipe": recipe["state"] = "disabled"
    elif fault == "component": recipe["manifest"]["steps"][0]["digest"] = "d" * 64
    tools = [item.tool for item in operations().values()]
    if fault == "unknown-tool": tools.append(SimpleNamespace(name="mcpme_authorize", inputSchema={"additionalProperties": False}))
    class Session:
        async def list_tools(self): return SimpleNamespace(tools=tools)
    denied, calls = [], []
    async def invoke(session, name, arguments=None, *, error=None):
        calls.append(name)
        if error:
            denied.append((name, error)); return {}
        if name == "mcpme_capabilities": return capability(driver)
        if name == "mcpme_create_import": return {"draft_id": "draft", "revision": 1}
        if name == "mcpme_get_preparation_form": return form
        if name == "mcpme_get_recipe": return recipe
        if name == "mcpme_plan_preparation_form":
            return {"execution_available": fault != "plan", "blockers": [], "plan_id": "plan", "plan_digest": "c" * 64}
        if name == "mcpme_list_preparations": return {"preparations": [{}] if fault == "job" else []}
        if name == "mcpme_list_resolutions": return {"resolutions": []}
        return {}
    monkeypatch.setattr(harness, "invoke", invoke)
    if fault is None:
        draft, plan = await harness.review(Session(), Path("/synthetic-source"))
        assert plan["plan_id"] == "plan" and draft["revision"] == 1
        assert denied == [("mcpme_start_preparation", "grant_scope"), ("mcpme_enable_recipe", "grant_scope"),
            ("mcpme_apply_install", "grant_scope")]
    else:
        with pytest.raises(driver.common.AcceptanceError): await harness.review(Session(), Path("/synthetic-source"))
        assert not denied


@pytest.mark.asyncio
async def test_closed_release_fails_before_signature_or_start_without_input_mutation(driver, policy, tmp_path, monkeypatch):
    app = tmp_path / "Closed.app"
    host = app / "Contents/Resources/host"; host.mkdir(parents=True)
    native = app / "Contents/MacOS/mcpme"; native.parent.mkdir(parents=True); native.write_bytes(b"metadata fixture")
    target = host / driver.POLICY_PATH; target.parent.mkdir(parents=True)
    policy.value["preparation"]["recipe_component_sets"] = []
    target.write_text(json.dumps(policy.value))
    before = driver.signed.snapshot(app)
    monkeypatch.setattr(driver, "source_snapshot", lambda root: ("a" * 40, {"synthetic": (0o644, "b" * 64)}))
    def forbidden(*args, **kwargs): pytest.fail("closed policy reached signatures or host launch")
    monkeypatch.setattr(driver.signed, "signature", forbidden)
    monkeypatch.setattr(driver.common.Harness, "start", forbidden)
    report = {}
    with pytest.raises(driver.common.AcceptanceError): await driver.verify(host, native, report, source_root=tmp_path)
    assert report["original_signed_app_preserved"] and report["source_tree_preserved"] and driver.signed.snapshot(app) == before
