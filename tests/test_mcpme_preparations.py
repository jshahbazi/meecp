"""Independent synthetic contract, authority and admission race fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcpme.contracts import validate_package
from mcpme.imports import ImportError, ImportStore
from mcpme.preparation_contracts import (
    ExecutionPolicy, PreparationDescriptor, PreparationError, PreparationReceipt,
    RecipeManifest, canonical_bytes, check_schema, contract_digest, load_contract, parse_descriptor, schema_accepts,
)
from mcpme.preparations import PreparationStore, RECEIPT_NAME
from mcpme.recipe_sdk import scaffold_recipe
from mcpme.recipes import RecipeStore
from test_mcpme_packages import edit_manifest, make_package, relock

ROOT = Path(__file__).resolve().parents[1]


def recipe_bundle(root, *, recipe_id="test.synthetic", version="1.0.0"):
    """An author outside the dispatcher creates a new declarative bundle."""
    root.mkdir()
    manifest = {
        "schema_version": 1, "id": recipe_id, "version": version, "kind": "declarative", "worker_api_version": 1,
        "input_schema": {"type": "object", "properties": {"label": {"type": "string", "maxLength": 64}},
                         "required": ["label"], "additionalProperties": False},
        "form": [{"field": "label", "label": "Synthetic task label", "kind": "string", "default": "Generated"}],
        "steps": [{"component": "python-function-v1", "version": "1.0.0",
                   "digest": hashlib.sha256((ROOT / "templates/python-function-v1/adapter.py").read_bytes()).hexdigest(), "config": {}}],
        "licenses": ["LICENSE"], "provenance": {"origin": "local synthetic author"},
    }
    files = {"LICENSE": b"Synthetic fixture permission notice.\n", "README.md": b"Independent synthetic recipe.\n"}
    for name, content in files.items():
        (root / name).write_bytes(content)
    raw = canonical_bytes(manifest)
    (root / "mcpme.recipe.json").write_bytes(raw)
    lock = {"schema_version": 1, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "artifacts": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                           "license": "LICENSE", "origin": "synthetic fixture"} for name, content in sorted(files.items())]}
    (root / "mcpme.recipe.lock.json").write_bytes(canonical_bytes(lock))
    return root


def descriptor(recipe, lock_digest):
    return {"schema_version": 1, "recipe": {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
        "package": {"id": "test.synthetic", "version": "1.0.0", "name": "Synthetic validator"},
        "interface": {"kind": "python-callable", "module": "validator", "function": "validate_json", "tools": [{
            "name": "validate_json", "description": "Synthetic validator", "input_schema": {"type": "object",
                "properties": {"document": {"type": "string"}}, "required": ["document"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"valid": {"type": "boolean"}}, "required": ["valid"]}}]},
        "dependencies": {"project": ".", "lock_sha256": lock_digest, "python_version": "3.12.13", "mcp_version": "1.30.0"},
        "source_license": "LICENSE", "inputs": {"label": "Generated"}, "resources": {"memory_mb": 128},
        "validation": [{"id": "synthetic", "tool": "validate_json", "arguments": {"document": "{}"},
                        "expected": {"valid": True}, "synthetic": True}]}


@pytest.fixture
def foundation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    # A top-level side effect would fail if inspection imported this source.
    (source / "validator.py").write_text("raise RuntimeError('inspection executed source')\ndef validate_json(document): return {'valid': True}\n")
    (source / "LICENSE").write_text("Synthetic source fixture notice.\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    lock = b"version = 1\nrevision = 3\n"
    (source / "uv.lock").write_bytes(lock)
    with ImportStore(tmp_path / "state") as imports:
        recipes = RecipeStore(imports)
        recipe = recipes.add(recipe_bundle(tmp_path / "bundle"))
        recipe = recipes.enable(recipe["recipe_id"], recipe["version"])
        draft = imports.create(source, name="Synthetic", intent="Expose the synthetic validator")
        jobs = PreparationStore(imports, recipes)
        yield imports, recipes, jobs, draft, descriptor(recipe, hashlib.sha256(lock).hexdigest())


def policy():
    return ExecutionPolicy(schema_version=1, operations=["build", "probe"], wall_seconds=120,
                           disk_bytes=1024 * 1024 * 1024, memory_mb=128)


def verifying(jobs, draft, value, *, settled=True):
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    for state in ("snapshotting", "preparing", "verifying"):
        job = jobs.transition(job["job_id"], state, ownership_settled=settled)
    return job


def reseal(package):
    """Extend the common fixture lock with the ordinary locked receipt artifact."""
    relock(package)
    receipt = (package / RECEIPT_NAME).read_bytes()
    lock_path = package / "mcpme.lock.json"
    lock = json.loads(lock_path.read_bytes())
    digest = hashlib.sha256(receipt).hexdigest()
    lock["artifacts"].append({"path": RECEIPT_NAME, "sha256": digest, "size": len(receipt), "executable": False,
                             "source": "prepared:sha256:" + digest, "provenance": "Synthetic preparation receipt", "license": "LICENSE"})
    lock_path.write_bytes(canonical_bytes(lock))


def candidate(tmp_path, job, value):
    package = make_package(tmp_path / "candidate")
    descriptor = parse_descriptor(value)
    edit_manifest(package, **PreparationStore._candidate_contract(descriptor, job["binding"]["source_revision"]))
    receipt = PreparationReceipt(schema_version=1, binding=job["binding"],
        dependency_lock_digest=job["dependency_lock_digest"], runtime_digest="d" * 64,
        fixture_digest=job["fixture_digest"])
    (package / RECEIPT_NAME).write_bytes(canonical_bytes(receipt.model_dump()))
    reseal(package)
    return package


def test_canonicalization_domain_and_order():
    assert canonical_bytes({"z": 1, "a": [2, 1]}) == b'{"a":[2,1],"z":1}'
    assert contract_digest("preparation-descriptor-v1", {"a": 1, "b": 2}) == contract_digest("preparation-descriptor-v1", {"b": 2, "a": 1})
    assert contract_digest("preparation-descriptor-v1", [1, 2]) != contract_digest("preparation-descriptor-v1", [2, 1])
    assert contract_digest("preparation-descriptor-v1", {}) != contract_digest("preparation-policy-v1", {})


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'[]', b'{', b'\xff', b'{"a":"\\ud800"}'])
def test_strict_json(raw):
    with pytest.raises(PreparationError):
        load_contract(raw)


def test_contract_work_bounds():
    with pytest.raises(PreparationError):
        canonical_bytes({"a": "x" * 16385})
    with pytest.raises(PreparationError):
        canonical_bytes({"a": list(range(16385))})
    nested = {}
    for _ in range(25):
        nested = {"a": nested}
    with pytest.raises(PreparationError):
        canonical_bytes(nested)
    with pytest.raises(PreparationError):
        canonical_bytes({"a": float("nan")})


@pytest.mark.parametrize("schema", [
    {"$ref": "https://example.invalid/schema"}, {"type": "string", "pattern": "(a+)+$"},
    {"$defs": {"self": {"$ref": "#/$defs/self"}}, "$ref": "#/$defs/self"},
    {"$ref": "#/$defs/missing"},
])
def test_schema_evaluation_is_static_and_bounded(schema):
    with pytest.raises(PreparationError):
        check_schema(schema)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("schema_version", True), ("command", "sh"), ("engine", {})])
def test_descriptor_unknown_version_and_field(foundation, field, value):
    raw = copy.deepcopy(foundation[-1])
    raw[field] = value
    with pytest.raises(PreparationError):
        parse_descriptor(raw)


def test_descriptor_boolean_paths_schema_and_fixture(foundation):
    value = foundation[-1]
    for mutate in (
        lambda raw: raw["resources"].update(cpu_only=1),
        lambda raw: raw["dependencies"].update(project="../escape"),
        lambda raw: raw.update(source_license="/private/license"),
        lambda raw: raw["validation"][0].update(synthetic=1),
        lambda raw: raw["validation"][0].update(tool="missing"),
        lambda raw: raw["validation"][0].update(arguments={"document": 7}),
        lambda raw: raw["interface"]["tools"][0].update(input_schema={"$ref": "https://example.invalid"}),
    ):
        raw = copy.deepcopy(value)
        mutate(raw)
        with pytest.raises((PreparationError, ValidationError)):
            parse_descriptor(raw)


def test_credential_rejection_precedes_storage(foundation):
    imports, recipes, jobs, draft, value = foundation
    raw = copy.deepcopy(value)
    raw["inputs"]["label"] = "sk-" + "A" * 30
    with pytest.raises(PreparationError, match="preparation_credentials_detected"):
        jobs.plan(draft["draft_id"], 1, raw)
    assert jobs.list() == []
    assert ("sk-" + "A" * 30).encode() not in imports.path.read_bytes()


def test_plan_never_executes_source_or_promotes_capability(foundation, monkeypatch):
    imports, recipes, jobs, draft, value = foundation
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("static planning executed code"))
    planned = jobs.plan(draft["draft_id"], 1, value)
    assert planned["execution_available"] is False
    assert "preparation_runner_unavailable" in planned["blockers"]
    assert planned["descriptor_digest"] == parse_descriptor(value).digest
    assert recipes.inspect(value["recipe"]["id"], "1.0.0")["runnable"] is False


@pytest.mark.parametrize("required_tools,accepted", [
    ([], True), (["validate_json", "extra_tool"], True),
    (["extra_tool", "validate_json"], True), (["validate_json"], False), (["missing_tool"], False),
])
def test_existing_mcp_plan_and_reservation_match_import_tool_contract(foundation, tmp_path, required_tools, accepted):
    imports, recipes, jobs, draft, value = foundation
    bundle = tmp_path / "mcp-bundle"
    scaffold_recipe(bundle, "test.existing", component="existing-mcp-v1")
    recipe = recipes.add(bundle)
    recipe = recipes.enable(recipe["recipe_id"], recipe["version"])
    value = copy.deepcopy(value)
    value["recipe"] = {"id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]}
    value["interface"].update(kind="python-mcp")
    del value["interface"]["function"]
    value["interface"]["tools"].append({"name": "extra_tool", "description": "Synthetic additional tool",
                                        "input_schema": {"type": "object"}})
    draft = imports.revise(draft["draft_id"], 1, requirements={"tools": required_tools})
    if accepted:
        assert len(jobs.plan(draft["draft_id"], 2, value)["tools"]) == 2
        assert jobs.reserve(draft["draft_id"], 2, value, policy(), idempotency_key=str(uuid.uuid4()))["state"] == "queued"
    else:
        with pytest.raises(PreparationError, match="tool_requirement_mismatch"):
            jobs.plan(draft["draft_id"], 2, value)
        with pytest.raises(PreparationError, match="tool_requirement_mismatch"):
            jobs.reserve(draft["draft_id"], 2, value, policy(), idempotency_key=str(uuid.uuid4()))
        assert jobs.list() == []


def test_job_start_identity_and_privacy(foundation):
    imports, recipes, jobs, draft, value = foundation
    key = str(uuid.uuid4())
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=key)
    assert jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=key) == job
    changed = copy.deepcopy(value)
    changed["inputs"]["label"] = "Changed"
    with pytest.raises(PreparationError, match="preparation_key_conflict"):
        jobs.reserve(draft["draft_id"], 1, changed, policy(), idempotency_key=key)
    assert "arguments" not in json.dumps(job)
    assert "inspection executed source" not in json.dumps(job)
    with pytest.raises(PreparationError, match="preparation_in_progress"):
        jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))


def test_wrong_recipe_lock_and_source_bindings(foundation):
    imports, recipes, jobs, draft, value = foundation
    for mutation in (lambda raw: raw["recipe"].update(digest="f" * 64),
                     lambda raw: raw["dependencies"].update(lock_sha256="f" * 64),
                     lambda raw: raw["interface"].update(module="unknown")):
        raw = copy.deepcopy(value)
        mutation(raw)
        with pytest.raises((PreparationError, ValueError)):
            jobs.plan(draft["draft_id"], 1, raw)


def test_cancel_retains_reservation_until_owned_work_settles(foundation):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value, settled=False)
    canceled = jobs.cancel(job["job_id"])
    assert canceled["state"] == "cancel_requested" and canceled["reservation_retained"]
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        jobs.transition(job["job_id"], "canceled", ownership_settled=False)
    with pytest.raises(PreparationError, match="preparation_in_progress"):
        jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    settled = jobs.transition(job["job_id"], "canceled", ownership_settled=True)
    assert not settled["reservation_retained"]


def test_restart_never_adopts_process_or_releases_unsettled_reservation(foundation):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value, settled=False)
    jobs.recover()
    interrupted = jobs.get(job["job_id"])
    assert interrupted["state"] == "interrupted" and interrupted["reservation_retained"]
    assert "pid" not in json.dumps(interrupted)
    with pytest.raises(PreparationError, match="preparation_in_progress"):
        jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))


@pytest.mark.parametrize("next_state", ["ready_to_attach", "attaching", "succeeded"])
def test_only_independent_verifier_can_promote(foundation, next_state):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    with pytest.raises(PreparationError):
        jobs.transition(job["job_id"], next_state)


@pytest.mark.parametrize("winner", ["cancel", "revise", "cancel_draft", "revoke"])
def test_invalidation_wins_after_verification_and_staging(foundation, tmp_path, winner):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    jobs.record_verified(job["job_id"], validate_package(package))
    stage = imports.stage_attachment(draft["draft_id"], 1, package)
    if winner == "cancel":
        jobs.cancel(job["job_id"])
    elif winner == "revise":
        imports.revise(draft["draft_id"], 1, intent="Revised synthetic intent")
    elif winner == "cancel_draft":
        imports.cancel(draft["draft_id"], 1)
    else:
        recipes.revoke(value["recipe"]["id"], "1.0.0")
    with pytest.raises((PreparationError, ImportError, ValueError)):
        jobs.admit(job["job_id"], stage)
    assert imports.inspect(draft["draft_id"])["attachment"] is None
    assert jobs.get(job["job_id"])["state"] != "succeeded"


def test_admission_commit_wins_and_cancel_reports_completion(foundation, tmp_path):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    validated = validate_package(package)
    jobs.record_verified(job["job_id"], validated)
    stage = imports.stage_attachment(draft["draft_id"], 1, package, expected_digest=validated.digest)
    attached = jobs.admit(job["job_id"], stage)
    assert attached["attachment"]["digest"] == validated.digest
    assert jobs.get(job["job_id"])["state"] == "succeeded"
    assert jobs.cancel(job["job_id"])["state"] == "succeeded"
    assert attached["operation_id"] is None and attached["result_slot_id"] is None


def test_receipt_mismatch_and_failed_admission_roll_back_job(foundation, tmp_path, monkeypatch):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    receipt_path = package / RECEIPT_NAME
    raw = load_contract(receipt_path.read_bytes())
    raw["binding"]["policy_digest"] = "f" * 64
    receipt_path.write_bytes(canonical_bytes(raw))
    reseal(package)
    with pytest.raises(PreparationError, match="preparation_receipt_mismatch"):
        jobs.record_verified(job["job_id"], validate_package(package))
    raw["binding"] = job["binding"]
    receipt_path.write_bytes(canonical_bytes(raw))
    reseal(package)
    jobs.record_verified(job["job_id"], validate_package(package))
    stage = imports.stage_attachment(draft["draft_id"], 1, package)
    monkeypatch.setattr(imports, "_save", lambda *args: (_ for _ in ()).throw(ImportError("synthetic_commit_failure")))
    with pytest.raises(ImportError, match="synthetic_commit_failure"):
        jobs.admit(job["job_id"], stage)
    assert jobs.get(job["job_id"])["state"] == "ready_to_attach"
    assert imports.inspect(draft["draft_id"])["attachment"] is None


def test_disabled_queue_and_pinned_active_job(foundation, tmp_path):
    imports, recipes, jobs, draft, value = foundation
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    recipes.disable(value["recipe"]["id"], "1.0.0")
    with pytest.raises(ValueError):
        jobs.transition(job["job_id"], "snapshotting")
    recipes.enable(value["recipe"]["id"], "1.0.0")
    for state in ("snapshotting", "preparing", "verifying"):
        jobs.transition(job["job_id"], state)
    jobs.settle(job["job_id"])
    recipes.disable(value["recipe"]["id"], "1.0.0")
    package = candidate(tmp_path, job, value)
    jobs.record_verified(job["job_id"], validate_package(package))
    stage = imports.stage_attachment(draft["draft_id"], 1, package)
    assert jobs.admit(job["job_id"], stage)["attachment"] is not None


def test_public_collection_has_compact_bounded_metadata(foundation):
    imports, recipes, jobs, draft, value = foundation
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    summary = jobs.list_summaries()[0]
    assert set(summary) == {"job_id", "import_id", "revision", "state", "updated_at", "reservation_retained", "error_code", "progress"}
    assert summary["job_id"] == job["job_id"] and summary["import_id"] == draft["draft_id"]
    assert len(json.dumps({"preparations": [summary] * 1024}, allow_nan=False).encode()) < 1024 * 1024


def test_default_transition_cannot_release_pending_owned_work(foundation):
    imports, recipes, jobs, draft, value = foundation
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    snapshotting = jobs.transition(job["job_id"], "snapshotting")
    assert not snapshotting["ownership_settled"]
    jobs.transition(job["job_id"], "preparing")
    canceled = jobs.cancel(job["job_id"])
    assert canceled["state"] == "cancel_requested" and canceled["reservation_retained"]
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        jobs.transition(job["job_id"], "canceled")
    assert jobs.get(job["job_id"])["reservation_retained"]
    assert jobs.settle(job["job_id"])["state"] == "canceled"
    assert not jobs.get(job["job_id"])["reservation_retained"]


@pytest.mark.parametrize("state", [[], {}, True, "unknown"])
def test_invalid_transition_is_bounded_and_content_free(foundation, state):
    imports, recipes, jobs, draft, value = foundation
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    with pytest.raises(PreparationError, match="^invalid_preparation_transition$"):
        jobs.transition(job["job_id"], state)
    assert jobs.get(job["job_id"])["state"] == "queued"


def test_restart_requires_explicit_owner_settlement_before_retry(foundation, monkeypatch):
    imports, recipes, jobs, draft, value = foundation
    job = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    jobs.transition(job["job_id"], "snapshotting")
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: pytest.fail("restart replayed a step"))
    monkeypatch.setattr("os.kill", lambda *args, **kwargs: pytest.fail("restart adopted a PID"))
    jobs.recover()
    assert jobs.get(job["job_id"])["reservation_retained"]
    assert jobs.settle(job["job_id"])["state"] == "interrupted"
    retry = jobs.reserve(draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
    assert retry["job_id"] != job["job_id"] and retry["state"] == "queued"


@pytest.mark.parametrize("change", ["tools", "access", "resources", "lifecycle", "name"])
def test_verifier_binds_manifest_fidelity(foundation, tmp_path, change):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    changes = {"tools": [{"name": "other", "description": "Changed tool", "input_schema": {"type": "object"}}],
               "access": {"writes": True}, "resources": {"memory_mb": 129, "cpu_only": True},
               "lifecycle": {"max_sessions": 2}, "name": "Changed app"}
    edit_manifest(package, **{change: changes[change]})
    reseal(package)
    with pytest.raises(PreparationError, match="preparation_receipt_mismatch"):
        jobs.record_verified(job["job_id"], validate_package(package))
    assert jobs.get(job["job_id"])["state"] == "verifying"


def test_verifier_rechecks_stale_package_object(foundation, tmp_path):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    old = validate_package(package)
    (package / "LICENSE").write_text("Changed ordinary artifact\n")
    reseal(package)
    with pytest.raises(PreparationError, match="preparation_result_mismatch"):
        jobs.record_verified(job["job_id"], old)


@pytest.mark.parametrize("winner", ["cancel", "revise", "cancel_draft", "revoke"])
def test_cross_connection_invalidation_wins_before_result_admission(foundation, tmp_path, monkeypatch, winner):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    jobs.record_verified(job["job_id"], validate_package(package))
    stage = imports.stage_attachment(draft["draft_id"], 1, package)
    entered, release = threading.Event(), threading.Event()
    original = validate_package
    def paused_validation(path):
        if Path(path) == Path(stage["path"]):
            entered.set()
            assert release.wait(5), "admission barrier was not released"
        return original(path)
    monkeypatch.setattr("mcpme.imports.validate_package", paused_validation)
    with ImportStore(imports.root) as other:
        other_recipes = RecipeStore(other)
        other_jobs = PreparationStore(other, other_recipes)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(jobs.admit, job["job_id"], stage)
            try:
                assert entered.wait(5), "admission did not reach its independent hash phase"
                if winner == "cancel":
                    other_jobs.cancel(job["job_id"])
                elif winner == "revise":
                    other.revise(draft["draft_id"], 1, intent="Changed during admission hashing")
                elif winner == "cancel_draft":
                    other.cancel(draft["draft_id"], 1)
                else:
                    other_recipes.revoke(value["recipe"]["id"], "1.0.0")
            finally:
                release.set()
            with pytest.raises((PreparationError, ImportError)):
                future.result(timeout=5)
        assert other.inspect(draft["draft_id"])["attachment"] is None
        assert other_jobs.get(job["job_id"])["state"] != "succeeded"


def test_cross_connection_attachment_commit_wins_over_cancel(foundation, tmp_path, monkeypatch):
    imports, recipes, jobs, draft, value = foundation
    job = verifying(jobs, draft, value)
    package = candidate(tmp_path, job, value)
    jobs.record_verified(job["job_id"], validate_package(package))
    stage = imports.stage_attachment(draft["draft_id"], 1, package)
    entered, release, cancel_attempted = threading.Event(), threading.Event(), threading.Event()
    original_save = jobs._save
    def paused_save(record):
        original_save(record)
        if record["state"] == "succeeded":
            entered.set()
            assert release.wait(5), "commit barrier was not released"
    monkeypatch.setattr(jobs, "_save", paused_save)
    with ImportStore(imports.root) as other:
        other_jobs = PreparationStore(other, RecipeStore(other))
        original_cancel = other_jobs.cancel
        def concurrent_cancel():
            cancel_attempted.set()
            return original_cancel(job["job_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            attached = pool.submit(jobs.admit, job["job_id"], stage)
            try:
                assert entered.wait(5), "attachment did not enter its commit transaction"
                canceled = pool.submit(concurrent_cancel)
                assert cancel_attempted.wait(5)
            finally:
                release.set()
            assert attached.result(timeout=5)["attachment"] is not None
            assert canceled.result(timeout=5)["state"] == "succeeded"
        assert other_jobs.get(job["job_id"])["state"] == "succeeded"


@pytest.mark.parametrize("winner", ["revise", "revoke"])
def test_recipe_preflight_hashing_has_no_write_lock_and_rechecks_metadata(foundation, monkeypatch, winner):
    imports, recipes, jobs, draft, value = foundation
    entered, release = threading.Event(), threading.Event()
    original = recipes.resolve
    def paused_resolve(*args, **kwargs):
        resolved = original(*args, **kwargs)
        assert not imports._connection.in_transaction
        entered.set()
        assert release.wait(5), "preflight barrier was not released"
        return resolved
    monkeypatch.setattr(recipes, "resolve", paused_resolve)
    with ImportStore(imports.root) as other:
        other_recipes = RecipeStore(other)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(jobs.reserve, draft["draft_id"], 1, value, policy(), idempotency_key=str(uuid.uuid4()))
            try:
                assert entered.wait(5)
                if winner == "revise":
                    other.revise(draft["draft_id"], 1, intent="Changed during recipe preflight")
                else:
                    other_recipes.revoke(value["recipe"]["id"], "1.0.0")
            finally:
                release.set()
            with pytest.raises((PreparationError, ImportError)):
                future.result(timeout=5)
        assert jobs.list() == []


def test_fixture_semantic_validation_has_reference_expansion_budget(monkeypatch):
    import mcpme.preparation_contracts as contracts
    schema = {"$defs": {"integer": {"type": "integer"}},
              "allOf": [{"$ref": "#/$defs/integer"} for _ in range(32)]}
    check_schema(schema)
    monkeypatch.setattr(contracts, "MAX_SCHEMA_EVALUATIONS", 12)
    with pytest.raises(PreparationError, match="preparation_schema_evaluation_limit"):
        schema_accepts(schema, 7)
    monkeypatch.setattr(contracts, "MAX_SCHEMA_EVALUATIONS", 32768)
    assert schema_accepts(schema, 7)


def test_schema_array_pointer_defaults_and_instance_bounds():
    schema = {"$defs": {"tuple": {"prefixItems": [{"type": "integer"}]}}, "$ref": "#/$defs/tuple/prefixItems/0"}
    check_schema(schema)
    assert schema_accepts(schema, 7)
    assert not schema_accepts(schema, "7")
    with pytest.raises(PreparationError, match="preparation_schema_evaluation_limit"):
        schema_accepts({"type": "array", "uniqueItems": True}, list(range(129)))
    schema = {"$defs": {"label": {"type": "string", "maxLength": 3}},
              "type": "object", "additionalProperties": False,
              "properties": {"label": {"type": "string", "$ref": "#/$defs/label"}}}
    assert schema_accepts(schema, "OK", subschema=schema["properties"]["label"])
    assert not schema_accepts(schema, "Long", subschema=schema["properties"]["label"])


def test_schema_property_names_and_literal_defaults_are_data():
    schema = {"type": "object", "properties": {"pattern": {"type": "string"}, "$id": {"type": "string"}},
              "default": {"$ref": "https://example.invalid/is-only-a-default-value"}}
    check_schema(schema)
    assert schema_accepts(schema, {"pattern": "literal", "$id": "literal"})


@pytest.mark.parametrize("schema", [
    {"$id": "https://example.invalid/schema", "type": "object"},
    {"$defs": {"literal": 7}, "$ref": "#/$defs/literal"},
    {"$defs": {"a~2b": {}}, "$ref": "#/$defs/a~2b"},
])
def test_schema_scope_changing_and_invalid_pointer_targets_are_rejected(schema):
    with pytest.raises(PreparationError):
        check_schema(schema)
