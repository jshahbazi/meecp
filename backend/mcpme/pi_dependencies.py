"""Host-owned dependency setup for a Pi staging workspace.

The agent supplies ordinary project metadata, never installer commands, index
configuration or credentials. Python resolution reaches public PyPI
through the authenticated bounded index broker. Installation consumes exact
hash-bound wheels offline and copies the verified private Python closure.
Node projects resolve a standard npm lock and install that lock without implicit
lifecycle scripts. Pi can then explicitly run project build steps in its sandbox.
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
import shlex
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
from .pi_environment import copy_regular_tree, stage_trust_bundle

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


def node_project(data: bytes) -> dict:
    """Accept ordinary npm metadata without an app-family template."""
    try:
        if not 0 < len(data) <= MAX_PROJECT_BYTES:
            raise ValueError()
        document = json.loads(data)
        if not isinstance(document, dict):
            raise ValueError()
        for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            dependencies = document.get(key, {})
            if not isinstance(dependencies, dict) or len(dependencies) > 1024:
                raise ValueError()
            for name, spec in dependencies.items():
                if not isinstance(name, str) or not isinstance(spec, str) or not 0 < len(name) <= 256 or not 0 < len(spec) <= 4096:
                    raise ValueError()
        return document
    except (ValueError, UnicodeError, TypeError):
        raise PreparationError("pi_node_project_invalid") from None


def _node_lock(data: bytes) -> dict:
    """Keep npm's public, integrity-bound dependency identities for provenance."""
    from urllib.parse import urlsplit
    try:
        if not 0 < len(data) <= MAX_LOCK_BYTES:
            raise ValueError()
        document = json.loads(data)
        packages = document.get("packages")
        if not isinstance(packages, dict) or len(packages) > 10000 or document.get("lockfileVersion") not in {2, 3}:
            raise ValueError()
        for path, package in packages.items():
            if not isinstance(path, str) or ".." in Path(path).parts or Path(path).is_absolute() or not isinstance(package, dict):
                raise ValueError()
            resolved = package.get("resolved")
            if resolved is not None:
                if not isinstance(resolved, str) or len(resolved) > 4096:
                    raise ValueError()
                parsed = urlsplit(resolved)
                if parsed.username is not None or parsed.password is not None:
                    raise PreparationError("pi_node_dependency_credentials_unsupported")
                if parsed.scheme in {"http", "https"} and (parsed.query or parsed.fragment):
                    raise PreparationError("pi_node_dependency_locator_unsupported")
        return document
    except PreparationError:
        raise
    except (ValueError, UnicodeError, TypeError, AttributeError):
        raise PreparationError("pi_node_lock_invalid") from None


def _node_bin_launchers(source: Path, target: Path, check=lambda: None) -> int:
    """Preserve CLI script location while publishing a regular-file closure.

    Copying a bin symlink's bytes into .bin changes __dirname and relative
    imports. A shell launcher instead executes the original project-relative
    target and prepends this project's relocated private Node interpreter.
    """
    source, target = source.resolve(strict=True), target.resolve(strict=True)
    count = 0
    for path in source.rglob("*"):
        check()
        if path.parent.name != ".bin" or not path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(source) or not resolved.is_file():
            raise PreparationError("pi_node_bin_target_unsafe")
        destination = target / path.relative_to(source)
        original = target / resolved.relative_to(source)
        if not original.is_file() or destination.is_symlink() or not destination.is_file():
            raise PreparationError("pi_node_bin_target_unsafe")
        relative = shlex.quote(os.path.relpath(original, destination.parent))
        private_node = shlex.quote(os.path.relpath(target / ".mcpme-node/bin", destination.parent))
        launcher = ("#!/bin/sh\n"
            'mcpme_bin_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
            + 'mcpme_private_node_dir="$mcpme_bin_dir"/' + private_node + "\n"
            + 'PATH="$mcpme_private_node_dir:$PATH"\nexport PATH\n'
            + 'exec "$mcpme_bin_dir"/' + relative + ' "$@"\n')
        destination.write_text(launcher)
        destination.chmod(0o755)
        count += 1
    return count


