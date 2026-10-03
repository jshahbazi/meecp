"""Separately admitted macOS 26 arm64 confined fork/exec ownership.

No catalog registration, aggregate resource guarantee, or PID adoption. The
host must verify an immutable package and current authority in ``admit``.
Payloads and process output are transient and are never journaled or logged.
"""
from __future__ import annotations

import asyncio
import ctypes
from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import stat
import struct
import subprocess
import tempfile
import time

from .private import check_private_file, ensure_private_directory, read_private_json, write_private_json

POLICY = "macos26-arm64-confined-fork-exec-v2"
SEED_SIZE = 296
RECEIPT_SIZE = 301


class CommandExecutionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _hex(value: object, size: int = 64) -> bool:
    return isinstance(value, str) and len(value) == size and all(c in "0123456789abcdef" for c in value)


def _platform_policy() -> str:
    if platform.system() != "Darwin" or platform.machine() != "arm64" or platform.mac_ver()[0].split(".")[0] != "26":
        raise CommandExecutionError("command_platform_unsupported")
    # Kernel build/version and OS product version are bound, not just the major.
    return _digest([POLICY, platform.mac_ver()[0], platform.release(), platform.version(), platform.machine()])


@dataclass(frozen=True)
class CommandSpecV2:
    package: Path
    executable: Path
    cwd: Path
    package_digest: str
    argv: tuple[str, ...] = ()
    deadline_ms: int = 1000
    grace_ms: int = 50
    settlement_ms: int = 2000
    input_limit: int = 65536
    output_limit: int = 65536
    reservation_units: int = 1
    authority_digest: str = "0" * 64

    def validate(self) -> None:
        for value, low, high in ((self.deadline_ms, 1, 3600000), (self.grace_ms, 0, 1000),
                                 (self.settlement_ms, 50, 10000), (self.input_limit, 0, 1048576),
                                 (self.output_limit, 0, 1048576), (self.reservation_units, 1, 4096)):
            if type(value) is not int or not low <= value <= high:
                raise CommandExecutionError("invalid_command_spec")
        if (not _hex(self.package_digest) or not _hex(self.authority_digest) or type(self.argv) is not tuple or len(self.argv) > 128 or
                any(type(arg) is not str or "\0" in arg for arg in self.argv)):
            raise CommandExecutionError("invalid_command_spec")
        for path in (self.package, self.executable, self.cwd):
            if not isinstance(path, Path) or not path.is_absolute() or path.resolve() != path or len(str(path).encode()) >= 4096:
                raise CommandExecutionError("invalid_command_spec")
        if (self.package == Path("/") or not self.package.is_dir() or not self.cwd.is_dir() or
                not self.executable.is_file() or self.package not in self.executable.parents or
                (self.cwd != self.package and self.package not in self.cwd.parents)):
            raise CommandExecutionError("invalid_command_spec")
        if sum(len(arg.encode()) + 4 for arg in self.argv) + sum(len(str(p).encode()) + 4 for p in (self.package, self.executable, self.cwd)) > 60000:
            raise CommandExecutionError("invalid_command_spec")

    @property
    def digest(self) -> str:
        return _digest({name: str(value) if isinstance(value, Path) else value for name, value in vars(self).items()})


@dataclass(frozen=True)
class CommandResultV2:
    stdout: bytes
    returncode: int
    elapsed_ms: int


def _seed(item: dict) -> bytes:
    return ("MCPMECR3" + item["instance"] + item["nonce"] + item["helper_digest"] +
            item["policy_digest"] + item["contract_digest"]).encode("ascii")


def _string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("!I", len(encoded)) + encoded


def _publish_nonlaunch(directory: int, staged: str, complete: str) -> None:
    # Darwin RENAME_EXCL, from sys/stdio.h. The source's full payload/mode have
    # been fsynced and all writable descriptors closed before this final step.
    rename = ctypes.CDLL(None, use_errno=True).renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(staged), directory, os.fsencode(complete), 0x00000004):
        raise CommandExecutionError("command_receipt_publication_failed")


