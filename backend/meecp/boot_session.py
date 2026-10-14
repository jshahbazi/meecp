"""Kernel boot identity for settling ownership left behind by a Mac restart."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
import sys
import uuid


@dataclass(frozen=True)
class BootSession:
    identity: str


def valid_boot_identity(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def current_boot_session() -> BootSession | None:
    """Read kernel metadata; unavailable/invalid metadata proves nothing.

    Boot UUIDs are independent of wall-clock changes. Linux is supported for
    the isolated host test harness.
    """
    try:
        if sys.platform == "darwin":
            sysctl = ctypes.CDLL(None).sysctlbyname
            sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_void_p, ctypes.c_size_t]
            sysctl.restype = ctypes.c_int
            identity = ctypes.create_string_buffer(128)
            size = ctypes.c_size_t(ctypes.sizeof(identity))
            if sysctl(b"kern.bootsessionuuid", identity, ctypes.byref(size), None, 0) != 0:
                return None
            boot_id = identity.value.decode("ascii").lower()
        elif sys.platform == "linux":
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip().lower()
        else:
            return None
        if not valid_boot_identity(boot_id):
            return None
        return BootSession(boot_id)
    except (OSError, ValueError, AttributeError, UnicodeError, StopIteration):
        return None
