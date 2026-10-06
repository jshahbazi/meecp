"""Host-owned wheel-only Python dependency setup for a Pi staging workspace.

The agent supplies ordinary project metadata, never installer commands, index
configuration, credentials or a build hook. Resolution reaches public PyPI
through the authenticated bounded index broker. Installation consumes exact
hash-bound wheels offline and copies the verified private Python closure.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as ThreadCancelledError
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import tempfile
import threading
import time
import tomllib

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from .preparation_acquisition import WheelAcquirer
from .preparation_builder import MCP_VERSION, PYTHON_VERSION, _runtime, load_private_toolchain
from .preparation_contracts import PreparationError
from .preparation_processes import PreparationExecutor
from .preparation_resolution_network import ResolutionIndexBroker
from .processes import OwnershipLost, _await_ownership

MAX_PROJECT_BYTES = 1024 * 1024
MAX_LOCK_BYTES = 16 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 600
SANDBOX = Path("/usr/bin/sandbox-exec")
MARKER = ".mcpme-dependencies.json"


def dependency_project(data: bytes) -> bytes:
    """Reduce untrusted pyproject metadata to a static wheel dependency list."""
    try:
        if not 0 < len(data) <= MAX_PROJECT_BYTES:
            raise ValueError()
        document = tomllib.loads(data.decode("utf-8"))
        project = document.get("project")
        if not isinstance(project, dict) or project.get("dynamic"):
            raise ValueError()
        name = project.get("name", "mcpme-pi-app")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise ValueError()
        version = project.get("version", "1.0.0")
        if not isinstance(version, str) or len(version) > 128:
            raise ValueError()
        Version(version)
        requires_python = project.get("requires-python", ">=3.12")
        if not isinstance(requires_python, str) or len(requires_python) > 256 or PYTHON_VERSION not in SpecifierSet(requires_python):
            raise PreparationError("pi_dependency_python_version_unsupported")
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or len(dependencies) > 128:
            raise ValueError()
        for raw in dependencies:
            if not isinstance(raw, str) or not 0 < len(raw) <= 4096:
                raise ValueError()
            requirement = Requirement(raw)
            if requirement.url:
                raise PreparationError("pi_dependency_direct_url_unsupported")
            if canonicalize_name(requirement.name) == "mcp" and MCP_VERSION not in requirement.specifier:
                raise PreparationError("pi_dependency_mcp_version_unsupported")
        # mcpme's generated adapters use the host-pinned MCP SDK. Even a source
        # app with no other dependencies receives this explicit integration input.
        dependencies = [*dependencies, "mcp==" + MCP_VERSION]
        return ('[project]\nname = ' + json.dumps(name) + '\nversion = ' + json.dumps(version)
                + '\nrequires-python = "==' + PYTHON_VERSION + '"\ndependencies = '
                + json.dumps(dependencies) + '\n').encode("utf-8")
    except PreparationError:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise PreparationError("pi_dependency_project_unsupported") from None


def _read_file(fd: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                or not 0 < before.st_size <= limit):
            raise PreparationError("pi_dependency_file_unsafe")
        value = bytearray()
        while len(value) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=fd, follow_symlinks=False)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_nlink,
                                 item.st_uid, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(named) or len(value) != before.st_size:
            raise PreparationError("pi_dependency_source_changed")
        return bytes(value)
    finally:
        os.close(descriptor)


def _profile(work: Path, toolchain, executable: Path, python: Path, *, port: int | None = None) -> str:
    """Permit only pinned installer/version-check programs and one broker port."""
    paths = (work, toolchain.python_runtime, executable, python)
    if any(not item.is_absolute() or item.resolve() != item for item in paths):
        raise PreparationError("pi_dependency_sandbox_path_unsafe")
    quote = lambda item: json.dumps(str(item), ensure_ascii=True)
    ancestors = sorted({ancestor for item in paths for ancestor in item.parents}, key=str)
    lines = ["(version 1)", "(deny default (with no-log))", "(allow sysctl-read)",
        "(allow file-read-metadata " + " ".join("(literal " + quote(item) + ")" for item in ancestors) + ")",
        '(allow file-read* (literal "/") (subpath "/System/Library") (subpath "/System/Cryptexes/OS/usr/lib") '
        '(subpath "/System/Volumes/Preboot/Cryptexes/OS/usr/lib") (subpath "/usr/lib") (subpath "/usr/share") '
        '(subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") '
        + " ".join("(subpath " + quote(item) + ")" for item in (work, toolchain.python_runtime))
        + " (literal " + quote(toolchain.uv) + "))",
        "(allow file-write* (subpath " + quote(work) + ') (literal "/dev/null"))',
        "(allow process-exec (literal " + quote(executable) + ") (literal " + quote(python) + "))",
        "(allow process-fork)", "(deny syscall-unix (syscall-number 82 147) (with no-log))"]
    # uv's trusted interpreter checks use posix_spawn. Only the two exact
    # verified programs above can execute; no source/build program is admitted.
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise PreparationError("pi_dependency_broker_invalid")
        lines.append(f'(allow network-outbound (remote tcp "localhost:{port}"))')
    return "\n".join(lines)


def _write_file(fd: int, name: str, data: bytes) -> None:
    temporary = ".mcpme-lock-" + os.urandom(12).hex()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _versions(directory: Path) -> list[dict[str, str]]:
    records = json.loads((directory / "dependency-notices.json").read_bytes())["distributions"]
    if not isinstance(records, list) or not 1 <= len(records) <= 512:
        raise PreparationError("pi_dependency_metadata_invalid")
    result = []
    for item in records:
        name, version = item["name"], item["version"]
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
                or not isinstance(version, str) or len(version) > 128):
            raise PreparationError("pi_dependency_metadata_invalid")
        Version(version)
        result.append({"name": canonicalize_name(name), "version": version})
    return sorted(result, key=lambda item: item["name"])


class _Installer:
    def __init__(self, broker, host_root):
        self.broker, self.host_root = broker, Path(host_root)
        self.deadline = time.monotonic() + MAX_SECONDS
        self.canceled = threading.Event()
        self.operations = set()
        self.executor = PreparationExecutor(broker.owner.reaper, owner=broker.owner)

    def check(self):
        if self.canceled.is_set() or self.broker._closed:
            raise PreparationError("pi_dependency_canceled")
        if time.monotonic() >= self.deadline:
            raise PreparationError("pi_dependency_timeout")

    async def controlled(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.operations.add(task)
        try:
            return await task
        finally:
            self.operations.discard(task)

    async def thread(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.canceled.set()
            for operation in tuple(self.operations):
                operation.cancel()
            await _await_ownership(asyncio.gather(task, *tuple(self.operations), return_exceptions=True))
            raise

    async def execute(self, work, toolchain, argv, *, env_extra=None, index=None, stdout_limit=4096):
        self.check()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(work / "home"), "TMPDIR": str(work / "tmp"),
            "UV_CACHE_DIR": str(work / "cache"), "UV_PYTHON": str(toolchain.python_runtime / "bin/python3.12"),
            "UV_PYTHON_DOWNLOADS": "never", "UV_NO_ENV_FILE": "1", "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1", "LC_ALL": "C", "LANG": "C", **(env_extra or {})}
        profile = _profile(work, toolchain, Path(argv[0]), Path(env["UV_PYTHON"]), port=index.port if index else None)
        result = await self.controlled(self.executor.execute(SANDBOX, ["-p", profile, *map(str, argv)], work, env,
            timeout_seconds=min(MAX_SECONDS, self.deadline - time.monotonic()), stdout_limit=stdout_limit))
        self.check()
        return result.stdout.decode("utf-8")

    async def build(self, work: Path, source: bytes):
        self.check()
        toolchain = await self.thread(load_private_toolchain, self.host_root, self.check)
        derived, runtime, builder = work / "derived", work / "runtime", work / "builder"
        for item in (derived, runtime, builder, work / "home", work / "cache", work / "tmp"):
            item.mkdir(mode=0o700)
        (derived / "pyproject.toml").write_bytes(dependency_project(source))
        async with ResolutionIndexBroker(work / "index", byte_limit=MAX_DOWNLOAD_BYTES, check=self.check) as index:
            config = work / "uv.toml"
            config.write_text('[[index]]\nname = "mcpme-pi-dependencies"\nurl = ' + json.dumps(index.base + "/simple")
                              + '\ndefault = true\nauthenticate = "always"\n')
            try:
                await self.execute(work, toolchain, [toolchain.uv, "lock", "--quiet", "--project", derived,
                    "--config-file", config, "--no-sources", "--no-build", "--no-python-downloads", "--keyring-provider", "disabled"],
                    index=index, env_extra={"UV_INDEX_MCPME_PI_DEPENDENCIES_USERNAME": "mcpme",
                                            "UV_INDEX_MCPME_PI_DEPENDENCIES_PASSWORD": index.password})
            except PreparationError:
                if index.error_code:
                    raise PreparationError(index.error_code) from None
                raise
            fd = os.open(derived, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                lock = index.canonical_lock(_read_file(fd, "uv.lock", MAX_LOCK_BYTES))
            finally:
                os.close(fd)
            (derived / "uv.lock").write_bytes(lock)
        loop = asyncio.get_running_loop()
        def command(spec):
            self.check()
            future = asyncio.run_coroutine_threadsafe(self.execute(work, toolchain, spec.argv,
                env_extra=spec.env, stdout_limit=spec.stdout_limit), loop)
            try:
                return future.result()
            except ThreadCancelledError:
                raise PreparationError("pi_dependency_canceled") from None
        async def acquire(requests, destination, check):
            await self.controlled(WheelAcquirer().acquire(requests, destination,
                allowed_hosts=frozenset({"files.pythonhosted.org"}), disk_bytes=MAX_DOWNLOAD_BYTES,
                wall_seconds=self.deadline - time.monotonic(), check_canceled=check))
        def wheels(requests, destination, check):
            self.check()
            future = asyncio.run_coroutine_threadsafe(acquire(requests, destination, check), loop)
            try:
                future.result()
            except ThreadCancelledError:
                raise PreparationError("pi_dependency_canceled") from None
        await self.thread(_runtime, runtime, derived, toolchain, command, self.check, builder, wheels)
        self.check()
        return runtime, lock, await self.thread(_versions, runtime)


async def install_dependencies(broker, args: dict, host_root: Path | None) -> dict:
    """Build and atomically publish a private runtime; expose metadata only.

    The PiToolBroker owns mutation serialization and closes its ProcessOwner
    after this hook settles. This hook shares that owner without closing it.
    """
    work = None
    parent_fd = project_fd = None
    installer = None
    try:
        if (broker._closed or not isinstance(args, dict) or set(args) - {"ecosystem", "project_path", "target_path"}
                or args.get("ecosystem") not in {"python", "node"}):
            raise PreparationError("pi_dependency_arguments_invalid")
        if args["ecosystem"] == "node":
            return {"success": False, "output": "pi_node_dependencies_unavailable"}
        if host_root is None or platform.system() != "Darwin" or platform.machine() != "arm64" or not SANDBOX.is_file():
            raise PreparationError("pi_dependency_runtime_unavailable")
        project = broker._path(args.get("project_path"), write=True, directory=True)
        project_fd, project_leaf, _ = broker._parent(str(project / "pyproject.toml"), write=True)
        source = _read_file(project_fd, project_leaf, MAX_PROJECT_BYTES)
        dependency_project(source)  # Reject unsafe declarations before opening network or launching uv.
        value = args.get("target_path", str(project / ".mcpme-runtime"))
        parent_fd, leaf, target = broker._parent(value, write=True, create=True)
        if target == project or target in project.parents or target == broker.workspace:
            raise PreparationError("pi_dependency_target_invalid")
        existing = False
        try:
            details = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(details.st_mode):
                raise PreparationError("pi_dependency_target_invalid")
            marker_fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                marker = json.loads(_read_file(marker_fd, MARKER, 65536))
                if marker.get("schema_version") != 1 or marker.get("ecosystem") != "python":
                    raise PreparationError("pi_dependency_target_exists")
            finally:
                os.close(marker_fd)
            existing = True
        except FileNotFoundError:
            if os.path.lexists(target):
                raise PreparationError("pi_dependency_target_exists") from None
        installer = _Installer(broker, host_root)
        work = Path(tempfile.mkdtemp(prefix=".mcpme-dependencies-", dir=broker.workspace))
        async with asyncio.timeout(MAX_SECONDS):
            runtime, lock, versions = await installer.build(work, source)
            if _read_file(project_fd, project_leaf, MAX_PROJECT_BYTES) != source:
                raise PreparationError("pi_dependency_source_changed")
            result = {"schema_version": 1, "ecosystem": "python", "runtime_path": str(target),
                "python_path": str(target / "python/bin/python3.12"),
                "site_packages_path": str(target / "python/site-packages"), "uv_lock_path": str(target / "uv.lock"),
                "requirements_path": str(target / "requirements.txt"), "versions": versions,
                "pyproject_sha256": hashlib.sha256(source).hexdigest()}
            if len(json.dumps(result, sort_keys=True).encode()) > 65536:
                raise PreparationError("pi_dependency_metadata_limit")
            (runtime / MARKER).write_text(json.dumps(result, sort_keys=True) + "\n")
            _write_file(project_fd, "uv.lock", lock)
            previous = None
            if existing:
                previous = ".mcpme-previous-" + os.urandom(12).hex()
                os.rename(leaf, previous, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            try:
                os.rename(runtime, leaf, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            except BaseException:
                if previous is not None:
                    os.rename(previous, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                raise
            if previous is not None:
                shutil.rmtree(previous, dir_fd=parent_fd)
            return {"success": True, "output": json.dumps(result, sort_keys=True)}
    except asyncio.CancelledError:
        raise
    except OwnershipLost:
        raise
    except TimeoutError:
        return {"success": False, "output": "pi_dependency_timeout"}
    except PreparationError as error:
        return {"success": False, "output": error.code}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"success": False, "output": "pi_dependency_setup_failed"}
    finally:
        if installer is not None:
            installer.canceled.set()
            for operation in tuple(installer.operations):
                operation.cancel()
            await _await_ownership(asyncio.gather(*tuple(installer.operations), return_exceptions=True))
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        for fd in (project_fd, parent_fd):
            if fd is not None:
                os.close(fd)
