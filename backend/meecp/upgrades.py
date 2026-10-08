"""Private data clones and durable, content-free upgrade recovery metadata.

The supervisor owns serialization and quiesces every old-generation writer
before calling ``snapshot_data``. This module neither launches package code nor
decides which generation may serve calls. In particular, an exposure marker can
never be cleared: it means candidate writes may have happened.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
import time
from typing import Any
import unicodedata
import uuid

from .private import (PrivateStateError, check_private_file, ensure_private_directory,
                      read_private_json, write_private_json)

MAX_DATA_FILES = 20_000
MAX_DATA_DIRECTORIES = 20_000
MAX_DATA_BYTES = 64 * 1024 * 1024 * 1024
MAX_TREE_METADATA_BYTES = 16 * 1024 * 1024
MAX_UPGRADES = 4096
MAX_RECORD_BYTES = 64 * 1024
DISK_HEADROOM_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
_IDENTITY = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RECORD = re.compile(_IDENTITY + r"\.json")
_TEMPORARY = re.compile(_IDENTITY + r"\.json\.[0-9a-f]{24}\.tmp")
_HASH = re.compile(r"[0-9a-f]{64}")
_ERROR = re.compile(r"[a-z][a-z0-9_]{0,63}")
_STAGES = {"staging", "quiesced", "snapshot_ready", "candidate_verified", "selected", "exposed",
           "completed", "rolled_back", "canceled", "recovery_required"}
_TERMINAL = {"completed", "rolled_back", "canceled"}
_NEXT = {
    "staging": {"quiesced", "rolled_back", "canceled", "recovery_required"},
    "quiesced": {"snapshot_ready", "rolled_back", "canceled", "recovery_required"},
    "snapshot_ready": {"candidate_verified", "rolled_back", "canceled", "recovery_required"},
    "candidate_verified": {"selected", "rolled_back", "canceled", "recovery_required"},
    "selected": {"exposed", "rolled_back", "canceled", "recovery_required"},
    "exposed": {"completed", "recovery_required"},
    "recovery_required": {"completed", "rolled_back", "canceled", "exposed"},
    "completed": set(), "rolled_back": set(), "canceled": set(),
}
_SLOT_FIELDS = {"slot_id", "package_digest", "package_path", "name", "public_port", "start_policy",
                "desired_state", "observed_state", "generation", "wake_suppressed", "error_code",
                "credential_ref", "settings_digest", "data_path", "settings_path"}
_RECORD_FIELDS = {"schema_version", "operation_id", "plan_id", "slot_id", "status", "old", "candidate",
                  "old_activation", "selected", "exposed", "cancel_requested", "snapshot", "error_code",
                  "created_at", "updated_at"}
_STAT_FIELDS = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")


class UpgradeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identity(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise UpgradeError("invalid_upgrade_identity") from None
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> os.stat_result:
    for ancestor in reversed(path.parents):
        if ancestor.is_symlink():
            raise UpgradeError("upgrade_data_unsafe")
    details = path.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise UpgradeError("upgrade_data_unsafe")
    return details


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, key) == getattr(right, key) for key in _STAT_FIELDS)


def _canceled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpgradeError("upgrade_canceled")


def _name(name: str, relative: str) -> None:
    try:
        encoded = name.encode("utf-8")
        path_bytes = relative.encode("utf-8")
    except UnicodeError:
        raise UpgradeError("upgrade_data_unsafe") from None
    if (not name or name in {".", ".."} or "\\" in name or ":" in name or len(encoded) > 255
            or len(path_bytes) > 4096 or len(Path(relative).parts) > 64
            or any(unicodedata.category(char).startswith("C") for char in name)):
        raise UpgradeError("upgrade_data_unsafe")


def _tree(root: Path, cancel: threading.Event | None) -> dict[str, os.stat_result]:
    """Scan by owned directory descriptors, retaining bounded identity metadata."""
    baseline = _private_directory(root)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    rows: dict[str, os.stat_result] = {"": baseline}
    canonical: set[str] = set()
    counts = {"files": 0, "directories": 0, "bytes": 0, "metadata": 0}

    def visit(directory_fd: int, prefix: str) -> None:
        _canceled(cancel)
        if not _same(rows[prefix], os.fstat(directory_fd)):
            raise UpgradeError("upgrade_data_changed")
        # Limit listing allocation as well as recursive metadata accumulation.
        names = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if len(names) >= MAX_DATA_FILES + MAX_DATA_DIRECTORIES:
                    raise UpgradeError("upgrade_data_limit")
                names.append(entry.name)
        for name in sorted(names):
            _canceled(cancel)
            relative = prefix + "/" + name if prefix else name
            _name(name, relative)
            normalized = unicodedata.normalize("NFC", relative).casefold()
            if normalized in canonical:
                raise UpgradeError("upgrade_data_unsafe")
            canonical.add(normalized)
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = stat.S_IMODE(details.st_mode)
            if details.st_uid != os.getuid():
                raise UpgradeError("upgrade_data_unsafe")
            if stat.S_ISDIR(details.st_mode) and mode == 0o700:
                counts["directories"] += 1
                if counts["directories"] > MAX_DATA_DIRECTORIES:
                    raise UpgradeError("upgrade_data_limit")
            elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1 and mode in {0o600, 0o700}:
                counts["files"] += 1
                counts["bytes"] += details.st_size
                if counts["files"] > MAX_DATA_FILES or counts["bytes"] > MAX_DATA_BYTES:
                    raise UpgradeError("upgrade_data_limit")
            else:
                raise UpgradeError("upgrade_data_unsafe")
            counts["metadata"] += len(relative.encode("utf-8")) + 256
            if counts["metadata"] > MAX_TREE_METADATA_BYTES:
                raise UpgradeError("upgrade_data_limit")
            rows[relative] = details
            if stat.S_ISDIR(details.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
        if not _same(rows[prefix], os.fstat(directory_fd)):
            raise UpgradeError("upgrade_data_changed")

    try:
        visit(descriptor, "")
        if not _same(baseline, root.lstat()):
            raise UpgradeError("upgrade_data_changed")
        return rows
    finally:
        os.close(descriptor)


def _publish(staged: Path, destination: Path) -> None:
    """Publish a directory atomically without replacing even an empty directory."""
    library = ctypes.CDLL(None, use_errno=True)
    if hasattr(library, "renamex_np"):
        function = library.renamex_np
        function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        result = function(os.fsencode(staged), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    elif hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(staged), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise UpgradeError("upgrade_snapshot_unavailable")
    if result != 0:
        if ctypes.get_errno() in {errno.EEXIST, errno.ENOTEMPTY}:
            raise UpgradeError("upgrade_snapshot_exists")
        raise UpgradeError("upgrade_snapshot_unavailable")


def snapshot_data(source: Path, destination: Path, cancel: threading.Event | None = None) -> dict[str, Any]:
    """Clone quiesced private data into an inactive, durable, nonoverwriting tree.

    Returns only aggregate identity metadata. No names or content are journaled.
    A failed copy removes only its uniquely created, identity-checked staging
    directory. The caller retains ownership until this synchronous call settles.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    staged: Path | None = None
    staged_identity: tuple[int, int] | None = None
    try:
        if ".." in source.parts or ".." in destination.parts:
            raise UpgradeError("upgrade_data_unsafe")
        if source == destination or source in destination.parents or destination in source.parents:
            raise UpgradeError("upgrade_data_unsafe")
        _canceled(cancel)
        rows = _tree(source, cancel)
        ensure_private_directory(destination.parent)
        if destination.exists() or destination.is_symlink():
            raise UpgradeError("upgrade_snapshot_exists")
        # Reject an aliased source root anywhere on the destination's ancestry.
        for ancestor in (destination.parent, *destination.parent.parents):
            identity = ancestor.lstat()
            if (identity.st_dev, identity.st_ino) == (rows[""].st_dev, rows[""].st_ino):
                raise UpgradeError("upgrade_data_unsafe")
        size = sum(details.st_size for details in rows.values() if stat.S_ISREG(details.st_mode))
        if shutil.disk_usage(destination.parent).free < size + DISK_HEADROOM_BYTES:
            raise UpgradeError("upgrade_disk_unavailable")
        staged = destination.with_name(destination.name + "." + secrets.token_hex(12) + ".tmp")
        staged.mkdir(mode=0o700)
        created = staged.lstat()
        staged_identity = (created.st_dev, created.st_ino)
        aggregate = hashlib.sha256()
        files = copied_bytes = 0
        children_by_parent: dict[str, list[str]] = {}
        for relative in sorted(rows):
            if relative:
                parent = str(Path(relative).parent)
                children_by_parent.setdefault("" if parent == "." else parent, []).append(relative)
        root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        def clone(source_fd: int, prefix: str) -> None:
            nonlocal files, copied_bytes
            _canceled(cancel)
            if not _same(rows[prefix], os.fstat(source_fd)):
                raise UpgradeError("upgrade_data_changed")
            for relative in children_by_parent.get(prefix, []):
                _canceled(cancel)
                name = Path(relative).name
                details = rows[relative]
                target = staged / relative
                if not _same(details, os.stat(name, dir_fd=source_fd, follow_symlinks=False)):
                    raise UpgradeError("upgrade_data_changed")
                if stat.S_ISDIR(details.st_mode):
                    target.mkdir(mode=0o700)
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source_fd)
                    try:
                        clone(child_fd, relative)
                    finally:
                        os.close(child_fd)
                    identity = {"path": relative, "kind": "directory"}
                else:
                    digest = hashlib.sha256()
                    count = 0
                    input_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
                    with os.fdopen(input_fd, "rb") as incoming:
                        if not _same(details, os.fstat(incoming.fileno())):
                            raise UpgradeError("upgrade_data_changed")
                        output_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(output_fd, "wb") as outgoing:
                            while chunk := incoming.read(min(CHUNK_BYTES, details.st_size - count + 1)):
                                _canceled(cancel)
                                count += len(chunk)
                                if count > details.st_size:
                                    raise UpgradeError("upgrade_data_changed")
                                digest.update(chunk)
                                outgoing.write(chunk)
                            if (count != details.st_size or not _same(details, os.fstat(incoming.fileno()))
                                    or not _same(details, os.stat(name, dir_fd=source_fd, follow_symlinks=False))):
                                raise UpgradeError("upgrade_data_changed")
                            outgoing.flush()
                            os.fchmod(outgoing.fileno(), stat.S_IMODE(details.st_mode))
                            os.fsync(outgoing.fileno())
                    files += 1
                    copied_bytes += count
                    identity = {"path": relative, "kind": "file", "size": count,
                                "mode": stat.S_IMODE(details.st_mode), "sha256": digest.hexdigest()}
                aggregate.update(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=False).encode("utf-8") + b"\n")
            if not _same(rows[prefix], os.fstat(source_fd)):
                raise UpgradeError("upgrade_data_changed")
            _sync_directory(staged / prefix)

        try:
            clone(root_fd, "")
        finally:
            os.close(root_fd)
        _canceled(cancel)
        after = _tree(source, cancel)
        if set(after) != set(rows) or any(not _same(rows[key], after[key]) for key in rows):
            raise UpgradeError("upgrade_data_changed")
        _canceled(cancel)
        _publish(staged, destination)
        staged = None
        _sync_directory(destination.parent)
        return {"bytes": copied_bytes, "files": files, "digest": aggregate.hexdigest()}
    except UpgradeError:
        raise
    except PrivateStateError:
        raise UpgradeError("upgrade_data_unsafe") from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise UpgradeError("upgrade_snapshot_unavailable") from None
    finally:
        if staged is not None and staged_identity is not None:
            try:
                current = staged.lstat()
                if (current.st_dev, current.st_ino) == staged_identity and stat.S_ISDIR(current.st_mode):
                    shutil.rmtree(staged)
                    _sync_directory(staged.parent)
            except FileNotFoundError:
                pass
            except OSError:
                raise UpgradeError("upgrade_snapshot_unavailable") from None


