"""Actual no-folder forms and closed service authority, without execution."""
import copy
import json
import uuid

import pytest

from meecp.imports import ImportStore
from meecp.preparation_contracts import PreparationError
from meecp.preparation_forms import PreparationForms
from meecp.preparation_service import ServicePreparationFamily, references, descriptor, validate_components
from meecp.preparation_service_contracts import parse_policy
from meecp.preparation_service_source import source
from meecp.preparations import PreparationStore
from meecp.recipe_sdk import scaffold_recipe, relock_recipe
from meecp.recipes import RecipeStore
from meecp.settings import SecretStore


@pytest.fixture
def service(tmp_path):
    imports = ImportStore(tmp_path / "state")
    secrets = SecretStore(tmp_path / "state")
    secret = secrets.create("Synthetic local service", "synthetic-nonsecret-token")
    refs = references()
    connection = {"schema_version": 1, **{key: ref.model_dump() for key, ref in zip(("protocol", "transport_auth", "task"), refs)},
        "endpoint": {"scheme": "http", "host": "127.0.0.1", "port": 12345, "base_path": "/v1"},
        "model": "synthetic-model", "secret_references": {"api_key": secret["secret_id"]},
        "limits": {"timeout_seconds": 2, "max_input_bytes": 128, "max_output_tokens": 32, "max_response_bytes": 4096}}
    draft = imports.create_service(source(connection, secrets, validate_components=validate_components), name="Local service", intent="Generate locally")
    recipes = RecipeStore(imports)
    path = tmp_path / "recipe"; scaffold_recipe(path, "test.local-service")
    manifest_path = path / "meecp.recipe.json"; manifest = json.loads(manifest_path.read_bytes())
    manifest.update(form=[], input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        steps=[{"component": ref.id, "version": ref.version, "digest": ref.digest, "config": {}} for ref in refs])
    manifest_path.write_text(json.dumps(manifest)); relock_recipe(path)
    record = recipes.add(path); recipes.enable(record["recipe_id"], record["version"])
    record = recipes.resolve(record["recipe_id"], record["version"], record["digest"])
    family = ServicePreparationFamily(secrets)
    store = PreparationStore(imports, recipes, families=(family,))
    try:
        yield imports, secrets, draft, record, family, store
    finally:
        imports.close()


def test_actual_no_folder_form_selection_and_policy(service):
    imports, secrets, draft, record, family, store = service
    forms = PreparationForms(store)
    form = forms.form(draft["draft_id"], 1)
    assert not form["blockers"] and not form["execution_available"]
    assert [tool["name"] for tool in form["tools"]] == ["generate", "check_connection"]
    value, _ = forms.resolve(draft["draft_id"], 1, candidate_id=form["candidate_id"], recipe=form["recipe"],
        form_digest=form["form_digest"], answers={item["id"]: item["value"] for item in form["fields"]})
    selected = store.select_descriptor(draft["draft_id"], 1, value.model_dump(exclude_none=True),
        candidate_id=form["candidate_id"], form_digest=form["form_digest"])
    assert selected["source"] == draft["source"] and not (imports.staging / draft["draft_id"]).exists()
    policy = family.execution_policy(value, selected)
    assert policy.schema_version == 2 and parse_policy(policy.model_dump()) == policy
    assert policy.service_probe.max_calls == 1 and policy.service_probe.secret_reference == draft["source"]["connection"]["secret_references"]["api_key"]
    assert policy.network == ["files.pythonhosted.org"]
    assert store.list() == []


@pytest.mark.parametrize("fault", ["origin", "secret", "fixture", "calls", "deadline", "descriptor", "schema"])
def test_exact_probe_grant_rejects_changed_disclosure(service, fault):
    _, _, draft, record, family, _ = service
    value = descriptor(draft, record, name=draft["name"], memory_mb=256)
    raw = family.execution_policy(value, draft).model_dump()
    if fault == "origin": raw["service_probe"]["endpoint"]["port"] += 1
    elif fault == "secret": raw["service_probe"]["secret_reference"] = str(uuid.uuid4())
    elif fault == "fixture": raw["service_probe"]["fixture_digest"] = "a" * 64
    elif fault == "calls": raw["service_probe"]["max_calls"] = 2
    elif fault == "deadline": raw["service_probe"]["timeout_seconds"] += 1
    elif fault == "descriptor": raw["service_probe"]["descriptor_digest"] = "a" * 64
    elif fault == "schema": raw["schema_version"] = 1
    with pytest.raises(ValueError): family.execution_policy(value, draft, raw)


def test_revoke_prevents_plan_and_current_admission(service):
    _, secrets, draft, record, family, _ = service
    value = descriptor(draft, record, name=draft["name"], memory_mb=256)
    assert family.check_current(value, draft)
    policy = family.execution_policy(value, draft)
    secrets.revoke(draft["source"]["connection"]["secret_references"]["api_key"])
    for check in (lambda: family.check_current(value, draft), lambda: family.execution_policy(value, draft, policy)):
        with pytest.raises(PreparationError, match="service_secret_unavailable"): check()


