"""Closed nonsecret settings and immutable owner-managed secret references.

Only the private secret records contain values. Inspection, plans, and slot
settings contain opaque references and metadata, never secret-derived hashes.
The supervisor holds its state ownership lock for this store's lifetime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import unicodedata
import uuid
from typing import Any

from .contracts import MAX_CONFIGURATION_INTEGER, PackageManifest
from .private import (PrivateStateError, check_private_file, ensure_private_directory,
                      write_private_json)

MAX_CONFIGURATION_BYTES = 16 * 1024
MAX_SECRET_BYTES = 4096
MAX_SECRET_RECORD_BYTES = 32 * 1024
MAX_SECRETS = 4096
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_IDENTITY = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RECORD = re.compile(_IDENTITY + r"\.json")
_TEMPORARY = re.compile(_IDENTITY + r"\.json\.[0-9a-f]{24}\.tmp")


class SettingsError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsError("invalid_secret_reference")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise SettingsError("invalid_secret_reference") from None
    return value


def _primitive(value: Any) -> bool:
    if type(value) is bool:
        return True
    if type(value) is int:
        return -MAX_CONFIGURATION_INTEGER <= value <= MAX_CONFIGURATION_INTEGER
    if type(value) is str and len(value) <= 4096 and "\x00" not in value:
        try:
            value.encode("utf-8")
            return True
        except UnicodeError:
            pass
    return False


def _configuration_size(configuration: dict[str, Any]) -> None:
    try:
        data = json.dumps(configuration, sort_keys=True, separators=(",", ":"),
                          allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SettingsError("invalid_settings") from None
    if len(data) > MAX_CONFIGURATION_BYTES:
        raise SettingsError("invalid_settings")


def validate_settings_choices(payload: Any = None) -> dict[str, Any]:
    """Validate settings before a package is known, without resolving references."""
    if payload is None:
        payload = {"configuration": {}, "secret_references": {}}
    if (not isinstance(payload, dict) or set(payload) - {"schema_version", "configuration", "secret_references"}
            or not {"configuration", "secret_references"} <= set(payload)
            or type(payload.get("schema_version", 1)) is not int or payload.get("schema_version", 1) != 1):
        raise SettingsError("invalid_settings")
    configuration, references = payload["configuration"], payload["secret_references"]
    if (not isinstance(configuration, dict) or len(configuration) > 32
            or any(not isinstance(name, str) or not _NAME.fullmatch(name) or not _primitive(value)
                   for name, value in configuration.items())
            or not isinstance(references, dict) or len(references) > 16
            or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in references)):
        raise SettingsError("invalid_settings")
    _configuration_size(configuration)
    try:
        secret_references = {name: _uuid(value) for name, value in references.items()}
    except SettingsError:
        raise SettingsError("invalid_settings") from None
    return {"schema_version": 1, "configuration": dict(configuration), "secret_references": secret_references}


def normalize_settings(manifest: PackageManifest, payload: Any = None) -> dict[str, Any]:
    """Bind selected values and opaque references to a prepared package contract."""
    settings = validate_settings_choices(payload)
    selected = settings["configuration"]
    fields = manifest.configuration.fields
    if set(selected) - set(fields):
        raise SettingsError("invalid_settings")
    effective: dict[str, Any] = {}
    for name, declaration in fields.items():
        if name in selected:
            value = selected[name]
        elif "default" in declaration.model_fields_set:
            value = declaration.default
        elif declaration.required:
            raise SettingsError("configuration_required")
        else:
            continue
        if not declaration.accepts(value):
            raise SettingsError("invalid_settings")
        effective[name] = value
    if set(settings["secret_references"]) != set(manifest.access.secrets):
        raise SettingsError("secret_reference_unavailable")
    _configuration_size(effective)
    return {"schema_version": 1, "configuration": effective,
            "secret_references": settings["secret_references"]}


def settings_digest(settings: dict[str, Any]) -> str:
    """Digest only nonsecret configuration and opaque reference identities."""
    normalized = validate_settings_choices(settings)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _secret_name(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return not any(unicodedata.category(char).startswith("C") for char in value)


def _secret_value(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_SECRET_BYTES
    except UnicodeError:
        return False


def _private_directory(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError:
        raise PrivateStateError() from None
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise PrivateStateError()


def _read_record(path: Path) -> dict[str, Any]:
    """Check the opened leaf and its identity again after a bounded read."""
    check_private_file(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > MAX_SECRET_RECORD_BYTES):
            raise PrivateStateError()
        data = source.read(MAX_SECRET_RECORD_BYTES + 1)
        after, current = os.fstat(source.fileno()), path.lstat()
        attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_nlink")
        if (len(data) != before.st_size or len(data) > MAX_SECRET_RECORD_BYTES
                or any(getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(current, key)
                       for key in attrs)):
            raise PrivateStateError()

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PrivateStateError()
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(PrivateStateError()))
    if not isinstance(value, dict):
        raise PrivateStateError()
    return value


class SecretStore:
    """Private immutable values, durable revocation tombstones, metadata-only inspection."""

    def __init__(self, root: Path):
        try:
            self.state_root = ensure_private_directory(Path(root))
            self.root = ensure_private_directory(self.state_root / "secrets")
            self.records = ensure_private_directory(self.root / "records")
            cleaned = False
            for path in self.records.iterdir():
                if _TEMPORARY.fullmatch(path.name):
                    check_private_file(path)
                    path.unlink()
                    cleaned = True
            if cleaned:
                descriptor = os.open(self.records, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            for path in self._paths():
                self._record(path.stem)
        except (PrivateStateError, OSError, SettingsError, ValueError, UnicodeError, RecursionError):
            raise SettingsError("secrets_unavailable") from None

    def _directories(self) -> None:
        for directory in (self.state_root, self.root, self.records):
            _private_directory(directory)

    def _paths(self) -> list[Path]:
        self._directories()
        paths = []
        for path in self.records.iterdir():
            if not _RECORD.fullmatch(path.name):
                raise SettingsError("secrets_unavailable")
            check_private_file(path)
            paths.append(path)
            if len(paths) > MAX_SECRETS:
                raise SettingsError("secrets_unavailable")
        return paths

    def _record(self, secret_id: str) -> dict[str, Any]:
        self._directories()
        path = self.records / (secret_id + ".json")
        record = _read_record(path)
        expected = {"schema_version", "secret_id", "name", "status", "created_at"}
        if record.get("status") == "active":
            expected.add("value")
        created = record.get("created_at")
        if (set(record) != expected or type(record.get("schema_version")) is not int
                or record["schema_version"] != 1 or record.get("secret_id") != secret_id
                or not isinstance(record.get("status"), str) or record["status"] not in {"active", "revoked"}
                or not _secret_name(record.get("name")) or type(created) not in (float, int)
                or not 0 <= created <= 2 ** 53 or not math.isfinite(created)
                or record["status"] == "active" and not _secret_value(record.get("value"))):
            raise SettingsError("secrets_unavailable")
        _uuid(record["secret_id"])
        return record

    @staticmethod
    def _metadata(record: dict[str, Any]) -> dict[str, Any]:
        return {name: record[name] for name in ("secret_id", "name", "status", "created_at")}

    def create(self, name: Any, value: Any) -> dict[str, Any]:
        if not _secret_name(name) or not _secret_value(value):
            raise SettingsError("invalid_secret")
        try:
            if len(self._paths()) >= MAX_SECRETS:
                raise SettingsError("secret_limit")
            secret_id = str(uuid.uuid4())
            path = self.records / (secret_id + ".json")
            if path.exists() or path.is_symlink():
                raise SettingsError("secrets_unavailable")
            record = {"schema_version": 1, "secret_id": secret_id, "name": name, "status": "active",
                      "created_at": time.time(), "value": value}
            write_private_json(path, record)
            return self._metadata(record)
        except (PrivateStateError, OSError, ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, SettingsError):
                raise
            raise SettingsError("secrets_unavailable") from None

    def get(self, secret_id: Any) -> dict[str, Any]:
        secret_id = _uuid(secret_id)
        try:
            return self._metadata(self._record(secret_id))
        except FileNotFoundError:
            raise SettingsError("secret_not_found") from None
        except (PrivateStateError, OSError, ValueError, UnicodeError, RecursionError):
            # A missing file is distinguished only after directory safety checks.
            try:
                self._directories()
                path = self.records / (secret_id + ".json")
                if not path.exists() and not path.is_symlink():
                    raise SettingsError("secret_not_found")
            except SettingsError:
                raise
            except (PrivateStateError, OSError):
                pass
            raise SettingsError("secrets_unavailable") from None

    def list(self) -> list[dict[str, Any]]:
        try:
            records = [self._metadata(self._record(path.stem)) for path in self._paths()]
            return sorted(records, key=lambda record: (record["created_at"], record["secret_id"]), reverse=True)[:100]
        except (PrivateStateError, OSError, ValueError, UnicodeError, RecursionError):
            raise SettingsError("secrets_unavailable") from None

    def count(self) -> int:
        try:
            return len(self._paths())
        except (PrivateStateError, OSError, ValueError):
            raise SettingsError("secrets_unavailable") from None

    def revoke(self, secret_id: Any) -> dict[str, Any]:
        secret_id = _uuid(secret_id)
        self.get(secret_id)
        try:
            record = self._record(secret_id)
            if record["status"] == "active":
                record = {"schema_version": 1, **self._metadata(record), "status": "revoked"}
                write_private_json(self.records / (secret_id + ".json"), record)
            return self._metadata(record)
        except (PrivateStateError, OSError, ValueError, UnicodeError, RecursionError):
            raise SettingsError("secrets_unavailable") from None

    def resolve(self, references: Any) -> dict[str, str]:
        """Resolve only explicitly selected logical names; failures reveal no material."""
        try:
            if (not isinstance(references, dict) or len(references) > 16
                    or any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in references)):
                raise SettingsError("secret_reference_unavailable")
            result = {}
            for name, secret_id in references.items():
                record = self._record(_uuid(secret_id))
                if record["status"] != "active":
                    raise SettingsError("secret_reference_unavailable")
                result[name] = record["value"]
            return result
        except (PrivateStateError, OSError, ValueError, UnicodeError, RecursionError):
            raise SettingsError("secret_reference_unavailable") from None
