"""Deterministic private builder. The preparation runner owns every process.

This module copies and verifies artifacts, interprets bounded metadata, and
requests closed command launches through an injected runner. It never imports
selected app modules and has no subprocess implementation or ambient fallback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from typing import Callable
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename

from .contracts import PackageError, package_relative_path
from .preparation_contracts import PreparationDescriptor, PreparationError, parse_descriptor
from .recipes import _read as _read_artifact

PYTHON_VERSION = "3.12.13"
MCP_VERSION = "1.30.0"
UV_VERSION = "0.12.13"
UV_ARCHIVE_URL = "https://github.com/astral-sh/uv/releases/download/0.12.13/uv-aarch64-apple-darwin.tar.gz"
UV_ARCHIVE_SHA256 = "7e6ddb9316acc00f2296c82ff4d99977870ee34b2f0ddcae9444d714db9364ed"
UV_EXECUTABLE_SHA256 = "964e82d2f7736a5dd3c8d4dfe299697286c655ff44c9ae3802886f27c5bf4d4a"
UV_LICENSE_SHA256 = {"licenses/uv-LICENSE-APACHE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
                     "licenses/uv-LICENSE-MIT": "860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c"}
MAX_TOOLCHAIN_FILES = 16384
MAX_TOOLCHAIN_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOOLCHAIN_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_DEPENDENCY_FILES = 65536
MAX_DEPENDENCY_BYTES = 2 * 1024 * 1024 * 1024
_OPERATIONS = frozenset({"dependency_export", "dependency_install", "private_python_version", "private_uv_version",
                         "private_python_metadata", "onnx_model_validation"})


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: int
    operation: str
    stdout_limit: int = 4096
    memory_mb: int = 2048
    disk_limit_bytes: int = 4 * 1024 * 1024 * 1024
    network_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        if (self.operation not in _OPERATIONS or not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= 256
                or any(not isinstance(value, str) or "\0" in value or len(value) > 16384 for value in self.argv)
                or sum(len(value) for value in self.argv) > 65536 or not Path(self.argv[0]).is_absolute()
                or not isinstance(self.cwd, Path) or not self.cwd.is_absolute()
                or any(type(value) is not int for value in (self.timeout_seconds, self.stdout_limit, self.memory_mb,
                                                           self.disk_limit_bytes))
                or not 1 <= self.timeout_seconds <= 600
                or not 1 <= self.stdout_limit <= 65536 or not 128 <= self.memory_mb <= 4096
                or not 1 <= self.disk_limit_bytes <= 8 * 1024 * 1024 * 1024
                or not isinstance(self.env, dict) or len(self.env) > 32
                or any(not isinstance(key, str) or not isinstance(value, str) or "\0" in key + value
                       for key, value in self.env.items())
                or not isinstance(self.network_hosts, tuple) or len(self.network_hosts) > 64
                or any(not re.fullmatch(r"[a-z0-9.-]{1,253}", host) for host in self.network_hosts)):
            raise PreparationError("invalid_preparation_command")


@dataclass(frozen=True)
class PrivateToolchain:
    root: Path
    uv: Path
    python_runtime: Path
    identity: str
    artifacts: dict


@dataclass(frozen=True)
class WheelRequest:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    size: int


def _digest(path: Path, check_canceled: Callable[[], None] = lambda: None) -> str:
    value = hashlib.sha256()
    with _opened(path) as stream:
        while chunk := stream.read(1024 * 1024):
            check_canceled()
            value.update(chunk)
    return value.hexdigest()


@contextmanager
def _opened(path: Path):
    """Hold no-follow ancestry and file identity while streaming copied bytes."""
    parent_fd = None
    try:
        if not path.is_absolute():
            raise PreparationError("unsafe_preparation_artifact")
        parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_TOOLCHAIN_BYTES
                    or before.st_mode & 0o6000):
                raise PreparationError("unsafe_preparation_artifact")
            yield stream
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
            if any(getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(current, key)
                   for key in fields):
                raise PreparationError("preparation_artifact_changed")
    except OSError:
        raise PreparationError("unsafe_preparation_artifact") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _json(data: bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise PreparationError("invalid_preparation_toolchain")
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(PreparationError("invalid_preparation_toolchain")))
    except (ValueError, RecursionError, UnicodeError):
        raise PreparationError("invalid_preparation_toolchain") from None


def _regular(path: Path, root: Path) -> os.stat_result:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise PreparationError("unsafe_preparation_toolchain")
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_mode & 0o6000:
        raise PreparationError("unsafe_preparation_toolchain")
    return details


def load_private_toolchain(host_root: Path, check_canceled: Callable[[], None] = lambda: None) -> PrivateToolchain:
    """Verify the entire private closure before executing any of its artifacts."""
    try:
        if not host_root.is_absolute() or host_root.resolve() != host_root:
            raise PreparationError("private_preparation_toolchain_required")
        root = host_root / "preparation-toolchain"
        if root.is_symlink() or not root.is_dir():
            raise PreparationError("private_preparation_toolchain_required")
        path = root / "toolchain.json"
        if _regular(path, root).st_size > MAX_TOOLCHAIN_MANIFEST_BYTES:
            raise PreparationError("preparation_toolchain_limit")
        data = _read_artifact(path, MAX_TOOLCHAIN_MANIFEST_BYTES)
        manifest = _json(data)
        if (not isinstance(manifest, dict)
                or set(manifest) != {"schema_version", "platform", "uv_version", "python_version", "mcp_version",
                                     "origin", "licenses", "artifacts"}
                or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
                or manifest.get("platform") != "aarch64-apple-darwin"
                or manifest.get("uv_version") != UV_VERSION or manifest.get("python_version") != PYTHON_VERSION
                or manifest.get("mcp_version") != MCP_VERSION):
            raise PreparationError("unsupported_preparation_toolchain")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= MAX_TOOLCHAIN_FILES:
            raise PreparationError("invalid_preparation_toolchain")
        total = 0
        for relative, record in artifacts.items():
            check_canceled()
            package_relative_path(relative)
            if (not isinstance(record, dict) or set(record) != {"size", "sha256", "executable"}
                    or type(record["size"]) is not int or not 0 <= record["size"] <= MAX_TOOLCHAIN_BYTES
                    or type(record["executable"]) is not bool
                    or not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                raise PreparationError("invalid_preparation_toolchain")
            total += record["size"]
            if total > MAX_TOOLCHAIN_BYTES:
                raise PreparationError("preparation_toolchain_limit")
            artifact = root / relative
            details = _regular(artifact, root)
            if (details.st_size != record["size"] or bool(details.st_mode & 0o111) != record["executable"]
                    or _digest(artifact, check_canceled) != record["sha256"]):
                raise PreparationError("preparation_toolchain_changed")
        found = set()
        pending = [root]
        entries = 0
        while pending:
            directory = pending.pop()
            for artifact in directory.iterdir():
                check_canceled()
                entries += 1
                if entries > MAX_TOOLCHAIN_FILES * 2:
                    raise PreparationError("preparation_toolchain_limit")
                details = artifact.lstat()
                if stat.S_ISDIR(details.st_mode):
                    pending.append(artifact)
                elif stat.S_ISREG(details.st_mode):
                    found.add(artifact.relative_to(root).as_posix())
                else:
                    raise PreparationError("unsafe_preparation_toolchain")
        if found != set(artifacts) | {"toolchain.json"}:
            raise PreparationError("preparation_toolchain_changed")
        required = {"bin/uv", "python/bin/python3.12", "python/lib/python3.12/LICENSE.txt",
                    "templates/prepare.py", "templates/LICENSE", "templates/python-function-v1/adapter.py",
                    "licenses/uv-LICENSE-APACHE", "licenses/uv-LICENSE-MIT", "licenses/NOTICE.md"}
        if not required <= set(artifacts) or any(not artifacts[name]["size"] for name in required):
            raise PreparationError("private_preparation_toolchain_required")
        if (not isinstance(manifest["origin"], dict)
                or set(manifest["origin"]) != {"uv_archive", "uv_archive_sha256", "uv_executable_sha256", "python"}
                or not isinstance(manifest["licenses"], list) or not 1 <= len(manifest["licenses"]) <= 64
                or len(set(manifest["licenses"])) != len(manifest["licenses"])
                or any(name not in artifacts or not artifacts[name]["size"] for name in manifest["licenses"])):
            raise PreparationError("invalid_preparation_toolchain")
        origin = manifest["origin"]
        if (origin["uv_archive"] != UV_ARCHIVE_URL or origin["uv_archive_sha256"] != UV_ARCHIVE_SHA256
                or origin["uv_executable_sha256"] != UV_EXECUTABLE_SHA256
                or any(artifacts[name]["sha256"] != expected for name, expected in UV_LICENSE_SHA256.items())):
            raise PreparationError("preparation_toolchain_origin_mismatch")
        if not artifacts["bin/uv"]["executable"] or not artifacts["python/bin/python3.12"]["executable"]:
            raise PreparationError("private_preparation_toolchain_required")
        return PrivateToolchain(root, root / "bin/uv", root / "python", hashlib.sha256(data).hexdigest(), artifacts)
    except (OSError, PackageError, KeyError, TypeError):
        raise PreparationError("invalid_preparation_toolchain") from None


def _copy(source: Path, target: Path, check_canceled: Callable[[], None]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = source.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise PreparationError("unsafe_preparation_artifact")
    with _opened(source) as incoming, target.open("xb") as outgoing:
        while chunk := incoming.read(1024 * 1024):
            check_canceled()
            outgoing.write(chunk)
    target.chmod(0o700 if details.st_mode & 0o111 else 0o600)


def _lock_packages(lock: Path) -> list:
    if lock.stat().st_size > 16 * 1024 * 1024:
        raise PreparationError("dependency_lock_limit")
    try:
        value = tomllib.loads(lock.read_text())
    except (OSError, ValueError, UnicodeError):
        raise PreparationError("invalid_dependency_lock") from None
    packages = value.get("package")
    if not isinstance(packages, list) or not 1 <= len(packages) <= 4096:
        raise PreparationError("invalid_dependency_lock")
    return packages


def _requirements(path: Path) -> list[tuple[Requirement, set[str]]]:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise PreparationError("dependency_requirements_limit")
    statements = []
    current = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        current += " " + line.rstrip("\\").strip()
        if line.endswith("\\"):
            continue
        statements.append(current.strip())
        current = ""
    if current or not statements:
        raise PreparationError("invalid_locked_requirements")
    result = []
    for requirement in statements:
        # Direct URL/local/editable requirements need a separately admitted
        # dependency recipe. This path supports registry wheels only.
        if (not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+", requirement)
                or " @ " in requirement or "--hash=sha256:" not in requirement
                or re.search(r"--(?!hash=sha256:[0-9a-f]{64}(?:\s|$))", requirement)):
            raise PreparationError("unsupported_locked_dependency")
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", requirement))
        try:
            parsed = Requirement(re.sub(r"\s+--hash=sha256:[0-9a-f]{64}", "", requirement))
        except InvalidRequirement:
            raise PreparationError("unsupported_locked_dependency") from None
        specs = list(parsed.specifier)
        if parsed.url or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise PreparationError("unsupported_locked_dependency")
        result.append((parsed, hashes))
    return result


def _wheel_requests(requirements: Path, lock: Path, environment: dict) -> tuple[WheelRequest, ...]:
    """Select one compatible immutable wheel for each active frozen requirement."""
    packages = _lock_packages(lock)
    marker_environment = environment["markers"]
    version = environment["macos_version"]
    platforms = list(mac_platforms(version=tuple(version), arch="arm64"))
    tags = list(cpython_tags((3, 12), abis=("cp312",), platforms=platforms))
    tags.extend(compatible_tags((3, 12), interpreter="cp312", platforms=platforms))
    rank = {tag: index for index, tag in reversed(list(enumerate(tags)))}
    selected = []
    names = set()
    for requirement, hashes in _requirements(requirements):
        if requirement.marker and not requirement.marker.evaluate(marker_environment):
            continue
        name = canonicalize_name(requirement.name)
        requested_version = next(iter(requirement.specifier)).version
        if name in names:
            raise PreparationError("ambiguous_locked_dependency")
        names.add(name)
        records = [item for item in packages if canonicalize_name(item.get("name", "")) == name
                   and item.get("version") == requested_version]
        if len(records) != 1 or set(records[0].get("source", {})) != {"registry"}:
            raise PreparationError("unsupported_locked_dependency")
        candidates = []
        for wheel in records[0].get("wheels", []):
            if not isinstance(wheel, dict):
                raise PreparationError("invalid_dependency_lock")
            url = wheel.get("url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                port = parsed.port if parsed is not None else None
            except ValueError:
                raise PreparationError("unsupported_dependency_locator") from None
            if (parsed is None or parsed.scheme != "https" or parsed.username or parsed.password
                    or not parsed.hostname or port not in {None, 443} or parsed.fragment or parsed.query):
                raise PreparationError("unsupported_dependency_locator")
            filename = Path(parsed.path).name
            if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,256}\.whl", filename):
                raise PreparationError("unsupported_dependency_locator")
            try:
                wheel_name, wheel_version, _, wheel_tags = parse_wheel_filename(filename)
            except ValueError:
                raise PreparationError("invalid_locked_wheel") from None
            compatible = [rank[tag] for tag in wheel_tags if tag in rank]
            if not compatible:
                continue
            hashed = wheel.get("hash")
            size = wheel.get("size")
            if (wheel_name != name or str(wheel_version) != requested_version
                    or not isinstance(hashed, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", hashed)
                    or hashed[7:] not in hashes or type(size) is not int or not 1 <= size <= MAX_DEPENDENCY_BYTES):
                raise PreparationError("invalid_locked_wheel")
            candidates.append((min(compatible), filename, WheelRequest(name, requested_version, filename, url, hashed[7:], size)))
        if not candidates:
            raise PreparationError("compatible_locked_wheel_required")
        selected.append(min(candidates, key=lambda candidate: candidate[:2])[2])
    if not selected or len(selected) > 512 or sum(item.size for item in selected) > MAX_DEPENDENCY_BYTES:
        raise PreparationError("dependency_artifact_limit")
    return tuple(selected)


def _dependency_notices(site: Path, output: Path, check_canceled: Callable[[], None]) -> None:
    """Read metadata only; retain actual wheel licenses and record each identity."""
    distributions = sorted(site.glob("*.dist-info"))
    if not distributions or len(distributions) > 512:
        raise PreparationError("dependency_notice_required")
    records = []
    mcp_version = None
    count = total = 0
    for path in site.rglob("*"):
        check_canceled()
        if path.is_symlink():
            raise PreparationError("unsafe_dependency_artifact")
        if path.is_file():
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise PreparationError("unsafe_dependency_artifact")
            count += 1
            total += details.st_size
            if count > MAX_DEPENDENCY_FILES or total > MAX_DEPENDENCY_BYTES:
                raise PreparationError("dependency_artifact_limit")
    for distribution in distributions:
        check_canceled()
        metadata_path = distribution / "METADATA"
        if not metadata_path.is_file() or metadata_path.stat().st_size > 1024 * 1024:
            raise PreparationError("dependency_notice_required")
        metadata = BytesParser().parsebytes(metadata_path.read_bytes())
        name, version = metadata.get("Name"), metadata.get("Version")
        if not name or not version or len(name + version) > 512:
            raise PreparationError("dependency_notice_required")
        license_files = []
        declared = metadata.get_all("License-File", [])
        for relative in declared:
            try:
                package_relative_path(relative)
            except PackageError:
                raise PreparationError("dependency_notice_required") from None
            candidates = [distribution / "licenses" / relative, distribution / relative]
            selected = next((candidate for candidate in candidates if candidate.is_file() and candidate.stat().st_size), None)
            if selected is None:
                raise PreparationError("dependency_notice_required")
            license_files.append(selected)
        # Older wheels predate License-File but retain their text conventionally.
        if not declared:
            license_files = [path for path in distribution.rglob("*") if path.is_file() and path.stat().st_size
                             and re.match(r"(?i)^(?:licen[cs]e|copying|notice)(?:[._-]|$)", path.name)]
        if not license_files:
            raise PreparationError("dependency_notice_required")
        if name.lower().replace("_", "-") == "mcp":
            mcp_version = version
        records.append({"name": name, "version": version,
                        "license_expression": metadata.get("License-Expression"),
                        "license_files": [{"path": path.relative_to(site).as_posix(), "sha256": _digest(path, check_canceled)}
                                          for path in sorted(set(license_files))]})
    if mcp_version != MCP_VERSION:
        raise PreparationError("unsupported_mcp_sdk_version")
    (output / "dependency-notices.json").write_text(json.dumps({"schema_version": 1, "distributions": records},
                                                              sort_keys=True, indent=2) + "\n")


def _runtime(destination: Path, dependency_project: Path, toolchain: PrivateToolchain,
             run: Callable[[CommandSpec], str], check_canceled: Callable[[], None], work: Path,
             acquire_wheels: Callable[[tuple[WheelRequest, ...], Path, Callable[[], None]], None] | None) -> str:
    check_canceled()
    if not (dependency_project / "uv.lock").is_file() or not (dependency_project / "pyproject.toml").is_file():
        raise PreparationError("locked_dependency_project_required")
    for relative, record in toolchain.artifacts.items():
        if relative.startswith("python/"):
            check_canceled()
            _copy(toolchain.root / relative, destination / relative, check_canceled)
            if _digest(destination / relative, check_canceled) != record["sha256"]:
                raise PreparationError("preparation_toolchain_changed")
    for name in ("home", "cache", "tmp"):
        (work / name).mkdir(mode=0o700, exist_ok=True)
    python = destination / "python/bin/python3.12"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work / "home"), "TMPDIR": str(work / "tmp"),
           "UV_CACHE_DIR": str(work / "cache"), "UV_PYTHON": str(python), "UV_PYTHON_DOWNLOADS": "never",
           "UV_NO_CONFIG": "1", "UV_NO_ENV_FILE": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONSAFEPATH": "1", "LC_ALL": "C", "LANG": "C"}
    def command(argv, operation, *, timeout=60):
        check_canceled()
        result = run(CommandSpec(tuple(map(str, argv)), work, dict(env), timeout, operation))
        check_canceled()
        if not isinstance(result, str) or len(result.encode()) > 4096:
            raise PreparationError("preparation_command_output_limit")
        return result.strip()
    uv_version = command((toolchain.uv, "--version"), "private_uv_version")
    if not re.fullmatch(r"uv " + re.escape(UV_VERSION) + r"(?: \([^\n]{1,128}\))?", uv_version):
        raise PreparationError("unsupported_private_uv_version")
    requirements = destination / "requirements.txt"
    command((toolchain.uv, "export", "--quiet", "--project", dependency_project, "--frozen", "--offline", "--no-dev",
             "--no-emit-project", "--no-header", "--format", "requirements.txt", "--output-file", requirements),
            "dependency_export")
    if acquire_wheels is None:
        raise PreparationError("controlled_dependency_acquisition_required")
    metadata_code = (
        "import json,os,platform,sys; "
        "print(json.dumps({'markers': {'implementation_name':sys.implementation.name,"
        "'implementation_version':platform.python_version(),'os_name':os.name,"
        "'platform_machine':platform.machine(),'platform_release':platform.release(),"
        "'platform_system':platform.system(),'platform_version':platform.version(),"
        "'python_full_version':platform.python_version(),'platform_python_implementation':platform.python_implementation(),"
        "'python_version':'.'.join(map(str,sys.version_info[:2])),'sys_platform':sys.platform},"
        "'macos_version':list(map(int,platform.mac_ver()[0].split('.')[:2]))}))"
    )
    metadata = _json(command((python, "-I", "-S", "-B", "-c", metadata_code), "private_python_metadata").encode())
    markers = metadata.get("markers", {})
    macos_version = metadata.get("macos_version")
    if (markers.get("python_full_version") != PYTHON_VERSION or markers.get("implementation_name") != "cpython"
            or markers.get("platform_system") != "Darwin" or markers.get("platform_machine") != "arm64"
            or not isinstance(macos_version, list) or len(macos_version) != 2
            or any(type(item) is not int or not 0 <= item <= 99 for item in macos_version)
            or macos_version[0] < 26):
        raise PreparationError("unsupported_private_python_platform")
    requests = _wheel_requests(requirements, dependency_project / "uv.lock", metadata)
    wheels = work / "wheels"
    wheels.mkdir(mode=0o700)
    acquire_wheels(requests, wheels, check_canceled)
    check_canceled()
    if {path.name for path in wheels.iterdir()} != {item.filename for item in requests}:
        raise PreparationError("dependency_acquisition_identity_mismatch")
    for item in requests:
        path = wheels / item.filename
        if _regular(path, wheels).st_size != item.size or _digest(path, check_canceled) != item.sha256:
            raise PreparationError("dependency_acquisition_identity_mismatch")
    command((toolchain.uv, "pip", "install", "--quiet", "--python", python, "--target", destination / "python/site-packages",
             "--offline", "--no-index", "--find-links", wheels,
             # uv treats --no-build and --only-binary as mutually exclusive.
             # :all: refuses every source distribution and build hook.
             "--require-hashes", "--only-binary", ":all:", "--no-deps", "--link-mode", "copy",
             "--requirement", requirements), "dependency_install", timeout=600,
            )
    _dependency_notices(destination / "python/site-packages", destination, check_canceled)
    _copy(dependency_project / "uv.lock", destination / "uv.lock", check_canceled)
    _copy(dependency_project / "pyproject.toml", destination / "dependency-project.toml", check_canceled)
    _copy(toolchain.python_runtime / "lib/python3.12/LICENSE.txt", destination / "licenses/python-LICENSE.txt", check_canceled)
    (destination / "licenses/DEPENDENCIES.md").write_text(
        "# Locked Python dependencies\n\nPrivate CPython " + PYTHON_VERSION + " is licensed in python-LICENSE.txt.\n\n"
        "Exact wheel versions and SHA-256 hashes are retained in uv.lock and requirements.txt. "
        "dependency-notices.json records every installed distribution and retained license text identity. "
        "Wheel licenses and notices remain inside python/site-packages/*.dist-info.\n")
    python_version = command((python, "-I", "-S", "-B", "--version"), "private_python_version")
    if python_version != "Python " + PYTHON_VERSION:
        raise PreparationError("unsupported_private_python_version")
    (destination / "python-runtime-origin.json").write_text(json.dumps({
        "python_version": PYTHON_VERSION, "managed_interpreter_sha256": _digest(python, check_canceled),
        "uv_version": UV_VERSION, "uv_sha256": _digest(toolchain.uv, check_canceled),
        "toolchain_digest": toolchain.identity, "uv_lock_sha256": _digest(destination / "uv.lock", check_canceled),
        "mcp_sdk_version": MCP_VERSION, "installation": "hash-checked frozen wheels; copied private runtime",
    }, indent=2) + "\n")
    return python_version


def prepare_bound(descriptor, draft: dict, toolchain: PrivateToolchain, output: Path,
                  run: Callable[[CommandSpec], str], check_canceled: Callable[[], None], *,
                  acquire_wheels: Callable[[tuple[WheelRequest, ...], Path, Callable[[], None]], None] | None = None,
                  resolved=None):
    """Build one validated candidate. Job receipt/probing/attachment stay runner-owned."""
    try:
        from .preparation_inputs import parse_preparation_descriptor, descriptor_tools
        value = parse_preparation_descriptor(descriptor)
        candidate = None
        if value.schema_version == 2:
            from .preparation_resolved_python import resolved_input, validate_resolution
            if resolved is None:
                raise PreparationError("resolution_result_unavailable")
            result, derived_path = resolved
            selected_input = resolved_input(value)
            candidate = validate_resolution(value, draft, result)
            if result.request.runtime.digest != toolchain.identity or value.artifacts:
                raise PreparationError("resolution_runtime_mismatch")
        if not output.is_absolute() or output.parent.resolve() != output.parent or output.exists() or output.parent.is_symlink():
            raise PreparationError("private_preparation_workspace_required")
        check_canceled()
        # A toolchain object is a validated immutable binding, never a PATH hint.
        current = load_private_toolchain(toolchain.root.parent, check_canceled)
        if current.identity != toolchain.identity:
            raise PreparationError("preparation_toolchain_changed")
        work = output.parent / (output.name + "-builder")
        work.mkdir(mode=0o700)
        exported = work / "setup-export.json"
        exported.write_text(json.dumps({"api_version": 1, "draft_id": draft["draft_id"], "revision": draft["revision"],
                                       "source": draft["source"], "requirements": draft["requirements"]}) + "\n")
        tool_spec = work / "tools.json"
        tool_spec.write_text(json.dumps([tool.model_dump(exclude_none=True) for tool in descriptor_tools(value)]) + "\n")
        access_spec = work / "access.json"
        access_spec.write_text(json.dumps(value.access.model_dump()) + "\n")
        validation_spec = work / "validation.json"
        # The managed verifier executes the full descriptor fixture set; schema-1
        # install-time smoke calls have a separate four-fixture contract.
        validation_spec.write_text(json.dumps({"fixtures": [{"tool": item.tool, "arguments": item.arguments,
                                                             "expected": item.expected} for item in value.validation[:4]]}) + "\n")
        module_name = "mcpme._private_preparation_generator"
        spec = importlib.util.spec_from_file_location(module_name, toolchain.root / "templates/prepare.py")
        if spec is None or spec.loader is None:
            raise PreparationError("private_preparation_toolchain_required")
        generator = importlib.util.module_from_spec(spec)
        # This is pinned host implementation code. Selected source is only read
        # as exact snapshot files by the generator and is never imported here.
        sys.modules[module_name] = generator
        implementation = _read_artifact(toolchain.root / "templates/prepare.py", 1024 * 1024)
        if hashlib.sha256(implementation).hexdigest() != current.artifacts["templates/prepare.py"]["sha256"]:
            raise PreparationError("preparation_toolchain_changed")
        # Importlib's source loader can create __pycache__ beside the sealed
        # implementation. Execute its already verified source without a cache.
        exec(compile(implementation, str(toolchain.root / "templates/prepare.py"), "exec"), generator.__dict__)
        if candidate is None:
            kind, module, function = value.interface.kind, value.interface.module, getattr(value.interface, "function", None)
            source_license, project = value.source_license, output / "source" / value.dependencies.project
            lock_digest = value.dependencies.lock_sha256
        else:
            kind, module, function = candidate["kind"], candidate["module"], candidate.get("function")
            source_license, project = selected_input.source_license, work / "derived"
            project.mkdir(mode=0o700)
            from .preparation_resolution import RESULT_FILES
            from .recipes import _read
            for item in result.derived_inputs:
                check_canceled()
                name = RESULT_FILES[item.role]
                data = _read(derived_path / name, 16 * 1024 * 1024)
                if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
                    raise PreparationError("resolution_result_changed")
                (project / name).write_bytes(data)
            lock_digest = next(item.sha256 for item in result.derived_inputs if item.role == "dependency-lock")
            if kind == "python-mcp":
                original_launcher = generator.write_launcher
                def resolved_launcher(destination, **kwargs):
                    original_launcher(destination, **kwargs)
                    bootstrap = destination / "bootstrap.py"
                    lines = bootstrap.read_text().splitlines()
                    # The same exact declared foreground entry used by SDK
                    # discovery; the original source tree remains untouched.
                    lines = lines[:-1]
                    lines += ["sys.path.insert(1, str(root / 'source' / " + repr(candidate["root"]) + "))",
                        "os.chdir(root / 'source' / " + repr(candidate["root"]) + ")"]
                    pieces = candidate["declared_entrypoint"].split(":")
                    if len(pieces) == 2:
                        lines += ["import importlib", "getattr(importlib.import_module(" + repr(pieces[0]) + "), " + repr(pieces[1]) + ")()"]
                    else:
                        lines += ["runpy.run_module(" + repr(pieces[0]) + ", run_name='__main__', alter_sys=True)"]
                    bootstrap.write_text("\n".join(lines) + "\n")
                generator.write_launcher = resolved_launcher
        args = argparse.Namespace(template="python-function" if kind == "python-callable" else "existing-mcp",
            setup_export=exported, tool_spec=tool_spec, output=output, package_id=value.package.id,
            name=value.package.name, version=value.package.version, source_license=source_license,
            dependency_project=project,
            memory_mb=value.resources.memory_mb, max_sessions=value.lifecycle.max_sessions,
            timeout_seconds=value.lifecycle.operation_timeout_seconds, access_spec=access_spec,
            configuration_spec=None, validation_spec=validation_spec, entrypoint_arg=[], extra_executable=[],
            module=module, function=function, script=None, executable=None,
            http_factory=None)
        def build_runtime(destination, project):
            if _digest(project / "uv.lock", check_canceled) != lock_digest:
                raise PreparationError("dependency_lock_mismatch")
            return _runtime(destination, project, current, run, check_canceled, work, acquire_wheels)
        package = generator.prepare(args, runtime_builder=build_runtime, check_canceled=check_canceled,
                                    lifecycle_override=value.lifecycle.model_dump())
        check_canceled()
        return package
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, (PreparationError, PackageError)):
            raise
        if error.__class__.__name__ == "PreparationError" and re.fullmatch(r"[a-z][a-z0-9_]{1,127}", str(error)):
            raise PreparationError(str(error)) from None
        raise PreparationError("package_preparation_failed") from None
