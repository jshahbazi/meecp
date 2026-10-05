"""Versioned confinement for the two exact private Python probe launchers.

The host selects this policy; a descriptor or recipe cannot request a broader
policy. This is an exact-platform Seatbelt checkpoint, not a supported public
macOS isolation API or an aggregate physical-memory ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import stat

from .contracts import ValidatedPackage
from .preparation_contracts import PreparationError, canonical_bytes

SANDBOX = Path("/usr/bin/sandbox-exec")
_PYTHON = "python/bin/python3.12"
# uv's standalone macOS interpreter embeds Python and exceeds 16 MiB.
# Keep this larger bound specific to the interpreter binding.
_INTERPRETER_MAX_BYTES = 32 * 1024**2
_BOOTSTRAP = "bootstrap.py"
_PREFIX = ('#!/bin/sh\nset -eu\n'
           'BIN=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
           'PACKAGE=$(CDPATH= cd -- "$BIN/.." && pwd)\n')
# Published generators remain immutable. Only their exact launcher bytes
# establish equivalence with the direct private-interpreter launch below.
_LAUNCHERS = {
    (_PREFIX + 'exec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/bootstrap.py" "$@" 2>/dev/null\n').encode(),
    (_PREFIX + 'exec "$PACKAGE/python/bin/python3.12" -I -S -B "$PACKAGE/bootstrap.py" 2>/dev/null\n').encode(),
}


def _failure(code="preparation_probe_confinement_mismatch"):
    raise PreparationError(code)


def _directory(path: Path, *, private=False) -> tuple[int, int, int, int]:
    if not path.is_absolute() or path.resolve() != path:
        _failure("unsafe_preparation_probe_path")
    try:
        details = path.lstat()
    except OSError:
        _failure("unsafe_preparation_probe_path")
    if (not stat.S_ISDIR(details.st_mode) or
            (private and (details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700))):
        _failure("unsafe_preparation_probe_path")
    return details.st_dev, details.st_ino, details.st_uid, stat.S_IMODE(details.st_mode)


def _file(path: Path, *, maximum=16 * 1024**2) -> tuple[str, bytes]:
    """Bounded held-file read; named replacement cannot become a valid binding."""
    descriptor = None
    try:
        _directory(path.parent)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_mode & 0o022 or not 0 < before.st_size <= maximum):
            _failure()
        value = bytearray()
        while len(value) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        after, named = os.fstat(descriptor), path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_nlink,
                                 item.st_uid, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(named) or len(value) != before.st_size:
            _failure()
        _directory(path.parent)
        result = bytes(value)
        return hashlib.sha256(result).hexdigest(), result
    except (OSError, ValueError):
        _failure()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def platform_identity() -> dict[str, str]:
    product = platform.mac_ver()[0]
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not product.startswith("26."):
        _failure("preparation_probe_confinement_unavailable")
    return {"system": "Darwin", "architecture": "arm64", "product_version": product,
            "kernel_release": platform.release(), "kernel_version": platform.version()}


def restricted_profile(package: Path, interpreter: Path, home: Path, temporary: Path) -> str:
    """No fork, spawn, group escape, network or write outside private probe data.

    Syscall numbers are the Darwin arm64 SDK ABI (82, 147, 244); the complete
    current platform and sandbox binary are bound by the host launch plan.
    """
    for path in (package, interpreter.parent, home, temporary):
        _directory(path)
    quote = lambda path: json.dumps(str(path), ensure_ascii=True)
    reads = " ".join("(subpath " + quote(path) + ")" for path in (package, home, temporary))
    writes = " ".join("(subpath " + quote(path) + ")" for path in (home, temporary))
    ancestors = sorted({ancestor for path in (package, home, temporary) for ancestor in path.parents}, key=str)
    metadata = " ".join("(literal " + quote(path) + ")" for path in ancestors)
    return "\n".join([
        "(version 1)", "(deny default (with no-log))", "(allow sysctl-read)",
        "(allow file-read-metadata " + metadata + ")",
        '(allow file-read* (literal "/") (subpath "/System") (subpath "/usr/lib") '
        '(subpath "/usr/share") (subpath "/private/var/db/dyld") '
        '(literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") ' + reads + ")",
        '(allow file-write* (literal "/dev/null") ' + writes + ")",
        "(allow process-exec (literal " + quote(interpreter) + "))",
        "(deny syscall-unix (syscall-number 82 147 244) (with no-log))",
    ])


@dataclass(frozen=True)
class ProbeConfinementV1:
    package_root: Path
    package_digest: str
    workspace: Path
    home: Path
    temporary: Path
    reaper: Path
    reaper_sha256: str
    sandbox_sha256: str
    platform_json: bytes
    profile: str
    policy_digest: str
    launcher_sha256: str
    interpreter_sha256: str
    bootstrap_sha256: str
    directory_identities: tuple[tuple[int, int, int, int], ...]

    @classmethod
    def capture(cls, package: ValidatedPackage, workspace: Path, reaper: Path) -> "ProbeConfinementV1":
        root, workspace, reaper = package.root, Path(workspace), Path(reaper)
        _directory(root)
        _directory(workspace, private=True)
        if root == workspace or root in workspace.parents or workspace in root.parents:
            _failure("unsafe_preparation_probe_path")
        entry = package.manifest.entrypoint
        if (package.manifest.profile != "mcp-stdio" or entry is None or
                entry.executable != "bin/service" or entry.cwd != "."):
            _failure("unsupported_preparation_probe_confinement")
        launcher_hash, launcher = _file(root / entry.executable, maximum=4096)
        if launcher not in _LAUNCHERS or (b'"$@"' not in launcher and entry.args):
            _failure("unsupported_preparation_probe_confinement")
        records = {artifact.path: artifact for artifact in package.lock.artifacts}
        interpreter_hash, _ = _file(root / _PYTHON, maximum=_INTERPRETER_MAX_BYTES)
        bootstrap_hash, _ = _file(root / _BOOTSTRAP)
        for name, observed in ((entry.executable, launcher_hash), (_PYTHON, interpreter_hash), (_BOOTSTRAP, bootstrap_hash)):
            if name not in records or records[name].sha256 != observed:
                _failure()
        current_platform = canonical_bytes(platform_identity())
        reaper_hash, _ = _file(reaper)
        sandbox_hash, _ = _file(SANDBOX)
        home, temporary = workspace / "confined-home", workspace / "confined-tmp"
        created = []
        try:
            for directory in (home, temporary):
                directory.mkdir(mode=0o700)
                created.append(directory)
                _directory(directory, private=True)
            profile = restricted_profile(root, root / _PYTHON, home, temporary)
            identities = tuple(_directory(directory) for directory in (root, workspace, home, temporary))
            payload = {"schema_version": 1, "kind": "private-python-probe-v1", "package_digest": package.digest,
                       "package_root": str(root), "workspace": str(workspace), "profile": profile,
                       "platform": json.loads(current_platform), "reaper_sha256": reaper_hash,
                       "sandbox_sha256": sandbox_hash, "launcher_sha256": launcher_hash,
                       "interpreter_sha256": interpreter_hash, "bootstrap_sha256": bootstrap_hash,
                       "directory_identities": [list(identity) for identity in identities]}
            digest = hashlib.sha256(b"mcpme-probe-confinement-v1\0" + canonical_bytes(payload)).hexdigest()
            return cls(root, package.digest, workspace, home, temporary, reaper, reaper_hash,
                       sandbox_hash, current_platform, profile, digest, launcher_hash, interpreter_hash, bootstrap_hash, identities)
        except BaseException:
            for directory in reversed(created):
                with contextlib.suppress(OSError):
                    directory.rmdir()
            raise

    def check(self, package: ValidatedPackage, reaper: Path) -> None:
        if package.root != self.package_root or package.digest != self.package_digest or Path(reaper) != self.reaper:
            _failure()
        if (self.home != self.workspace / "confined-home" or self.temporary != self.workspace / "confined-tmp"
                or self.package_root == self.workspace or self.package_root in self.workspace.parents
                or self.workspace in self.package_root.parents):
            _failure("unsafe_preparation_probe_path")
        entry = package.manifest.entrypoint
        if (package.manifest.profile != "mcp-stdio" or entry is None
                or entry.executable != "bin/service" or entry.cwd != "."):
            _failure("unsupported_preparation_probe_confinement")
        launcher = _file(self.package_root / "bin/service", maximum=4096)[1]
        if launcher not in _LAUNCHERS or (b'"$@"' not in launcher and entry.args):
            _failure("unsupported_preparation_probe_confinement")
        _directory(self.workspace, private=True)
        _directory(self.home, private=True)
        _directory(self.temporary, private=True)
        if tuple(_directory(directory) for directory in (self.package_root, self.workspace, self.home, self.temporary)) != self.directory_identities:
            _failure("preparation_probe_confinement_mismatch")
        if self.profile != restricted_profile(self.package_root, self.package_root / _PYTHON, self.home, self.temporary):
            _failure()
        payload = {"schema_version": 1, "kind": "private-python-probe-v1", "package_digest": self.package_digest,
                   "package_root": str(self.package_root), "workspace": str(self.workspace), "profile": self.profile,
                   "platform": json.loads(self.platform_json), "reaper_sha256": self.reaper_sha256,
                   "sandbox_sha256": self.sandbox_sha256, "launcher_sha256": self.launcher_sha256,
                   "interpreter_sha256": self.interpreter_sha256, "bootstrap_sha256": self.bootstrap_sha256,
                   "directory_identities": [list(identity) for identity in self.directory_identities]}
        if hashlib.sha256(b"mcpme-probe-confinement-v1\0" + canonical_bytes(payload)).hexdigest() != self.policy_digest:
            _failure()
        if canonical_bytes(platform_identity()) != self.platform_json:
            _failure()
        if _file(self.package_root / _PYTHON, maximum=_INTERPRETER_MAX_BYTES)[0] != self.interpreter_sha256:
            _failure()
        for path, expected in ((self.reaper, self.reaper_sha256), (SANDBOX, self.sandbox_sha256),
                               (self.package_root / "bin/service", self.launcher_sha256),
                               (self.package_root / _BOOTSTRAP, self.bootstrap_sha256)):
            if _file(path)[0] != expected:
                _failure()
        # The last file read may yield to a trusted filesystem mutation. A
        # successful check must still describe the same private directories.
        _directory(self.workspace, private=True)
        _directory(self.home, private=True)
        _directory(self.temporary, private=True)
        if tuple(_directory(directory) for directory in (self.package_root, self.workspace, self.home, self.temporary)) != self.directory_identities:
            _failure()

    def launch(self, package: ValidatedPackage, reaper: Path):
        self.check(package, reaper)
        return SANDBOX, ["-p", self.profile, str(self.package_root / _PYTHON), "-I", "-S", "-B",
                         str(self.package_root / _BOOTSTRAP), *package.manifest.entrypoint.args], self.package_root, {
                             "PATH": "/usr/bin:/bin", "HOME": str(self.home), "TMPDIR": str(self.temporary), "LANG": "C"}
