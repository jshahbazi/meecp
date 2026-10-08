"""Publish a pinned ZIP from an already sealed package without executing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from meecp.contracts import (LOCK_NAME, MANIFEST_NAME, MAX_LOCK_METADATA_BYTES, MAX_METADATA_BYTES,
                             PackageError, _read_file, validate_package)


def prepare(package_root: Path, output: Path) -> dict:
    package = validate_package(package_root.resolve())
    output = output.absolute()
    if output == package.root or package.root in output.parents:
        raise PackageError("invalid_archive_output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + "." + str(uuid.uuid4()) + ".partial")
    metadata = {MANIFEST_NAME: _read_file(package.root / MANIFEST_NAME, MAX_METADATA_BYTES),
                LOCK_NAME: _read_file(package.root / LOCK_NAME, MAX_LOCK_METADATA_BYTES)}
    if hashlib.sha256(metadata[MANIFEST_NAME] + b"\0" + metadata[LOCK_NAME]).hexdigest() != package.digest:
        raise PackageError("package_changed")
    artifacts = {item.path: item for item in package.lock.artifacts}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as archive_file:
            # Stored entries keep decompression and disk requirements predictable.
            with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for name in sorted(set(artifacts) | set(metadata)):
                    executable = name in artifacts and artifacts[name].executable
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | (0o700 if executable else 0o600)) << 16
                    if name in metadata:
                        archive.writestr(info, metadata[name])
                        continue
                    artifact = artifacts[name]
                    identity = hashlib.sha256()
                    total = 0
                    source_fd = os.open(package.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(source_fd, "rb") as source, archive.open(info, "w", force_zip64=True) as target:
                        before = os.fstat(source.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != artifact.size:
                            raise PackageError("package_changed")
                        while chunk := source.read(min(1024 * 1024, artifact.size - total + 1)):
                            total += len(chunk)
                            if total > artifact.size:
                                raise PackageError("package_changed")
                            identity.update(chunk)
                            target.write(chunk)
                        after = os.fstat(source.fileno())
                        attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
                        if (total != artifact.size or identity.hexdigest() != artifact.sha256
                                or any(getattr(before, key) != getattr(after, key) for key in attrs)):
                            raise PackageError("package_changed")
            archive_file.flush()
            os.fsync(archive_file.fileno())
        if validate_package(package.root).digest != package.digest:
            raise PackageError("package_changed")
        with temporary.open("rb") as source:
            sha256 = hashlib.file_digest(source, "sha256").hexdigest()
        size = temporary.stat().st_size
        # Atomic no-overwrite publication also protects an existing output link.
        os.link(temporary, output, follow_symlinks=False)
        temporary.unlink()
        parent = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return {"sha256": sha256, "size": size, "package_digest": package.digest, "archive": str(output)}
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare(args.package, args.output)
    except PackageError as error:
        print(json.dumps({"error": {"code": error.code}}))
        return 1
    except OSError:
        print(json.dumps({"error": {"code": "archive_write_failed"}}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
