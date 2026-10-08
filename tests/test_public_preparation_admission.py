"""Closed public authority, ordinary resolution and truthful live discovery."""
import copy
from pathlib import Path
from types import SimpleNamespace
import uuid

import httpx
import pytest

from meecp.preparation_admission import PublicPreparationAdmission, public_preparation_admission
from meecp.preparation_components import ComponentReference
from meecp.preparation_contracts import PreparationError, canonical_bytes
from meecp.preparation_inputs import parse_preparation_descriptor
from meecp.preparation_resolution import ResolutionStore, result_from_files
from meecp.preparation_resolution_runner import OwnedResolutionRunner
from meecp.preparation_runner import PreparationRunner, _Attempt
from meecp.preparation_resolved_python import ResolvedPythonFamily
from meecp.supervisor import Supervisor, capabilities, management_app
from test_meecp_preparations import foundation
from test_preparation_resolution import source, request, policy, dependency_project


def recipe_keys(recipes, record):
    return frozenset((*key.rsplit("@", 1), item["digest"])
        for key, item in recipes._component_closure(record["manifest"]).items())


def legacy_admission(recipes, raw):
    record = recipes.inspect(raw["recipe"]["id"], raw["recipe"]["version"])
    return PublicPreparationAdmission(frozenset(), frozenset({recipe_keys(recipes, record)}))


def ordinary_admission(kinds=("python-callable",)):
    return PublicPreparationAdmission(frozenset(), frozenset(), frozenset(kinds))


@pytest.mark.parametrize("damage", ["mutable", "empty_set", "shared_empty_set", "key", "digest", "agent", "schema"])
def test_admission_is_typed_immutable_and_closed(damage):
    values = dict(families=frozenset(), recipe_component_sets=frozenset(), resolution_candidate_kinds=frozenset())
    if damage == "mutable": values["families"] = set()
    if damage == "empty_set": values["recipe_component_sets"] = frozenset({frozenset()})
    if damage == "shared_empty_set": values["recipe_component_sets"] = frozenset({values["families"]})
    if damage == "key": values["families"] = frozenset({("python",)})
    if damage == "digest": values["families"] = frozenset({("python", "1.0.0", "invalid")})
    if damage == "agent": values["resolution_candidate_kinds"] = frozenset({"agent-adapter"})
    if damage == "schema": values["families"] = frozenset({("python", True, "a" * 64)})
    with pytest.raises(PreparationError, match="invalid_public_preparation_admission"):
        PublicPreparationAdmission(**values)


async def test_schema1_requires_exact_closed_set_and_allows_independent_recipe_id(foundation, tmp_path):
    imports, recipes, store, draft, raw = foundation
    admission = legacy_admission(recipes, raw)
    assert raw["recipe"]["id"] == "test.synthetic"
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True, public_admission=admission)
    record = recipes.inspect(raw["recipe"]["id"], raw["recipe"]["version"])
    assert runner.execution_available_for_recipe(record)
    assert runner._require_admission(parse_preparation_descriptor(raw)) is None
    denied = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True,
        public_admission=PublicPreparationAdmission(frozenset(), frozenset()))
    assert not denied.execution_available_for_recipe(record)
    with pytest.raises(PreparationError, match="preparation_recipe_not_admitted"):
        denied._require_admission(parse_preparation_descriptor(raw))
    changed = copy.deepcopy(record)
    changed["manifest"]["steps"].append({"component": "existing-mcp-v1", "version": "1.0.0",
        "digest": recipes.component_catalog["existing-mcp-v1@1.0.0"]["digest"], "config": {}})
    assert not runner.execution_available_for_recipe(changed)
    changed = copy.deepcopy(record)
    changed["manifest"]["steps"][0]["digest"] = "f" * 64
    assert not runner.execution_available_for_recipe(changed)
    stale = copy.deepcopy(raw)
    stale["recipe"]["digest"] = "0" * 64
    with pytest.raises(PreparationError, match="preparation_recipe_not_admitted"):
        runner._require_admission(parse_preparation_descriptor(stale))
    await runner.close()
    await denied.close()


async def test_complete_transitive_closure_is_required(foundation, tmp_path):
    _, recipes, store, _, raw = foundation
    admission = legacy_admission(recipes, raw)
    record = recipes.inspect(raw["recipe"]["id"], raw["recipe"]["version"])
    component = recipes.component_catalog["python-function-v1@1.0.0"]
    component["dependencies"] = [{"component": "existing-mcp-v1", "version": "1.0.0",
        "digest": recipes.component_catalog["existing-mcp-v1@1.0.0"]["digest"]}]
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True, public_admission=admission)
    assert not runner.execution_available_for_recipe(record)
    with pytest.raises(PreparationError, match="preparation_recipe_not_admitted"):
        runner._require_admission(parse_preparation_descriptor(raw))
    await runner.close()