class _NodeInstaller(_Installer):
    async def build(self, work: Path, source: bytes, project: Path, target: Path):
        self.check()
        runtime, derived = work / "runtime", work / "project"
        for lock_name in ("package-lock.json", "npm-shrinkwrap.json"):
            source_lock = project / lock_name
            if source_lock.exists():
                parent, leaf, _ = self.broker._parent(str(source_lock), write=True)
                try:
                    _node_lock(_read_file(parent, leaf, MAX_LOCK_BYTES))
                finally:
                    os.close(parent)
        def copy_project():
            copy_regular_tree(project, derived, self.check, allowed_root=project, exclude_paths=(target,),
                exclude_names={"node_modules", ".git", ".npmrc", ".cache", ".npm", ".pnpm-store", ".home", ".tmp", ".trust"},
                exclude_private=True)
        await self.thread(copy_project)
        (derived / "package.json").write_bytes(source)
        host_runtime = self.host_root / "pi-runtime"
        node, npm = host_runtime / "bin/node", host_runtime / "npm/bin/npm-cli.js"
        if not node.is_file() or not npm.is_file():
            raise PreparationError("pi_node_installer_unavailable")
        config, global_config = work / "empty-user-npmrc", work / "empty-global-npmrc"
        config.write_bytes(b"")
        global_config.write_bytes(b"")
        common = [str(node), "--use-system-ca", str(npm), "--cache=" + str(work / "cache"),
            "--userconfig=" + str(config), "--globalconfig=" + str(global_config),
            "--registry=https://registry.npmjs.org/", "--ignore-scripts", "--no-audit", "--no-fund",
            "--no-update-notifier", "--logs-max=0", "--fetch-retries=1", "--fetch-timeout=60000",
            "--fetch-retry-mintimeout=1000", "--fetch-retry-maxtimeout=3000"]
        environment = {"PATH": str(host_runtime / "bin") + ":/usr/bin:/bin"}
        trust = await self.thread(stage_trust_bundle, work / "extra-ca.pem")
        if trust is not None:
            environment["NODE_EXTRA_CA_CERTS"] = str(trust)
        lock_name = "npm-shrinkwrap.json" if (derived / "npm-shrinkwrap.json").is_file() else "package-lock.json"
        # Node supports the platform trust store; no user npm configuration,
        # registry credential or provider secret enters this subprocess.
        for operation in ("install", "ci"):
            argv = [*common, operation, *(["--package-lock-only"] if operation == "install" else [])]
            result = await self.controlled(self.broker._run({"argv": argv, "cwd": str(derived),
                "timeout_seconds": min(MAX_SECONDS, max(1, int(self.deadline - time.monotonic()))),
                "network": True, "env": environment}))
            self.check()
            if not result.get("success"):
                # Diagnostics are returned in memory to Pi for repair. The host
                # never logs them or persists npm's debug history.
                return None, None, result
            lock = (derived / lock_name).read_bytes()
            _node_lock(lock)
        # Keep ordinary package-relative imports, workspace dependencies and
        # build scripts intact beside node_modules. The target is a complete
        # private project, rather than a detached dependency directory.
        await self.thread(lambda: copy_regular_tree(derived, runtime, self.check, allowed_root=derived))
        private_node = runtime / ".mcpme-node/bin"
        private_node.mkdir(mode=0o700, parents=True)
        shutil.copyfile(node, private_node / "node")
        (private_node / "node").chmod(0o755)
        bin_launchers = await self.thread(_node_bin_launchers, derived, runtime, self.check)
        if not (runtime / "node_modules").exists():
            (runtime / "node_modules").mkdir(mode=0o700)
        (runtime / ".mcpme-node/licenses").mkdir(mode=0o700)
        shutil.copyfile(host_runtime / "licenses/NODE-LICENSE.txt", runtime / ".mcpme-node/licenses/NODE-LICENSE.txt")
        shutil.copyfile(host_runtime / "provenance.json", runtime / ".mcpme-node/node-provenance.json")
        (runtime / lock_name).write_bytes(lock)
        versions = []
        lifecycle = []
        for relative, record in _node_lock(lock)["packages"].items():
            if not relative:
                continue
            name = record.get("name") or relative.rsplit("node_modules/", 1)[-1]
            version = record.get("version", "local")
            item = {"name": name, "version": version, "license": record.get("license", "unspecified")}
            for key in ("resolved", "integrity"):
                if key in record:
                    item[key] = record[key]
            versions.append(item)
            if record.get("hasInstallScript"):
                lifecycle.append(relative)
        (runtime / ".mcpme-node/dependency-notices.json").write_text(json.dumps({"schema_version": 1, "ecosystem": "node",
            "distributions": versions, "lifecycle_scripts_pending": lifecycle}, sort_keys=True) + "\n")
        return runtime, lock, {"versions": versions, "lifecycle_scripts_pending": lifecycle, "lock_name": lock_name,
                               "bin_launcher_count": bin_launchers}


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
        if host_root is None or platform.system() != "Darwin" or platform.machine() != "arm64" or not SANDBOX.is_file():
            raise PreparationError("pi_dependency_runtime_unavailable")
        ecosystem = args["ecosystem"]
        project = broker._path(args.get("project_path"), write=True, directory=True)
        manifest = "pyproject.toml" if ecosystem == "python" else "package.json"
        project_fd, project_leaf, _ = broker._parent(str(project / manifest), write=True)
        source = _read_file(project_fd, project_leaf, MAX_PROJECT_BYTES)
        (dependency_project if ecosystem == "python" else node_project)(source)
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
                if marker.get("schema_version") != 1 or marker.get("ecosystem") != ecosystem:
                    raise PreparationError("pi_dependency_target_exists")
            finally:
                os.close(marker_fd)
            existing = True
        except FileNotFoundError:
            if os.path.lexists(target):
                raise PreparationError("pi_dependency_target_exists") from None
        installer = (_Installer if ecosystem == "python" else _NodeInstaller)(broker, host_root)
        work = Path(tempfile.mkdtemp(prefix=".mcpme-dependencies-", dir=broker.workspace))
        async with asyncio.timeout(MAX_SECONDS):
            runtime, lock, details = await (installer.build(work, source) if ecosystem == "python"
                                          else installer.build(work, source, project, target))
            if runtime is None:
                return details
            if _read_file(project_fd, project_leaf, MAX_PROJECT_BYTES) != source:
                raise PreparationError("pi_dependency_source_changed")
            if ecosystem == "python":
                result = {"schema_version": 1, "ecosystem": "python", "runtime_path": str(target),
                "python_path": str(target / "python/bin/python3.12"),
                "site_packages_path": str(target / "python/site-packages"), "uv_lock_path": str(target / "uv.lock"),
                "requirements_path": str(target / "requirements.txt"), "versions": details,
                "pyproject_sha256": hashlib.sha256(source).hexdigest()}
                lock_name = "uv.lock"
            else:
                result = {"schema_version": 1, "ecosystem": "node", "runtime_path": str(target),
                    "project_path": str(target), "node_path": str(target / ".mcpme-node/bin/node"), "node_modules_path": str(target / "node_modules"),
                    "package_lock_path": str(target / details["lock_name"]),
                    "dependency_notices_path": str(target / ".mcpme-node/dependency-notices.json"),
                    "dependency_count": len(details["versions"]),
                    "versions": [{"name": item["name"], "version": item["version"]} for item in details["versions"][:64]],
                    "lifecycle_scripts_pending": details["lifecycle_scripts_pending"][:64],
                    "lifecycle_script_count": len(details["lifecycle_scripts_pending"]),
                    "bin_launcher_count": details["bin_launcher_count"],
                    "package_json_sha256": hashlib.sha256(source).hexdigest()}
                lock_name = details["lock_name"]
            if len(json.dumps(result, sort_keys=True).encode()) > 65536:
                raise PreparationError("pi_dependency_metadata_limit")
            (runtime / MARKER).write_text(json.dumps(result, sort_keys=True) + "\n")
            _write_file(project_fd, lock_name, lock)
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
