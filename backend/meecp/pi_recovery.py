"""Bounded file checkpoints for fresh-session setup recovery.

The caller admits disk work and joins this synchronous operation before
releasing its reservation. Checkpoints grant no process or session authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat

from .contracts import PackageError
from .preparation_artifacts import (
    ArtifactError, _DirectoryChain, _absolute_path, _directory_identity, _identity,
)
from .private import copy_regular_file

MAX_FILES = 32768
MAX_BYTES = 32 * 1024**3
MAX_FILE_BYTES = 16 * 1024**3
MAX_ENTRIES = 2 * MAX_FILES
MAX_DEPTH = 64
MAX_PATH_BYTES = 4096
EXCLUDED = frozenset({"source", "resources", "tools", ".home", ".pi-home", ".tmp", ".trust", "probe",
                      "setup.json", "preflight.json", "resources.json", "package.schema.json", "lock.schema.json"})
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK


class PiRecoveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class _Checkpoint:
    files: list[tuple[tuple[str, ...], os.stat_result]]
    directories: dict[tuple[str, ...], tuple[int, ...]]
    total: int = 0
    skipped: int = 0


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise PiRecoveryError("pi_canceled")


def _path(value):
    try:
        path = _absolute_path(str(value))
        if len(os.fsencode(path)) > MAX_PATH_BYTES or path == Path("/"):
            raise ValueError()
        return path
    except (ArtifactError, ValueError, TypeError, UnicodeError):
        raise PiRecoveryError("pi_resume_unavailable") from None


@contextmanager
def _root(path, *, destination=False):
    chain = None
    try:
        chain = _DirectoryChain(path)
        details = os.fstat(chain.fd)
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
                or destination and stat.S_IMODE(details.st_mode) != 0o700):
            chain.close()
            raise PiRecoveryError("pi_resume_unavailable")
    except (OSError, ArtifactError):
        if chain is not None:
            chain.close()
        raise PiRecoveryError("pi_resume_unavailable") from None
    try:
        yield chain
    finally:
        chain.close()


def _safe_name(name, relative):
    return (name not in {"", ".", ".."} and "\\" not in name
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name)
            and len(os.fsencode(name)) <= 255
            and len(os.fsencode("/".join(relative))) <= MAX_PATH_BYTES)


def _check_edges(edges):
    for parent, name, descriptor, expected in edges:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (_directory_identity(current) != expected
                or _directory_identity(os.fstat(descriptor)) != expected):
            raise PiRecoveryError("pi_source_changed")


def _scan(chain, cancel_event=None):
    result = _Checkpoint([], {})
    visited = 0

    def directory(descriptor, relative, edges):
        nonlocal visited
        _cancel(cancel_event)
        chain.check()
        _check_edges(edges)
        if len(relative) > MAX_DEPTH:
            raise PiRecoveryError("pi_resume_limit")
        # scandir keeps enumeration bounded even for an untrusted huge tree.
        with os.scandir(descriptor) as entries:
            for entry in entries:
                _cancel(cancel_event)
                chain.check()
                _check_edges(edges)
                name = entry.name
                if not relative and name in EXCLUDED:
                    continue
                visited += 1
                if visited > MAX_ENTRIES:
                    raise PiRecoveryError("pi_resume_limit")
                nested = (*relative, name)
                if not _safe_name(name, nested):
                    result.skipped += 1
                    continue
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if before.st_uid != os.getuid():
                    result.skipped += 1
                    continue
                if stat.S_ISDIR(before.st_mode):
                    if len(result.directories) >= MAX_FILES:
                        raise PiRecoveryError("pi_resume_limit")
                    child = os.open(name, _DIRECTORY, dir_fd=descriptor)
                    try:
                        expected = _directory_identity(before)
                        if _directory_identity(os.fstat(child)) != expected:
                            raise PiRecoveryError("pi_source_changed")
                        result.directories[nested] = expected
                        nested_edges = [*edges, (descriptor, name, child, expected)]
                        directory(child, nested, nested_edges)
                        _check_edges(nested_edges)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    result.total += before.st_size
                    if (not 0 <= before.st_size <= MAX_FILE_BYTES or result.total > MAX_BYTES
                            or len(result.files) >= MAX_FILES):
                        raise PiRecoveryError("pi_resume_limit")
                    result.files.append((nested, before))
                else:
                    result.skipped += 1
        chain.check()
        _check_edges(edges)

    directory(chain.fd, (), [])
    return result


def estimate_workspace_bytes(source: Path) -> int:
    """Return retained regular-file bytes without reading file contents."""
    try:
        with _root(_path(source)) as chain:
            return _scan(chain).total
    except (OSError, ArtifactError):
        raise PiRecoveryError("pi_source_changed") from None


@contextmanager
def _relative_directory(root, parts, *, expected=None):
    """Open each component relative to held parents, creating private outputs."""
    descriptors, edges = [], []
    parent = root.fd
    try:
        for index, name in enumerate(parts):
            root.check()
            _check_edges(edges)
            if expected is None:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(name, _DIRECTORY, dir_fd=parent)
            descriptors.append(child)
            details = os.fstat(child)
            identity = _directory_identity(details)
            if expected is not None:
                if identity != expected[parts[:index + 1]]:
                    raise PiRecoveryError("pi_source_changed")
            elif details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700:
                raise PiRecoveryError("pi_resume_unavailable")
            edges.append((parent, name, child, identity))
            parent = child
        _check_edges(edges)
        yield parent, lambda: _check_edges(edges)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _free_bytes(descriptor):
    details = os.fstatvfs(descriptor)
    return details.f_bavail * details.f_frsize


def restore_workspace(source: Path, destination: Path, *, cancel_event=None,
                      disk_ceiling_bytes: int):
    """Copy retained files into an existing owned private workspace.

    Protected source/resources/session roots are rematerialized by the caller.
    Unsafe generated entries are skipped; changed pinned identities abort.
    """
    source, destination = _path(source), _path(destination)
    if (source == destination or source in destination.parents or destination in source.parents
            or type(disk_ceiling_bytes) is not int or disk_ceiling_bytes < 0):
        raise PiRecoveryError("pi_resume_unavailable")
    _cancel(cancel_event)
    try:
        with _root(source) as incoming, _root(destination, destination=True) as outgoing:
            checkpoint = _scan(incoming, cancel_event)
            if checkpoint.total > disk_ceiling_bytes or checkpoint.total > _free_bytes(outgoing.fd):
                raise PiRecoveryError("pi_resume_limit")
            written = 0
            for relative, before in checkpoint.files:
                _cancel(cancel_event)
                with (_relative_directory(incoming, relative[:-1], expected=checkpoint.directories) as selected,
                      _relative_directory(outgoing, relative[:-1]) as target):
                    parent, source_check = selected
                    target_parent, target_check = target
                    name = relative[-1]
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    created = False
                    try:
                        try:
                            os.stat(name, dir_fd=target_parent, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise PiRecoveryError("pi_resume_unavailable")

                        def check(source_check=source_check, target_check=target_check,
                                  descriptor=descriptor, before=before, name=name, parent=parent, written=written):
                            _cancel(cancel_event)
                            incoming.check()
                            outgoing.check()
                            source_check()
                            target_check()
                            if (_identity(os.fstat(descriptor)) != _identity(before)
                                    or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(before)):
                                raise PiRecoveryError("pi_source_changed")
                            if written + before.st_size > disk_ceiling_bytes:
                                raise PiRecoveryError("pi_resume_limit")

                        check()
                        if before.st_size > _free_bytes(target_parent):
                            raise PiRecoveryError("pi_resume_limit")
                        created = True
                        copy_regular_file(descriptor, Path(name), size=before.st_size,
                                          executable=bool(before.st_mode & 0o111), check=check,
                                          target_dir_fd=target_parent)
                        check()
                        written += before.st_size
                    except BaseException:
                        if created:
                            # unlink is relative to the pinned directory and
                            # cannot follow a renamed ancestor or a leaf link.
                            try:
                                os.unlink(name, dir_fd=target_parent)
                            except FileNotFoundError:
                                pass
                        raise
                    finally:
                        os.close(descriptor)
            _cancel(cancel_event)
            incoming.check()
            outgoing.check()
            return {"files": len(checkpoint.files), "bytes": written, "skipped_unsafe": checkpoint.skipped}
    except OSError as error:
        code = "pi_resume_limit" if error.errno in {errno.ENOSPC, errno.EDQUOT} else "pi_source_changed"
        raise PiRecoveryError(code) from None
    except (ArtifactError, PackageError):
        raise PiRecoveryError("pi_source_changed") from None