async def test_composite_family_and_legacy_recipe_have_independent_gates(foundation, tmp_path):
    _, recipes, store, _, raw = foundation
    family = next(value for value in store.families.values() if type(value) is ResolvedPythonFamily)
    key = family.component.reference.key
    record = {"manifest": {"steps": [{"component": key[0], "version": key[1], "digest": key[2]}]}}
    admission = PublicPreparationAdmission(frozenset({key}), frozenset({frozenset({key})}))
    runner = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True, enabled_families={key}, public_admission=admission)
    assert runner.execution_available_for_recipe(record)
    legacy = recipes.inspect(raw["recipe"]["id"], raw["recipe"]["version"])
    assert not runner.execution_available_for_recipe(legacy)
    with pytest.raises(PreparationError, match="preparation_recipe_not_admitted"):
        runner._require_admission(parse_preparation_descriptor(raw))
    unadmitted = PreparationRunner(store, tmp_path, tmp_path / "reaper", enabled=True,
        public_admission=legacy_admission(recipes, raw))
    assert not unadmitted.execution_available_for_recipe(record)
    wrong = copy.deepcopy(record)
    wrong["manifest"]["steps"][0]["version"] = "1.0.0"
    assert not runner.execution_available_for_recipe(wrong)
    mixed = copy.deepcopy(record)
    mixed["manifest"]["steps"] += legacy["manifest"]["steps"]
    assert not runner.execution_available_for_recipe(mixed)
    await runner.close()
    await unadmitted.close()


async def test_recipe_policy_changes_execution_producer_identity(foundation, tmp_path):
    _, recipes, store, _, raw = foundation
    descriptor = parse_preparation_descriptor(raw)
    admitted = legacy_admission(recipes, raw)
    extra = frozenset({("existing-mcp-v1", "1.0.0", recipes.component_catalog["existing-mcp-v1@1.0.0"]["digest"])})
    broader = PublicPreparationAdmission(frozenset(), admitted.recipe_component_sets | {extra})
    (tmp_path / "unused").write_bytes(b"Synthetic digest-only native helper fixture")
    (tmp_path / "unused").chmod(0o700)
    first = PreparationRunner(store, tmp_path, tmp_path / "unused", enabled=True, public_admission=admitted)
    second = PreparationRunner(store, tmp_path, tmp_path / "unused", enabled=True, public_admission=broader)
    one = await first._implementation_identities(descriptor, "a" * 64)
    two = await second._implementation_identities(descriptor, "a" * 64)
    assert one[0] != two[0] and one[1] == two[1]
    await first.close()
    await second.close()


async def test_host_defaults_and_live_capabilities_match_actual_runners(tmp_path):
    assert public_preparation_admission() is None
    assert not capabilities()["automatic_preparation"]
    default = Supervisor(tmp_path / "default", tmp_path / "reaper")
    try:
        assert not capabilities(default)["recipe_execution"]
        assert not capabilities(default)["preparation_resolution"]["execution"]
    finally:
        await default.close()
    admission = ordinary_admission()
    host = Supervisor(tmp_path / "public", tmp_path / "reaper", public_admission=admission)
    try:
        assert type(host.resolutions) is ResolutionStore
        assert type(host.resolution_runner) is OwnedResolutionRunner
        assert not host.preparation_runner.enabled
        assert not capabilities(host)["automatic_preparation"]
        assert capabilities(host)["preparation_resolution"]["candidate_kinds"] == ["python-callable"]
        app = management_app(host, "synthetic-test-token", 19999)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:19999") as client:
            response = await client.get("/v1/capabilities", headers={"Authorization": "Bearer synthetic-test-token"})
        assert response.status_code == 200 and response.json()["preparation_resolution"]["execution"]
        await host.resolution_runner.close()
        assert not capabilities(host)["preparation_resolution"]["execution"]
    finally:
        await host.close()


async def test_host_admits_custom_recipe_and_never_mixes_public_and_private_factories(tmp_path, foundation):
    _, recipes, _, _, raw = foundation
    admission = legacy_admission(recipes, raw)
    with pytest.raises(PreparationError, match="invalid_public_preparation_admission"):
        Supervisor(tmp_path / "bad", tmp_path / "reaper", public_admission=admission,
            preparation_runner_factory=lambda *args, **kwargs: None)
    host = Supervisor(tmp_path / "host", tmp_path / "reaper", public_admission=admission)
    try:
        record = recipes.inspect(raw["recipe"]["id"], raw["recipe"]["version"])
        added = host.recipes.add(Path(record["path"]))
        host.recipes.enable(added["recipe_id"], added["version"])
        caps = capabilities(host)
        assert caps["automatic_preparation"] and caps["recipe_execution"]
        assert caps["preparation_execution"]["families"] == []
        assert "application_directory" in caps["import_sources"]
        host.recipes.disable(added["recipe_id"], added["version"])
        # The independently shipped legacy recipe has the same exact closure.
        for item in host.recipes.list_summaries():
            if item["state"] == "enabled": host.recipes.disable(item["recipe_id"], item["version"])
        assert not capabilities(host)["automatic_preparation"]
    finally:
        await host.close()


