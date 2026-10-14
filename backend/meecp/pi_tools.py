"""Bounded host tools for Pi; selected code runs only in a private sandbox.

The credential-bearing Pi worker does not execute these operations. The host
owns filesystem descriptors, public HTTPS transfers and native process groups.
No tool payload or exception is written to a log.
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path

from .downloads import DownloadError, PinnedHTTPSNetwork, normalize_spec
from .pi_listeners import ListenerError, nonloopback_listener, watch_listeners
from .private import PrivateStateError, ensure_private_directory
from .processes import OwnershipLost, ProcessError, ProcessOwner, _await_ownership, _watch_ownership

MAX_FILE = 1024 * 1024
# Even worst-case ASCII JSON escaping must fit the host/worker 1 MiB frame.
# The write bound remains larger because files do not become response frames.
MAX_READ = 128 * 1024
MAX_OUTPUT = 64 * 1024
# stdout is JSON-encoded once here and again in the worker protocol envelope.
# Keep the encoded result bounded independently of its raw byte stream.
MAX_COMMAND_RESULT = 128 * 1024
MAX_COMMAND = 128
MAX_ARG_BYTES = 32 * 1024
MAX_COMMAND_BYTES = 128 * 1024
MAX_PATH = 4096
MAX_FETCH = 16 * 1024**3
MAX_FETCH_TOTAL = 32 * 1024**3
FETCH_TIMEOUT = 900
SANDBOX = Path("/usr/bin/sandbox-exec")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_SYSTEM_READS = (
    "/bin", "/usr/bin", "/usr/lib", "/usr/share", "/System/Library",
    "/System/Volumes/Preboot/Cryptexes/OS/System/Library",
    "/System/Volumes/Preboot/Cryptexes/OS/usr/lib", "/private/var/db/dyld",
    "/Library/Developer/CommandLineTools",
)
_SYSCTLS = (
    "hw.ncpu", "hw.activecpu", "hw.memsize", "hw.physicalcpu", "hw.physicalcpu_max",
    "hw.logicalcpu", "hw.logicalcpu_max", "hw.optional.arm64", "hw.pagesize",
    "kern.osrelease", "kern.osversion", "kern.ostype", "kern.maxfilesperproc",
)


class PiToolError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def _settle(action):
    """Finish ownership cleanup before propagating cancellation during it."""
    task = asyncio.current_task()
    cancellations = task.cancelling()
    result = await _await_ownership(action)
    if task.cancelling() > cancellations:
        raise asyncio.CancelledError
    return result


def _identity(details):
    return details.st_dev, details.st_ino, details.st_uid, stat.S_IMODE(details.st_mode)


def _absolute_directory(path: Path) -> int:
    """Walk the original spelling without resolving away a symlink first."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PiToolError("unsafe_root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass(frozen=True)
class _Root:
    path: Path
    descriptor: int
    identity: tuple[int, int, int, int]
    private: bool


