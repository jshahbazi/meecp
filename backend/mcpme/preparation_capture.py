"""Durable local artifact capture, references and owned asynchronous writers.

Capture grants no execution authority. Its journal shares the import database;
only a host callback can admit independently captured bytes to a draft. Recovery
uses recorded leases and inode identities, never orphan discovery or replay.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Annotated, Any, Literal
import uuid

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .contracts import StrictModel
from .imports import _choices
from .preparation_artifacts import (
    AcquisitionRequestV1, ArtifactError, ArtifactStore, CaptureHooks,
    CapturedArtifactCollectionV1, MAX_ARTIFACT_BYTES, MAX_ARTIFACT_FILES,
    MANIFEST_NAME, _DirectoryChain, _directory_identity, _open_directory,
    _private_directory, _publish_directory, _same_directory, _lock_publication, _Budget,
)
from .preparation_contracts import MAX_CONTRACT_BYTES, canonical_bytes, contract_digest

MAX_CAPTURES = 1024
MAX_PENDING = 4
TERMINAL = frozenset({"succeeded", "canceled", "failed", "interrupted"})
UUIDText = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", strict=True)]
DigestText = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**31-1)]


class _CaptureContract(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            if "schema_version" in value and type(value["schema_version"]) is not int:
                raise ValueError("artifact_capture_contract_invalid")
            if "memory_mb" in value and type(value["memory_mb"]) is not int:
                raise ValueError("artifact_capture_contract_invalid")
            canonical_bytes(value)
        return value


class CapturePolicyV1(_CaptureContract):
    schema_version: Literal[1] = 1
    disk_ceiling_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_ARTIFACT_BYTES + MAX_CONTRACT_BYTES)]
    wall_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 300
    memory_mb: Literal[64] = 64

    @model_validator(mode="after")
    def fixed_memory(self):
        if type(self.memory_mb) is not int:
            raise ValueError("artifact_capture_contract_invalid")
        return self

    @property
    def digest(self):
        return contract_digest("artifact-capture-policy-v1", self.model_dump(mode="json"))


class ArtifactStoragePolicyV1(_CaptureContract):
    schema_version: Literal[1] = 1
    max_retained_bytes: Annotated[int, Field(strict=True, ge=1, le=128 * 1024**3)] = 64 * 1024**3
    retention_seconds: Annotated[int, Field(strict=True, ge=0, le=30 * 24 * 3600)] = 30 * 24 * 3600


class CreateImportTargetV1(_CaptureContract):
    kind: Literal["create-import"] = "create-import"
    name: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    intent: Annotated[str, Field(strict=True, max_length=16384)] = ""
    document_role: Literal["instructions", "reference"] | None = None
    start: bool = False
    start_policy: Literal["manual", "host"] = "manual"
    requirements: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def choices(self):
        if len(canonical_bytes(self.requirements)) > 16384:
            raise ValueError("artifact_capture_contract_invalid")
        _choices(self.model_dump(mode="json"))
        return self


class AttachImportTargetV1(_CaptureContract):
    kind: Literal["attach-to-import"] = "attach-to-import"
    import_id: UUIDText
    revision: Revision


CaptureTarget = Annotated[CreateImportTargetV1 | AttachImportTargetV1, Field(discriminator="kind")]
_TARGET = TypeAdapter(CaptureTarget)


class CaptureAdmissionV1(_CaptureContract):
    import_id: UUIDText
    revision: Revision


class ArtifactReferenceV1(_CaptureContract):
    kind: Literal["draft", "job", "read"]
    owner_id: UUIDText
    owner_revision: Annotated[int, Field(strict=True, ge=0, le=2**31-1)] = 0

    @model_validator(mode="after")
    def revision(self):
        if self.kind == "draft" and self.owner_revision == 0:
            raise ValueError("artifact_reference_invalid")
        return self


def _validated(model, value, code="artifact_capture_contract_invalid"):
    try:
        raw = value.model_dump(mode="json") if isinstance(value, StrictModel) else value
        return model.validate_python(raw) if isinstance(model, TypeAdapter) else model.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        raise ArtifactError(code) from None


def _uuid(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ArtifactError("artifact_capture_identity_invalid") from None


def _dump(value):
    return canonical_bytes(value).decode("utf-8")


def _summary(record):
    return {key: record[key] for key in (
        "schema_version", "capture_id", "state", "created_at", "updated_at",
        "expires_at", "policy", "target", "collection_digest", "result",
        "ownership_settled", "reservation_retained", "error_code",
    )}


class ArtifactCaptureStore:
    """Metadata-only transactions and exact host-owned filesystem receipts."""
    def __init__(self, imports, artifacts: ArtifactStore, *, storage_policy=None, clock=time.time):
        self.imports, self.artifacts, self.db = imports, artifacts, imports._connection
        self.clock = clock
        self.storage_policy = _validated(ArtifactStoragePolicyV1, storage_policy or ArtifactStoragePolicyV1())
        self.owners = imports.root / "artifact-capture-owners"
        with _DirectoryChain(self.owners, create=True) as chain:
            _private_directory(chain.fd)
            self._owners_identity = _directory_identity(os.fstat(chain.fd))
        with imports._transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS artifact_captures (
                capture_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                input_digest TEXT NOT NULL, request TEXT NOT NULL, data TEXT NOT NULL,
                candidate TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS captured_artifact_objects (
                digest TEXT PRIMARY KEY, manifest TEXT NOT NULL, data TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS captured_artifact_references (
                digest TEXT NOT NULL, kind TEXT NOT NULL, owner_id TEXT NOT NULL,
                owner_revision INTEGER NOT NULL,
                PRIMARY KEY(digest,kind,owner_id,owner_revision))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS captured_artifact_gc (
                digest TEXT PRIMARY KEY, data TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS captured_artifact_readers (
                read_id TEXT PRIMARY KEY, data TEXT NOT NULL)""")

    def _metadata(self, capture_id):
        _uuid(capture_id)
        row = self.db.execute("SELECT data FROM artifact_captures WHERE capture_id=?", (capture_id,)).fetchone()
        if row is None:
            raise ArtifactError("artifact_capture_not_found")
        return json.loads(row[0])

    def _get(self, capture_id):
        _uuid(capture_id)
        row = self.db.execute("SELECT * FROM artifact_captures WHERE capture_id=?", (capture_id,)).fetchone()
        if row is None:
            raise ArtifactError("artifact_capture_not_found")
        record = json.loads(row["data"])
        record["request"] = json.loads(row["request"])
        record["candidate"] = json.loads(row["candidate"]) if row["candidate"] else None
        return record

    def _save(self, record):
        data = {key: value for key, value in record.items() if key not in {"request", "candidate"}}
        data["updated_at"] = self.clock()
        self.db.execute("UPDATE artifact_captures SET data=? WHERE capture_id=?", (_dump(data), record["capture_id"]))

    def get(self, capture_id):
        with self.imports._lock:
            return _summary(self._metadata(capture_id))

    def get_record(self, capture_id):
        """Private host input; never return this acquisition request over API."""
        with self.imports._lock:
            return self._get(capture_id)

    def list_summaries(self):
        summaries = [_summary(record) for record in self.recovery_records()]
        for summary in summaries:
            summary["target"] = {key: value for key, value in summary["target"].items() if key in {"kind", "import_id", "revision"}}
        return summaries

    def recovery_records(self, *, only_unsettled=False):
        """Bounded ownership metadata; acquisition requests are loaded singly."""
        with self.imports._lock:
            query = "SELECT data FROM artifact_captures"
            if only_unsettled:
                query += " WHERE json_extract(data,'$.ownership_settled')=0 OR json_extract(data,'$.owner_released')=0"
            return [json.loads(row[0]) for row in self.db.execute(query + " ORDER BY rowid")]

    def recovery_policy(self, capture_id):
        record = self.get_record(capture_id)
        request = _validated(AcquisitionRequestV1, record["request"])
        policy = _validated(CapturePolicyV1, record["policy"])
        target = _validated(_TARGET, record["target"])
        scope = {"request": request.model_dump(mode="json"), "policy": policy.model_dump(mode="json"),
                 "target": target.model_dump(mode="json")}
        if contract_digest("artifact-capture-request-v1", scope) != record["input_digest"]:
            raise ArtifactError("artifact_capture_scope_mismatch")
        return policy

    def unsettled_reservations(self):
        with self.imports._lock:
            identities = [row[0] for row in self.db.execute("SELECT capture_id FROM artifact_captures WHERE json_extract(data,'$.reservation_retained')=1")]
        result = []
        for capture_id in identities:
            policy = self.recovery_policy(capture_id)
            record = self.get(capture_id)
            result.append({"capture_id": capture_id, "policy": policy.model_dump(mode="json"), "policy_digest": policy.digest,
                           "ownership_settled": record["ownership_settled"], "reservation_retained": record["reservation_retained"]})
        return result

    def _target(self, target, callback=None):
        if isinstance(target, AttachImportTargetV1):
            draft = self.imports._get(target.import_id, target.revision)
            self.imports._mutable(draft)
            if draft["expires_at"] <= self.clock():
                raise ArtifactError("artifact_capture_target_expired")
        if callback:
            callback(target)

    def check_target(self, capture_id, validate_target=None):
        with self.imports._lock:
            record = self._get(capture_id)
            if record["state"] == "cancel_requested":
                raise ArtifactError("artifact_capture_canceled")
            if record["expires_at"] <= self.clock():
                raise ArtifactError("artifact_deadline_exceeded")
            self._target(_validated(_TARGET, record["target"]), validate_target)

    def begin(self, request, policy, target, idempotency_key, validate_target=None):
        request = _validated(AcquisitionRequestV1, request)
        policy = _validated(CapturePolicyV1, policy)
        target = _validated(_TARGET, target)
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise ArtifactError("artifact_capture_idempotency_invalid")
        scope = {"request": request.model_dump(mode="json"), "policy": policy.model_dump(mode="json"),
                 "target": target.model_dump(mode="json")}
        digest = contract_digest("artifact-capture-request-v1", scope)
        with self.imports._transaction():
            previous = self.db.execute("SELECT capture_id,input_digest FROM artifact_captures WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if previous:
                if previous["input_digest"] != digest:
                    raise ArtifactError("artifact_capture_idempotency_conflict")
                return _summary(self._get(previous["capture_id"]))
            self._target(target, validate_target)
            count, pending_count = self.db.execute("SELECT count(*),sum(json_extract(data,'$.state') NOT IN ('succeeded','canceled','failed','interrupted')) FROM artifact_captures").fetchone()
            if count >= MAX_CAPTURES or (pending_count or 0) >= MAX_PENDING:
                raise ArtifactError("artifact_capture_busy")
            occupied = sum(json.loads(row[0])["bytes"] for row in self.db.execute("SELECT data FROM captured_artifact_objects"))
            pending = self.db.execute("SELECT coalesce(sum(json_extract(data,'$.policy.disk_ceiling_bytes')),0) FROM artifact_captures WHERE json_extract(data,'$.reservation_retained')=1").fetchone()[0]
            if occupied + pending + policy.disk_ceiling_bytes > self.storage_policy.max_retained_bytes:
                raise ArtifactError("artifact_storage_limit")
            capture_id, now = str(uuid.uuid4()), self.clock()
            record = {"schema_version": 1, "capture_id": capture_id, "state": "queued", "input_digest": digest,
                      "nonce": uuid.uuid4().hex, "stage_name": capture_id,
                      "owner_identity": None, "owner_released": False, "stage_identity": None, "execution_started": False,
                      "policy": policy.model_dump(mode="json"), "target": target.model_dump(mode="json"),
                      "created_at": now, "updated_at": now, "expires_at": now + policy.wall_seconds,
                      "collection_digest": None, "result": None, "ownership_settled": False,
                      "reservation_retained": True, "error_code": None}
            self.db.execute("INSERT INTO artifact_captures VALUES (?,?,?,?,?,NULL)",
                            (capture_id, idempotency_key, digest, _dump(scope["request"]), _dump(record)))
            return _summary(record)

    def owner_metadata(self, record):
        return canonical_bytes({"schema_version": 1, "capture_id": record["capture_id"],
                                "nonce": record["nonce"], "input_digest": record["input_digest"],
                                "stage_name": record["stage_name"]})

    def record_owner(self, capture_id, identity):
        with self.imports._transaction():
            record = self._get(capture_id)
            self.recovery_policy(capture_id)
            if record["state"] != "queued" or record["execution_started"]:
                raise ArtifactError("artifact_capture_canceled")
            self._target(_validated(_TARGET, record["target"]))
            if record["expires_at"] <= self.clock():
                raise ArtifactError("artifact_deadline_exceeded")
            record.update(owner_identity=list(identity), execution_started=True, state="copying")
            self._save(record)

    def record_owner_released(self, capture_id):
        with self.imports._transaction():
            record = self._get(capture_id)
            record["owner_released"] = True
            self._save(record)

    def record_stage(self, capture_id, identity):
        with self.imports._transaction():
            record = self._get(capture_id)
            if record["state"] != "copying":
                raise ArtifactError("artifact_capture_canceled")
            record["stage_identity"] = list(identity)
            self._save(record)

    def record_candidate(self, capture_id, collection, identity):
        collection = _validated(CapturedArtifactCollectionV1, collection)
        with self.imports._transaction():
            record = self._get(capture_id)
            if record["state"] != "copying" or record["stage_identity"] != list(identity):
                raise ArtifactError("artifact_capture_canceled")
            record["collection_digest"] = collection.digest
            self.db.execute("UPDATE artifact_captures SET candidate=? WHERE capture_id=?",
                            (_dump(collection.model_dump(mode="json")), capture_id))
            self._save(record)

    def _index(self, collection, identity):
        row = self.db.execute("SELECT manifest,data FROM captured_artifact_objects WHERE digest=?", (collection.digest,)).fetchone()
        manifest = _dump(collection.model_dump(mode="json"))
        if row:
            if row["manifest"] != manifest or json.loads(row["data"])["state"] != "available":
                raise ArtifactError("artifact_object_unavailable")
            return
        now = self.clock()
        data = {"bytes": sum(entry.size for entry in collection.entries) + len(manifest.encode()),
                "identity": list(identity), "state": "available", "created_at": now, "last_used_at": now}
        self.db.execute("INSERT INTO captured_artifact_objects VALUES (?,?,?)", (collection.digest, manifest, _dump(data)))

    def record_published(self, capture_id, collection, identity):
        with self.imports._transaction():
            record = self._get(capture_id)
            if record["candidate"] != collection.model_dump(mode="json") or record["collection_digest"] != collection.digest:
                raise ArtifactError("artifact_identity_mismatch")
            self._index(collection, identity)
            if record["state"] == "copying":
                record["state"] = "published"
            self._save(record)

    def request_cancel(self, capture_id):
        with self.imports._transaction():
            record = self._get(capture_id)
            if record["state"] not in TERMINAL:
                record["state"] = "cancel_requested"
                self._save(record)
            return _summary(record)

    def _reference(self, digest, reference):
        reference = _validated(ArtifactReferenceV1, reference, "artifact_reference_invalid")
        row = self.db.execute("SELECT data FROM captured_artifact_objects WHERE digest=?", (digest,)).fetchone()
        if row is None or json.loads(row[0])["state"] != "available":
            raise ArtifactError("artifact_object_unavailable")
        self.db.execute("INSERT OR IGNORE INTO captured_artifact_references VALUES (?,?,?,?)",
                        (digest, reference.kind, reference.owner_id, reference.owner_revision))
        data = json.loads(row[0]); data["last_used_at"] = self.clock()
        self.db.execute("UPDATE captured_artifact_objects SET data=? WHERE digest=?", (_dump(data), digest))

    def acquire_reference(self, digest, reference):
        """Host ledger API; call _reference inside an existing admission txn."""
        with self.imports._transaction():
            self._reference(digest, reference)

    def release_reference(self, digest, reference):
        reference = _validated(ArtifactReferenceV1, reference, "artifact_reference_invalid")
        with self.imports._transaction():
            self.db.execute("DELETE FROM captured_artifact_references WHERE digest=? AND kind=? AND owner_id=? AND owner_revision=?",
                            (digest, reference.kind, reference.owner_id, reference.owner_revision))
            row = self.db.execute("SELECT data FROM captured_artifact_objects WHERE digest=?", (digest,)).fetchone()
            if row:
                data = json.loads(row[0]); data["last_used_at"] = self.clock()
                self.db.execute("UPDATE captured_artifact_objects SET data=? WHERE digest=?", (_dump(data), digest))

    def _reconcile_draft_references(self):
        """Metadata-only; caller holds the existing import transaction.

        Transfer surviving captured inputs to the current revision. Jobs/read
        owners are independent and remain pinned after draft deactivation.
        """
        rows = [dict(row) for row in self.db.execute("SELECT * FROM captured_artifact_references WHERE kind='draft'")]
        for row in rows:
            draft_row = self.db.execute("SELECT data FROM drafts WHERE draft_id=?", (row["owner_id"],)).fetchone()
            draft = json.loads(draft_row[0]) if draft_row else None
            current_digest = None
            if draft and draft["state"] not in {"canceled", "expired"} and draft["expires_at"] > self.clock():
                value = (draft["source"].get("collection") if draft["source"]["kind"] == "captured_artifact_collection"
                         else (draft.get("captured_artifacts") or {}).get("collection"))
                if value is not None:
                    current_digest = _validated(CapturedArtifactCollectionV1, value).digest
            if current_digest == row["digest"] and draft["revision"] != row["owner_revision"]:
                self._reference(current_digest, ArtifactReferenceV1(kind="draft", owner_id=draft["draft_id"], owner_revision=draft["revision"]))
            if current_digest != row["digest"] or draft["revision"] != row["owner_revision"]:
                self.db.execute("DELETE FROM captured_artifact_references WHERE digest=? AND kind='draft' AND owner_id=? AND owner_revision=?",
                                (row["digest"], row["owner_id"], row["owner_revision"]))
                stored = self.db.execute("SELECT data FROM captured_artifact_objects WHERE digest=?", (row["digest"],)).fetchone()
                if stored:
                    data = json.loads(stored[0]); data["last_used_at"] = self.clock()
                    self.db.execute("UPDATE captured_artifact_objects SET data=? WHERE digest=?", (_dump(data), row["digest"]))

    def reconcile_draft_references(self):
        with self.imports._transaction():
            self._reconcile_draft_references()

    def references(self, digest):
        with self.imports._lock:
            return [dict(row) for row in self.db.execute("SELECT kind,owner_id,owner_revision FROM captured_artifact_references WHERE digest=?", (digest,))]

    def admit(self, capture_id, collection, callback, validate_target=None, cancel_event=None):
        with self.imports._transaction():
            record = self._get(capture_id)
            if record["state"] != "published" or (cancel_event and cancel_event.is_set()):
                raise ArtifactError("artifact_capture_canceled")
            if record["expires_at"] <= self.clock():
                raise ArtifactError("artifact_deadline_exceeded")
            if collection.model_dump(mode="json") != record["candidate"]:
                raise ArtifactError("artifact_identity_mismatch")
            target = _validated(_TARGET, record["target"])
            self._target(target, validate_target)
            record["state"] = "admitting"
            result = _validated(CaptureAdmissionV1, callback(target, collection, capture_id), "artifact_capture_admission_invalid")
            if cancel_event and cancel_event.is_set():
                raise ArtifactError("artifact_capture_canceled")
            self._reference(collection.digest, ArtifactReferenceV1(kind="draft", owner_id=result.import_id, owner_revision=result.revision))
            record.update(state="succeeded", result=result.model_dump(mode="json"))
            self._save(record)
            return _summary(record)

    def settle(self, capture_id, *, error_code=None, settled=True, interrupted=False):
        with self.imports._transaction():
            record = self._get(capture_id)
            if not settled:
                record.update(state=record["state"] if record["state"] == "succeeded" else "interrupted", ownership_settled=False, reservation_retained=True,
                              error_code="artifact_capture_reconciliation_required")
            else:
                if record["state"] != "succeeded":
                    record["state"] = ("canceled" if record["state"] == "cancel_requested" or error_code == "artifact_capture_canceled"
                                       else "interrupted" if interrupted else "failed")
                record.update(ownership_settled=True, reservation_retained=False,
                              error_code=None if record["state"] == "succeeded" else error_code)
            self._save(record)
            return _summary(record)

    def _owners(self):
        chain = _DirectoryChain(self.owners)
        if _directory_identity(os.fstat(chain.fd)) != self._owners_identity:
            chain.close()
            raise ArtifactError("artifact_capture_reconciliation_required")
        return chain

    @contextlib.contextmanager
    def _owner_coordination(self):
        with self._owners() as chain:
            fcntl.flock(chain.fd, fcntl.LOCK_EX)
            try:
                yield chain
                chain.check()
            finally:
                fcntl.flock(chain.fd, fcntl.LOCK_UN)

    def create_owner(self, capture_id):
        record = self.get_record(capture_id)
        metadata = self.owner_metadata(record)
        with self._owner_coordination() as chain:
            name = capture_id + ".owner"
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=chain.fd)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _write_all(descriptor, metadata)
                os.fsync(descriptor); os.fsync(chain.fd)
                identity = _owner_identity(descriptor, metadata)
                self.record_owner(capture_id, identity)
                return _Lease(descriptor, identity, metadata)
            except BaseException:
                # Keep a durable unclaimed owner for recovery; no bytes can be
                # copied until record_owner has committed.
                os.close(descriptor)
                raise

    def recover_owner(self, record):
        with self._owner_coordination() as chain:
            try:
                descriptor = os.open(record["capture_id"] + ".owner", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=chain.fd)
            except FileNotFoundError:
                if not record["execution_started"]:
                    return None
                raise ArtifactError("artifact_capture_reconciliation_required") from None
            try:
                metadata = self.owner_metadata(record)
                identity = _owner_identity(descriptor, metadata)
                if record["owner_identity"] is not None and identity != tuple(record["owner_identity"]):
                    raise ArtifactError("artifact_capture_reconciliation_required")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ArtifactError("artifact_capture_owner_live") from None
                return _Lease(descriptor, identity, metadata)
            except BaseException:
                os.close(descriptor)
                raise

    def release_owner(self, record, lease):
        if lease is None:
            return
        try:
            with self._owner_coordination() as chain:
                current = os.stat(record["capture_id"] + ".owner", dir_fd=chain.fd, follow_symlinks=False)
                if _directory_identity(current) != lease.identity or _owner_identity(lease.fd, lease.metadata) != lease.identity:
                    raise ArtifactError("artifact_capture_reconciliation_required")
                os.unlink(record["capture_id"] + ".owner", dir_fd=chain.fd)
                os.fsync(chain.fd)
        finally:
            os.close(lease.fd)

    def reconcile_files(self, record):
        """No writer is live; index only the exact persisted candidate."""
        if record["candidate"] is not None:
            candidate = _validated(CapturedArtifactCollectionV1, record["candidate"])
            try:
                with self.artifacts._verified_object(candidate.digest, None, time.monotonic() + 3600) as (fd, actual, _):
                    if actual != candidate:
                        raise ArtifactError("artifact_identity_mismatch")
                    self.record_published(record["capture_id"], actual, _directory_identity(os.fstat(fd)))
            except ArtifactError as error:
                # A candidate may still be an unpublished stage. Only absence
                # is normal; a present invalid object retains ownership cost.
                with self.artifacts._root() as chain:
                    objects = _open_directory(chain.fd, "objects")
                    try:
                        try:
                            os.stat(candidate.digest, dir_fd=objects, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise error
                    finally:
                        os.close(objects)
        with self.artifacts._root() as chain:
            staging = _open_directory(chain.fd, "staging")
            try:
                try:
                    stage = _open_directory(staging, record["stage_name"])
                except FileNotFoundError:
                    return
                try:
                    if record["stage_identity"] is None or _directory_identity(os.fstat(stage)) != tuple(record["stage_identity"]):
                        raise ArtifactError("artifact_capture_reconciliation_required")
                    _remove_known_tree(stage, {entry["name"] for entry in record["request"]["entries"]})
                    chain.check(); _same_directory(chain.fd, "staging", staging)
                    _same_directory(staging, record["stage_name"], stage)
                    os.rmdir(record["stage_name"], dir_fd=staging); os.fsync(staging)
                finally:
                    os.close(stage)
            finally:
                os.close(staging)

    @contextlib.contextmanager
    def open_artifact(self, digest, name, *, cancel_event=None, deadline=None):
        read_id = str(uuid.uuid4())
        reference = ArtifactReferenceV1(kind="read", owner_id=read_id)
        reader = {"read_id": read_id, "digest": digest, "name": name, "nonce": uuid.uuid4().hex,
                  "identity": None, "settled": False}
        lease = None
        with self.imports._transaction():
            self._reference(digest, reference)
            self.db.execute("INSERT INTO captured_artifact_readers VALUES (?,?)", (read_id, _dump(reader)))
        try:
            lease = self._create_reader_owner(reader)
            with self.artifacts.open_artifact(digest, name, cancel_event=cancel_event, deadline=deadline) as descriptor:
                yield descriptor
        finally:
            if lease is not None:
                self._settle_reader(reader, lease)

    def _reader_metadata(self, reader):
        return canonical_bytes({key: reader[key] for key in ("read_id", "digest", "name", "nonce")})

    def _create_reader_owner(self, reader):
        metadata = self._reader_metadata(reader)
        with self._owner_coordination() as chain:
            descriptor = os.open("read-" + reader["read_id"] + ".owner", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=chain.fd)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _write_all(descriptor, metadata); os.fsync(descriptor); os.fsync(chain.fd)
                identity = _owner_identity(descriptor, metadata)
                with self.imports._transaction():
                    reader["identity"] = list(identity)
                    self.db.execute("UPDATE captured_artifact_readers SET data=? WHERE read_id=?", (_dump(reader), reader["read_id"]))
                return _Lease(descriptor, identity, metadata)
            except BaseException:
                os.close(descriptor)
                raise

    def _settle_reader(self, reader, lease):
        try:
            with self.imports._transaction():
                self.db.execute("DELETE FROM captured_artifact_references WHERE digest=? AND kind='read' AND owner_id=?", (reader["digest"], reader["read_id"]))
                reader["settled"] = True
                self.db.execute("UPDATE captured_artifact_readers SET data=? WHERE read_id=?", (_dump(reader), reader["read_id"]))
            if lease:
                with self._owner_coordination() as chain:
                    name = "read-" + reader["read_id"] + ".owner"
                    current = os.stat(name, dir_fd=chain.fd, follow_symlinks=False)
                    if _directory_identity(current) != lease.identity or _owner_identity(lease.fd, lease.metadata) != lease.identity:
                        raise ArtifactError("artifact_capture_reconciliation_required")
                    os.unlink(name, dir_fd=chain.fd); os.fsync(chain.fd)
            with self.imports._transaction():
                self.db.execute("DELETE FROM captured_artifact_readers WHERE read_id=?", (reader["read_id"],))
        finally:
            if lease:
                os.close(lease.fd)

    def recover_readers(self):
        with self.imports._lock:
            readers = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM captured_artifact_readers")]
        for reader in readers:
            lease = None
            with self._owner_coordination() as chain:
                try:
                    fd = os.open("read-" + reader["read_id"] + ".owner", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=chain.fd)
                except FileNotFoundError:
                    if reader["identity"] is not None and not reader["settled"]:
                        continue
                else:
                    try:
                        metadata = self._reader_metadata(reader)
                        identity = _owner_identity(fd, metadata)
                        if reader["identity"] is not None and identity != tuple(reader["identity"]):
                            raise ArtifactError("artifact_capture_reconciliation_required")
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        lease = _Lease(fd, identity, metadata)
                    except (ArtifactError, BlockingIOError):
                        os.close(fd)
                        continue
            self._settle_reader(reader, lease)

    def reader_recovery_records(self):
        with self.imports._lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM captured_artifact_readers")]

    def collect(self, digest):
        """Conservative known-object GC, serialized with reference admission."""
        with self.artifacts._verified_object(digest, None, time.monotonic() + 3600) as (fd, collection, _):
            identity = _directory_identity(os.fstat(fd))
        with self.imports._transaction():
            row = self.db.execute("SELECT data FROM captured_artifact_objects WHERE digest=?", (digest,)).fetchone()
            if row is None:
                raise ArtifactError("artifact_object_unavailable")
            data = json.loads(row[0])
            if self.references(digest) or data["state"] != "available" or data["last_used_at"] + self.storage_policy.retention_seconds > self.clock():
                raise ArtifactError("artifact_collection_retained")
            if tuple(data["identity"]) != identity:
                raise ArtifactError("artifact_capture_reconciliation_required")
            intent = {"digest": digest, "name": str(uuid.uuid4()), "identity": list(identity),
                      "entries": [entry.name for entry in collection.entries]}
            self.db.execute("INSERT INTO captured_artifact_gc VALUES (?,?)", (digest, _dump(intent)))
            data["state"] = "collecting"
            self.db.execute("UPDATE captured_artifact_objects SET data=? WHERE digest=?", (_dump(data), digest))
        self._finish_gc(intent)

    def _finish_gc(self, intent):
        with self.artifacts._root() as chain:
            objects = _open_directory(chain.fd, "objects")
            quarantine = -1
            try:
                # The directory lease serializes the complete recorded GC
                # operation with recovery and ArtifactStore publication. A
                # second finisher observes either the original, quarantine,
                # or completed absence; it never deletes a concurrent claim.
                _lock_publication(objects, _Budget(MAX_ARTIFACT_BYTES + MAX_CONTRACT_BYTES, None, time.monotonic() + 3600))
                with self.imports._lock:
                    current = self.db.execute("SELECT data FROM captured_artifact_gc WHERE digest=?", (intent["digest"],)).fetchone()
                    if current is None:
                        return
                    if json.loads(current[0]) != intent:
                        raise ArtifactError("artifact_capture_reconciliation_required")
                quarantine = _open_directory(chain.fd, "quarantine", create=True)
                try:
                    fd = _open_directory(quarantine, intent["name"])
                except FileNotFoundError:
                    try:
                        fd = _open_directory(objects, intent["digest"])
                    except FileNotFoundError:
                        fd = None
                    if fd is not None:
                        if _directory_identity(os.fstat(fd)) != tuple(intent["identity"]):
                            os.close(fd)
                            raise ArtifactError("artifact_capture_reconciliation_required")
                        _publish_directory(objects, intent["digest"], quarantine, intent["name"])
                        os.fsync(objects); os.fsync(quarantine)
                if fd is not None:
                    try:
                        if _directory_identity(os.fstat(fd)) != tuple(intent["identity"]):
                            raise ArtifactError("artifact_capture_reconciliation_required")
                        _remove_known_tree(fd, set(intent["entries"]))
                        chain.check(); _same_directory(chain.fd, "quarantine", quarantine)
                        _same_directory(quarantine, intent["name"], fd)
                        os.rmdir(intent["name"], dir_fd=quarantine); os.fsync(quarantine)
                    finally:
                        os.close(fd)
                # Commit removal before allowing a new publication of this
                # digest; its new index cannot be deleted by this old intent.
                with self.imports._transaction():
                    self.db.execute("DELETE FROM captured_artifact_objects WHERE digest=?", (intent["digest"],))
                    self.db.execute("DELETE FROM captured_artifact_gc WHERE digest=?", (intent["digest"],))
            finally:
                if quarantine >= 0:
                    os.close(quarantine)
                os.close(objects)

    def recover_gc(self):
        with self.imports._lock:
            intents = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM captured_artifact_gc")]
        for intent in intents:
            self._finish_gc(intent)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ArtifactError("artifact_capture_owner_invalid")
        view = view[written:]


def _owner_identity(fd, metadata):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(metadata)):
        raise ArtifactError("artifact_capture_reconciliation_required")
    os.lseek(fd, 0, os.SEEK_SET)
    if os.read(fd, len(metadata) + 1) != metadata:
        raise ArtifactError("artifact_capture_reconciliation_required")
    return _directory_identity(info)


