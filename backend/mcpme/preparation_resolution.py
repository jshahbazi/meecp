"""Explicit, durable authority for derived dependency and interface inputs.

Resolution never edits an import snapshot and never authorizes preparation or
installation. Only the owned runner can publish a settled resolution result.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
import uuid
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Digest, Tool
from .preparation_components import ComponentReference
from .preparation_admission import PublicPreparationAdmission
from .preparation_contracts import PreparationError, SourceIdentity, UUID, canonical_bytes, check_schema, contract_digest
from .preparation_inputs import CollectionReference, DerivedInputReference, InputContract, RuntimeReference
from .preparation_inspection import inspect_source
from .recipes import _read

TERMINAL = frozenset({"succeeded", "failed", "canceled", "interrupted"})
MAX_RESOLUTIONS = 1024
PROGRESS_LIMITS = {"commands_finished": 4096, "wheels_acquired": 4096, "bytes_acquired": 8 * 1024**3,
                   "files_checked": 200000, "tools_discovered": 128}
RESULT_FILES = {"dependency-project": "pyproject.toml", "dependency-lock": "uv.lock", "interface": "interface.json"}


@dataclass(frozen=True)
class CheckedResolutionBinding:
    """In-process proof, never deserialized from client data."""
    owner: object
    import_id: str
    observed_revision: int
    prior_revision: int
    candidate_id: str
    source: bytes
    result: bytes
    job_id: str
    job: bytes


class ResolutionPolicyV1(InputContract):
    schema_version: Literal[1]
    operations: tuple[Literal["resolve_dependencies", "discover_interface"], ...] = Field(min_length=1, max_length=2)
    network: tuple[Literal["pypi.org", "files.pythonhosted.org"], ...] = Field(max_length=2)
    wall_seconds: int = Field(ge=1, le=600)
    disk_bytes: int = Field(ge=1024 * 1024, le=8 * 1024**3)
    memory_mb: int = Field(ge=128, le=4096)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.operations)) != len(self.operations) or len(set(self.network)) != len(self.network):
            raise PreparationError("invalid_resolution_policy")
        return self

    @property
    def digest(self):
        return contract_digest("preparation-resolution-policy-v1", self.model_dump(mode="json"))


class ResolutionRequestV1(InputContract):
    schema_version: Literal[1]
    import_id: UUID
    revision: int = Field(ge=1)
    source_revision: SourceIdentity
    candidate_id: Digest
    candidate_kind: Literal["python-callable", "python-mcp"]
    candidate_digest: Digest
    policy: ResolutionPolicyV1
    runtime: RuntimeReference
    producer: ComponentReference

    @property
    def digest(self):
        return contract_digest("preparation-resolution-request-v1", self.model_dump(mode="json"))


class ResolutionResultV1(InputContract):
    schema_version: Literal[1]
    request: ResolutionRequestV1
    collection: CollectionReference
    derived_inputs: tuple[DerivedInputReference, ...] = Field(min_length=2, max_length=3)
    tools: tuple[Tool, ...] = Field(max_length=128)
    protocol_version: Literal["2025-11-25"] | None = None
    peak_memory_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def closure(self):
        expected = {"dependency-project", "dependency-lock"}
        if "discover_interface" in self.request.policy.operations:
            expected.add("interface")
        if (self.collection.kind != "derived_input_collection" or {item.role for item in self.derived_inputs} != expected
                or len(self.derived_inputs) != len(expected) or len({tool.name for tool in self.tools}) != len(self.tools)
                or self.peak_memory_bytes > self.request.policy.memory_mb * 1024**2):
            raise PreparationError("resolution_result_mismatch")
        if ("interface" in expected) != (self.protocol_version is not None and bool(self.tools)):
            raise PreparationError("resolution_result_mismatch")
        if "interface" not in expected and self.tools:
            raise PreparationError("resolution_result_mismatch")
        for item in self.derived_inputs:
            if (item.name != item.role or item.collection_digest != self.collection.digest
                    or item.source_revision != self.request.source_revision or item.producer != self.request.producer
                    or item.request_digest != self.request.digest or item.policy_digest != self.request.policy.digest
                    or item.parent_digests != (self.request.source_revision.removeprefix("sha256:"), self.request.runtime.digest)):
                raise PreparationError("resolution_result_mismatch")
        for tool in self.tools:
            check_schema(tool.input_schema)
            if tool.output_schema is not None:
                check_schema(tool.output_schema)
        return self

    @property
    def digest(self):
        return contract_digest("preparation-resolution-result-v1", self.model_dump(mode="json"))


class AcceptedResolutionReferenceV1(InputContract):
    schema_version: Literal[1]
    import_id: UUID
    prior_revision: int = Field(ge=1)
    revision: int = Field(ge=1)
    candidate_id: Digest
    candidate_digest: Digest
    source_revision: SourceIdentity
    result_digest: Digest
    original_request_digest: Digest

    @model_validator(mode="after")
    def forward_only(self):
        if self.revision < self.prior_revision:
            raise PreparationError("invalid_resolution_binding")
        return self


def result_from_files(request, directory, tools, protocol_version, peak_memory_bytes):
    roles = ["dependency-project", "dependency-lock"] + (["interface"] if protocol_version else [])
    if {path.name for path in directory.iterdir()} != {RESULT_FILES[role] for role in roles}:
        raise PreparationError("resolution_result_changed")
    records = []
    for role in roles:
        data = _read(directory / RESULT_FILES[role], 16 * 1024 * 1024)
        if role == "interface":
            interface = json.loads(data)
            if (set(interface) != {"protocol_version", "tools", "entrypoint"}
                    or interface["protocol_version"] != protocol_version
                    or interface["tools"] != [tool.model_dump() for tool in tools]):
                raise PreparationError("resolution_result_mismatch")
        records.append({"role": role, "path": RESULT_FILES[role], "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    digest = contract_digest("preparation-derived-input-collection-v1", records)
    return ResolutionResultV1(request=request.model_dump(mode="json"), schema_version=1,
        collection={"kind": "derived_input_collection", "schema_version": 1, "digest": digest},
        derived_inputs=[DerivedInputReference(name=item["role"], role=item["role"], collection_digest=digest,
            sha256=item["sha256"], size=item["size"], producer=request.producer.model_dump(), source_revision=request.source_revision,
            request_digest=request.digest, policy_digest=request.policy.digest,
            parent_digests=[request.source_revision.removeprefix("sha256:"), request.runtime.digest]).model_dump(mode="json") for item in records],
        tools=[tool.model_dump() for tool in tools], protocol_version=protocol_version, peak_memory_bytes=peak_memory_bytes)


class ResolutionStore:
    def __init__(self, imports, *, public_admission=None, current_producer=None):
        if public_admission is not None and (type(public_admission) is not PublicPreparationAdmission
                or not callable(current_producer)):
            raise PreparationError("invalid_public_preparation_admission")
        if public_admission is None and current_producer is not None:
            raise PreparationError("invalid_public_preparation_admission")
        self.public_admission, self._current_producer = public_admission, current_producer
        self.imports, self.db = imports, imports._connection
        self._binding_owner = object()
        with imports._transaction():
            from .management_workflow_grants import init_job_authority
            init_job_authority(self)
            self.db.execute("CREATE TABLE IF NOT EXISTS resolution_plans (plan_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS resolution_jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS accepted_resolutions (
                import_id TEXT NOT NULL, revision INTEGER NOT NULL, candidate_id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(import_id,revision,candidate_id))""")

    def _get(self, job_id):
        if not isinstance(job_id, str):
            raise PreparationError("invalid_resolution_identity")
        row = self.db.execute("SELECT data FROM resolution_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise PreparationError("resolution_not_found")
        return json.loads(row["data"])

    def _save(self, job):
        job["updated_at"] = time.time()
        self.db.execute("UPDATE resolution_jobs SET data=? WHERE job_id=?", (canonical_bytes(job).decode(), job["job_id"]))

    def get(self, job_id):
        with self.imports._lock:
            return self._get(job_id)

    def list(self):
        with self.imports._lock:
            return [json.loads(row["data"]) for row in self.db.execute("SELECT data FROM resolution_jobs ORDER BY rowid")]

    def list_summaries(self):
        return [{"job_id": item["job_id"], "import_id": item["request"]["import_id"],
            "revision": item["request"]["revision"], "source_revision": item["request"]["source_revision"],
            "candidate_id": item["request"]["candidate_id"], "state": item["state"], "stage": item["stage"],
            "result_digest": item["result_digest"], "error_code": item["error_code"],
            "ownership_settled": item["ownership_settled"], "reservation_retained": item["reservation_retained"],
            "updated_at": item["updated_at"], "progress": item.get("progress", {key: 0 for key in PROGRESS_LIMITS})}
            for item in self.list()]

    def progress(self, job_id, **counters):
        if not set(counters) <= set(PROGRESS_LIMITS) or any(type(value) is not int or not 0 <= value <= PROGRESS_LIMITS[key]
                for key, value in counters.items()):
            raise PreparationError("invalid_resolution_progress")
        with self.imports._transaction():
            job = self._get(job_id)
            self.check_authority(job_id)
            previous = job.get("progress", {key: 0 for key in PROGRESS_LIMITS})
            if any(value < previous[key] for key, value in counters.items()):
                raise PreparationError("invalid_resolution_progress")
            job["progress"] = {**previous, **counters}
            self._save(job)

    def candidate(self, import_id, revision, candidate_id):
        with self.imports._lock:
            draft = copy.deepcopy(self.imports._get(import_id, revision))
            self.imports._mutable(draft)
            if draft["expires_at"] <= time.time():
                raise PreparationError("resolution_authority_expired")
        inspection = inspect_source(draft)
        candidate = next((item for item in inspection["candidates"] if item["candidate_id"] == candidate_id), None)
        if candidate is None or candidate["kind"] not in {"python-callable", "python-mcp"}:
            raise PreparationError("resolution_candidate_mismatch")
        if self.public_admission is not None:
            self.public_admission.require_candidate(candidate)
        with self.imports._lock:
            current = self.imports._get(import_id, revision)
            self.imports._mutable(current)
            if current["source"] != draft["source"]:
                raise PreparationError("source_identity_mismatch")
        return draft, candidate

    def require_public_request(self, request, candidate=None):
        """Off-loop exact source/producer fence, outside write transactions."""
        if self.public_admission is None:
            return
        with self.imports._lock:
            if self.db.in_transaction:
                raise PreparationError("resolution_prevalidation_transaction")
        self.public_admission.require_request(request)
        draft, original = ResolutionStore.candidate(self, request.import_id, request.revision, request.candidate_id)
        if (draft["source"]["revision"] != request.source_revision or original["kind"] != request.candidate_kind
                or contract_digest("preparation-resolution-candidate-v1", original) != request.candidate_digest
                or candidate is not None and candidate != original):
            raise PreparationError("resolution_candidate_not_admitted")
        self.require_public_producer(request)

    def require_public_producer(self, request):
        if self.public_admission is None:
            return
        self.public_admission.check_current(full=True)
        self.require_public_kind(request)
        if self.current_producer_for(request) != request.producer:
            raise PreparationError("resolution_implementation_changed")

    def require_public_kind(self, request):
        """Metadata-only domain guard; trusted dispatchers may narrow further."""
        if self.public_admission is not None:
            self.public_admission.check_current()
            self.public_admission.require_request(request)

    def current_producer_for(self, request):
        return self._current_producer()

    def plan(self, request, candidate, *, expires_in):
        if type(expires_in) is not int or not 1 <= expires_in <= 300:
            raise PreparationError("invalid_resolution_expiry")
        self.require_public_request(request, candidate)
        value = {"schema_version": 1, "plan_id": str(uuid.uuid4()), "request": request.model_dump(mode="json"),
                 "candidate": candidate, "expires_at": time.time() + expires_in}
        value["plan_digest"] = contract_digest("preparation-resolution-plan-v1", value)
        with self.imports._transaction():
            self._current(request)
            self.db.execute("DELETE FROM resolution_plans WHERE json_extract(data,'$.expires_at')<?", (time.time(),))
            if self.db.execute("SELECT count(*) FROM resolution_plans").fetchone()[0] >= MAX_RESOLUTIONS:
                raise PreparationError("resolution_limit")
            self.db.execute("INSERT INTO resolution_plans VALUES (?,?)", (value["plan_id"], canonical_bytes(value).decode()))
        return value

    def _current(self, request):
        self.require_public_kind(request)
        draft = self.imports._get(request.import_id, request.revision)
        self.imports._mutable(draft)
        if draft["expires_at"] <= time.time():
            raise PreparationError("resolution_authority_expired")
        if draft["source"]["revision"] != request.source_revision:
            raise PreparationError("source_identity_mismatch")
        return draft

    def authorize_start(self, plan_id, plan_digest, idempotency_key, *, grant_id=None):
        try:
            if str(uuid.UUID(idempotency_key)) != idempotency_key:
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise PreparationError("invalid_resolution_identity") from None
        if self.public_admission is not None:
            with self.imports._lock:
                row = self.db.execute("SELECT data FROM resolution_plans WHERE plan_id=?", (plan_id,)).fetchone()
                if row is None:
                    raise PreparationError("resolution_plan_not_found")
                checked_plan_bytes = row["data"]
                checked_plan = json.loads(checked_plan_bytes)
            self.require_public_request(ResolutionRequestV1.model_validate(checked_plan["request"]), checked_plan["candidate"])
        with self.imports._transaction():
            from .management_workflow_grants import start_authority, existing_start
            authority_plan = None
            if grant_id is not None:
                authority_plan_row = self.db.execute("SELECT data FROM resolution_plans WHERE plan_id=?", (plan_id,)).fetchone()
                if authority_plan_row is None:
                    raise PreparationError("resolution_plan_not_found")
                authority_plan = json.loads(authority_plan_row[0])
            authority = start_authority(self, "resolution", authority_plan, idempotency_key, grant_id)
            previous = self.db.execute("SELECT data FROM resolution_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if previous:
                job = json.loads(previous["data"])
                if job["plan_id"] != plan_id or job["plan_digest"] != plan_digest:
                    raise PreparationError("resolution_idempotency_conflict")
                if self.public_admission is not None and (job["request"] != checked_plan["request"]
                        or job["candidate"] != checked_plan["candidate"]):
                    raise PreparationError("resolution_plan_mismatch")
                existing_start(self, "resolution", job, authority)
                return job
            row = self.db.execute("SELECT data FROM resolution_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise PreparationError("resolution_plan_not_found")
            if self.public_admission is not None and row["data"] != checked_plan_bytes:
                raise PreparationError("resolution_plan_mismatch")
            plan = json.loads(row["data"])
            if plan["plan_digest"] != plan_digest:
                raise PreparationError("resolution_plan_mismatch")
            if plan["expires_at"] <= time.time():
                raise PreparationError("resolution_authority_expired")
            self._current(ResolutionRequestV1.model_validate(plan["request"]))
            if self.db.execute("SELECT count(*) FROM resolution_jobs").fetchone()[0] >= MAX_RESOLUTIONS:
                raise PreparationError("resolution_limit")
            job = {**plan, "schema_version": 2, "management_authority": authority,
                   "job_id": str(uuid.uuid4()), "state": "queued", "stage": "queued", "workspace": None,
                   "ownership": [], "ownership_settled": True, "reservation_retained": False,
                   "error_code": None, "result": None, "result_digest": None, "updated_at": time.time(),
                   "progress": {key: 0 for key in PROGRESS_LIMITS}}
            self.db.execute("INSERT INTO resolution_jobs VALUES (?,?,?)", (job["job_id"], idempotency_key, canonical_bytes(job).decode()))
            from .management_workflow_grants import record_job_authority
            record_job_authority(self, "resolution", job)
            ledger = getattr(self, "management_execution_grants", None)
            if ledger is not None:
                ledger.bind_job("resolution", job)
            return job

    def check_authority(self, job_id):
        with self.imports._lock:
            job = self._get(job_id)
            from .management_workflow_grants import check_job_authority
            check_job_authority(self, "resolution", job)
            if job["state"] in TERMINAL or job["state"] == "cancel_requested":
                raise PreparationError("resolution_canceled")
            # The plan must still be current while queued for resource admission.
            if job["state"] == "queued" and job["expires_at"] <= time.time():
                raise PreparationError("resolution_authority_expired")
            return copy.deepcopy(self._current(ResolutionRequestV1.model_validate(job["request"])))

    def begin(self, job_id, workspace):
        with self.imports._transaction():
            self.check_authority(job_id)
            job = self._get(job_id)
            if job["state"] != "queued":
                raise PreparationError("invalid_resolution_transition")
            job.update(state="running", stage="snapshotting", workspace=str(workspace), ownership_settled=False, reservation_retained=True)
            self._save(job)

    def stage(self, job_id, stage):
        if stage not in {"snapshotting", "resolving", "installing", "discovering", "publishing"}:
            raise PreparationError("invalid_resolution_stage")
        with self.imports._transaction():
            self.check_authority(job_id)
            job = self._get(job_id)
            job["stage"] = stage
            self._save(job)

    def cancel(self, job_id):
        with self.imports._transaction():
            job = self._get(job_id)
            if job["state"] not in TERMINAL:
                job["state"] = "cancel_requested"
                self._save(job)
            return job

    def register_ownership(self, job_id, metadata):
        with self.imports._transaction():
            self.check_authority(job_id)
            job = self._get(job_id)
            if len(job["ownership"]) >= 1024:
                raise PreparationError("resolution_ownership_limit")
            job["ownership"].append(metadata)
            self._save(job)

    def ownership_receipts(self, job_id):
        return self.get(job_id)["ownership"]

    def confirm_ownership(self, job_id, instance_id):
        with self.imports._transaction():
            job = self._get(job_id)
            job["ownership"] = [item for item in job["ownership"] if item["instance_id"] != instance_id]
            self._save(job)

    def finish(self, job_id, error_code=None, *, interrupted=False):
        with self.imports._transaction():
            job = self._get(job_id)
            settled = not job["ownership"]
            if job["state"] not in TERMINAL:
                canceled = job["state"] == "cancel_requested"
                job["state"] = "interrupted" if interrupted or not settled else "canceled" if canceled else "failed" if error_code else "canceled"
                job["error_code"] = None if canceled and settled else error_code
            job.update(ownership_settled=settled, reservation_retained=not settled)
            self._save(job)
            return job

    def publish(self, job_id, result):
        request = result.request
        self.require_public_request(request)
        draft, candidate = self.candidate(request.import_id, request.revision, request.candidate_id)
        if contract_digest("preparation-resolution-candidate-v1", candidate) != request.candidate_digest:
            raise PreparationError("resolution_candidate_mismatch")
        with self.imports._transaction():
            self.check_authority(job_id)
            job = self._get(job_id)
            if job["ownership"] or job["request"] != request.model_dump(mode="json"):
                raise PreparationError("resolution_ownership_unsettled")
            job.update(state="succeeded", stage="succeeded", result=result.model_dump(mode="json"), result_digest=result.digest,
                       ownership_settled=True, reservation_retained=False)
            self._save(job)
            return job

    def _read_result(self, result_digest):
        with self.imports._lock:
            jobs = [job for job in self.list() if job["state"] == "succeeded" and job["result_digest"] == result_digest]
        if not jobs:
            raise PreparationError("resolution_result_unavailable")
        # Independent authorized attempts may produce identical immutable
        # results. Their content identity does not depend on the attempt UUID.
        job = jobs[-1]
        result = ResolutionResultV1.model_validate(job["result"])
        # A reviewed metadata revision may explicitly carry a result produced
        # at an older revision. Callers inspect the current original candidate;
        # producer continuity is checked here without replaying that old draft.
        self.require_public_producer(result.request)
        if result.digest != result_digest or not job["ownership_settled"] or job["reservation_retained"] or job["ownership"]:
            raise PreparationError("resolution_result_mismatch")
        path = Path(job["workspace"]) / "derived"
        actual = result_from_files(result.request, path, result.tools, result.protocol_version, result.peak_memory_bytes)
        if actual != result:
            raise PreparationError("resolution_result_changed")
        return result, path

    def _accepted(self, import_id, revision, candidate_id, result):
        request = result.request
        if (request.import_id, request.candidate_id) != (import_id, candidate_id):
            raise PreparationError("resolution_result_mismatch")
        if revision == request.revision:
            return
        row = self.db.execute("SELECT data FROM accepted_resolutions WHERE import_id=? AND revision=? AND candidate_id=?",
            (import_id, revision, candidate_id)).fetchone()
        if row is None:
            raise PreparationError("resolution_binding_required")
        binding = AcceptedResolutionReferenceV1.model_validate(json.loads(row["data"]))
        if (binding.result_digest != result.digest or binding.original_request_digest != request.digest
                or binding.source_revision != request.source_revision or binding.candidate_digest != request.candidate_digest):
            raise PreparationError("resolution_result_mismatch")

    def prepare_result_binding(self, import_id, revision, candidate_id, result_digest, *, prior_revision=None):
        """Hash/inspect off-loop before opening the caller's write transaction."""
        with self.imports._lock:
            if self.db.in_transaction:
                raise PreparationError("resolution_prevalidation_transaction")
        prior_revision = revision if prior_revision is None else prior_revision
        draft, candidate = self.candidate(import_id, revision, candidate_id)
        result, _ = self._read_result(result_digest)
        if (draft["source"]["revision"] != result.request.source_revision
                or contract_digest("preparation-resolution-candidate-v1", candidate) != result.request.candidate_digest):
            raise PreparationError("resolution_result_mismatch")
        with self.imports._lock:
            current = self.imports._get(import_id, revision)
            self.imports._mutable(current)
            if current["source"] != draft["source"]:
                raise PreparationError("source_identity_mismatch")
            self._accepted(import_id, prior_revision, candidate_id, result)
            jobs = [job for job in self.list() if job["state"] == "succeeded" and job["result_digest"] == result_digest]
            if not jobs:
                raise PreparationError("resolution_result_unavailable")
            job = jobs[-1]
            if job["result"] != result.model_dump(mode="json") or job["ownership"] or not job["ownership_settled"] or job["reservation_retained"]:
                raise PreparationError("resolution_result_mismatch")
            return CheckedResolutionBinding(self._binding_owner, import_id, revision, prior_revision, candidate_id,
                canonical_bytes(draft["source"]), canonical_bytes(result.model_dump(mode="json")), job["job_id"], canonical_bytes(job))

    def commit_result_binding(self, proof, new_revision):
        """Bounded authority fence inside selection's atomic write; no file IO.

        The immutable result is rehashed again when consumed by the builder.
        This metadata carry does not grant authority over later mutated bytes.
        """
        if not isinstance(proof, CheckedResolutionBinding) or proof.owner is not self._binding_owner or not self.db.in_transaction:
            raise PreparationError("resolution_checked_binding_required")
        if new_revision not in {proof.observed_revision, proof.observed_revision + 1}:
            raise PreparationError("resolution_binding_conflict")
        current = self.imports._get(proof.import_id, new_revision)
        self.imports._mutable(current)
        if canonical_bytes(current["source"]) != proof.source or current["expires_at"] <= time.time():
            raise PreparationError("source_identity_mismatch")
        if canonical_bytes(self._get(proof.job_id)) != proof.job:
            raise PreparationError("resolution_result_mismatch")
        result = ResolutionResultV1.model_validate(json.loads(proof.result))
        self.require_public_kind(result.request)
        self._accepted(proof.import_id, proof.prior_revision, proof.candidate_id, result)
        import_id, prior_revision, candidate_id = proof.import_id, proof.prior_revision, proof.candidate_id
        binding = AcceptedResolutionReferenceV1(schema_version=1, import_id=import_id, prior_revision=prior_revision,
            revision=new_revision, candidate_id=candidate_id, candidate_digest=result.request.candidate_digest,
            source_revision=result.request.source_revision, result_digest=result.digest, original_request_digest=result.request.digest)
        raw = canonical_bytes(binding.model_dump(mode="json")).decode()
        self.db.execute("INSERT OR IGNORE INTO accepted_resolutions VALUES (?,?,?,?)",
            (import_id, new_revision, candidate_id, raw))
        saved = self.db.execute("SELECT data FROM accepted_resolutions WHERE import_id=? AND revision=? AND candidate_id=?",
            (import_id, new_revision, candidate_id)).fetchone()["data"]
        if saved != raw:
            raise PreparationError("resolution_binding_conflict")
        return binding

    def bind_result(self, import_id, prior_revision, new_revision, candidate_id, result_digest):
        """Standalone explicit carry; selection uses prepare/commit around its write."""
        proof = self.prepare_result_binding(import_id, new_revision, candidate_id, result_digest, prior_revision=prior_revision)
        with self.imports._transaction():
            return self.commit_result_binding(proof, new_revision)

    def resolve_result(self, import_id, revision, candidate_id, result_digest):
        draft, candidate = self.candidate(import_id, revision, candidate_id)
        result, path = self._read_result(result_digest)
        if (draft["source"]["revision"] != result.request.source_revision
                or contract_digest("preparation-resolution-candidate-v1", candidate) != result.request.candidate_digest):
            raise PreparationError("resolution_result_mismatch")
        with self.imports._lock:
            current = self.imports._get(import_id, revision)
            self.imports._mutable(current)
            if current["source"] != draft["source"]:
                raise PreparationError("source_identity_mismatch")
            self._accepted(import_id, revision, candidate_id, result)
        return result, path