class PiToolBroker:
    def __init__(self, workspace: Path, reaper: Path, *, read_roots=(), executable_env=None):
        self._roots: list[_Root] = []
        self._closed = False
        self._busy = asyncio.Lock()
        self._fetch_bytes = 0
        self.host_root = None
        # Only the host callback can admit and reserve a native GPU command.
        self.native_runner = None
        self.native_ownership_settled = False
        self.credential_resolver = None
        self.services = {}
        self.network = PinnedHTTPSNetwork()
        try:
            self.workspace = ensure_private_directory(Path(workspace))
            self.read_roots = tuple(Path(root).absolute() for root in read_roots)
            if len(self.read_roots) > 16 or len(set(self.read_roots)) != len(self.read_roots):
                raise PiToolError("unsafe_root")
            self.home = ensure_private_directory(self.workspace / ".home")
            self.temporary = ensure_private_directory(self.workspace / ".tmp")
            for path, private in ((self.workspace, True), *((root, False) for root in self.read_roots),
                                  (self.home, True), (self.temporary, True)):
                fd = _absolute_directory(path)
                details = os.fstat(fd)
                if (details.st_uid not in (0, os.getuid()) or details.st_mode & 0o022 or
                        (private and (details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700))):
                    os.close(fd)
                    raise PiToolError("unsafe_root")
                self._roots.append(_Root(path, fd, _identity(details), private))
            self.owner = ProcessOwner(Path(reaper))
            self.env = self._environment(executable_env)
            from .pi_toolchain import ToolchainManager
            self.toolchain_manager = ToolchainManager(self)
        except BaseException as error:
            for root in self._roots:
                os.close(root.descriptor)
            self._roots.clear()
            if isinstance(error, (OSError, PrivateStateError)):
                raise PiToolError("unsafe_root") from None
            raise

    def _check_roots(self, *, validate_toolchain=True):
        if self._closed:
            raise PiToolError("broker_closed")
        for root in self._roots:
            try:
                fd = _absolute_directory(root.path)
                try:
                    if _identity(os.fstat(fd)) != root.identity or _identity(os.fstat(root.descriptor)) != root.identity:
                        raise PiToolError("root_changed")
                finally:
                    os.close(fd)
            except OSError:
                raise PiToolError("root_changed") from None
        if any(process.ownership_lost for process in self.owner.instances.values()):
            raise OwnershipLost()
        if validate_toolchain and hasattr(self, "toolchain_manager"):
            self.toolchain_manager.validate()

    def _select(self, value: str, *, write=False):
        if (not isinstance(value, str) or not value or len(value) > MAX_PATH or
                any(ord(char) < 32 or ord(char) == 127 for char in value) or ".." in Path(value).parts):
            raise PiToolError("invalid_path")
        self._check_roots()
        path = Path(value) if os.path.isabs(value) else self.workspace / value
        roots = self._roots[:1] if write else self._roots[:1 + len(self.read_roots)]
        for root in sorted(roots, key=lambda item: len(item.path.parts), reverse=True):
            if path == root.path or root.path in path.parents:
                return root, path, path.relative_to(root.path).parts
        raise PiToolError("path_outside_workspace")

    def _parent(self, value: str, *, write=False, create=False):
        root, path, parts = self._select(value, write=write)
        if not parts:
            raise PiToolError("invalid_file")
        fd = os.dup(root.descriptor)
        try:
            for component in parts[:-1]:
                try:
                    following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                if write:
                    details = os.fstat(following)
                    if details.st_uid != os.getuid() or details.st_mode & 0o022:
                        os.close(following)
                        raise PiToolError("unsafe_path")
                os.close(fd)
                fd = following
            self._check_roots()
            return fd, parts[-1], path
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _regular(details, *, write=False):
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or
                (write and (details.st_uid != os.getuid() or details.st_mode & 0o022))):
            raise PiToolError("unsafe_file")

    def _path(self, value: str, *, write=False, directory=False) -> Path:
        root, path, parts = self._select(value, write=write)
        fd = os.dup(root.descriptor)
        try:
            for index, component in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if directory or index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                following = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = following
            details = os.fstat(fd)
            if directory:
                if not stat.S_ISDIR(details.st_mode):
                    raise PiToolError("invalid_cwd")
            elif not stat.S_ISDIR(details.st_mode):
                self._regular(details, write=write)
            self._check_roots()
            return path
        finally:
            os.close(fd)

    def _read(self, value, *, offset=None, limit=None):
        parent, name, _ = self._parent(value)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                self._regular(before)
                paged = offset is not None or limit is not None
                offset = 0 if offset is None else offset
                limit = MAX_READ if limit is None else limit
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_READ:
                    raise PiToolError("invalid_arguments")
                if not paged and before.st_size > MAX_READ:
                    raise PiToolError("file_size_limit")
                os.lseek(fd, offset, os.SEEK_SET)
                bound = limit if paged else MAX_READ + 1
                data = bytearray()
                while len(data) < bound:
                    chunk = os.read(fd, min(64 * 1024, bound - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > MAX_READ:
                    raise PiToolError("file_size_limit")
                after = os.fstat(fd)
                self._regular(after)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise PiToolError("file_changed")
                self._check_roots()
                try:
                    if not paged:
                        return data.decode("utf-8")
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    content = decoder.decode(data, final=offset + len(data) >= before.st_size)
                    consumed = len(data) - len(decoder.getstate()[0])
                    if data and not consumed:
                        raise PiToolError("read_limit_too_small")
                    while True:
                        consumed = len(content.encode("utf-8"))
                        encoded = json.dumps({"content": content, "next_offset": offset + consumed,
                            "eof": offset + consumed >= before.st_size}, ensure_ascii=True)
                        if len(encoded.encode()) <= MAX_COMMAND_RESULT:
                            return encoded
                        content = content[:len(content) // 2]
                except UnicodeError:
                    raise PiToolError("file_not_utf8") from None
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def _output(self, value):
        parent, name, path = self._parent(value, write=True, create=True)
        temporary = ".pi-tool-" + secrets.token_hex(16)
        try:
            try:
                self._regular(os.stat(name, dir_fd=parent, follow_symlinks=False), write=True)
            except FileNotFoundError:
                pass
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            return parent, name, temporary, descriptor, path
        except BaseException:
            os.close(parent)
            raise

    @staticmethod
    def _write_bytes(fd, data):
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise PiToolError("filesystem_unavailable")
            remaining = remaining[written:]

    def _publish(self, parent, name, temporary, fd):
        os.fsync(fd)
        self._check_roots()
        try:
            self._regular(os.stat(name, dir_fd=parent, follow_symlinks=False), write=True)
        except FileNotFoundError:
            pass
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        self._check_roots()

    def _write(self, value, content):
        if not isinstance(content, str) or len(content) > MAX_FILE:
            raise PiToolError("file_size_limit")
        try:
            data = content.encode("utf-8")
        except UnicodeError:
            raise PiToolError("invalid_content") from None
        if len(data) > MAX_FILE:
            raise PiToolError("file_size_limit")
        parent, name, temporary, fd, _ = self._output(value)
        try:
            self._write_bytes(fd, data)
            self._publish(parent, name, temporary, fd)
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            os.close(parent)

    @staticmethod
    def _arguments(args, required, optional=()):
        if type(args) is not dict or set(args) - (set(required) | set(optional)) or not set(required) <= set(args):
            raise PiToolError("invalid_arguments")

    async def invoke(self, name: str, args: dict) -> dict:
        try:
            async with self._busy:
                # Fixed host discovery can inspect a replacement Apple
                # installation before repinning its read-only toolchain roots.
                # Every ordinary command still validates the selected roots.
                self._check_roots(validate_toolchain=name not in {"preflight", "ensure_metal_toolchain"})
                if name == "read_file":
                    self._arguments(args, ("path",), ("offset", "limit"))
                    return {"success": True, "output": self._read(args["path"], offset=args.get("offset"), limit=args.get("limit"))}
                if name == "list_files":
                    self._arguments(args, (), ("path", "recursive", "offset", "limit"))
                    return {"success": True, "output": self._list_files(args)}
                if name == "capabilities":
                    self._arguments(args, ())
                    return {"success": True, "output": json.dumps(self.capabilities())}
                if name == "preflight":
                    self._arguments(args, ())
                    return {"success": True, "output": json.dumps(await self.preflight())}
                if name == "ensure_metal_toolchain":
                    self._arguments(args, (), ("required",))
                    facts = await self.toolchain_manager.ensure(args)
                    return {"success": facts["available"] or not facts["required"], "output": json.dumps(facts)}
                if name == "native_command":
                    from .pi_native import validate_native
                    validate_native(self, args)
                    if self.native_runner is None:
                        raise PiToolError("native_command_unavailable")
                    if args.get("require_metal_compiler", False):
                        facts = await self.toolchain_manager.ensure({"required": True})
                        if not facts["available"]:
                            return {"success": False, "output": json.dumps(facts)}
                    return await self.native_runner(args)
                if name == "write_file":
                    self._arguments(args, ("path", "content"))
                    self._write(args["path"], args["content"])
                    return {"success": True, "output": "written"}
                if name == "run_command":
                    self._arguments(args, ("argv",), ("cwd", "timeout_seconds", "network", "env", "credentials"))
                    return await self._run(args)
                if name == "start_process":
                    self._arguments(args, ("argv",), ("cwd", "network", "env", "credentials"))
                    return await self._start_process(args)
                if name in {"read_process", "stop_process"}:
                    self._arguments(args, ("process_id",))
                    return await self._service(name, args["process_id"])
                if name == "fetch_url":
                    self._arguments(args, ("url", "path"), ("sha256", "size"))
                    return await self._fetch(args)
                if name == "install_dependencies":
                    self._arguments(args, ("ecosystem", "project_path"), ("target_path",))
                    if self.host_root is None:
                        raise PiToolError("dependency_ecosystem_unavailable")
                    from .pi_dependencies import install_dependencies
                    return await install_dependencies(self, args, self.host_root)
                if name in {"prepare_environment", "extract_archive"}:
                    from .pi_environment import extract_archive, prepare_environment
                    if name == "prepare_environment":
                        self._arguments(args, ("runtime",), ("target_path",))
                        if self.host_root is None:
                            raise PiToolError("dependency_ecosystem_unavailable")
                        return await prepare_environment(self, args, self.host_root)
                    self._arguments(args, ("archive_path", "target_path"), ("strip_components",))
                    return await extract_archive(self, args)
                raise PiToolError("unknown_tool")
        except asyncio.CancelledError:
            raise
        except PiToolError as error:
            return {"success": False, "output": error.code}
        except OwnershipLost:
            # Loss of the native owner invalidates the whole setup session.
            # An agent cannot repair it or continue allocating new authority.
            raise
        except TimeoutError:
            return {"success": False, "output": "tool_timeout"}
        except DownloadError as error:
            code = error.code if error.code in {
                "invalid_download_url", "download_network_blocked", "download_network_unavailable",
                "download_response_invalid", "download_redirect_blocked", "download_http_failed",
            } else "download_failed"
            return {"success": False, "output": code}
        except (OSError, PrivateStateError):
            return {"success": False, "output": "filesystem_unavailable"}
        except ProcessError:
            return {"success": False, "output": "command_failed"}
        except Exception:
            return {"success": False, "output": "tool_failed"}

    def _environment(self, provided):
        if provided is not None and (type(provided) is not dict or set(provided) - {"PATH"}):
            raise PiToolError("invalid_environment")
        path = (provided or {}).get("PATH", "/usr/bin:/bin")
        if not isinstance(path, str) or not path or len(path) > MAX_PATH:
            raise PiToolError("invalid_environment")
        for directory in path.split(":"):
            if directory in ("/usr/bin", "/bin"):
                continue
            if not os.path.isabs(directory):
                raise PiToolError("invalid_environment")
            self._path(directory, directory=True)
        return {"PATH": path, "HOME": str(self.home), "TMPDIR": str(self.temporary),
                "LANG": "C", "LC_ALL": "C"}

    def _command_environment(self, args):
        values = args.get("env", {})
        if (not isinstance(values, dict) or len(values) > 64 or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) or not isinstance(value, str)
                or len(value) > 8192 or "\0" in value for key, value in values.items())):
            raise PiToolError("invalid_environment")
        if set(values) & {"HOME", "TMPDIR", "XDG_CONFIG_HOME", "MCPME_SETTINGS_FD", "DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS"}:
            raise PiToolError("invalid_environment")
        result = dict(self.env)
        result.update(values)
        if "PATH" in values:
            result.update(self._environment({"PATH": values["PATH"]}))
        references = args.get("credentials", {})
        if (not isinstance(references, dict) or len(references) > 16 or any(
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key) or not isinstance(value, str)
                for key, value in references.items()) or set(references) & {
                    "PATH", "HOME", "TMPDIR", "MCPME_SETTINGS_FD", "DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS"}):
            raise PiToolError("invalid_credentials")
        if references:
            if self.credential_resolver is None:
                raise PiToolError("credentials_unavailable")
            try:
                result.update(self.credential_resolver(references))
            except Exception:
                raise PiToolError("credentials_unavailable") from None
        result["PATH"] = self.toolchain_manager.command_path(result["PATH"])
        return result, bool(references)

    def _trust_environment(self):
        """Stage configured certificates or native public roots for confined clients."""
        source = os.environ.get("NODE_EXTRA_CA_CERTS")
        target = self.workspace / ".trust/ca.pem"
        if not target.is_file():
            try:
                if source:
                    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                            raise ValueError()
                        data = stream.read(MAX_FILE + 1).decode("ascii")
                else:
                    from .pi_trust import native_anchor_pem
                    data = native_anchor_pem()
                blocks = re.findall(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----", data)
                if not blocks or len(blocks) > 512:
                    raise ValueError()
                for block in blocks:
                    ssl.PEM_cert_to_DER_cert(block)
                self._write(".trust/ca.pem", "\n".join(blocks) + "\n")
            except (OSError, UnicodeError, ValueError):
                raise PiToolError("trust_configuration_unavailable") from None
        return {"NODE_EXTRA_CA_CERTS": str(target), "SSL_CERT_FILE": str(target), "GIT_SSL_CAINFO": str(target)}

    def capabilities(self):
        return {"platform": {"os": platform.system(), "architecture": platform.machine()},
            "workspace": str(self.workspace), "host_root": str(self.host_root) if self.host_root else None,
            "tools": ["capabilities", "preflight", "ensure_metal_toolchain", "native_command", "list_files", "read_file", "write_file", "run_command", "start_process",
                "read_process", "stop_process", "fetch_url", "prepare_environment", "extract_archive",
                "install_dependencies", "retain_resource", "ask_user", "check_package"],
            "commands": {"network": "optional outbound network; owned TCP listeners checked for loopback",
                "filesystem": "private workspace writes, selected runtimes and system resources readable",
                "environment": "explicit values; opaque credential IDs may be injected without returning their output"},
            "native_commands": {"available": self.native_runner is not None, "gpu": "host-admitted exclusive Metal",
                "memory_mb": "required bounded resource declaration", "metal_compiler": dict(self.toolchain_manager.snapshot)},
            "retained_resources": {"inventory": "resources.json", "checkpoint": "fresh session; verified files and opaque credentials only",
                "max_file_bytes": MAX_FETCH, "max_collection_bytes": MAX_FETCH_TOTAL},
            "runtimes": {"bundled": ["python", "node"], "other": "fetch and extract a runtime or build one in the workspace"},
            "dependency_managers": "Python uv and npm helpers; any staged manager can run custom commands",
            "developer_tools": self.toolchain_manager.command_tools,
            "system_tools": [*self.toolchain_manager.command_tools.values(),
                *(str(Path("/usr/bin") / name) for name in ("tar", "unzip") if (Path("/usr/bin") / name).is_file())],
            "package": {"entrypoint": "relocatable MCP stdio launcher; may own child services",
                "schemas": ["package.schema.json", "lock.schema.json"], "recipe_required": False}}

    def _list_files(self, args):
        recursive, offset, limit = args.get("recursive", False), args.get("offset", 0), args.get("limit", 200)
        if (type(recursive) is not bool or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 500):
            raise PiToolError("invalid_arguments")
        directory = self._path(args.get("path", "."), directory=True)
        entries, pending = [], [directory]
        while pending:
            selected = pending.pop()
            self._path(str(selected), directory=True)
            for item in sorted(selected.iterdir()):
                info = item.lstat()
                kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "link" if stat.S_ISLNK(info.st_mode) else "special"
                entries.append({"path": str(item.relative_to(directory)), "kind": kind, "size": info.st_size})
                if recursive and kind == "directory":
                    pending.append(item)
                if len(entries) >= 20000:
                    pending.clear()
                    break
        entries.sort(key=lambda item: item["path"])
        chosen = entries[offset:offset + limit]
        while True:
            encoded = json.dumps({"entries": chosen, "next_offset": offset + len(chosen),
                "eof": offset + len(chosen) >= len(entries), "scan_limit": 20000}, ensure_ascii=True)
            if len(encoded.encode()) <= MAX_COMMAND_RESULT:
                return encoded
            chosen.pop()

    async def _start_process(self, args):
        if sum(not item["task"].done() for item in self.services.values()) >= 16 or len(self.services) >= 128:
            raise PiToolError("process_limit")
        self._argv(args["argv"])
        cwd = self._path(args.get("cwd", "."), write=True, directory=True)
        env, hidden = self._command_environment(args)
        executable, sandbox_args, cwd, env = self.launch_spec(args["argv"], str(cwd), network=args.get("network", False), env=env)
        process = await self.owner.launch(executable, [*sandbox_args[:2], "/bin/sh", "-c", 'exec "$@" 2>&1',
            "meecp-command", *sandbox_args[2:]], cwd, env, owned_group=True,
            expected_reaper=self.owner.reaper, startup_timeout=15, stop_grace=0.2, admit=self._launch_admitted)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        await process.stdin.wait_closed()
        identity = secrets.token_hex(16)
        item = {"process": process, "output": bytearray(), "hidden": hidden, "code": None, "error": None}
        async def pump():
            async def consume():
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk:
                        break
                    if not hidden:
                        item["output"].extend(chunk)
                        del item["output"][:-MAX_OUTPUT]
                item["code"] = await process.wait()
            await _watch_ownership(process, consume())
        item["task"] = asyncio.create_task(pump())
        async def monitor():
            try:
                while not item["task"].done():
                    if await self._nonloopback_listener(process):
                        item["error"] = "listener_not_loopback"
                        await process.stop()
                        return
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                item["error"] = "listener_inspection_failed"
                await process.stop()
        item["monitor"] = asyncio.create_task(monitor())
        self.services[identity] = item
        await asyncio.sleep(0.15)
        if item["error"]:
            return {"success": False, "output": item["error"]}
        return {"success": True, "output": json.dumps({"process_id": identity, "state": "running"})}

    async def _nonloopback_listener(self, process):
        try:
            return await nonloopback_listener(process)
        except ListenerError as error:
            raise PiToolError(error.code) from None

    async def _service(self, name, identity):
        if not isinstance(identity, str) or identity not in self.services:
            raise PiToolError("process_not_found")
        item = self.services[identity]
        if name == "stop_process":
            await _settle(item["process"].stop())
            await _settle(asyncio.gather(item["task"], return_exceptions=True))
        if item["error"]:
            return {"success": False, "output": item["error"]}
        if item["task"].done():
            outcome = item["task"].exception() if not item["task"].cancelled() else None
            if isinstance(outcome, OwnershipLost):
                raise outcome
        data = bytes(item["output"])
        while True:
            encoded = json.dumps({"process_id": identity, "state": "exited" if item["task"].done() else "running",
                "returncode": item["code"], "stdout": data.decode("utf-8", "replace"), "output_hidden": item["hidden"]}, ensure_ascii=True)
            if len(encoded.encode()) <= MAX_COMMAND_RESULT:
                return {"success": True, "output": encoded}
            data = data[len(data) // 2:]

    @staticmethod
    def _argv(command):
        if type(command) is not list or not 1 <= len(command) <= MAX_COMMAND:
            raise PiToolError("invalid_command")
        total = 0
        for index, value in enumerate(command):
            if not isinstance(value, str) or "\0" in value or (index == 0 and not value):
                raise PiToolError("invalid_command")
            if len(value) > MAX_ARG_BYTES:
                raise PiToolError("command_size_limit")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError:
                raise PiToolError("invalid_command") from None
            total += size
            if size > MAX_ARG_BYTES or total > MAX_COMMAND_BYTES:
                raise PiToolError("command_size_limit")

    def launch_spec(self, argv: list[str], cwd: str = ".", *, network=False, gpu=False, env=None):
        """Confined launch tuple; GPU access is a host-selected probe capability."""
        if (platform.system() != "Darwin" or platform.machine() != "arm64" or
                not platform.mac_ver()[0].startswith("26.")):
            raise PiToolError("unsupported_platform")
        self._argv(argv)
        if type(network) is not bool or type(gpu) is not bool:
            raise PiToolError("invalid_arguments")
        cwd_path = self._path(str(cwd), directory=True)
        self._check_roots()
        argv = self.toolchain_manager.command_argv(argv)
        quote = lambda value: json.dumps(str(value), ensure_ascii=True)
        toolchain_roots = self.toolchain_manager.read_roots
        reads = (*_SYSTEM_READS, self.workspace, *self.read_roots, *toolchain_roots)
        ancestors = sorted({ancestor for path in (self.workspace, *self.read_roots, *toolchain_roots) for ancestor in path.parents}, key=str)
        profile = "\n".join([
            "(version 1)", "(deny default (with no-log))",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            "(allow file-read-metadata " + " ".join("(literal " + quote(path) + ")" for path in ancestors) + ")",
            '(allow file-read-metadata (literal "/private/var/select"))',
            # macOS /bin/sh reads this root-managed shell selection record.
            '(allow file-read* (literal "/private/var/select/sh") '
            '(literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") '
            + " ".join("(subpath " + quote(path) + ")" for path in reads) + ")",
            '(allow file-write* (literal "/dev/null") (subpath ' + quote(self.workspace) + "))",
            "(allow process-exec)", "(allow process-fork)",
            # Darwin arm64 setpgid/setsid: descendants must retain the host's
            # native owned process group so timeout and cancellation reap them.
            "(deny syscall-unix (syscall-number 82 147) (with no-log))",
        ])
        if gpu:
            # Selected only for an admitted native command or package probe.
            # IOSurface access is required by native Metal residency sets.
            profile += '\n(allow iokit-open (iokit-user-client-class "AGXDeviceUserClient") (iokit-user-client-class "IOSurfaceRootUserClient"))'
            profile += '\n(allow mach-lookup (global-name "com.apple.MTLCompilerService"))'
        if network:
            profile += '\n(allow network-outbound)\n(allow network-bind (local tcp4 "localhost:*"))\n(allow network-inbound (local tcp4 "localhost:*"))'
            profile += '\n(allow file-read* (literal "/private/etc/resolv.conf") (literal "/private/var/run/resolv.conf") (literal "/private/etc/hosts") (literal "/private/etc/services") (subpath "/private/etc/ssl"))'
            profile += '\n(allow mach-lookup (global-name "com.apple.mDNSResponder") (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.trustd") (global-name "com.apple.trustd.agent"))'
        environment = dict(self.env) if env is None else dict(env)
        if network:
            for name, value in self._trust_environment().items():
                environment.setdefault(name, value)
        return SANDBOX, ["-p", profile, *argv], cwd_path, environment

    def command_launch(self, command, cwd=None):
        return self.launch_spec(command, "." if cwd is None else str(cwd))

    async def _run(self, args, *, gpu=False):
        if gpu:
            # Argument/sandbox validation below cannot create native authority.
            self.native_ownership_settled = True
        self._argv(args["argv"])
        timeout = args.get("timeout_seconds", 60)
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise PiToolError("invalid_timeout")
        cwd = self._path(args.get("cwd", "."), write=True, directory=True)
        env, hidden = self._command_environment(args)
        executable, sandbox_args, cwd, env = self.launch_spec(args["argv"], str(cwd), network=args.get("network", False), gpu=gpu, env=env)
        # Reaper stderr is deliberately discarded for other host processes.
        # This fixed wrapper merges command diagnostics into the same bounded
        # in-memory result. Argument forwarding evaluates no selected shell text.
        launch = (executable, [*sandbox_args[:2], "/bin/sh", "-c", 'exec "$@" 2>&1',
            "meecp-command", *sandbox_args[2:]], cwd, env)
        process = None
        previous_instances = set(self.owner.instances)
        previous_pending = set(self.owner._pending_ids)
        try:
            async with asyncio.timeout(timeout):
                try:
                    if gpu:
                        self.native_ownership_settled = False
                    process = await self.owner.launch(*launch, owned_group=True,
                        expected_reaper=self.owner.reaper, startup_timeout=min(10, timeout), stop_grace=0.2,
                        admit=lambda: self._launch_admitted())
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.close()
                    await process.stdin.wait_closed()
                    async def consume():
                        data = bytearray()
                        while True:
                            chunk = await process.stdout.read(min(64 * 1024, MAX_OUTPUT + 1 - len(data)))
                            if not chunk:
                                break
                            if not hidden:
                                data.extend(chunk)
                            if len(data) > MAX_OUTPUT:
                                raise PiToolError("output_limit")
                        code = await process.wait()
                        output = json.dumps({"returncode": code,
                            "stdout": data.decode("utf-8", "replace"), **({"output_hidden": True} if hidden else {}),
                            **({"peak_memory_bytes": process.peak_memory_bytes,
                                "reserved_memory_mb": args["memory_mb"]} if gpu else {})}, ensure_ascii=True)
                        if len(output.encode("utf-8")) > MAX_COMMAND_RESULT:
                            raise PiToolError("output_limit")
                        return {"success": code == 0, "output": output}
                    action = watch_listeners(process, consume()) if args.get("network", False) else consume()
                    result = await _watch_ownership(process, action)
                finally:
                    if process is not None:
                        async def stop():
                            await process.stop()
                            if gpu:
                                self.native_ownership_settled = True
                        await _settle(stop())
                    elif gpu:
                        # A canceled/failed launch may leave an unknown owner.
                        # Existing CPU services do not belong to this command.
                        self.native_ownership_settled = not (
                            set(self.owner.instances) - previous_instances
                            or set(self.owner._pending_ids) - previous_pending)
            self._check_roots()
            return result
        except TimeoutError:
            raise PiToolError("command_timeout") from None
        except ListenerError as error:
            raise PiToolError(error.code) from None

    async def native_run_impl(self, args):
        """Host callback implementation; admission must precede this call."""
        from .pi_native import validate_native
        self.native_ownership_settled = False
        try:
            validate_native(self, args)
        except BaseException:
            # This explicit validation path has not attempted a native launch.
            self.native_ownership_settled = True
            raise
        return await self._run(args, gpu=True)

    async def preflight(self):
        from .pi_native import preflight
        return await preflight(self)

    def _launch_admitted(self):
        self._check_roots()
        return True

    def _toolchain_discovery_admitted(self):
        # This callback is only for the host's fixed system-tool commands.
        # Workspace/runtime roots and native ownership remain mandatory; old
        # compiler/SDK/asset pins must not prevent their own rediscovery.
        self._check_roots(validate_toolchain=False)
        return True

    async def _fetch(self, args):
        digest = args.get("sha256")
        if digest is not None and (not isinstance(digest, str) or not _HASH.fullmatch(digest)):
            raise PiToolError("invalid_hash")
        expected_size = args.get("size")
        if expected_size is not None and (type(expected_size) is not int or not 1 <= expected_size <= MAX_FETCH):
            raise PiToolError("invalid_download_size")
        url = normalize_spec({"url": args["url"], "sha256": "0" * 64,
            "size": expected_size if expected_size is not None else 1})["url"]
        parent, name, temporary, fd, path = self._output(args["path"])
        size, observed = 0, hashlib.sha256()
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                async with self.network.stream(url, accept="application/octet-stream", follow_redirects=True) as chunks:
                    async for chunk in chunks:
                        self._check_roots()
                        if not isinstance(chunk, bytes):
                            raise PiToolError("download_response_invalid")
                        size += len(chunk)
                        self._fetch_bytes += len(chunk)
                        if size > MAX_FETCH or self._fetch_bytes > MAX_FETCH_TOTAL:
                            raise PiToolError("download_size_limit")
                        if expected_size is not None and size > expected_size:
                            raise PiToolError("download_size_mismatch")
                        observed.update(chunk)
                        self._write_bytes(fd, chunk)
            actual = observed.hexdigest()
            if expected_size is not None and size != expected_size:
                raise PiToolError("download_size_mismatch")
            if digest is not None and digest != actual:
                raise PiToolError("download_hash_mismatch")
            self._publish(parent, name, temporary, fd)
            return {"success": True, "output": json.dumps({"path": str(path.relative_to(self.workspace)),
                "size": size, "sha256": actual}, ensure_ascii=True)}
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent)
            os.close(parent)

    async def close(self):
        await _settle(self._close())

    async def _close(self):
        self._closed = True
        try:
            await _await_ownership(self.owner.close())
        finally:
            tasks = [task for item in self.services.values() for task in (item["task"], item["monitor"])]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await _await_ownership(asyncio.gather(*tasks, return_exceptions=True))
            async with self._busy:
                for root in self._roots:
                    os.close(root.descriptor)
                self._roots.clear()
