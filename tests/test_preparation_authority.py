"""Scoped authority admission and durable ownership journal regression checks."""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcpme.contracts import validate_package
from mcpme.imports import ImportError
from mcpme.ownership_receipts import MAGIC, prepare_receipt
from mcpme.preparation_contracts import PreparationError, parse_descriptor
from mcpme.preparations import PreparationStore
from mcpme.preparation_verifier import VerificationReport
from test_mcpme_preparations import candidate, foundation, policy


def plan_for(foundation, **kwargs):
    _, _, jobs, draft, descriptor = foundation
    return jobs.execution_plan(draft["draft_id"], draft["revision"], descriptor, policy(),
        runner_digest="a" * 64, verifier_digest="b" * 64, runtime_digest=kwargs.pop("runtime_digest", "d" * 64), **kwargs)


def start(foundation):
    plan = plan_for(foundation)
    jobs = foundation[2]
    return jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))


def test_review_is_static_and_binds_actual_implementation(foundation):
    _, _, jobs, draft, descriptor = foundation
    plan = plan_for(foundation)
    assert jobs.list() == []
    key = str(uuid.uuid4())
    job = jobs.authorize_start(plan["plan_id"], plan["plan_digest"], key)
    assert job["state"] == "queued" and job["managed"]
    assert job["binding"]["runner_digest"] == "a" * 64
    assert job["binding"]["verifier_digest"] == "b" * 64
    assert job["runtime_digest"] == "d" * 64
    bound, source, scope = jobs.job_inputs(job["job_id"])
    assert bound == parse_descriptor(descriptor)
    assert source["source"] == draft["source"] and scope == policy()
    assert jobs.authorize_start(plan["plan_id"], plan["plan_digest"], key) == job
    with pytest.raises(PreparationError, match="preparation_plan_consumed"):
        jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))


def test_parallel_duplicate_start_creates_one_attempt(foundation):
    jobs = foundation[2]
    plan = plan_for(foundation)
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: jobs.authorize_start(plan["plan_id"], plan["plan_digest"], key), range(2)))
    assert attempts[0]["job_id"] == attempts[1]["job_id"]
    assert len(jobs.list()) == 1


def test_tampered_plan_digest_and_expired_intent_do_not_reserve(foundation, monkeypatch):
    jobs = foundation[2]
    plan = plan_for(foundation, expires_in=1)
    with pytest.raises(PreparationError, match="preparation_plan_mismatch"):
        jobs.authorize_start(plan["plan_id"], "c" * 64, str(uuid.uuid4()))
    monkeypatch.setattr("mcpme.preparation_authority.time.time", lambda: plan["expires_at"] + 1)
    with pytest.raises(PreparationError, match="preparation_authority_expired"):
        jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert jobs.list() == []


def test_revision_and_revocation_fence_reviewed_start(foundation):
    imports, recipes, jobs, draft, _ = foundation
    plan = plan_for(foundation)
    imports.revise(draft["draft_id"], 1, name="Changed task")
    with pytest.raises(ImportError, match="stale_import_revision"):
        jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert jobs.list() == []


def test_disabled_recipe_cannot_start_reviewed_plan(foundation):
    _, recipes, jobs, _, value = foundation
    plan = plan_for(foundation)
    recipes.disable(value["recipe"]["id"], value["recipe"]["version"])
    with pytest.raises(PreparationError):
        jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    assert jobs.list() == []


def test_authority_expiry_fences_resource_wait_not_admitted_scope(foundation, monkeypatch):
    jobs = foundation[2]
    plan = plan_for(foundation, expires_in=1)
    job = jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    jobs.transition(job["job_id"], "snapshotting")
    monkeypatch.setattr("mcpme.preparation_authority.time.time", lambda: plan["expires_at"] + 1)
    with pytest.raises(PreparationError, match="preparation_authority_expired"):
        jobs.transition(job["job_id"], "preparing")
    assert jobs.get(job["job_id"])["state"] == "snapshotting"


def test_started_scope_remains_bound_after_review_expiry(foundation, monkeypatch):
    jobs = foundation[2]
    job = start(foundation)
    jobs.transition(job["job_id"], "snapshotting")
    admitted = jobs.transition(job["job_id"], "preparing")
    monkeypatch.setattr("mcpme.preparation_authority.time.time", lambda: admitted["authority_expires_at"] + 1)
    assert jobs.check_authority(job["job_id"])["state"] == "preparing"
    jobs.cancel(job["job_id"])
    with pytest.raises(PreparationError, match="preparation_canceled"):
        jobs.check_authority(job["job_id"])


