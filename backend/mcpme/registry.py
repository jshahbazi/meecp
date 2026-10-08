"""Durable owner-only slot identities and bounded lifecycle operation metadata."""
from __future__ import annotations

import os
import json
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .private import check_private_file, ensure_private_directory

START_POLICIES = {"manual", "host", "first_call"}
DESIRED_STATES = {"running", "stopped"}
OBSERVED_STATES = {"stopped", "starting", "ready", "stopping", "draining", "failed", "ownership_lost", "updating"}
OPERATION_ACTIONS = {"install", "upgrade", "start", "stop", "restart", "remove", "recover"}
OPERATION_STATES = {"pending", "running", "completed", "failed", "canceled", "recovery_required"}
TERMINAL_OPERATIONS = {"completed", "failed", "canceled"}


class RegistryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Slot:
    slot_id: str
    package_digest: str
    package_path: str
    name: str
    public_port: int
    start_policy: str
    desired_state: str
    observed_state: str
    generation: int
    wake_suppressed: bool
    error_code: str | None
    credential_ref: str | None
    settings_digest: str | None = None
    data_path: str | None = None
    settings_path: str | None = None

    @property
    def id(self) -> str:
        return self.slot_id

    @property
    def uuid(self) -> str:
        return self.slot_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Operation:
    operation_id: str
    action: str
    slot_id: str | None
    generation: int | None
    status: str
    error_code: str | None
    created_at: float
    updated_at: float
    idempotency_key: str | None
    details: dict[str, Any]

    @property
    def id(self) -> str:
        return self.operation_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _uuid(value: str) -> None:
    try:
        if str(uuid.UUID(value)) != value:
            raise RegistryError("invalid_identity")
    except (ValueError, AttributeError, TypeError):
        raise RegistryError("invalid_identity") from None


def _error_code(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)):
        raise RegistryError("invalid_error_code")


def _slot_values(values: dict[str, Any]) -> None:
    if "name" in values and (not isinstance(values["name"], str) or not 1 <= len(values["name"]) <= 256
                             or any(ord(char) < 32 for char in values["name"])):
        raise RegistryError("invalid_slot_name")
    for field, allowed in (("start_policy", START_POLICIES), ("desired_state", DESIRED_STATES),
                           ("observed_state", OBSERVED_STATES)):
        if field in values and (not isinstance(values[field], str) or values[field] not in allowed):
            raise RegistryError("invalid_slot_state")
    if "generation" in values and (type(values["generation"]) is not int or values["generation"] < 1):
        raise RegistryError("invalid_generation")
    if "wake_suppressed" in values and type(values["wake_suppressed"]) is not bool:
        raise RegistryError("invalid_slot_state")
    if "error_code" in values:
        _error_code(values["error_code"])
    for key in ("package_digest", "settings_digest"):
        if key == "settings_digest" and values.get(key) is None:
            continue
        if key in values and (not isinstance(values[key], str) or not re.fullmatch(r"[0-9a-f]{64}", values[key])):
            raise RegistryError("invalid_package_digest" if key == "package_digest" else "invalid_settings_digest")
    for key in ("package_path", "credential_ref", "data_path", "settings_path"):
        if key in values and values[key] is not None:
            value = values[key]
            if (not isinstance(value, str) or not 1 <= len(value) <= 4096 or not Path(value).is_absolute() or "\x00" in value
                    or ".." in Path(value).parts):
                raise RegistryError("invalid_state_reference")


def _operation_details(details: dict[str, Any]) -> None:
    # Explicit metadata allowlist: never persist arbitrary package/client inputs.
    if not isinstance(details, dict) or set(details) - {"plan_id", "package_digest", "package_path", "result_slot_id",
                                                    "import_id", "import_revision", "import_digest", "settings_digest",
                                                    "selected", "exposed", "cancel_requested", "stage"}:
        raise RegistryError("invalid_operation_details")
    for key in ("selected", "exposed", "cancel_requested"):
        if key in details and type(details[key]) is not bool:
            raise RegistryError("invalid_operation_details")
    if details.get("exposed") and not details.get("selected"):
        raise RegistryError("invalid_operation_details")
    if "stage" in details and details["stage"] not in {"staging", "files_ready", "registered", "candidate_verified",
                                                        "exposed", "completed", "recovery_required"}:
        raise RegistryError("invalid_operation_details")
    for key in ("plan_id", "result_slot_id", "import_id"):
        if key in details:
            _uuid(details[key])
    _slot_values({key: value for key, value in details.items() if key in ("package_digest", "package_path", "settings_digest")})
    import_fields = {"import_id", "import_revision", "import_digest"}
    if import_fields & details.keys():
        if not import_fields <= details.keys() or type(details["import_revision"]) is not int or details["import_revision"] < 1:
            raise RegistryError("invalid_operation_details")
        _slot_values({"package_digest": details["import_digest"]})


