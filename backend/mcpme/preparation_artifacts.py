"""Code-free, local-only capture of bounded preparation artifact collections.

The caller owns scheduling and joins this synchronous operation before releasing
its disk reservation. A computed or supplied hash proves bytes, not publisher
identity. Format labels are declarations; a recipe must validate compatibility.
"""
from __future__ import annotations

import contextlib
import codecs
import ctypes
import errno
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Annotated, Iterator, Literal
import uuid

from pydantic import Field, ValidationError, model_validator

from .contracts import Digest, StrictModel
from .preparation_contracts import (MAX_CONTRACT_BYTES, PreparationError,
                                    canonical_bytes, contract_digest, load_contract)

MAX_ARTIFACT_FILES = 64
MAX_ARTIFACT_FILE_BYTES = 16 * 1024 ** 3
MAX_ARTIFACT_BYTES = 32 * 1024 ** 3
MAX_PATH_COMPONENTS = 128
COPY_CHUNK_BYTES = 1024 * 1024
MANIFEST_NAME = "collection.json"
COLLECTION_DOMAIN = "captured-artifact-collection-v1"
LogicalName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$", strict=True)]
Role = Literal["model-weights", "runtime-file", "license", "validation"]
ArtifactFormat = Literal["onnx", "gguf", "safetensors", "binary", "text", "json", "png", "jpeg", "npy"]
_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY = _OPEN | os.O_DIRECTORY
_CREDENTIAL_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json",
                     "credentials.ini", "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa",
                     "id_ed25519", "authorized_keys", "private_key", "private-key"}
_CREDENTIAL_DIRECTORIES = {".ssh", ".aws", ".azure", ".gnupg"}
_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".gz", ".xz", ".bz2", ".7z", ".rar", ".whl", ".npz", ".zst", ".lz4"}
_CREDENTIAL_BYTES = re.compile(rb"-----BEGIN (?:[A-Z ]{1,32} )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")


class ArtifactError(PreparationError):
    """Content-free failures suitable for the host's sanitized progress journal."""


