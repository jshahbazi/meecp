"""Redirect regressions use pinned public peers and real hostname-checked TLS."""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

import pytest

from meecp import downloads
from meecp.pi_tools import PiToolBroker


ORIGIN = "downloads.example.com"
TARGET = "assets.example.com"
PUBLIC = {ORIGIN: "8.8.8.8", TARGET: "1.1.1.1", "wrong.example.com": "9.9.9.9"}


def response(status=200, location=None, body=b"payload", headers=b""):
    if location is not None:
        headers += b"Location: " + location + b"\r\n"
    return b"HTTP/1.1 " + str(status).encode() + b" Test\r\n" + headers + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


class WireWriter:
    def __init__(self, *, stall=False):
        self.transport = self
        self.stall = stall
        self.closed = asyncio.Event()
        self.aborted = False
        self.request = bytearray()

    def write(self, data):
        self.request.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed.set()

    async def wait_closed(self):
        if self.stall:
            await asyncio.Event().wait()

    def abort(self):
        self.aborted = True


class WireReader(asyncio.StreamReader):
    def __init__(self):
        super().__init__()
        self.body_started = asyncio.Event()

    async def readexactly(self, size):
        self.body_started.set()
        return await super().readexactly(size)


@contextlib.asynccontextmanager
async def wire_network(monkeypatch, responses, *, dns=None, stalls=(), eof=True):
    writers, resolves, connects = [], [], []

    async def resolve(host, port, **kwargs):
        resolves.append(host)
        assert port == 443 and kwargs == {"type": socket.SOCK_STREAM, "proto": socket.IPPROTO_TCP}
        addresses = (dns or {}).get(host, [PUBLIC.get(host, "8.8.4.4")])
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)) for address in addresses]

    async def connect(address, port, **kwargs):
        connects.append((address, port, kwargs["server_hostname"]))
        reader = WireReader()
        reader.feed_data(responses[len(writers)])
        if eof:
            reader.feed_eof()
        writer = WireWriter(stall=len(writers) in stalls)
        writer.reader = reader
        writers.append(writer)
        return reader, writer

    with monkeypatch.context() as patch:
        patch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        patch.setattr(asyncio, "open_connection", connect)
        patch.setattr(downloads, "_tls_context", lambda: object())
        yield writers, resolves, connects


async def collect(network, url=f"https://{ORIGIN}/releases/file", **options):
    async with network.stream(url, **options) as chunks:
        return b"".join([chunk async for chunk in chunks])


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_opt_in_follows_supported_relative_redirects(monkeypatch, status):
    async with wire_network(monkeypatch, [response(status, b"../artifact?signature=synthetic"), response()]) as (writers, resolves, connects):
        assert await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True) == b"payload"
        assert resolves == [ORIGIN, ORIGIN] and len(connects) == 2
        assert writers[0].closed.is_set() and writers[1].closed.is_set()
        assert bytes(writers[1].request).startswith(b"GET /artifact?signature=synthetic HTTP/1.1\r\n")
        assert b"Authorization:" not in writers[1].request and b"Cookie:" not in writers[1].request


