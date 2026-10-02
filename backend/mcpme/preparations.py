"""Preparation admission foundation; no builder or executable worker is enabled.

Jobs and recipe state deliberately share the import database and write lock.
The host's future independent verifier is the only caller of record_verified;
there is no management route for claiming verification or admitting a result.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pydantic import ValidationError

from .contracts import PackageError, _read_file, validate_package
from .preparation_contracts import (
    ExecutionPolicy, JobBinding, PreparationError, PreparationReceipt,
    MAX_CONTRACT_BYTES, RUNNER_IDENTITY, VERIFIER_IDENTITY, contract_digest, load_contract, parse_descriptor, schema_accepts,
)

RECEIPT_NAME = "mcpme.preparation.json"
TERMINAL = {"succeeded", "canceled", "failed", "interrupted"}
TRANSITIONS = {
    "queued": {"snapshotting", "canceled", "failed"},
    "snapshotting": {"preparing", "cancel_requested", "failed", "interrupted"},
    "preparing": {"verifying", "cancel_requested", "failed", "interrupted"},
    "verifying": {"ready_to_attach", "cancel_requested", "failed", "interrupted"},
    "ready_to_attach": {"attaching", "cancel_requested", "failed", "interrupted"},
    "attaching": {"succeeded", "cancel_requested", "failed", "interrupted"},
    "cancel_requested": {"canceled", "interrupted"},
    **{state: set() for state in TERMINAL},
}
MAX_JOBS = 1024


class PreparationStore:
    def __init__(self, imports, recipes):
        self.imports, self.recipes = imports, recipes
        self.db = imports._connection
        with imports._transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                input_digest TEXT NOT NULL, data TEXT NOT NULL)""")

    def _get(self, job_id):
        try:
            if str(uuid.UUID(job_id)) != job_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PreparationError("invalid_preparation_identity") from None
        row = self.db.execute("SELECT data FROM preparation_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise PreparationError("preparation_not_found")
        return json.loads(row["data"])

    def _save(self, job):
        job["updated_at"] = time.time()
        self.db.execute("UPDATE preparation_jobs SET data=? WHERE job_id=?",
                        (json.dumps(job, sort_keys=True, separators=(",", ":"), allow_nan=False), job["job_id"]))

    def get(self, job_id):
        with self.imports._lock:
            return self._get(job_id)

    def list(self):
        with self.imports._lock:
            return [json.loads(row["data"]) for row in self.db.execute("SELECT data FROM preparation_jobs ORDER BY rowid")]

    def list_summaries(self):
        """Bound the public collection independently of detailed job bindings."""
        return [{"job_id": job["job_id"], "import_id": job["binding"]["import_id"],
                 "revision": job["binding"]["revision"], "state": job["state"], "updated_at": job["updated_at"],
                 "reservation_retained": job["reservation_retained"], "error_code": job["error_code"]}
                for job in self.list()]

    def _bound_descriptor(self, import_id, revision, value):
        descriptor = parse_descriptor(value)
        # Full bundle hashing/schema evaluation occurs outside the connection
        # lock and write transaction. Admission rechecks only registry metadata.
        recipe = self.recipes.resolve(descriptor.recipe.id, descriptor.recipe.version, descriptor.recipe.digest)
        with self.imports._lock:
            draft = self.imports._get(import_id, revision)
            self.imports._mutable(draft)
        if draft["source"]["kind"] == "prepared_package":
            raise PreparationError("preparation_not_required")
        manifest = recipe["manifest"]
        if not schema_accepts(manifest["input_schema"], descriptor.inputs):
            raise PreparationError("recipe_input_mismatch")
        component = "python-function-v1" if descriptor.interface.kind == "python-callable" else "existing-mcp-v1"
        if not any(step["component"] == component for step in manifest["steps"]):
            raise PreparationError("recipe_interface_mismatch")
        files = {item["path"]: item for item in draft["source"]["files"]}
        prefix = "" if descriptor.dependencies.project == "." else descriptor.dependencies.project + "/"
        lock = files.get(prefix + "uv.lock")
        if lock is None or lock["sha256"] != descriptor.dependencies.lock_sha256 or prefix + "pyproject.toml" not in files:
            raise PreparationError("dependency_lock_mismatch")
        if descriptor.source_license not in files or not files[descriptor.source_license]["size"]:
            raise PreparationError("source_license_required")
        module = descriptor.interface.module.replace(".", "/")
        if module + ".py" not in files and module + "/__init__.py" not in files:
            raise PreparationError("entrypoint_source_required")
        if draft["requirements"].get("profile") not in {None, "mcp-stdio"}:
            raise PreparationError("unsupported_execution_profile")
        if draft["requirements"].get("secret_references"):
            raise PreparationError("secret_reference_unavailable")
        required_tools = draft["requirements"].get("tools", [])
        if required_tools and set(required_tools) != {tool.name for tool in descriptor.interface.tools}:
            raise PreparationError("tool_requirement_mismatch")
        memory = draft["requirements"].get("memory_mb")
        if memory is not None and descriptor.resources.memory_mb > memory:
            raise PreparationError("resource_requirement_mismatch")
        return descriptor, draft, recipe

    def plan(self, import_id, revision, descriptor):
        value, observed, recipe = self._bound_descriptor(import_id, revision, descriptor)
        with self.imports._lock:
            draft = self._recheck_descriptor(value, observed)
            return {"schema_version": 1, "import_id": import_id, "revision": revision,
                    "source_revision": draft["source"]["revision"], "descriptor_digest": value.digest,
                    "recipe": value.recipe.model_dump(), "form": recipe["manifest"]["form"],
                    "execution_available": False,
                    "blockers": ["preparation_runner_unavailable", "independent_runtime_verifier_unavailable"],
                    "tools": [tool.model_dump(exclude_none=True) for tool in value.interface.tools],
                    "access": value.access.model_dump(), "resources": value.resources.model_dump()}

    def _recheck_descriptor(self, descriptor, observed):
        """Metadata-only check; caller holds the read lock or admission write."""
        current = self.imports._get(observed["draft_id"], observed["revision"])
        self.imports._mutable(current)
        if current["source"] != observed["source"]:
            raise PreparationError("source_identity_mismatch")
        selected = descriptor.recipe
        self.recipes.assert_admissible(selected.id, selected.version, selected.digest)
        return current

    @staticmethod
    def _candidate_contract(value, source_revision):
        return {"package_id": value.package.id, "version": value.package.version, "name": value.package.name,
                "source_revision": source_revision, "profile": "mcp-stdio",
                "tools": [tool.model_dump(exclude_none=True) for tool in value.interface.tools],
                "access": value.access.model_dump(), "resources": value.resources.model_dump(),
                "lifecycle": value.lifecycle.model_dump()}

    def reserve(self, import_id, revision, descriptor, policy: ExecutionPolicy, *, idempotency_key: str):
        """Internal host-only contract harness. No public execution admission yet."""
        try:
            if str(uuid.UUID(idempotency_key)) != idempotency_key:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PreparationError("invalid_preparation_key") from None
        value, observed, _ = self._bound_descriptor(import_id, revision, descriptor)
        if not isinstance(policy, ExecutionPolicy):
            raise PreparationError("invalid_preparation_policy")
        if policy.memory_mb < value.resources.memory_mb or set(policy.operations) != {"build", "probe"}:
            raise PreparationError("preparation_policy_mismatch")
        inputs = {"import_id": import_id, "revision": revision, "source_revision": observed["source"]["revision"],
                  "descriptor_digest": value.digest, "policy_digest": policy.digest}
        identity = contract_digest("preparation-start-v1", inputs)
        candidate_digest = contract_digest("preparation-candidate-v1", self._candidate_contract(value, observed["source"]["revision"]))
        fixture_digest = contract_digest("preparation-fixtures-v1", [item.model_dump() for item in value.validation])
        artifact_digest = contract_digest("artifact-collection-v1", [item.model_dump() for item in value.artifacts])
        with self.imports._transaction():
            draft = self._recheck_descriptor(value, observed)
            row = self.db.execute("SELECT input_digest,data FROM preparation_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row is not None:
                if row["input_digest"] != identity:
                    raise PreparationError("preparation_key_conflict")
                return json.loads(row["data"])
            if self.db.execute("SELECT count(*) FROM preparation_jobs").fetchone()[0] >= MAX_JOBS:
                raise PreparationError("preparation_limit")
            for job in self.list():
                if (job["binding"]["import_id"] == import_id
                        and (job["state"] not in TERMINAL or job["reservation_retained"])):
                    raise PreparationError("preparation_in_progress")
            job_id = str(uuid.uuid4())
            binding = JobBinding(schema_version=1, job_id=job_id, import_id=import_id, revision=revision,
                source_revision=draft["source"]["revision"], descriptor_digest=value.digest,
                artifact_collection_digest=artifact_digest,
                recipe=value.recipe.model_dump(), policy_digest=policy.digest, runner_digest=RUNNER_IDENTITY, verifier_digest=VERIFIER_IDENTITY)
            now = time.time()
            job = {"schema_version": 1, "job_id": job_id, "binding": binding.model_dump(), "state": "queued",
                   "created_at": now, "updated_at": now, "error_code": None, "reservation_retained": True,
                   "ownership_settled": True, "expected_package_digest": None, "receipt_digest": None,
                   "dependency_lock_digest": value.dependencies.lock_sha256,
                   "candidate_contract_digest": candidate_digest, "fixture_digest": fixture_digest}
            self.db.execute("INSERT INTO preparation_jobs VALUES (?,?,?,?)", (job_id, idempotency_key, identity,
                json.dumps(job, sort_keys=True, separators=(",", ":"))))
            return job

    def _current_inputs(self, job):
        binding = job["binding"]
        draft = self.imports._get(binding["import_id"], binding["revision"])
        self.imports._mutable(draft)
        if draft["source"]["revision"] != binding["source_revision"]:
            raise PreparationError("source_identity_mismatch")
        recipe = binding["recipe"]
        self.recipes.assert_admissible(recipe["id"], recipe["version"], recipe["digest"],
                                      existing_job=job["state"] != "queued")

    def transition(self, job_id, state, *, ownership_settled: bool | None = None, error_code: str | None = None):
        if ownership_settled is not None and type(ownership_settled) is not bool:
            raise PreparationError("invalid_preparation_ownership")
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code)):
            raise PreparationError("invalid_preparation_error")
        with self.imports._transaction():
            job = self._get(job_id)
            if not isinstance(state, str) or state not in TRANSITIONS[job["state"]]:
                raise PreparationError("invalid_preparation_transition")
            if state not in {"canceled", "cancel_requested", "failed", "interrupted"}:
                self._current_inputs(job)
            if state in {"ready_to_attach", "attaching", "succeeded"}:
                raise PreparationError("independent_verification_required")
            settled = ownership_settled
            if settled is None:
                settled = False if state in {"snapshotting", "preparing", "verifying"} else job["ownership_settled"]
            if state in {"canceled", "failed"} and not settled:
                raise PreparationError("preparation_ownership_unsettled")
            job.update(state=state, error_code=error_code, ownership_settled=settled)
            if state in TERMINAL and settled:
                job["reservation_retained"] = False
            self._save(job)
            return job

    def settle(self, job_id):
        """Internal owner acknowledgement after all copying/process work settles.

        This never discovers/adopts a PID or restarts a step. Readers and public
        cancellation cannot manufacture this acknowledgement.
        """
        with self.imports._transaction():
            job = self._get(job_id)
            job["ownership_settled"] = True
            if job["state"] == "cancel_requested":
                job["state"] = "canceled"
            if job["state"] in TERMINAL:
                job["reservation_retained"] = False
            self._save(job)
            return job

    def cancel(self, job_id):
        with self.imports._transaction():
            job = self._get(job_id)
            if job["state"] in TERMINAL or job["state"] == "cancel_requested":
                return job
            job["state"] = "canceled" if job["ownership_settled"] else "cancel_requested"
            job["reservation_retained"] = not job["ownership_settled"]
            self._save(job)
            return job

    def record_verified(self, job_id, package):
        """Internal verifier result binding, never recipe-reported evidence.

        Callers must independently probe this exact package before calling. The
        first foundation ships no such caller, so no runtime capability is
        promoted by this primitive or by static recipe enablement.
        """
        try:
            current = validate_package(package.root)
            if current.digest != package.digest:
                raise PreparationError("preparation_result_mismatch")
        except PackageError as error:
            raise PreparationError(error.code) from None
        artifact = next((item for item in current.lock.artifacts if item.path == RECEIPT_NAME), None)
        if artifact is None or artifact.size > MAX_CONTRACT_BYTES:
            raise PreparationError("preparation_receipt_required")
        try:
            data = _read_file(current.root / RECEIPT_NAME, MAX_CONTRACT_BYTES)
        except PackageError as error:
            raise PreparationError(error.code) from None
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise PreparationError("preparation_receipt_mismatch")
        try:
            receipt = PreparationReceipt.model_validate(load_contract(data))
        except ValidationError:
            raise PreparationError("invalid_preparation_receipt") from None
        manifest = current.manifest.model_dump(exclude_none=True)
        candidate_digest = contract_digest("preparation-candidate-v1", {
            key: manifest[key] for key in ("package_id", "version", "name", "source_revision", "profile", "tools", "access", "resources", "lifecycle")
        })
        with self.imports._transaction():
            job = self._get(job_id)
            self._current_inputs(job)
            if (job["state"] != "verifying" or not job["ownership_settled"]
                    or receipt.binding.model_dump() != job["binding"]
                    or receipt.dependency_lock_digest != job["dependency_lock_digest"]
                    or receipt.fixture_digest != job["fixture_digest"]
                    or candidate_digest != job["candidate_contract_digest"]
                    or current.manifest.source_revision != job["binding"]["source_revision"]):
                raise PreparationError("preparation_receipt_mismatch")
            job.update(state="ready_to_attach", expected_package_digest=current.digest,
                       receipt_digest=artifact.sha256)
            self._save(job)
            return job

    def admit(self, job_id, stage):
        """Use the same atomic winner as cancellation, draft edits and revoke."""
        def admission(draft, sealed):
            job = self._get(job_id)
            if job["state"] != "ready_to_attach":
                raise PreparationError("preparation_not_verified")
            self._current_inputs(job)
            binding = job["binding"]
            receipt = next((item for item in sealed.lock.artifacts if item.path == RECEIPT_NAME), None)
            if (binding["import_id"] != draft["draft_id"] or binding["revision"] != draft["revision"]
                    or sealed.digest != job["expected_package_digest"]
                    or receipt is None or receipt.sha256 != job["receipt_digest"]):
                raise PreparationError("preparation_result_mismatch")
            job.update(state="succeeded", reservation_retained=False, ownership_settled=True)
            self._save(job)
        return self.imports.admit_attachment(stage, admission=admission)

    def recover(self):
        """Explicit coordinator restart recovery. Never invoked by readers."""
        with self.imports._transaction():
            for job in self.list():
                if job["state"] not in TERMINAL:
                    # There is no process adoption or PID lookup. A live owner
                    # must reconcile/reap before reservations can be released.
                    job.update(state="interrupted", error_code="preparation_interrupted")
                    if job["ownership_settled"]:
                        job["reservation_retained"] = False
                    self._save(job)
