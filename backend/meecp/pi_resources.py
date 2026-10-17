"""Durable, code-free Pi inputs with exact draft binding and owned materialization.

Resources are independent of text-source limits and agent conversations. They
grant no execution or installation authority. Unlicensed acquired bytes remain
explicitly unverified inputs until their package's license contract is checked.
"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
import uuid

from .contracts import PackageError
from .downloads import DownloadError, normalize_spec
from .preparation_artifacts import (
    ArtifactError, MAX_ARTIFACT_BYTES, MAX_ARTIFACT_FILE_BYTES, _DirectoryChain,
    _absolute_path, _contains_credential, _identity, _publish_directory, _safe_source_name,
)
from .private import copy_regular_file, ensure_private_directory, read_private_json, write_private_json
from .processes import _await_ownership

MAX_RESOURCES = 64
MAX_RESOURCE_FILES = 60000
MAX_METADATA_BYTES = 16 * 1024**2
MAX_RETAINED_BYTES = 64 * 1024**3
HEADROOM_BYTES = 1024 * 1024
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_HASH = re.compile(r"[0-9a-f]{64}")
_ROLES = {"model-weights", "runtime-file", "validation", "license"}
_KINDS = {"file", "tree", "archive"}
_FORMATS = {"onnx", "gguf", "safetensors", "binary", "text", "json", "png", "jpeg", "npy", "archive"}


class PiResourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _binding(import_id, revision, source_revision):
    try:
        if (not isinstance(import_id, str) or str(uuid.UUID(import_id)) != import_id
                or type(revision) is not int or not 1 <= revision <= 2**31 - 1
                or not isinstance(source_revision, str) or not 1 <= len(source_revision) <= 256
                or any(ord(c) < 33 for c in source_revision)):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PiResourceError("pi_resource_binding_invalid") from None
    return {"import_id": import_id, "revision": revision, "source_revision": source_revision}


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value
            or any(ord(c) < 32 for c in value)):
        raise PiResourceError("pi_resource_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise PiResourceError("pi_resource_path_invalid")
    return value


def _source_path(value, *, archive=False):
    if not isinstance(value, str):
        raise PiResourceError("pi_resource_path_invalid")
    try:
        path = _absolute_path(value)
        _safe_source_name(value)
    except ArtifactError as error:
        if not archive or error.code != "artifact_archive_disallowed":
            raise PiResourceError(error.code) from None
        path = _absolute_path(value)
    return path


def _selection(value):
    allowed = {"name", "kind", "path", "role", "format", "licenses", "expected_sha256", "expected_size", "origin"}
    if (not isinstance(value, dict) or set(value) - allowed or not {"name", "path"} <= set(value)
            or not isinstance(value["name"], str) or not _NAME.fullmatch(value["name"])):
        raise PiResourceError("pi_resource_request_invalid")
    result = {"kind": "file", "role": "runtime-file", "format": "binary", "licenses": [], **value}
    if (not isinstance(result["kind"], str) or result["kind"] not in _KINDS
            or not isinstance(result["role"], str) or result["role"] not in _ROLES
            or not isinstance(result["format"], str) or result["format"] not in _FORMATS
            or not isinstance(result["licenses"], list) or len(result["licenses"]) > 64
            or len(set(map(str, result["licenses"]))) != len(result["licenses"])):
        raise PiResourceError("pi_resource_request_invalid")
    result["path"] = str(_source_path(result["path"], archive=result["kind"] == "archive"))
    for license_path in result["licenses"]:
        _source_path(license_path)
    if ("expected_sha256" in result and (not isinstance(result["expected_sha256"], str)
            or not _HASH.fullmatch(result["expected_sha256"]))):
        raise PiResourceError("pi_resource_request_invalid")
    if ("expected_size" in result and (type(result["expected_size"]) is not int
            or not 1 <= result["expected_size"] <= MAX_ARTIFACT_BYTES)):
        raise PiResourceError("pi_resource_request_invalid")
    if result["kind"] == "tree" and "expected_sha256" in result:
        raise PiResourceError("pi_resource_request_invalid")
    origin = result.get("origin", {"kind": "local-selection", "publisher_trust": "publisher_unverified"})
    if not isinstance(origin, dict):
        raise PiResourceError("pi_resource_origin_invalid")
    if not isinstance(origin.get("kind"), str):
        raise PiResourceError("pi_resource_origin_invalid")
    if origin.get("kind") in {"download", "https"} and set(origin) <= {"kind", "url", "publisher_trust"}:
        try:
            url = normalize_spec({"url": origin["url"], "sha256": "0" * 64, "size": 1})["url"]
        except (DownloadError, KeyError):
            raise PiResourceError("pi_resource_origin_invalid") from None
        origin = {"kind": "download", "url": url, "publisher_trust": "publisher_unverified"}
    elif origin.get("kind") in {"local-selection", "prepared"} and set(origin) <= {"kind", "publisher_trust"}:
        origin = {"kind": origin["kind"], "publisher_trust": "publisher_unverified"}
    else:
        raise PiResourceError("pi_resource_origin_invalid")
    result["origin"] = origin
    return result


class _Budget:
    def __init__(self, ceiling, cancel_event, deadline):
        if (type(ceiling) is not int or not 1 <= ceiling <= 2 * MAX_ARTIFACT_BYTES + MAX_METADATA_BYTES
                or cancel_event is not None and not isinstance(cancel_event, threading.Event)
                or deadline is not None and (type(deadline) not in {int, float} or not 0 < deadline < float("inf"))):
            raise PiResourceError("pi_resource_budget_invalid")
        self.ceiling, self.cancel, self.deadline, self.written = ceiling, cancel_event, deadline, 0

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise PiResourceError("pi_resource_canceled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise PiResourceError("pi_resource_deadline_exceeded")

    def require(self, path, size):
        self.check()
        if self.written + size + HEADROOM_BYTES > self.ceiling:
            raise PiResourceError("pi_resource_disk_limit")
        if size + HEADROOM_BYTES > shutil.disk_usage(path).free:
            raise PiResourceError("pi_resource_disk_unavailable")


def _scan(path, *, tree=False, check=lambda: None):
    """Pin source ancestors while discovering regular files without reading bytes."""
    result, total = [], 0
    def file(parent, name, relative):
        nonlocal total
        check()
        # A selected tree is an opaque input collection. Model formats such as
        # NPZ and bundled runtime wheels remain ordinary files here; capture
        # never inspects or expands an archive.
        _source_path(str(path / relative) if tree else str(path), archive=True)
        details = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or not 0 <= details.st_size <= MAX_ARTIFACT_FILE_BYTES):
            raise PiResourceError("pi_resource_file_unsafe")
        result.append((relative, _identity(details), details.st_size, bool(details.st_mode & 0o111)))
        total += details.st_size
        if len(result) > MAX_RESOURCE_FILES or total > MAX_ARTIFACT_BYTES:
            raise PiResourceError("pi_resource_size_limit")
    def directory(fd, relative, depth):
        if depth > 64:
            raise PiResourceError("pi_resource_size_limit")
        check()
        for name in sorted(os.listdir(fd)):
            nested = (PurePosixPath(relative) / name).as_posix() if relative else name
            _relative(nested)
            details = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    directory(child, nested, depth + 1)
                    if _identity(details) != _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                        raise PiResourceError("pi_resource_source_changed")
                finally:
                    os.close(child)
            else:
                file(fd, name, nested)
    try:
        with _DirectoryChain(path if tree else path.parent) as chain:
            if tree:
                directory(chain.fd, "", 0)
            else:
                # Archive files are not interpreted during capture.
                details = os.stat(path.name, dir_fd=chain.fd, follow_symlinks=False)
                if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                        or not 0 <= details.st_size <= MAX_ARTIFACT_FILE_BYTES):
                    raise PiResourceError("pi_resource_file_unsafe")
                result.append((path.name, _identity(details), details.st_size, bool(details.st_mode & 0o111)))
            chain.check()
    except (OSError, ArtifactError):
        raise PiResourceError("pi_resource_file_unsafe") from None
    if not result:
        raise PiResourceError("pi_resource_empty")
    return result


def _hash_file(fd, size, check, *, text=False):
    os.lseek(fd, 0, os.SEEK_SET)
    digest, count, tail = hashlib.sha256(), 0, b""
    decoder = codecs.getincrementaldecoder("utf-8")() if text else None
    while True:
        check()
        chunk = os.read(fd, min(1024 * 1024, size - count + 1))
        if not chunk:
            break
        count += len(chunk)
        if count > size:
            raise PiResourceError("pi_resource_source_changed")
        if _contains_credential(tail + chunk):
            raise PiResourceError("artifact_credentials_disallowed")
        if decoder:
            try:
                decoder.decode(chunk)
            except UnicodeError:
                raise PiResourceError("artifact_license_format_required") from None
        digest.update(chunk)
        tail = chunk[-256:]
    if count != size:
        raise PiResourceError("pi_resource_source_changed")
    if decoder:
        try:
            decoder.decode(b"", final=True)
        except UnicodeError:
            raise PiResourceError("artifact_license_format_required") from None
    return digest.hexdigest()


def _digest(manifest):
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_METADATA_BYTES:
        raise PiResourceError("pi_resource_metadata_limit")
    return hashlib.sha256(b"pi-resource-v1\x00" + encoded).hexdigest()


def _read_metadata(path):
    size = path.lstat().st_size
    if size > MAX_METADATA_BYTES:
        raise PiResourceError("pi_resource_metadata_limit")
    # Avoid allocating the entire metadata ceiling for every tiny manifest.
    return read_private_json(path, max_bytes=max(4096, size))


class PiResourceStore:
    def __init__(self, root: Path):
        self.root = ensure_private_directory(root)
        self.objects = ensure_private_directory(self.root / "objects")
        self.staging = ensure_private_directory(self.root / "staging")
        self.bindings = ensure_private_directory(self.root / "bindings")
        self.lock = threading.RLock()

    def _catalog(self, binding, *, create=False):
        path = self.bindings / (binding["import_id"] + ".json")
        if not path.exists():
            if not create:
                return {"schema_version": 1, "binding": binding, "resources": []}
            return {"schema_version": 1, "binding": binding, "resources": []}
        value = _read_metadata(path)
        if (set(value) != {"schema_version", "binding", "resources"} or value["schema_version"] != 1
                or not isinstance(value["resources"], list) or len(value["resources"]) > MAX_RESOURCES
                or value["binding"] != binding):
            raise PiResourceError("pi_resource_binding_changed")
        for entry in value["resources"]:
            if (not isinstance(entry, dict) or set(entry) != {"resource_id", "name", "removed"}
                    or not isinstance(entry["resource_id"], str) or not _HASH.fullmatch(entry["resource_id"])
                    or not isinstance(entry["name"], str) or not _NAME.fullmatch(entry["name"])
                    or type(entry["removed"]) is not bool):
                raise PiResourceError("pi_resource_metadata_invalid")
        return value

    def _save(self, catalog):
        write_private_json(self.bindings / (catalog["binding"]["import_id"] + ".json"), catalog)

    def estimate_capture(self, request):
        selected = _selection(request)
        rows = _scan(Path(selected["path"]), tree=selected["kind"] == "tree")
        licenses = [_scan(Path(path))[0][2] for path in selected["licenses"]]
        size = sum(row[2] for row in rows) + sum(licenses)
        if size > MAX_ARTIFACT_BYTES:
            raise PiResourceError("pi_resource_size_limit")
        return {"bytes": size, "file_count": len(rows) + len(licenses),
                "peak_disk_bytes": size + MAX_METADATA_BYTES + HEADROOM_BYTES}

    def capture(self, import_id, revision, source_revision, request, *, disk_ceiling_bytes,
                cancel_event=None, deadline=None, validate_binding=None):
        binding = _binding(import_id, revision, source_revision)
        selected = _selection(request)
        budget = _Budget(disk_ceiling_bytes, cancel_event, deadline)
        with self.lock:
            self._catalog(binding, create=True)
        if validate_binding:
            validate_binding(binding)
        stage = ensure_private_directory(self.staging / str(uuid.uuid4()))
        try:
            path = Path(selected["path"])
            rows = _scan(path, tree=selected["kind"] == "tree", check=budget.check)
            total = sum(row[2] for row in rows)
            if total == 0 and selected["role"] in {"model-weights", "license"}:
                raise PiResourceError("pi_resource_empty")
            if selected.get("expected_size", total) != total:
                raise PiResourceError("artifact_size_mismatch")
            entries = []
            inputs = [(path / row[0] if selected["kind"] == "tree" else path, "content/" + row[0], row, False)
                      for row in rows]
            for i, license_path in enumerate(selected["licenses"]):
                license_source = Path(license_path)
                license_rows = _scan(license_source, check=budget.check)
                if license_rows[0][2] == 0:
                    raise PiResourceError("artifact_license_format_required")
                inputs.append((license_source, "licenses/notice-" + str(i), license_rows[0], True))
            if sum(row[2][2] for row in inputs) > MAX_ARTIFACT_BYTES:
                raise PiResourceError("pi_resource_size_limit")
            for source, relative, row, license_text in inputs:
                budget.require(stage, row[2])
                destination = stage / relative
                ensure_private_directory(destination.parent)
                with _DirectoryChain(source.parent) as chain:
                    fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=chain.fd)
                    try:
                        before = os.fstat(fd)
                        if _identity(before) != row[1]:
                            raise PiResourceError("pi_resource_source_changed")
                        digest = _hash_file(fd, row[2], budget.check, text=license_text)
                        if not license_text and selected["kind"] != "tree" and selected.get("expected_sha256", digest) != digest:
                            raise PiResourceError("artifact_hash_mismatch")
                        copy_regular_file(fd, destination, size=row[2], executable=row[3] and not license_text, check=budget.check)
                        if (_identity(os.fstat(fd)) != row[1]
                                or _identity(os.stat(source.name, dir_fd=chain.fd, follow_symlinks=False)) != row[1]):
                            raise PiResourceError("pi_resource_source_changed")
                        chain.check()
                    finally:
                        os.close(fd)
                budget.written += row[2]
                entries.append({"path": relative, "size": row[2], "sha256": digest,
                                "executable": row[3] if not license_text else False,
                                "role": "license" if license_text else selected["role"]})
            if _scan(path, tree=selected["kind"] == "tree", check=budget.check) != rows:
                raise PiResourceError("pi_resource_source_changed")
            manifest = {"schema_version": 1, "kind": selected["kind"], "role": selected["role"],
                "format": selected["format"], "origin": selected["origin"],
                "license_status": "declared" if selected["licenses"] or selected["role"] == "license" else "missing",
                "entries": sorted(entries, key=lambda item: item["path"])}
            resource_id = _digest(manifest)
            metadata_size = len(json.dumps(manifest, separators=(",", ":")).encode())
            budget.require(stage, metadata_size)
            write_private_json(stage / "resource.json", manifest)
            budget.written += metadata_size
            self._verify_tree(stage, resource_id, budget.check)
            with self.lock:
                budget.check()
                if validate_binding:
                    validate_binding(binding)
                catalog = self._catalog(binding, create=True)
                previous = next((item for item in catalog["resources"] if item["name"] == selected["name"]), None)
                if previous is not None and not previous["removed"] and previous["resource_id"] != resource_id:
                    raise PiResourceError("pi_resource_name_conflict")
                destination = self.objects / resource_id
                if destination.exists() or destination.is_symlink():
                    self._verify_tree(destination, resource_id, budget.check)
                else:
                    self._sync_directories(stage)
                    if self.retained_bytes() + sum(entry["size"] for entry in entries) > MAX_RETAINED_BYTES:
                        raise PiResourceError("pi_resource_retention_limit")
                    source_fd = os.open(self.staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    target_fd = os.open(self.objects, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        try:
                            _publish_directory(source_fd, stage.name, target_fd, resource_id)
                        except FileExistsError:
                            self._verify_tree(destination, resource_id, budget.check)
                        os.fsync(source_fd)
                        os.fsync(target_fd)
                    finally:
                        os.close(source_fd)
                        os.close(target_fd)
                item = {"resource_id": resource_id, "name": selected["name"], "removed": False}
                catalog["resources"] = [old for old in catalog["resources"] if old["name"] != selected["name"]] + [item]
                if len(catalog["resources"]) > MAX_RESOURCES:
                    raise PiResourceError("pi_resource_limit")
                retained = sum(self._summary(old)["bytes"] for old in catalog["resources"] if not old["removed"])
                if retained > MAX_ARTIFACT_BYTES:
                    raise PiResourceError("pi_resource_size_limit")
                self._save(catalog)
            result = self._summary(item)
            result["reference_created"] = previous is None or previous["removed"]
            return result
        except (OSError, ArtifactError, PackageError):
            raise PiResourceError("pi_resource_capture_failed") from None
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def _verify_tree(self, root, resource_id, check=lambda: None):
        ensure_private_directory(root)
        manifest = _read_metadata(root / "resource.json")
        if (_digest(manifest) != resource_id or set(manifest) != {"schema_version", "kind", "role", "format", "origin", "license_status", "entries"}
                or manifest["schema_version"] != 1 or manifest["kind"] not in _KINDS
                or manifest["role"] not in _ROLES or manifest["format"] not in _FORMATS
                or manifest["license_status"] not in {"declared", "missing"}
                or not isinstance(manifest["entries"], list) or not 1 <= len(manifest["entries"]) <= MAX_RESOURCE_FILES + 64):
            raise PiResourceError("pi_resource_metadata_invalid")
        expected = {"resource.json"}
        size = 0
        for entry in manifest["entries"]:
            check()
            if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256", "executable", "role"}
                    or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_ARTIFACT_FILE_BYTES
                    or type(entry["executable"]) is not bool or entry["role"] not in _ROLES
                    or entry["role"] == "license" and (entry["size"] == 0 or entry["executable"])
                    or not isinstance(entry["sha256"], str) or not _HASH.fullmatch(entry["sha256"])):
                raise PiResourceError("pi_resource_metadata_invalid")
            relative = _relative(entry["path"])
            if relative in expected or not relative.startswith(("content/", "licenses/")):
                raise PiResourceError("pi_resource_metadata_invalid")
            expected.add(relative)
            size += entry["size"]
            with _DirectoryChain((root / relative).parent) as chain:
                fd = os.open((root / relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=chain.fd)
                try:
                    before = os.fstat(fd)
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                            or before.st_size != entry["size"]
                            or stat.S_IMODE(before.st_mode) != (0o700 if entry["executable"] else 0o600)
                            or _hash_file(fd, entry["size"], check, text=entry["role"] == "license") != entry["sha256"]):
                        raise PiResourceError("pi_resource_changed")
                    if (_identity(before) != _identity(os.fstat(fd))
                            or _identity(before) != _identity(os.stat((root / relative).name, dir_fd=chain.fd, follow_symlinks=False))):
                        raise PiResourceError("pi_resource_changed")
                    chain.check()
                finally:
                    os.close(fd)
        if size > MAX_ARTIFACT_BYTES:
            raise PiResourceError("pi_resource_size_limit")
        actual = set()
        for directory, directories, files in os.walk(root, followlinks=False):
            check()
            ensure_private_directory(Path(directory))
            if any((Path(directory) / name).is_symlink() for name in directories):
                raise PiResourceError("pi_resource_changed")
            actual.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
        if actual != expected:
            raise PiResourceError("pi_resource_changed")
        return manifest

    @staticmethod
    def _sync_directories(root):
        for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def retained_bytes(self):
        """Declared committed storage, including soft-removed and orphan objects."""
        total = 0
        for path in self.objects.iterdir():
            if path.is_symlink() or not _HASH.fullmatch(path.name):
                raise PiResourceError("pi_resource_metadata_invalid")
            manifest = _read_metadata(path / "resource.json")
            if _digest(manifest) != path.name:
                raise PiResourceError("pi_resource_metadata_invalid")
            total += sum(entry["size"] for entry in manifest["entries"])
        return total

    def _summary(self, item, manifest=None):
        manifest = manifest or self._verify_tree(self.objects / item["resource_id"], item["resource_id"])
        result = {"resource_id": item["resource_id"], "name": item["name"], "kind": manifest["kind"],
                "role": manifest["role"], "format": manifest["format"], "origin": manifest["origin"],
                "license_status": manifest["license_status"], "bytes": sum(entry["size"] for entry in manifest["entries"]),
                "file_count": len(manifest["entries"]), "removed": item["removed"],
                "licenses": [{"path": entry["path"], "sha256": entry["sha256"], "size": entry["size"]}
                    for entry in manifest["entries"] if entry["role"] == "license"]}
        if manifest["kind"] != "tree":
            content = [entry for entry in manifest["entries"] if entry["path"].startswith("content/")]
            if len(content) != 1:
                raise PiResourceError("pi_resource_metadata_invalid")
            result.update(file_size=content[0]["size"], file_sha256=content[0]["sha256"])
        return result

    def source_file(self, resource_id):
        """Verified single-file path for trusted host code, never a public reply.

        Consumers hold their own no-follow FD while copying and recheck the
        returned summary's file SHA-256 before publication to a workspace.
        """
        if not isinstance(resource_id, str) or not _HASH.fullmatch(resource_id):
            raise PiResourceError("pi_resource_identity_invalid")
        root = self.objects / resource_id
        manifest = self._verify_tree(root, resource_id)
        if manifest["kind"] == "tree":
            raise PiResourceError("pi_resource_file_required")
        content = [entry for entry in manifest["entries"] if entry["path"].startswith("content/")]
        if len(content) != 1:
            raise PiResourceError("pi_resource_metadata_invalid")
        return root / content[0]["path"]

    def list(self, import_id, revision, source_revision, *, validate_binding=None):
        binding = _binding(import_id, revision, source_revision)
        if validate_binding:
            validate_binding(binding)
        with self.lock:
            catalog = self._catalog(binding)
            items = list(catalog["resources"])
        resources = [self._summary(item) for item in items if not item["removed"]]
        if validate_binding:
            validate_binding(binding)
        return {"schema_version": 1, "binding": binding, "resources": resources,
                "bytes": sum(item["bytes"] for item in resources)}

    def rebind(self, import_id, old_revision, new_revision, source_revision, *, validate_binding=None):
        old = _binding(import_id, old_revision, source_revision)
        new = _binding(import_id, new_revision, source_revision)
        if new_revision <= old_revision:
            raise PiResourceError("pi_resource_binding_invalid")
        with self.lock:
            catalog = self._catalog(old)
            for item in catalog["resources"]:
                if not item["removed"]:
                    self._summary(item)
            if validate_binding:
                validate_binding(new)
            catalog["binding"] = new
            self._save(catalog)
        return self.list(import_id, new_revision, source_revision, validate_binding=validate_binding)

    def remove(self, import_id, revision, source_revision, resource_id, *, validate_binding=None):
        binding = _binding(import_id, revision, source_revision)
        if not isinstance(resource_id, str) or not _HASH.fullmatch(resource_id):
            raise PiResourceError("pi_resource_identity_invalid")
        with self.lock:
            if validate_binding:
                validate_binding(binding)
            catalog = self._catalog(binding)
            found = False
            for item in catalog["resources"]:
                if item["resource_id"] == resource_id:
                    item["removed"] = True
                    found = True
            if not found:
                raise PiResourceError("pi_resource_not_found")
            self._save(catalog)
        return {"resource_id": resource_id, "removed": True}

    def remove_name(self, import_id, revision, source_revision, name, *, resource_id=None, validate_binding=None):
        """Soft-remove one named reference without affecting aliases or bytes."""
        binding = _binding(import_id, revision, source_revision)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise PiResourceError("pi_resource_request_invalid")
        if resource_id is not None and (not isinstance(resource_id, str) or not _HASH.fullmatch(resource_id)):
            raise PiResourceError("pi_resource_identity_invalid")
        with self.lock:
            if validate_binding:
                validate_binding(binding)
            catalog = self._catalog(binding)
            item = next((item for item in catalog["resources"] if item["name"] == name), None)
            if item is None:
                raise PiResourceError("pi_resource_not_found")
            if resource_id is not None and item["resource_id"] != resource_id:
                raise PiResourceError("pi_resource_binding_changed")
            item["removed"] = True
            self._save(catalog)
        return {"resource_id": item["resource_id"], "name": name, "removed": True}

    def materialize(self, import_id, revision, source_revision, workspace, *, disk_ceiling_bytes,
                    cancel_event=None, deadline=None, validate_binding=None):
        binding = _binding(import_id, revision, source_revision)
        budget = _Budget(disk_ceiling_bytes, cancel_event, deadline)
        workspace = ensure_private_directory(workspace)
        with self.lock:
            catalog = self._catalog(binding)
            items = [dict(item) for item in catalog["resources"] if not item["removed"]]
        if validate_binding:
            validate_binding(binding)
        stage = ensure_private_directory(workspace / (".resources-" + str(uuid.uuid4())))
        destination = workspace / "resources"
        resources = []
        try:
            for item in items:
                budget.check()
                source = self.objects / item["resource_id"]
                manifest = self._verify_tree(source, item["resource_id"], budget.check)
                for entry in manifest["entries"]:
                    budget.require(stage, entry["size"])
                    relative = entry["path"]
                    target = stage / item["name"] / relative
                    ensure_private_directory(target.parent)
                    with _DirectoryChain((source / entry["path"]).parent) as chain:
                        fd = os.open((source / entry["path"]).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=chain.fd)
                        try:
                            copy_regular_file(fd, target, size=entry["size"], executable=entry["executable"], check=budget.check)
                            chain.check()
                        finally:
                            os.close(fd)
                    budget.written += entry["size"]
                    with target.open("rb") as copied:
                        if _hash_file(copied.fileno(), entry["size"], budget.check, text=entry["role"] == "license") != entry["sha256"]:
                            raise PiResourceError("pi_resource_changed")
                self._verify_tree(source, item["resource_id"], budget.check)
                summary = self._summary(item, manifest)
                summary["root_path"] = "resources/" + item["name"]
                summary["path"] = summary["root_path"] + "/content"
                summary["licenses_path"] = summary["root_path"] + "/licenses"
                if manifest["kind"] != "tree":
                    summary["file_path"] = summary["path"] + "/" + next(entry["path"].removeprefix("content/")
                        for entry in manifest["entries"] if entry["path"].startswith("content/"))
                resources.append(summary)
            with self.lock:
                if self._catalog(binding) != catalog:
                    raise PiResourceError("pi_resource_binding_changed")
                if validate_binding:
                    validate_binding(binding)
                budget.check()
                if destination.exists() or destination.is_symlink():
                    raise PiResourceError("pi_resource_mount_exists")
                self._sync_directories(stage)
                os.rename(stage, destination)
                parent_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            return {"schema_version": 1, "binding": binding, "resources": resources,
                    "bytes": budget.written, "path": "resources"}
        except (OSError, ArtifactError, PackageError):
            raise PiResourceError("pi_resource_materialize_failed") from None
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    async def capture_async(self, *args, **kwargs):
        return await self._owned(self.capture, *args, **kwargs)

    async def materialize_async(self, *args, **kwargs):
        return await self._owned(self.materialize, *args, **kwargs)

    @staticmethod
    async def _owned(function, *args, **kwargs):
        cancel = kwargs.get("cancel_event")
        if cancel is None:
            cancel = kwargs["cancel_event"] = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            raise
        finally:
            await _await_ownership(asyncio.gather(task, return_exceptions=True))