async def test_default_remains_no_redirect(monkeypatch):
    async with wire_network(monkeypatch, [response(302, b"/artifact")]) as (writers, resolves, _):
        with pytest.raises(downloads.DownloadError, match="^download_redirect_blocked$"):
            await collect(downloads.PinnedHTTPSNetwork())
        assert resolves == [ORIGIN] and writers[0].closed.is_set()


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
async def test_follow_redirects_requires_explicit_boolean(monkeypatch, value):
    async with wire_network(monkeypatch, []) as (_, resolves, _):
        with pytest.raises(downloads.DownloadError, match="^download_response_invalid$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=value)
        assert not resolves


@pytest.mark.parametrize("target", [
    b"http://assets.example.com/file", b"https://user@assets.example.com/file",
    b"https://@assets.example.com/file", b"https://:password@assets.example.com/file",
    b"https://assets.example.com:444/file", b"https://assets.example.com/file#fragment", b"/file#",
    b"https://assets.example.com./file", b"https://assets%2eexample.com/file", b"https://[fe80::1%25en0]/file",
    b"/file?signature=%0d%0aInjected", b"/file?signature=%7F", b"/file?signature=%zz", b"/file%00",
    b"/file%zz", b"/with space", b"/file\\other", b"", b"/nonascii-\xff",
])
async def test_unsafe_redirects_never_resolve_or_connect_target(monkeypatch, target, capfd, caplog):
    async with wire_network(monkeypatch, [response(302, target)]) as (writers, resolves, _):
        with pytest.raises(downloads.DownloadError, match="^download_redirect_blocked$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)
        assert resolves == [ORIGIN] and len(writers) == 1 and writers[0].closed.is_set()
    assert capfd.readouterr() == ("", "") and caplog.text == ""


@pytest.mark.parametrize("target", [b"https://127.0.0.1/file", b"https://[::1]/file", b"https://10.0.0.1/file"])
async def test_private_literal_redirect_is_blocked_before_dns(monkeypatch, target):
    async with wire_network(monkeypatch, [response(302, target)]) as (_, resolves, _):
        with pytest.raises(downloads.DownloadError, match="^download_network_blocked$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)
        assert resolves == [ORIGIN]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["1.1.1.1", "10.0.0.1"]])
async def test_each_redirect_rechecks_all_dns_results(monkeypatch, addresses):
    async with wire_network(monkeypatch, [response(302, f"https://{TARGET}/file".encode())], dns={TARGET: addresses}) as (_, resolves, connects):
        with pytest.raises(downloads.DownloadError, match="^download_network_blocked$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)
        assert resolves == [ORIGIN, TARGET] and connects == [("8.8.8.8", 443, ORIGIN)]


async def test_scheme_relative_redirect_preserves_public_ip_pinning_and_new_hostname(monkeypatch):
    async with wire_network(monkeypatch, [response(302, f"//{TARGET}/file?token=synthetic".encode()), response()]) as (writers, resolves, connects):
        assert await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True) == b"payload"
        assert resolves == [ORIGIN, TARGET]
        assert connects == [("8.8.8.8", 443, ORIGIN), ("1.1.1.1", 443, TARGET)]
        assert b"Host: assets.example.com\r\n" in writers[1].request


async def test_five_redirects_succeed_and_sixth_is_rejected(monkeypatch):
    chain = [response(302, f"/hop-{index}".encode()) for index in range(1, 6)]
    async with wire_network(monkeypatch, [*chain, response()]):
        assert await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True) == b"payload"
    async with wire_network(monkeypatch, [*chain, response(302, b"/hop-6")]) as (writers, _, _):
        with pytest.raises(downloads.DownloadError, match="^download_redirect_blocked$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)
        assert len(writers) == 6 and all(writer.closed.is_set() for writer in writers)


async def test_redirect_loop_is_rejected_without_reconnecting(monkeypatch):
    async with wire_network(monkeypatch, [response(302, f"https://{ORIGIN}:443/releases/file".encode())]) as (writers, _, _):
        with pytest.raises(downloads.DownloadError, match="^download_redirect_blocked$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)
        assert len(writers) == 1


@pytest.mark.parametrize("wire,code", [
    (b"HTTP/1.1 302 Found\r\n\r\n", "download_redirect_blocked"),
    (response(304, b"/file"), "download_redirect_blocked"),
    (b"HTTP/1.1 302 Found\r\nLocation: /a\r\nLocation: /b\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 302 Found\r\nLocation: /a\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n\r\n", "download_response_invalid"),
])
async def test_redirect_response_framing_is_validated(monkeypatch, wire, code):
    async with wire_network(monkeypatch, [wire]):
        with pytest.raises(downloads.DownloadError, match="^" + code + "$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)


async def test_redirect_close_timeout_aborts_before_next_hop(monkeypatch):
    monkeypatch.setattr(downloads, "NETWORK_CLOSE_SECONDS", .01)
    async with wire_network(monkeypatch, [response(302, b"/file"), response()], stalls=(0,)) as (writers, _, _):
        assert await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True) == b"payload"
        assert writers[0].aborted and writers[1].closed.is_set()


async def test_cancellation_during_redirect_close_aborts_without_following(monkeypatch):
    async with wire_network(monkeypatch, [response(302, b"/file")], stalls=(0,)) as (writers, resolves, _):
        task = asyncio.create_task(collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True))
        async with asyncio.timeout(1):
            while not writers:
                await asyncio.sleep(0)
            await writers[0].closed.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert writers[0].aborted and resolves == [ORIGIN]


async def test_fetch_body_cancellation_closes_transport_and_removes_partial_file(monkeypatch, tmp_path):
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused"))
    async with wire_network(monkeypatch, [b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n"], eof=False) as (writers, _, _):
        try:
            task = asyncio.create_task(broker.invoke("fetch_url", {"url": f"https://{ORIGIN}/file", "path": "artifact"}))
            async with asyncio.timeout(1):
                while not writers:
                    await asyncio.sleep(0)
                await writers[0].reader.body_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert writers[0].closed.is_set()
            assert not (broker.workspace / "artifact").exists() and not list(broker.workspace.glob(".pi-tool-*"))
        finally:
            await broker.close()


@pytest.fixture(scope="module")
def two_name_certificate(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL required for real redirect TLS fixture")
    root = tmp_path_factory.mktemp("pi-redirect-tls").resolve()
    configuration = root / "certificate.cnf"
    configuration.write_text("[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n"
        f"[subject]\nCN={ORIGIN}\n[extensions]\nsubjectAltName=DNS:{ORIGIN},DNS:{TARGET}\n"
        "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign\nextendedKeyUsage=serverAuth\n")
    certificate, key = root / "certificate.pem", root / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-config",
        str(configuration), "-keyout", str(key), "-out", str(certificate)], check=True, capture_output=True, timeout=15)
    return certificate, key


@contextlib.asynccontextmanager
async def real_tls(monkeypatch, certificate, responder):
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(*certificate)
    snis, requests, connects, resolves = [], [], [], []
    server_context.set_servername_callback(lambda _socket, name, _context: snis.append(name))
    handlers = set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            requests.append(request)
            selected = responder(request)
            writer.write(selected)
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
    port = server.sockets[0].getsockname()[1]
    original_connect = asyncio.open_connection
    original_context = downloads._tls_context

    async def resolve(host, selected_port, **kwargs):
        resolves.append(host)
        assert selected_port == 443
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC[host], 443))]

    async def connect(address, selected_port, **kwargs):
        connects.append((address, selected_port, kwargs["server_hostname"]))
        assert selected_port == 443 and address == PUBLIC[kwargs["server_hostname"]]
        return await original_connect("127.0.0.1", port, **kwargs)

    def context():
        result = original_context()
        result.load_verify_locations(cafile=str(certificate[0]))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        patch.setattr(asyncio, "open_connection", connect)
        patch.setattr(downloads, "_tls_context", context)
        try:
            yield snis, requests, connects, resolves
        finally:
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 3)