@pytest.fixture
def public_resolution(source):
    imports, _, draft, candidate, folder = source
    admission = ordinary_admission()
    producer = [ComponentReference(id="python-resolution-v1", version="1.0.1", digest="b" * 64)]
    store = ResolutionStore(imports, public_admission=admission, current_producer=lambda: producer[0])
    req = request(draft, candidate).model_copy(update={"producer": producer[0]})
    return store, draft, candidate, req, producer


@pytest.mark.parametrize("damage", ["agent", "kind", "digest", "source", "producer", "future", "old"])
def test_public_resolution_plan_rejects_unreviewed_candidates_and_producers(public_resolution, damage):
    store, draft, candidate, req, producer = public_resolution
    selected = copy.deepcopy(candidate)
    if damage == "agent": selected["_agent_proposal_id"] = str(uuid.uuid4())
    if damage == "kind": req = req.model_copy(update={"candidate_kind": "python-mcp"})
    if damage == "digest": req = req.model_copy(update={"candidate_digest": "0" * 64})
    if damage == "source": req = req.model_copy(update={"source_revision": "sha256:" + "0" * 64})
    if damage == "producer": producer[0] = producer[0].model_copy(update={"digest": "c" * 64})
    if damage in {"future", "old"}: req = req.model_copy(update={"producer": req.producer.model_copy(
        update={"version": "1.0.2" if damage == "future" else "1.0.0"})})
    with pytest.raises(PreparationError): store.plan(req, selected, expires_in=60)
    assert not store.list() and store.db.execute("SELECT count(*) FROM resolution_plans").fetchone()[0] == 0


def test_start_rechecks_producer_and_keeps_plan_bytes(public_resolution):
    store, _, candidate, req, producer = public_resolution
    plan = store.plan(req, candidate, expires_in=60)
    before = store.db.execute("SELECT data FROM resolution_plans").fetchone()[0]
    producer[0] = producer[0].model_copy(update={"digest": "c" * 64})
    with pytest.raises(PreparationError, match="resolution_implementation_changed"):
        store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert not store.list() and store.db.execute("SELECT data FROM resolution_plans").fetchone()[0] == before


def test_start_fences_metadata_mutation_during_public_prevalidation(public_resolution, monkeypatch):
    store, _, candidate, req, _ = public_resolution
    plan = store.plan(req, candidate, expires_in=60)
    original = store.require_public_request
    def changed(request, candidate=None):
        original(request, candidate)
        damaged = copy.deepcopy(plan)
        damaged["request"]["candidate_digest"] = "0" * 64
        store.db.execute("UPDATE resolution_plans SET data=? WHERE plan_id=?",
            (canonical_bytes(damaged).decode(), plan["plan_id"]))
    monkeypatch.setattr(store, "require_public_request", changed)
    with pytest.raises(PreparationError, match="resolution_plan_mismatch"):
        store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert not store.list()


def published(public_resolution, tmp_path):
    store, draft, candidate, req, producer = public_resolution
    plan = store.plan(req, candidate, expires_in=60)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    workspace = tmp_path.resolve() / "workspace"
    directory = workspace / "derived"
    directory.mkdir(parents=True)
    (directory / "pyproject.toml").write_bytes(dependency_project(draft, candidate))
    (directory / "uv.lock").write_bytes(b'version=1\n')
    store.begin(job["job_id"], workspace)
    result = result_from_files(req, directory, [], None, 1024)
    return job, result


