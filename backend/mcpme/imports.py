"""Static source intake and durable, revision-bound external preparation drafts.

Import mutations run synchronously inside one SQLite transaction. The caller must
link its durable installation intent with ``submit`` before scheduling execution.
This store never executes selected sources or package entrypoints.
"""
from __future__ import annotations

import contextlib
import fcntl
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
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from .contracts import LOCK_NAME, MANIFEST_NAME, PackageError, package_relative_path, validate_package
from .private import PrivateStateError, _check_ancestors, check_private_file, ensure_private_directory, snapshot_package

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
_ACTIVE_ATTACHMENT_STAGES_LOCK = threading.RLock()
_ATTACHMENT_STAGE_LOCKS: dict[Path, _AttachmentLease] = {}


@dataclass
class _AttachmentLease:
    descriptor: int
    metadata: bytes
    identity: tuple[int, ...] | None = None
    claimed: bool = False


def _stage_metadata(stage: dict[str, Any]) -> bytes:
    return json.dumps(stage, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _root_identity(path: Path) -> tuple[int, ...]:
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise ImportError("package_digest_mismatch")
    return (details.st_dev, details.st_ino, details.st_mode, details.st_mtime_ns, details.st_ctime_ns)


def _existing_private_tree(path: Path) -> bool:
    """Check a cleanup target without recreating a tree another owner removed."""
    _check_ancestors(path)
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(details.st_mode):
        raise ImportError("imports_unavailable")
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise PrivateStateError()
    return True


def _missing_cleanup_entry(function, path, error) -> None:
    # A collector can remove a child or quarantine its parent after discovery.
    # Other failures still abort cleanup and retain the existing error contract.
    if not isinstance(error, FileNotFoundError):
        raise error


def _remove_attachment_tree(path: Path) -> None:
    if _existing_private_tree(path):
        try:
            shutil.rmtree(path, onexc=_missing_cleanup_entry)
        except FileNotFoundError:
            # A second collector may already be deleting a quarantined tree.
            pass
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
            or set(requirements) - {"tools", "profile", "memory_mb", "secret_references", "configuration", "engine_profile"}):
        raise ImportError("invalid_import_requirements")
    for field in ("tools", "secret_references"):
        values = requirements.get(field, [])
        if field == "secret_references" and isinstance(values, dict):
            continue
        if not isinstance(values, list) or len(values) > 128 or len(set(map(str, values))) != len(values):
            raise ImportError("invalid_import_requirements")
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
                raise ImportError("invalid_import_requirements")
    from .settings import SettingsError, validate_settings_choices
    try:
        references = requirements.get("secret_references", {})
        validate_settings_choices({"configuration": requirements.get("configuration", {}),
                                   "secret_references": references if isinstance(references, dict) else {}})
    except SettingsError:
        raise ImportError("invalid_import_requirements") from None
    if requirements.get("profile") not in (None, "mcp-stdio", "mcp-http", "model-service"):
        raise ImportError("invalid_import_requirements")
    if requirements.get("engine_profile") not in (None, "jevk5-llamacpp-v1"):
        raise ImportError("unsupported_model_profile")
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
            "resources": package.manifest.resources.model_dump(),
            "configuration": package.manifest.configuration.model_dump()}