async def test_real_tls_cross_host_signed_redirect_rechecks_hostname_and_sni(monkeypatch, two_name_certificate, capfd, caplog):
    def responder(request):
        return response(302, f"https://{TARGET}/file?signature=synthetic".encode()) if b"Host: downloads.example.com\r\n" in request else response()
    async with real_tls(monkeypatch, two_name_certificate, responder) as (snis, requests, connects, resolves):
        assert await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True) == b"payload"
        assert snis == [ORIGIN, TARGET] and resolves == [ORIGIN, TARGET]
        assert connects == [("8.8.8.8", 443, ORIGIN), ("1.1.1.1", 443, TARGET)]
        assert requests[1].startswith(b"GET /file?signature=synthetic HTTP/1.1\r\n")
    assert capfd.readouterr() == ("", "") and caplog.text == ""


async def test_real_tls_redirect_rejects_certificate_for_wrong_target(monkeypatch, two_name_certificate):
    async with real_tls(monkeypatch, two_name_certificate, lambda _: response(302, b"https://wrong.example.com/file")) as (snis, requests, connects, _):
        with pytest.raises(downloads.DownloadError, match="^download_network_unavailable$"):
            await collect(downloads.PinnedHTTPSNetwork(), follow_redirects=True)
        assert snis == [ORIGIN, "wrong.example.com"] and len(requests) == 1 and len(connects) == 2


async def test_concurrent_streams_keep_response_status_independent(monkeypatch, two_name_certificate):
    completed_plain = asyncio.Event()
    class InterleavedNetwork(downloads.PinnedHTTPSNetwork):
        async def _headers(self, *args, **kwargs):
            result = await super()._headers(*args, **kwargs)
            if result[0] == 302:
                await asyncio.wait_for(completed_plain.wait(), 2)
            elif result[0] == 200:
                completed_plain.set()
            return result
    def responder(request):
        if request.startswith(b"GET /a "):
            return response(302, b"/done-a", body=b"redirect-body")
        return response(body=b"a" if request.startswith(b"GET /done-a ") else b"b")
    async with real_tls(monkeypatch, two_name_certificate, responder):
        network = InterleavedNetwork()
        a, b = await asyncio.gather(collect(network, f"https://{ORIGIN}/a", follow_redirects=True),
                                   collect(network, f"https://{ORIGIN}/b", follow_redirects=True))
        assert a == b"a" and b == b"b"
