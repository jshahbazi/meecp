"""Authenticated, bounded PyPI index broker for a network-confined private uv.

Only public registry wheels and hash-bound PEP 658 metadata are exposed. Source
distributions, redirects, arbitrary URLs and direct dependency URLs cannot be
requested through this broker. No selected code runs in the broker process.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from .downloads import PinnedHTTPSNetwork
from .preparation_contracts import PreparationError
from .processes import _await_ownership

MAX_INDEX_BYTES = 16 * 1024**2
MAX_METADATA_BYTES = 2 * 1024**2
MAX_WHEEL_BYTES = 64 * 1024**2
_HASH = re.compile(r"^[a-f0-9]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


def sandbox_profile(workspace: Path, runtime: Path, executable: Path, *, broker_port: int | None = None, fork=False,
                    read_paths=None, write_paths=None, interpreter=None):
    """Reviewed macOS profile; failure to apply is a failed owned operation.

    uv may exec only the pinned interpreter for version checks. Discovery has
    no fork permission. Both can write only inside the attempt workspace.
    """
    reads = [workspace] if read_paths is None else list(read_paths)
    writes = [workspace] if write_paths is None else list(write_paths)
    interpreter = interpreter or runtime / "bin/python3.12"
    paths = (workspace, runtime, executable, interpreter, *reads, *writes)
    if any(not path.is_absolute() or path.resolve() != path for path in paths):
        raise PreparationError("unsafe_resolution_sandbox_path")
    quote = lambda path: json.dumps(str(path))
    profile = ['(version 1)', '(deny default)', '(allow sysctl-read)',
        '(allow file-read-metadata)',
        '(allow file-read* (literal "/") (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share") '
        '(subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
        '(allow file-read* ' + ' '.join(f'(subpath {quote(path)})' for path in [runtime, *reads]) + f' (literal {quote(executable)}))',
        '(allow file-write* ' + ' '.join(f'(subpath {quote(path)})' for path in writes) + ' (literal "/dev/null"))',
        f'(allow process-exec (literal {quote(executable)}) (literal {quote(interpreter)}))']
    if fork:
        profile.append('(allow process-fork)')
    if broker_port is not None:
        if type(broker_port) is not int or not 1 <= broker_port <= 65535:
            raise PreparationError("invalid_resolution_broker")
        profile.append(f'(allow network-outbound (remote tcp "localhost:{broker_port}"))')
    return "\n".join(profile)


class ResolutionIndexBroker:
    def __init__(self, directory: Path, *, byte_limit: int, check, network=None):
        self.directory, self.byte_limit, self.check = directory, min(byte_limit, 512 * 1024**2), check
        self.network = network or PinnedHTTPSNetwork()
        self.password = secrets.token_hex(32)
        self.authorization = "Basic " + base64.b64encode(("mcpme:" + self.password).encode()).decode()
        self.server = None
        self.tasks = set()
        self.catalog = {}
        self.indexes = {}
        self.bytes = 0
        self.requests = 0
        self.error_code = None
        self._serial = asyncio.Lock()

    async def __aenter__(self):
        self.directory.mkdir(mode=0o700)
        self.server = await asyncio.start_server(self._connect, "127.0.0.1", 0, limit=8192)
        self.port = self.server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await _await_ownership(asyncio.gather(*tuple(self.tasks), return_exceptions=True))

    def _connect(self, reader, writer):
        if len(self.tasks) >= 16:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self.tasks.add(task)
        def completed(value):
            self.tasks.discard(value)
            if not value.cancelled():
                value.exception()  # Never let transport diagnostics log payloads.
        task.add_done_callback(completed)

    async def _fetch(self, url, limit, *, expected_hash=None, expected_size=None, index=False):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != ("pypi.org" if index else "files.pythonhosted.org")
                or parts.username or parts.password or parts.port not in (None, 443) or parts.query or parts.fragment):
            raise PreparationError("resolution_network_blocked")
        result = bytearray()
        async with self.network.stream(url, accept="application/vnd.pypi.simple.v1+json" if index else "application/octet-stream",
                                       **({"gzip_limit": MAX_INDEX_BYTES} if index else {})) as chunks:
            async for chunk in chunks:
                self.check()
                self.bytes += len(chunk)
                if self.bytes > self.byte_limit or len(result) + len(chunk) > limit:
                    raise PreparationError("resolution_download_limit")
                result.extend(chunk)
        self.check()
        data = bytes(result)
        if ((expected_size is not None and len(data) != expected_size)
                or (expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash)):
            raise PreparationError("resolution_download_identity_mismatch")
        return data

    async def _index(self, name):
        if not _NAME.fullmatch(name) or canonicalize_name(name) != name:
            raise PreparationError("resolution_network_blocked")
        if name in self.indexes:
            return self.indexes[name]
        if len(self.indexes) >= 128:
            raise PreparationError("resolution_dependency_limit")
        # Simple JSON pages can exceed the general small-contract bound.
        raw = await self._fetch(f"https://pypi.org/simple/{name}/", MAX_INDEX_BYTES, index=True)
        try:
            upstream = json.loads(raw)
        except (ValueError, UnicodeError):
            raise PreparationError("invalid_resolution_index") from None
        files = upstream.get("files")
        if not isinstance(files, list) or len(files) > 65536:
            raise PreparationError("resolution_dependency_limit")
        exposed = []
        for item in files:
            filename = item.get("filename", "")
            if not isinstance(filename, str) or not filename.endswith(".whl") or item.get("yanked"):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,256}\.whl", filename):
                raise PreparationError("invalid_resolution_index")
            try:
                wheel_name, _, _, _ = parse_wheel_filename(filename)
            except ValueError:
                continue
            digest = item.get("hashes", {}).get("sha256")
            metadata = item.get("core-metadata", item.get("dist-info-metadata"))
            metadata_hash = metadata.get("sha256") if isinstance(metadata, dict) else None
            size = item.get("size")
            if (wheel_name != name or not isinstance(digest, str) or not _HASH.fullmatch(digest)
                    or type(size) is not int or not 0 < size <= MAX_WHEEL_BYTES):
                continue
            # Never let uv execute metadata hooks or trust an unhashed sidecar.
            if not isinstance(metadata_hash, str) or not _HASH.fullmatch(metadata_hash):
                continue
            url = item.get("url")
            if not isinstance(url, str) or len(url) > 4096:
                raise PreparationError("invalid_resolution_index")
            parts = urlsplit(url)
            if (parts.scheme != "https" or parts.hostname != "files.pythonhosted.org" or parts.username or parts.password
                    or parts.port not in (None, 443) or parts.query or parts.fragment or Path(parts.path).name != filename):
                raise PreparationError("resolution_network_blocked")
            path = f"/files/{digest}/{filename}"
            record = {"url": url, "sha256": digest, "size": size, "metadata_hash": metadata_hash}
            if path in self.catalog and self.catalog[path] != record:
                raise PreparationError("resolution_index_collision")
            self.catalog[path] = record
            if len(self.catalog) > 65536:
                raise PreparationError("resolution_dependency_limit")
            exposed.append({"filename": filename, "url": self.base + path, "hashes": {"sha256": digest},
                "size": size, "requires-python": item.get("requires-python"), "core-metadata": {"sha256": metadata_hash}})
        result = json.dumps({"meta": {"api-version": "1.1"}, "name": name, "files": exposed}, separators=(",", ":")).encode()
        if sum(map(len, self.indexes.values())) + len(result) > 64 * 1024**2:
            raise PreparationError("resolution_dependency_limit")
        self.indexes[name] = result
        return result

    async def _file(self, path):
        metadata = path.endswith(".metadata")
        key = path.removesuffix(".metadata") if metadata else path
        record = self.catalog.get(key)
        if record is None:
            raise PreparationError("resolution_network_blocked")
        identity = record["metadata_hash"] if metadata else record["sha256"]
        cached = self.directory / identity
        if not cached.exists():
            data = await self._fetch(record["url"] + (".metadata" if metadata else ""),
                MAX_METADATA_BYTES if metadata else MAX_WHEEL_BYTES, expected_hash=identity,
                expected_size=None if metadata else record["size"])
            if metadata:
                from email.parser import BytesParser
                parsed = BytesParser().parsebytes(data)
                for requirement in parsed.get_all("Requires-Dist", []):
                    if Requirement(requirement).url:
                        raise PreparationError("unsupported_resolution_dependency")
            cached.write_bytes(data)
            cached.chmod(0o400)
        return cached.read_bytes()

    async def _serve(self, reader, writer):
        try:
            async with asyncio.timeout(30):
                request = await reader.readuntil(b"\r\n\r\n")
                if len(request) > 8192:
                    raise PreparationError("invalid_resolution_broker_request")
                lines = request.decode("ascii").split("\r\n")
                method, path, protocol = lines[0].split(" ")
                headers = {}
                for line in lines[1:-2]:
                    key, sep, value = line.partition(":")
                    if not sep or key.lower() in headers:
                        raise PreparationError("invalid_resolution_broker_request")
                    headers[key.lower()] = value.strip()
                if (method != "GET" or protocol != "HTTP/1.1" or headers.get("host") != f"127.0.0.1:{self.port}"
                        or "origin" in headers or "transfer-encoding" in headers or headers.get("content-length", "0") != "0"
                        or not hmac.compare_digest(headers.get("authorization", ""), self.authorization)):
                    writer.write(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=\"resolution\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                async with self._serial:
                    self.check()
                    self.requests += 1
                    if self.requests > 2048:
                        raise PreparationError("resolution_request_limit")
                    if path.startswith("/simple/") and path.endswith("/"):
                        data = await self._index(path[8:-1])
                        mime = "application/vnd.pypi.simple.v1+json"
                    else:
                        data = await self._file(path)
                        mime = "application/octet-stream"
                    self.check()
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode())
                    for offset in range(0, len(data), 65536):
                        self.check()
                        writer.write(data[offset:offset + 65536])
                        await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error_code = error.code if isinstance(error, PreparationError) else "resolution_index_unavailable"
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except (OSError, RuntimeError):
                pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            except (OSError, RuntimeError, asyncio.CancelledError, TimeoutError):
                writer.transport.abort()

    def canonical_lock(self, data: bytes):
        """Replace broker locators only through exact previously observed mappings."""
        import tomllib
        try:
            text = data.decode()
            value = tomllib.loads(text)
            replacements = {self.base + "/simple": "https://pypi.org/simple"}
            packages = value["package"]
            if not isinstance(packages, list) or not 1 <= len(packages) <= 512:
                raise ValueError()
            for package in packages:
                source = package["source"]
                if source == {"virtual": "."}:
                    continue
                if source != {"registry": self.base + "/simple"}:
                    raise ValueError()
                package["source"] = {"registry": "https://pypi.org/simple"}
                if "sdist" in package:
                    raise ValueError()
                for wheel in package.get("wheels", []):
                    url = wheel["url"]
                    if not url.startswith(self.base):
                        raise ValueError()
                    record = self.catalog[url[len(self.base):]]
                    if wheel["hash"] != "sha256:" + record["sha256"] or wheel.get("size") != record["size"]:
                        raise ValueError()
                    wheel["url"] = record["url"]
                    replacements[url] = record["url"]
            for old, new in replacements.items():
                text = text.replace(json.dumps(old), json.dumps(new))
            if tomllib.loads(text) != value:
                raise ValueError()
            result = text.encode()
            if self.password.encode() in result or self.base.encode() in result or b"127.0.0.1" in result:
                raise ValueError()
            return result
        except (ValueError, KeyError, TypeError):
            raise PreparationError("invalid_derived_dependency_lock") from None