class Registry:
    def __init__(self, state_root: Path):
        self.state_root = ensure_private_directory(Path(state_root))
        self.path = self.state_root / "registry.sqlite"
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists() or candidate.is_symlink():
                check_private_file(candidate)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        check_private_file(self.path)
        try:
            self._connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._connection.execute("PRAGMA synchronous = FULL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                self._connection.close()
                raise RegistryError("unsupported_registry_schema")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS slots (
                    slot_id TEXT PRIMARY KEY,
                    package_digest TEXT NOT NULL,
                    package_path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    public_port INTEGER NOT NULL UNIQUE CHECK(public_port BETWEEN 1024 AND 65535),
                    start_policy TEXT NOT NULL CHECK(start_policy IN ('manual','host','first_call')),
                    desired_state TEXT NOT NULL CHECK(desired_state IN ('running','stopped')),
                    observed_state TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    wake_suppressed INTEGER NOT NULL CHECK(wake_suppressed IN (0,1)),
                    error_code TEXT,
                    credential_ref TEXT
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    slot_id TEXT,
                    generation INTEGER,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS management_grants (
                    grant_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    mode TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    scope TEXT NOT NULL,
                    operation_id TEXT
                );
            """)
            # Old rows have no user settings. Preserve that boundary explicitly;
            # new installations bind their immutable settings to the generation.
            with self.transaction():
                columns = {row[1] for row in self._connection.execute("PRAGMA table_info(slots)")}
                if "settings_digest" not in columns:
                    self._connection.execute("ALTER TABLE slots ADD COLUMN settings_digest TEXT")
                if "data_path" not in columns:
                    self._connection.execute("ALTER TABLE slots ADD COLUMN data_path TEXT")
                if "settings_path" not in columns:
                    self._connection.execute("ALTER TABLE slots ADD COLUMN settings_path TEXT")
                    for row in self._connection.execute("SELECT slot_id FROM slots WHERE settings_digest IS NOT NULL").fetchall():
                        self._connection.execute("UPDATE slots SET settings_path=? WHERE slot_id=?",
                            (str(self.state_root / "slots" / row[0] / "settings.json"), row[0]))
                # Version 5 requires live session guards for native consent
                # grants. Earlier hosts must reject this state rather than
                # treating those token hashes as standalone file authority.
                self._connection.execute("PRAGMA user_version = 5")
        except sqlite3.Error:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise RegistryError("registry_unavailable") from None

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self):
        """Commit linked authority and operation records together, without yielding."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
            self._connection.execute("COMMIT")
        except BaseException as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise RegistryError("registry_unavailable") from None
            raise

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @staticmethod
    def _slot(row: sqlite3.Row) -> Slot:
        data = dict(row)
        data["wake_suppressed"] = bool(data["wake_suppressed"])
        return Slot(**data)

    def list_slots(self) -> list[Slot]:
        return [self._slot(row) for row in self._connection.execute("SELECT * FROM slots ORDER BY rowid")]

    def get_slot(self, slot_id: str) -> Slot:
        _uuid(slot_id)
        row = self._connection.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
        if row is None:
            raise RegistryError("slot_not_found")
        return self._slot(row)

    def add_slot(self, *, package_digest: str, package_path: Path | str, name: str,
                 public_port: int, start_policy: str = "manual", credential_ref: Path | str | None = None,
                 slot_id: str | None = None, settings_digest: str | None = None,
                 data_path: Path | str | None = None, settings_path: Path | str | None = None) -> Slot:
        slot_id = slot_id or str(uuid.uuid4())
        _uuid(slot_id)
        if type(public_port) is not int or not 1024 <= public_port <= 65535:
            raise RegistryError("invalid_public_port")
        values = dict(package_digest=package_digest, package_path=str(package_path), name=name,
                      start_policy=start_policy, credential_ref=str(credential_ref) if credential_ref else None,
                      settings_digest=settings_digest, data_path=str(data_path) if data_path else None,
                      settings_path=str(settings_path) if settings_path else
                      (str(self.state_root / "slots" / slot_id / "settings.json") if settings_digest else None))
        _slot_values(values)
        slot = Slot(slot_id=slot_id, public_port=public_port, desired_state="stopped", observed_state="stopped",
                    generation=1, wake_suppressed=False, error_code=None, **values)
        try:
            self._connection.execute("INSERT INTO slots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(asdict(slot).values()))
        except sqlite3.IntegrityError:
            raise RegistryError("slot_conflict") from None
        except sqlite3.Error:
            raise RegistryError("registry_unavailable") from None
        return slot

    def update_slot(self, slot_id: str, *, expected_generation: int | None = None, **changes: Any) -> Slot:
        _uuid(slot_id)
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 1):
            raise RegistryError("invalid_generation")
        allowed = {"name", "start_policy", "desired_state", "observed_state", "generation", "wake_suppressed",
                   "error_code", "package_digest", "package_path", "credential_ref", "settings_digest", "data_path", "settings_path"}
        if not changes or set(changes) - allowed:
            raise RegistryError("invalid_slot_update")
        for key in ("package_path", "credential_ref", "data_path", "settings_path"):
            if key in changes and isinstance(changes[key], Path):
                changes[key] = str(changes[key])
        _slot_values(changes)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            current = self.get_slot(slot_id)
            if expected_generation is not None and current.generation != expected_generation:
                raise RegistryError("stale_generation")
            if "generation" in changes and changes["generation"] != current.generation + 1:
                raise RegistryError("invalid_generation")
            if ({"package_digest", "package_path", "settings_digest", "data_path", "settings_path"} & changes.keys()) and changes.get("generation") != current.generation + 1:
                raise RegistryError("generation_required")
            if current.observed_state == "ownership_lost" and changes.get("observed_state", "ownership_lost") != "ownership_lost":
                raise RegistryError("ownership_reconciliation_required")
            assignments = ",".join(f"{key} = ?" for key in changes)
            self._connection.execute(f"UPDATE slots SET {assignments} WHERE slot_id = ?", (*changes.values(), slot_id))
            updated = self.get_slot(slot_id)
            self._connection.execute("COMMIT")
            return updated
        except Exception as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise RegistryError("registry_unavailable") from None
            raise

    def create_operation(self, action: str | None = None, slot_id: str | None = None, *,
                         kind: str | None = None, generation: int | None = None,
                         idempotency_key: str | None = None, details: dict[str, Any] | None = None) -> Operation:
        if action is not None and kind is not None and action != kind:
            raise RegistryError("invalid_operation_action")
        action = action or kind
        if action not in OPERATION_ACTIONS:
            raise RegistryError("invalid_operation_action")
        details = details or {}
        _operation_details(details)
        if idempotency_key is not None:
            _uuid(idempotency_key)
            existing = self.get_operation_by_key(idempotency_key)
            if existing is not None:
                if (existing.action == action and existing.slot_id == slot_id and existing.details == details
                        and (generation is None or generation == existing.generation)):
                    return existing
                raise RegistryError("operation_conflict")
        if slot_id is not None:
            _uuid(slot_id)
        if generation is not None and (type(generation) is not int or generation < 1):
            raise RegistryError("invalid_generation")
        if slot_id is not None:
            try:
                slot = self.get_slot(slot_id)
            except RegistryError:
                if action != "install":
                    raise
                slot = None
            if slot is not None:
                if generation is not None and generation != slot.generation:
                    raise RegistryError("stale_generation")
                generation = slot.generation
            elif generation is not None and (type(generation) is not int or generation != 1):
                raise RegistryError("invalid_generation")
            elif slot is None:
                generation = 1
        elif generation is not None:
            raise RegistryError("invalid_generation")
        timestamp = time.time()
        operation = Operation(str(uuid.uuid4()), action, slot_id, generation, "pending", None, timestamp,
                              timestamp, idempotency_key, details)
        try:
            serialized = asdict(operation)
            serialized["details"] = json.dumps(details, sort_keys=True, separators=(",", ":"))
            self._connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(serialized.values()))
        except sqlite3.IntegrityError:
            if idempotency_key is not None:
                row = self._connection.execute("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if row is not None:
                    existing = self._operation(row)
                    if (existing.action, existing.slot_id, existing.generation, existing.details) == (action, slot_id, generation, details):
                        return existing
            raise RegistryError("operation_conflict") from None
        except sqlite3.Error:
            raise RegistryError("registry_unavailable") from None
        return operation

    def get_operation_by_key(self, key: str) -> Operation | None:
        _uuid(key)
        row = self._connection.execute("SELECT * FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        return self._operation(row) if row is not None else None

    def discard_slot(self, slot_id: str, *, expected_generation: int | None = None) -> None:
        """Remove an inactive registry row; the owner must first confirm its children are reaped."""
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 1):
            raise RegistryError("invalid_generation")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            slot = self.get_slot(slot_id)
            if expected_generation is not None and slot.generation != expected_generation:
                raise RegistryError("stale_generation")
            if slot.observed_state not in {"stopped", "failed"} or slot.desired_state != "stopped":
                raise RegistryError("slot_not_stopped")
            self._connection.execute("DELETE FROM slots WHERE slot_id=?", (slot_id,))
            self._connection.execute("COMMIT")
        except Exception as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise RegistryError("registry_unavailable") from None
            raise

    def complete_removal(self, slot_id: str, operation_id: str, *, expected_generation: int) -> None:
        """Atomically unregister a confirmed inactive slot and finalize its removal."""
        _uuid(slot_id)
        _uuid(operation_id)
        if type(expected_generation) is not int or expected_generation < 1:
            raise RegistryError("invalid_generation")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            slot = self.get_slot(slot_id)
            operation = self.get_operation(operation_id)
            if slot.generation != expected_generation or operation.generation != expected_generation:
                raise RegistryError("stale_generation")
            if operation.action != "remove" or operation.slot_id != slot_id or operation.status not in {"running", "recovery_required"}:
                raise RegistryError("operation_conflict")
            if slot.observed_state != "stopped" or slot.desired_state != "stopped":
                raise RegistryError("slot_not_stopped")
            self._connection.execute("DELETE FROM slots WHERE slot_id=?", (slot_id,))
            self._connection.execute("UPDATE operations SET status='completed',error_code=NULL,updated_at=? WHERE operation_id=?",
                                     (time.time(), operation_id))
            self._connection.execute("COMMIT")
        except Exception as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise RegistryError("registry_unavailable") from None
            raise

    def get_operation(self, operation_id: str) -> Operation:
        _uuid(operation_id)
        row = self._connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None:
            raise RegistryError("operation_not_found")
        return self._operation(row)

    def select_upgrade(self, operation_id: str, candidate: Slot, *, expected_generation: int,
                       selected: bool = True) -> Slot:
        """Commit the generation pointer and upgrade selection in one transaction."""
        _slot_values(candidate.to_dict())
        with self.transaction():
            operation = self.get_operation(operation_id)
            current = self.get_slot(candidate.id)
            if (operation.action != "upgrade" or operation.slot_id != candidate.id
                    or operation.status not in {"running", "recovery_required"}
                    or operation.details.get("exposed")):
                raise RegistryError("operation_conflict")
            if current.generation != expected_generation or candidate.generation != expected_generation + 1:
                raise RegistryError("stale_generation")
            if current.observed_state == "ownership_lost":
                raise RegistryError("ownership_reconciliation_required")
            for key in ("public_port", "credential_ref", "name", "start_policy"):
                if getattr(current, key) != getattr(candidate, key):
                    raise RegistryError("invalid_slot_update")
            changes = candidate.to_dict()
            changes.pop("slot_id")
            assignments = ",".join(f"{key}=?" for key in changes)
            self._connection.execute(f"UPDATE slots SET {assignments} WHERE slot_id=?",
                                     (*changes.values(), candidate.id))
            details = {**operation.details, "selected": selected}
            self._connection.execute("UPDATE operations SET details=?,updated_at=? WHERE operation_id=?",
                                     (json.dumps(details, sort_keys=True), time.time(), operation_id))
            return self.get_slot(candidate.id)

    def reconcile_owned_exit(self, slot_id: str, *, expected_generation: int) -> Slot:
        """Owner-only reconciliation after every ledger identity has exit proof."""
        with self.transaction():
            slot = self.get_slot(slot_id)
            if slot.generation != expected_generation:
                raise RegistryError("stale_generation")
            if slot.observed_state == "ownership_lost":
                self._connection.execute("UPDATE slots SET observed_state='failed',error_code='operation_interrupted' WHERE slot_id=?",
                                         (slot_id,))
            return self.get_slot(slot_id)

    def expose_upgrade(self, operation_id: str, *, expected_generation: int) -> Operation:
        """Persist permission to admit ordinary traffic before opening the gateway."""
        with self.transaction():
            operation = self.get_operation(operation_id)
            slot = self.get_slot(operation.slot_id)
            if (operation.action not in {"install", "upgrade"} or operation.status not in {"running", "recovery_required"}
                    or not operation.details.get("selected") or slot.generation != expected_generation):
                raise RegistryError("operation_conflict")
            if operation.details.get("cancel_requested") and not operation.details.get("exposed"):
                raise RegistryError("upgrade_canceled")
            details = {**operation.details, "exposed": True}
            self._connection.execute("UPDATE operations SET details=?,updated_at=? WHERE operation_id=?",
                                     (json.dumps(details, sort_keys=True), time.time(), operation_id))
            return self.get_operation(operation_id)

    def register_install(self, operation_id: str, *, desired_state: str = "stopped", **values) -> Slot:
        """Register a first candidate and its selected pointer atomically."""
        _slot_values({"desired_state": desired_state})
        with self.transaction():
            operation = self.get_operation(operation_id)
            if (operation.action != "install" or operation.status not in {"running", "recovery_required"}
                    or operation.slot_id != values.get("slot_id") or operation.details.get("selected")):
                raise RegistryError("operation_conflict")
            slot = self.add_slot(**values)
            self._connection.execute("UPDATE slots SET desired_state=? WHERE slot_id=?", (desired_state, slot.id))
            details = {**operation.details, "selected": True, "stage": "registered"}
            self._connection.execute("UPDATE operations SET details=?,updated_at=? WHERE operation_id=?",
                                     (json.dumps(details, sort_keys=True), time.time(), operation_id))
            return self.get_slot(slot.id)

    @staticmethod
    def _operation(row: sqlite3.Row) -> Operation:
        data = dict(row)
        data["details"] = json.loads(data["details"])
        return Operation(**data)

    def list_operations(self, *, limit: int = 100) -> list[Operation]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RegistryError("invalid_operation_limit")
        return [self._operation(row) for row in self._connection.execute(
            "SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (limit,))]

    def list_unfinished_operations(self) -> list[Operation]:
        """Recovery must inspect every unfinished operation, independent of display pagination."""
        return [self._operation(row) for row in self._connection.execute(
            "SELECT * FROM operations WHERE status IN ('pending','running','recovery_required') ORDER BY created_at")]

    def list_import_operations(self) -> list[Operation]:
        """Import recovery must not depend on the recent-operation display limit."""
        result = []
        for row in self._connection.execute("SELECT * FROM operations WHERE action='install' ORDER BY created_at"):
            operation = self._operation(row)
            if "import_id" in operation.details:
                result.append(operation)
        return result

    def update_operation(self, operation_id: str, status: str, *, error_code: str | None = None,
                         details: dict[str, Any] | None = None) -> Operation:
        if status not in OPERATION_STATES:
            raise RegistryError("invalid_operation_status")
        _error_code(error_code)
        if details is not None:
            _operation_details(details)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            current = self.get_operation(operation_id)
            if current.status in TERMINAL_OPERATIONS and (current.status != status or current.error_code != error_code):
                raise RegistryError("operation_finalized")
            if current.status in TERMINAL_OPERATIONS and details is not None and details != current.details:
                raise RegistryError("operation_finalized")
            if current.status in TERMINAL_OPERATIONS:
                self._connection.execute("COMMIT")
                return current
            if current.status == "running" and status == "pending":
                raise RegistryError("invalid_operation_transition")
            updated_details = details if details is not None else current.details
            if current.details.get("exposed") and not updated_details.get("exposed"):
                raise RegistryError("upgrade_already_exposed")
            if current.details.get("cancel_requested") and not updated_details.get("cancel_requested"):
                raise RegistryError("invalid_operation_details")
            self._connection.execute("UPDATE operations SET status=?,error_code=?,updated_at=?,details=? WHERE operation_id=?",
                                     (status, error_code, time.time(), json.dumps(updated_details, sort_keys=True), operation_id))
            result = self.get_operation(operation_id)
            self._connection.execute("COMMIT")
            return result
        except Exception as error:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.Error):
                raise RegistryError("registry_unavailable") from None
            raise
