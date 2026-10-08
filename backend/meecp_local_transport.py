"""Owner-only Unix HTTP transport shared by bundled clients; no TCP fallback."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import socket
import stat
import uuid
import unicodedata

import httpx


def _service_name(service: str) -> str:
    if service not in {"manager", "jevk5", "authorization"} and str(uuid.UUID(service)) != service:
        raise ValueError("invalid_private_service")
    return service


def socket_path(state_dir: Path, service: str, *, create: bool = False) -> Path:
    # macOS sockaddr_un is too short for Application Support + a slot UUID.
    # A deterministic short directory is private to the owner of this state.
    state = str(state_dir.resolve(strict=True))
    if platform.system() == "Darwin":
        state = unicodedata.normalize("NFC", state)
    identity = hashlib.sha256(os.fsencode(state)).hexdigest()[:24]
    base = Path("/private/tmp" if platform.system() == "Darwin" else "/tmp")
    directory = base / f"mcpme-{os.getuid()}-{identity}"
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    details = directory.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise ValueError("unsafe_private_transport")
    return directory / (_service_name(service) + ".sock")


def checked_socket(path: Path) -> str:
    directory = path.parent.lstat()
    details = path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600):
        raise ValueError("unsafe_private_transport")
    return str(path)


def bind_private_listener(state_dir: Path, service: str) -> socket.socket:
    """Caller must hold service ownership before replacing its stale socket."""
    path = socket_path(state_dir, service, create=True)
    if path.exists() or path.is_symlink():
        checked_socket(path)
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


def private_transport(path: Path) -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(uds=checked_socket(path), trust_env=False, retries=0)
