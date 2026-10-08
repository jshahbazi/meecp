"""Metadata/transport controls for the public driver; no released-host workloads."""
import importlib
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from test_management_workflow_grants import workflow, issued, start
from test_meecp_preparations import foundation


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
    # Historical closed-agent controls use an explicit fixture, independently
    # of whether the current distribution admits the reviewed selected model.
    value["agent"] = {"components": [], "providers": [], "unlocked_resolution": False}
    target = tmp_path / driver.POLICY_PATH
    target.parent.mkdir(parents=True)
    def write(): target.write_text(json.dumps(value))
    write()
    return SimpleNamespace(host=tmp_path, value=value, path=target, write=write)


def test_explicit_closed_policy_has_exact_python_closure(driver, policy):
    assert driver.active_policy(policy.host) == policy.path.read_bytes()
    assert driver.typed_policy_scope(policy.path.read_bytes()) == "closed"


def test_actual_current_policy_has_exact_python_closure_and_reviewed_agent(driver, tmp_path, monkeypatch):
    from meecp.preparation_agent_providers import OpenAIResponsesAdapter
    from meecp.preparation_release_policy import ReleasePolicy
    source = Path(__file__).resolve().parents[1] / "templates/release-policies/phase3-v1.json"
    data = source.read_bytes(); value = json.loads(data)
    target = tmp_path / driver.POLICY_PATH
    target.parent.mkdir(parents=True); target.write_bytes(data)
    monkeypatch.setattr(OpenAIResponsesAdapter, "backend", lambda *_: pytest.fail("provider constructed"))
    assert driver.active_policy(tmp_path) == data
    public, agent, command = ReleasePolicy.model_validate(value).admissions()
    assert driver.typed_policy_scope(data) == "selected_model_accepted"
    assert agent.providers == frozenset({("openai-responses-v1", OpenAIResponsesAdapter().digest,
        driver.REVIEWED_AGENT_MODEL)}) and agent.components <= public.families
    assert source.read_bytes() == target.read_bytes() == data
    # Policy inspection creates no host, state, job, credential or dispatcher.
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()) == [driver.POLICY_PATH]


@pytest.mark.parametrize("fault", ["model", "extra-model", "adapter", "digest", "extra-provider",
    "component", "extra-component", "locked", "missing-family", "missing-closure", "extra-agent-field"])
def test_selected_policy_rejects_any_unreviewed_agent_authority(driver, tmp_path, fault):
    source = Path(__file__).resolve().parents[1] / "templates/release-policies/phase3-v1.json"
    value = json.loads(source.read_bytes())
    selected = value["agent"]
    if fault == "model": selected["providers"][0]["models"] = ["another-model"]
    elif fault == "extra-model": selected["providers"][0]["models"].append("another-model")
    elif fault == "adapter": selected["providers"][0]["adapter_id"] = "another-adapter"
    elif fault == "digest": selected["providers"][0]["adapter_digest"] = "0" * 64
    elif fault == "extra-provider": selected["providers"].append(copy.deepcopy(selected["providers"][0]))
    elif fault == "component": selected["components"][0]["digest"] = "0" * 64
    elif fault == "extra-component": selected["components"].append(copy.deepcopy(selected["components"][0]))
    elif fault == "locked": selected["unlocked_resolution"] = False
    elif fault == "missing-family": value["preparation"]["families"].remove(driver.live.UNLOCKED)
    elif fault == "missing-closure": value["preparation"]["recipe_component_sets"].remove([driver.live.UNLOCKED])
    else: selected["allow_extra"] = True
    target = tmp_path / driver.POLICY_PATH
    target.parent.mkdir(parents=True); target.write_text(json.dumps(value)); before = target.read_bytes()
    with pytest.raises(driver.common.AcceptanceError): driver.active_policy(tmp_path)
    with pytest.raises((driver.common.AcceptanceError, ValueError)): driver.typed_policy_scope(before)
    assert target.read_bytes() == before and source.read_bytes() != before


