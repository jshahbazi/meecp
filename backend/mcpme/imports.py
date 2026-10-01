"""Static source intake and durable, revision-bound external preparation drafts.

Import mutations run synchronously inside one SQLite transaction. The caller must
link its durable installation intent with ``submit`` before scheduling execution.
This store never executes selected sources or package entrypoints.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .contracts import LOCK_NAME, MANIFEST_NAME, PackageError, package_relative_path, validate_package
from .private import check_private_file, ensure_private_directory, snapshot_package

IMPORT_SCHEMA_VERSION = 1
MAX_SOURCE_FILES = 2048
MAX_SCANNED_ENTRIES = 10000
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_DRAFTS = 1024
MAX_DRAFT_METADATA_BYTES = 512 * 1024
RETENTION_SECONDS = 30 * 24 * 60 * 60
TEMPLATE_IDS = ["python-function-v1", "existing-mcp-v1", "jevk5-llamacpp-v1"]
_TERMINAL_OPERATIONS = {"completed", "failed", "canceled"}
_OPERATION_STATUSES = _TERMINAL_OPERATIONS | {"pending", "running", "recovery_required"}
_EXCLUDED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".tox", ".nox",
    "build", "dist", "target", "artifacts", "bundles", "models", "runtime", "logs",
}
_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".md", ".txt",
    ".rst", ".toml", ".json", ".yaml", ".yml", ".lock", ".c", ".h", ".cpp",
    ".hpp", ".rs", ".go", ".swift", ".java", ".rb", ".sh", ".css", ".html",
}
_SOURCE_NAMES = {"license", "copying", "notice", "readme", "makefile", "dockerfile"}
_SECRET_NAMES = {
    "credentials", "credentials.json", "secrets", "secrets.json", "secrets.yaml", "secrets.yml",
    "token", "tokens", "token.json", "config.local.json", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys", "known_hosts",
}
_CREDENTIAL_PATTERN = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|"
    rb"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
)


class ImportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identity(value: str) -> None:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ImportError("invalid_import_identity") from None


def _label(value: Any, code: str, *, limit: int = 256, empty: bool = False) -> None:
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any((ord(char) < 32 and char not in "\n\t") or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ImportError(code)


def _choices(data: dict[str, Any]) -> None:
    _label(data["name"], "invalid_slot_name")
    if any(ord(char) < 32 for char in data["name"]):
        raise ImportError("invalid_slot_name")
    _label(data["intent"], "invalid_import_intent", limit=16384, empty=True)
    if any(_CREDENTIAL_PATTERN.search(data[field].encode("utf-8")) for field in ("name", "intent")):
        raise ImportError("import_credentials_detected")
    if data["document_role"] is not None and data["document_role"] not in ("instructions", "reference"):
        raise ImportError("invalid_document_role")
    if type(data["start"]) is not bool or data["start_policy"] not in ("manual", "host"):
        raise ImportError("invalid_start_policy")
    requirements = data["requirements"]
    if (not isinstance(requirements, dict)
            or set(requirements) - {"tools", "profile", "memory_mb", "secret_references"}):
        raise ImportError("invalid_import_requirements")
    for field in ("tools", "secret_references"):
        values = requirements.get(field, [])
        if not isinstance(values, list) or len(values) > 128 or len(set(map(str, values))) != len(values):
            raise ImportError("invalid_import_requirements")
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
                raise ImportError("invalid_import_requirements")
    if requirements.get("profile") not in (None, "mcp-stdio", "model-service"):
        raise ImportError("invalid_import_requirements")
    memory = requirements.get("memory_mb")
    if memory is not None and (type(memory) is not int or not 1 <= memory <= 262144):
        raise ImportError("invalid_import_requirements")


def _excluded(relative: str) -> bool:
    parts = Path(relative).parts
    name = parts[-1].lower()
    return (any(part.lower() in _EXCLUDED_DIRECTORIES or part.startswith(".") for part in parts[:-1])
            or name.startswith(".") or name in _SECRET_NAMES or name.startswith(".env")
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".log"))
            or name.endswith((".local.json", ".local.toml", ".local.yaml", ".local.yml"))
            or "secret" in name or "credential" in name)


def _relative(value: Any) -> str:
    if not isinstance(value, str):
        raise ImportError("unsafe_source_path")
    try:
        package_relative_path(value)
    except PackageError:
        raise ImportError("unsafe_source_path") from None
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_source(path: Path) -> bytes:
    parent_fd = None
    try:
        # Resolve the chain through directory descriptors: checking only the
        # leaf with O_NOFOLLOW would still follow an ancestor replaced after
        # scanning the source tree.
        parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ImportError("unsafe_source_file")
            if before.st_size > MAX_SOURCE_FILE_BYTES:
                raise ImportError("source_size_exceeded")
            content = source.read(MAX_SOURCE_FILE_BYTES + 1)
            after = os.fstat(source.fileno())
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if len(content) > MAX_SOURCE_FILE_BYTES or any(
                    getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(current, key)
                    for key in attributes):
                raise ImportError("source_changed")
            visible_parent = path.parent.stat()
            opened_parent = os.fstat(parent_fd)
            if (path.resolve() != path or visible_parent.st_dev != opened_parent.st_dev
                    or visible_parent.st_ino != opened_parent.st_ino):
                raise ImportError("source_changed")
        if b"\0" in content:
            raise ImportError("unsupported_source_file")
        try:
            content.decode("utf-8")
        except UnicodeError:
            raise ImportError("unsupported_source_file") from None
        if _CREDENTIAL_PATTERN.search(content):
            raise ImportError("source_credentials_detected")
        return content
    except OSError:
        raise ImportError("unsafe_source_file") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _selected_files(root: Path, files: list[str] | None) -> list[str]:
    if files is not None:
        if not isinstance(files, list) or not files or len(files) > MAX_SOURCE_FILES:
            raise ImportError("invalid_source_selection")
        result = sorted(_relative(value) for value in files)
        if len(set(result)) != len(result):
            raise ImportError("invalid_source_selection")
        for relative in result:
            if _excluded(relative):
                raise ImportError("source_file_excluded")
            for parent in (root / relative).parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise ImportError("unsafe_source_path")
        return result
    result = []
    scanned = 0
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            for item in sorted(directory.iterdir()):
                scanned += 1
                if scanned > MAX_SCANNED_ENTRIES:
                    raise ImportError("source_size_exceeded")
                relative = str(item.relative_to(root))
                if _excluded(relative) or item.name.lower() in _EXCLUDED_DIRECTORIES:
                    continue
                mode = item.lstat().st_mode
                if stat.S_ISDIR(mode):
                    pending.append(item)
                elif stat.S_ISREG(mode):
                    if item.suffix.lower() in _SOURCE_SUFFIXES or item.name.lower() in _SOURCE_NAMES:
                        result.append(_relative(relative))
                else:
                    raise ImportError("unsafe_source_file")
                if len(result) > MAX_SOURCE_FILES:
                    raise ImportError("source_size_exceeded")
    except OSError:
        raise ImportError("unsafe_source_file") from None
    if not result:
        raise ImportError("supported_source_required")
    return sorted(result)


def _snapshot_source(source: Path, destination: Path, files: list[str] | None) -> dict[str, Any]:
    source = Path(source).absolute()
    if source.is_symlink() or source.resolve() != source:
        raise ImportError("unsafe_source_path")
    if source.is_file():
        if source.suffix.lower() not in {".md", ".txt"} or files is not None:
            raise ImportError("unsupported_source")
        root, selected, kind = source.parent, [source.name], "document"
        if _excluded(source.name):
            raise ImportError("source_file_excluded")
    elif source.is_dir():
        root, selected, kind = source, _selected_files(source, files), "local_folder"
    else:
        raise ImportError("unsupported_source")
    ensure_private_directory(destination)
    entries = []
    total = 0
    for relative in selected:
        data = _read_source(root / relative)
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise ImportError("source_size_exceeded")
        target = destination / relative
        ensure_private_directory(target.parent)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        entries.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    # A second read detects edits while the bounded source selection was copied.
    for entry in entries:
        if hashlib.sha256(_read_source(root / entry["path"])).hexdigest() != entry["sha256"]:
            raise ImportError("source_changed")
    for directory, _, _ in os.walk(destination, topdown=False):
        _sync_directory(Path(directory))
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"kind": kind, "locator": str(source), "snapshot_path": str(destination),
            "files": entries, "digest": digest, "revision": "sha256:" + digest, "bytes": total}


def _attachment(package) -> dict[str, Any]:
    return {"digest": package.digest, "path": str(package.root),
            "source_revision": package.manifest.source_revision,
            "tools": [tool.mcp_schema() for tool in package.manifest.tools],
            "profile": package.manifest.profile, "access": package.manifest.access.model_dump(),
            "resources": package.manifest.resources.model_dump()}


def _blockers(draft: dict[str, Any]) -> list[dict[str, str]]:
    blockers = []
    def add(code: str, field: str) -> None:
        blockers.append({"code": code, "field": field})
    if draft["source"]["kind"] != "prepared_package" and not draft["intent"].strip():
        add("intent_required", "intent")
    if draft["source"]["kind"] == "document":
        if draft["document_role"] is None:
            add("document_role_required", "document_role")
        elif draft["document_role"] == "reference":
            add("document_template_required", "document_role")
    attachment = draft["attachment"]
    if attachment is None:
        add("prepared_package_required", "attachment")
    requirements = draft["requirements"]
    if requirements.get("secret_references"):
        add("secret_reference_unavailable", "requirements.secret_references")
    if attachment is not None:
        expected_tools = requirements.get("tools", [])
        if expected_tools and set(expected_tools) != {tool["name"] for tool in attachment["tools"]}:
            add("tool_contract_mismatch", "requirements.tools")
        if requirements.get("profile") and requirements["profile"] != attachment["profile"]:
            add("unsupported_process_topology", "requirements.profile")
        if requirements.get("memory_mb") is not None and attachment["resources"]["memory_mb"] > requirements["memory_mb"]:
            add("resource_requirement_mismatch", "requirements.memory_mb")
    return blockers


def _refresh(draft: dict[str, Any]) -> None:
    draft["blockers"] = _blockers(draft)
    if draft["state"] in {"canceled", "expired", "submitted", "installed"}:
        return
    information = any(item["code"] != "prepared_package_required" for item in draft["blockers"])
    if draft["attachment"] is not None:
        draft["state"] = "package_received" if draft["blockers"] else "ready_to_plan"
    else:
        draft["state"] = "needs_information" if information else "waiting_for_agent"


class ImportStore:
    """Persist choices and immutable snapshots independently of slot runtime state."""
    def __init__(self, state_root: Path):
        self.root = ensure_private_directory(Path(state_root))
        self.staging = ensure_private_directory(self.root / "imports")
        self.path = self.root / "imports.sqlite"
        self._lock = threading.RLock()
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists() or candidate.is_symlink():
                check_private_file(candidate)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        check_private_file(self.path)
        try:
            self._connection = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, IMPORT_SCHEMA_VERSION):
                raise ImportError("unsupported_import_schema")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS drafts (
                    draft_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    draft_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY(draft_id, revision)
                );
                PRAGMA user_version=1;
            """)
        except (sqlite3.Error, ImportError) as error:
            if hasattr(self, "_connection"):
                self._connection.close()
            if isinstance(error, ImportError):
                raise
            raise ImportError("imports_unavailable") from None

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ImportStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield
                self._connection.execute("COMMIT")
            except BaseException as error:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise ImportError("imports_unavailable") from None
                raise

    def _get(self, draft_id: str, revision: int | None = None) -> dict[str, Any]:
        _identity(draft_id)
        row = self._connection.execute("SELECT data FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            raise ImportError("import_not_found")
        draft = json.loads(row["data"])
        if revision is not None and (type(revision) is not int or revision != draft["revision"]):
            raise ImportError("stale_import_revision")
        return draft

    def _save(self, draft: dict[str, Any]) -> None:
        draft["updated_at"] = time.time()
        _refresh(draft)
        data = json.dumps(draft, sort_keys=True, separators=(",", ":"), allow_nan=False)
        # Reserve space for durable operation/result identities before apply.
        metadata_limit = MAX_DRAFT_METADATA_BYTES - (256 if draft["operation_id"] is None else 0)
        if len(data.encode("utf-8")) > metadata_limit:
            raise ImportError("import_metadata_limit")
        self._connection.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?)",
                                 (draft["draft_id"], draft["revision"], data))
        self._connection.execute("INSERT OR REPLACE INTO revisions VALUES (?,?,?)",
                                 (draft["draft_id"], draft["revision"], data))

    @staticmethod
    def _mutable(draft: dict[str, Any]) -> None:
        if draft["operation_id"] is not None:
            code = "draft_finalized" if draft["operation_status"] in _TERMINAL_OPERATIONS else "operation_in_progress"
            raise ImportError(code)
        if draft["state"] in {"canceled", "expired"}:
            raise ImportError("draft_inactive")

    def create(self, source: Path, *, name: str, intent: str = "", document_role: str | None = None,
               files: list[str] | None = None, start: bool = False, start_policy: str = "manual") -> dict[str, Any]:
        now = time.time()
        draft = {"schema_version": IMPORT_SCHEMA_VERSION, "draft_id": str(uuid.uuid4()), "revision": 1,
                 "name": name, "intent": intent, "document_role": document_role, "start": start,
                 "start_policy": start_policy, "requirements": {}, "attachment": None,
                 "state": "needs_information", "blockers": [], "operation_id": None,
                 "operation_status": None, "result_slot_id": None, "created_at": now,
                 "updated_at": now, "expires_at": now + RETENTION_SECONDS}
        _choices(draft)
        directory = self.staging / draft["draft_id"]
        try:
            with self._transaction():
                if self._connection.execute("SELECT count(*) FROM drafts").fetchone()[0] >= MAX_DRAFTS:
                    raise ImportError("import_limit_exceeded")
                ensure_private_directory(directory)
                source = Path(source).absolute()
                if source.is_dir() and (source / MANIFEST_NAME).exists():
                    if files is not None:
                        raise ImportError("invalid_source_selection")
                    package = validate_package(source)
                    sealed = snapshot_package(package, directory / "package", expected_digest=package.digest)
                    draft["source"] = {"kind": "prepared_package", "locator": str(source),
                                       "snapshot_path": str(sealed.root), "digest": sealed.digest,
                                       "revision": sealed.manifest.source_revision,
                                       "files": [], "file_inventory": LOCK_NAME,
                                       "artifact_count": len(sealed.lock.artifacts),
                                       "bytes": sum(item.size for item in sealed.lock.artifacts)}
                    draft["attachment"] = _attachment(sealed)
                else:
                    draft["source"] = _snapshot_source(source, directory / "source", files)
                _sync_directory(directory)
                _sync_directory(self.staging)
                self._save(draft)
            return draft
        except BaseException:
            if directory.exists():
                shutil.rmtree(directory)
            raise

    def inspect(self, draft_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get(draft_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row["data"]) for row in self._connection.execute("SELECT data FROM drafts ORDER BY rowid")]

    def list_summaries(self) -> list[dict[str, Any]]:
        """Bound UI discovery without repeating every file, schema, and intent."""
        with self._lock:
            result = []
            for row in self._connection.execute("SELECT data FROM drafts ORDER BY rowid"):
                draft = json.loads(row["data"])
                summary = {key: draft[key] for key in ("draft_id", "revision", "state", "start", "start_policy",
                                                       "operation_id", "result_slot_id")}
                summary.update({"summary": True, "name": draft["name"][:64], "source": {"kind": draft["source"]["kind"]}})
                if draft["attachment"] is not None:
                    summary["attachment"] = {"digest": draft["attachment"]["digest"]}
                result.append(summary)
            return result

    def revise(self, draft_id: str, revision: int, **changes: Any) -> dict[str, Any]:
        if not changes or set(changes) - {"name", "intent", "document_role", "start", "start_policy", "requirements"}:
            raise ImportError("invalid_import_update")
        with self._transaction():
            draft = self._get(draft_id, revision)
            self._mutable(draft)
            previous_attachment = draft["attachment"]
            draft.update(changes)
            _choices(draft)
            draft["revision"] += 1
            # An explicit edit is required before a different returned package.
            draft["attachment"] = None
            draft["expires_at"] = time.time() + RETENTION_SECONDS
            self._save(draft)
        if previous_attachment is not None and previous_attachment["path"] != draft["source"]["snapshot_path"]:
            previous_path = Path(previous_attachment["path"])
            ensure_private_directory(previous_path)
            shutil.rmtree(previous_path)
        return draft

    def export(self, draft_id: str, revision: int) -> dict[str, Any]:
        with self._lock:
            draft = self._get(draft_id, revision)
            specification = json.loads(json.dumps(draft))
            specification["template_ids"] = TEMPLATE_IDS.copy()
            specification["retention_seconds"] = RETENTION_SECONDS
            specification["agent_instructions"] = (
                f"Prepare a sealed mcpme package for import {draft_id}, revision {revision}. "
                f"Use the source snapshot at {draft['source']['snapshot_path']}; its resolved source identity is "
                f"{draft['source']['revision']}. Read the structured intent and requirements in this specification. "
                "Use a separate workspace, locked dependencies, license/provenance records, and nonsecret synthetic "
                "validation fixtures. Supported preparation templates: " + ", ".join(TEMPLATE_IDS) + ". "
                "The host supports macOS arm64, package schema 1, MCP 2025-11-25 tools, mcp-stdio and model-service "
                "profiles. Do not include credentials or production inputs. Bind manifest.source_revision to the "
                "resolved source identity. Return with "
                f"mcpme imports attach {draft_id} --revision {revision} --package /absolute/path/to/package --json. "
                "Attachment validates the package statically; installation requires a separate authorized plan."
            )
            if draft["source"]["kind"] == "prepared_package":
                specification["agent_instructions"] = (
                    f"Import {draft_id}, revision {revision}, already contains a verified prepared package. "
                    "Its complete file inventory is the sealed mcpme.lock.json. Review the attached tools, access, "
                    "resources, and start settings, then request the exact installation plan with "
                    f"mcpme install plan --import {draft_id} --revision {revision} --json. "
                    "Apply only within the user's authority for that plan. No additional preparation is required."
                )
            return specification

    def attach(self, draft_id: str, revision: int, package: Path) -> dict[str, Any]:
        with self._transaction():
            draft = self._get(draft_id, revision)
            self._mutable(draft)
            validated = validate_package(package)
            if (draft["source"]["kind"] != "prepared_package"
                    and validated.manifest.source_revision != draft["source"]["revision"]):
                raise ImportError("source_identity_mismatch")
            existing = draft["attachment"]
            if existing is not None:
                if existing["digest"] != validated.digest:
                    raise ImportError("import_revision_required")
                if validate_package(Path(existing["path"])).digest != existing["digest"]:
                    raise ImportError("package_digest_mismatch")
                return draft
            path = self.staging / draft_id / f"package-{revision}-{validated.digest}"
            sealed = snapshot_package(validated, path, expected_digest=validated.digest)
            draft["attachment"] = _attachment(sealed)
            self._save(draft)
            return draft

    def _binding(self, draft_id: str, revision: int, package_digest: str) -> dict[str, Any]:
        draft = self._get(draft_id, revision)
        if draft["state"] in {"canceled", "expired"}:
            raise ImportError("draft_inactive")
        attachment = draft["attachment"]
        if attachment is None or attachment["digest"] != package_digest:
            raise ImportError("import_binding_mismatch")
        if draft["blockers"]:
            raise ImportError("import_not_ready")
        verified = validate_package(Path(attachment["path"]))
        if verified.digest != package_digest:
            raise ImportError("package_digest_mismatch")
        return draft

    def validate_binding(self, draft_id: str, revision: int, package_digest: str) -> dict[str, Any]:
        with self._transaction():
            draft = self._get(draft_id, revision)
            self._mutable(draft)
            return self._binding(draft_id, revision, package_digest)

    def submit(self, draft_id: str, revision: int, package_digest: str, operation_id: str) -> dict[str, Any]:
        _identity(operation_id)
        with self._transaction():
            draft = self._get(draft_id, revision)
            if draft["state"] in {"canceled", "expired"}:
                raise ImportError("draft_inactive")
            if draft["attachment"] is None or draft["attachment"]["digest"] != package_digest:
                raise ImportError("import_binding_mismatch")
            if draft["operation_id"] is not None:
                if draft["operation_id"] != operation_id:
                    raise ImportError("import_operation_mismatch")
                return draft
            # validate_binding precedes the caller's durable operation creation.
            # Reconciliation must preserve that intent even if an artifact was
            # damaged after the operation record committed.
            if draft["blockers"]:
                raise ImportError("import_not_ready")
            draft["operation_id"] = operation_id
            draft["operation_status"] = "pending"
            draft["state"] = "submitted"
            self._save(draft)
            return draft

    def operation_status(self, draft_id: str, operation: Any) -> dict[str, Any]:
        data = operation.to_dict() if hasattr(operation, "to_dict") else operation
        if not isinstance(data, dict) or data.get("status") not in _OPERATION_STATUSES:
            raise ImportError("invalid_operation_status")
        with self._transaction():
            draft = self._get(draft_id)
            if draft["operation_id"] is None or data.get("operation_id") != draft["operation_id"]:
                raise ImportError("import_operation_mismatch")
            if draft["operation_status"] in _TERMINAL_OPERATIONS:
                if draft["operation_status"] != data["status"]:
                    raise ImportError("draft_finalized")
                return draft
            if draft["operation_status"] == "running" and data["status"] == "pending":
                raise ImportError("invalid_operation_transition")
            draft["operation_status"] = data["status"]
            if data["status"] == "completed":
                slot_id = data.get("details", {}).get("result_slot_id") or data.get("slot_id")
                _identity(slot_id)
                draft["result_slot_id"] = slot_id
                draft["state"] = "installed"
            self._save(draft)
            return draft

    def _deactivate(self, draft_id: str, revision: int, state: str) -> dict[str, Any]:
        with self._transaction():
            draft = self._get(draft_id, revision)
            if draft["state"] != state:
                self._mutable(draft)
                draft["state"] = state
                self._save(draft)
        # Publication of the inactive state precedes reclamation. Submission
        # always reads that same state inside a write transaction.
        directory = self.staging / draft_id
        if directory.exists():
            ensure_private_directory(directory)
            try:
                shutil.rmtree(directory)
                _sync_directory(self.staging)
            except OSError:
                raise ImportError("import_cleanup_failed") from None
        return draft

    def cancel(self, draft_id: str, revision: int) -> dict[str, Any]:
        return self._deactivate(draft_id, revision, "canceled")

    def expire(self, draft_id: str, revision: int) -> dict[str, Any]:
        return self._deactivate(draft_id, revision, "expired")