def _path(value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096 or "\x00" in value
            or not Path(value).is_absolute() or ".." in Path(value).parts):
        raise UpgradeError("upgrades_unavailable")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise UpgradeError("upgrades_unavailable") from None


def _slot(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _SLOT_FIELDS:
        raise UpgradeError("upgrades_unavailable")
    _identity(value["slot_id"])
    for field in ("package_digest", "settings_digest"):
        digest = value[field]
        if field == "settings_digest" and digest is None:
            continue
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise UpgradeError("upgrades_unavailable")
    _path(value["package_path"])
    for field in ("credential_ref", "data_path", "settings_path"):
        _path(value[field], nullable=True)
    if (not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 256
            or any(ord(char) < 32 for char in value["name"])
            or type(value["public_port"]) is not int or not 1024 <= value["public_port"] <= 65535
            or value["start_policy"] not in {"manual", "host", "first_call"}
            or value["desired_state"] not in {"running", "stopped"}
            or value["observed_state"] not in {"stopped", "starting", "ready", "stopping", "draining", "failed", "ownership_lost", "updating"}
            or type(value["generation"]) is not int or value["generation"] < 1
            or type(value["wake_suppressed"]) is not bool
            or value["error_code"] is not None and (not isinstance(value["error_code"], str) or not _ERROR.fullmatch(value["error_code"]))):
        raise UpgradeError("upgrades_unavailable")
    if (value["settings_path"] is None) != (value["settings_digest"] is None):
        raise UpgradeError("upgrades_unavailable")


def _snapshot(value: Any) -> None:
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {"bytes", "files", "digest"}
            or type(value["bytes"]) is not int or not 0 <= value["bytes"] <= MAX_DATA_BYTES
            or type(value["files"]) is not int or not 0 <= value["files"] <= MAX_DATA_FILES
            or not isinstance(value["digest"], str) or not _HASH.fullmatch(value["digest"])):
        raise UpgradeError("upgrades_unavailable")


