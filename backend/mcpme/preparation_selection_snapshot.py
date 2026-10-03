"""Metadata continuity around exact directory-source byte validation.

No bytes are read here. The trusted selection worker takes this baseline before
its full source validation and compares it afterwards; the transaction compares
that same baseline before publishing. It never adopts a post-await baseline.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .imports import MAX_SCANNED_ENTRIES, MAX_SOURCE_FILES, _relative
from .preparation_application_source import MAX_SOURCE_DEPTH, _identity, _open_directory
from .preparation_contracts import PreparationError


@dataclass(frozen=True)
class SelectionSnapshotMetadata:
    root: str
    ancestors: tuple
    entries: tuple


def selection_snapshot_metadata(draft):
    source = draft["source"]
    if source["kind"] not in {"local_folder", "document", "application_directory"}:
        # Existing model/capture families retain their separate typed source
        # capability validation. Agent adoption admits directory inputs only.
        return None
    descriptor = None
    try:
        root = Path(source["snapshot_path"])
        expected = tuple(_relative(item["path"]) for item in source["files"])
        if not 1 <= len(expected) <= MAX_SOURCE_FILES or expected != tuple(sorted(set(expected))):
            raise PreparationError("source_identity_mismatch")
        ancestors, entries, files = [], [], []
        count = 0
        def walk(directory, prefix, depth):
            nonlocal count
            before = os.fstat(directory)
            if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o700):
                raise PreparationError("source_identity_mismatch")
            entries.append((prefix, "directory", _identity(before)))
            names = []
            with os.scandir(directory) as children:
                for child in children:
                    count += 1
                    if count > MAX_SCANNED_ENTRIES:
                        raise PreparationError("source_identity_mismatch")
                    names.append(child.name)
            for name in sorted(names):
                relative = _relative(prefix + name)
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(current.st_mode):
                    if depth >= MAX_SOURCE_DEPTH:
                        raise PreparationError("source_identity_mismatch")
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        if _identity(current) != _identity(os.fstat(child)):
                            raise PreparationError("source_identity_mismatch")
                        walk(child, relative + "/", depth + 1)
                        if _identity(current) != _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                            raise PreparationError("source_identity_mismatch")
                    finally:
                        os.close(child)
                else:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        held = os.fstat(child)
                        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid() or held.st_nlink != 1
                                or stat.S_IMODE(held.st_mode) != 0o600
                                or _identity(current) != _identity(held)
                                or _identity(held) != _identity(os.stat(name, dir_fd=directory, follow_symlinks=False))):
                            raise PreparationError("source_identity_mismatch")
                        files.append(relative)
                        if len(files) > MAX_SOURCE_FILES:
                            raise PreparationError("source_identity_mismatch")
                        entries.append((relative, "file", _identity(held)))
                    finally:
                        os.close(child)
            if _identity(before) != _identity(os.fstat(directory)):
                raise PreparationError("source_identity_mismatch")
        descriptor = _open_directory(root, ancestors)
        walk(descriptor, "", 0)
        visible_ancestors = []
        visible = _open_directory(root, visible_ancestors)
        try:
            if (ancestors != visible_ancestors or _identity(os.fstat(descriptor)) != _identity(os.fstat(visible))
                    or tuple(sorted(files)) != expected):
                raise PreparationError("source_identity_mismatch")
        finally:
            os.close(visible)
        return SelectionSnapshotMetadata(str(root), tuple(ancestors), tuple(sorted(entries)))
    except (OSError, KeyError, TypeError, ValueError):
        raise PreparationError("source_identity_mismatch") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
