"""Durable preparation authority, ownership and exact-result admission.

Jobs and recipe state deliberately share the import database and write lock.
Only the host's independent verifier can publish managed runtime evidence;
there is no management route for claiming verification or admitting a result.
Public execution activation remains controlled by the supervisor's gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pydantic import ValidationError

from .contracts import PackageError, _read_file, validate_package
from .preparation_components import builtin_component_registry
from .preparation_authority import PreparationAuthority
from .preparation_inputs import (parse_preparation_descriptor, descriptor_tools, parse_preparation_receipt)
from .preparation_contracts import (
    ExecutionPolicy, JobBinding, PreparationError, PreparationReceipt,
    MAX_CONTRACT_BYTES, RUNNER_IDENTITY, VERIFIER_IDENTITY, contract_digest, schema_accepts,
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
PROGRESS_STAGES = frozenset({"queued", "snapshotting", "dependency_export", "dependency_acquisition",
    "dependency_install", "private_python_version", "private_uv_version", "private_python_metadata",
    "onnx_model_validation", "sealing", "verifying", "attaching", "settling", "succeeded"})
PROGRESS_COUNTERS = {"commands_finished": 4096, "wheels_acquired": 4096,
                     "bytes_acquired": 64 * 1024**3, "files_checked": 200000, "fixtures_checked": 64}


class PreparationStore(PreparationAuthority):
    def __init__(self, imports, recipes, *, resolutions=None, families=()):
        self.imports, self.recipes = imports, recipes
        self.resolutions = resolutions
        self.components = builtin_component_registry()
        from .preparation_resolved_python import ResolvedPythonFamily
        default = ResolvedPythonFamily(resolutions)
        self.families = {default.component.reference.key: default}
        for family in families:
            if family.component.reference.key in self.families:
                raise PreparationError("duplicate_preparation_component")
            for component in getattr(family, "components", (family.component,)):
                self.components.register(component)
            self.families[family.component.reference.key] = family
        self.db = imports._connection
        with imports._transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                input_digest TEXT NOT NULL, data TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS preparation_selections (
                import_id TEXT PRIMARY KEY, data TEXT NOT NULL)""")
            self._init_authority()

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
                 "source_revision": job["binding"]["source_revision"],
                 "revision": job["binding"]["revision"], "state": job["state"], "updated_at": job["updated_at"],
                 "reservation_retained": job["reservation_retained"], "error_code": job["error_code"],
                 "progress": job.get("progress")}
                for job in self.list()]

    def record_progress(self, job_id, stage, **counters):
        """Persist only bounded host stage/counter metadata, never worker output."""
        if not isinstance(stage, str) or stage not in PROGRESS_STAGES or not set(counters) <= set(PROGRESS_COUNTERS):
            raise PreparationError("invalid_preparation_progress")
        if any(type(value) is not int or not 0 <= value <= PROGRESS_COUNTERS[key] for key, value in counters.items()):
            raise PreparationError("invalid_preparation_progress")
        with self.imports._transaction():
            job = self._get(job_id)
            if job["state"] in TERMINAL:
                raise PreparationError("invalid_preparation_progress")
            previous = job.get("progress") or {"stage": "queued", **{key: 0 for key in PROGRESS_COUNTERS}}
            if any(value < previous[key] for key, value in counters.items()):
                raise PreparationError("invalid_preparation_progress")
            job["progress"] = {**previous, "stage": stage, **counters}
            self._save(job)
            return job

    def _bound_descriptor(self, import_id, revision, value, *, enforce_selection=True):
        descriptor = parse_preparation_descriptor(value)
        # Full bundle hashing/schema evaluation occurs outside the connection
        # lock and write transaction. Admission rechecks only registry metadata.
        recipe = self.recipes.resolve(descriptor.recipe.id, descriptor.recipe.version, descriptor.recipe.digest)
        with self.imports._lock:
            draft = self.imports._get(import_id, revision)
            self.imports._mutable(draft)
            if enforce_selection:
                self._check_selection(descriptor, draft)
        if draft["source"]["kind"] == "prepared_package":
            raise PreparationError("preparation_not_required")
        manifest = recipe["manifest"]
        if not schema_accepts(manifest["input_schema"], descriptor.inputs):
            raise PreparationError("recipe_input_mismatch")
        self.components.validate_source(descriptor, draft, recipe)
        if descriptor.schema_version == 2:
            self.resolved_inputs(descriptor, draft)
        profile = (getattr(self.family_for(descriptor), "target_profile", "mcp-stdio")
                   if descriptor.schema_version == 2 else "mcp-stdio")
        if profile not in {"mcp-stdio", "mcp-http", "model-service"}:
            raise PreparationError("unsupported_execution_profile")
        if draft["requirements"].get("profile") not in {None, profile}:
            raise PreparationError("unsupported_execution_profile")
        if draft["requirements"].get("secret_references"):
            raise PreparationError("secret_reference_unavailable")
        required_tools = draft["requirements"].get("tools", [])
        if required_tools and set(required_tools) != {tool.name for tool in descriptor_tools(descriptor)}:
            raise PreparationError("tool_requirement_mismatch")
        memory = draft["requirements"].get("memory_mb")
        if memory is not None and descriptor.resources.memory_mb > memory:
            raise PreparationError("resource_requirement_mismatch")
        return descriptor, draft, recipe

    def resolved_inputs(self, descriptor, draft):
        return self.family_for(descriptor).validate_inputs(descriptor, draft)

    def family_for(self, descriptor):
        if descriptor.schema_version != 2:
            raise PreparationError("unsupported_component_descriptor")
        selected = {item.component.key for item in descriptor.components}
        matching = [family for family in self.families.values() if selected == {
            item.reference.key for item in getattr(family, "components", (family.component,))}]
        if not matching:
            raise PreparationError("unknown_preparation_component")
        if len(matching) != 1:
            raise PreparationError("ambiguous_preparation_provider")
        return matching[0]

    def inspect_source(self, draft):
        from .preparation_inspection import inspect_source
        kind = draft["source"]["kind"]
        matching = [family for family in self.families.values() if
            (family.supports_source(draft) if hasattr(family, "supports_source") else family.source_kind == kind)]
        inspections = ([inspect_source(draft)] if kind in {"local_folder", "document"} or not matching else [])
        inspections.extend(family.inspect_source(draft) for family in matching)
        if any(item["source_revision"] != draft["source"]["revision"] for item in inspections):
            raise PreparationError("source_identity_mismatch")
        candidates = [candidate for item in inspections for candidate in item["candidates"]]
        if len({item["candidate_id"] for item in candidates}) != len(candidates):
            raise PreparationError("ambiguous_preparation_provider")
        return {"schema_version": 1, "source_revision": draft["source"]["revision"],
                "candidates": sorted(candidates, key=lambda item: item["candidate_id"]),
                "blockers": [] if candidates else sorted({code for item in inspections for code in item["blockers"]})}

    def release_family_inputs(self, job_id):
        with self.imports._lock:
            row = self.db.execute("SELECT data FROM preparation_job_inputs WHERE job_id=?", (job_id,)).fetchone()
        if row is not None:
            descriptor = parse_preparation_descriptor(json.loads(row["data"])["descriptor"])
            if descriptor.schema_version == 2:
                self.family_for(descriptor).release(job_id, descriptor)

    def plan(self, import_id, revision, descriptor):
        value, observed, recipe = self._bound_descriptor(import_id, revision, descriptor)
        with self.imports._lock:
            draft = self._recheck_descriptor(value, observed)
            return {"schema_version": 1, "import_id": import_id, "revision": revision,
                    "source_revision": draft["source"]["revision"], "descriptor_digest": value.digest,
                    "recipe": value.recipe.model_dump(), "form": recipe["manifest"]["form"],
                    "execution_available": False,
                    "blockers": ["preparation_runner_unavailable", "independent_runtime_verifier_unavailable"],
                    "tools": [tool.model_dump(exclude_none=True) for tool in descriptor_tools(value)],
                    "access": value.access.model_dump(), "resources": value.resources.model_dump()}

    def _recheck_descriptor(self, descriptor, observed, *, enforce_selection=True):
        """Metadata-only check; caller holds the read lock or admission write."""
        current = self.imports._get(observed["draft_id"], observed["revision"])
        self.imports._mutable(current)
        if current["source"] != observed["source"]:
            raise PreparationError("source_identity_mismatch")
        if enforce_selection:
            self._check_selection(descriptor, current)
        selected = descriptor.recipe
        self.recipes.assert_admissible(selected.id, selected.version, selected.digest)
        return current

    def selected_descriptor(self, import_id):
        with self.imports._lock:
            row = self.db.execute("SELECT data FROM preparation_selections WHERE import_id=?", (import_id,)).fetchone()
            return json.loads(row["data"]) if row is not None else None

    def _check_selection(self, descriptor, draft):
        selected = self.selected_descriptor(draft["draft_id"])
        if selected is not None and (selected["revision"] != draft["revision"]
                or selected["source_revision"] != draft["source"]["revision"]
                or selected["descriptor_digest"] != descriptor.digest):
            raise PreparationError("preparation_selection_mismatch")

    def select_descriptor(self, import_id, revision, descriptor, *, candidate_id, form_digest, resolution_digest=None):
        """Atomically bind resolved questions to a new editable revision.

        This grants no execution authority. Repeated identical resolution is
        idempotent; every changed choice invalidates previous revision plans.
        Legacy drafts without a selection keep their existing descriptor API.
        """
        from .imports import RETENTION_SECONDS, _choices, _remove_attachment_tree
        from pathlib import Path
        for digest in (candidate_id, form_digest):
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise PreparationError("invalid_preparation_form")
        value, observed, _ = self._bound_descriptor(import_id, revision, descriptor, enforce_selection=False)
        proof = None
        if value.schema_version == 2:
            family = self.family_for(value)
            binding = family.resolution_binding(value) if hasattr(family, "resolution_binding") else None
            if binding is not None:
                if binding[0] != candidate_id or resolution_digest not in (None, binding[1]):
                    raise PreparationError("resolution_result_mismatch")
                resolution_digest = binding[1]
                proof = self.resolutions.prepare_result_binding(import_id, revision, candidate_id, resolution_digest)
            elif resolution_digest is not None:
                raise PreparationError("resolution_result_mismatch")
        elif resolution_digest is not None:
            raise PreparationError("resolution_result_mismatch")
        previous_attachment = None
        with self.imports._transaction():
            current = self._recheck_descriptor(value, observed, enforce_selection=False)
            previous = self.selected_descriptor(import_id)
            if previous is not None and (previous["revision"], previous["source_revision"],
                    previous["descriptor_digest"], previous["candidate_id"]) == (
                    revision, current["source"]["revision"], value.digest, candidate_id):
                return current
            previous_attachment = current["attachment"]
            current["name"] = value.package.name
            _choices(current)
            current["attachment"] = None
            current["revision"] += 1
            current["expires_at"] = time.time() + RETENTION_SECONDS
            self.imports._save(current)
            if proof is not None:
                self.resolutions.commit_result_binding(proof, current["revision"])
            selected = {"revision": current["revision"], "source_revision": current["source"]["revision"],
                "candidate_id": candidate_id, "form_digest": form_digest,
                "descriptor_digest": value.digest, "descriptor": value.model_dump(exclude_none=True)}
            if resolution_digest is not None:
                selected["resolution_digest"] = resolution_digest
            self.db.execute("INSERT OR REPLACE INTO preparation_selections VALUES (?,?)",
                (import_id, json.dumps(selected, sort_keys=True, separators=(",", ":"), allow_nan=False)))
        if previous_attachment is not None and previous_attachment["path"] != current["source"].get("snapshot_path"):
            _remove_attachment_tree(Path(previous_attachment["path"]))
        return current

    @staticmethod
    def _candidate_contract(value, source_revision):
        return {"package_id": value.package.id, "version": value.package.version, "name": value.package.name,
                "source_revision": source_revision, "profile": "mcp-stdio",
                "tools": [tool.model_dump(exclude_none=True) for tool in descriptor_tools(value)],
                "access": value.access.model_dump(), "resources": value.resources.model_dump(),
                "lifecycle": value.lifecycle.model_dump()}

    def reserve(self, import_id, revision, descriptor, policy: ExecutionPolicy, *, idempotency_key: str,
                authorization=None):
        """Host-only admission shared by legacy fixtures and scoped managed starts."""
        try:
            if str(uuid.UUID(idempotency_key)) != idempotency_key:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PreparationError("invalid_preparation_key") from None
        value, observed, recipe_record = self._bound_descriptor(import_id, revision, descriptor)
        bound_inputs = None
        if value.schema_version == 2:
            if authorization is None:
                raise PreparationError("preparation_authority_required")
            bound_inputs = self.family_for(value).bound_inputs(value, observed, recipe_record, self.components,
                authorization["runtime_digest"])
        if not isinstance(policy, ExecutionPolicy):
            raise PreparationError("invalid_preparation_policy")
        if policy.memory_mb < value.resources.memory_mb or set(policy.operations) != {"build", "probe"}:
            raise PreparationError("preparation_policy_mismatch")
        inputs = {"import_id": import_id, "revision": revision, "source_revision": observed["source"]["revision"],
                  "descriptor_digest": value.digest, "policy_digest": policy.digest}
        if authorization is not None:
            inputs.update(plan_id=authorization["plan_id"], runner_digest=authorization["runner_digest"],
                          verifier_digest=authorization["verifier_digest"], runtime_digest=authorization["runtime_digest"])
        identity = contract_digest("preparation-start-v1", inputs)
        candidate_digest = contract_digest("preparation-candidate-v1", self._candidate_contract(value, observed["source"]["revision"]))
        fixture_digest = contract_digest("preparation-fixtures-v1", [item.model_dump() for item in value.validation])
        artifact_digest = contract_digest("artifact-collection-v1", [item.model_dump() for item in value.artifacts])
        if bound_inputs is not None and bound_inputs.captured_collection is not None:
            artifact_digest = bound_inputs.captured_collection.digest
        with self.imports._transaction():
            draft = self._recheck_descriptor(value, observed)
            row = self.db.execute("SELECT input_digest,data FROM preparation_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row is not None:
                if row["input_digest"] != identity:
                    raise PreparationError("preparation_key_conflict")
                return json.loads(row["data"])
            if authorization is not None:
                self._check_start_authority(authorization, value, observed, policy)
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
                recipe=value.recipe.model_dump(), policy_digest=policy.digest,
                runner_digest=RUNNER_IDENTITY if authorization is None else authorization["runner_digest"],
                verifier_digest=VERIFIER_IDENTITY if authorization is None else authorization["verifier_digest"])
            now = time.time()
            job = {"schema_version": 1, "job_id": job_id, "binding": binding.model_dump(), "state": "queued",
                   "created_at": now, "updated_at": now, "error_code": None, "reservation_retained": True,
                   "ownership_settled": True, "expected_package_digest": None, "receipt_digest": None,
                   "dependency_lock_digest": value.dependencies.lock_sha256 if value.schema_version == 1 else None,
                   "candidate_contract_digest": candidate_digest, "fixture_digest": fixture_digest,
                   "progress": {"stage": "queued", **{key: 0 for key in PROGRESS_COUNTERS}}}
            if bound_inputs is not None:
                job["bound_inputs"] = bound_inputs.model_dump(mode="json")
                job["bound_inputs_digest"] = bound_inputs.digest
            if authorization is not None:
                self._record_start_authority(job, authorization, value, policy)
            self.db.execute("INSERT INTO preparation_jobs VALUES (?,?,?,?)", (job_id, idempotency_key, identity,
                json.dumps(job, sort_keys=True, separators=(",", ":"))))
            return job

    def _current_inputs(self, job):
        self._check_managed_authority(job)
        binding = job["binding"]
        draft = self.imports._get(binding["import_id"], binding["revision"])
        self.imports._mutable(draft)
        if draft["source"]["revision"] != binding["source_revision"]:
            raise PreparationError("source_identity_mismatch")
        recipe = binding["recipe"]
        self.recipes.assert_admissible(recipe["id"], recipe["version"], recipe["digest"],
            existing_job=job["execution_started_at"] is not None if job.get("managed") else job["state"] != "queued")

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
            if settled and job.get("managed") and self.ownership_receipts(job_id):
                raise PreparationError("preparation_ownership_unsettled")
            job.update(state=state, error_code=error_code, ownership_settled=settled)
            if state == "preparing" and job.get("managed") and job["execution_started_at"] is None:
                job["execution_started_at"] = time.time()
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
            if job.get("managed") and self.ownership_receipts(job_id):
                raise PreparationError("preparation_ownership_unsettled")
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

    def record_verified(self, job_id, package, *, verification=None):
        """Internal verifier result binding, never recipe-reported evidence.

        Managed callers supply the independent SDK report for this exact seal.
        Static recipe enablement and a recipe's receipt never promote runtime
        capability. Legacy synthetic fixtures retain the foundation contract.
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
            receipt = parse_preparation_receipt(data)
        except ValidationError:
            raise PreparationError("invalid_preparation_receipt") from None
        manifest = current.manifest.model_dump(exclude_none=True)
        candidate_digest = contract_digest("preparation-candidate-v1", {
            key: manifest[key] for key in ("package_id", "version", "name", "source_revision", "profile", "tools", "access", "resources", "lifecycle")
        })
        with self.imports._transaction():
            job = self._get(job_id)
            self._current_inputs(job)
            ownership_ok = job["ownership_settled"] or (job.get("managed")
                and job.get("verification_publication_pending") and not self.ownership_receipts(job_id))
            if (job["state"] != "verifying" or not ownership_ok
                    or receipt.binding.model_dump() != job["binding"]
                    or (receipt.schema_version == 1 and (job.get("bound_inputs") is not None
                        or receipt.dependency_lock_digest != job["dependency_lock_digest"]))
                    or (receipt.schema_version == 2 and (receipt.inputs.model_dump(mode="json") != job.get("bound_inputs")
                        or receipt.inputs.digest != job.get("bound_inputs_digest")))
                    or receipt.fixture_digest != job["fixture_digest"]
                    or job.get("managed") and receipt.schema_version == 1 and receipt.runtime_digest != job["runtime_digest"]
                    or candidate_digest != job["candidate_contract_digest"]
                    or current.manifest.source_revision != job["binding"]["source_revision"]):
                raise PreparationError("preparation_receipt_mismatch")
            if job.get("managed"):
                from dataclasses import asdict
                from .preparation_verifier import VerificationReport
                if (not isinstance(verification, VerificationReport) or verification.package_digest != current.digest
                        or verification.protocol_version != "2025-11-25"
                        or type(verification.tools_checked) is not int or verification.tools_checked != job["expected_tools"]
                        or type(verification.fixtures_checked) is not int or verification.fixtures_checked != job["expected_fixtures"]
                        or type(verification.peak_memory_bytes) is not int
                        or not 0 < verification.peak_memory_bytes <= job["probe_memory_mb"] * 1024 * 1024):
                    raise PreparationError("independent_verification_required")
                job["verification"] = asdict(verification)
            job.update(state="ready_to_attach", expected_package_digest=current.digest,
                       receipt_digest=artifact.sha256, ownership_settled=True)
            if job.get("managed"):
                job["verification_publication_pending"] = False
            self._save(job)
            return job

    def begin_verification_publication(self, job_id):
        """Retain ownership while off-loop package hashes precede ready publication."""
        with self.imports._transaction():
            job = self._get(job_id)
            self._current_inputs(job)
            if (not job.get("managed") or job["state"] != "verifying" or not job["ownership_settled"]
                    or self.ownership_receipts(job_id)):
                raise PreparationError("preparation_ownership_unsettled")
            job.update(ownership_settled=False, verification_publication_pending=True)
            self._save(job)
            return job

    def begin_attachment(self, job_id):
        """Retain copying ownership before the asynchronous attachment stage."""
        with self.imports._transaction():
            job = self._get(job_id)
            self._current_inputs(job)
            if not job.get("managed") or job["state"] != "ready_to_attach" or not job.get("verification"):
                raise PreparationError("preparation_not_verified")
            job.update(state="attaching", ownership_settled=False)
            if job.get("progress") is not None:
                job["progress"]["stage"] = "attaching"
            self._save(job)
            return job

    def admit(self, job_id, stage):
        """Use the same atomic winner as cancellation, draft edits and revoke."""
        def admission(draft, sealed):
            job = self._get(job_id)
            if job["state"] not in ({"attaching"} if job.get("managed") else {"ready_to_attach"}):
                raise PreparationError("preparation_not_verified")
            if job.get("managed") and self.ownership_receipts(job_id) or not job.get("managed") and not job["ownership_settled"]:
                raise PreparationError("preparation_ownership_unsettled")
            if job.get("managed") and (not job.get("verification")
                    or job["verification"]["package_digest"] != job["expected_package_digest"]):
                raise PreparationError("independent_verification_required")
            self._current_inputs(job)
            binding = job["binding"]
            receipt = next((item for item in sealed.lock.artifacts if item.path == RECEIPT_NAME), None)
            if (binding["import_id"] != draft["draft_id"] or binding["revision"] != draft["revision"]
                    or sealed.digest != job["expected_package_digest"]
                    or receipt is None or receipt.sha256 != job["receipt_digest"]):
                raise PreparationError("preparation_result_mismatch")
            job.update(state="succeeded", reservation_retained=False, ownership_settled=True)
            if job.get("progress") is not None:
                job["progress"]["stage"] = "succeeded"
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