def test_publish_adoption_and_consumption_recheck_exact_producer(public_resolution, tmp_path):
    store, draft, candidate, req, producer = public_resolution
    job, result = published(public_resolution, tmp_path)
    producer[0] = producer[0].model_copy(update={"digest": "c" * 64})
    with pytest.raises(PreparationError, match="resolution_implementation_changed"):
        store.publish(job["job_id"], result)
    producer[0] = req.producer
    store.publish(job["job_id"], result)
    assert store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], result.digest)[0] == result
    store.imports.revise(draft["draft_id"], 1, name="Reviewed metadata")
    proof = store.prepare_result_binding(draft["draft_id"], 2, candidate["candidate_id"], result.digest, prior_revision=1)
    with store.imports._transaction(): store.commit_result_binding(proof, 2)
    assert store.resolve_result(draft["draft_id"], 2, candidate["candidate_id"], result.digest)[0] == result
    before = canonical_bytes(store.get(job["job_id"]))
    producer[0] = req.producer.model_copy(update={"digest": "c" * 64})
    with pytest.raises(PreparationError, match="resolution_implementation_changed"):
        store.prepare_result_binding(draft["draft_id"], 2, candidate["candidate_id"], result.digest)
    with pytest.raises(PreparationError, match="resolution_implementation_changed"):
        store.resolve_result(draft["draft_id"], 2, candidate["candidate_id"], result.digest)
    assert canonical_bytes(store.get(job["job_id"])) == before


async def test_before_execution_denies_changed_producer_before_launch(public_resolution, tmp_path):
    store, _, candidate, req, producer = public_resolution
    runner = OwnedResolutionRunner(store, tmp_path, tmp_path / "reaper", enabled=True,
        public_admission=store.public_admission)
    plan = store.plan(req, candidate, expires_in=60)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    producer[0] = req.producer.model_copy(update={"digest": "c" * 64})
    attempt = _Attempt(job["job_id"])
    attempt.check = lambda: None
    executor = SimpleNamespace(execute=lambda *args, **kwargs: pytest.fail("denied work launched"))
    with pytest.raises(PreparationError, match="resolution_implementation_changed"):
        await runner._execute(attempt, executor, SimpleNamespace(), [tmp_path / "command"])
    await runner.close()


async def test_public_ordinary_coordinator_publishes_checked_metadata_result(public_resolution, tmp_path, monkeypatch):
    store, draft, candidate, req, producer = public_resolution
    runner = OwnedResolutionRunner(store, tmp_path, tmp_path / "unused-reaper", enabled=True,
        public_admission=store.public_admission)
    monkeypatch.setattr(runner, "_producer_sync", lambda: producer[0])
    monkeypatch.setattr("meecp.preparation_resolution_runner.load_private_toolchain", lambda *args:
        SimpleNamespace(identity=req.runtime.digest))
    async def resolve(attempt, toolchain, current_draft, current_candidate, executor):
        assert current_candidate == candidate
        directory = attempt.workspace / "derived"
        directory.mkdir()
        (directory / "pyproject.toml").write_bytes(dependency_project(current_draft, current_candidate))
        (directory / "uv.lock").write_bytes(b"version=1\n")
        attempt.peak = 1024
        return directory
    monkeypatch.setattr(runner, "_resolve", resolve)
    try:
        plan = await runner.plan(draft["draft_id"], 1, candidate["candidate_id"], policy())
        assert plan["request"]["producer"]["version"] == "1.0.1"
        job = await runner.start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
        await runner.tasks[job["job_id"]]
        final = store.get(job["job_id"])
        assert final["state"] == "succeeded" and final["ownership_settled"] and not final["reservation_retained"]
        assert store.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], final["result_digest"])[0].request == req
    finally:
        await runner.close()


@pytest.mark.parametrize("version", ["1.0.0", "1.0.2"])
def test_public_build_consumption_keeps_rejected_historical_results_unchanged(source, tmp_path, version):
    imports, private, draft, candidate, _ = source
    req = request(draft, candidate)
    req = req.model_copy(update={"producer": req.producer.model_copy(update={"version": version})})
    plan = private.plan(req, candidate, expires_in=60)
    job = private.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    workspace = tmp_path.resolve() / "historical"
    derived = workspace / "derived"
    derived.mkdir(parents=True)
    (derived / "pyproject.toml").write_bytes(dependency_project(draft, candidate))
    (derived / "uv.lock").write_bytes(b"version=1\n")
    private.begin(job["job_id"], workspace)
    result = result_from_files(req, derived, [], None, 1024)
    private.publish(job["job_id"], result)
    before = canonical_bytes(private.get(job["job_id"]))
    public = ResolutionStore(imports, public_admission=ordinary_admission(), current_producer=lambda:
        ComponentReference(id="python-resolution-v1", version="1.0.1", digest="b" * 64))
    with pytest.raises(PreparationError, match="resolution_candidate_not_admitted"):
        public.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], result.digest)
    assert canonical_bytes(private.get(job["job_id"])) == before
    assert private.resolve_result(draft["draft_id"], 1, candidate["candidate_id"], result.digest)[0] == result


def test_source_hook_cannot_be_enabled_through_environment(monkeypatch):
    monkeypatch.setenv("MCPME_AUTOMATIC_PREPARATION", "1")
    monkeypatch.setenv("MCPME_PREPARATION_ADMISSION", '{"families":["*"]}')
    assert public_preparation_admission() is None
