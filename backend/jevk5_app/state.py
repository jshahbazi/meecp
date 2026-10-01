"""Owner-only connection state, process ownership, and verified model identity."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
from pathlib import Path


HOST = "127.0.0.1"
PORT = 19385
ENDPOINT = f"http://{HOST}:{PORT}"


def atomic_private_json(path: Path, data: dict):
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PrivateState:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise RuntimeError("Invalid private state directory.")
        directory.chmod(0o700)
        self.lock_fd = os.open(directory / "backend.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(self.lock_fd, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            raise RuntimeError("The local service is already running.") from None
        path = directory / "connection.json"
        token = None
        if path.exists() and not path.is_symlink() and path.stat().st_uid == os.getuid():
            try:
                candidate = json.loads(path.read_text()).get("token")
                if isinstance(candidate, str) and len(candidate) >= 32:
                    token = candidate
            except (ValueError, OSError):
                pass
        self.token = token or secrets.token_urlsafe(48)
        atomic_private_json(path, {"endpoint": ENDPOINT, "mcp_endpoint": ENDPOINT + "/mcp", "token": self.token})

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def verified_model(resources: Path, state_dir: Path) -> tuple[dict, Path]:
    manifest = json.loads((resources / "model-manifest.json").read_text())
    filename = manifest["file"]
    if Path(filename).name != filename or not filename.endswith(".gguf"):
        raise ValueError("Invalid model manifest.")
    digest = manifest["sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid model manifest.")
    if (manifest["temperature"], manifest["knockout_temperature"], manifest["context_tokens"]) != (1.22, 0.93, 8192):
        raise ValueError("Unsupported model profile.")
    model = resources / "models" / filename
    details = model.stat()
    if details.st_size != manifest["bytes"]:
        raise ValueError("Model size verification failed.")
    identity = {"sha256": digest, "bytes": details.st_size, "mtime_ns": details.st_mtime_ns,
                "ctime_ns": details.st_ctime_ns, "path": str(model.resolve())}
    receipt = state_dir / "model-verification.json"
    try:
        if not receipt.is_symlink() and json.loads(receipt.read_text()) == identity:
            return manifest, model
    except (ValueError, OSError):
        pass
    actual = hashlib.sha256()
    with model.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            actual.update(block)
    current = model.stat()
    attributes = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev")
    if actual.hexdigest() != digest or any(getattr(current, field) != getattr(details, field) for field in attributes):
        raise ValueError("Model hash verification failed.")
    atomic_private_json(receipt, identity)
    return manifest, model
