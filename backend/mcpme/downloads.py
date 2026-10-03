"""Pinned, bounded prepared-package ZIP intake; this module never executes code.

Fetching the same descriptor coalesces active/completed jobs. A failed or canceled
descriptor starts another attempt under its existing identity. Partial transfers
are never resumed. The production transport uses direct, public-IP-pinned TLS.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import ipaddress
import itertools
import json
import math
import os
import re
import shutil
import socket
import ssl
import stat
import struct
import threading
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from .contracts import (MAX_ARTIFACTS, MAX_PACKAGE_BYTES, PackageError,
                        package_relative_path, validate_package)
from .private import (PrivateStateError, check_private_file, ensure_private_directory,
                      read_private_json, write_private_json)

MAX_DOWNLOADS = 4096
MAX_ACTIVE_DOWNLOADS = 4
MAX_ARCHIVE_BYTES = MAX_PACKAGE_BYTES + 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = MAX_PACKAGE_BYTES + 17 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = MAX_ARTIFACTS * 2 + 2
MAX_ZIP_METADATA_BYTES = 16 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
CHUNK_BYTES = 64 * 1024
DISK_HEADROOM_BYTES = 16 * 1024 * 1024
NETWORK_IDLE_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 3600
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = {"completed", "failed", "canceled"}
_ERROR_CODES = {
    "download_network_blocked", "download_network_unavailable", "download_response_invalid",
    "download_redirect_blocked", "download_http_failed", "download_size_mismatch", "download_hash_mismatch",
    "download_archive_unsafe", "download_archive_limit", "download_package_invalid",
    "download_package_digest_mismatch", "download_disk_unavailable", "download_interrupted",
    "download_canceled", "download_timeout", "download_failed", "downloads_unavailable",
}


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DownloadError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def normalize_spec(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) - {"url", "sha256", "size", "package_digest"}
            or not {"url", "sha256", "size"} <= set(value)
            or type(value["size"]) is not int or not 1 <= value["size"] <= MAX_ARCHIVE_BYTES
            or not isinstance(value["sha256"], str) or not _HASH.fullmatch(value["sha256"])
            or ("package_digest" in value and (not isinstance(value["package_digest"], str)
                                                or not _HASH.fullmatch(value["package_digest"])) )):
        raise DownloadError("invalid_download_spec")
    raw = value["url"]
    if (not isinstance(raw, str) or not 1 <= len(raw) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in raw) or "\\" in raw
            or "?" in raw or "#" in raw):
        raise DownloadError("invalid_download_url")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or host.endswith(".") or "%" in host):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                                      for part in host.split(".")):
                raise ValueError()
            authority = host.lower()
        else:
            if not _public_address(address):
                raise DownloadError("download_network_blocked")
            authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
        path = parsed.path or "/"
        if not path.startswith("/") or re.search(r"%(?![0-9a-fA-F]{2})", path):
            raise ValueError()
        # Encoded control bytes would make a locator ambiguous to an HTTP server.
        if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", path, re.IGNORECASE):
            raise ValueError()
        url = urlunsplit(("https", authority, path, "", ""))
    except DownloadError:
        raise
    except (ValueError, UnicodeError):
        raise DownloadError("invalid_download_url") from None
    result = {"url": url, "sha256": value["sha256"], "size": value["size"]}
    if "package_digest" in value:
        result["package_digest"] = value["package_digest"]
    return result


def _public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _public_address(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv6Address) and any(address in network for network in (
            ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("64:ff9b:1::/48"),
            ipaddress.IPv6Network("2002::/16"), ipaddress.IPv6Network("2001::/32"))):
        # Translation/tunnel endpoints do not establish the final peer's scope.
        return False
    return address.is_global and not (address.is_multicast or address.is_reserved
                                      or address.is_unspecified or address.is_loopback)


class PinnedHTTPSNetwork:
    """No proxy, authentication, cookies, redirect, or DNS re-resolution on connect."""

    @contextlib.asynccontextmanager
    async def stream(self, url: str) -> AsyncIterator[AsyncIterator[bytes]]:
        parsed = urlsplit(url)
        host = parsed.hostname
        writer = None
        try:
            async with asyncio.timeout(NETWORK_IDLE_SECONDS):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            if not addresses or len(addresses) > 64:
                raise DownloadError("download_network_blocked")
            resolved = []
            for family, _, _, _, address in addresses:
                ip = ipaddress.ip_address(address[0])
                if family not in (socket.AF_INET, socket.AF_INET6) or not _public_address(ip):
                    raise DownloadError("download_network_blocked")
                if (family, str(ip)) not in resolved:
                    resolved.append((family, str(ip)))
            context = ssl.create_default_context()
            # Connecting to the vetted numeric address cannot trigger a second DNS
            # lookup. TLS verifies the selected URL hostname, including SNI.
            async with asyncio.timeout(NETWORK_IDLE_SECONDS):
                reader, writer = await asyncio.open_connection(
                    resolved[0][1], 443, family=resolved[0][0], ssl=context,
                    server_hostname=host, limit=8192)
                request = (f"GET {parsed.path or '/'} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                           "Accept: application/zip\r\nAccept-Encoding: identity\r\n"
                           "Connection: close\r\nUser-Agent: mcpme/1\r\n\r\n")
                writer.write(request.encode("ascii"))
                await writer.drain()
                headers = await self._headers(reader)
            yield self._body(reader, headers)
        except DownloadError:
            raise
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise DownloadError("download_network_unavailable") from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(2):
                        await writer.wait_closed()
                except (OSError, asyncio.TimeoutError):
                    pass

    async def _line(self, reader: asyncio.StreamReader) -> bytes:
        async with asyncio.timeout(NETWORK_IDLE_SECONDS):
            value = await reader.readline()
        if len(value) > 8192 or not value.endswith(b"\r\n"):
            raise DownloadError("download_response_invalid")
        return value

    async def _headers(self, reader: asyncio.StreamReader) -> dict[bytes, bytes]:
        first = await self._line(reader)
        if not re.fullmatch(rb"HTTP/1\.[01] [0-9]{3}(?: [\x20-\x7e]*)?\r\n", first):
            raise DownloadError("download_response_invalid")
        status = int(first.split(b" ")[1])
        if 300 <= status <= 399:
            raise DownloadError("download_redirect_blocked")
        if status != 200:
            raise DownloadError("download_http_failed")
        headers: dict[bytes, bytes] = {}
        total = len(first)
        for _ in range(128):
            line = await self._line(reader)
            total += len(line)
            if total > 65536:
                raise DownloadError("download_response_invalid")
            if line == b"\r\n":
                break
            name, separator, raw = line[:-2].partition(b":")
            if (not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or any(char < 32 and char != 9 or char == 127 for char in raw)):
                raise DownloadError("download_response_invalid")
            name = name.lower()
            if name in headers:
                raise DownloadError("download_response_invalid")
            headers[name] = raw.strip()
        else:
            raise DownloadError("download_response_invalid")
        if headers.get(b"content-encoding", b"identity").lower() != b"identity":
            raise DownloadError("download_response_invalid")
        if b"transfer-encoding" in headers:
            if headers[b"transfer-encoding"].lower() != b"chunked" or b"content-length" in headers:
                raise DownloadError("download_response_invalid")
        if b"content-length" in headers:
            length = headers[b"content-length"]
            if not re.fullmatch(rb"[0-9]{1,20}", length) or int(length) > MAX_ARCHIVE_BYTES:
                raise DownloadError("download_response_invalid")
        return headers

    async def _read(self, reader: asyncio.StreamReader, amount: int, *, exact: bool = False) -> bytes:
        async with asyncio.timeout(NETWORK_IDLE_SECONDS):
            return await (reader.readexactly(amount) if exact else reader.read(amount))

    async def _body(self, reader: asyncio.StreamReader, headers: dict[bytes, bytes]) -> AsyncIterator[bytes]:
        if b"transfer-encoding" in headers:
            while True:
                line = await self._line(reader)
                # Chunk extensions and trailers are deliberately unsupported.
                if not re.fullmatch(rb"[0-9a-fA-F]{1,16}\r\n", line):
                    raise DownloadError("download_response_invalid")
                remaining = int(line[:-2], 16)
                if remaining > MAX_ARCHIVE_BYTES:
                    raise DownloadError("download_response_invalid")
                if remaining == 0:
                    if await self._line(reader) != b"\r\n":
                        raise DownloadError("download_response_invalid")
                    return
                while remaining:
                    data = await self._read(reader, min(CHUNK_BYTES, remaining), exact=True)
                    remaining -= len(data)
                    yield data
                if await self._read(reader, 2, exact=True) != b"\r\n":
                    raise DownloadError("download_response_invalid")
        elif b"content-length" in headers:
            remaining = int(headers[b"content-length"])
            while remaining:
                data = await self._read(reader, min(CHUNK_BYTES, remaining), exact=True)
                remaining -= len(data)
                yield data
            # Closing framing cannot conceal bytes following the selected body.
            if await self._read(reader, 1):
                raise DownloadError("download_response_invalid")
        else:
            while data := await self._read(reader, CHUNK_BYTES):
                yield data


def _check_canceled(event: threading.Event) -> None:
    if event.is_set():
        raise DownloadError("download_canceled")


def _zip_directory(archive: Path) -> tuple[int, int]:
    """Bound central metadata before ZipFile can allocate it; permit bounded ZIP64."""
    size = archive.stat().st_size
    with archive.open("rb") as source:
        if source.read(4) != b"PK\x03\x04" or size < 22:
            raise DownloadError("download_archive_unsafe")
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0 or len(tail) - position < 22:
            raise DownloadError("download_archive_unsafe")
        end = size - len(tail) + position
        signature, disk, central_disk, disk_count, count, metadata_bytes, offset, comment = struct.unpack(
            "<4s4H2LH", tail[position:position + 22])
        if disk or central_disk or comment or end + 22 != size or disk_count != count:
            raise DownloadError("download_archive_unsafe")
        if count == 65535 or metadata_bytes == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            if end < 20:
                raise DownloadError("download_archive_unsafe")
            source.seek(end - 20)
            locator, start_disk, zip64_offset, disks = struct.unpack("<4sLQL", source.read(20))
            if locator != b"PK\x06\x07" or start_disk or disks != 1 or zip64_offset + 56 > end - 20:
                raise DownloadError("download_archive_unsafe")
            source.seek(zip64_offset)
            details = source.read(56)
            (signature, record_bytes, _, _, disk, central_disk, disk_count, count,
             metadata_bytes, offset) = struct.unpack("<4sQ2H2L4Q", details)
            if (signature != b"PK\x06\x06" or not 44 <= record_bytes <= 1024
                    or zip64_offset + 12 + record_bytes != end - 20 or disk or central_disk or disk_count != count):
                raise DownloadError("download_archive_unsafe")
            end = zip64_offset
        if count > MAX_ARCHIVE_ENTRIES or metadata_bytes > MAX_ZIP_METADATA_BYTES:
            raise DownloadError("download_archive_limit")
        if offset + metadata_bytes != end:
            raise DownloadError("download_archive_unsafe")
    return count, offset


def _archive_entries(archive: Path) -> tuple[zipfile.ZipFile, list[tuple[zipfile.ZipInfo, str, bool]], int]:
    declared_count, central_offset = _zip_directory(archive)
    source = zipfile.ZipFile(archive)
    try:
        infos = source.infolist()
        if len(infos) != declared_count:
            raise DownloadError("download_archive_unsafe")
        names: dict[str, str] = {}
        explicit: set[str] = set()
        parents: set[str] = set()
        files: set[str] = set()
        entries = []
        total = 0
        for entry in infos:
            directory = entry.is_dir()
            name = entry.orig_filename[:-1] if directory else entry.orig_filename
            try:
                parts = package_relative_path(name).parts
            except PackageError:
                raise DownloadError("download_archive_unsafe") from None
            if (len(name.encode("utf-8")) > 4096 or len(parts) > 128 or re.match(r"^[A-Za-z]:", name)
                    or "\x00" in entry.orig_filename or entry.filename != entry.orig_filename
                    or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or not 0 <= entry.header_offset < central_offset):
                raise DownloadError("download_archive_unsafe")
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG) or mode & 0o7000
                    or len(entry.extra) > 4096 or len(entry.comment) > 4096):
                raise DownloadError("download_archive_unsafe")
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                folded = unicodedata.normalize("NFC", prefix).casefold()
                if folded in names and names[folded] != prefix or prefix in files and index < len(parts):
                    raise DownloadError("download_archive_unsafe")
                names[folded] = prefix
                if index < len(parts):
                    parents.add(prefix)
            if name in explicit or not directory and name in parents:
                raise DownloadError("download_archive_unsafe")
            explicit.add(name)
            if directory:
                if entry.file_size or entry.compress_size:
                    raise DownloadError("download_archive_unsafe")
            else:
                files.add(name)
                total += entry.file_size
                if (entry.file_size > MAX_EXTRACTED_BYTES or total > MAX_EXTRACTED_BYTES
                        or entry.file_size > max(1, entry.compress_size) * MAX_COMPRESSION_RATIO):
                    raise DownloadError("download_archive_limit")
            entries.append((entry, name, directory))
        return source, entries, total
    except BaseException:
        source.close()
        raise


class DownloadManager:
    def __init__(self, state_root: Path, *, network: Any = None):
        self.root = ensure_private_directory(Path(state_root).absolute() / "downloads")
        self.records_root = ensure_private_directory(self.root / "records")
        self.staging = ensure_private_directory(self.root / "staging")
        self.packages = ensure_private_directory(self.root / "packages")
        lock_path = self.root / "owner.lock"
        if lock_path.exists() or lock_path.is_symlink():
            check_private_file(lock_path)
        self._lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            details = os.fstat(self._lock_fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or stat.S_IMODE(details.st_mode) != 0o600:
                raise PrivateStateError()
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PrivateStateError):
            os.close(self._lock_fd)
            raise DownloadError("downloads_unavailable") from None
        self.network = network if network is not None else PinnedHTTPSNetwork()
        self._records: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, threading.Event] = {}
        self._cancel_codes: dict[str, str] = {}
        self._reservations: dict[str, int] = {}
        self._mutex = threading.RLock()
        self._closed = False
        self._closing = False
        self._close_task: asyncio.Task | None = None
        try:
            self._recover()
        except BaseException:
            os.close(self._lock_fd)
            raise

    def _recover(self) -> None:
        entries = list(itertools.islice(self.records_root.iterdir(), MAX_DOWNLOADS * 2 + 1))
        if len(entries) > MAX_DOWNLOADS * 2:
            raise DownloadError("downloads_unavailable")
        paths = []
        for path in entries:
            if re.fullmatch(r"[0-9a-f-]{36}\.json\.[0-9a-f]{24}\.tmp", path.name):
                check_private_file(path)
                path.unlink()
            else:
                paths.append(path)
        if len(paths) > MAX_DOWNLOADS:
            raise DownloadError("downloads_unavailable")
        if len(paths) != len(entries):
            _sync_directory(self.records_root)
        for path in paths:
            try:
                value = read_private_json(path, max_bytes=16384)
                identity = value["download_id"]
                descriptor = normalize_spec(value["descriptor"])
                fields = {"schema_version", "download_id", "descriptor", "status", "stage", "received_bytes", "size",
                          "sha256", "attempt", "created_at", "updated_at", "error_code", "cancel_requested"}
                if value["status"] == "completed":
                    fields |= {"package_path", "package_digest"}
                stages = {"pending": {"pending", "canceling"}, "running": {"downloading", "verifying", "canceling"},
                          "completed": {"completed"}, "failed": {"failed"}, "canceled": {"canceled"}}
                if (set(value) != fields or path.name != identity + ".json" or self._identity(descriptor) != identity
                        or type(value["schema_version"]) is not int or value["schema_version"] != 1
                        or value["status"] not in stages or value["stage"] not in stages[value["status"]]
                        or type(value["attempt"]) is not int or value["attempt"] < 1
                        or type(value["received_bytes"]) is not int or not 0 <= value["received_bytes"] <= descriptor["size"]
                        or type(value["size"]) is not int or value["size"] != descriptor["size"]
                        or value["sha256"] != descriptor["sha256"] or descriptor != value["descriptor"]
                        or type(value["cancel_requested"]) is not bool
                        or value["cancel_requested"] != (value["stage"] == "canceling")
                        or any(type(value[key]) not in {int, float} or not 0 < value[key] <= 253402300799
                               or not math.isfinite(value[key])
                               for key in ("created_at", "updated_at"))
                        or value["updated_at"] < value["created_at"]
                        or (value["status"] in {"pending", "running", "completed"} and value["error_code"] is not None)
                        or (value["status"] == "failed" and value["error_code"] not in _ERROR_CODES)
                        or (value["status"] == "canceled" and value["error_code"] != "download_canceled")):
                    raise ValueError()
                if value["status"] == "completed":
                    expected = str(self.packages / identity)
                    if (value.get("package_path") != expected or not isinstance(value.get("package_digest"), str)
                            or not _HASH.fullmatch(value["package_digest"])
                            or descriptor.get("package_digest", value["package_digest"]) != value["package_digest"]
                            or value["received_bytes"] != descriptor["size"]):
                        raise ValueError()
                    completed_path = Path(expected)
                    if not completed_path.exists() and not completed_path.is_symlink():
                        # Never recreate an absent completed tree and present the
                        # resulting empty directory as a verified package.
                        value.pop("package_path")
                        value.pop("package_digest")
                        value.update(status="failed", stage="failed", error_code="download_package_invalid")
                        self._save(value)
                    else:
                        ensure_private_directory(completed_path)
                self._records[identity] = value
                if value["status"] not in _TERMINAL:
                    value.update(status="failed", stage="failed", error_code="download_interrupted", cancel_requested=False)
                    self._save(value)
            except (KeyError, TypeError, ValueError, PrivateStateError):
                raise DownloadError("downloads_unavailable") from None
        # The lock excludes another manager, including one waiting on extraction.
        # Only recognized private staging trees are reclaimed, never package roots.
        for path in self.staging.iterdir():
            if not re.fullmatch(r"[0-9a-f-]{36}\.[1-9][0-9]*", path.name):
                raise DownloadError("downloads_unavailable")
            self._remove_stage(path)

    @staticmethod
    def _identity(spec: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return str(uuid.UUID(hex=digest[:32], version=5))

    def _save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = max(time.time(), record["created_at"], record.get("updated_at", 0))
        write_private_json(self.records_root / (record["download_id"] + ".json"), record)

    def _record(self, identity: str) -> dict[str, Any]:
        if self._closed:
            raise DownloadError("downloads_unavailable")
        if not isinstance(identity, str) or identity not in self._records:
            raise DownloadError("download_not_found")
        return self._records[identity]

    def get(self, identity: str) -> dict[str, Any]:
        with self._mutex:
            return json.loads(json.dumps(self._record(identity)))

    def list(self) -> list[dict[str, Any]]:
        with self._mutex:
            if self._closed:
                raise DownloadError("downloads_unavailable")
            fields = {"schema_version", "download_id", "status", "stage", "received_bytes", "size", "sha256",
                      "attempt", "error_code", "cancel_requested", "created_at", "updated_at", "package_digest"}
            return [{key: value for key, value in record.items() if key in fields}
                    for record in sorted(self._records.values(), key=lambda value: value["created_at"], reverse=True)[:100]]

    def count(self) -> int:
        with self._mutex:
            if self._closed:
                raise DownloadError("downloads_unavailable")
            return len(self._records)

    def _reserve(self, identity: str, amount: int) -> None:
        with self._mutex:
            other = sum(size for key, size in self._reservations.items() if key != identity)
            if shutil.disk_usage(self.root).free < amount + other + DISK_HEADROOM_BYTES:
                raise DownloadError("download_disk_unavailable")
            self._reservations[identity] = amount

    def _consumed(self, identity: str, amount: int) -> None:
        with self._mutex:
            self._reservations[identity] = max(0, self._reservations.get(identity, 0) - amount)

    async def start(self, spec: dict[str, Any]) -> dict[str, Any]:
        descriptor = normalize_spec(spec)
        identity = self._identity(descriptor)
        with self._mutex:
            if self._closed or self._closing:
                raise DownloadError("downloads_unavailable")
            existing = self._records.get(identity)
            if existing and (existing["status"] not in {"failed", "canceled"} or identity in self._tasks):
                return self.get(identity)
            if (len(self._tasks) >= MAX_ACTIVE_DOWNLOADS
                    or existing is None and len(self._records) >= MAX_DOWNLOADS):
                raise DownloadError("download_limit")
            self._reserve(identity, descriptor["size"])
            record = {"schema_version": 1, "download_id": identity, "descriptor": descriptor,
                      "status": "pending", "stage": "pending", "received_bytes": 0,
                      "size": descriptor["size"], "sha256": descriptor["sha256"],
                      "attempt": existing["attempt"] + 1 if existing else 1,
                      "created_at": existing["created_at"] if existing else time.time(),
                      "error_code": None, "cancel_requested": False}
            try:
                self._save(record)
            except BaseException:
                self._reservations.pop(identity, None)
                raise
            self._records[identity] = record
            self._events[identity] = threading.Event()
            self._cancel_codes.pop(identity, None)
            task = asyncio.create_task(self._run(identity), name="mcpme-download")
            self._tasks[identity] = task
            task.add_done_callback(lambda finished, key=identity: self._finished(key, finished))
            return self.get(identity)

    def _finished(self, identity: str, task: asyncio.Task) -> None:
        # Consume unexpected persistence errors without asyncio exception logging.
        if not task.cancelled():
            task.exception()
        with self._mutex:
            if self._tasks.get(identity) is task:
                self._tasks.pop(identity, None)
                self._events.pop(identity, None)
                self._reservations.pop(identity, None)

    async def cancel(self, identity: str) -> dict[str, Any]:
        with self._mutex:
            record = self._record(identity)
            if record["status"] in _TERMINAL:
                return self.get(identity)
            self._request_cancel(identity, "download_canceled")
            return self.get(identity)

    def _request_cancel(self, identity: str, code: str) -> None:
        record = self._records[identity]
        if record["cancel_requested"]:
            return
        record.update(cancel_requested=True, stage="canceling")
        self._cancel_codes[identity] = code
        self._events[identity].set()
        # A just-created task must run once to establish its cleanup handler.
        if record["status"] == "running":
            self._tasks[identity].cancel()
        # Cancellation is an in-memory ownership obligation even when an atomic
        # record write fails. Retry/close must still join the signaled task.
        self._save(record)

    async def _thread(self, function, *args, cancel_event: threading.Event | None = None):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        canceled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                canceled = True
                if cancel_event is not None:
                    cancel_event.set()
            except BaseException:
                break
        if canceled:
            with contextlib.suppress(BaseException):
                task.result()
            raise asyncio.CancelledError()
        return task.result()

    def _extract(self, identity: str, archive: Path, package: Path, event: threading.Event) -> str:
        _check_canceled(event)
        try:
            source, entries, total = _archive_entries(archive)
            with source:
                self._reserve(identity, total)
                package.mkdir(mode=0o700)
                for entry, name, directory in entries:
                    _check_canceled(event)
                    destination = package / name
                    for ancestor in reversed(destination.parents):
                        if ancestor == package or package in ancestor.parents:
                            ensure_private_directory(ancestor)
                    if directory:
                        ensure_private_directory(destination)
                        continue
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as output, source.open(entry) as input_file:
                        copied = 0
                        while data := input_file.read(min(CHUNK_BYTES, entry.file_size - copied + 1)):
                            _check_canceled(event)
                            copied += len(data)
                            if copied > entry.file_size:
                                raise DownloadError("download_archive_limit")
                            output.write(data)
                            self._consumed(identity, len(data))
                        if copied != entry.file_size:
                            raise DownloadError("download_archive_unsafe")
                        output.flush()
                        os.fchmod(output.fileno(), 0o700 if entry.external_attr >> 16 & 0o111 else 0o600)
                        os.fsync(output.fileno())
                for directory, _, _ in os.walk(package, topdown=False, followlinks=False):
                    _check_canceled(event)
                    _sync_directory(Path(directory))
                _check_canceled(event)
                verified = validate_package(package)
                _check_canceled(event)
                return verified.digest
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, struct.error, UnicodeError):
            raise DownloadError("download_archive_unsafe") from None

    @staticmethod
    def _remove_stage(path: Path) -> None:
        if path.exists() or path.is_symlink():
            ensure_private_directory(path)
            shutil.rmtree(path)

    async def _run(self, identity: str) -> None:
        record = self._records[identity]
        event = self._events[identity]
        stage = self.staging / f"{identity}.{record['attempt']}"
        error = None
        completed = False
        try:
            _check_canceled(event)
            stage.mkdir(mode=0o700)
            record.update(status="running", stage="downloading")
            self._save(record)
            archive = stage / "archive.zip"
            descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            digest = hashlib.sha256()
            last_save = time.monotonic()
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
                with os.fdopen(descriptor, "wb") as output:
                    async with self.network.stream(record["descriptor"]["url"]) as stream:
                        async for data in stream:
                            _check_canceled(event)
                            if not isinstance(data, bytes) or len(data) > CHUNK_BYTES:
                                raise DownloadError("download_response_invalid")
                            if not data:
                                continue
                            if record["received_bytes"] + len(data) > record["size"]:
                                raise DownloadError("download_size_mismatch")
                            output.write(data)
                            digest.update(data)
                            record["received_bytes"] += len(data)
                            self._consumed(identity, len(data))
                            if time.monotonic() - last_save >= 0.5:
                                self._save(record)
                                last_save = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                _check_canceled(event)
                if record["received_bytes"] != record["size"]:
                    raise DownloadError("download_size_mismatch")
                if digest.hexdigest() != record["sha256"]:
                    raise DownloadError("download_hash_mismatch")
                record["stage"] = "verifying"
                self._save(record)
                package = stage / "package"
                package_digest = await self._thread(self._extract, identity, archive, package, event, cancel_event=event)
                _check_canceled(event)
                if record["descriptor"].get("package_digest", package_digest) != package_digest:
                    raise DownloadError("download_package_digest_mismatch")
                destination = self.packages / identity
                if destination.exists() or destination.is_symlink():
                    # A crash after rename but before record publication leaves a
                    # retained candidate. Validate it and never overwrite it.
                    ensure_private_directory(destination)
                    retained = await self._thread(validate_package, destination)
                    _check_canceled(event)
                    if retained.digest != package_digest:
                        raise DownloadError("download_package_digest_mismatch")
                else:
                    os.rename(package, destination)
                    _sync_directory(self.packages)
                record.update(status="completed", stage="completed", package_path=str(destination),
                              package_digest=package_digest, error_code=None, cancel_requested=False)
                self._save(record)
                completed = True
        except asyncio.CancelledError:
            event.set()
            error = self._cancel_codes.get(identity, "download_interrupted")
        except DownloadError as failure:
            error = self._cancel_codes.get(identity, failure.code if failure.code in _ERROR_CODES else "download_failed")
        except PackageError:
            error = "download_package_invalid"
        except PrivateStateError:
            error = "downloads_unavailable"
        except OSError as failure:
            error = "download_disk_unavailable" if failure.errno in (28, 122) else "download_failed"
        except asyncio.TimeoutError:
            event.set()
            error = "download_timeout"
        except Exception:
            error = "download_failed"
        finally:
            # Threads have settled before reaching cleanup, even on cancellation.
            try:
                self._remove_stage(stage)
            except (OSError, PrivateStateError):
                error = error or "downloads_unavailable"
            if not completed:
                record.pop("package_path", None)
                record.pop("package_digest", None)
                status = "canceled" if error == "download_canceled" else "failed"
                record.update(status=status, stage=status, error_code=error or "download_failed", cancel_requested=False)
                self._save(record)

    async def close(self) -> None:
        if self._closed:
            return
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close())
        canceled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                canceled = True
        self._close_task.result()
        if canceled:
            raise asyncio.CancelledError()

    async def _close(self) -> None:
        persistence_error = None
        with self._mutex:
            for identity in tuple(self._tasks):
                if self._records[identity]["status"] not in _TERMINAL:
                    try:
                        self._request_cancel(identity, "download_interrupted")
                    except PrivateStateError as error:
                        persistence_error = error
            tasks = tuple(self._tasks.values())
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._closed = True
            os.close(self._lock_fd)
        if persistence_error is not None:
            raise persistence_error
