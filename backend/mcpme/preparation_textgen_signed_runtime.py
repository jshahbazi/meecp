"""Closed, versioned signed runtime bytes for the TextGen family.

No discovery, process launch, network access or signing occurs here. A release
producer freezes the asset once; jobs copy only the reviewed artifact set.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .preparation_builder import _copy, _opened
from .preparation_contracts import PreparationError
from .recipes import _read

PROFILE = "textgen-signed-cpu-v1"
ASSET_DIRECTORY = "text-generate-signed-v1"
MAX_FILES = 12000
MAX_BYTES = 2 * 1024**3


def fail():
    raise PreparationError("textgen_signed_runtime_identity_mismatch")


def asset_root():
    return Path(__file__).resolve().parents[2] / "templates" / ASSET_DIRECTORY


def manifest(root=None):
    """Parse only the compiled-in profile; caller metadata cannot select trust."""
    try:
        data = _read((root or asset_root()) / "signed-runtime.json", 4 * 1024**2)
        value = json.loads(data)
        if (set(value) != {"schema_version", "profile", "archive", "signing", "artifacts", "provenance_sha256"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["profile"] != PROFILE):
            fail()
        archive, signing, records = value["archive"], value["signing"], value["artifacts"]
        if (set(archive) != {"sha256", "bytes", "format"} or archive["format"] != "tar-gzip"
                or type(archive["bytes"]) is not int or not 0 < archive["bytes"] <= MAX_BYTES
                or not re.fullmatch(r"[0-9a-f]{64}", archive["sha256"])
                or not re.fullmatch(r"[0-9a-f]{64}", value["provenance_sha256"])):
            fail()
        if (set(signing) != {"certificate_sha1", "team_id", "hardened_runtime", "secure_timestamp", "entitlements"}
                or not re.fullmatch(r"[0-9A-F]{40}", signing["certificate_sha1"])
                or not re.fullmatch(r"[A-Z0-9]{10}", signing["team_id"])
                or signing["hardened_runtime"] is not True or signing["secure_timestamp"] is not True
                or signing["entitlements"] != {}):
            fail()
        if not isinstance(records, dict) or not 1 <= len(records) <= MAX_FILES:
            fail()
        total = 0
        for name, record in records.items():
            path = PurePosixPath(name)
            if (not isinstance(name, str) or not 0 < len(name) <= 4096 or path.is_absolute()
                    or path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts)
                    or "\\" in name or "\0" in name or not isinstance(record, dict)
                    or set(record) != {"sha256", "size", "executable"}
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                    or type(record["size"]) is not int or not 0 <= record["size"] <= MAX_BYTES
                    or type(record["executable"]) is not bool):
                fail()
            total += record["size"]
        required = {"bin/textgen-engine", "bin/textgen-adapter", "python/bin/python3.12",
                    "llama/llama-server.real", "origin.json", "uv.lock", "requirements.txt",
                    "dependency-project.toml", "dependency-notices.json", "python-runtime-origin.json",
                    "licenses/DEPENDENCIES.md", "licenses/python-LICENSE.txt"}
        if (total > MAX_BYTES or not required <= records.keys() or "llama/llama-server" in records
                or records["bin/textgen-engine"] != records["bin/textgen-adapter"]
                or any(not records[name]["executable"] for name in
                       ("bin/textgen-engine", "python/bin/python3.12", "llama/llama-server.real"))):
            fail()
        provenance = _read((root or asset_root()) / "signed-runtime-provenance.json", 4 * 1024**2)
        if hashlib.sha256(provenance).hexdigest() != value["provenance_sha256"]:
            fail()
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        fail()


def _members(root, check):
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        fail()
    names = set()
    for path in root.rglob("*"):
        check()
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            fail()
        names.add(path.relative_to(root).as_posix())
        if len(names) > MAX_FILES + 512:
            fail()
    return names


def verify_runtime(root, value=None, *, exact=False, check=lambda: None):
    """Hash complete bytes with no-follow descriptor checks, before and after copy."""
    value = manifest() if value is None else value
    records = value["artifacts"]
    before = _members(root, check)
    if exact and before != set(records):
        fail()
    if {p for p in before if p.startswith(("python/", "llama/", "bin/"))} != {
            p for p in records if p.startswith(("python/", "llama/", "bin/"))}:
        fail()
    for name, record in records.items():
        check()
        path = root / name
        total, digest = 0, hashlib.sha256()
        with _opened(path) as source:
            info = os.fstat(source.fileno())
            if bool(info.st_mode & 0o111) != record["executable"] or info.st_size != record["size"]:
                fail()
            while chunk := source.read(1024 * 1024):
                check()
                total += len(chunk)
                if total > record["size"]:
                    fail()
                digest.update(chunk)
        if total != record["size"] or digest.hexdigest() != record["sha256"]:
            fail()
    if _members(root, check) != before:
        fail()
    return value


def copy_runtime(source, destination, *, check=lambda: None):
    value = verify_runtime(source, exact=True, check=check)
    for name in value["artifacts"]:
        check()
        _copy(source / name, destination / name, check)
    verify_runtime(source, value, exact=True, check=check)
    verify_runtime(destination, value, check=check)
    return "Frozen Developer ID TextGen runtime " + value["archive"]["sha256"]