def _validate(record: Any) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS or type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise UpgradeError("upgrades_unavailable")
    for field in ("operation_id", "plan_id", "slot_id"):
        _identity(record[field])
    _slot(record["old"])
    _slot(record["candidate"])
    old, candidate = record["old"], record["candidate"]
    if (old["slot_id"] != record["slot_id"] or candidate["slot_id"] != record["slot_id"]
            or candidate["generation"] != old["generation"] + 1
            or any(old[key] != candidate[key] for key in ("public_port", "credential_ref", "name", "start_policy"))):
        raise UpgradeError("upgrades_unavailable")
    activation = record["old_activation"]
    if (not isinstance(activation, dict) or set(activation) != {"generation", "verified", "exposure_permitted", "legacy_jev"}
            or type(activation["generation"]) is not int or activation["generation"] != old["generation"]
            or any(type(activation[field]) is not bool for field in ("verified", "exposure_permitted", "legacy_jev"))):
        raise UpgradeError("upgrades_unavailable")
    if (not isinstance(record["status"], str) or record["status"] not in _STAGES
            or any(type(record[field]) is not bool for field in ("selected", "exposed", "cancel_requested"))):
        raise UpgradeError("upgrades_unavailable")
    if (record["exposed"] and not record["selected"]
            or record["status"] in {"selected", "exposed", "completed"} and not record["selected"]
            or record["status"] in {"exposed", "completed"} and not record["exposed"]
            or record["status"] in {"staging", "quiesced", "snapshot_ready", "candidate_verified", "rolled_back", "canceled"} and record["exposed"]
            or record["status"] in {"staging", "quiesced", "snapshot_ready", "candidate_verified", "rolled_back", "canceled"} and record["selected"]):
        raise UpgradeError("upgrades_unavailable")
    _snapshot(record["snapshot"])
    if record["status"] in {"snapshot_ready", "candidate_verified", "selected", "exposed", "completed"} and record["snapshot"] is None:
        raise UpgradeError("upgrades_unavailable")
    if record["error_code"] is not None and (not isinstance(record["error_code"], str) or not _ERROR.fullmatch(record["error_code"])):
        raise UpgradeError("upgrades_unavailable")
    if (any(type(record[field]) not in {int, float} or not math.isfinite(record[field]) or record[field] < 0
            for field in ("created_at", "updated_at")) or record["updated_at"] < record["created_at"]):
        raise UpgradeError("upgrades_unavailable")
    try:
        encoded = json.dumps(record, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise UpgradeError("upgrades_unavailable") from None
    if len(encoded) > MAX_RECORD_BYTES:
        raise UpgradeError("upgrades_unavailable")


def recovery_action(record: dict[str, Any]) -> str:
    """Any durable exposure means recover candidate, never restore old data."""
    try:
        _validate(record)
    except (UpgradeError, ValueError, TypeError, RecursionError):
        raise UpgradeError("upgrades_unavailable") from None
    return "recover_candidate" if record["exposed"] else "restore_old"


class UpgradeStore:
    """Strict upgrade records under the supervisor's existing ownership lock."""

    def __init__(self, root: Path):
        try:
            self.state_root = ensure_private_directory(Path(root))
            self.root = ensure_private_directory(self.state_root / "upgrades")
            self.records = ensure_private_directory(self.root / "records")
            cleaned = False
            for path in self.records.iterdir():
                if _TEMPORARY.fullmatch(path.name):
                    check_private_file(path)
                    path.unlink()
                    cleaned = True
            if cleaned:
                _sync_directory(self.records)
            for path in self._paths():
                self._record(path.stem)
        except (PrivateStateError, UpgradeError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise UpgradeError("upgrades_unavailable") from None

    def _directories(self) -> None:
        for path in (self.state_root, self.root, self.records):
            _private_directory(path)

    def _paths(self) -> list[Path]:
        self._directories()
        paths = []
        for path in self.records.iterdir():
            if not _RECORD.fullmatch(path.name):
                raise UpgradeError("upgrades_unavailable")
            paths.append(path)
            if len(paths) > MAX_UPGRADES:
                raise UpgradeError("upgrade_limit")
        return sorted(paths)

    def _record(self, operation_id: str) -> dict[str, Any]:
        self._directories()
        path = self.records / (operation_id + ".json")
        if not path.exists() and not path.is_symlink():
            raise UpgradeError("upgrade_not_found")
        record = read_private_json(path, max_bytes=MAX_RECORD_BYTES)
        _validate(record)
        if record["operation_id"] != operation_id:
            raise UpgradeError("upgrades_unavailable")
        return record

    def get(self, operation_id: str) -> dict[str, Any]:
        operation_id = _identity(operation_id)
        try:
            return self._record(operation_id)
        except UpgradeError as error:
            if error.code == "upgrade_not_found":
                raise
            raise UpgradeError("upgrades_unavailable") from None
        except (PrivateStateError, OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise UpgradeError("upgrades_unavailable") from None

    def list_unfinished(self) -> list[dict[str, Any]]:
        try:
            return [record for path in self._paths() if (record := self._record(path.stem))["status"] not in _TERMINAL]
        except (PrivateStateError, OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            if isinstance(error, UpgradeError) and error.code == "upgrade_limit":
                raise
            raise UpgradeError("upgrades_unavailable") from None

    def create(self, operation_id: str, plan_id: str, old_slot: dict[str, Any],
               candidate_slot: dict[str, Any], old_activation: dict[str, Any]) -> dict[str, Any]:
        operation_id, plan_id = _identity(operation_id), _identity(plan_id)
        try:
            existing = self.get(operation_id)
        except UpgradeError as error:
            if error.code != "upgrade_not_found":
                raise
        else:
            if (existing["plan_id"] != plan_id or existing["old"] != old_slot
                    or existing["candidate"] != candidate_slot or existing["old_activation"] != old_activation):
                raise UpgradeError("upgrade_identity_conflict")
            return existing
        created = time.time()
        record = {"schema_version": 1, "operation_id": operation_id, "plan_id": plan_id,
                  "slot_id": old_slot.get("slot_id") if isinstance(old_slot, dict) else None,
                  "status": "staging", "old": copy.deepcopy(old_slot), "candidate": copy.deepcopy(candidate_slot),
                  "old_activation": copy.deepcopy(old_activation), "selected": False, "exposed": False,
                  "cancel_requested": False, "snapshot": None, "error_code": None,
                  "created_at": created, "updated_at": created}
        try:
            _validate(record)
            if len(self._paths()) >= MAX_UPGRADES:
                raise UpgradeError("upgrade_limit")
            write_private_json(self.records / (operation_id + ".json"), record)
            return copy.deepcopy(record)
        except (PrivateStateError, OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            if isinstance(error, UpgradeError):
                raise
            raise UpgradeError("upgrades_unavailable") from None

    def update(self, operation_id: str, stage: str, **fields: Any) -> dict[str, Any]:
        if not isinstance(stage, str) or stage not in _STAGES or set(fields) - {"selected", "exposed", "cancel_requested", "snapshot", "error_code"}:
            raise UpgradeError("invalid_upgrade_update")
        record = self.get(operation_id)
        if stage != record["status"] and stage not in _NEXT[record["status"]]:
            raise UpgradeError("invalid_upgrade_transition")
        # Recovery follows the durable boundary rather than continuing an
        # interrupted pre-exposure candidate toward first exposure.
        if (record["status"] == "recovery_required" and stage in {"exposed", "completed"}
                and not record["exposed"]):
            raise UpgradeError("invalid_upgrade_transition")
        if (record["exposed"] and (fields.get("exposed") is False or fields.get("selected") is False
                                   or stage in {"rolled_back", "canceled"})
                or record["cancel_requested"] and fields.get("cancel_requested") is False):
            raise UpgradeError("upgrade_already_exposed" if record["exposed"] else "invalid_upgrade_update")
        if record["snapshot"] is not None and "snapshot" in fields and fields["snapshot"] != record["snapshot"]:
            raise UpgradeError("invalid_upgrade_update")
        updated = copy.deepcopy(record)
        updated.update(copy.deepcopy(fields))
        updated["status"] = stage
        if stage in {"selected", "exposed", "completed"}:
            updated["selected"] = True
        if stage in {"exposed", "completed"}:
            updated["exposed"] = True
        if stage in {"rolled_back", "canceled"}:
            updated["selected"] = False
        if updated["exposed"] and updated["cancel_requested"] and not record["exposed"]:
            raise UpgradeError("upgrade_canceled")
        updated["updated_at"] = max(record["updated_at"], time.time())
        try:
            _validate(updated)
            write_private_json(self.records / (operation_id + ".json"), updated)
            return copy.deepcopy(updated)
        except (PrivateStateError, OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
            if isinstance(error, UpgradeError):
                raise
            raise UpgradeError("upgrades_unavailable") from None

    def request_cancel(self, operation_id: str) -> dict[str, Any]:
        record = self.get(operation_id)
        if record["exposed"]:
            raise UpgradeError("upgrade_already_exposed")
        if record["status"] in {"rolled_back", "canceled"} or record["cancel_requested"]:
            return record
        return self.update(operation_id, record["status"], cancel_requested=True)