def test_typed_policy_requires_current_adapter_without_constructing_provider(driver, monkeypatch):
    from meecp.preparation_agent_providers import OpenAIResponsesAdapter
    source = Path(__file__).resolve().parents[1] / "templates/release-policies/phase3-v1.json"
    before = source.read_bytes()
    monkeypatch.setattr(OpenAIResponsesAdapter, "digest", property(lambda _: "0" * 64))
    monkeypatch.setattr(OpenAIResponsesAdapter, "backend", lambda *_: pytest.fail("provider constructed"))
    with pytest.raises(driver.common.AcceptanceError, match="^management_adapter_not_reviewed$"):
        driver.typed_policy_scope(before)
    assert source.read_bytes() == before


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
    launcher, state, credential = (tmp_path / name for name in ("meecp", "state", "credential"))
    configuration = {"command": str(launcher), "args": ["--state-dir", str(state), "--credential-file",
        str(credential), "management", "connect"]}
    value = {"configuration": {"mcpServers": {"meecp-management": configuration}}}
    if fault == "command": configuration["command"] = "/usr/bin/python3"
    elif fault == "credential": configuration["args"][3] = str(tmp_path / "other")
    elif fault == "state": configuration["args"][1] = str(tmp_path / "other")
    elif fault == "environment": configuration["env"] = {"ALLOW_EXECUTION": "1"}
    elif fault == "extra-server": value["configuration"]["mcpServers"]["other"] = configuration
    if fault is None: assert driver.validate_export(value, launcher, state, credential) == configuration
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.validate_export(value, launcher, state, credential)


