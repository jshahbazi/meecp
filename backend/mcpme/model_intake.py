"""Bounded, code-free JevK5 weight and relocated-engine artifact intake.

Binary artifacts have a separate source contract. Neither intake nor receipt
inspection loads a library, runs an engine, or parses tensor contents.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct

from .contracts import PackageError, package_relative_path
from .private import ensure_private_directory

PROFILE = "jevk5-llamacpp-v1"
ENGINE_BUILD = "11146-7fe450e19305b828c199d602c23a8337aaa1f03b"
MODEL = {"model_id": "JevK5-4B-v0.3-Q8_0", "file": "jevk5-4b-v0.3-Q8_0.gguf",
         "bytes": 4482402720, "sha256": "aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30",
         "revision": "ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a", "license": "Apache-2.0"}
MAX_WEIGHT_BYTES = 64 * 1024**3
MAX_ENGINE_BYTES = 2 * 1024**3
MAX_ENGINE_FILES = 256
MAX_METADATA_BYTES = 16 * 1024**2
_ATTRS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")


class ModelIntakeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_digest(records: list[dict]) -> str:
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _open(path: Path):
    """Anchor every directory component and the leaf with O_NOFOLLOW."""
    path = path.absolute()
    if path.resolve() != path:
        raise ModelIntakeError("unsafe_source_path")
    try:
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ModelIntakeError("unsafe_source_path") from None
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            os.close(descriptor)
            raise ModelIntakeError("unsafe_source_file")
        return descriptor, parent, before
    except OSError:
        os.close(parent)
        raise ModelIntakeError("unsafe_source_file") from None
    except BaseException:
        os.close(parent)
        raise


def copy_artifact(path: Path, target: Path | None, *, limit: int, role: str,
                  expected_sha256: str | None = None, expected_size: int | None = None) -> dict:
    """Stream bytes through a hash and recheck descriptor/path identities."""
    parent = None
    try:
        descriptor, parent, before = _open(path)
        with os.fdopen(descriptor, "rb") as stream:
            if before.st_size > limit:
                raise ModelIntakeError("artifact_size_exceeded")
            if expected_size is not None and before.st_size != expected_size:
                raise ModelIntakeError("unsupported_model_artifact" if role == "model-weights" else "engine_identity_mismatch")
            header = stream.read(24) if role == "model-weights" else b""
            if role == "model-weights":
                if len(header) != 24 or header[:4] != b"GGUF" or struct.unpack("<I", header[4:8])[0] != 3:
                    raise ModelIntakeError("unsupported_model_format")
                tensors, metadata = struct.unpack("<QQ", header[8:24])
                if not 1 <= tensors <= 1000000 or not 1 <= metadata <= 100000:
                    raise ModelIntakeError("invalid_model_metadata")
                stream.seek(0)
            output = None
            if target is not None:
                ensure_private_directory(target.parent)
                output = os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")
            try:
                digest, total = hashlib.sha256(), 0
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ModelIntakeError("artifact_size_exceeded")
                    digest.update(chunk)
                    if output:
                        output.write(chunk)
                after = os.fstat(stream.fileno())
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                visible_parent, opened_parent = path.parent.stat(), os.fstat(parent)
                if (any(getattr(before, attr) != getattr(after, attr) or getattr(after, attr) != getattr(current, attr)
                        for attr in _ATTRS) or total != before.st_size or path.resolve() != path
                        or (visible_parent.st_dev, visible_parent.st_ino) != (opened_parent.st_dev, opened_parent.st_ino)):
                    raise ModelIntakeError("source_changed")
                sha256 = digest.hexdigest()
                if expected_sha256 is not None and sha256 != expected_sha256:
                    raise ModelIntakeError("unsupported_model_artifact" if role == "model-weights" else "engine_identity_mismatch")
                if output:
                    # Original executable bits are declarations only; snapshots remain private.
                    os.fchmod(output.fileno(), 0o700 if before.st_mode & 0o111 else 0o600)
                    output.flush()
                    os.fsync(output.fileno())
                return {"sha256": sha256, "size": total, "role": role, "executable": bool(before.st_mode & 0o111)}
            finally:
                if output:
                    output.close()
    except OSError as error:
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise ModelIntakeError("insufficient_disk_space") from None
        raise ModelIntakeError("unsafe_source_file") from None
    finally:
        if parent is not None:
            os.close(parent)


def engine_records(engine: Path) -> tuple[Path, list[tuple[str, str, str | None]]]:
    """A build contains llama/, licenses/ and its complete relocation receipt."""
    engine = engine.absolute()
    if (engine / "Contents/Resources").is_dir():
        engine = engine / "Contents/Resources"
    if engine.resolve() != engine or not engine.is_dir():
        raise ModelIntakeError("engine_build_required")
    receipt = engine / "runtime-manifest.json"
    receipt_record = copy_artifact(receipt, None, limit=MAX_METADATA_BYTES, role="receipt")
    try:
        descriptor, parent, before = _open(receipt)
        try:
            with os.fdopen(descriptor, "rb") as stream:
                data = stream.read(MAX_METADATA_BYTES + 1)
            if len(data) > MAX_METADATA_BYTES or hashlib.sha256(data).hexdigest() != receipt_record["sha256"]:
                raise ModelIntakeError("source_changed")
        finally:
            os.close(parent)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        manifest = json.loads(data, object_pairs_hook=unique)
        entries = manifest["llama"]
        if (not isinstance(entries, dict) or not 1 <= len(entries) <= MAX_ENGINE_FILES
                or "llama/llama-server.real" not in entries):
            raise ValueError()
        records = [("runtime-manifest.json", "receipt", receipt_record["sha256"])]
        for relative, item in sorted(entries.items()):
            package_relative_path(relative)
            if (not relative.startswith("llama/") or not isinstance(item, dict)
                    or set(item) != {"sha256", "source_sha256"}
                    or any(not isinstance(item[key], str) or not re.fullmatch("[0-9a-f]{64}", item[key]) for key in item)):
                raise ValueError()
            if not re.fullmatch(r"llama/(?:llama-server(?:\.real)?|lib/[A-Za-z0-9_.-]+\.dylib|plugins/[A-Za-z0-9_.-]+\.so)", relative):
                raise ModelIntakeError("engine_build_unsupported")
            if relative == "llama/llama-server" and not (engine / relative).exists() and not (engine / relative).is_symlink():
                # Prepared JevK5 builds removed the retired fixed-name reaper
                # while retaining its original relocation provenance receipt.
                continue
            records.append((relative, "runtime-file", item["sha256"]))
        names = {relative for relative, _, _ in records}
        if ("llama/plugins/libggml-metal.so" not in names
                or not {"llama/lib/libggml-base.dylib", "llama/lib/libggml-base.0.dylib"}.intersection(names)):
            raise ModelIntakeError("engine_build_unsupported")
        licenses = engine / "licenses"
        if licenses.is_symlink() or not licenses.is_dir():
            raise ModelIntakeError("engine_license_required")
        scanned = 0
        for directory, dirs, files in os.walk(licenses, followlinks=False):
            scanned += len(dirs) + len(files)
            if scanned > MAX_ENGINE_FILES * 2 or len(Path(directory).relative_to(licenses).parts) > 8:
                raise ModelIntakeError("artifact_size_exceeded")
            if any((Path(directory) / value).is_symlink() for value in dirs):
                raise ModelIntakeError("unsafe_source_file")
            for filename in sorted(files):
                relative = str((Path(directory) / filename).relative_to(engine))
                package_relative_path(relative)
                from .imports import _excluded
                if _excluded(relative):
                    raise ModelIntakeError("source_file_excluded")
                records.append((relative, "license", None))
                if len(records) > MAX_ENGINE_FILES:
                    raise ModelIntakeError("artifact_size_exceeded")
        if not {"licenses/llama.cpp-LICENSE", "licenses/ggml-LICENSE"}.issubset({item[0] for item in records}):
            raise ModelIntakeError("engine_license_required")
        return engine, sorted(records)
    except ModelIntakeError:
        raise
    except (KeyError, TypeError, ValueError, PackageError, UnicodeError):
        raise ModelIntakeError("engine_receipt_invalid") from None


def snapshot_model(source: Path, destination: Path, engine: Path | None = None) -> dict:
    source = source.absolute()
    # Pins are compatibility requirements, not inferred from arbitrary GGUF metadata.
    if source.suffix.lower() != ".gguf":
        raise ModelIntakeError("unsupported_source")
    records, selections = [], [(source, "weights/" + MODEL["file"], "model-weights", MODEL["sha256"], MODEL["bytes"])]
    if engine is not None:
        engine_root, values = engine_records(engine)
        selections += [(engine_root / relative, "engine/" + relative, role, sha, None) for relative, role, sha in values]
    total_estimate, engine_bytes = 0, 0
    for path, _, role, _, _ in selections:
        descriptor, parent, before = _open(path)
        os.close(descriptor)
        os.close(parent)
        limit = MAX_WEIGHT_BYTES if role == "model-weights" else MAX_METADATA_BYTES if role in {"license", "receipt"} else MAX_ENGINE_BYTES
        if before.st_size > limit:
            raise ModelIntakeError("artifact_size_exceeded")
        if role == "runtime-file":
            engine_bytes += before.st_size
            if engine_bytes > MAX_ENGINE_BYTES:
                raise ModelIntakeError("artifact_size_exceeded")
        total_estimate += before.st_size
    if total_estimate > MAX_WEIGHT_BYTES + MAX_ENGINE_BYTES + MAX_METADATA_BYTES * MAX_ENGINE_FILES:
        raise ModelIntakeError("artifact_size_exceeded")
    ensure_private_directory(destination)
    if shutil.disk_usage(destination).free < total_estimate + MAX_METADATA_BYTES:
        raise ModelIntakeError("insufficient_disk_space")
    for path, relative, role, sha, size in selections:
        record = copy_artifact(path, destination / relative, limit=MAX_WEIGHT_BYTES if role == "model-weights" else MAX_ENGINE_BYTES,
                               role=role, expected_sha256=sha, expected_size=size)
        records.append({"path": relative, **record})
    records.sort(key=lambda item: item["path"])
    for directory, _, _ in os.walk(destination, topdown=False):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    digest = canonical_digest(records)
    return {"kind": "artifact_collection", "contract_version": 1, "locator": str(source),
            "snapshot_path": str(destination), "files": records, "digest": digest, "revision": "sha256:" + digest,
            "bytes": sum(item["size"] for item in records), "model": MODEL.copy(),
            "engine": {"profile": PROFILE, "build": ENGINE_BUILD} if engine is not None else None}