def _verify_staged(fd: int, directory: int, name: str) -> None:
    opened = os.fstat(fd)
    named = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if (not stat.S_ISREG(named.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or
            named.st_uid != os.getuid() or stat.S_IMODE(named.st_mode) != 0o400 or
            named.st_nlink != 1 or named.st_size != RECEIPT_SIZE):
        raise CommandExecutionError("command_receipt_publication_failed")


async def _join(awaitable):
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class CommandExecutorV2:
    def __init__(self, helper: Path, expected_helper_sha256: str, state: Path, *, admit: Callable[[CommandSpecV2], bool]):
        self.policy_digest = _platform_policy()
        self.helper = Path(helper)
        self.helper_digest = expected_helper_sha256
        self.admit = admit
        self._verify_helper()
        if not callable(admit):
            raise CommandExecutionError("command_admission_required")
        self.state = ensure_private_directory(Path(state).absolute())
        self.journal_path = self.state / "command-v2.json"
        lock_path = self.state / "command-v2.lock"
        self.lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._native: dict[str, subprocess.Popen] = {}
        try:
            check_private_file(lock_path)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.journal = read_private_json(self.journal_path) if self.journal_path.exists() else {"version": 3, "entries": []}
            self._validate_journal()
            self.reconcile()
        except BaseException:
            os.close(self.lock_fd)
            raise
        self.serial = asyncio.Lock()
        self.closed = False
        self._active: subprocess.Popen | None = None
        self._control: int | None = None

    def _verify_helper(self) -> None:
        try:
            if (not _hex(self.helper_digest) or not self.helper.is_absolute() or self.helper.resolve() != self.helper or
                    not self.helper.is_file() or not os.access(self.helper, os.X_OK)):
                raise ValueError()
            info = self.helper.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
                raise ValueError()
            if hashlib.sha256(self.helper.read_bytes()).hexdigest() != self.helper_digest:
                raise ValueError()
        except (OSError, ValueError):
            raise CommandExecutionError("command_helper_mismatch") from None

    def _validate_journal(self) -> None:
        value = self.journal
        if type(value) is not dict or set(value) != {"version", "entries"} or type(value["version"]) is not int or value["version"] != 3 or type(value["entries"]) is not list or len(value["entries"]) > 256:
            raise CommandExecutionError("invalid_command_journal")
        identities = set()
        for item in value["entries"]:
            if (type(item) is not dict or set(item) != {"instance", "nonce", "helper_digest", "policy_digest", "contract_digest", "units", "state", "receipt_failed"} or
                    not _hex(item["instance"], 32) or item["instance"] in identities or
                    any(not _hex(item[key]) for key in ("nonce", "helper_digest", "policy_digest", "contract_digest")) or
                    type(item["units"]) is not int or not 1 <= item["units"] <= 4096 or item["state"] not in ("pending", "settled") or
                    type(item["receipt_failed"]) is not bool):
                raise CommandExecutionError("invalid_command_journal")
            identities.add(item["instance"])

    def _receipt_path(self, item: dict) -> Path:
        return self.state / (item["instance"] + ".command-v3.complete")

    def _staged_path(self, item: dict) -> Path:
        return self.state / (item["instance"] + ".command-v3.pending")

    def _proof(self, item: dict) -> tuple[bool, int]:
        if item["receipt_failed"]:
            return False, 0
        native = self._native.get(item["instance"])
        if native is not None and native.poll() not in (0, 64):
            # A visible marker cannot race the current owner's final sync or
            # observed error. Restart has no adopted process handle.
            return False, 0
        try:
            path = self._receipt_path(item)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1 or info.st_size != RECEIPT_SIZE:
                    return False, 0
                frame = os.read(fd, RECEIPT_SIZE + 1)
            finally:
                os.close(fd)
            valid = len(frame) == RECEIPT_SIZE and frame[:SEED_SIZE] == _seed(item) and frame[SEED_SIZE:SEED_SIZE + 1] in (b"C", b"N")
            code = struct.unpack("!i", frame[-4:])[0] if valid else 0
            valid = valid and -127 <= code <= 255 and (frame[SEED_SIZE:SEED_SIZE + 1] != b"N" or code == 0)
            return valid, code if valid else 0
        except (OSError, ValueError):
            return False, 0

    def reconcile(self) -> dict[str, int]:
        changed = False
        for item in self.journal["entries"]:
            native = self._native.get(item["instance"])
            if native is not None and native.poll() not in (None, 0, 64) and not item["receipt_failed"]:
                item["receipt_failed"] = True
                changed = True
            state = "settled" if self._proof(item)[0] else "pending"
            if item["state"] != state:
                item["state"] = state
                changed = True
        if changed:
            write_private_json(self.journal_path, self.journal)
        self._native = {key: process for key, process in self._native.items() if process.poll() is None}
        pending = [item for item in self.journal["entries"] if item["state"] == "pending"]
        return {"pending": len(pending), "reserved_units": sum(item["units"] for item in pending)}

    def _cancel(self) -> None:
        if self._control is not None:
            os.close(self._control)
            self._control = None

    async def _finish(self, process: subprocess.Popen, hard_deadline: float) -> None:
        while process.poll() is None and time.monotonic() < hard_deadline:
            await asyncio.sleep(0.005)
        # Never kill the native owner: losing it cannot settle its target tree.
        # At the deadline ownership remains pending; its receipt can arrive later.

    async def execute(self, spec: CommandSpecV2, payload: bytes = b"", *, instance_id: str | None = None) -> CommandResultV2:
        if type(spec) is not CommandSpecV2 or type(payload) is not bytes:
            raise CommandExecutionError("invalid_command_spec")
        try:
            spec.validate()
        except (OSError, UnicodeError):
            raise CommandExecutionError("invalid_command_spec") from None
        if len(payload) > spec.input_limit:
            raise CommandExecutionError("command_input_limit")
        if instance_id is not None and not _hex(instance_id, 32):
            raise CommandExecutionError("invalid_command_instance")
        async with self.serial:
            if self.closed or self.reconcile()["pending"]:
                raise CommandExecutionError("command_ownership_unsettled")
            if len(self.journal["entries"]) >= 256:
                raise CommandExecutionError("command_journal_limit")
            self._verify_helper()
            if not self._admitted(spec):
                raise CommandExecutionError("command_not_admitted")
            if any(item["instance"] == instance_id for item in self.journal["entries"]):
                raise CommandExecutionError("command_instance_reused")
            return await self._execute(spec, payload, instance_id)

    def _admitted(self, spec: CommandSpecV2) -> bool:
        try:
            return self.admit(spec) is True
        except Exception:
            raise CommandExecutionError("command_admission_failed") from None

    async def _execute(self, spec: CommandSpecV2, payload: bytes, instance_id: str | None = None) -> CommandResultV2:
        item = {"instance": instance_id or secrets.token_hex(16), "nonce": secrets.token_hex(32), "helper_digest": self.helper_digest,
                "policy_digest": self.policy_digest, "contract_digest": spec.digest, "units": spec.reservation_units,
                "state": "pending", "receipt_failed": False}
        directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        process = None
        control_read = None
        started = time.monotonic()
        hard_deadline = started + (spec.deadline_ms + spec.grace_ms + spec.settlement_ms) / 1000 + 7
        output = bytearray()
        result = None
        try:
            fd = os.open(self._staged_path(item).name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            frame = _seed(item) + b"P" + struct.pack("!i", 0)
            if os.write(fd, frame) != RECEIPT_SIZE:
                raise OSError()
            os.fsync(fd)
            self.journal["entries"].append(item)
            write_private_json(self.journal_path, self.journal)
            wire = (b"MCPMEC03" + _seed(item) + struct.pack("!III", spec.deadline_ms, spec.grace_ms, spec.settlement_ms) +
                    b"".join(_string(str(value)) for value in (POLICY, spec.package, spec.executable, spec.cwd)) +
                    struct.pack("!I", len(spec.argv)) + b"".join(_string(arg) for arg in spec.argv))
            control_read, self._control = os.pipe()
            # An anonymous unlinked file contains the bounded transport spec;
            # never a path-based prompt, source, stdout, or response log.
            with tempfile.TemporaryFile() as transport:
                transport.write(wire)
                transport.flush()
                transport.seek(0)
                process = subprocess.Popen([str(self.helper), str(transport.fileno()), str(control_read), str(fd), str(directory), str(os.getpid())],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={}, close_fds=True,
                    pass_fds=(transport.fileno(), control_read, fd, directory), start_new_session=True)
                self._native[item["instance"]] = process
                os.close(fd)
                fd = None
                if os.write(self._control, b"G") != 1:
                    raise OSError()
            self._active = process
            os.close(control_read)
            control_read = None
            assert process.stdin is not None and process.stdout is not None
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            sent = 0
            eof = False
            while process.poll() is None or not eof:
                if not process.stdin.closed:
                    try:
                        if sent < len(payload):
                            sent += os.write(process.stdin.fileno(), payload[sent:sent + 65536])
                        if sent == len(payload):
                            process.stdin.close()
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        process.stdin.close()
                try:
                    chunk = os.read(process.stdout.fileno(), min(65536, spec.output_limit - len(output) + 1))
                    if not chunk:
                        eof = True
                    elif len(output) + len(chunk) > spec.output_limit:
                        raise CommandExecutionError("command_output_limit")
                    else:
                        output.extend(chunk)
                except BlockingIOError:
                    pass
                if time.monotonic() - started >= spec.deadline_ms / 1000:
                    raise CommandExecutionError("command_timeout")
                await asyncio.sleep(0.005)
            proven, code = self._proof(item)
            if process.returncode != 0 or not proven:
                raise CommandExecutionError("command_ownership_unsettled")
            if code:
                raise CommandExecutionError("command_failed")
            result = CommandResultV2(bytes(output), code, int((time.monotonic() - started) * 1000))
        except OSError:
            raise CommandExecutionError("command_io_failed") from None
        finally:
            self._cancel()
            if control_read is not None:
                os.close(control_read)
            try:
                if process is None and fd is not None:
                    # Only this pre-Popen path may acknowledge proven nonlaunch.
                    frame = _seed(item) + b"N" + struct.pack("!i", 0)
                    if os.pwrite(fd, frame, 0) != RECEIPT_SIZE:
                        raise OSError()
                    os.fchmod(fd, 0o400)
                    os.fsync(fd)
                    _verify_staged(fd, directory, self._staged_path(item).name)
                    os.close(fd)
                    fd = None
                    _publish_nonlaunch(directory, self._staged_path(item).name, self._receipt_path(item).name)
                elif process is not None:
                    settle_by = min(hard_deadline, time.monotonic() + (spec.grace_ms + spec.settlement_ms) / 1000 + 7)
                    await _join(self._finish(process, settle_by))
                    if process.poll() not in (None, 0, 64):
                        item["receipt_failed"] = True
                        write_private_json(self.journal_path, self.journal)
            finally:
                if process is not None:
                    if process.stdin is not None:
                        process.stdin.close()
                    if process.stdout is not None:
                        process.stdout.close()
                if fd is not None:
                    os.close(fd)
                os.close(directory)
                self._active = None
            self.reconcile()
        if item["state"] != "settled":
            raise CommandExecutionError("command_ownership_unsettled")
        if not self._admitted(spec):
            raise CommandExecutionError("command_authority_changed")
        assert result is not None
        return result

    async def close(self) -> None:
        self.closed = True
        self._cancel()
        async with self.serial:
            self.reconcile()
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