@pytest.mark.asyncio
@pytest.mark.parametrize("app_name", ["Synthetic.app", "Relocated app.app"])
async def test_actual_bundled_export_reaches_exact_native_sdk_entrypoint(driver, tmp_path, monkeypatch, app_name):
    """Use production export/credential parsing; stop before opening transport."""
    from contextlib import asynccontextmanager
    import uuid
    import mcp.client.stdio as transport
    from meecp import __main__ as cli
    from meecp.private import write_private_json

    app = tmp_path / app_name
    host = app / "Contents/Resources/host"
    module = host / "backend/meecp/__main__.py"
    native = app / "Contents/MacOS/meecp"
    launcher = host / "bin/meecp"
    for path in (module, native, launcher):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic location fixture.\n")
    monkeypatch.setattr(cli, "__file__", str(module))
    state = tmp_path / "state"
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    credential = credentials / "scoped.json"
    token = "s" * 43
    grant_id = str(uuid.uuid4())
    write_private_json(credential, {"schema_version": 1, "grant_id": grant_id, "token": token})
    args = cli.parser().parse_args(["--state-dir", str(state), "--credential-file", str(credential),
        "management", "export", "--json"])
    exported = await cli.execute(args)
    expected = {"command": str(native), "args": ["--state-dir", str(state), "--credential-file",
        str(credential), "management", "connect"]}
    assert exported["grant_id"] == grant_id
    assert driver.validate_export(exported, native, state, credential) == expected
    assert token not in json.dumps(exported)
    with pytest.raises(driver.common.AcceptanceError, match="^management_export_binding_changed$"):
        driver.validate_export(exported, launcher, state, credential)

    harness = driver.ManagementHarness(tmp_path, host, native, {"safe_to_remove": True},
        report={"public_cli_starts": 0, "sdk_sessions_exited": 0, "cases": {}})
    exports = []
    async def actual_export(*arguments):
        exports.append(arguments)
        return await cli.execute(args)
    monkeypatch.setattr(harness, "cli", actual_export)
    transports = []
    class StopBeforeTransport(Exception):
        pass
    @asynccontextmanager
    async def guarded_transport(parameters, **kwargs):
        transports.append(parameters)
        assert parameters.command == str(native) and parameters.args == expected["args"]
        raise StopBeforeTransport()
        yield
    monkeypatch.setattr(transport, "stdio_client", guarded_transport)
    with pytest.raises(StopBeforeTransport):
        async with harness.sdk(state, {"PATH": "/usr/bin:/bin"}, tmp_path, credential):
            pytest.fail("guarded fixture opened a session")
    assert len(exports) == len(transports) == 1 and not harness.children
    assert harness.report["sdk_sessions_exited"] == 0


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
    from meecp.management import operations
    from meecp.recipes import validate_bundle
    builtin = Path(__file__).resolve().parents[1] / "templates/builtin-recipes/python-callable-v1-0-1"
    checked = validate_bundle(builtin)
    recipe = {**checked, "recipe_id": checked["manifest"]["id"], "version": checked["manifest"]["version"], "state": "enabled", "revoked": False}
    reference = {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]}
    form = {"execution_available": fault != "form", "resolution_required": fault == "resolution", "recipe": reference,
        "candidate_id": "a" * 64, "form_digest": "b" * 64, "fields": []}
    if fault == "recipe": recipe["state"] = "disabled"
    elif fault == "component": recipe["manifest"]["steps"][0]["digest"] = "d" * 64
    tools = [item.tool for item in operations().values()]
    if fault == "unknown-tool": tools.append(SimpleNamespace(name="meecp_authorize", inputSchema={"additionalProperties": False}))
    class Session:
        async def list_tools(self): return SimpleNamespace(tools=tools)
    denied, calls = [], []
    async def invoke(session, name, arguments=None, *, error=None):
        calls.append(name)
        if error:
            denied.append((name, error)); return {}
        if name == "meecp_capabilities": return capability(driver)
        if name == "meecp_create_import": return {"draft_id": "draft", "revision": 1}
        if name == "meecp_get_preparation_form": return form
        if name == "meecp_get_recipe": return recipe
        if name == "meecp_plan_preparation_form":
            return {"execution_available": fault != "plan", "blockers": [], "plan_id": "plan", "plan_digest": "c" * 64}
        if name == "meecp_list_preparations": return {"preparations": [{}] if fault == "job" else []}
        if name == "meecp_list_resolutions": return {"resolutions": []}
        return {}
    monkeypatch.setattr(harness, "invoke", invoke)
    if fault is None:
        draft, plan = await harness.review(Session(), Path("/synthetic-source"))
        assert plan["plan_id"] == "plan" and draft["revision"] == 1
        assert denied == [("meecp_start_preparation", "grant_scope"), ("meecp_enable_recipe", "grant_scope"),
            ("meecp_apply_install", "grant_scope")]
    else:
        with pytest.raises(driver.common.AcceptanceError): await harness.review(Session(), Path("/synthetic-source"))
        assert not denied


@pytest.mark.asyncio
async def test_closed_release_fails_before_signature_or_start_without_input_mutation(driver, policy, tmp_path, monkeypatch):
    app = tmp_path / "Closed.app"
    host = app / "Contents/Resources/host"; host.mkdir(parents=True)
    native = app / "Contents/MacOS/meecp"; native.parent.mkdir(parents=True); native.write_bytes(b"metadata fixture")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initialize", "discovery", "lifetime"])
