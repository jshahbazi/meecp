"""Exact management workflow scope, durable association and current fences."""
import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
import sys
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from meecp.grants import GrantError, read_credential
from meecp.imports import ImportStore, ImportError as SourceImportError
from meecp.management import Management, operations
from meecp.management_workflow_grants import WorkflowGrantStore, OWNER_AUTHORITY
from meecp.preparation_contracts import PreparationError, canonical_bytes
from meecp.preparation_resolution import ResolutionStore
from meecp.preparations import PreparationStore
from meecp.recipes import RecipeStore, validate_bundle
from test_meecp_preparations import foundation, policy, recipe_bundle, descriptor
from test_preparation_resolution import source, request
from test_meecp_grants import client, credential
from test_meecp_host import prepared
from test_meecp_management import registered
from test_meecp_management_cli import private_manager, execute
from test_meecp_management_mcp import sdk
from test_preparation_processes import group_reaper
from test_preparation_runner import controlled, planned, finished


@pytest.fixture
def workflow(foundation):
    imports, recipes, store, draft, raw = foundation
    resolutions = ResolutionStore(imports)
    grants = WorkflowGrantStore(store, resolutions, recipes)
    plan = store.execution_plan(draft["draft_id"], draft["revision"], raw, policy(),
        runner_digest="a" * 64, verifier_digest="b" * 64, runtime_digest="c" * 64)
    return grants, store, plan, imports, recipes, draft


@pytest.fixture
def resolution_workflow(source):
    imports, store, draft, candidate, _ = source
    recipes = RecipeStore(imports)
    preparations = PreparationStore(imports, recipes, resolutions=store)
    grants = WorkflowGrantStore(preparations, store, recipes)
    plan = store.plan(request(draft, candidate), candidate, expires_in=300)
    return grants, store, plan, imports, recipes, draft


def issued(fixture, kind="preparation", **extra):
    grants, _, plan, *_ = fixture
    key = str(uuid.uuid4())
    grant = grants.issue({"mode": kind + "-execution", "plan_id": plan["plan_id"],
        "plan_digest": plan["plan_digest"], "idempotency_key": key, **extra})
    return grant, key


def start(fixture, grant, key):
    _, store, plan, *_ = fixture
    return store.authorize_start(plan["plan_id"], plan["plan_digest"], key, grant_id=grant["grant_id"])


@pytest.mark.parametrize("extra",[{"ttl_seconds":True},{"ttl_seconds":0},{"ttl_seconds":3601},
    {"job_id":"injected"},{"service_probe":{}},{"secret_ref":"injected"}])
def test_issuance_rejects_unreviewed_authority_fields_and_invalid_ttl(workflow,extra):
    grants,store,*_=workflow
    with pytest.raises(GrantError,match="^invalid_grant$"):
        issued(workflow,**extra)
    assert grants.list()["total"]==0 and list(grants.root.iterdir())==[] and store.list()==[]


def test_issuance_limit_keeps_existing_token_and_binding(workflow,monkeypatch):
    grants,store,*_=workflow
    monkeypatch.setattr("meecp.management_workflow_grants.MAX_GRANTS",1)
    grant,key=issued(workflow)
    token=credential(grant)
    with pytest.raises(GrantError,match="^grant_limit$"):
        issued(workflow)
    assert grants.authenticate(token)["grant_id"]==grant["grant_id"] and grants.list()["total"]==1
    assert [path.name for path in grants.root.iterdir()]==[grant["grant_id"]+".json"]
    assert start(workflow,grant,key)["management_authority"]["grant_id"]==grant["grant_id"]


@pytest.mark.parametrize("state",["queued","running","succeeded"])
def test_resolution_grant_plan_survives_unrelated_planning_after_plan_expiry(resolution_workflow,tmp_path,monkeypatch,state):
    """Plan collection cannot shorten an admitted grant's execution lifetime."""
    grants,store,plan,*_=resolution_workflow
    grant,key=issued(resolution_workflow,"resolution",ttl_seconds=3600)
    job=start(resolution_workflow,grant,key)
    if state in {"running","succeeded"}:store.begin(job["job_id"],tmp_path/"unused-workspace")
    if state=="succeeded":
        from meecp.preparation_resolution import ResolutionRequestV1,result_from_files
        derived=tmp_path/"unused-workspace"/"derived";derived.mkdir(parents=True)
        (derived/"pyproject.toml").write_bytes(b"[project]\nname='synthetic'\nversion='1.0.0'\n")
        (derived/"uv.lock").write_bytes(b"version=1\n")
        result=result_from_files(ResolutionRequestV1.model_validate(plan["request"]),derived,[],None,1024)
        store.publish(job["job_id"],result)
    original=store.db.execute("SELECT data FROM resolution_plans WHERE plan_id=?",(plan["plan_id"],)).fetchone()[0]
    monkeypatch.setattr("meecp.preparation_resolution.time.time",lambda:plan["expires_at"]+1)
    def assert_current():
        if state=="running":assert store.check_authority(job["job_id"])
        elif state=="succeeded":
            grants.authorize_route(grant["grant_id"],"GET","/v1/resolutions/"+job["job_id"])
            assert [item["job_id"] for item in grants.filter_jobs(grant["grant_id"],store.list_summaries())]==[job["job_id"]]
        else:
            with pytest.raises(PreparationError,match="^resolution_authority_expired$"):
                store.check_authority(job["job_id"])
    assert_current()
    from meecp.preparation_resolution import ResolutionRequestV1
    other=store.plan(ResolutionRequestV1.model_validate(plan["request"]),plan["candidate"],expires_in=300)
    assert other["plan_id"]!=plan["plan_id"]
    assert_current()
    assert store.db.execute("SELECT data FROM resolution_plans WHERE plan_id=?",(plan["plan_id"],)).fetchone()[0]==original
    grants.revoke(grant["grant_id"])
    with pytest.raises(PreparationError,match="^management_execution_revoked$"):
        store.check_authority(job["job_id"])