def _model_return(draft: dict[str, Any], package) -> None:
    """A returned model package must contain the exact selected binary artifacts."""
    captured = (draft["source"].get("collection")
                if draft["source"]["kind"] == "captured_artifact_collection"
                else draft.get("captured_artifacts", {}).get("collection"))
    if captured is not None:
        from .preparation_artifacts import CapturedArtifactCollectionV1
        collection = CapturedArtifactCollectionV1.model_validate(captured)
        locked = {(item.sha256, item.size) for item in package.lock.artifacts}
        if any((item.sha256, item.size) not in locked for item in collection.entries):
            raise ImportError("captured_artifact_mismatch")
    if draft["source"]["kind"] != "artifact_collection":
        return
    from .model_intake import ENGINE_BUILD, MODEL, PROFILE
    model = package.manifest.model
    if (model is None or model.model_id != MODEL["model_id"] or model.sha256 != MODEL["sha256"]
            or model.profile != "m5-benchmark-v1" or model.context_tokens != 8192
            or model.engine_identity != {"runtime": "llama.cpp", "build": ENGINE_BUILD}
            or model.parameters != {"temperature": 1.22, "knockout_temperature": 0.93}
            or model.artifact != "models/" + MODEL["file"]):
        raise ImportError("model_artifact_mismatch")
    artifacts = {item.path: item for item in package.lock.artifacts}
    for item in draft["source"]["files"]:
        if item["role"] == "model-weights":
            relative = model.artifact
        elif item["path"] == "engine/llama/llama-server":
            continue  # Legacy reaper ownership is deliberately replaced by the host.
        elif item["role"] in {"runtime-file", "license", "receipt"}:
            relative = item["path"].removeprefix("engine/")
        else:
            raise ImportError("invalid_model_source_contract")
        artifact = artifacts.get(relative)
        if artifact is None or (artifact.sha256, artifact.size, artifact.executable) != (item["sha256"], item["size"], item["executable"]):
            raise ImportError("model_artifact_mismatch")
    receipt_path = package.root / "preparation-receipt.json"
    try:
        descriptor = os.open(receipt_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_DRAFT_METADATA_BYTES + 1)
        receipt = json.loads(raw)
        if (len(raw) > MAX_DRAFT_METADATA_BYTES or receipt.get("schema_version") != 1
                or receipt.get("template_id") != PROFILE or receipt.get("template_version") != 1
                or receipt.get("draft_id") != draft["draft_id"] or receipt.get("draft_revision") != draft["revision"]
                or receipt.get("source_revision") != draft["source"]["revision"]
                or receipt.get("source_files") != draft["source"]["files"]
                or receipt.get("engine_build") != ENGINE_BUILD):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise ImportError("model_preparation_receipt_required") from None


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
    if draft["source"]["kind"] == "artifact_collection":
        if draft["source"].get("engine") is None:
            add("engine_artifacts_required", "source.engine")
        if draft["requirements"].get("engine_profile") != "jevk5-llamacpp-v1":
            add("model_profile_required", "requirements.engine_profile")
        if draft["requirements"].get("profile") != "model-service":
            add("model_service_required", "requirements.profile")
        if set(draft["requirements"].get("tools", [])) != {"jevk5_decide", "jevk5_status"}:
            add("model_tools_required", "requirements.tools")
    attachment = draft["attachment"]
    if attachment is None:
        add("prepared_package_required", "attachment")
    requirements = draft["requirements"]
    if requirements.get("secret_references") and not isinstance(requirements["secret_references"], dict):
        add("secret_reference_unavailable", "requirements.secret_references")
    if attachment is not None:
        from .contracts import Access, Configuration
        from .settings import SettingsError, normalize_settings
        references = requirements.get("secret_references", {})
        try:
            manifest = SimpleNamespace(configuration=Configuration.model_validate(attachment.get("configuration", {})),
                                       access=Access.model_validate(attachment["access"]))
            normalize_settings(manifest, {"configuration": requirements.get("configuration", {}),
                                         "secret_references": references if isinstance(references, dict) else {}})
        except SettingsError as error:
            add(error.code, "requirements.configuration" if error.code == "configuration_required" else "requirements")
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
        self.attachment_staging = ensure_private_directory(self.root / "attachment-staging")
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
            self._recover_inactive_staging()
            self._recover_attachment_staging()
        except (sqlite3.Error, ImportError, OSError, ValueError, UnicodeError) as error:
            if hasattr(self, "_connection"):
                self._connection.close()
            if isinstance(error, ImportError):
                raise
            raise ImportError("imports_unavailable") from None

    def _recover_inactive_staging(self) -> None:
        """Quarantine abandoned publications under the same admission winner.

        The database lock excludes a live publisher between rename and commit.
        Only directory renames occur inside the transaction; deletion follows.
        """
        abandoned = []
        with self._transaction():
            drafts = [json.loads(row["data"]) for row in self._connection.execute("SELECT data FROM drafts")]
            referenced = {Path(draft["attachment"]["path"]).absolute()
                          for draft in drafts if draft.get("attachment")}
            inactive = {draft["draft_id"] for draft in drafts if draft.get("state") in {"canceled", "expired"}}
            for directory in self.staging.iterdir():
                if directory.name in inactive and directory.is_symlink():
                    raise ImportError("imports_unavailable")
                if directory.is_symlink() or not directory.is_dir():
                    continue
                candidates = [directory] if directory.name in inactive else list(directory.glob("package-*"))
                for candidate in candidates:
                    if candidate.absolute() in referenced and directory.name not in inactive:
                        continue
                    if not _existing_private_tree(candidate):
                        continue
                    quarantine = self.attachment_staging / (".garbage-" + uuid.uuid4().hex)
                    try:
                        os.rename(candidate, quarantine)
                    except FileNotFoundError:
                        if candidate.exists() or candidate.is_symlink():
                            raise
                        continue
                    abandoned.append(quarantine)
                if directory.exists():
                    try:
                        _sync_directory(directory)
                    except FileNotFoundError:
                        if directory.exists() or directory.is_symlink():
                            raise
            _sync_directory(self.staging)
        for candidate in abandoned:
            _remove_attachment_tree(candidate)

    @contextlib.contextmanager
    def _attachment_coordination(self) -> Iterator[None]:
        """Serialize lease creation and collection across processes.

        Creating a file and flocking it are not atomic. This short directory
        lease prevents a collector from unlinking a not-yet-locked owner file.
        It is never held during package validation or copying.
        """
        with _ACTIVE_ATTACHMENT_STAGES_LOCK:
            descriptor = os.open(self.attachment_staging / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                check_private_file(self.attachment_staging / ".lock")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)

    def _recover_attachment_staging(self) -> None:
        """Collect only trees whose OS lease proves their owner has stopped."""
        with self._attachment_coordination():
            protected = set()
            entries = list(self.attachment_staging.iterdir())
            for entry in entries:
                if not entry.name.endswith(".owner"):
                    continue
                base = entry.name[:-6]
                check_private_file(entry)
                descriptor = os.open(entry, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        # Both the completed tree and snapshot_package's
                        # in-progress temp tree belong to this live lease.
                        protected.add(base)
                        continue
                    for tree in entries:
                        if tree.name == base or (tree.name.startswith(base + ".") and tree.name.endswith(".tmp")):
                            _remove_attachment_tree(tree)
                    entry.unlink(missing_ok=True)
                finally:
                    os.close(descriptor)
            for entry in entries:
                if entry.name == ".lock" or entry.name.endswith(".owner"):
                    continue
                if any(entry.name == base or entry.name.startswith(base + ".") for base in protected):
                    continue
                _remove_attachment_tree(entry)
            _sync_directory(self.attachment_staging)

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
               files: list[str] | None = None, start: bool = False, start_policy: str = "manual",
               model_engine: Path | None = None) -> dict[str, Any]:
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
            source = Path(source).absolute()
            model_source = source.suffix.lower() == ".gguf"
            if model_engine is not None and not model_source:
                raise ImportError("invalid_source_selection")
            if model_source:
                if files is not None or document_role is not None:
                    raise ImportError("invalid_source_selection")
                # A multi-GiB binary copy never holds the SQLite write lock.
                from .model_intake import ModelIntakeError, snapshot_model
                ensure_private_directory(directory)
                try:
                    draft["source"] = snapshot_model(source, directory / "source", Path(model_engine) if model_engine is not None else None)
                except ModelIntakeError as error:
                    raise ImportError(error.code) from None
            with self._transaction():
                if self._connection.execute("SELECT count(*) FROM drafts").fetchone()[0] >= MAX_DRAFTS:
                    raise ImportError("import_limit_exceeded")
                ensure_private_directory(directory)
                if model_source:
                    pass
                elif source.is_dir() and (source / MANIFEST_NAME).exists():
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

    def _admit_captured(self, collection, *, choices: dict[str, Any] | None = None,
                        draft_id: str | None = None, revision: int | None = None) -> dict[str, Any]:
        """Admit a host-verified collection inside the capture transaction.

        This metadata-only seam is called by the capture coordinator after its
        owned verification barrier. The coordinator acquires the resulting draft
        reference and records success in this same transaction. External routes
        must submit local selections to that coordinator, never a manifest here.
        """
        from .preparation_artifacts import CapturedArtifactCollectionV1
        if not self._connection.in_transaction or not isinstance(collection, CapturedArtifactCollectionV1):
            raise ImportError("capture_admission_required")
        reference = {"schema_version": 1, "kind": "captured_artifact_collection",
                     "digest": collection.digest, "collection": collection.model_dump(mode="json")}
        now = time.time()
        if draft_id is None:
            if revision is not None or not isinstance(choices, dict) or set(choices) - {
                    "name", "intent", "document_role", "start", "start_policy", "requirements"}:
                raise ImportError("invalid_capture_target")
            if self._connection.execute("SELECT count(*) FROM drafts").fetchone()[0] >= MAX_DRAFTS:
                raise ImportError("import_limit_exceeded")
            draft = {"schema_version": IMPORT_SCHEMA_VERSION, "draft_id": str(uuid.uuid4()), "revision": 1,
                     "name": choices.get("name"), "intent": choices.get("intent", ""),
                     "document_role": choices.get("document_role"), "start": choices.get("start", False),
                     "start_policy": choices.get("start_policy", "manual"),
                     "requirements": choices.get("requirements", {}), "attachment": None,
                     "state": "needs_information", "blockers": [], "operation_id": None,
                     "operation_status": None, "result_slot_id": None, "created_at": now,
                     "updated_at": now, "expires_at": now + RETENTION_SECONDS}
            draft["source"] = {"kind": "captured_artifact_collection", "contract_version": 1,
                               "digest": collection.digest, "revision": "sha256:" + collection.digest,
                               "collection": reference["collection"], "files": [],
                               "file_inventory": "captured-artifact-collection-v1",
                               "artifact_count": len(collection.entries),
                               "bytes": sum(item.size for item in collection.entries)}
        else:
            if choices is not None or type(revision) is not int:
                raise ImportError("invalid_capture_target")
            draft = self._get(draft_id, revision)
            self._mutable(draft)
            if draft["source"]["kind"] not in {"local_folder", "document", "captured_artifact_collection"}:
                raise ImportError("unsupported_capture_target")
            if draft["source"]["kind"] == "captured_artifact_collection":
                draft["source"] = {"kind": "captured_artifact_collection", "contract_version": 1,
                                   "digest": collection.digest, "revision": "sha256:" + collection.digest,
                                   "collection": reference["collection"], "files": [],
                                   "file_inventory": "captured-artifact-collection-v1",
                                   "artifact_count": len(collection.entries),
                                   "bytes": sum(item.size for item in collection.entries)}
            else:
                # Supplemental binaries change preparation inputs, not the
                # original folder's byte identity or immutable snapshot.
                draft["captured_artifacts"] = reference
            draft["revision"] += 1
            draft["attachment"] = None
            draft["expires_at"] = now + RETENTION_SECONDS
        _choices(draft)
        self._save(draft)
        return draft

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(row["data"]) for row in self._connection.execute("SELECT data FROM drafts ORDER BY rowid")]

    def discard_previous_capture_attachment(self, draft_id: str, revision: int) -> None:
        """Collect only the superseded attachment after capture admission commits."""
        _identity(draft_id)
        if type(revision) is not int or revision < 1:
            raise ImportError("invalid_import_revision")
        with self._lock:
            current = self._get(draft_id)
            row = self._connection.execute("SELECT data FROM revisions WHERE draft_id=? AND revision=?",
                                           (draft_id, revision - 1)).fetchone()
            previous = json.loads(row[0])["attachment"] if row is not None else None
            if (previous is None or current["revision"] < revision
                    or previous["path"] == current["source"].get("snapshot_path")
                    or current["attachment"] is not None and previous["path"] == current["attachment"]["path"]):
                return
            path = Path(previous["path"])
            if not path.is_relative_to(self.staging / draft_id):
                raise ImportError("imports_unavailable")
        _remove_attachment_tree(path)

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

    def revise(self, draft_id: str, revision: int, *, carry_attachment: bool = False, **changes: Any) -> dict[str, Any]:
        if not changes or set(changes) - {"name", "intent", "document_role", "start", "start_policy", "requirements"}:
            raise ImportError("invalid_import_update")
        if type(carry_attachment) is not bool:
            raise ImportError("invalid_import_update")
        if carry_attachment:
            with self._lock:
                previous = self._get(draft_id, revision)
                self._mutable(previous)
            if previous["attachment"] is None or set(changes) - {"name", "start", "start_policy", "requirements"}:
                raise ImportError("invalid_import_update")
            requirements = changes.get("requirements", previous["requirements"])
            if (not isinstance(requirements, dict) or {key: value for key, value in requirements.items()
                    if key not in {"configuration", "secret_references"}} != {key: value for key, value in previous["requirements"].items()
                    if key not in {"configuration", "secret_references"}}):
                raise ImportError("invalid_import_update")
            try:
                sealed = validate_package(Path(previous["attachment"]["path"]))
            except PackageError as error:
                raise ImportError(error.code) from None
            if (sealed.digest != previous["attachment"]["digest"]
                    or sealed.manifest.source_revision != previous["attachment"]["source_revision"]):
                raise ImportError("package_digest_mismatch")
        with self._transaction():
            draft = self._get(draft_id, revision)
            self._mutable(draft)
            previous_attachment = draft["attachment"]
            draft.update(changes)
            _choices(draft)
            draft["revision"] += 1
            # An explicit edit is required before a different returned package.
            if not carry_attachment:
                draft["attachment"] = None
            else:
                # The explicit settings-only path binds a new revision to the
                # same static artifact; all installation plans are invalidated.
                # A draft may keep valid partial choices while the required
                # fields or secret references remain installation blockers.
                # Supplied choices still bind to the verified package contract.
                configuration = draft["requirements"].get("configuration", {})
                references = draft["requirements"].get("secret_references", {})
                fields = sealed.manifest.configuration.fields
                if (set(configuration) - set(fields)
                        or any(not fields[name].accepts(value) for name, value in configuration.items())
                        or (references and not isinstance(references, dict))
                        or set(references) - set(sealed.manifest.access.secrets)):
                    raise ImportError("invalid_import_requirements")
            draft["expires_at"] = time.time() + RETENTION_SECONDS
            self._save(draft)
        if not carry_attachment and previous_attachment is not None and previous_attachment["path"] != draft["source"].get("snapshot_path"):
            previous_path = Path(previous_attachment["path"])
            _remove_attachment_tree(previous_path)
        return draft

    def add_engine(self, draft_id: str, revision: int, engine: Path) -> dict[str, Any]:
        """Add an explicit engine build to a saved model draft without execution."""
        from .model_intake import MODEL, ModelIntakeError, snapshot_model
        with self._lock:
            previous = self._get(draft_id, revision)
            self._mutable(previous)
            if previous["source"]["kind"] != "artifact_collection":
                raise ImportError("model_draft_required")
        directory = self.staging / draft_id
        target = directory / ("source-" + uuid.uuid4().hex)
        try:
            try:
                selected = snapshot_model(Path(previous["source"]["snapshot_path"]) / "weights" / MODEL["file"], target, Path(engine))
            except ModelIntakeError as error:
                raise ImportError(error.code) from None
            selected["locator"] = previous["source"]["locator"]
            with self._transaction():
                draft = self._get(draft_id, revision)
                self._mutable(draft)
                attachment = draft["attachment"]
                draft["source"] = selected
                draft["attachment"] = None
                draft["revision"] += 1
                draft["expires_at"] = time.time() + RETENTION_SECONDS
                _sync_directory(directory)
                self._save(draft)
            if attachment is not None:
                _remove_attachment_tree(Path(attachment["path"]))
            return draft
        except BaseException:
            _remove_attachment_tree(target)
            raise

    def export(self, draft_id: str, revision: int) -> dict[str, Any]:
        with self._lock:
            draft = self._get(draft_id, revision)
            specification = json.loads(json.dumps(draft))
            specification["template_ids"] = TEMPLATE_IDS.copy()
            specification["retention_seconds"] = RETENTION_SECONDS
            source_instruction = (f"Use the source snapshot at {draft['source']['snapshot_path']}; its resolved source identity is "
                                  if "snapshot_path" in draft["source"] else
                                  "Use the selected captured artifact collection; its resolved source identity is ")
            specification["agent_instructions"] = (
                f"Prepare a sealed mcpme package for import {draft_id}, revision {revision}. "
                + source_instruction +
                f"{draft['source']['revision']}. Read the structured intent and requirements in this specification. "
                "Use a separate workspace, locked dependencies, license/provenance records, and nonsecret synthetic "
                "validation fixtures. Supported preparation templates: " + ", ".join(TEMPLATE_IDS) + ". "
                "The host supports macOS arm64, package schema 1, MCP 2025-11-25 tools, mcp-stdio, mcp-http and model-service "
                "profiles. Do not include credentials or production inputs. Bind manifest.source_revision to the "
                "resolved source identity. Declare typed nonsecret configuration in manifest.configuration and "
                "logical secret names in access.secrets. Read selected values only from MCPME_SETTINGS_FD at runtime; "
                "never put secret material in the package, source snapshot, fixtures, or agent instructions. Bind to the "
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
            elif draft["source"]["kind"] == "artifact_collection":
                specification["agent_instructions"] += (
                    " This source uses artifact_collection contract version 1. Do not decode binary weights as text. "
                    "Use templates/jevk5-llamacpp-v1/prepare.py --setup-export SPEC.json --host HOST --output PACKAGE "
                    "in your separate workspace. The generator consumes only this revision's private weight/engine "
                    "snapshot, verifies pinned JevK5 compatibility and exact relocation receipts, and derives the "
                    "package manifest and lock automatically. Engine artifacts and the explicit engine profile, "
                    "model-service profile, and exact JevK5 tools must be selected first. Intake, attachment and "
                    "planning never execute the source or engine."
                )
            if draft["source"]["kind"] == "captured_artifact_collection" or "captured_artifacts" in draft:
                specification["agent_instructions"] += (
                    " Captured artifacts have a separate version-1 collection identity and exact license references. "
                    "Original paths are unavailable. Materialize only through host-verified captured file descriptors; "
                    "do not decode model weights as text or substitute uncaptured files. Intake establishes bytes "
                    "and retention, while the selected recipe must establish runtime and model compatibility."
                )
            return specification

    def stage_attachment(self, draft_id: str, revision: int, package: Path,
                         *, expected_digest: str | None = None) -> dict[str, Any]:
        """Validate and copy a package without holding the import write lock."""
        with self._lock:
            draft = self._get(draft_id, revision)
            self._mutable(draft)
        # Hashing a large package must not hold even the connection's read lock.
        try:
            source = validate_package(package)
        except PackageError as error:
            raise ImportError(error.code) from None
        if expected_digest is not None and source.digest != expected_digest:
            raise ImportError("package_digest_mismatch")
        if (draft["source"]["kind"] != "prepared_package"
                and source.manifest.source_revision != draft["source"]["revision"]):
            raise ImportError("source_identity_mismatch")
        _model_return(draft, source)
        digest = source.digest
        stage = self.attachment_staging / f"{draft_id}-{revision}-{digest}-{uuid.uuid4().hex}"
        metadata = {"draft_id": draft_id, "revision": revision,
                    "path": str(stage), "digest": digest,
                    "source_revision": source.manifest.source_revision}
        frozen = _stage_metadata(metadata)
        owner_path = Path(str(stage) + ".owner")
        with self._attachment_coordination():
            owner = os.open(owner_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with os.fdopen(os.dup(owner), "wb") as output:
                    output.write(frozen)
                    output.flush()
                    os.fsync(output.fileno())
                _ATTACHMENT_STAGE_LOCKS[stage] = _AttachmentLease(owner, frozen)
                _sync_directory(self.attachment_staging)
            except BaseException:
                os.close(owner)
                owner_path.unlink(missing_ok=True)
                raise
        completed = False
        try:
            sealed = snapshot_package(source, stage, expected_digest=digest)
            with _ACTIVE_ATTACHMENT_STAGES_LOCK:
                _ATTACHMENT_STAGE_LOCKS[stage].identity = _root_identity(sealed.root)
            completed = True
            return metadata
        except PackageError as error:
            raise ImportError(error.code) from None
        except (OSError, ValueError):
            raise ImportError("package_snapshot_failed") from None
        finally:
            if not completed:
                self._discard_attachment_stage(stage)

    def _discard_attachment_stage(self, path: Path) -> None:
        """Clean while the lease remains held, then release its identity."""
        with _ACTIVE_ATTACHMENT_STAGES_LOCK:
            lease = _ATTACHMENT_STAGE_LOCKS.get(path)
        if lease is None:
            return
        try:
            _remove_attachment_tree(path)
        finally:
            with self._attachment_coordination():
                try:
                    _ATTACHMENT_STAGE_LOCKS.pop(path, None)
                    Path(str(path) + ".owner").unlink(missing_ok=True)
                    _sync_directory(self.attachment_staging)
                finally:
                    os.close(lease.descriptor)

    def _reclaim_publication(self, final: Path, identity: tuple[int, ...], stage: Path) -> None:
        """Rollback cleanup cannot remove a publication another writer admitted."""
        with self._transaction():
            references = [json.loads(row["data"]).get("attachment")
                          for row in self._connection.execute("SELECT data FROM drafts")]
            if any(item and Path(item["path"]).absolute() == final for item in references):
                return
            if final.exists() and _root_identity(final)[:2] == identity[:2]:
                os.rename(final, stage)
                _sync_directory(final.parent)

    def admit_attachment(self, stage: dict[str, Any], *, admission=None) -> dict[str, Any]:
        """Publish an already copied package with a short transaction."""
        required = {"draft_id", "revision", "path", "digest", "source_revision"}
        if not isinstance(stage, dict) or set(stage) != required:
            raise ImportError("invalid_attachment_stage")
        draft_id, revision = stage["draft_id"], stage["revision"]
        if not all(isinstance(stage[key], str) for key in ("path", "digest", "source_revision", "draft_id")):
            raise ImportError("invalid_attachment_stage")
        stage_path = Path(stage["path"]).absolute()
        if stage_path.parent != self.attachment_staging:
            raise ImportError("invalid_attachment_stage")
        _identity(draft_id)
        if type(revision) is not int or revision < 1 or not re.fullmatch(r"[0-9a-f]{64}", stage["digest"]):
            raise ImportError("invalid_attachment_stage")
        with _ACTIVE_ATTACHMENT_STAGES_LOCK:
            lease = _ATTACHMENT_STAGE_LOCKS.get(stage_path)
            if lease is None or lease.claimed or lease.identity is None or lease.metadata != _stage_metadata(stage):
                raise ImportError("invalid_attachment_stage")
            lease.claimed = True
        published = None
        committed = False
        try:
            sealed = validate_package(stage_path)
            if (sealed.digest != stage["digest"] or sealed.manifest.source_revision != stage["source_revision"]
                    or _root_identity(stage_path) != lease.identity):
                raise ImportError("package_digest_mismatch")
            final = self.staging / draft_id / f"package-{revision}-{sealed.digest}"
            while True:
                with self._lock:
                    observed = self._get(draft_id, revision)
                    self._mutable(observed)
                existing = observed["attachment"]
                if existing is not None and existing["digest"] != sealed.digest:
                    raise ImportError("import_revision_required")
                target = Path(existing["path"]).absolute() if existing else final
                target_identity = None
                # Validate even an idempotent attachment or an orphaned final
                # tree. These expensive reads are outside the admission lock.
                if existing is not None or target.exists() or target.is_symlink():
                    if validate_package(target).digest != sealed.digest:
                        raise ImportError("package_digest_mismatch")
                    target_identity = _root_identity(target)
                retry = False
                with self._transaction():
                    draft = self._get(draft_id, revision)
                    self._mutable(draft)
                    if draft["source"]["kind"] != "prepared_package" and sealed.manifest.source_revision != draft["source"]["revision"]:
                        raise ImportError("source_identity_mismatch")
                    if draft["attachment"] != existing:
                        # Another writer won while validation was in progress.
                        # Validate its tree outside the transaction before retry.
                        retry = True
                    elif target_identity is None and (target.exists() or target.is_symlink()):
                        retry = True
                    else:
                        if target_identity is not None and _root_identity(target) != target_identity:
                            raise ImportError("package_digest_mismatch")
                        if _root_identity(stage_path) != lease.identity:
                            raise ImportError("package_digest_mismatch")
                        if admission is not None:
                            admission(draft, sealed)
                        if existing is None:
                            if target_identity is None:
                                ensure_private_directory(final.parent)
                                os.rename(stage_path, final)
                                published = (final, _root_identity(final))
                                _sync_directory(final.parent)
                            attachment = _attachment(sealed)
                            attachment["path"] = str(final)
                            draft["attachment"] = attachment
                            self._save(draft)
                if not retry:
                    committed = True
                    return draft
        except ImportError:
            raise
        except PackageError as error:
            raise ImportError(error.code) from None
        except OSError:
            raise ImportError("package_snapshot_failed") from None
        finally:
            try:
                if published is not None and not committed:
                    self._reclaim_publication(*published, stage_path)
            finally:
                self._discard_attachment_stage(stage_path)

    def attach(self, draft_id: str, revision: int, package: Path) -> dict[str, Any]:
        stage = self.stage_attachment(draft_id, revision, package)
        return self.admit_attachment(stage)

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
        try:
            _remove_attachment_tree(directory)
            _sync_directory(self.staging)
        except OSError:
            raise ImportError("import_cleanup_failed") from None
        return draft

    def cancel(self, draft_id: str, revision: int) -> dict[str, Any]:
        return self._deactivate(draft_id, revision, "canceled")

    def expire(self, draft_id: str, revision: int) -> dict[str, Any]:
        return self._deactivate(draft_id, revision, "expired")