def _remove_known_tree(fd, names):
    """Resume partial deletion only under a held, proven private directory."""
    _private_directory(fd)
    present = list(os.listdir(fd))
    if len(present) > 2 or not set(present) <= {MANIFEST_NAME, "files"}:
        raise ArtifactError("artifact_capture_reconciliation_required")
    if "files" in present:
        files = _open_directory(fd, "files")
        try:
            entries = list(os.listdir(files))
            if len(entries) > MAX_ARTIFACT_FILES or not set(entries) <= names:
                raise ArtifactError("artifact_capture_reconciliation_required")
            for name in entries:
                info = os.stat(name, dir_fd=files, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise ArtifactError("artifact_capture_reconciliation_required")
                os.unlink(name, dir_fd=files)
            _same_directory(fd, "files", files)
            os.rmdir("files", dir_fd=fd)
        finally:
            os.close(files)
    if MANIFEST_NAME in present:
        info = os.stat(MANIFEST_NAME, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ArtifactError("artifact_capture_reconciliation_required")
        os.unlink(MANIFEST_NAME, dir_fd=fd)
    os.fsync(fd)


@dataclass
class _Lease:
    fd: int
    identity: tuple[int, ...]
    metadata: bytes


@dataclass
class _Attempt:
    stop: threading.Event = field(default_factory=threading.Event)
    lease: _Lease | None = None
    threads: set = field(default_factory=set)


async def _joined(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


async def _owned_thread(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _joined(task)
        raise


async def _join_worker(task):
    try:
        return await _joined(task)
    except asyncio.CancelledError:
        return None


_SAFE_ERRORS = frozenset({"resource_blocked", "host_closing", "stale_import_revision", "draft_inactive",
                         "draft_finalized", "operation_in_progress", "artifact_capture_target_expired"})


def _error(error):
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"artifact_[a-z_]{1,70}", code):
        return code
    return code if isinstance(code, str) and code in _SAFE_ERRORS else "artifact_capture_failed"


class ArtifactCaptureCoordinator:
    """Durable attempts own their copy, admission and cleanup until joined."""
    def __init__(self, store, *, admit_target, validate_target=None,
                 reserve_resources=None, release_resources=None):
        if not callable(admit_target):
            raise ArtifactError("artifact_capture_admission_required")
        self.store = store
        self.admit_target, self.validate_target = admit_target, validate_target
        self.reserve_resources, self.release_resources = reserve_resources, release_resources
        self.tasks, self.attempts = {}, {}
        self.admissions = set()
        self._serial = asyncio.Semaphore(1)
        self._control_lock = asyncio.Lock()
        self._recovery_task = self._close_task = None
        self.closing = False

    async def start(self, request, policy, target, idempotency_key):
        if self.closing:
            raise ArtifactError("artifact_capture_closing")
        task = asyncio.create_task(self._start(request, policy, target, idempotency_key))
        self.admissions.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _joined(task)
            raise
        finally:
            self.admissions.discard(task)

    async def _start(self, request, policy, target, key):
        await self.recover()
        async with self._control_lock:
            record = await _owned_thread(self.store.begin, request, policy, target, key, self.validate_target)
            capture_id = record["capture_id"]
            if record["state"] == "queued" and capture_id not in self.tasks:
                attempt = _Attempt()
                self.attempts[capture_id] = attempt
                self.tasks[capture_id] = asyncio.create_task(self._work(capture_id, attempt))
            return record

    async def _thread(self, attempt, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        attempt.threads.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            attempt.stop.set()
            await _joined(task)
            raise
        finally:
            attempt.threads.discard(task)

    async def _callback(self, callback, *args, **kwargs):
        if callback is None:
            return
        task = asyncio.create_task(callback(*args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _joined(task)
            raise

    def _capture(self, capture_id, attempt, deadline):
        attempt.lease = self.store.create_owner(capture_id)
        record = self.store.get_record(capture_id)
        return self.store.artifacts.capture(
            _validated(AcquisitionRequestV1, record["request"]),
            disk_ceiling_bytes=record["policy"]["disk_ceiling_bytes"], cancel_event=attempt.stop, deadline=deadline,
            hooks=CaptureHooks(record["stage_name"],
                lambda identity: self.store.record_stage(capture_id, identity),
                lambda collection, identity: self.store.record_candidate(capture_id, collection, identity)))

    async def _monitor(self, capture_id, attempt, worker):
        while not worker.done():
            await asyncio.sleep(0.05)
            try:
                await _owned_thread(self.store.check_target, capture_id, self.validate_target)
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt.stop.set()
                worker.cancel()
                return

    async def _work(self, capture_id, attempt):
        error_code = None
        monitor = None
        try:
            async with self._serial:
                await self._thread(attempt, self.store.check_target, capture_id, self.validate_target)
                record = await self._thread(attempt, self.store.get_record, capture_id)
                policy = await self._thread(attempt, self.store.recovery_policy, capture_id)
                deadline = time.monotonic() + max(0, record["expires_at"] - self.store.clock())
                await self._callback(self.reserve_resources, capture_id, policy)
                monitor = asyncio.create_task(self._monitor(capture_id, attempt, asyncio.current_task()))
                collection = await self._thread(attempt, self._capture, capture_id, attempt, deadline)
                def verify_publish():
                    with self.store.artifacts._verified_object(collection.digest, attempt.stop, deadline) as (fd, actual, _):
                        if actual != collection:
                            raise ArtifactError("artifact_identity_mismatch")
                        self.store.record_published(capture_id, actual, _directory_identity(os.fstat(fd)))
                await self._thread(attempt, verify_publish)
                monitor.cancel()
                await _join_worker(monitor)
                monitor = None
                await self._thread(attempt, self.store.admit, capture_id, collection, self.admit_target,
                                   self.validate_target, attempt.stop)
        except asyncio.CancelledError:
            attempt.stop.set()
            error_code = "artifact_capture_canceled"
        except Exception as error:
            error_code = _error(error)
        finally:
            if monitor:
                monitor.cancel()
                try:
                    await _joined(monitor)
                except asyncio.CancelledError:
                    pass
            cleanup = asyncio.create_task(asyncio.to_thread(self._finish, capture_id, attempt, error_code))
            try:
                await _joined(cleanup)
                settled = (await _owned_thread(self.store.get, capture_id))["ownership_settled"]
                await self._callback(self.release_resources, capture_id, ownership_settled=settled)
            except Exception:
                # _finish retains durable cost on proof failure. Resource
                # release failures also stay conservative for host recovery.
                pass

    def _finish(self, capture_id, attempt, error_code):
        record = self.store.get_record(capture_id)
        lease = attempt.lease
        try:
            if lease is None:
                lease = self.store.recover_owner(record)
            self.store.reconcile_files(record)
            # Publish settled journal first: a crash before unlink leaves only
            # a recorded stale owner file, never an unknown running writer.
            result = self.store.settle(capture_id, error_code=error_code,
                                       interrupted=error_code == "artifact_capture_interrupted")
            self.store.release_owner(record, lease)
            self.store.record_owner_released(capture_id)
            attempt.lease = None
            return result
        except Exception:
            if lease is not None:
                try:
                    os.close(lease.fd)
                except OSError:
                    pass
                attempt.lease = None
            return self.store.settle(capture_id, settled=False)

    async def cancel(self, capture_id):
        record = await _owned_thread(self.store.request_cancel, capture_id)
        task = self.tasks.get(capture_id)
        if task is not None and not task.done():
            self.attempts[capture_id].stop.set()
            task.cancel()
            await _join_worker(task)
        current = await _owned_thread(self.store.get, capture_id)
        if not current["ownership_settled"]:
            await self.recover()
        return await _owned_thread(self.store.get, capture_id)

    async def cancel_target(self, import_id):
        _uuid(import_id)
        records = await _owned_thread(self.store.recovery_records, only_unsettled=True)
        for record in records:
            if record["target"].get("import_id") == import_id and not record["ownership_settled"]:
                await self.cancel(record["capture_id"])

    async def recover(self):
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recover())
        try:
            return await asyncio.shield(self._recovery_task)
        except asyncio.CancelledError:
            await _joined(self._recovery_task)
            raise

    async def _recover(self):
        async with self._control_lock:
            await self._recover_locked()

    async def _recover_locked(self):
        records = await _owned_thread(self.store.recovery_records, only_unsettled=True)
        for record in records:
            capture_id = record["capture_id"]
            task = self.tasks.get(capture_id)
            if task is not None and not task.done():
                continue
            if record["ownership_settled"]:
                # Recorded settled owners can remain after a crash between
                # journal settlement and owner unlink. No staging is adopted.
                try:
                    lease = await _owned_thread(self.store.recover_owner, record)
                    await _owned_thread(self.store.release_owner, record, lease)
                    await _owned_thread(self.store.record_owner_released, capture_id)
                except Exception:
                    continue
            else:
                attempt = _Attempt()
                await _owned_thread(self._finish, capture_id, attempt, "artifact_capture_interrupted")
                current = await _owned_thread(self.store.get, capture_id)
                if current["ownership_settled"]:
                    await self._callback(self.release_resources, capture_id, ownership_settled=True)
                elif not self.closing:
                    await self._callback(self.reserve_resources, capture_id, _validated(CapturePolicyV1, record["policy"]))
        await _owned_thread(self.store.recover_gc)
        await _owned_thread(self.store.recover_readers)

    async def close(self):
        self.closing = True
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        try:
            return await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await _joined(self._close_task)
            raise

    async def _close(self):
        for task in list(self.admissions):
            await _joined(task)
        for capture_id, task in list(self.tasks.items()):
            if not task.done():
                await _owned_thread(self.store.request_cancel, capture_id)
                self.attempts[capture_id].stop.set()
                task.cancel()
                await _join_worker(task)
        await self.recover()
        records = await _owned_thread(self.store.recovery_records)
        readers = await _owned_thread(self.store.reader_recovery_records)
        if any(not record["ownership_settled"] for record in records) or readers:
            raise ArtifactError("artifact_capture_reconciliation_required")


# Short store name retained for the integration contract proposed to the host.
CaptureStore = ArtifactCaptureStore