class _ArtifactContract(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def bounded(cls, value):
        if isinstance(value, dict):
            if "schema_version" in value and type(value["schema_version"]) is not int:
                raise ValueError("invalid_artifact_contract")
            # Arrays are immutable in the in-memory contract, canonical JSON
            # still uses ordered arrays. No input is sorted silently here.
            raw = dict(value)
            for key, item in tuple(raw.items()):
                if isinstance(item, StrictModel):
                    raw[key] = item.model_dump(mode="json")
            for key in ("entries", "licenses"):
                if isinstance(raw.get(key), (list, tuple)):
                    raw[key] = [item.model_dump(mode="json") if isinstance(item, StrictModel) else item for item in raw[key]]
            canonical_bytes(raw)
            value = dict(value)
            for key in ("entries", "licenses"):
                if isinstance(value.get(key), list):
                    value[key] = tuple(value[key])
        return value


class LocalFileAcquisitionV1(_ArtifactContract):
    name: LogicalName
    role: Role
    path: Annotated[str, Field(min_length=1, max_length=4096, strict=True)]
    format: ArtifactFormat
    expected_sha256: Digest | None = None
    expected_size: Annotated[int, Field(strict=True, ge=1, le=MAX_ARTIFACT_FILE_BYTES)] | None = None
    licenses: tuple[LogicalName, ...] = Field(default_factory=tuple, max_length=MAX_ARTIFACT_FILES)

    @model_validator(mode="after")
    def local_selection(self):
        _absolute_path(self.path)
        _safe_source_name(self.path)
        _check_license_names(self.role, self.licenses)
        if self.role == "license" and self.format != "text":
            raise ValueError("artifact_license_format_required")
        return self


class AcquisitionRequestV1(_ArtifactContract):
    schema_version: Literal[1] = 1
    kind: Literal["local-files"] = "local-files"
    entries: tuple[LocalFileAcquisitionV1, ...] = Field(min_length=1, max_length=MAX_ARTIFACT_FILES)

    @model_validator(mode="after")
    def selection(self):
        names = [entry.name for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError("artifact_names_not_unique")
        licenses = {entry.name for entry in self.entries if entry.role == "license"}
        if any(set(entry.licenses) - licenses for entry in self.entries):
            raise ValueError("artifact_license_reference_invalid")
        return self


class LocalSelectionOriginV1(_ArtifactContract):
    kind: Literal["local-selection"] = "local-selection"
    publisher_trust: Literal["publisher_unverified"] = "publisher_unverified"
    supplied_sha256: Digest | None = None


class CapturedLicenseReferenceV1(_ArtifactContract):
    name: LogicalName
    sha256: Digest


class CapturedArtifactV1(_ArtifactContract):
    name: LogicalName
    role: Role
    path: Annotated[str, Field(pattern=r"^files/[a-z][a-z0-9_-]{0,63}$", strict=True)]
    sha256: Digest
    size: Annotated[int, Field(strict=True, ge=1, le=MAX_ARTIFACT_FILE_BYTES)]
    format: ArtifactFormat
    origin: LocalSelectionOriginV1
    licenses: tuple[CapturedLicenseReferenceV1, ...] = Field(default_factory=tuple, max_length=MAX_ARTIFACT_FILES)

    @model_validator(mode="after")
    def artifact(self):
        if self.path != "files/" + self.name:
            raise ValueError("invalid_artifact_path")
        _check_license_names(self.role, tuple(item.name for item in self.licenses))
        if self.role == "license" and self.format != "text":
            raise ValueError("artifact_license_format_required")
        if self.origin.supplied_sha256 is not None and self.origin.supplied_sha256 != self.sha256:
            raise ValueError("artifact_hash_mismatch")
        return self


class CapturedArtifactCollectionV1(_ArtifactContract):
    schema_version: Literal[1] = 1
    kind: Literal["captured-artifact-collection"] = "captured-artifact-collection"
    entries: tuple[CapturedArtifactV1, ...] = Field(min_length=1, max_length=MAX_ARTIFACT_FILES)

    @model_validator(mode="after")
    def collection(self):
        names = [entry.name for entry in self.entries]
        if names != sorted(set(names)):
            raise ValueError("artifact_entries_not_sorted_unique")
        if sum(entry.size for entry in self.entries) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact_size_limit")
        licenses = {entry.name: entry.sha256 for entry in self.entries if entry.role == "license"}
        if any(licenses.get(ref.name) != ref.sha256 for entry in self.entries for ref in entry.licenses):
            raise ValueError("artifact_license_reference_invalid")
        return self

    @property
    def digest(self) -> str:
        return contract_digest(COLLECTION_DOMAIN, self.model_dump(mode="json"))


def _check_license_names(role: str, names: tuple[str, ...]) -> None:
    if names != tuple(sorted(set(names))):
        raise ValueError("artifact_licenses_not_sorted_unique")
    if role == "license":
        if names:
            raise ValueError("artifact_license_exemption_invalid")
    elif not names:
        raise ValueError("artifact_license_required")


def _absolute_path(value: str) -> Path:
    if (not value.startswith("/") or value != os.path.normpath(value)
            or value.startswith("//") or "\\" in value or "\x00" in value
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ArtifactError("artifact_path_invalid")
    path = Path(value)
    if len(path.parts) > MAX_PATH_COMPONENTS:
        raise ArtifactError("artifact_path_invalid")
    return path


def _safe_source_name(value: str) -> None:
    path = Path(value)
    parts = [part.lower() for part in path.parts]
    name = parts[-1]
    if (any(part in _CREDENTIAL_DIRECTORIES for part in parts)
            or name in _CREDENTIAL_NAMES or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx"))):
        raise ArtifactError("artifact_credentials_disallowed")
    if path.suffix.lower() in _ARCHIVE_SUFFIXES:
        raise ArtifactError("artifact_archive_disallowed")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def _private_directory(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ArtifactError("artifact_store_unsafe")


class _DirectoryChain:
    """Pin every ancestor and recheck each name against its open descriptor."""
    def __init__(self, path: Path, *, create: bool = False):
        self.fds: list[int] = []
        self.edges: list[tuple[int, str, int]] = []
        try:
            self.fds.append(os.open("/", _DIRECTORY))
            for part in path.parts[1:]:
                parent = self.fds[-1]
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                fd = os.open(part, _DIRECTORY, dir_fd=parent)
                self.edges.append((parent, part, fd))
                self.fds.append(fd)
            self.check()
        except BaseException:
            self.close()
            raise

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def check(self) -> None:
        for parent, name, fd in self.edges:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _directory_identity(current) != _directory_identity(os.fstat(fd)):
                raise ArtifactError("artifact_ancestor_changed")

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _open_directory(parent: int, name: str, *, create: bool = False) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        _private_directory(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _same_directory(parent: int, name: str, fd: int) -> None:
    if _directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _directory_identity(os.fstat(fd)):
        raise ArtifactError("artifact_ancestor_changed")
    _private_directory(fd)


def _new_directory(parent: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=parent)
    try:
        created = _directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except OSError:
        raise ArtifactError("artifact_capture_reconciliation_required") from None
    try:
        fd = _open_directory(parent, name)
    except BaseException:
        # rmdir never follows a node or removes a nonempty foreign tree.
        try:
            if _directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != created:
                raise ArtifactError("artifact_capture_reconciliation_required")
            os.rmdir(name, dir_fd=parent)
        except (OSError, ArtifactError):
            raise ArtifactError("artifact_capture_reconciliation_required") from None
        raise
    if _directory_identity(os.fstat(fd)) != created:
        os.close(fd)
        raise ArtifactError("artifact_capture_reconciliation_required")
    return fd


class _Budget:
    def __init__(self, disk_ceiling_bytes: int, cancel_event: threading.Event | None, deadline: float | None):
        try:
            finite_deadline = deadline is None or (type(deadline) in (float, int) and math.isfinite(deadline))
        except OverflowError:
            finite_deadline = False
        if (type(disk_ceiling_bytes) is not int or not 1 <= disk_ceiling_bytes <= MAX_ARTIFACT_BYTES + MAX_CONTRACT_BYTES
                or not finite_deadline
                or (cancel_event is not None and not isinstance(cancel_event, threading.Event))):
            raise ArtifactError("artifact_budget_invalid")
        self.ceiling = disk_ceiling_bytes
        self.cancel = cancel_event
        self.deadline = deadline
        self.written = 0

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ArtifactError("artifact_capture_canceled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ArtifactError("artifact_deadline_exceeded")

    def require_disk(self, fd: int, amount: int) -> None:
        self.check()
        if self.written + amount > self.ceiling:
            raise ArtifactError("artifact_disk_limit")
        available = os.fstatvfs(fd)
        if amount > available.f_bavail * available.f_frsize:
            raise ArtifactError("artifact_disk_unavailable")


def _regular(fd: int, *, readonly: bool = False) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArtifactError("artifact_file_unsafe")
    if readonly and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400):
        raise ArtifactError("artifact_store_unsafe")
    if not 1 <= info.st_size <= MAX_ARTIFACT_FILE_BYTES:
        raise ArtifactError("artifact_size_limit")
    return info


def _same_file(parent: int, name: str, fd: int, before: os.stat_result) -> None:
    if (_identity(before) != _identity(os.fstat(fd))
            or _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
        raise ArtifactError("artifact_file_changed")


def _hash_fd(fd: int, size: int, budget: _Budget, *, license_text: bool = False) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    count = 0
    tail = b""
    header = b""
    text = codecs.getincrementaldecoder("utf-8")() if license_text else None
    while True:
        budget.check()
        data = os.read(fd, min(COPY_CHUNK_BYTES, size - count + 1))
        if not data:
            break
        count += len(data)
        if count > size:
            raise ArtifactError("artifact_file_changed")
        header += data[:max(0, 512 - len(header))]
        if _archive_header(header):
            raise ArtifactError("artifact_archive_disallowed")
        if _CREDENTIAL_BYTES.search(tail + data):
            raise ArtifactError("artifact_credentials_disallowed")
        if text is not None:
            try:
                text.decode(data)
            except UnicodeDecodeError:
                raise ArtifactError("artifact_license_format_required") from None
        tail = data[-256:]
        digest.update(data)
    if count != size:
        raise ArtifactError("artifact_file_changed")
    if text is not None:
        try:
            text.decode(b"", final=True)
        except UnicodeDecodeError:
            raise ArtifactError("artifact_license_format_required") from None
    return digest.hexdigest()


def _archive_header(data: bytes) -> bool:
    return (data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07",
                             b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd", b"\x04\x22\x4d\x18"))
            or data[257:262] == b"ustar")


def _write(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ArtifactError("artifact_copy_failed")
        view = view[written:]


def _lock_publication(fd: int, budget: _Budget) -> None:
    while True:
        budget.check()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(0.01)


def _publish_directory(source: int, name: str, destination: int, digest: str) -> None:
    """Atomically rename a complete tree, never replacing even an empty node.

    macOS SDK sys/stdio.h defines renameatx_np and RENAME_EXCL (0x4).
    Linux uses the same fd-relative interface with RENAME_NOREPLACE (1).
    No path-based or overwrite-capable fallback is permitted.
    """
    library = ctypes.CDLL(None, use_errno=True)
    if hasattr(library, "renameatx_np"):
        function, flag = library.renameatx_np, 0x00000004
    elif hasattr(library, "renameat2"):
        function, flag = library.renameat2, 1
    else:
        raise ArtifactError("artifact_atomic_publish_unavailable")
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    result = function(source, os.fsencode(name), destination, os.fsencode(digest), flag)
    if result:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(errno.EEXIST, "artifact_object_exists")
        raise ArtifactError("artifact_atomic_publish_unavailable")


def _copy_source(entry: LocalFileAcquisitionV1, destination: int, budget: _Budget, remaining: int) -> tuple[int, str]:
    path = _absolute_path(entry.path)
    with _DirectoryChain(path.parent) as chain:
        fd = os.open(path.name, _OPEN, dir_fd=chain.fd)
        try:
            before = _regular(fd)
            if before.st_size > remaining:
                raise ArtifactError("artifact_size_limit")
            if entry.expected_size is not None and entry.expected_size != before.st_size:
                raise ArtifactError("artifact_size_mismatch")
            budget.require_disk(destination, before.st_size + MAX_CONTRACT_BYTES)
            out = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=destination)
            try:
                digest = hashlib.sha256()
                count = 0
                tail = b""
                header = b""
                text = codecs.getincrementaldecoder("utf-8")() if entry.role == "license" else None
                while True:
                    budget.check()
                    chain.check()
                    data = os.read(fd, min(COPY_CHUNK_BYTES, before.st_size - count + 1))
                    if not data:
                        break
                    count += len(data)
                    if count > before.st_size:
                        raise ArtifactError("artifact_file_changed")
                    header += data[:max(0, 512 - len(header))]
                    if _archive_header(header):
                        raise ArtifactError("artifact_archive_disallowed")
                    if _CREDENTIAL_BYTES.search(tail + data):
                        raise ArtifactError("artifact_credentials_disallowed")
                    tail = data[-256:]
                    if text is not None:
                        try:
                            text.decode(data)
                        except UnicodeDecodeError:
                            raise ArtifactError("artifact_license_format_required") from None
                    budget.require_disk(destination, len(data))
                    _write(out, data)
                    budget.written += len(data)
                    digest.update(data)
                if count != before.st_size:
                    raise ArtifactError("artifact_file_changed")
                if text is not None:
                    try:
                        text.decode(b"", final=True)
                    except UnicodeDecodeError:
                        raise ArtifactError("artifact_license_format_required") from None
                actual = digest.hexdigest()
                if entry.expected_sha256 is not None and entry.expected_sha256 != actual:
                    raise ArtifactError("artifact_hash_mismatch")
                _same_file(chain.fd, path.name, fd, before)
                # Identity checks alone cannot prove content equality. Re-read
                # the pinned source and captured output independently.
                if _hash_fd(fd, before.st_size, budget, license_text=entry.role == "license") != actual:
                    raise ArtifactError("artifact_file_changed")
                _same_file(chain.fd, path.name, fd, before)
                chain.check()
                os.fsync(out)
                os.fchmod(out, 0o400)
            finally:
                os.close(out)
            return before.st_size, actual
        finally:
            os.close(fd)


def _parse_collection(data: bytes) -> CapturedArtifactCollectionV1:
    try:
        return CapturedArtifactCollectionV1.model_validate(load_contract(data))
    except (ValidationError, PreparationError, ValueError):
        raise ArtifactError("artifact_manifest_invalid") from None


def _directory_names(fd: int, expected: set[str]) -> None:
    # Bound enumeration as well as parsing; do not materialize an unbounded
    # directory controlled by a same-user writer.
    names = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.name not in expected or entry.name in names:
                raise ArtifactError("artifact_tree_invalid")
            names.add(entry.name)
    if names != expected:
        raise ArtifactError("artifact_tree_invalid")


class ArtifactStore:
    """Private content-addressed objects, separate from legacy import storage."""
    def __init__(self, root: Path):
        self.root = _absolute_path(str(root))
        try:
            with _DirectoryChain(self.root, create=True) as chain:
                _private_directory(chain.fd)
                self._root_identity = _directory_identity(os.fstat(chain.fd))
                for name in ("objects", "staging"):
                    fd = _open_directory(chain.fd, name, create=True)
                    os.close(fd)
                chain.check()
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactError("artifact_store_unsafe") from None

    def _root(self) -> _DirectoryChain:
        chain = _DirectoryChain(self.root)
        try:
            if _directory_identity(os.fstat(chain.fd)) != self._root_identity:
                raise ArtifactError("artifact_ancestor_changed")
            _private_directory(chain.fd)
        except BaseException:
            chain.close()
            raise
        return chain

    def capture(self, request: AcquisitionRequestV1, *, disk_ceiling_bytes: int,
                cancel_event: threading.Event | None = None, deadline: float | None = None) -> CapturedArtifactCollectionV1:
        """Copy selected bytes, recheck, seal and publish without executing them.

        ``deadline`` is an absolute monotonic timestamp. Disk ceiling includes
        captured bytes and the bounded manifest. Async callers must join the
        capture thread, including cancellation, before releasing ownership.
        """
        try:
            if not isinstance(request, AcquisitionRequestV1):
                raise ArtifactError("artifact_request_invalid")
            request = AcquisitionRequestV1.model_validate(request.model_dump(mode="json"))
            budget = _Budget(disk_ceiling_bytes, cancel_event, deadline)
            budget.check()
            with self._root() as chain:
                objects = _open_directory(chain.fd, "objects")
                try:
                    staging = _open_directory(chain.fd, "staging")
                    try:
                        return self._capture(request, chain, objects, staging, budget)
                    finally:
                        os.close(staging)
                finally:
                    os.close(objects)
        except ArtifactError:
            raise
        except (ValidationError, PreparationError, ValueError):
            raise ArtifactError("artifact_request_invalid") from None
        except OSError:
            raise ArtifactError("artifact_capture_failed") from None

    def _capture(self, request, chain, objects, staging, budget):
        with contextlib.ExitStack() as owned:
            return self._capture_owned(request, chain, objects, staging, budget, owned)

    def _capture_owned(self, request, chain, objects, staging, budget, owned):
        name = str(uuid.uuid4())
        stage = _new_directory(staging, name)
        owned.callback(os.close, stage)
        files = -1
        moved = False
        collection = None
        try:
            files = _open_directory(stage, "files", create=True)
            owned.callback(os.close, files)
            records = []
            total = 0
            for entry in sorted(request.entries, key=lambda item: item.name):
                chain.check()
                size, digest = _copy_source(entry, files, budget, MAX_ARTIFACT_BYTES - total)
                total += size
                if total > MAX_ARTIFACT_BYTES:
                    raise ArtifactError("artifact_size_limit")
                records.append((entry, size, digest))
            licenses = {entry.name: digest for entry, _, digest in records if entry.role == "license"}
            collection = CapturedArtifactCollectionV1(entries=tuple(
                CapturedArtifactV1(name=entry.name, role=entry.role, path="files/" + entry.name, size=size,
                                   sha256=digest, format=entry.format,
                                   origin=LocalSelectionOriginV1(supplied_sha256=entry.expected_sha256),
                                   licenses=tuple(CapturedLicenseReferenceV1(name=ref, sha256=licenses[ref]) for ref in entry.licenses))
                for entry, size, digest in records))
            data = canonical_bytes(collection.model_dump(mode="json"))
            budget.require_disk(stage, len(data))
            manifest = os.open(MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage)
            try:
                _write(manifest, data)
                budget.written += len(data)
                os.fsync(manifest)
                os.fchmod(manifest, 0o400)
            finally:
                os.close(manifest)
            os.fsync(files)
            os.fsync(stage)
            self._verify_fd(stage, collection.digest, budget)
            chain.check()
            _same_directory(chain.fd, "objects", objects)
            _same_directory(chain.fd, "staging", staging)
            budget.check()
            _lock_publication(objects, budget)
            owned.callback(fcntl.flock, objects, fcntl.LOCK_UN)
            _same_directory(staging, name, stage)
            budget.check()
            # The complete tree moves in one exclusive operation. A crash
            # cannot publish an empty or partly populated digest claim.
            try:
                _publish_directory(staging, name, objects, collection.digest)
                moved = True
            except FileExistsError:
                existing = _open_directory(objects, collection.digest)
                try:
                    if self._verify_fd(existing, collection.digest, budget) != collection:
                        raise ArtifactError("artifact_object_collision")
                    chain.check()
                    _same_directory(chain.fd, "objects", objects)
                    _same_directory(chain.fd, "staging", staging)
                    _same_directory(objects, collection.digest, existing)
                    budget.check()
                finally:
                    os.close(existing)
                return collection
            self._verify_fd(stage, collection.digest, budget)
            chain.check()
            _same_directory(chain.fd, "objects", objects)
            _same_directory(chain.fd, "staging", staging)
            _same_directory(objects, collection.digest, stage)
            budget.check()
            os.fsync(objects)
            os.fsync(staging)
            budget.check()
            return collection
        finally:
            # Publication commits immutable cache bytes, not execution. Never
            # dismantle a visible object: another caller may already hold its
            # verified identity. Also cover a native move that completed but
            # raised before Python recorded ``moved``.
            try:
                if not moved and collection is not None:
                    try:
                        current = os.stat(collection.digest, dir_fd=objects, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        moved = _directory_identity(current) == _directory_identity(os.fstat(stage))
                if not moved:
                    _same_directory(staging, name, stage)
            except (OSError, ArtifactError):
                raise ArtifactError("artifact_capture_reconciliation_required") from None
            # No path-based recursive deletion: cleanup only entries under our
            # held stage descriptors, never follow a replacement/link outward.
            if files >= 0:
                if not moved:
                    for entry in request.entries:
                        try:
                            os.unlink(entry.name, dir_fd=files)
                        except FileNotFoundError:
                            pass
            if not moved:
                for relative in (MANIFEST_NAME, "files"):
                    try:
                        (os.rmdir if relative == "files" else os.unlink)(relative, dir_fd=stage)
                    except FileNotFoundError:
                        pass
                _same_directory(staging, name, stage)
                os.rmdir(name, dir_fd=staging)
                os.fsync(staging)

    def _verify_fd(self, directory: int, digest: str, budget: _Budget) -> CapturedArtifactCollectionV1:
        _private_directory(directory)
        _directory_names(directory, {MANIFEST_NAME, "files"})
        manifest = os.open(MANIFEST_NAME, _OPEN, dir_fd=directory)
        files = -1
        held: list[tuple[str, int, os.stat_result]] = []
        try:
            manifest_before = _regular(manifest, readonly=True)
            if manifest_before.st_size > MAX_CONTRACT_BYTES:
                raise ArtifactError("artifact_manifest_invalid")
            data = bytearray()
            while len(data) <= manifest_before.st_size:
                budget.check()
                chunk = os.read(manifest, min(COPY_CHUNK_BYTES, manifest_before.st_size - len(data) + 1))
                if not chunk:
                    break
                data.extend(chunk)
            _same_file(directory, MANIFEST_NAME, manifest, manifest_before)
            collection = _parse_collection(bytes(data))
            if bytes(data) != canonical_bytes(collection.model_dump(mode="json")) or collection.digest != digest:
                raise ArtifactError("artifact_identity_mismatch")
            files = _open_directory(directory, "files")
            _directory_names(files, {entry.name for entry in collection.entries})
            for entry in collection.entries:
                fd = os.open(entry.name, _OPEN, dir_fd=files)
                try:
                    before = _regular(fd, readonly=True)
                except BaseException:
                    os.close(fd)
                    raise
                held.append((entry.name, fd, before))
                before = held[-1][2]
                if before.st_size != entry.size or _hash_fd(fd, entry.size, budget, license_text=entry.role == "license") != entry.sha256:
                    raise ArtifactError("artifact_hash_mismatch")
                _same_file(files, entry.name, fd, before)
            current = os.stat("files", dir_fd=directory, follow_symlinks=False)
            if _directory_identity(current) != _directory_identity(os.fstat(files)):
                raise ArtifactError("artifact_ancestor_changed")
            _directory_names(directory, {MANIFEST_NAME, "files"})
            _directory_names(files, {entry.name for entry in collection.entries})
            _same_file(directory, MANIFEST_NAME, manifest, manifest_before)
            for name, fd, before in held:
                _same_file(files, name, fd, before)
        finally:
            for _, fd, _ in held:
                os.close(fd)
            if files >= 0:
                os.close(files)
            os.close(manifest)
        return collection

    def verify(self, digest: str, *, cancel_event: threading.Event | None = None,
               deadline: float | None = None) -> CapturedArtifactCollectionV1:
        with self._verified_object(digest, cancel_event, deadline) as (_, collection, _):
            return collection

    @contextlib.contextmanager
    def _verified_object(self, digest, cancel_event, deadline):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ArtifactError("artifact_identity_invalid")
        budget = _Budget(MAX_ARTIFACT_BYTES + MAX_CONTRACT_BYTES, cancel_event, deadline)
        try:
            with self._root() as chain:
                objects = _open_directory(chain.fd, "objects")
                try:
                    directory = _open_directory(objects, digest)
                    try:
                        collection = self._verify_fd(directory, digest, budget)
                        chain.check()
                        _same_directory(chain.fd, "objects", objects)
                        _same_directory(objects, digest, directory)
                        budget.check()
                        yield directory, collection, budget
                        chain.check()
                        _same_directory(chain.fd, "objects", objects)
                        _same_directory(objects, digest, directory)
                        budget.check()
                    finally:
                        os.close(directory)
                finally:
                    os.close(objects)
        except ArtifactError:
            raise
        except OSError:
            raise ArtifactError("artifact_verification_failed") from None

    @contextlib.contextmanager
    def open_artifact(self, digest: str, name: str, *, cancel_event: threading.Event | None = None,
                      deadline: float | None = None) -> Iterator[int]:
        """Yield a verified read-only FD to captured bytes, never an intake path.

        The caller streams/copies using this FD, keeps this context open through
        the copy, and discards its output if the final identity check fails.
        No descriptor is transferred beyond the context's ownership lifetime.
        """
        with self._verified_object(digest, cancel_event, deadline) as (directory, collection, budget):
            entry = next((item for item in collection.entries if item.name == name), None)
            if entry is None:
                raise ArtifactError("artifact_name_unknown")
            files = _open_directory(directory, "files")
            try:
                fd = os.open(entry.name, _OPEN, dir_fd=files)
                try:
                    before = _regular(fd, readonly=True)
                    if before.st_size != entry.size or _hash_fd(fd, entry.size, budget, license_text=entry.role == "license") != entry.sha256:
                        raise ArtifactError("artifact_hash_mismatch")
                    _same_file(files, entry.name, fd, before)
                    _same_directory(directory, "files", files)
                    os.lseek(fd, 0, os.SEEK_SET)
                    budget.check()
                    yield fd
                    if _hash_fd(fd, entry.size, budget, license_text=entry.role == "license") != entry.sha256:
                        raise ArtifactError("artifact_hash_mismatch")
                    _same_file(files, entry.name, fd, before)
                    _same_directory(directory, "files", files)
                    budget.check()
                finally:
                    os.close(fd)
            finally:
                os.close(files)
