"""Create fresh installation drafts from terminal operations."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from .contracts import PackageError, validate_package
from .imports import (
    MAX_DRAFTS,
    RETENTION_SECONDS,
    ImportError,
    _attachment,
    _choices,
    _refresh,
    snapshot_package,
)

_RETRY_LOCK = threading.RLock()


def _ensure_table(store) -> None:
    store._connection.execute("""CREATE TABLE IF NOT EXISTS installation_retries (
        original_id TEXT NOT NULL, original_revision INTEGER NOT NULL,
        operation_id TEXT NOT NULL, digest TEXT NOT NULL, retry_id TEXT NOT NULL,
        PRIMARY KEY (original_id, original_revision, operation_id, digest),
        UNIQUE (retry_id)
    )""")


def retry_installation(store, draft_id: str, revision: int) -> dict:
    """Return a fresh prepared-package draft for a terminal install attempt."""
    if type(revision) is not int:
        raise ImportError("stale_import_revision")
    with _RETRY_LOCK:
        with store._lock:
            original = store._get(draft_id, revision)
            status = original.get("operation_status")
            operation_id = original.get("operation_id")
            attachment = original.get("attachment")
            if status == "recovery_required":
                raise ImportError("installation_recovery_required")
            if status not in {"failed", "canceled"} or not isinstance(operation_id, str):
                raise ImportError("installation_retry_unavailable")
            if not isinstance(attachment, dict) or not isinstance(attachment.get("path"), str):
                raise ImportError("prepared_package_required")
            expected_digest = attachment.get("digest")
            expected_source = attachment.get("source_revision")
            package_path = Path(attachment["path"])

        try:
            sealed = validate_package(package_path)
        except PackageError as error:
            raise ImportError(error.code) from None
        if sealed.digest != expected_digest or sealed.manifest.source_revision != expected_source:
            raise ImportError("package_digest_mismatch")

        # Resolve an existing durable mapping before copying the package.  The
        # retained candidate is checked after the transaction so SQLite never
        # becomes coupled to filesystem validation.
        with store._transaction():
            _ensure_table(store)
            row = store._connection.execute(
                "SELECT retry_id FROM installation_retries WHERE original_id=? AND original_revision=? AND operation_id=? AND digest=?",
                (draft_id, revision, operation_id, sealed.digest)).fetchone()
        if row is not None:
            with store._lock:
                existing = store._get(row[0])
            existing_revision = existing.get("revision")
            observed_attachment = existing.get("attachment")
            if not isinstance(observed_attachment, dict) or not isinstance(observed_attachment.get("path"), str):
                raise ImportError("stale_installation_retry")
            try:
                retained = validate_package(existing["attachment"]["path"])
            except (KeyError, PackageError):
                raise ImportError("package_digest_mismatch") from None
            if retained.digest != sealed.digest:
                raise ImportError("package_digest_mismatch") from None
            with store._transaction():
                current = store._get(draft_id, revision)
                if (current.get("operation_id") != operation_id
                        or current.get("operation_status") != status
                        or current.get("attachment", {}).get("digest") != sealed.digest):
                    raise ImportError("stale_installation_retry") from None
                existing = store._get(row[0], existing_revision)
                if (existing.get("draft_id") != row[0] or existing.get("revision") != existing_revision
                        or existing.get("attachment") != observed_attachment):
                    raise ImportError("stale_installation_retry")
                if any(existing.get(field) != current.get(field) for field in
                       ("name", "intent", "start", "start_policy", "requirements")):
                    raise ImportError("stale_installation_retry")
            return existing

        new_id = str(uuid.uuid4())
        target_root = store.staging / new_id / "package"
        try:
            sealed_copy = snapshot_package(sealed, target_root, expected_digest=sealed.digest)
        except PackageError as error:
            _remove_private_tree(target_root)
            raise ImportError(error.code) from None
        except OSError:
            _remove_private_tree(target_root)
            raise ImportError("package_snapshot_failed") from None

        try:
            with store._transaction():
                _ensure_table(store)
                row = store._connection.execute(
                    "SELECT retry_id FROM installation_retries WHERE original_id=? AND original_revision=? AND operation_id=? AND digest=?",
                    (draft_id, revision, operation_id, sealed.digest)).fetchone()
                if row is not None:
                    raise _ExistingRetry(store._get(row[0]))
                current = store._get(draft_id, revision)
                if (current.get("operation_id") != operation_id or current.get("operation_status") != status
                        or current.get("attachment", {}).get("digest") != sealed.digest):
                    raise ImportError("stale_installation_retry")
                if store._connection.execute("SELECT count(*) FROM drafts").fetchone()[0] >= MAX_DRAFTS:
                    raise ImportError("import_limit_exceeded")
                now = time.time()
                retry = {
                    "schema_version": current["schema_version"], "draft_id": new_id, "revision": 1,
                    "name": current["name"], "intent": current["intent"], "document_role": current["document_role"],
                    "start": current["start"], "start_policy": current["start_policy"],
                    "requirements": json.loads(json.dumps(current["requirements"])),
                    "source": {"kind": "prepared_package", "locator": str(package_path),
                               "snapshot_path": str(sealed_copy.root), "digest": sealed.digest,
                               "revision": sealed.manifest.source_revision, "files": [],
                               "file_inventory": sealed.lock_name,
                               "artifact_count": len(sealed.lock.artifacts),
                               "bytes": sum(item.size for item in sealed.lock.artifacts)},
                    "attachment": _attachment(sealed_copy), "state": "package_received",
                    "operation_id": None, "operation_status": None, "result_slot_id": None,
                    "created_at": now, "updated_at": now, "expires_at": now + RETENTION_SECONDS,
                }
                _choices(retry)
                _refresh(retry)
                store._save(retry)
                store._connection.execute(
                    "INSERT INTO installation_retries VALUES (?,?,?,?,?)",
                    (draft_id, revision, operation_id, sealed.digest, new_id))
                return retry
        except _ExistingRetry as existing:
            _remove_private_tree(target_root)
            with store._lock:
                observed = store._get(existing.draft["draft_id"])
            if not isinstance(observed.get("attachment"), dict) or not isinstance(observed["attachment"].get("path"), str):
                raise ImportError("stale_installation_retry")
            try:
                retained = validate_package(observed["attachment"]["path"])
            except (KeyError, PackageError):
                raise ImportError("package_digest_mismatch") from None
            if retained.digest != sealed.digest:
                raise ImportError("package_digest_mismatch") from None
            with store._transaction():
                current = store._get(draft_id, revision)
                candidate = store._get(observed["draft_id"], observed["revision"])
                if (current.get("operation_id") != operation_id
                        or current.get("operation_status") != status
                        or current.get("attachment", {}).get("digest") != sealed.digest
                        or candidate.get("revision") != observed["revision"]
                        or candidate.get("attachment") != observed.get("attachment")
                        or any(candidate.get(field) != current.get(field) for field in
                               ("name", "intent", "start", "start_policy", "requirements"))):
                    raise ImportError("stale_installation_retry") from None
            return candidate
        except BaseException:
            _remove_private_tree(target_root)
            raise


class _ExistingRetry(Exception):
    def __init__(self, draft):
        self.draft = draft


def _remove_private_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path.parent if path.name == "package" else path, ignore_errors=True)
