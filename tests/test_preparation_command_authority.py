"""Real SQLite job guards keep command proofs independent of legacy receipts."""
from __future__ import annotations

import os
import uuid

import pytest

from mcpme.preparation_command_owner import recover_command_ownership
from mcpme.preparation_contracts import PreparationError, canonical_bytes, contract_digest
from mcpme.private import write_private_json
from test_mcpme_preparations import foundation, policy


def pending_command(foundation):
    imports, recipes, store, draft, raw = foundation
    plan = store.execution_plan(draft["draft_id"], 1, raw, policy(), runner_digest="a" * 64,
        verifier_digest="b" * 64, runtime_digest="c" * 64)
    job = store.authorize_start(plan["plan_id"], plan["plan_digest"], str(uuid.uuid4()))
    identifier = job["job_id"]
    store.transition(identifier, "snapshotting")
    workspace = imports.root / "preparations" / identifier
    workspace.mkdir(mode=0o700, parents=True)
    workspace.parent.chmod(0o700)
    store.set_workspace(identifier, workspace)
    store.transition(identifier, "preparing")
    store.transition(identifier, "verifying")
    metadata = {"schema_version": 1, "kind": "command-v3", "instance_id": "a" * 32, "attempt_id": "b" * 32,
        "job_binding_digest": contract_digest("preparation-command-job-binding-v1", job["binding"]),
        "candidate_digest": "d" * 64, "fixture_digest": "e" * 64, "helper_sha256": "f" * 64,
        "policy_digest": "0" * 64, "receipt_format": 3}
    metadata["journal_identity"] = contract_digest("preparation-command-journal-v1", metadata)
    state = workspace / "command-probe-v3"
    state.mkdir(mode=0o700)
    write_private_json(state / "owner.json", metadata)
    write_private_json(state / "invocations.json", {"version": 1, "entries": []})
    write_private_json(state / "command-v2.json", {"version": 3, "entries": []})
    fd = os.open(state / "command-v2.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    # Seed a recovered command row directly so these DB guard tests do not
    # manufacture an admitted command family out of this legacy job fixture.
    with imports._transaction():
        store.db.execute("INSERT INTO preparation_command_ownership VALUES (?,?,?)",
            (identifier, metadata["instance_id"], canonical_bytes(metadata).decode()))
    return store, identifier, workspace, metadata


@pytest.mark.parametrize("missing", ["owner.json", "invocations.json", "command-v2.json", "command-v2.lock"])
def test_actual_store_missing_journal_retains_cancel_and_recovery(foundation, missing):
    store, job_id, workspace, metadata = pending_command(foundation)
    (workspace / "command-probe-v3" / missing).unlink()
    assert store.pending_ownership(job_id) and not store.ownership_receipts(job_id)
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        store.confirm_command_ownership(job_id, metadata["instance_id"], "0" * 64)
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        store.settle(job_id)
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        store.transition(job_id, "failed", ownership_settled=True)
    store.cancel(job_id)
    store.recover()
    job = store.get(job_id)
    assert job["state"] == "interrupted" and not job["ownership_settled"] and job["reservation_retained"]
    assert store.command_ownership(job_id) == [metadata]


def test_store_recomputes_certificate_and_legacy_confirmation_cannot_clear_command(foundation):
    store, job_id, workspace, metadata = pending_command(foundation)
    certificate = recover_command_ownership(workspace, metadata)
    assert certificate is not None
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        store.confirm_ownership(job_id, metadata["instance_id"])
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        store.confirm_command_ownership(job_id, metadata["instance_id"], "0" * 64)
    write_private_json(workspace / "command-probe-v3/invocations.json", {"version": 1, "entries": [
        {"instance": "c" * 32, "spec": "c" * 64, "authority": "d" * 64}]})
    with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
        store.confirm_command_ownership(job_id, metadata["instance_id"], certificate)
    current = recover_command_ownership(workspace, metadata)
    assert current is not None and current != certificate
    store.confirm_command_ownership(job_id, metadata["instance_id"], current)
    assert not store.pending_ownership(job_id)
    assert store.settle(job_id)["ownership_settled"]


def test_legacy_job_cannot_register_command_execution_authority(foundation):
    store, job_id, workspace, metadata = pending_command(foundation)
    with pytest.raises(PreparationError):
        store.register_command_ownership(job_id, metadata)