def test_expired_unused_resolution_plan_is_collected_without_grant_rollover(resolution_workflow,monkeypatch):
    grants,store,plan,*_=resolution_workflow
    grant,key=issued(resolution_workflow,"resolution",ttl_seconds=3600)
    monkeypatch.setattr("meecp.preparation_resolution.time.time",lambda:plan["expires_at"]+1)
    from meecp.preparation_resolution import ResolutionRequestV1
    other=store.plan(ResolutionRequestV1.model_validate(plan["request"]),plan["candidate"],expires_in=300)
    assert store.db.execute("SELECT 1 FROM resolution_plans WHERE plan_id=?",(plan["plan_id"],)).fetchone() is None
    with pytest.raises(PreparationError,match="^resolution_plan_not_found$"):
        start(resolution_workflow,grant,key)
    with pytest.raises(GrantError,match="^grant_scope$"):
        store.authorize_start(other["plan_id"],other["plan_digest"],key,grant_id=grant["grant_id"])
    assert store.list()==[] and grants.inspect(grant["grant_id"])["job_id"] is None


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
def test_exact_attempt_hash_only_credentials_and_atomic_duplicate(request, kind):
    fixture = request.getfixturevalue("workflow" if kind == "preparation" else "resolution_workflow")
    grants, store, plan, imports, *_ = fixture
    grant, key = issued(fixture, kind)
    token = credential(grant)
    assert token not in json.dumps(grant) and "token_hash" not in grant and "token_digest" not in grant
    assert (Path(grant["credential_file"]).stat().st_mode & 0o777) == 0o600
    assert (grants.root.stat().st_mode & 0o777) == 0o700
    row = grants.db.execute("SELECT * FROM workflow_management_grants").fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    captured = json.loads(row["scope"])
    assert captured["plan"] == {k:v for k,v in store._execution_plan(plan["plan_id"]).items() if k != "job_id"} if kind == "preparation" else captured["plan"] == plan
    assert token.encode() not in imports.path.read_bytes()
    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(lambda _: start(fixture, grant, key), range(2)))
    assert jobs[0] == jobs[1] and len(store.list()) == 1
    job = jobs[0]
    assert job["schema_version"] == 2 and job["management_authority"]["grant_id"] == grant["grant_id"]
    assert grants.inspect(grant["grant_id"])["job_id"] == job["job_id"]
    assert grants.db.execute("SELECT count(*) FROM workflow_management_jobs").fetchone()[0] == 1
    assert grants.db.execute("SELECT count(*) FROM workflow_management_job_kinds").fetchone()[0] == 1
    assert grants.authenticate(token)["workflow_grant"] is True
    with pytest.raises(GrantError):
        store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()), grant_id=grant["grant_id"])
    with pytest.raises(GrantError):
        store.authorize_start(plan["plan_id"], plan["plan_digest"], key)


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
def test_scope_cannot_adopt_owner_job_or_other_grant(request, kind):
    fixture = request.getfixturevalue("workflow" if kind == "preparation" else "resolution_workflow")
    grants, store, plan, *_ = fixture
    grant, key = issued(fixture, kind)
    owner = store.authorize_start(plan["plan_id"], plan["plan_digest"], key)
    assert owner["management_authority"] == OWNER_AUTHORITY
    with pytest.raises(GrantError, match="^grant_operation_mismatch$"):
        start(fixture, grant, key)
    assert grants.inspect(grant["grant_id"])["job_id"] is None
    assert not grants.db.execute("SELECT * FROM workflow_management_jobs").fetchall()


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
@pytest.mark.parametrize("damage", ["association_missing", "association_changed", "grant_missing", "kind_missing", "marker_missing", "version_downgrade", "marker_bool", "scope_changed", "expiry_changed", "credential_changed"])
def test_missing_or_corrupt_authority_never_becomes_owner(request, kind, damage):
    fixture = request.getfixturevalue("workflow" if kind == "preparation" else "resolution_workflow")
    grants, store, _, imports, *_ = fixture
    grant, key = issued(fixture, kind)
    job = start(fixture, grant, key)
    with imports._transaction():
        if damage == "association_missing":
            grants.db.execute("DELETE FROM workflow_management_jobs")
        elif damage == "association_changed":
            grants.db.execute("UPDATE workflow_management_jobs SET binding_digest=?", ("f" * 64,))
        elif damage == "grant_missing":
            grants.db.execute("DELETE FROM workflow_management_grants")
        elif damage == "kind_missing":
            grants.db.execute("DELETE FROM workflow_management_job_kinds")
        elif damage == "scope_changed":
            grants.db.execute("UPDATE workflow_management_grants SET scope_digest=?", ("f" * 64,))
        elif damage == "expiry_changed":
            grants.db.execute("UPDATE workflow_management_grants SET expires_at=expires_at+60")
        elif damage == "credential_changed":
            grants.db.execute("UPDATE workflow_management_grants SET token_hash=?", ("f" * 64,))
        else:
            if damage == "marker_bool": job["management_authority"]["schema_version"] = True
            else: job.pop("management_authority")
            if damage == "version_downgrade": job["schema_version"] = 1
            store._save(job)
    with pytest.raises(PreparationError, match="^management_execution_authority_invalid$"):
        store.check_authority(job["job_id"])
    assert store.get(job["job_id"])["state"] == "queued"


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
@pytest.mark.parametrize("mode", ["revoke", "expire"])
def test_revocation_and_expiry_hold_before_and_after_resource_admission(request, kind, mode, monkeypatch):
    fixture = request.getfixturevalue("workflow" if kind == "preparation" else "resolution_workflow")
    grants, store, _, *_ = fixture
    grant, key = issued(fixture, kind)
    job = start(fixture, grant, key)
    assert store.check_authority(job["job_id"])
    if mode == "revoke": grants.revoke(grant["grant_id"])
    else: monkeypatch.setattr("meecp.management_workflow_grants.time.time", lambda: grant["expires_at"] + 1)
    code = "management_execution_revoked" if mode == "revoke" else "management_execution_expired"
    with pytest.raises(PreparationError, match="^"+code+"$"):
        store.check_authority(job["job_id"])
    with pytest.raises(GrantError): start(fixture, grant, key)
    assert store.get(job["job_id"])["state"] == "queued"


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
def test_association_publication_rolls_back_with_job_admission(request, kind, monkeypatch):
    fixture = request.getfixturevalue("workflow" if kind == "preparation" else "resolution_workflow")
    grants, store, _, *_ = fixture
    grant, key = issued(fixture, kind)
    original = grants.bind_job
    def reject(*args):
        original(*args)
        raise GrantError("synthetic_admission_rollback")
    monkeypatch.setattr(grants, "bind_job", reject)
    with pytest.raises(GrantError, match="^synthetic_admission_rollback$"):
        start(fixture, grant, key)
    assert store.list() == [] and grants.inspect(grant["grant_id"])["job_id"] is None
    assert grants.db.execute("SELECT count(*) FROM workflow_management_jobs").fetchone()[0] == 0
    assert grants.db.execute("SELECT count(*) FROM workflow_management_job_kinds").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["plan_digest", "descriptor", "policy", "source", "runtime", "recipe"])
