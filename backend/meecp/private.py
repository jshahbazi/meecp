"""Owner-only state and verified copies of prepared packages."""
from __future__ import annotations

import json
import ctypes
import errno
import os
import platform
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any

from .contracts import (MAX_METADATA_BYTES, MAX_LOCK_METADATA_BYTES, PackageError,
                        ValidatedPackage, validate_package)


class PrivateStateError(ValueError):
    def __init__(self, code: str = "unsafe_private_state"):
        self.code = code
        super().__init__(code)


def _check_ancestors(path: Path) -> None:
    for ancestor in reversed(path.absolute().parents):
        try:
            if ancestor.is_symlink():
                raise PrivateStateError()
        except OSError:
            raise PrivateStateError() from None


def ensure_private_directory(path: Path) -> Path:
    path = Path(path).absolute()
    _check_ancestors(path)
    try:
        if not path.exists() and not path.is_symlink():
            pending: list[Path] = []
            ancestor = path
            while not ancestor.exists():
                pending.append(ancestor)
                ancestor = ancestor.parent
            for directory in reversed(pending):
                directory.mkdir(mode=0o700)
        details = path.lstat()
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o700):
            raise PrivateStateError()
        return path
    except OSError:
        raise PrivateStateError() from None


def check_private_file(path: Path) -> None:
    _check_ancestors(path)
    try:
        details = path.lstat()
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or details.st_nlink != 1 or stat.S_IMODE(details.st_mode) != 0o600):
            raise PrivateStateError()
    except OSError:
        raise PrivateStateError() from None


def read_private_json(path: Path, *, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    check_private_file(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            details = os.fstat(source.fileno())
            if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                    or stat.S_IMODE(details.st_mode) != 0o600 or details.st_nlink != 1):
                raise PrivateStateError()
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise PrivateStateError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise PrivateStateError()
                result[key] = value
            return result
        value = json.loads(data, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise PrivateStateError()
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise PrivateStateError() from None


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        check_private_file(path)
    temporary = path.with_name(path.name + "." + secrets.token_hex(12) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output, allow_nan=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, TypeError):
        raise PrivateStateError() from None
    finally:
        temporary.unlink(missing_ok=True)


def _clone_file(source_fd: int, target: Path, *, target_dir_fd: int | None = None) -> bool:
    """Attempt an independent APFS clone through held file descriptors."""
    if platform.system() != "Darwin":
        return False
    library = ctypes.CDLL(None, use_errno=True)
    clone = getattr(library, "fclonefileat", None)
    if clone is None:
        return False
    clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    clone.restype = ctypes.c_int
    parent = (os.dup(target_dir_fd) if target_dir_fd is not None else
              os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    try:
        # CLONE_NOOWNERCOPY | CLONE_NOFOLLOW_ANY. No hard links or ACL copying.
        if clone(source_fd, parent, os.fsencode(target.name), 0x0002 | 0x0008) == 0:
            return True
        code = ctypes.get_errno()
        if code in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOSYS}:
            return False
        raise OSError(code, "clone_failed")
    finally:
        os.close(parent)


def copy_regular_file(source_fd: int, target: Path, *, size: int, executable: bool = False,
                      check=lambda: None, target_dir_fd: int | None = None) -> None:
    """Copy a held regular source into a new owned file, cloning when available.

    The caller owns the destination tree and validates content hashes. A clone
    preserves independent inodes; writes to either file do not alter the other.
    """
    before = os.fstat(source_fd)
    if (type(size) is not int or size < 0 or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1 or before.st_size != size):
        raise PackageError("package_changed")
    check()
    cloned = (_clone_file(source_fd, target) if target_dir_fd is None else
              _clone_file(source_fd, target, target_dir_fd=target_dir_fd))
    flags = (os.O_RDONLY if cloned else os.O_WRONLY) | os.O_NOFOLLOW
    if not cloned:
        flags |= os.O_CREAT | os.O_EXCL
    output_fd = os.open(target if target_dir_fd is None else target.name, flags, 0o600, dir_fd=target_dir_fd)
    with os.fdopen(output_fd, "rb" if cloned else "wb") as output:
        if not cloned:
            os.lseek(source_fd, 0, os.SEEK_SET)
            copied = 0
            while chunk := os.read(source_fd, min(1024 * 1024, size - copied + 1)):
                check()
                copied += len(chunk)
                if copied > size:
                    raise PackageError("package_changed")
                output.write(chunk)
            if copied != size:
                raise PackageError("package_changed")
        check()
        output.flush()
        details = os.fstat(output.fileno())
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or details.st_uid != os.getuid() or details.st_size != size):
            raise PackageError("package_changed")
        os.fchmod(output.fileno(), 0o700 if executable else 0o600)
        os.fsync(output.fileno())
    after = os.fstat(source_fd)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise PackageError("package_changed")


def snapshot_package(package: ValidatedPackage | Path, destination: Path,
                     *, expected_digest: str | None = None) -> ValidatedPackage:
    """Copy into a private staged tree, revalidate, then publish without overwrite."""
    source = validate_package(package.root if isinstance(package, ValidatedPackage) else package)
    expected = expected_digest or (package.digest if isinstance(package, ValidatedPackage) else source.digest)
    if source.digest != expected:
        raise PackageError("package_digest_mismatch")
    destination = Path(destination).absolute()
    ensure_private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise PrivateStateError()
        existing = validate_package(destination)
        if existing.digest != expected:
            raise PackageError("package_digest_mismatch")
        _check_snapshot_permissions(destination)
        return existing
    staged = destination.with_name(destination.name + "." + secrets.token_hex(12) + ".tmp")
    limits = {artifact.path: artifact.size for artifact in source.lock.artifacts}
    limits.update({source.manifest_name: MAX_METADATA_BYTES, source.lock_name: MAX_LOCK_METADATA_BYTES})
    try:
        staged.mkdir(mode=0o700)
        for source_dir, directory_names, file_names in os.walk(source.root, followlinks=False):
            relative = Path(source_dir).relative_to(source.root)
            for name in directory_names:
                candidate = Path(source_dir) / name
                if candidate.is_symlink():
                    raise PackageError("package_changed")
                (staged / relative / name).mkdir(mode=0o700)
            for name in file_names:
                candidate = Path(source_dir) / name
                # Check again at open: copies must never dereference a replaced leaf symlink.
                descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as input_file:
                    details = os.fstat(input_file.fileno())
                    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                        raise PackageError("package_changed")
                    limit = limits.get(str(relative / name))
                    if limit is None or details.st_size > limit:
                        raise PackageError("package_changed")
                    target = staged / relative / name
                    copy_regular_file(input_file.fileno(), target, size=details.st_size,
                                      executable=bool(details.st_mode & 0o111))
        verified = validate_package(staged)
        if verified.digest != expected or validate_package(source.root).digest != expected:
            raise PackageError("package_digest_mismatch")
        os.rename(staged, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return validate_package(destination)
    except OSError:
        raise PackageError("package_snapshot_failed") from None
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def _check_snapshot_permissions(root: Path) -> None:
    ensure_private_directory(root)
    for directory, _, names in os.walk(root):
        ensure_private_directory(Path(directory))
        for name in names:
            details = (Path(directory) / name).lstat()
            expected_mode = 0o700 if details.st_mode & 0o111 else 0o600
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != expected_mode:
                raise PrivateStateError()


# Compatibility spelling for callers migrating the existing private-state helper.
atomic_private_json = write_private_json
