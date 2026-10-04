"""Fixed signed distribution boundary for nonempty release authority.

These public certificate identifiers are reviewed source constants. A policy,
host receipt, user setting or environment variable cannot select another signer.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess

from .preparation_builder import _opened
from .preparation_contracts import PreparationError, load_contract
from .recipes import _read

CERTIFICATE_SHA1 = "EFE6CD2666B9106E68724C38ADD1C405267A2F94"
TEAM_ID = "S5BT2PRN5B"
MAX_FILES = 20000
MAX_BYTES = 4 * 1024**3


def _deny():
    raise PreparationError("invalid_release_artifact")


def load_artifact_json(data):
    """Build inventories exceed small preparation-contract limits."""
    if type(data) is not bytes or len(data) > 8 * 1024**2: _deny()
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: _deny()
            value[key] = item
        return value
    try:
        value = json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: _deny())
        if type(value) is not dict: _deny()
        return value
    except (UnicodeError, ValueError, RecursionError):
        _deny()


def _identity(path, full=True):
    value = path.lstat()
    identity = (value.st_dev, value.st_ino, value.st_mode, value.st_uid)
    return identity + ((value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns) if full else ())


def _hash(path):
    with _opened(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class ReleaseArtifactProof:
    """Held verified baseline. Checks never adopt replacement artifact bytes."""
    identities: tuple
    critical: frozenset
    policy: Path
    policy_bytes: bytes
    host_manifest_digest: str = "0" * 64

    def check_current(self, *, full=False):
        try:
            for path, identity, complete in self.identities:
                if (full or path in self.critical) and _identity(path, complete) != identity:
                    _deny()
            if _read(self.policy, 64 * 1024) != self.policy_bytes: _deny()
        except (OSError, ValueError, TypeError):
            _deny()


def _signature(app):
    requirement = ('=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        ' and certificate leaf = H"' + CERTIFICATE_SHA1 + '"')
    checks = (["--verify", "--deep", "--strict", "-R", requirement], ["--display", "--verbose=4"])
    results = [subprocess.run(["/usr/bin/codesign", *arguments, str(app)], capture_output=True,
        timeout=30, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}) for arguments in checks]
    if any(result.returncode != 0 for result in results): _deny()
    detail = results[1].stderr.decode("utf-8", errors="replace")
    if ("(runtime)" not in detail or not re.search(r"^Timestamp=.+$", detail, re.M)
            or not re.search(r"^TeamIdentifier=" + TEAM_ID + r"$", detail, re.M)):
        _deny()


def verify_release_artifact(host_root, policy_path, policy_bytes):
    """Validate the exact running host's closed payload and enclosing app seal.

    Called off-loop once before constructing any execution owner. This proves
    signed byte provenance in the original same-user filesystem scope; it does
    not claim protection from that user replacing the running process itself.
    """
    try:
        if (type(host_root) is not type(Path()) or not host_root.is_absolute()
                or host_root.resolve(strict=True) != host_root
                or host_root != Path(__file__).absolute().parents[2]
                or host_root.parts[-3:] != ("Contents", "Resources", "host")):
            _deny()
        app = host_root.parents[2]
        if not app.name.endswith(".app"): _deny()
        files, directories, total = {}, {host_root}, 0
        for path in host_root.rglob("*"):
            value = path.lstat()
            if value.st_uid != os.getuid() or value.st_mode & 0o6022: _deny()
            if stat.S_ISDIR(value.st_mode): directories.add(path)
            elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                files[path.relative_to(host_root).as_posix()] = path
                total += value.st_size
            else: _deny()
            if len(files) > MAX_FILES or len(directories) > MAX_FILES + 4096 or total > MAX_BYTES: _deny()
        # Retain all enclosing app identities too, including its CodeResources
        # seal. A later check never trusts a replacement seal or receipt.
        app_files = set()
        for path in app.rglob("*"):
            value = path.lstat()
            if stat.S_ISDIR(value.st_mode): directories.add(path)
            elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1: app_files.add(path)
            else: _deny()
            if len(app_files) > MAX_FILES or len(directories) > MAX_FILES + 4096: _deny()
        critical = {app / "Contents/Info.plist", app / "Contents/Resources/app-build.json", app / "Contents/MacOS/mcpme",
            app / "Contents/_CodeSignature/CodeResources", host_root / "build.json", host_root / policy_path}
        if not critical <= app_files: _deny()
        all_files = set(files.values()) | app_files
        directories |= {parent for path in all_files for parent in path.parents}
        for path in directories:
            value = path.lstat()
            if not stat.S_ISDIR(value.st_mode): _deny()
            if (path == app or app in path.parents) and (value.st_uid != os.getuid() or value.st_mode & 0o6022): _deny()
        app_bytes = 0
        for path in all_files:
            value = path.lstat()
            app_bytes += value.st_size
            if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                    or value.st_uid != os.getuid() or value.st_mode & 0o6022): _deny()
            if app_bytes > MAX_BYTES: _deny()
        baseline = {path: _identity(path, path in all_files or path == app or app in path.parents)
            for path in all_files | directories}
        build_bytes = _read(host_root / "build.json", 8 * 1024**2)
        build = load_artifact_json(build_bytes)
        if (set(build) != {"host_version", "artifact_sha256", "source_commit", "source_tree_dirty", "python", "toolchain", "signing", "distribution"}
                or build["source_tree_dirty"] is not False or build["signing"] != "developer_id"
                or build["distribution"] != "bundled_native_app_host"
                or type(build["source_commit"]) is not str or not re.fullmatch(r"[0-9a-f]{40}", build["source_commit"])
                or type(build["artifact_sha256"]) is not dict
                or set(build["artifact_sha256"]) != set(files) - {"build.json"}
                or build["artifact_sha256"].get(policy_path) != hashlib.sha256(policy_bytes).hexdigest()): _deny()
        for name, digest in build["artifact_sha256"].items():
            if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest) or _hash(files[name]) != digest: _deny()
        info = plistlib.loads(_read(app / "Contents/Info.plist", 64 * 1024))
        receipt = load_contract(_read(app / "Contents/Resources/app-build.json", 64 * 1024))
        if (info.get("CFBundleIdentifier") != "com.jshahbazi.mcpme" or info.get("CFBundleExecutable") != "mcpme"
                or info.get("CFBundleShortVersionString") != build["host_version"]
                or receipt.get("signing") != "developer_id" or receipt.get("distribution") != "developer_id_native_app"
                or receipt.get("hardened_runtime") is not True or receipt.get("secure_timestamp") is not True
                or receipt.get("source_tree_dirty") is not False or receipt.get("source_commit") != build["source_commit"]
                or receipt.get("version") != build["host_version"]
                or receipt.get("host_receipt_sha256") != hashlib.sha256(build_bytes).hexdigest()): _deny()
        _signature(app)
        if (host_root.resolve(strict=True) != host_root or any(
                _identity(path, path in all_files or path == app or app in path.parents) != identity
                for path, identity in baseline.items()) or _read(host_root / policy_path, 64 * 1024) != policy_bytes): _deny()
        critical |= {parent for path in critical for parent in path.parents}
        proof = ReleaseArtifactProof(tuple((path, identity, path in all_files or path == app or app in path.parents)
            for path, identity in baseline.items()), frozenset(critical), host_root / policy_path, policy_bytes,
            hashlib.sha256(build_bytes).hexdigest())
        proof.check_current(full=True)
        return proof
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError):
        _deny()