def test_preparation_entire_captured_plan_is_current(workflow, damage):
    grants, store, plan, imports, *_ = workflow
    grant, key = issued(workflow)
    current = store._execution_plan(plan["plan_id"])
    if damage == "plan_digest": current["plan_digest"] = "d" * 64
    elif damage == "descriptor": current["descriptor"]["package"]["name"] = "Changed"
    elif damage == "policy": current["execution_policy"]["wall_seconds"] += 1
    elif damage == "source": current["source_revision"] = "sha256:" + "d" * 64
    elif damage == "runtime": current["runtime_digest"] = "d" * 64
    elif damage == "recipe": current["descriptor"]["recipe"]["digest"] = "d" * 64
    with imports._transaction():
        store.db.execute("UPDATE preparation_execution_plans SET data=? WHERE plan_id=?", (canonical_bytes(current).decode(), plan["plan_id"]))
    with pytest.raises((GrantError, PreparationError)):
        start(workflow, grant, key)
    assert store.list() == []


def test_recipe_grant_exact_bundle_action_and_transaction_fence(workflow, tmp_path):
    grants, _, _, _, recipes, _ = workflow
    bundle = recipe_bundle(tmp_path / "new-recipe", recipe_id="test.exact-grant")
    checked = validate_bundle(bundle)
    payload = {"mode": "recipe-management", "recipe_id": checked["manifest"]["id"], "version": checked["manifest"]["version"],
               "digest": checked["digest"], "actions": ["register", "enable"], "path": str(bundle)}
    grant = grants.issue(payload)
    admit = lambda record: grants.recipe_admit(grant["grant_id"], "register", record)
    record = recipes.add(bundle, admit=admit)
    assert record["state"] == "proposed" and record["runnable"] is False
    enabled = recipes.enable(record["recipe_id"], record["version"],
        admit=lambda record: grants.recipe_admit(grant["grant_id"], "enable", record))
    assert enabled["state"] == "enabled" and enabled["runnable"] is False
    with pytest.raises(GrantError):
        recipes.disable(record["recipe_id"], record["version"],
            admit=lambda record: grants.recipe_admit(grant["grant_id"], "disable", record))
    grants.revoke(grant["grant_id"])
    with pytest.raises(GrantError, match="^grant_revoked$"):
        recipes.enable(record["recipe_id"], record["version"],
            admit=lambda record: grants.recipe_admit(grant["grant_id"], "enable", record))
    assert recipes.inspect(record["recipe_id"], record["version"])["epoch"] == enabled["epoch"]


