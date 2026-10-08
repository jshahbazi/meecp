"""Seal an existing custom package without executing code or installing it.

Use bundled Python with -I -S. Empty directories are rejected, never removed.
This helper records bytes and a declared primary license; host validation and
MCP fixture checks still decide whether the package can proceed to review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys

MANIFEST = "meecp.package.json"
LOCK = "meecp.lock.json"
MAX_ARTIFACTS = 20_000
MAX_PACKAGE_BYTES = 64 * 1024 * 1024 * 1024
ATTRS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")


class LockError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or PurePosixPath(value).is_absolute() or str(PurePosixPath(value)) != value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise LockError("pi_lock_unsafe_path")
    return value


def _identity(details):
    return tuple(getattr(details, key) for key in ATTRS)


def _safe(details, *, directory=False):
    if (details.st_mode & 0o7022 or not (stat.S_ISDIR(details.st_mode) if directory
            else stat.S_ISREG(details.st_mode) and details.st_nlink == 1)):
        raise LockError("pi_lock_unsafe_file")


def _tree(root):
    files, directories, identities, pending = {}, {"."}, set(), [root]
    _safe(root.lstat(), directory=True)
    while pending:
        for path in sorted(pending.pop().iterdir()):
            name = _path(path.relative_to(root).as_posix())
            details = path.lstat()
            if stat.S_ISDIR(details.st_mode):
                _safe(details, directory=True)
                directories.add(name)
                pending.append(path)
            else:
                _safe(details)
                identity = (details.st_dev, details.st_ino)
                if identity in identities:
                    raise LockError("pi_lock_unsafe_file")
                identities.add(identity)
                files[name] = _identity(details)
            if len(files) + len(directories) > MAX_ARTIFACTS * 2 + 2:
                raise LockError("pi_lock_size_limit")
    expected = {"."}
    for name in files:
        expected.update(str(parent) for parent in PurePosixPath(name).parents)
    if directories != expected:
        raise LockError("pi_lock_empty_directory")
    return files


def _read(path, expected=None, *, limit=None, collect=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        _safe(before)
        if expected is not None and _identity(before) != expected:
            raise LockError("pi_lock_changed")
        if limit is not None and before.st_size > limit:
            raise LockError("pi_lock_size_limit")
        digest, count, contents = hashlib.sha256(), 0, []
        while block := stream.read(1024 * 1024):
            count += len(block)
            if count > before.st_size or limit is not None and count > limit:
                raise LockError("pi_lock_changed")
            digest.update(block)
            if collect:
                contents.append(block)
        if (count != before.st_size or _identity(os.fstat(stream.fileno())) != _identity(before)
                or _identity(path.lstat()) != _identity(before)):
            raise LockError("pi_lock_changed")
    return digest.hexdigest(), count, b"".join(contents)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LockError("pi_lock_metadata_invalid")
        result[key] = value
    return result


def _invalid_constant(_):
    raise LockError("pi_lock_metadata_invalid")


def _json(data):
    return json.loads(data, object_pairs_hook=_object, parse_constant=_invalid_constant)


def _manifest(data):
    value = _json(data)
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] not in {1, 2}
            or not isinstance(value.get("package_id"), str)
            or len(value["package_id"]) > 128
            or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", value["package_id"])
            or not isinstance(value.get("version"), str) or len(value["version"]) > 256
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", value["version"])
            or not isinstance(value.get("licenses"), list) or not 1 <= len(value["licenses"]) <= 128):
        raise LockError("pi_lock_manifest_invalid")
    for name in value["licenses"]:
        _path(name)
    if len(set(value["licenses"])) != len(value["licenses"]):
        raise LockError("pi_lock_license_required")
    return value


def seal(package, *, primary_license=None, toolchain_metadata=None):
    """Write only the lock; reject unsafe trees and preserve existing file modes."""
    root = Path(os.path.abspath(package))
    if root.resolve() != root or not root.is_dir():
        raise LockError("pi_lock_unsafe_path")
    files = _tree(root)
    if MANIFEST not in files:
        raise LockError("pi_lock_manifest_required")
    manifest_digest, _, content = _read(root / MANIFEST, files[MANIFEST], limit=1024 * 1024, collect=True)
    manifest = _manifest(content)
    primary = manifest["licenses"][0] if primary_license is None else _path(primary_license)
    if primary not in manifest["licenses"]:
        raise LockError("pi_lock_license_required")
    for name in manifest["licenses"]:
        if name not in files or name in {MANIFEST, LOCK} or files[name][4] == 0:
            raise LockError("pi_lock_license_required")
    toolchain = "not-recorded"
    if toolchain_metadata is not None:
        metadata = Path(os.path.abspath(toolchain_metadata))
        if metadata.resolve() != metadata:
            raise LockError("pi_lock_unsafe_path")
        digest, _, data = _read(metadata, limit=65536, collect=True)
        if not isinstance(_json(data), dict):
            raise LockError("pi_lock_metadata_invalid")
        toolchain = "recorded-metadata:sha256:" + digest
    artifacts, total = [], 0
    for name, identity in sorted(files.items()):
        if name in {MANIFEST, LOCK}:
            continue
        digest, size, _ = _read(root / name, identity, limit=MAX_PACKAGE_BYTES - total)
        total += size
        if total > MAX_PACKAGE_BYTES or len(artifacts) >= MAX_ARTIFACTS:
            raise LockError("pi_lock_size_limit")
        artifacts.append({"path": name, "sha256": digest, "size": size,
            "executable": bool(identity[2] & 0o111), "source": "prepared:sha256:" + digest,
            "provenance": "Pi custom package; captured file content identity; publisher unverified.",
            "license": primary})
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
        "manifest_sha256": manifest_digest, "artifacts": artifacts,
        "build": {"recipe": "pi-setup-v1", "builder": "pi-sdk", "toolchain": toolchain}}
    data = (json.dumps(lock, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(data) > 16 * 1024 * 1024:
        raise LockError("pi_lock_size_limit")
    if _tree(root) != files:
        raise LockError("pi_lock_changed")
    temporary = root / (".pi-lock-" + secrets.token_hex(16))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / LOCK)
    finally:
        temporary.unlink(missing_ok=True)
    return lock


class _Parser(argparse.ArgumentParser):
    def error(self, _):
        raise LockError("pi_lock_arguments_invalid")


def main(argv=None):
    try:
        parser = _Parser(add_help=False)
        parser.add_argument("package")
        parser.add_argument("--primary-license")
        parser.add_argument("--toolchain-metadata")
        args = parser.parse_args(argv)
        seal(args.package, primary_license=args.primary_license, toolchain_metadata=args.toolchain_metadata)
    except LockError as error:
        result = {"success": False, "code": error.code}
    except (OSError, ValueError, TypeError, RecursionError):
        result = {"success": False, "code": "pi_lock_failed"}
    else:
        result = {"success": True, "code": "pi_lock_ready"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
