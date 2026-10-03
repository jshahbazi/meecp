"""Host-selected, exact-platform confinement for the pinned CPU text graph.

This is a probe capability, never recipe-selected runtime admission. Custom
Seatbelt evidence does not establish a physical-memory ceiling or lifetime
descendant accounting.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat

from .contracts import validate_package
from .preparation_contracts import PreparationError, canonical_bytes
from .preparation_probe_confinement import SANDBOX, _directory as _checked_directory, _file, platform_identity
from .preparation_textgen import validate_model_package


def fail():
    raise PreparationError("textgen_probe_confinement_mismatch")


def _directory(path, **kwargs):
    try:
        return _checked_directory(path, **kwargs)
    except RuntimeError:
        # Path.resolve raises RuntimeError for a symlink cycle on Python 3.12.
        fail()


def _closed(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PreparationError:
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            fail()
    return bounded


def _identity(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_mode & 0o022:
        fail()
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_uid,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _directory_identity(path, root):
    identity = _directory(path)
    if path == root or root in path.parents:
        details = path.lstat()
        return (*identity, details.st_mtime_ns, details.st_ctime_ns)
    return identity


def _snapshot(package, reaper):
    root = package.root
    names = [root / item.path for item in package.lock.artifacts]
    names.extend((root / "mcpme.package.json", root / "mcpme.lock.json", reaper, SANDBOX))
    files = tuple((str(path), _identity(path)) for path in sorted(set(names)))
    directories = {root}
    for name, _ in files:
        path = Path(name).parent
        while path == root or root in path.parents:
            directories.add(path)
            if path == root:
                break
            path = path.parent
    return files, tuple((str(path), _directory_identity(path, root)) for path in sorted(directories))


def _records_digest(records):
    if type(records) is not tuple or len(records) > 32768:
        fail()
    digest = hashlib.sha256()
    for name, identity in records:
        if (type(name) is not str or len(name) > 4096 or type(identity) is not tuple
                or len(identity) > 8 or any(type(value) is not int for value in identity)):
            fail()
        digest.update(json.dumps([name, identity], ensure_ascii=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def role_profile(root, home, temporary, role, port, engine_port, keyfile):
    if (role not in {"engine", "adapter"} or type(port) is not int or not 1 <= port <= 65535
            or (role == "engine" and engine_port is not None)
            or (role == "adapter" and (type(engine_port) is not int or not 1 <= engine_port <= 65535
                                       or engine_port == port))):
        fail()
    quote = lambda value: json.dumps(str(value), ensure_ascii=True)
    paths = (root, home, temporary)
    ancestors = sorted({parent for path in (*paths, keyfile) for parent in path.parents}, key=str)
    metadata = " ".join("(literal " + quote(path) + ")" for path in ancestors)
    reads = " ".join("(subpath " + quote(path) + ")" for path in paths)
    writes = " ".join("(subpath " + quote(path) + ")" for path in (home, temporary))
    executables = [root / ("bin/textgen-" + role), root / "python/bin/python3.12"]
    if role == "engine":
        executables.append(root / "llama/llama-server.real")
    profile = ["(version 1)", "(deny default (with no-log))", "(allow sysctl-read)",
        "(allow file-read-metadata " + metadata + ")",
        '(allow file-read* (literal "/") (subpath "/System") (subpath "/usr/lib") '
        '(subpath "/usr/share") (subpath "/private/var/db/dyld") '
        '(literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") '
        + reads + " (literal " + quote(keyfile) + "))",
        '(allow file-write* (literal "/dev/null") ' + writes + ")",
        "(allow process-exec " + " ".join("(literal " + quote(path) + ")" for path in executables) + ")",
        "(deny syscall-unix (syscall-number 82 147 244) (with no-log))",
        '(allow network-bind (local tcp4 "localhost:' + str(port) + '"))',
        '(allow network-inbound (local tcp4 "localhost:' + str(port) + '"))']
    if role == "adapter":
        profile.append('(allow network-outbound (remote tcp4 "localhost:' + str(engine_port) + '"))')
    return "\n".join(profile)


@dataclass(frozen=True)
class TextgenProbeConfinementV1:
    package_root: Path
    package_digest: str
    descriptor_digest: str
    workspace: Path
    reaper: Path
    expected_launcher_sha256: str
    platform_json: bytes
    files: tuple[tuple[str, tuple[int, ...]], ...]
    directories: tuple[tuple[str, tuple[int, ...]], ...]
    bindings_json: bytes
    policy_digest: str

    @classmethod
    @_closed
    def from_host(cls, package, descriptor, workspace, reaper):
        from .preparation_builder import load_private_toolchain
        from .preparation_inputs import parse_preparation_receipt
        from .preparations import RECEIPT_NAME
        from .recipes import _read
        toolchain = load_private_toolchain(Path(reaper).parent.parent)
        receipt = parse_preparation_receipt(_read(package.root / RECEIPT_NAME, 1024**2))
        if (receipt.schema_version != 2 or
                [(item.name, item.digest) for item in receipt.inputs.runtimes] !=
                [("private-python-toolchain", toolchain.identity)]):
            fail()
        records = [toolchain.artifacts.get("textgen-runtime/bin/textgen-" + role)
                   for role in ("engine", "adapter")]
        if any(record is None or not record["executable"] for record in records):
            fail()
        if records[0]["sha256"] != records[1]["sha256"]:
            fail()
        return cls.capture(package, descriptor, workspace, reaper,
                           expected_launcher_sha256=records[0]["sha256"])

    @classmethod
    @_closed
    def capture(cls, package, descriptor, workspace, reaper, *, expected_launcher_sha256):
        """Run only on an owned joined worker; launcher identity is host input."""
        if type(expected_launcher_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_launcher_sha256) is None:
            fail()
        root, workspace, reaper = package.root, Path(workspace), Path(reaper)
        _directory(root)
        _directory(workspace, private=True)
        if root == workspace or root in workspace.parents or workspace in root.parents:
            fail()
        baseline = _snapshot(package, reaper)
        current = validate_package(root)
        if current.digest != package.digest:
            fail()
        validate_model_package(current, descriptor, require_receipt=True)
        bindings = {}
        for role in ("engine", "adapter"):
            name = "bin/textgen-" + role
            if not (root / name).lstat().st_mode & 0o111:
                fail()
            observed, _ = _file(root / name)
            if observed != expected_launcher_sha256:
                fail()
            bindings[name] = observed
        for name, path in (("reaper", reaper), ("sandbox", SANDBOX),
                           ("interpreter", root / "python/bin/python3.12"),
                           ("engine", root / "llama/llama-server.real")):
            if not path.lstat().st_mode & 0o111:
                fail()
            bindings[name] = _file(path)[0]
        if _snapshot(current, reaper) != baseline:
            fail()
        files, package_directories = baseline
        created = []
        try:
            for role in ("engine", "adapter"):
                for kind in ("home", "tmp"):
                    path = workspace / ("textgen-" + role + "-" + kind)
                    path.mkdir(mode=0o700)
                    created.append(path)
            directories = tuple(sorted((*package_directories,
                *((str(path), _directory(path, private=True)) for path in (workspace, *created)))))
            platform_json = canonical_bytes(platform_identity())
            bindings_json = canonical_bytes(bindings)
            values = (root, current.digest, descriptor.digest, workspace, reaper, expected_launcher_sha256,
                      platform_json, files, directories, bindings_json)
            plan = cls(*values, "")
            plan = cls(*values, plan._digest())
            plan.check(current, descriptor, reaper)
            return plan
        except BaseException:
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise

    def _digest(self):
        value = {"version": 1, "kind": "textgen-cpu-probe", "package": str(self.package_root),
            "package_digest": self.package_digest, "descriptor_digest": self.descriptor_digest,
            "workspace": str(self.workspace), "reaper": str(self.reaper),
            "launcher": self.expected_launcher_sha256, "platform": json.loads(self.platform_json),
            "files_digest": _records_digest(self.files),
            "directories_digest": _records_digest(self.directories),
            "bindings": json.loads(self.bindings_json)}
        return hashlib.sha256(b"mcpme-textgen-probe-confinement-v1\0" + canonical_bytes(value)).hexdigest()

    def admit(self):
        """Metadata-only final fence. No authority is inferred from this check."""
        try:
            if self._digest() != self.policy_digest:
                fail()
            if (self.package_root == self.workspace or self.package_root in self.workspace.parents
                    or self.workspace in self.package_root.parents):
                fail()
            for name, identity in self.directories:
                if _directory_identity(Path(name), self.package_root) != identity:
                    fail()
            for name, identity in self.files:
                if _identity(Path(name)) != identity:
                    fail()
            return True
        except (OSError, ValueError):
            fail()

    @_closed
    def check(self, package, descriptor, reaper):
        self.admit()
        if (package.root != self.package_root or package.digest != self.package_digest
                or descriptor.digest != self.descriptor_digest or Path(reaper) != self.reaper
                or canonical_bytes(platform_identity()) != self.platform_json):
            fail()
        current = validate_package(self.package_root)
        if current.digest != self.package_digest:
            fail()
        validate_model_package(current, descriptor, require_receipt=True)
        self.admit()

    @_closed
    def launch(self, package, descriptor, reaper, *, role, port, engine_port, keyfile, config_fd):
        self.check(package, descriptor, reaper)
        if (keyfile != self.workspace / (role + ".key") or type(config_fd) is not int or config_fd < 0):
            fail()
        details = keyfile.lstat()
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
            fail()
        _file(keyfile, maximum=4096)
        key_identity = _identity(keyfile)
        home = self.workspace / ("textgen-" + role + "-home")
        temporary = self.workspace / ("textgen-" + role + "-tmp")
        profile = role_profile(self.package_root, home, temporary, role, port, engine_port, keyfile)
        def admit():
            self.admit()
            if _identity(keyfile) != key_identity:
                fail()
            return True
        return SANDBOX, ["-p", profile, str(self.package_root / ("bin/textgen-" + role)),
            "--mcpme-config-fd", str(config_fd)], self.package_root, {
                "PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(temporary), "LANG": "C"}, admit