def test_recipe_changed_after_grant_never_registers(workflow, tmp_path):
    grants, _, _, _, recipes, _ = workflow
    bundle = recipe_bundle(tmp_path / "changed-recipe", recipe_id="test.changed-grant")
    bundle.chmod(0o700)
    for member in bundle.iterdir():
        member.chmod(0o600)
    checked = validate_bundle(bundle)
    grant = grants.issue({"mode":"recipe-management", "recipe_id":checked["manifest"]["id"], "version":"1.0.0",
        "digest":checked["digest"], "actions":["register"], "path":str(bundle)})
    from meecp.recipe_sdk import relock_recipe
    (bundle / "README.md").write_text("Changed harmless fixture.\n")
    relock_recipe(bundle)
    with pytest.raises(GrantError, match="^grant_scope_changed$"):
        recipes.add(bundle, admit=lambda record:grants.recipe_admit(grant["grant_id"],"register",record))
    assert not any(row["recipe_id"] == "test.changed-grant" for row in recipes.list())


def test_revocation_during_off_loop_input_validation_rolls_back(workflow, monkeypatch):
    grants, store, _, *_ = workflow
    grant, key = issued(workflow)
    reached, release = threading.Event(), threading.Event()
    original = store._bound_descriptor
    def paused(*args, **kwargs):
        value = original(*args, **kwargs)
        reached.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(store, "_bound_descriptor", paused)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(start, workflow, grant, key)
        assert reached.wait(5)
        try: grants.revoke(grant["grant_id"])
        finally: release.set()
        with pytest.raises(GrantError, match="^grant_revoked$"): future.result(5)
    assert store.list() == [] and grants.inspect(grant["grant_id"])["job_id"] is None


async def test_revoked_grant_denies_actual_native_creation(workflow, group_reaper):
    from meecp.preparation_runner import JournaledPreparationOwner
    grants, store, _, imports, *_ = workflow
    grant, key = issued(workflow)
    job = start(workflow, grant, key)
    workspace = imports.root / "preparations" / job["job_id"]
    workspace.mkdir(parents=True, mode=0o700)
    store.transition(job["job_id"], "snapshotting")
    store.set_workspace(job["job_id"], workspace)
    store.transition(job["job_id"], "preparing")
    owner = JournaledPreparationOwner(group_reaper, store, job["job_id"], workspace)
    try:
        grants.revoke(grant["grant_id"])
        with pytest.raises(PreparationError, match="^management_execution_revoked$"):
            await owner.launch(Path(sys.executable), ["-c", "pass"], workspace, {}, owned_group=True)
        assert not owner.instances and not store.ownership_receipts(job["job_id"])
    finally:
        await owner.close()


@pytest.mark.parametrize("mode",["revoke","expire"])
async def test_actual_owned_child_authority_loss_joins_before_reservation_release(controlled, monkeypatch, mode):
    runner, imports, recipes, draft, raw, resources, released = controlled
    grants = WorkflowGrantStore(runner.store, ResolutionStore(imports), recipes)
    plan = await planned(controlled)
    key = str(uuid.uuid4())
    grant = grants.issue({"mode":"preparation-execution","plan_id":plan["plan_id"],"plan_digest":plan["plan_digest"],"idempotency_key":key})
    reached = asyncio.Event();owned=[]
    async def held_build(attempt, descriptor, draft, toolchain, candidate, executor):
        execution = asyncio.create_task(runner._controlled(attempt, executor.execute(Path(sys.executable),
            ["-c","import time;time.sleep(60)"],attempt.workspace,{},timeout_seconds=60)))
        try:
            async with asyncio.timeout(5):
                while not executor.owner.instances:
                    await asyncio.sleep(.01)
            owned.extend(executor.owner.instances.values());reached.set()
            await execution
            pytest.fail("revoked_child_completed_build")
        finally:
            if not execution.done():execution.cancel()
            await asyncio.gather(execution,return_exceptions=True)
    monkeypatch.setattr(runner,"_build",held_build)
    try:
        job = await runner.start(plan["plan_id"],plan["plan_digest"],key,grant_id=grant["grant_id"])
        await asyncio.wait_for(reached.wait(),5)
        if mode=="revoke":grants.revoke(grant["grant_id"])
        else:monkeypatch.setattr("meecp.management_workflow_grants.time.time",lambda:grant["expires_at"]+1)
        final = await finished(runner,job["job_id"])
        code="management_execution_revoked" if mode=="revoke" else "management_execution_expired"
        assert final["error_code"]==code and final["state"]=="failed"
        assert final["ownership_settled"] and not final["reservation_retained"]
        assert all(process.returncode is not None for process in owned)
        assert not resources and released==[job["job_id"]] and not runner.store.pending_ownership(job["job_id"])
        assert imports.inspect(draft["draft_id"])["attachment"] is None
    finally:
        await runner.close()