def test_managed_runtime_receipt_must_match_reviewed_runtime(foundation, tmp_path):
    jobs, value = foundation[2], foundation[4]
    plan = plan_for(foundation, runtime_digest="e" * 64)
    job = jobs.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    for state in ("snapshotting", "preparing", "verifying"):
        job = jobs.transition(job["job_id"], state, ownership_settled=True)
    package = validate_package(candidate(tmp_path, job, value))
    with pytest.raises(PreparationError, match="preparation_receipt_mismatch"):
        jobs.record_verified(job["job_id"], package)


@pytest.mark.parametrize("changes", [{"package_digest": "f" * 64}, {"tools_checked": 2},
    {"fixtures_checked": 0}, {"protocol_version": "2024-11-05"}, {"peak_memory_bytes": 129 * 1024**2}])
def test_managed_admission_requires_matching_independent_evidence(foundation, tmp_path, changes):
    jobs, value = foundation[2], foundation[4]
    job = start(foundation)
    for state in ("snapshotting", "preparing", "verifying"):
        job = jobs.transition(job["job_id"], state, ownership_settled=True)
    package = validate_package(candidate(tmp_path, job, value))
    fields = {"package_digest": package.digest, "tools_checked": 1, "fixtures_checked": 1,
              "protocol_version": "2025-11-25", "peak_memory_bytes": 64 * 1024**2}
    with pytest.raises(PreparationError, match="independent_verification_required"):
        jobs.record_verified(job["job_id"], package)
    with pytest.raises(PreparationError, match="independent_verification_required"):
        jobs.record_verified(job["job_id"], package, verification=VerificationReport(**{**fields, **changes}))
    assert jobs.get(job["job_id"])["state"] == "verifying"
    # Synthetic admission evidence checks this store contract only. The runner
    # separately exercises the actual SDK verifier before supplying its report.
    job = jobs.record_verified(job["job_id"], package, verification=VerificationReport(**fields))
    assert job["verification"] == fields
    jobs.begin_attachment(job["job_id"])
    stage = foundation[0].stage_attachment(job["binding"]["import_id"], job["binding"]["revision"],
                                          package.root, expected_digest=package.digest)
    assert not jobs.get(job["job_id"])["ownership_settled"]
    attached = jobs.admit(job["job_id"], stage)
    assert attached["attachment"]["digest"] == package.digest
    assert jobs.get(job["job_id"])["state"] == "succeeded"
    assert jobs.get(job["job_id"])["ownership_settled"]


def verified_fixture(foundation, tmp_path):
    """Synthetic store evidence; real SDK reports are exercised by runner tests."""
    jobs, descriptor = foundation[2], foundation[4]
    job = start(foundation)
    for state in ("snapshotting", "preparing", "verifying"):
        job = jobs.transition(job["job_id"], state, ownership_settled=True)
    package = validate_package(candidate(tmp_path, job, descriptor))
    report = VerificationReport(package.digest, 1, 1, "2025-11-25", 64 * 1024**2)
    return jobs, job, package, report


@pytest.mark.parametrize("publication", ["verification", "attachment"])
def test_cancel_retains_reservation_until_publication_hash_thread_joins(foundation, tmp_path, monkeypatch, publication):
    jobs, job, package, report = verified_fixture(foundation, tmp_path)
    identifier = job["job_id"]
    entered, released = threading.Event(), threading.Event()
    original = validate_package

    def paused_validation(path):
        entered.set()
        assert released.wait(5)
        return original(path)

    if publication == "verification":
        jobs.begin_verification_publication(identifier)
        monkeypatch.setattr("mcpme.preparations.validate_package", paused_validation)
        publish = lambda: jobs.record_verified(identifier, package, verification=report)
        error = "preparation_receipt_mismatch"
    else:
        jobs.record_verified(identifier, package, verification=report)
        jobs.begin_attachment(identifier)
        stage = foundation[0].stage_attachment(job["binding"]["import_id"], job["binding"]["revision"],
                                              package.root, expected_digest=package.digest)
        monkeypatch.setattr("mcpme.imports.validate_package", paused_validation)
        publish = lambda: jobs.admit(identifier, stage)
        error = "preparation_not_verified"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish)
        try:
            assert entered.wait(5)
            canceled = jobs.cancel(identifier)
            assert canceled["state"] == "cancel_requested"
            assert canceled["reservation_retained"] and not canceled["ownership_settled"]
            assert foundation[0]._get(job["binding"]["import_id"])["attachment"] is None
        finally:
            released.set()
        with pytest.raises(PreparationError, match=error):
            future.result(timeout=5)
    settled = jobs.settle(identifier)
    assert settled["state"] == "canceled" and not settled["reservation_retained"]
    assert foundation[0]._get(job["binding"]["import_id"])["attachment"] is None