def test_bound_inputs_carry_source_and_derivation_without_secret_uuid(service):
    _, _, draft, record, family, store = service
    value = descriptor(draft, record, name=draft["name"], memory_mb=256)
    inputs = family.bound_inputs(value, draft, record, store.components, "e" * 64)
    encoded = inputs.model_dump(mode="json")
    assert inputs.schema_version == 2 and inputs.source.kind == "service_connection"
    assert "captured_collection" not in encoded and "derived_collection" not in encoded
    assert inputs.source.revision == draft["source"]["revision"]
    assert draft["source"]["connection"]["secret_references"]["api_key"] not in json.dumps(encoded)


def managed_job(service):
    _, _, draft, record, family, store = service
    value = descriptor(draft, record, name=draft["name"], memory_mb=256)
    policy = family.execution_policy(value, draft)
    plan = store.execution_plan(draft["draft_id"], draft["revision"], value.model_dump(exclude_none=True), policy,
        runner_digest="a"*64, verifier_digest="b"*64, runtime_digest="c"*64)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    return value, policy, plan, job


@pytest.mark.parametrize("fault", [None,"grant","source","derivation","configuration","runtime","recipe","kind","version","extra"])
def test_actual_job_service_receipt_is_complete_and_not_downgradable(service, fault):
    from meecp.preparation_service_receipts import ServicePreparationReceiptV1, parse_receipt, validate_job_receipt
    _, policy, _, job = managed_job(service)
    raw = {"schema_version":3,"binding":job["binding"],"inputs":copy.deepcopy(job["bound_inputs"]),
        "fixture_digest":job["fixture_digest"],"service_probe_grant_digest":policy.service_probe.digest}
    if fault == "grant": raw["service_probe_grant_digest"] = "f"*64
    elif fault == "source": raw["inputs"]["source"]["revision"] = "sha256:"+"f"*64
    elif fault == "derivation": raw["inputs"]["derivation_digest"] = "f"*64
    elif fault == "configuration": raw["inputs"]["portable_configuration_digest"] = "f"*64
    elif fault == "runtime": raw["inputs"]["runtimes"][0]["digest"] = "f"*64
    elif fault == "recipe": raw["inputs"]["recipe"]["digest"] = "f"*64
    elif fault == "kind": raw["inputs"]["source"]["kind"] = "local_folder"
    elif fault == "version": raw["schema_version"] = 2
    elif fault == "extra": raw["inputs"]["captured_collection"] = None
    if fault is None:
        parsed = parse_receipt(raw)
        assert type(parsed) is ServicePreparationReceiptV1
        validate_job_receipt(parsed, job)
        assert parsed.inputs.digest == job["bound_inputs_digest"]
        assert service[-1].job_inputs(job["job_id"])[2] == policy
    else:
        with pytest.raises(ValueError): validate_job_receipt(parse_receipt(raw), job)


def test_held_job_secret_revocation_fails_before_work(service):
    imports, secrets, draft, _, _, store = service
    _, _, _, job = managed_job(service)
    assert store.check_authority(job["job_id"])["state"] == "queued"
    secrets.revoke(draft["source"]["connection"]["secret_references"]["api_key"])
    with pytest.raises(PreparationError, match="service_secret_unavailable"):
        store.check_authority(job["job_id"])
    assert store.get(job["job_id"])["state"] == "queued"
    assert store.pending_ownership(job["job_id"]) is False


@pytest.mark.parametrize("fault", ["source", "secret", "recipe", "cancel", "expire"])
def test_held_service_plan_rejects_changed_authority_without_job(service, fault, monkeypatch):
    imports, secrets, draft, recipe, family, store = service
    value = descriptor(draft, recipe, name=draft["name"], memory_mb=256)
    policy = family.execution_policy(value, draft)
    plan = store.execution_plan(draft["draft_id"], draft["revision"], value.model_dump(exclude_none=True), policy,
        runner_digest="a"*64, verifier_digest="b"*64, runtime_digest="c"*64)
    if fault == "source":
        connection = copy.deepcopy(draft["source"]["connection"]); connection["model"] = "different-synthetic"
        imports.revise_service(draft["draft_id"], 1, source(connection, secrets, validate_components=validate_components))
    elif fault == "secret": secrets.revoke(draft["source"]["connection"]["secret_references"]["api_key"])
    elif fault == "recipe": store.recipes.revoke(recipe["recipe_id"], recipe["version"])
    elif fault == "cancel": imports.cancel(draft["draft_id"], 1)
    else: monkeypatch.setattr("meecp.preparation_authority.time.time", lambda: plan["expires_at"] + 1)
    with pytest.raises(ValueError): store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert store.list() == []