async def test_real_sdk_static_recipe_surface_and_legacy_execution_denials(tmp_path, prepared):
    _, reaper = prepared
    async with private_manager(tmp_path, reaper) as host:
        grant = await execute(host.root, "authorize", "--prepare")
        async with sdk(host.root, grant["credential_file"], tmp_path / "sdk-stderr") as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            required = {"meecp_list_recipes", "meecp_get_recipe", "meecp_get_recipe_schema", "meecp_register_recipe",
                "meecp_start_preparation", "meecp_cancel_preparation", "meecp_plan_resolution", "meecp_start_resolution"}
            assert required <= names and not {"meecp_issue_grant", "meecp_revoke_grant"} & names
            result = await session.call_tool("meecp_list_recipes", {})
            assert not result.isError and result.structuredContent["recipes"]
            record = result.structuredContent["recipes"][0]
            inspected = await session.call_tool("meecp_get_recipe", {"recipe_id": record["recipe_id"], "version": record["version"]})
            assert not inspected.isError and inspected.structuredContent["digest"] == record["digest"]
            schema = await session.call_tool("meecp_get_recipe_schema", {})
            assert not schema.isError and schema.structuredContent["manifest"]["additionalProperties"] is False
            key = str(uuid.uuid4())
            for name,args in (("meecp_start_preparation", {"plan_id":key,"plan_digest":"a"*64,"idempotency_key":key}),
                              ("meecp_start_resolution", {"plan_id":key,"plan_digest":"a"*64,"idempotency_key":key}),
                              ("meecp_cancel_preparation", {"job_id":key}),
                              ("meecp_enable_recipe", {"recipe_id":record["recipe_id"],"version":record["version"]})):
                denied = await session.call_tool(name,args)
                assert denied.isError and denied.structuredContent["error"]["code"] == "grant_scope"
            assert host.preparations.list() == host.resolutions.list() == []
            assert host.registry.list_slots() == [] and not host.workers and not host.owners


async def test_real_sdk_recipe_grant_exact_static_registration_no_admin_or_execution(tmp_path, prepared):
    _, reaper = prepared
    bundle=recipe_bundle(tmp_path / "sdk-recipe",recipe_id="test.sdk-grant")
    checked=validate_bundle(bundle)
    async with private_manager(tmp_path / "manager", reaper) as host:
        grant=await execute(host.root,"authorize","--recipe",checked["manifest"]["id"],"--recipe-version","1.0.0",
            "--recipe-digest",checked["digest"],"--recipe-action","register","--recipe-action","enable","--recipe-path",str(bundle))
        assert grant["mode"]=="recipe-management"
        async with sdk(host.root,grant["credential_file"],tmp_path / "recipe-sdk-stderr") as session:
            await session.initialize()
            added=await session.call_tool("meecp_register_recipe",{"path":str(bundle)})
            assert not added.isError and added.structuredContent["digest"]==checked["digest"]
            assert added.structuredContent["state"]=="proposed" and added.structuredContent["runnable"] is False
            enabled=await session.call_tool("meecp_enable_recipe",{"recipe_id":checked["manifest"]["id"],"version":"1.0.0"})
            assert not enabled.isError and enabled.structuredContent["runnable"] is False
            denied=await session.call_tool("meecp_disable_recipe",{"recipe_id":checked["manifest"]["id"],"version":"1.0.0"})
            assert denied.isError and denied.structuredContent["error"]["code"]=="grant_scope"
            async with client(host,credential(grant)) as api:
                for path,body in (("/v1/grants",{"mode":"read"}),("/v1/secrets",{"name":"never","value":"never"}),
                    ("/v1/resolutions/start",{}),("/v1/imports/service",{}),("/v1/downloads",{})):
                    response=await api.post(path,json=body)
                    if path == "/v1/imports/service":
                        # The separately owned service route is absent at this
                        # checkpoint. Its dynamic GET import path returns 405.
                        assert response.status_code in {403,404,405}
                    else:
                        assert response.status_code == 403
            assert host.preparations.list()==host.resolutions.list()==[] and not host.registry.list_slots()


@pytest.mark.parametrize("mode", ["read", "prepare", "install", "lifecycle"])
async def test_all_legacy_modes_still_deny_execution_recipe_mutation_and_new_authority(tmp_path, prepared, mode):
    package,reaper=prepared
    async with private_manager(tmp_path,reaper) as host:
        payload={"mode":mode}
        if mode=="install":
            plan=host.plan_install({"package":str(package)})
            payload["plan_id"]=plan["plan_id"]
            grant=host.grants.issue(payload,plan=plan)
        elif mode=="lifecycle":
            slot=registered(host)
            grant=host.grants.issue({**payload,"slot_ids":[slot.id],"actions":["stop"]})
        else:grant=host.grants.issue(payload)
        identifier=str(uuid.uuid4())
        async with client(host,credential(grant)) as api:
            for route,body in (("/v1/preparations/start",{"plan_id":identifier,"plan_digest":"a"*64,"idempotency_key":identifier}),
                ("/v1/resolutions/start",{"plan_id":identifier,"plan_digest":"a"*64,"idempotency_key":identifier}),
                ("/v1/preparations/"+identifier+"/cancel",{}),("/v1/resolutions/"+identifier+"/cancel",{}),
                ("/v1/recipes",{"path":str(tmp_path)}),("/v1/recipes/enable",{"recipe_id":"test.no-authority","version":"1.0.0"}),
                ("/v1/grants",{"mode":"preparation-execution","plan_id":identifier,"plan_digest":"a"*64,"idempotency_key":identifier})):
                result=await api.post(route,json=body)
                assert result.status_code==403 and result.json()["error"]["code"]=="grant_scope"
        assert host.preparations.list()==host.resolutions.list()==[]


