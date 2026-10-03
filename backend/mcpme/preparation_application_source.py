"""Bounded, code-free application-directory intake with explicit binary identity.

Version two binds original executable modes and text/binary classification. The
snapshot itself stays private and nonexecutable; only a reviewed builder can
copy a selected executable into a package. Legacy text-source bytes and digests
are unchanged. Neither inspection nor capture invokes selected code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .imports import (
    ImportError as SourceError, MAX_SCANNED_ENTRIES, MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES, MAX_SOURCE_FILE_BYTES, _CREDENTIAL_PATTERN,
    _EXCLUDED_DIRECTORIES, _SOURCE_NAMES, _SOURCE_SUFFIXES, _excluded,
    _relative, _snapshot_source, _sync_directory,
)
from .private import ensure_private_directory

MAX_SOURCE_DEPTH = 64


@dataclass(frozen=True)
class ApplicationSnapshotFile:
    path: str
    sha256: str
    size: int
    executable: bool
    kind: str
    data: bytes = field(repr=False)


def _identity(details):
    return tuple(getattr(details, key) for key in (
        "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _ancestor_identity(details):
    # Sibling activity may change /tmp directory times. Ancestor substitution
    # and permission changes are detected by their own stable object identity.
    return (details.st_dev, details.st_ino, details.st_mode)


def _open_directory(path: Path, observations=None):
    if not path.is_absolute() or ".." in path.parts:
        raise SourceError("unsafe_source_path")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if observations is not None:
            observations.append(_ancestor_identity(os.fstat(descriptor)))
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if observations is not None:
                observations.append(_ancestor_identity(os.fstat(descriptor)))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_file(path: Path, *, snapshot=False, with_identity=False):
    """Hold the ancestor chain and reject blocking special files and substitutions."""
    parent = None
    try:
        ancestors = []
        parent = _open_directory(path.parent, ancestors)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or snapshot and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600)):
                raise SourceError("unsafe_source_file")
            if before.st_size > MAX_SOURCE_FILE_BYTES:
                raise SourceError("source_size_exceeded")
            data = stream.read(MAX_SOURCE_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if len(data) > MAX_SOURCE_FILE_BYTES or _identity(before) != _identity(after) or _identity(after) != _identity(named):
                raise SourceError("source_changed")
            visible_ancestors = []
            visible_parent = _open_directory(path.parent, visible_ancestors)
            try:
                if ancestors != visible_ancestors or _identity(os.fstat(parent)) != _identity(os.fstat(visible_parent)):
                    raise SourceError("source_changed")
            finally:
                os.close(visible_parent)
        if _CREDENTIAL_PATTERN.search(data):
            raise SourceError("source_credentials_detected")
        try:
            data.decode("utf-8")
            kind = "binary" if b"\0" in data else "text"
        except UnicodeError:
            kind = "binary"
        result = (data, bool(before.st_mode & 0o111), kind)
        return (*result, (_identity(before), tuple(ancestors))) if with_identity else result
    except OSError:
        raise SourceError("unsafe_source_file") from None
    finally:
        if parent is not None:
            os.close(parent)


def _scan(root: Path, *, private=False):
    """Enumerate through held directory FDs; no unknown regular payload is skipped."""
    found, scanned = [], 0
    def walk(descriptor, prefix, depth):
        nonlocal scanned
        before = os.fstat(descriptor)
        if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o700):
            raise SourceError("unsafe_source_path")
        names = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCANNED_ENTRIES:
                    raise SourceError("source_size_exceeded")
                names.append(entry.name)
        for name in sorted(names):
            relative = prefix + name
            if not private and (_excluded(relative) or name.lower() in _EXCLUDED_DIRECTORIES):
                continue
            details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                if depth >= MAX_SOURCE_DEPTH:
                    raise SourceError("source_size_exceeded")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    if _identity(details) != _identity(os.fstat(child)):
                        raise SourceError("source_changed")
                    walk(child, relative + "/", depth + 1)
                    if _identity(os.fstat(child)) != _identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)):
                        raise SourceError("source_changed")
                finally:
                    os.close(child)
            elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                found.append(_relative(relative))
                if len(found) > MAX_SOURCE_FILES:
                    raise SourceError("source_size_exceeded")
            else:
                raise SourceError("unsafe_source_file")
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise SourceError("source_changed")
    descriptor = None
    try:
        descriptor = _open_directory(root)
        walk(descriptor, "", 0)
        return sorted(found)
    except OSError:
        raise SourceError("unsafe_source_file") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _digest(files, metadata):
    value = {"contract_version": 2, "kind": "application_directory", "files": files, "file_metadata": metadata}
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(b"mcpme-application-source-v2\0" + data).hexdigest()


def application_source_records(draft):
    """Validate complete v2 metadata without opening or executing source bytes."""
    try:
        source = draft["source"]
        files, metadata = source["files"], source["file_metadata"]
        if (source["kind"] != "application_directory" or type(source["contract_version"]) is not int
                or source["contract_version"] != 2 or type(files) is not list or type(metadata) is not list
                or not 1 <= len(files) <= MAX_SOURCE_FILES or len(metadata) != len(files)):
            raise SourceError("invalid_source_identity")
        paths, total = [], 0
        for item, flags in zip(files, metadata, strict=True):
            if (type(item) is not dict or set(item) != {"path", "sha256", "size"}
                    or type(flags) is not dict or set(flags) != {"path", "executable", "kind"}
                    or type(item["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    or type(item["size"]) is not int or not 0 <= item["size"] <= MAX_SOURCE_FILE_BYTES
                    or flags["path"] != item["path"] or type(flags["executable"]) is not bool
                    or flags["kind"] not in {"text", "binary"}):
                raise SourceError("invalid_source_identity")
            paths.append(_relative(item["path"]))
            total += item["size"]
        if (paths != sorted(set(paths)) or total > MAX_SOURCE_BYTES
                or type(source.get("bytes")) is not int or source["bytes"] != total):
            raise SourceError("invalid_source_identity")
        digest = _digest(files, metadata)
        if (source.get("digest"), source.get("revision")) != (digest, "sha256:" + digest):
            raise SourceError("source_identity_mismatch")
        return tuple(dict(item) for item in files)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError("invalid_source_identity") from None


def snapshot_application_directory(source: Path, destination: Path, files=None):
    """Capture automatic directory inputs; ordinary text selections retain v1."""
    source = Path(source).absolute()
    if not source.is_dir():
        return _snapshot_source(source, destination, files)
    if files is None:
        selected = _scan(source)
    else:
        if type(files) is not list or not 1 <= len(files) <= MAX_SOURCE_FILES:
            raise SourceError("invalid_source_selection")
        selected = sorted(_relative(item) for item in files)
        if len(selected) != len(set(selected)):
            raise SourceError("invalid_source_selection")
        if any(_excluded(item) for item in selected):
            raise SourceError("source_file_excluded")
        if any(len(Path(item).parts) - 1 > MAX_SOURCE_DEPTH for item in selected):
            raise SourceError("source_size_exceeded")
    if not selected:
        raise SourceError("supported_source_required")
    records, metadata, content, identities, total = [], [], {}, {}, 0
    for relative in selected:
        data, executable, kind, identity = _read_file(source / relative, with_identity=True)
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise SourceError("source_size_exceeded")
        content[relative] = data
        identities[relative] = identity
        records.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        metadata.append({"path": relative, "executable": executable, "kind": kind})
    if all(item["kind"] == "text" and (Path(item["path"]).suffix.lower() in _SOURCE_SUFFIXES
            or Path(item["path"]).name.lower() in _SOURCE_NAMES) for item in metadata):
        if files is None and _scan(source) != selected:
            raise SourceError("source_changed")
        return _snapshot_source(source, destination, selected)
    ensure_private_directory(destination)
    for relative, data in content.items():
        target = destination / relative
        ensure_private_directory(target.parent)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    if files is None and _scan(source) != selected:
        raise SourceError("source_changed")
    for flags in metadata:
        data, executable, kind, identity = _read_file(source / flags["path"], with_identity=True)
        if (data != content[flags["path"]] or executable != flags["executable"] or kind != flags["kind"]
                or identity != identities[flags["path"]]):
            raise SourceError("source_changed")
    for directory, _, _ in os.walk(destination, topdown=False):
        _sync_directory(Path(directory))
    digest = _digest(records, metadata)
    return {"kind": "application_directory", "contract_version": 2,
            "locator": str(source), "snapshot_path": str(destination), "files": records,
            "file_metadata": metadata, "digest": digest, "revision": "sha256:" + digest, "bytes": total}


def read_application_snapshot(draft) -> dict[str, ApplicationSnapshotFile]:
    """Recheck private full membership, classification and byte identities."""
    records = application_source_records(draft)
    try:
        root = Path(draft["source"]["snapshot_path"])
        if not root.is_absolute() or _scan(root, private=True) != [item["path"] for item in records]:
            raise SourceError("source_identity_mismatch")
        result = {}
        for item, flags in zip(records, draft["source"]["file_metadata"], strict=True):
            data, _, kind = _read_file(root / item["path"], snapshot=True)
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"] or kind != flags["kind"]:
                raise SourceError("source_identity_mismatch")
            result[item["path"]] = ApplicationSnapshotFile(**item, executable=flags["executable"], kind=kind, data=data)
        if _scan(root, private=True) != list(result):
            raise SourceError("source_identity_mismatch")
        for item in result.values():
            if _read_file(root / item.path, snapshot=True)[0] != item.data:
                raise SourceError("source_identity_mismatch")
        return result
    except (OSError, TypeError, KeyError):
        raise SourceError("unsafe_source_file") from None
