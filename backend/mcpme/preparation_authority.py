"""Host-owned, revision-bound preparation authority and ownership journal.

These methods share the import database transaction with job creation and
attachment. Public clients review a plan; they cannot report verification or
settled ownership. Reading plans and receipts never adopts a process.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
import uuid

from .ownership_receipts import verify_receipt
from .preparation_inputs import parse_preparation_descriptor, descriptor_tools
from .preparation_contracts import ExecutionPolicy, PreparationError, canonical_bytes, contract_digest

MAX_EXECUTION_PLANS = 1024
MAX_OWNERSHIP_RECORDS = 1024


def _uuid(value, code):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PreparationError(code) from None


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PreparationError("invalid_preparation_identity")


class PreparationAuthority:
    def _init_authority(self):
        from .management_workflow_grants import init_job_authority
        init_job_authority(self)
        self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_execution_plans (
            plan_id TEXT PRIMARY KEY, digest TEXT NOT NULL, data TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_job_inputs (
            job_id TEXT PRIMARY KEY, data TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_ownership (
            job_id TEXT NOT NULL, instance_id TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(job_id,instance_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_command_ownership (
            job_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, data TEXT NOT NULL)""")

    def _execution_plan(self, plan_id):
        _uuid(plan_id, "invalid_preparation_plan")
        row = self.db.execute("SELECT data FROM preparation_execution_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise PreparationError("preparation_plan_not_found")
        return json.loads(row["data"])

    def execution_plan(self, import_id, revision, descriptor, policy, *, runner_digest, verifier_digest,
                       runtime_digest, expires_in=300):
        """Compile and persist scope without acquiring dependencies or executing code."""
        for digest in (runner_digest, verifier_digest, runtime_digest):
            _digest(digest)
        if type(expires_in) is not int or not 1 <= expires_in <= 3600:
            raise PreparationError("invalid_preparation_plan_expiry")
        value, observed, recipe = self._bound_descriptor(import_id, revision, descriptor)
        if (not isinstance(policy, ExecutionPolicy) or set(policy.operations) != {"build", "probe"}
                or policy.memory_mb < value.resources.memory_mb):
            raise PreparationError("preparation_policy_mismatch")
        now = time.time()
        scope = {"schema_version": 1, "plan_id": str(uuid.uuid4()), "import_id": import_id,
                 "revision": revision, "source_revision": observed["source"]["revision"],
                 "descriptor_digest": value.digest, "descriptor": value.model_dump(exclude_none=True),
                 "execution_policy": policy.model_dump(), "policy_digest": policy.digest,
                 "runner_digest": runner_digest, "verifier_digest": verifier_digest, "runtime_digest": runtime_digest,
                 "created_at": now, "expires_at": now + expires_in}
        digest = contract_digest("preparation-execution-plan-v1", scope)
        stored = {**scope, "plan_digest": digest, "job_id": None}
        with self.imports._transaction():
            self._recheck_descriptor(value, observed)
            # Explicit planning collects only expired, unused plans. Referenced
            # authority remains available for receipt/restart verification.
            for row in self.db.execute("SELECT plan_id,data FROM preparation_execution_plans").fetchall():
                old = json.loads(row["data"])
                if old["job_id"] is None and old["expires_at"] < now:
                    self.db.execute("DELETE FROM preparation_execution_plans WHERE plan_id=?", (row["plan_id"],))
            if self.db.execute("SELECT count(*) FROM preparation_execution_plans").fetchone()[0] >= MAX_EXECUTION_PLANS:
                raise PreparationError("preparation_plan_limit")
            self.db.execute("INSERT INTO preparation_execution_plans VALUES (?,?,?)",
                            (scope["plan_id"], digest, canonical_bytes(stored).decode()))
        return {**scope, "plan_digest": digest, "form": recipe["manifest"]["form"]}

    def authorize_start(self, plan_id, plan_digest, idempotency_key, *, grant_id=None):
        """Owner intent selects the exact reviewed scope; one plan creates one attempt."""
        _digest(plan_digest)
        _uuid(idempotency_key, "invalid_preparation_key")
        with self.imports._lock:
            plan = self._execution_plan(plan_id)
            if plan["plan_digest"] != plan_digest:
                raise PreparationError("preparation_plan_mismatch")
            from .management_workflow_grants import start_authority, existing_start
            authority = start_authority(self, "preparation", plan, idempotency_key, grant_id)
            if plan["job_id"] is not None:
                row = self.db.execute("SELECT idempotency_key FROM preparation_jobs WHERE job_id=?", (plan["job_id"],)).fetchone()
                if row is None or row["idempotency_key"] != idempotency_key:
                    raise PreparationError("preparation_plan_consumed")
                job = self._get(plan["job_id"])
                existing_start(self, "preparation", job, authority)
                return job
        return self.reserve(plan["import_id"], plan["revision"], plan["descriptor"],
                            ExecutionPolicy.model_validate(plan["execution_policy"]), idempotency_key=idempotency_key,
                            authorization=plan, management_grant_id=grant_id)

    def _check_start_authority(self, plan, value, observed, policy):
        current = self._execution_plan(plan["plan_id"])
        if current != plan or current["job_id"] is not None:
            raise PreparationError("preparation_plan_consumed")
        if time.time() >= current["expires_at"]:
            raise PreparationError("preparation_authority_expired")
        if (current["descriptor_digest"] != value.digest or current["policy_digest"] != policy.digest
                or current["source_revision"] != observed["source"]["revision"]):
            raise PreparationError("preparation_plan_mismatch")

    def _record_start_authority(self, job, plan, value, policy):
        job.update(managed=True, authority_plan_id=plan["plan_id"], authority_plan_digest=plan["plan_digest"],
                   authority_expires_at=plan["expires_at"], execution_started_at=None,
                   runtime_digest=plan["runtime_digest"], workspace=None, verification=None,
                   verification_publication_pending=False,
                   expected_tools=len(descriptor_tools(value)), expected_fixtures=len(value.validation),
                   probe_memory_mb=value.resources.memory_mb)
        inputs = {"descriptor": value.model_dump(exclude_none=True), "execution_policy": policy.model_dump()}
        self.db.execute("INSERT INTO preparation_job_inputs VALUES (?,?)", (job["job_id"], canonical_bytes(inputs).decode()))
        current = {**plan, "job_id": job["job_id"]}
        self.db.execute("UPDATE preparation_execution_plans SET data=? WHERE plan_id=?",
                        (canonical_bytes(current).decode(), plan["plan_id"]))

    def _check_managed_authority(self, job):
        from .management_workflow_grants import check_job_authority
        check_job_authority(self, "preparation", job)
        if not job.get("managed"):
            return
        plan = self._execution_plan(job["authority_plan_id"])
        if (plan["job_id"] != job["job_id"] or plan["plan_digest"] != job["authority_plan_digest"]
                or any(job["binding"][key] != plan[key] for key in
                       ("descriptor_digest", "policy_digest", "runner_digest", "verifier_digest", "source_revision"))
                or job["runtime_digest"] != plan["runtime_digest"]):
            raise PreparationError("preparation_authority_mismatch")
        # Expiry fences queued/snapshotting admission. Admitted execution keeps
        # its original scope and is bounded by the runner's whole-job deadline.
        if job["execution_started_at"] is None and time.time() >= plan["expires_at"]:
            raise PreparationError("preparation_authority_expired")

    def check_authority(self, job_id):
        with self.imports._lock:
            job = self._get(job_id)
            self._current_inputs(job)
            self._check_managed_authority(job)
            if job["state"] in {"cancel_requested", "canceled", "failed", "interrupted"}:
                raise PreparationError("preparation_canceled")
            return job

    def job_inputs(self, job_id):
        with self.imports._lock:
            job = self._get(job_id)
            self._current_inputs(job)
            row = self.db.execute("SELECT data FROM preparation_job_inputs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or not job.get("managed"):
                raise PreparationError("preparation_authority_required")
            value = json.loads(row["data"])
            draft = self.imports._get(job["binding"]["import_id"], job["binding"]["revision"])
            descriptor = parse_preparation_descriptor(value["descriptor"])
            policy = ExecutionPolicy.model_validate(value["execution_policy"])
            if descriptor.digest != job["binding"]["descriptor_digest"] or policy.digest != job["binding"]["policy_digest"]:
                raise PreparationError("preparation_authority_mismatch")
            return descriptor, draft, policy

    def recovery_policy(self, job_id):
        """Read reservation scope after invalidation without granting execution."""
        with self.imports._lock:
            job = self._get(job_id)
            row = self.db.execute("SELECT data FROM preparation_job_inputs WHERE job_id=?", (job_id,)).fetchone()
            if not job.get("managed") or row is None:
                raise PreparationError("preparation_authority_required")
            policy = ExecutionPolicy.model_validate(json.loads(row["data"])["execution_policy"])
            if policy.digest != job["binding"]["policy_digest"]:
                raise PreparationError("preparation_authority_mismatch")
            return policy

    def set_workspace(self, job_id, workspace):
        with self.imports._transaction():
            job = self._get(job_id)
            expected = self.imports.root / "preparations" / job_id
            if not job.get("managed") or not isinstance(workspace, Path) or workspace != expected:
                raise PreparationError("invalid_preparation_workspace")
            self._current_inputs(job)
            if job["state"] not in {"queued", "snapshotting"}:
                raise PreparationError("invalid_preparation_workspace")
            if job["workspace"] not in (None, str(expected)):
                raise PreparationError("invalid_preparation_workspace")
            job["workspace"] = str(expected)
            self._save(job)

    def ownership_receipts(self, job_id):
        with self.imports._lock:
            self._get(job_id)
            return [json.loads(row["data"]) for row in self.db.execute(
                "SELECT data FROM preparation_ownership WHERE job_id=? ORDER BY rowid", (job_id,))]

    def command_ownership(self, job_id):
        with self.imports._lock:
            self._get(job_id)
            return [json.loads(row["data"]) for row in self.db.execute(
                "SELECT data FROM preparation_command_ownership WHERE job_id=?", (job_id,))]

    def pending_ownership(self, job_id):
        """Separate proof formats share reservation/publication guards only."""
        return bool(self.ownership_receipts(job_id) or self.command_ownership(job_id))

    def register_command_ownership(self, job_id, metadata):
        from .contracts import validate_package
        from .preparation_command_owner import validate_command_ownership
        from .private import check_private_file, read_private_json
        validate_command_ownership(metadata)
        with self.imports._lock:
            job = self._get(job_id)
            self._current_inputs(job)
            if (not job.get("managed") or not job.get("workspace") or job["state"] != "verifying"
                    or metadata["job_binding_digest"] != contract_digest("preparation-command-job-binding-v1", job["binding"])):
                raise PreparationError("invalid_command_ownership")
            descriptor, draft, _ = self.job_inputs(job_id)
            family = self.family_for(descriptor)
            if getattr(family, "preparation_owner_kind", None) != "command-v3":
                raise PreparationError("invalid_command_ownership")
        workspace = Path(job["workspace"])
        package = validate_package(workspace / "candidate")
        if (package.digest != metadata["candidate_digest"] or package.manifest.profile != "host-command"
                or package.manifest.command.helper_sha256 != metadata["helper_sha256"]
                or metadata["fixture_digest"] != contract_digest("preparation-command-fixtures-v1",
                    [item.model_dump(exclude_none=True) for item in descriptor.validation])):
            raise PreparationError("invalid_command_ownership")
        expected = family.expected_package_contract(descriptor, draft)
        manifest = package.manifest.model_dump(exclude_none=True)
        if (any(manifest.get(key) != value for key, value in expected.items() if key != "source_artifacts")
                or [item.model_dump() for item in package.lock.artifacts if item.path != "mcpme.preparation.json"]
                != expected["source_artifacts"]):
            raise PreparationError("invalid_command_ownership")
        from .preparation_command_owner import validate_initial_command_owner
        validate_initial_command_owner(workspace, metadata)
        with self.imports._transaction():
            current = self._get(job_id)
            self._current_inputs(current)
            if (current["binding"] != job["binding"] or current.get("workspace") != job["workspace"]
                    or current["state"] != "verifying" or not current.get("managed")):
                raise PreparationError("invalid_command_ownership")
            job = current
            if self.command_ownership(job_id):
                raise PreparationError("duplicate_preparation_ownership")
            self.db.execute("INSERT INTO preparation_command_ownership VALUES (?,?,?)",
                (job_id, metadata["instance_id"], canonical_bytes(metadata).decode()))
            job["ownership_settled"] = False
            self._save(job)

    def confirm_command_ownership(self, job_id, instance_id, verified_native_identity):
        from .preparation_command_owner import recover_command_ownership
        _digest(verified_native_identity)
        with self.imports._lock:
            job = self._get(job_id)
            metadata = next((item for item in self.command_ownership(job_id) if item["instance_id"] == instance_id), None)
            workspace = job.get("workspace")
        if (metadata is None or workspace is None
                or recover_command_ownership(Path(workspace), metadata) != verified_native_identity):
            raise PreparationError("preparation_ownership_unsettled")
        with self.imports._transaction():
            current = self._get(job_id)
            if current.get("workspace") != workspace or self.command_ownership(job_id) != [metadata]:
                raise PreparationError("preparation_ownership_unsettled")
            self.db.execute("DELETE FROM preparation_command_ownership WHERE job_id=? AND instance_id=?",
                (job_id, instance_id))

    def register_ownership(self, job_id, metadata):
        if (not isinstance(metadata, dict) or set(metadata) != {"schema_version", "instance_id", "nonce"}
                or type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
                or not isinstance(metadata["instance_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", metadata["instance_id"])
                or not isinstance(metadata["nonce"], str) or not re.fullmatch(r"[0-9a-f]{64}", metadata["nonce"])):
            raise PreparationError("invalid_preparation_ownership")
        with self.imports._transaction():
            job = self._get(job_id)
            self._current_inputs(job)
            if not job.get("managed") or not job.get("workspace") or job["state"] not in {"preparing", "verifying"}:
                raise PreparationError("invalid_preparation_ownership")
            if len(self.ownership_receipts(job_id)) >= MAX_OWNERSHIP_RECORDS:
                raise PreparationError("preparation_ownership_limit")
            if self.db.execute("SELECT 1 FROM preparation_ownership WHERE job_id=? AND instance_id=?",
                               (job_id, metadata["instance_id"])).fetchone():
                raise PreparationError("duplicate_preparation_ownership")
            self.db.execute("INSERT INTO preparation_ownership VALUES (?,?,?)",
                            (job_id, metadata["instance_id"], canonical_bytes(metadata).decode()))
            job["ownership_settled"] = False
            self._save(job)

    def confirm_ownership(self, job_id, instance_id):
        with self.imports._lock:
            job = self._get(job_id)
            metadata = next((item for item in self.ownership_receipts(job_id) if item["instance_id"] == instance_id), None)
            workspace = job.get("workspace")
        if metadata is None or workspace is None or not verify_receipt(Path(workspace), metadata):
            raise PreparationError("preparation_ownership_unsettled")
        with self.imports._transaction():
            self._get(job_id)
            self.db.execute("DELETE FROM preparation_ownership WHERE job_id=? AND instance_id=?", (job_id, instance_id))
