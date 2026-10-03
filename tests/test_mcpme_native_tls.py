"""Real native trust must never fetch certificate URLs outside pinned sockets."""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import http.server
import platform
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import (
    AuthorityInformationAccessOID, ExtendedKeyUsageOID, NameOID,
)

from mcpme import downloads


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS native trust check")
@pytest.mark.parametrize("send_intermediate,trust_root,hostname,expired,accepted,policy_failure,transfers", [
    (True, True, "downloads.example.com", False, True, False, 1),
    (False, True, "downloads.example.com", False, False, False, 1),
    (False, False, "downloads.example.com", False, False, False, 1),
    (True, False, "downloads.example.com", False, False, False, 1),
    (True, True, "other.example.com", False, False, False, 1),
    (True, True, "downloads.example.com", True, False, False, 1),
    (True, True, "downloads.example.com", False, False, True, 1),
    (True, True, "downloads.example.com", False, True, False, 16),
])
async def test_native_trust_never_fetches_peer_certificate_urls(
    tmp_path, monkeypatch, send_intermediate, trust_root, hostname, expired, accepted, policy_failure, transfers,
):
    fetched = []
    intermediate_der = b""

    class IssuerHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            fetched.append(1)
            self.send_response(200)
            self.send_header("Content-Length", str(len(intermediate_der)))
            self.end_headers()
            self.wfile.write(intermediate_der)

        def log_message(self, *args):
            pass

    issuer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), IssuerHandler)
    issuer_thread = threading.Thread(target=issuer.serve_forever)
    issuer_thread.start()
    server = None
    handlers = set()
    requests = []
    connects = []
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)]
        names = [x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)]) for text in (
            "root-" + str(x509.random_serial_number()),
            "issuer-" + str(x509.random_serial_number()), "downloads.example.com",
        )]

        def certificate(index, signer, ca):
            start = now - datetime.timedelta(days=2 if expired and not ca else 1)
            end = now - datetime.timedelta(days=1) if expired and not ca else now + datetime.timedelta(days=1)
            builder = (x509.CertificateBuilder().subject_name(names[index]).issuer_name(names[signer])
                       .public_key(keys[index].public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(start).not_valid_after(end)
                       .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                       .add_extension(x509.SubjectKeyIdentifier.from_public_key(keys[index].public_key()), critical=False)
                       .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(keys[signer].public_key()), critical=False))
            if not ca:
                builder = (builder.add_extension(x509.SubjectAlternativeName([
                    x509.DNSName("downloads.example.com")]), critical=False)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
                        AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(
                            f"http://127.0.0.1:{issuer.server_address[1]}/issuer.der"))]), critical=False))
            return builder.sign(keys[signer], hashes.SHA256())

        root, intermediate, leaf = certificate(0, 0, True), certificate(1, 0, True), certificate(2, 1, False)
        intermediate_der = intermediate.public_bytes(serialization.Encoding.DER)
        chain = leaf.public_bytes(serialization.Encoding.PEM)
        if send_intermediate:
            chain += intermediate.public_bytes(serialization.Encoding.PEM)
        cert_path, key_path = tmp_path / "server.pem", tmp_path / "server.key"
        cert_path.write_bytes(chain)
        key_path.write_bytes(keys[2].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        key_path.chmod(0o600)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)

        async def handle(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                requests.append(1)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx")
                await writer.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        actual_connect, actual_context = asyncio.open_connection, downloads._tls_context

        async def resolve(name, selected_port, **kwargs):
            assert name == hostname and selected_port == 443
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

        async def connect(address, selected_port, **kwargs):
            connects.append((address, selected_port, kwargs["server_hostname"]))
            assert address == "8.8.8.8" and selected_port == 443
            assert kwargs["family"] == socket.AF_INET
            return await actual_connect("127.0.0.1", port, **kwargs)

        contexts = []

        def context():
            result = actual_context()
            if trust_root:
                result.load_verify_locations(cadata=root.public_bytes(serialization.Encoding.PEM).decode("ascii"))
            contexts.append(result)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
            patch.setattr(asyncio, "open_connection", connect)
            patch.setattr(downloads, "_tls_context", context)
            evaluations = []
            if policy_failure:
                from mcpme._native_tls import _macos
                setter = _macos.Security.SecTrustSetNetworkFetchAllowed
                check_status = setter.errcheck
                evaluate = _macos._verify_peercerts_impl_macos_10_14

                def rejected_status(result, function, args):
                    return check_status(-50, function, args)

                def observed_evaluation(*args):
                    evaluations.append(1)
                    return evaluate(*args)

                patch.setattr(setter, "errcheck", rejected_status)
                patch.setattr(_macos, "_verify_peercerts_impl_macos_10_14", observed_evaluation)

            async def transfer():
                async with downloads.PinnedHTTPSNetwork().stream(f"https://{hostname}/package.zip") as chunks:
                    assert b"".join([chunk async for chunk in chunks]) == b"x"

            if accepted:
                await asyncio.gather(*(transfer() for _ in range(transfers)))
            else:
                with pytest.raises(downloads.DownloadError, match="^download_network_unavailable$"):
                    async with downloads.PinnedHTTPSNetwork().stream(f"https://{hostname}/package.zip"):
                        pytest.fail("invalid certificate chain must fail before an HTTP request")
            if policy_failure:
                assert evaluations == []
        assert connects == [("8.8.8.8", 443, hostname)] * transfers
        assert requests == ([1] * transfers if accepted else [])
        assert len(contexts) == transfers and len({id(context) for context in contexts}) == transfers
        assert all(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED for context in contexts)
        assert fetched == []
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        try:
            if handlers:
                await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 3)
        finally:
            await asyncio.to_thread(issuer.shutdown)
            issuer.server_close()
            issuer_thread.join(timeout=3)
            assert not issuer_thread.is_alive()
