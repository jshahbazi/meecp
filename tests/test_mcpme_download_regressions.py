"""Independent regressions exercise the production framing over a real TLS socket."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
import zipfile

import pytest

from mcpme import downloads
from mcpme.contracts import validate_package
from mcpme.private import PrivateStateError
from mcpme.supervisor import Supervisor
from test_mcpme_packages import make_package


@pytest.fixture(scope="module")
def tls_certificate(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required for the local TLS fixture")
    root = tmp_path_factory.mktemp("download-tls").resolve()
    config = root / "certificate.cnf"
    config.write_text(
        "[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n"
        "[subject]\nCN=downloads.example.com\n"
        "[extensions]\nsubjectAltName=DNS:downloads.example.com\n"
        "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign\n"
        "extendedKeyUsage=serverAuth\n"
    )
    certificate, key = root / "certificate.pem", root / "key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-config", str(config), "-keyout", str(key), "-out", str(certificate)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return certificate, key


@contextlib.asynccontextmanager
async def pinned_tls(monkeypatch, tls_certificate, response: bytes, *, hostname="downloads.example.com"):
    """Route a vetted public address to the fixture without bypassing actual TLS."""
    certificate, key = tls_certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    requests = []
    connects = []
    handlers = set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            requests.append(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3))
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    actual_connect = asyncio.open_connection
    actual_context = ssl.create_default_context

    async def resolve(name, selected_port, **kwargs):
        assert name == hostname and selected_port == 443
        assert kwargs == {"type": socket.SOCK_STREAM, "proto": socket.IPPROTO_TCP}
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

    async def connect(address, selected_port, **kwargs):
        connects.append((address, selected_port, kwargs["server_hostname"]))
        assert address == "8.8.8.8" and selected_port == 443
        assert kwargs["family"] == socket.AF_INET
        return await actual_connect("127.0.0.1", port, **kwargs)

    def trusted_context():
        return actual_context(cafile=str(certificate))

    with monkeypatch.context() as patch:
        patch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        patch.setattr(asyncio, "open_connection", connect)
        patch.setattr(ssl, "create_default_context", trusted_context)
        try:
            yield requests, connects
        finally:
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 3)


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\npayload",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\npay\r\n4\r\nload\r\n0\r\n\r\n",
    b"HTTP/1.0 200 OK\r\n\r\npayload",
])
async def test_pinned_https_real_tls_frames_and_original_hostname(monkeypatch, tls_certificate, response):
    monkeypatch.setenv("HTTPS_PROXY", "http://unusable.invalid:1")
    async with pinned_tls(monkeypatch, tls_certificate, response) as (requests, connects):
        async with downloads.PinnedHTTPSNetwork().stream("https://downloads.example.com/package.zip") as stream:
            assert b"".join([chunk async for chunk in stream]) == b"payload"
    assert connects == [("8.8.8.8", 443, "downloads.example.com")]
    assert requests == [
        b"GET /package.zip HTTP/1.1\r\nHost: downloads.example.com\r\n"
        b"Accept: application/zip\r\nAccept-Encoding: identity\r\n"
        b"Connection: close\r\nUser-Agent: mcpme/1\r\n\r\n"
    ]


async def test_pinned_https_real_tls_rejects_wrong_original_hostname(monkeypatch, tls_certificate):
    async with pinned_tls(monkeypatch, tls_certificate, b"", hostname="other.example.com") as (requests, connects):
        with pytest.raises(downloads.DownloadError, match="^download_network_unavailable$"):
            async with downloads.PinnedHTTPSNetwork().stream("https://other.example.com/package.zip"):
                pytest.fail("a certificate for another hostname must not be accepted")
    assert connects == [("8.8.8.8", 443, "other.example.com")]
    assert requests == []


@pytest.mark.parametrize(("response", "code"), [
    (b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.example/package.zip\r\n\r\n", "download_redirect_blocked"),
    (b"HTTP/1.1 404 Missing\r\n\r\nprivate publisher body", "download_http_failed"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nx", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nxy", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx", "download_network_unavailable"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nx", "download_network_unavailable"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;extension=x\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nTrailer: secret\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nBad: " + b"x" * 8192 + b"\r\n\r\n", "download_network_unavailable"),
])
async def test_pinned_https_real_tls_rejects_bad_wire_frames(monkeypatch, tls_certificate, response, code):
    async with pinned_tls(monkeypatch, tls_certificate, response):
        with pytest.raises(downloads.DownloadError, match="^" + code + "$"):
            async with downloads.PinnedHTTPSNetwork().stream("https://downloads.example.com/package.zip") as stream:
                async for _ in stream:
                    pass


async def test_download_publication_flushes_permissions_and_nested_directories(tmp_path, monkeypatch):
    source = make_package(tmp_path / "source")
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(source))
    payload = archive.read_bytes()

    class MemoryNetwork:
        @contextlib.asynccontextmanager
        async def stream(self, url):
            async def chunks():
                yield payload
            yield chunks()

    manager = downloads.DownloadManager(tmp_path / "state", network=MemoryNetwork())
    opened = {}
    events = []
    actual_open, actual_fsync = os.open, os.fsync
    actual_fchmod, actual_rename = os.fchmod, os.rename

    def tracked_open(path, *args, **kwargs):
        descriptor = actual_open(path, *args, **kwargs)
        opened[descriptor] = Path(path)
        return descriptor

    def tracked_fsync(descriptor):
        if descriptor in opened:
            events.append(("fsync", opened[descriptor]))
        return actual_fsync(descriptor)

    def tracked_fchmod(descriptor, mode):
        if descriptor in opened:
            events.append(("chmod", opened[descriptor]))
        return actual_fchmod(descriptor, mode)

    def tracked_rename(source, destination):
        events.append(("rename", Path(source)))
        return actual_rename(source, destination)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(os, "fchmod", tracked_fchmod)
    monkeypatch.setattr(os, "rename", tracked_rename)
    try:
        record = await manager.start({"url": "https://downloads.example.com/package.zip",
                                      "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
        task = manager._tasks[record["download_id"]]
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert manager.get(record["download_id"])["status"] == "completed"
        stage_package = manager.staging / (record["download_id"] + ".1") / "package"
        rename_index = events.index(("rename", stage_package))
        for path in source.rglob("*"):
            if path.is_file():
                extracted = stage_package / path.relative_to(source)
                chmod_index = events.index(("chmod", extracted))
                assert any(index > chmod_index and index < rename_index and event == ("fsync", extracted)
                           for index, event in enumerate(events)), "file mode must be durable before publication"
        bin_index = events.index(("fsync", stage_package / "bin"))
        package_index = events.index(("fsync", stage_package))
        assert bin_index < package_index < rename_index
    finally:
        await manager.close()


async def test_cancellation_retry_survives_transient_record_write_failure(tmp_path, monkeypatch):
    gate = asyncio.Event()
    entered = asyncio.Event()

    class BlockedNetwork:
        @contextlib.asynccontextmanager
        async def stream(self, url):
            async def chunks():
                entered.set()
                await gate.wait()
                yield b"unused"
            yield chunks()

    manager = downloads.DownloadManager(tmp_path / "state", network=BlockedNetwork())
    record = await manager.start({"url": "https://downloads.example.com/package.zip",
                                  "sha256": hashlib.sha256(b"unused").hexdigest(), "size": 6})
    identity = record["download_id"]
    task = manager._tasks[identity]
    original_save = manager._save

    def transient_failure(record):
        if record["cancel_requested"]:
            raise PrivateStateError()
        original_save(record)

    try:
        await asyncio.wait_for(entered.wait(), 2)
        monkeypatch.setattr(manager, "_save", transient_failure)
        with pytest.raises(PrivateStateError):
            await manager.cancel(identity)
        monkeypatch.setattr(manager, "_save", original_save)
        await manager.cancel(identity)
        await asyncio.wait_for(asyncio.shield(task), 2)
        assert manager.get(identity)["status"] == "canceled"
        assert list(manager.staging.iterdir()) == []
    finally:
        monkeypatch.setattr(manager, "_save", original_save)
        gate.set()
        await manager.close()


async def test_recovery_marks_missing_completed_package_failed_and_retry_restores_it(tmp_path):
    source = make_package(tmp_path / "source")
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(source))
    payload = archive.read_bytes()
    descriptor = {"url": "https://downloads.example.com/package.zip",
                  "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}

    class MemoryNetwork:
        @contextlib.asynccontextmanager
        async def stream(self, url):
            async def chunks():
                yield payload
            yield chunks()

    manager = downloads.DownloadManager(tmp_path / "state", network=MemoryNetwork())
    try:
        started = await manager.start(descriptor)
        identity = started["download_id"]
        await asyncio.wait_for(asyncio.shield(manager._tasks[identity]), 5)
        completed = manager.get(identity)
        assert completed["status"] == "completed"
        package = Path(completed["package_path"])
    finally:
        await manager.close()
    shutil.rmtree(package)

    recovered = downloads.DownloadManager(tmp_path / "state", network=MemoryNetwork())
    try:
        assert not package.exists(), "recovery must not create an empty completed package"
        interrupted = recovered.get(identity)
        assert interrupted["status"] == "failed"
        assert interrupted["error_code"] == "download_package_invalid"
        assert "package_path" not in interrupted and "package_digest" not in interrupted
        retried = await recovered.start(descriptor)
        assert retried["download_id"] == identity and retried["attempt"] == 2
        await asyncio.wait_for(asyncio.shield(recovered._tasks[identity]), 5)
        assert recovered.get(identity)["status"] == "completed"
        assert validate_package(package).digest == completed["package_digest"]
    finally:
        await recovered.close()


async def test_download_record_failure_does_not_abort_supervisor_shutdown(tmp_path, monkeypatch):
    entered = asyncio.Event()
    gate = asyncio.Event()

    class BlockedNetwork:
        @contextlib.asynccontextmanager
        async def stream(self, url):
            async def chunks():
                entered.set()
                await gate.wait()
                yield b"unused"
            yield chunks()

    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    host.downloads.network = BlockedNetwork()
    original_save = host.downloads._save
    replacement = None

    def transient_failure(record):
        if record["cancel_requested"]:
            raise PrivateStateError()
        original_save(record)

    try:
        await host.downloads.start({"url": "https://downloads.example.com/package.zip",
                                    "sha256": hashlib.sha256(b"unused").hexdigest(), "size": 6})
        await asyncio.wait_for(entered.wait(), 2)
        monkeypatch.setattr(host.downloads, "_save", transient_failure)
        with contextlib.suppress(PrivateStateError):
            await asyncio.wait_for(host.close(), 2)
        replacement = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    finally:
        monkeypatch.setattr(host.downloads, "_save", original_save)
        gate.set()
        if replacement is None:
            await host.close()
        else:
            await replacement.close()
