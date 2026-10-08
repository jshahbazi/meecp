"""Trusted builder adapter for complete application-directory snapshot copies.

Published preparation templates retain their original text-source identity.
The host explicitly selects this adapter for the independently validated v2
contract; unknown payloads and binary assets remain part of the source closure.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .imports import ImportError as SourceError
from .preparation_application_source import (
    _open_directory, application_source_records, read_application_snapshot,
)
from .preparation_contracts import PreparationError


def _private_directory(descriptor):
    details = os.fstat(descriptor)
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise PreparationError("private_preparation_workspace_required")
    return details.st_dev, details.st_ino, details.st_mode


def _write_file(root_descriptor, relative, data):
    directory = os.dup(root_descriptor)
    try:
        pieces = relative.split("/")
        for piece in pieces[:-1]:
            try:
                os.mkdir(piece, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            _private_directory(directory)
        descriptor = os.open(pieces[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | os.O_NONBLOCK, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def copy_application_snapshot(export, destination: Path, check_canceled=None):
    """Copy verified v2 bytes through held FDs, with no selected-code execution."""
    check = check_canceled if check_canceled is not None else lambda: None
    root_descriptor = None
    try:
        check()
        records = application_source_records(export)
        content = read_application_snapshot(export, check)
        destination = Path(destination)
        if not destination.is_absolute() or ".." in destination.parts:
            raise PreparationError("private_preparation_workspace_required")
        parent = _open_directory(destination.parent)
        try:
            _private_directory(parent)
            os.mkdir(destination.name, mode=0o700, dir_fd=parent)
            root_descriptor = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent)
        finally:
            os.close(parent)
        identity = _private_directory(root_descriptor)
        for record in records:
            check()
            _write_file(root_descriptor, record["path"], content[record["path"]].data)
        check()
        if read_application_snapshot(export, check) != content:
            raise PreparationError("source_identity_mismatch")
        copied = {"source": {**export["source"], "snapshot_path": str(destination)}}
        if read_application_snapshot(copied, check) != content:
            raise PreparationError("source_identity_mismatch")
        visible = _open_directory(destination)
        try:
            if _private_directory(visible) != identity or _private_directory(root_descriptor) != identity:
                raise PreparationError("private_preparation_workspace_required")
        finally:
            os.close(visible)
        os.fsync(root_descriptor)
        check()
        return [dict(record) for record in records]
    except SourceError as failure:
        raise PreparationError(failure.code) from None
    except OSError:
        raise PreparationError("unsafe_source_file") from None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
