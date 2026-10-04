"""Fixed signed distribution boundary for nonempty release authority.

These public certificate identifiers are reviewed source constants. A policy,
host receipt, user setting or environment variable cannot select another signer.
"""
from __future__ import annotations

import hashlib
import json
import ctypes
import errno
from dataclasses import dataclass, field
from functools import lru_cache
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import weakref

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


def _identity_matches(path, identity, full=True, *, app_root=None):
    current = _identity(path, full)
    # macOS may add MACL metadata to the enclosing app after first launch.
    # Only that verified directory's metadata history is excluded. Its current
    # structure and every descendant/file identity remain bound in full.
    if (app_root is not None and path == app_root and full
            and stat.S_ISDIR(current[2]) and stat.S_ISDIR(identity[2])):
        return current[:-1] == identity[:-1]
    return current == identity


@lru_cache(maxsize=1)
def _acl_api():
    if sys.platform != "darwin": _deny()
    try:
        api = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        signatures = {
            "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
            "acl_valid": ([ctypes.c_void_p], ctypes.c_int),
            "acl_get_entry": ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            "acl_size": ([ctypes.c_void_p], ctypes.c_ssize_t),
            "acl_copy_ext": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t], ctypes.c_ssize_t),
            "acl_free": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        return api
    except (OSError, AttributeError):
        _deny()


def _acl_identity(fd):
    # Darwin sys/acl.h: ACL_TYPE_EXTENDED=0x100, ACL_MAX_ENTRIES=128.
    # acl_copy_ext is the documented portable big-endian external form, with
    # ordered entries and raw UUIDs. No name lookup, text parsing or subprocess.
    api = _acl_api()
    ctypes.set_errno(0)
    acl = api.acl_get_fd_np(fd, 0x100)
    if not acl:
        # Darwin reports an absent extended ACL as ENOENT. The caller holds a
        # live directory FD and verifies its path/fstat identity on both sides.
        if ctypes.get_errno() == errno.ENOENT: return None
        _deny()
    try:
        if api.acl_valid(acl) != 0: _deny()
        for index in range(129):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = api.acl_get_entry(acl, index, ctypes.byref(entry))
            if result == -1 and ctypes.get_errno() == errno.EINVAL: break
            if result != 0 or index == 128: _deny()
        size = api.acl_size(acl)
        if not 0 < size <= 4096: _deny()
        buffer = ctypes.create_string_buffer(size)
        if api.acl_copy_ext(buffer, acl, size) != size: _deny()
        return hashlib.sha256(buffer.raw).digest()
    finally:
        if api.acl_free(acl) != 0: _deny()


class _AppRootMetadata:
    """One held no-follow directory, with a fixed ACL/group/flags baseline."""
    def __init__(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            value = os.fstat(fd)
            self.identity = (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_nlink, value.st_size, value.st_mtime_ns)
            if not stat.S_ISDIR(value.st_mode) or _identity(path)[:-1] != self.identity: _deny()
            self.flags = value.st_flags
            self.gid = value.st_gid
            self.acl = _acl_identity(fd)
            self.fd = fd
            self.check(path)
        except BaseException:
            os.close(fd)
            raise
        self._finalizer = weakref.finalize(self, os.close, fd)

    def check(self, path):
        before = os.fstat(self.fd)
        if (_identity(path)[:-1] != self.identity or before.st_flags != self.flags
                or before.st_gid != self.gid or path.lstat().st_gid != self.gid
                or (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                    before.st_nlink, before.st_size, before.st_mtime_ns) != self.identity
                or _acl_identity(self.fd) != self.acl): _deny()
        after = os.fstat(self.fd)
        if ((after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                after.st_nlink, after.st_size, after.st_mtime_ns) != self.identity
                or after.st_flags != self.flags or after.st_gid != self.gid
                or path.lstat().st_gid != self.gid or _identity(path)[:-1] != self.identity): _deny()


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
    _validated_app_root: Path | None = field(default=None, init=False, repr=False)
    _app_root_metadata: _AppRootMetadata | None = field(default=None, init=False, repr=False)

    def _check_app_root_metadata(self):
        if self._validated_app_root is not None:
            if self._app_root_metadata is None: _deny()
            self._app_root_metadata.check(self._validated_app_root)

    def check_current(self, *, full=False):
        try:
            self._check_app_root_metadata()
            for path, identity, complete in self.identities:
                if (full or path in self.critical) and not _identity_matches(
                        path, identity, complete, app_root=self._validated_app_root):
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
        root_metadata = _AppRootMetadata(app)
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
                not _identity_matches(path, identity, path in all_files or path == app or app in path.parents,
                    app_root=app)
                for path, identity in baseline.items()) or _read(host_root / policy_path, 64 * 1024) != policy_bytes): _deny()
        critical |= {parent for path in critical for parent in path.parents}
        proof = ReleaseArtifactProof(tuple((path, identity, path in all_files or path == app or app in path.parents)
            for path, identity in baseline.items()), frozenset(critical), host_root / policy_path, policy_bytes,
            hashlib.sha256(build_bytes).hexdigest())
        object.__setattr__(proof, "_validated_app_root", app)
        object.__setattr__(proof, "_app_root_metadata", root_metadata)
        proof.check_current(full=True)
        return proof
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError):
        _deny()