def test_pre_upgrade_owner_record_remains_byte_preserved(workflow):
    grants,store,plan,imports,*_=workflow
    job=store.authorize_start(plan["plan_id"],plan["plan_digest"],str(uuid.uuid4()))
    legacy={key:value for key,value in job.items() if key!="management_authority"}
    legacy["schema_version"]=1
    encoded=canonical_bytes(legacy).decode()
    with imports._transaction():
        store.db.execute("DELETE FROM workflow_management_job_kinds WHERE job_id=?",(job["job_id"],))
        store.db.execute("UPDATE preparation_jobs SET data=? WHERE job_id=?",(encoded,job["job_id"]))
    assert store.check_authority(job["job_id"])["schema_version"]==1
    assert store.db.execute("SELECT data FROM preparation_jobs WHERE job_id=?",(job["job_id"],)).fetchone()[0]==encoded


@pytest.mark.parametrize("version",[0,1])
def test_import_database_migrates_transactionally_without_rewriting_drafts(tmp_path,version):
    folder=tmp_path/"migration-source";folder.mkdir()
    (folder/"README.md").write_text("Synthetic preserved source.\n")
    state=tmp_path/"migration-state"
    with ImportStore(state) as store:
        draft=store.create(folder,name="Preserved draft")
        data=store._connection.execute("SELECT data FROM drafts").fetchone()[0]
        revisions=store._connection.execute("SELECT data FROM revisions").fetchall()
        store._connection.execute("PRAGMA user_version="+str(version))
    with ImportStore(state) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0]==2
        assert reopened._connection.execute("SELECT data FROM drafts").fetchone()[0]==data
        assert [row[0] for row in reopened._connection.execute("SELECT data FROM revisions")]==[row[0] for row in revisions]
        assert reopened.inspect(draft["draft_id"])["schema_version"]==1


def test_import_database_migration_failure_rolls_back_version_and_bytes(tmp_path,monkeypatch):
    folder=tmp_path/"rollback-source";folder.mkdir()
    (folder/"README.md").write_text("Synthetic preserved source.\n")
    state=tmp_path/"rollback-state"
    with ImportStore(state) as store:
        store.create(folder,name="Preserved draft")
        data=store._connection.execute("SELECT data FROM drafts").fetchone()[0]
        store._connection.execute("PRAGMA user_version=1")
    original=ImportStore._transaction
    @contextlib.contextmanager
    def interrupted(self):
        with original(self):
            yield
            raise SourceImportError("synthetic_migration_rollback")
    monkeypatch.setattr(ImportStore,"_transaction",interrupted)
    with pytest.raises(SourceImportError,match="^synthetic_migration_rollback$"):
        ImportStore(state)
    with sqlite3.connect(state/"imports.sqlite") as reader:
        assert reader.execute("PRAGMA user_version").fetchone()[0]==1
        assert reader.execute("SELECT data FROM drafts").fetchone()[0]==data


def test_newer_import_database_is_rejected_without_rewriting_state(tmp_path):
    state=tmp_path/"newer-state"
    with ImportStore(state) as store:
        store._connection.execute("PRAGMA user_version=3")
    before=(state/"imports.sqlite").read_bytes()
    with pytest.raises(SourceImportError,match="^unsupported_import_schema$"):
        ImportStore(state)
    assert (state/"imports.sqlite").read_bytes()==before


