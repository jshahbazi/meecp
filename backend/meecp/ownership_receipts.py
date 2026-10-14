"""Private identity-bound proof of child settlement or proven nonlaunch.

The supervisor journals opaque instance/nonce metadata before launch. A native
reaper inherits only a pre-opened file capability; it never resolves a receipt
path. Its C frame proves waitpid settled the held child, or parsing failed
before any child existed. The owner's N frame separately proves a native
wrapper never executed. Pending/missing receipts prove nothing.
"""
from __future__ import annotations

import os
import fcntl
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .boot_session import BootSession, valid_boot_identity
from .private import PrivateStateError, check_private_file, ensure_private_directory

MAGIC = b"MCPMEOR1"
RECEIPT_BYTES = 105
_INSTANCE = re.compile(r"[0-9a-f]{32}")
_NONCE = re.compile(r"[0-9a-f]{64}")
_ATTRIBUTES = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_nlink")


def _metadata(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"schema_version", "instance_id", "nonce"}
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and isinstance(value["instance_id"], str) and _INSTANCE.fullmatch(value["instance_id"]) is not None
            and isinstance(value["nonce"], str) and _NONCE.fullmatch(value["nonce"]) is not None)


def _frame(instance_id: str, nonce: str, state: bytes) -> bytes:
    return MAGIC + instance_id.encode("ascii") + nonce.encode("ascii") + state


def _private_directory(path: Path) -> None:
    details = path.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise PrivateStateError()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_receipt(slot_directory: Path, instance_id: str) -> tuple[int, dict[str, Any]]:
    """Create and fsync a pending capability before the caller journals launch.

    The caller owns the returned descriptor and must close it after launch. A
    duplicate identity never overwrites an existing pending/completed receipt.
    """
    if not isinstance(instance_id, str) or _INSTANCE.fullmatch(instance_id) is None:
        raise PrivateStateError("ownership_receipt_unavailable")
    descriptor = None
    path = None
    try:
        slot = ensure_private_directory(Path(slot_directory))
        directory = ensure_private_directory(slot / "ownership-receipts")
        path = directory / (instance_id + ".receipt")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        os.set_inheritable(descriptor, False)
        metadata = {"schema_version": 1, "instance_id": instance_id, "nonce": secrets.token_hex(32)}
        pending = _frame(instance_id, metadata["nonce"], b"P")
        offset = 0
        while offset < len(pending):
            count = os.write(descriptor, pending[offset:])
            if count <= 0:
                raise OSError()
            offset += count
        os.fsync(descriptor)
        _fsync_directory(directory)
        return descriptor, metadata
    except (OSError, PrivateStateError, ValueError, TypeError):
        if descriptor is not None:
            # Only reclaim the leaf created by this capability, never a replaced
            # path or an existing receipt rejected by O_EXCL.
            try:
                opened, current = os.fstat(descriptor), path.lstat()
                if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                    path.unlink()
            except OSError:
                pass
            os.close(descriptor)
        raise PrivateStateError("ownership_receipt_unavailable") from None


def validate_receipt_descriptor(descriptor: int, instance_id: str, nonce: str) -> bool:
    """Check the pending capability before giving it to the native reaper."""
    if (type(descriptor) is not int or descriptor < 3 or not isinstance(instance_id, str)
            or _INSTANCE.fullmatch(instance_id) is None or not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None):
        return False
    try:
        details = os.fstat(descriptor)
        return (stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
                and stat.S_IMODE(details.st_mode) == 0o600 and details.st_nlink == 1
                and fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDWR
                and details.st_size == RECEIPT_BYTES
                and os.pread(descriptor, RECEIPT_BYTES + 1, 0) == _frame(instance_id, nonce, b"P"))
    except OSError:
        return False


def acknowledge_nonlaunch(descriptor: int, instance_id: str, nonce: str) -> None:
    """Record host proof that ProcessOwner never executed a native wrapper.

    Only the owner's prelaunch validation or proven exec failure may invoke
    this acknowledgement. Empty instance maps and absent PIDs prove nothing.
    The distinct N frame cannot be confused with native reaping evidence.
    """
    if not validate_receipt_descriptor(descriptor, instance_id, nonce):
        raise PrivateStateError("ownership_receipt_unavailable")
    try:
        data = _frame(instance_id, nonce, b"N")
        if os.pwrite(descriptor, data, 0) != len(data):
            raise OSError()
        os.fsync(descriptor)
    except OSError:
        raise PrivateStateError("ownership_receipt_unavailable") from None


def _read_receipt(slot_directory: Path, metadata: dict[str, Any]) -> tuple[bytes, os.stat_result] | None:
    """Read only a stable private frame bound to this journal entry."""
    if not _metadata(metadata):
        return None
    try:
        slot = Path(slot_directory).absolute()
        directory = slot / "ownership-receipts"
        _private_directory(slot)
        _private_directory(directory)
        path = directory / (metadata["instance_id"] + ".receipt")
        check_private_file(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size != RECEIPT_BYTES):
                return None
            frame = source.read(RECEIPT_BYTES + 1)
            after, current = os.fstat(source.fileno()), path.lstat()
            if any(getattr(before, key) != getattr(after, key) or getattr(after, key) != getattr(current, key)
                   for key in _ATTRIBUTES):
                return None
        _private_directory(slot)
        _private_directory(directory)
        check_private_file(path)
        if frame not in tuple(_frame(metadata["instance_id"], metadata["nonce"], state)
                              for state in (b"P", b"C", b"N")):
            return None
        return frame, after
    except (OSError, ValueError, TypeError, PrivateStateError):
        return None


def verify_receipt(slot_directory: Path, metadata: dict[str, Any]) -> bool:
    """Require stable identity-bound native completion or proven nonlaunch.

    This read never creates directories, follows receipt links, signals a PID,
    or mutates the ownership ledger. Every absent/unsafe/invalid input fails shut.
    """
    receipt = _read_receipt(slot_directory, metadata)
    return receipt is not None and receipt[0][-1:] in (b"C", b"N")


def verify_previous_boot_receipt(slot_directory: Path, metadata: dict[str, Any],
                                 boot: BootSession | None, launch_boot_id: str | None = None) -> bool:
    """A previous kernel boot proves even an unfinished launch has ended.

    A legacy receipt first needs an observed boot UUID journaled by the host.
    File dates cannot prove a reboot because the wall clock can change.
    The same private/frame checks still apply.
    """
    if boot is None:
        return False
    receipt = _read_receipt(slot_directory, metadata)
    if receipt is None:
        return False
    return valid_boot_identity(launch_boot_id) and launch_boot_id.lower() != boot.identity