def test_persisted_inputs_and_idempotency_survive_store_reopen(foundation):
    imports, recipes, jobs, _, _ = foundation
    job = start(foundation)
    second = PreparationStore(imports, recipes)
    assert second.get(job["job_id"]) == job
    assert second.job_inputs(job["job_id"])[0].digest == job["binding"]["descriptor_digest"]


def test_pending_native_receipt_prevents_terminal_settlement(foundation):
    imports, _, jobs, _, _ = foundation
    job = start(foundation)
    workspace = imports.root / "preparations" / job["job_id"]
    jobs.set_workspace(job["job_id"], workspace)
    jobs.transition(job["job_id"], "snapshotting")
    jobs.transition(job["job_id"], "preparing")
    descriptor, metadata = prepare_receipt(workspace, uuid.uuid4().hex)
    try:
        jobs.register_ownership(job["job_id"], metadata)
        jobs.cancel(job["job_id"])
        with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
            jobs.settle(job["job_id"])
        with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
            jobs.confirm_ownership(job["job_id"], metadata["instance_id"])
        assert jobs.get(job["job_id"])["reservation_retained"]
        # Synthetic receipt-state fixture. Real native completion is covered by
        # the executor/runner tests; pending metadata alone cannot pass here.
        os.pwrite(descriptor, MAGIC + metadata["instance_id"].encode() + metadata["nonce"].encode() + b"C", 0)
        os.fsync(descriptor)
        jobs.confirm_ownership(job["job_id"], metadata["instance_id"])
        assert jobs.ownership_receipts(job["job_id"]) == []
        settled = jobs.settle(job["job_id"])
        assert settled["state"] == "canceled" and not settled["reservation_retained"]
    finally:
        os.close(descriptor)


def test_workspace_is_fixed_to_job_private_state(foundation, tmp_path):
    job = start(foundation)
    with pytest.raises(PreparationError, match="invalid_preparation_workspace"):
        foundation[2].set_workspace(job["job_id"], tmp_path / "arbitrary")


def test_recovery_reservation_scope_survives_source_invalidation(foundation):
    imports, _, jobs, draft, _ = foundation
    job = start(foundation)
    imports.revise(draft["draft_id"], 1, name="New revision")
    with pytest.raises(ImportError, match="stale_import_revision"):
        jobs.job_inputs(job["job_id"])
    assert jobs.recovery_policy(job["job_id"]) == policy()


def test_recipe_disable_before_first_execution_fences_snapshotting(foundation):
    _, recipes, jobs, _, descriptor = foundation
    job = start(foundation)
    jobs.transition(job["job_id"], "snapshotting")
    recipes.disable(descriptor["recipe"]["id"], descriptor["recipe"]["version"])
    with pytest.raises(PreparationError):
        jobs.transition(job["job_id"], "preparing")
    assert jobs.get(job["job_id"])["execution_started_at"] is None


def test_running_authorized_scope_can_finish_after_recipe_disable(foundation):
    _, recipes, jobs, _, descriptor = foundation
    job = start(foundation)
    jobs.transition(job["job_id"], "snapshotting")
    jobs.transition(job["job_id"], "preparing")
    recipes.disable(descriptor["recipe"]["id"], descriptor["recipe"]["version"])
    assert jobs.check_authority(job["job_id"])["state"] == "preparing"


def test_progress_rejects_content_and_regressing_counters(foundation):
    jobs = foundation[2]
    job = start(foundation)
    updated = jobs.record_progress(job["job_id"], "dependency_export", commands_finished=1)
    assert updated["progress"]["commands_finished"] == 1
    for stage, counters in (("raw worker error", {}), ("verifying", {"commands_finished": 0}),
                            ("verifying", {"commands_finished": True}), ("verifying", {"raw_output": "secret"})):
        with pytest.raises(PreparationError, match="invalid_preparation_progress"):
            jobs.record_progress(job["job_id"], stage, **counters)