def test_actual_original_774_reader_denies_new_authority_database(workflow):
    """Fresh process loads original Git bytes; it cannot read schema-2 jobs."""
    _,store,_,imports,*_=workflow
    grant,key=issued(workflow)
    job=start(workflow,grant,key)
    original=store.db.execute("SELECT data FROM preparation_jobs WHERE job_id=?",(job["job_id"],)).fetchone()[0]
    checkout=Path(__file__).resolve().parents[1]
    code="""import json,pathlib,subprocess,sys,types
root=pathlib.Path(sys.argv[1]); state=pathlib.Path(sys.argv[2])
sys.path[:0]=[str(root/'backend'),str(root/'vendor')]
data=subprocess.run(['git','show','774b43cd2688be8ce8138c5a3d516dbc09ea3f3b:backend/mcpme/imports.py'],cwd=root,capture_output=True,check=True).stdout
module=types.ModuleType('meecp._original_imports_774');sys.modules[module.__name__]=module
exec(compile(data,'original-imports-774','exec'),module.__dict__)
try:
 reader=module.ImportStore(state)
except module.ImportError as error:
 print(json.dumps({'code':error.code,'original_draft_version':module.IMPORT_SCHEMA_VERSION}))
else:
 reader.close();sys.exit(2)
"""
    result=subprocess.run([sys.executable,"-B","-c",code,str(checkout),str(imports.root)],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert json.loads(result.stdout)=={"code":"unsupported_import_schema","original_draft_version":1}
    assert store.db.execute("SELECT data FROM preparation_jobs WHERE job_id=?",(job["job_id"],)).fetchone()[0]==original


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
def test_bound_grant_and_original_job_survive_connection_restart(request,kind):
    fixture=request.getfixturevalue("workflow" if kind=="preparation" else "resolution_workflow")
    grants,store,plan,imports,*_=fixture
    grant,key=issued(fixture,kind)
    token=credential(grant)
    job=start(fixture,grant,key)
    table="preparation_jobs" if kind=="preparation" else "resolution_jobs"
    original=store.db.execute("SELECT data FROM "+table+" WHERE job_id=?",(job["job_id"],)).fetchone()[0]
    with ImportStore(imports.root) as reopened:
        recipes=RecipeStore(reopened)
        resolutions=ResolutionStore(reopened)
        preparations=PreparationStore(reopened,recipes,resolutions=resolutions)
        recovered=WorkflowGrantStore(preparations,resolutions,recipes)
        selected=preparations if kind=="preparation" else resolutions
        assert recovered.authenticate(token)["grant_id"]==grant["grant_id"]
        assert selected.authorize_start(plan["plan_id"],plan["plan_digest"],key,grant_id=grant["grant_id"])==job
        assert selected.db.execute("SELECT data FROM "+table+" WHERE job_id=?",(job["job_id"],)).fetchone()[0]==original
        recovered.revoke(grant["grant_id"])
    with pytest.raises(PreparationError,match="^management_execution_revoked$"):
        store.check_authority(job["job_id"])


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
def test_revocation_cleanup_keeps_original_binding_after_expiry_or_plan_change(request,kind,monkeypatch):
    fixture=request.getfixturevalue("workflow" if kind=="preparation" else "resolution_workflow")
    grants,store,plan,imports,*_=fixture
    grant,key=issued(fixture,kind)
    job=start(fixture,grant,key)
    with imports._transaction():
        table="preparation_execution_plans" if kind=="preparation" else "resolution_plans"
        changed={**plan,"expires_at":0}
        store.db.execute("UPDATE "+table+" SET data=? WHERE plan_id=?",(canonical_bytes(changed).decode(),plan["plan_id"]))
    monkeypatch.setattr("meecp.management_workflow_grants.time.time",lambda:grant["expires_at"]+1)
    grants.revoke(grant["grant_id"])
    assert grants.cleanup_job(grant["grant_id"])==(kind,job["job_id"])
    with pytest.raises(PreparationError,match="^management_execution_revoked$"):
        store.check_authority(job["job_id"])


@pytest.mark.parametrize("kind", ["preparation", "resolution"])
@pytest.mark.parametrize("damage", ["association_missing","kind_missing","job_changed","binding_changed","marker_changed"])
def test_revocation_cleanup_cannot_cancel_replaced_or_unassociated_job(request,kind,damage):
    fixture=request.getfixturevalue("workflow" if kind=="preparation" else "resolution_workflow")
    grants,store,_,imports,*_=fixture
    grant,key=issued(fixture,kind)
    job=start(fixture,grant,key)
    with imports._transaction():
        if damage=="association_missing":grants.db.execute("DELETE FROM workflow_management_jobs")
        elif damage=="kind_missing":grants.db.execute("DELETE FROM workflow_management_job_kinds")
        elif damage=="job_changed":grants.db.execute("UPDATE workflow_management_grants SET job_id=?",(str(uuid.uuid4()),))
        elif damage=="binding_changed":grants.db.execute("UPDATE workflow_management_jobs SET binding_digest=?",("f"*64,))
        else:
            job["management_authority"]=OWNER_AUTHORITY
            store._save(job)
    grants.revoke(grant["grant_id"])
    with pytest.raises((GrantError,PreparationError)):
        grants.cleanup_job(grant["grant_id"])
    assert store.get(job["job_id"])["state"]=="queued"


async def test_real_sdk_bound_attempt_and_owner_revocation_joins_actual_child(tmp_path,prepared,monkeypatch):
    """Private injected runner tests MCP control flow, never public admission.

    The only executed workload is a synthetic sleeping Python child. It does
    not build/probe a package or contact a provider/model service.
    """
    from meecp.preparation_runner import PreparationRunner
    _,reaper=prepared
    folder=tmp_path/"sdk-source";folder.mkdir()
    (folder/"validator.py").write_text("def validate_json(document): return {'valid':True}\n")
    (folder/"LICENSE").write_text("Synthetic permission notice.\n")
    (folder/"pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    lock=b"version=1\nrevision=3\n";(folder/"uv.lock").write_bytes(lock)
    monkeypatch.setattr("meecp.preparation_runner.load_private_toolchain",lambda *args:SimpleNamespace(identity="d"*64,uv=tmp_path/"unused-uv"))
    async with private_manager(tmp_path/"sdk-host",reaper) as host:
        resources={};released=[];owned=[];reached=asyncio.Event()
        async def reserve(identifier,policy):resources[identifier]=policy
        async def release(identifier,*,ownership_settled):
            assert ownership_settled
            resources.pop(identifier,None);released.append(identifier)
        await host.preparation_runner.close()
        runner=PreparationRunner(host.preparations,tmp_path/"private-host",reaper,enabled=True,
            reserve_resources=reserve,release_resources=release)
        host.preparation_runner=runner
        async def held_build(attempt,value,draft,toolchain,candidate,executor):
            execution=asyncio.create_task(runner._controlled(attempt,executor.execute(Path(sys.executable),
                ["-c","import time;time.sleep(60)"],attempt.workspace,{},timeout_seconds=60)))
            try:
                async with asyncio.timeout(5):
                    while not executor.owner.instances:await asyncio.sleep(.01)
                owned.extend(executor.owner.instances.values());reached.set()
                await execution
                pytest.fail("held_child_completed_build")
            finally:
                if not execution.done():execution.cancel()
                await asyncio.gather(execution,return_exceptions=True)
        monkeypatch.setattr(runner,"_build",held_build)
        recipe=host.recipes.add(recipe_bundle(tmp_path/"sdk-owned-recipe"))
        recipe=host.recipes.enable(recipe["recipe_id"],recipe["version"])
        draft=host.imports.create(folder,name="Synthetic SDK child")
        raw=descriptor(recipe,hashlib.sha256(lock).hexdigest())
        plan=await runner.plan(draft["draft_id"],1,raw,policy())
        key=str(uuid.uuid4())
        grant=await execute(host.root,"authorize","--preparation-plan",plan["plan_id"],"--plan-digest",plan["plan_digest"],"--idempotency-key",key)
        async with sdk(host.root,grant["credential_file"],tmp_path/"owned-sdk-stderr") as session:
            await session.initialize()
            payload={"plan_id":plan["plan_id"],"plan_digest":plan["plan_digest"],"idempotency_key":key}
            first=await session.call_tool("meecp_start_preparation",payload)
            assert not first.isError
            identifier=first.structuredContent["job_id"]
            await asyncio.wait_for(reached.wait(),5)
            second=await session.call_tool("meecp_start_preparation",payload)
            assert not second.isError and second.structuredContent["job_id"]==identifier
            listed=await session.call_tool("meecp_list_preparations",{})
            assert not listed.isError and [job["job_id"] for job in listed.structuredContent["preparations"]]==[identifier]
            inspected=await session.call_tool("meecp_get_preparation",{"job_id":identifier})
            assert not inspected.isError and inspected.structuredContent["management_authority"]["grant_id"]==grant["grant_id"]
            denied=await session.call_tool("meecp_get_preparation",{"job_id":str(uuid.uuid4())})
            assert denied.isError and denied.structuredContent["error"]["code"]=="grant_scope"
            revoked=await execute(host.root,"grants","revoke",grant["grant_id"])
            assert revoked["revoked"]
        final=host.preparations.get(identifier)
        assert final["state"]=="canceled" and final["ownership_settled"] and not final["reservation_retained"]
        assert all(process.returncode is not None for process in owned)
        assert not resources and released==[identifier] and not host.preparations.pending_ownership(identifier)
        assert host.imports.inspect(draft["draft_id"])["attachment"] is None and not host.registry.list_slots()


async def test_real_sdk_exact_execution_grant_cannot_enable_disabled_runner(tmp_path,prepared):
    _,reaper=prepared
    folder=tmp_path/"static-source";folder.mkdir()
    (folder/"validator.py").write_text("def validate_json(document): return {'valid':True}\n")
    (folder/"LICENSE").write_text("Synthetic permission notice.\n")
    (folder/"pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    lock=b"version=1\nrevision=3\n";(folder/"uv.lock").write_bytes(lock)
    async with private_manager(tmp_path/"closed-host",reaper) as host:
        recipe=host.recipes.add(recipe_bundle(tmp_path/"closed-recipe"))
        recipe=host.recipes.enable(recipe["recipe_id"],recipe["version"])
        draft=host.imports.create(folder,name="Synthetic closed host")
        raw=descriptor(recipe,hashlib.sha256(lock).hexdigest())
        plan=host.preparations.execution_plan(draft["draft_id"],1,raw,policy(),runner_digest="a"*64,verifier_digest="b"*64,runtime_digest="c"*64)
        key=str(uuid.uuid4())
        grant=await execute(host.root,"authorize","--preparation-plan",plan["plan_id"],"--plan-digest",plan["plan_digest"],"--idempotency-key",key)
        assert grant["mode"]=="preparation-execution"
        async with sdk(host.root,grant["credential_file"],tmp_path/"execution-sdk-stderr") as session:
            await session.initialize()
            wrong=await session.call_tool("meecp_start_preparation",{"plan_id":plan["plan_id"],"plan_digest":plan["plan_digest"],"idempotency_key":str(uuid.uuid4())})
            assert wrong.isError and wrong.structuredContent["error"]["code"]=="grant_scope"
            exact=await session.call_tool("meecp_start_preparation",{"plan_id":plan["plan_id"],"plan_digest":plan["plan_digest"],"idempotency_key":key})
            assert exact.isError and exact.structuredContent["error"]["code"]=="preparation_runner_unavailable"
            inspected=await session.call_tool("meecp_inspect_import",{"draft_id":draft["draft_id"]})
            assert not inspected.isError and inspected.structuredContent["source"]==draft["source"]
            listed=await session.call_tool("meecp_list_preparations",{})
            assert not listed.isError and listed.structuredContent["preparations"]==[]
        assert host.preparations.list()==host.resolutions.list()==[] and host.workflow_grants.inspect(grant["grant_id"])["job_id"] is None
        assert not host.workers and not host.owners and not host.registry.list_slots()


@pytest.mark.parametrize("name", ["meecp_start_preparation", "meecp_start_resolution", "meecp_register_recipe", "meecp_get_recipe"])
async def test_facade_unknown_fields_do_not_reach_api(name):
    called=False
    async def caller(*args,**kwargs):
        nonlocal called
        called=True
    facade=Management(Path("unused"),Path("unused"),caller=caller)
    result=await facade.invoke(name,{"unexpected":"synthetic-private-sentinel"})
    assert result.isError and result.structuredContent["error"]["code"]=="invalid_tool_arguments" and not called
    assert "synthetic-private-sentinel" not in result.model_dump_json()