async def test_actual_silent_sdk_child_times_out_and_original_transport_handle_joins(driver, harness, tmp_path, monkeypatch, stage):
    import asyncio
    import mcp.client.stdio as transport
    launcher = harness.native
    launcher.parent.mkdir(parents=True, exist_ok=True)
    code = ("#!" + sys.executable + "\nimport json,sys\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        + (" if request.get('method')=='initialize':\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2025-11-25',"
            "'capabilities':{'tools':{}},'serverInfo':{'name':'synthetic','version':'1'}}}),flush=True)\n"
            if stage != "initialize" else ""))
    compile(code, "silent-fixture", "exec")
    launcher.write_text(code); launcher.chmod(0o700)
    import subprocess
    preflight = subprocess.run([sys.executable, "-I", "-B", str(launcher)],
        input=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n', capture_output=True, timeout=5)
    assert preflight.returncode == 0
    if stage != "initialize": assert json.loads(preflight.stdout)["result"]["protocolVersion"] == "2025-11-25"
    owned = []
    create = transport._create_platform_compatible_process
    async def capture(**kwargs):
        process = await create(**kwargs); owned.append(process); return process
    monkeypatch.setattr(transport, "_create_platform_compatible_process", capture)
    monkeypatch.setattr(driver, "SDK_READ_SECONDS", 3)
    monkeypatch.setattr(driver, "SDK_STEP_SECONDS", .5)
    monkeypatch.setattr(driver, "SDK_SESSION_SECONDS", 1.5)
    state, credential = tmp_path / "state", tmp_path / "credential"
    async def cli(*args):
        return {"configuration": {"mcpServers": {"meecp-management": {"command": str(launcher),
            "args": ["--state-dir", str(state), "--credential-file", str(credential), "management", "connect"]}}}}
    monkeypatch.setattr(harness, "cli", cli)
    caught = None
    try:
        async with asyncio.timeout(5):
            async with harness.sdk(state, {"PATH": "/usr/bin:/bin"}, tmp_path, credential) as session:
                if stage == "discovery": await harness.discovery(session)
                elif stage == "lifetime": await asyncio.sleep(60)
                else: pytest.fail("silent initializer admitted a session")
    except BaseException as error:
        caught, _ = driver.common.bounded_failure(error)
    assert isinstance(caught, driver.common.AcceptanceError) and caught.code == "management_sdk_timeout"
    assert len(owned) == 1 and owned[0].returncode is not None and not harness.children


@pytest.mark.asyncio
async def test_actual_early_exit_is_not_reclassified_as_a_fired_sdk_timeout(driver, harness, tmp_path, monkeypatch):
    import asyncio
    import mcp.client.stdio as transport
    launcher = harness.native
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!" + sys.executable + "\nraise SystemExit(0)\n")
    launcher.chmod(0o700)
    owned = []
    create = transport._create_platform_compatible_process
    async def capture(**kwargs):
        process = await create(**kwargs); owned.append(process); return process
    monkeypatch.setattr(transport, "_create_platform_compatible_process", capture)
    monkeypatch.setattr(driver, "SDK_READ_SECONDS", 5)
    monkeypatch.setattr(driver, "SDK_STEP_SECONDS", 3)
    monkeypatch.setattr(driver, "SDK_SESSION_SECONDS", 8)
    state, credential = tmp_path / "state", tmp_path / "credential"
    async def cli(*args):
        return {"configuration": {"mcpServers": {"meecp-management": {"command": str(launcher),
            "args": ["--state-dir", str(state), "--credential-file", str(credential), "management", "connect"]}}}}
    monkeypatch.setattr(harness, "cli", cli)
    caught = None
    try:
        async with asyncio.timeout(10):
            async with harness.sdk(state, {"PATH": "/usr/bin:/bin"}, tmp_path, credential):
                pytest.fail("early-exiting facade admitted a session")
    except BaseException as error:
        caught, _ = driver.common.bounded_failure(error)
    assert caught is not None
    assert not isinstance(caught, driver.common.AcceptanceError) or caught.code != "management_sdk_timeout"
    assert len(owned) == 1 and owned[0].returncode == 0 and not harness.children


@pytest.mark.asyncio
async def test_pending_prelaunch_journal_never_counts_as_a_started_probe(driver, harness, tmp_path, monkeypatch):
    import uuid
    identifier = str(uuid.uuid4())
    reservation = tmp_path / "preparation-reservations" / (identifier + ".json")
    reservation.parent.mkdir(); reservation.write_text("{}"); reservation.chmod(0o600)
    calls = []
    async def cli(*args):
        calls.append(args)
        return {"job_id": identifier, "state": "verifying" if len(calls) == 1 else "canceled",
            "workspace": str(tmp_path / "preparations" / identifier)}
    scans = []
    def pending(*args): scans.append(args); return None
    monkeypatch.setattr(harness, "cli", cli)
    monkeypatch.setattr(driver.common, "preparation_ownership_count", lambda *args: 1)
    monkeypatch.setattr(driver, "probe_entry", pending)
    with pytest.raises(driver.common.AcceptanceError, match="^management_slow_worker_not_observed$"):
        await harness.wait_job(tmp_path, {}, tmp_path, identifier, owned=True,
            observation=(object(), object(), (1, 2, 3, 4, 5), "meecp-probe-" + "a" * 32))
    assert len(scans) == 1 and "probe_entry_witness" not in harness.report


@pytest.mark.asyncio
async def test_actual_owned_process_chain_entry_marker_and_original_handles_join(driver, harness, tmp_path):
    import asyncio
    import subprocess
    import uuid
    marker = "meecp-probe-" + uuid.uuid4().hex
    probe = ("import ctypes,sys\napi=ctypes.CDLL('/usr/lib/libSystem.B.dylib')\n"
        "api.pthread_setname_np.argtypes=[ctypes.c_char_p]\napi.pthread_setname_np.restype=ctypes.c_int\n"
        "assert api.pthread_setname_np(" + repr(marker.encode()) + ")==0\nsys.stdin.buffer.read()\n")
    def guardian(child):
        return ("import subprocess,sys\np=subprocess.Popen([sys.executable,'-I','-B','-c'," + repr(child)
            + "],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "try:sys.stdin.buffer.read()\nfinally:p.stdin.close();p.wait(10)\n")
    manager = subprocess.Popen([sys.executable, "-I", "-B", "-c", guardian(guardian(probe))],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"})
    harness.children.add(manager)
    reader = driver.DarwinProcessReader()
    witness = None
    try:
        identity = reader.identity(manager.pid)
        executable = Path(reader.path(manager.pid))
        async with asyncio.timeout(5):
            while witness is None:
                witness = driver.probe_entry(reader, manager, identity, executable, executable, marker)
                await asyncio.sleep(.02)
        assert witness["probe"][1] == witness["reaper"][0] and witness["reaper"][1] == manager.pid
        assert driver.probe_entry(reader, manager, identity, executable, executable, "meecp-probe-" + "0" * 32) is None
        assert driver.probe_entry(reader, manager, identity, tmp_path / "wrong-reaper", executable, marker) is None
        assert driver.probe_entry(reader, manager, identity, executable, tmp_path / "wrong-interpreter", marker) is None
        with pytest.raises(driver.common.AcceptanceError):
            driver.probe_entry(reader, manager, (*identity[:-1], identity[-1] + 1), executable, executable, marker)
    finally:
        manager.stdin.close()
        await asyncio.to_thread(manager.wait, 10)
        await harness.stop(manager)
    assert manager.returncode == 0 and not harness.children
    assert all(reader.identity(value[0]) != value for value in witness.values())


@pytest.mark.parametrize("fault", ["probe-start", "wrapper-start", "manager-start", "probe-path", "wrapper-path"])
def test_marker_scan_rechecks_full_start_and_path_chain(driver, fault):
    manager = SimpleNamespace(pid=10, poll=lambda: None)
    baseline = (10, 1, 1, 1, 1)
    identities = {10: baseline, 20: (20, 10, 1, 2, 1), 30: (30, 20, 1, 3, 1)}
    paths = {20: "/fixture/reaper", 30: "/fixture/interpreter"}
    class Reader:
        changed = False
        def identity(self, pid):
            original = identities[pid]
            target = {"probe-start": 30, "wrapper-start": 20, "manager-start": 10}.get(fault)
            return (*original[:-1], original[-1] + 1) if self.changed and pid == target else original
        def path(self, pid):
            target = {"probe-path": 30, "wrapper-path": 20}.get(fault)
            return "/fixture/replaced" if self.changed and pid == target else paths[pid]
        def children(self, pid): return {10: [20], 20: [30]}[pid]
        def tagged(self, pid, marker): self.changed = True; return True
    with pytest.raises(driver.common.AcceptanceError, match="^management_probe_identity_changed$"):
        driver.probe_entry(Reader(), manager, baseline, Path(paths[20]), Path(paths[30]), "meecp-probe-" + "a" * 32)
