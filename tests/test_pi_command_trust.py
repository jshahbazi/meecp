"""Configured public trust reaches native Git without weakening TLS checks."""

import asyncio
import contextlib
import ssl
import stat
import subprocess

import pytest

from meecp.pi_tools import PiToolError
from meecp.pi_trust import MAX_ANCHORS, MAX_BUNDLE_BYTES, NativeTrustError
from test_meecp_download_regressions import tls_certificate
from test_pi_tools import broker, native


@native
async def test_network_launch_stages_certificate_only_trust_for_git(broker, tmp_path, monkeypatch, tls_certificate):
    certificate, _ = tls_certificate
    source = tmp_path / "configured-trust.pem"
    source.write_bytes(certificate.read_bytes() + b"-----BEGIN PRIVATE KEY-----\nexcluded\n-----END PRIVATE KEY-----\n")
    source.chmod(0o600)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(source))
    def forbidden_export():
        pytest.fail("configured_trust_must_remain_authoritative")
    monkeypatch.setattr("meecp.pi_trust.native_anchor_pem", forbidden_export)
    _, _, _, environment = broker.launch_spec(["/usr/bin/git", "--version"], network=True)
    staged = broker.workspace / ".trust/ca.pem"
    assert environment["GIT_SSL_CAINFO"] == environment["SSL_CERT_FILE"] == environment["NODE_EXTRA_CA_CERTS"] == str(staged)
    assert staged.read_bytes() == certificate.read_bytes()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert not {"GIT_SSL_NO_VERIFY", "NODE_TLS_REJECT_UNAUTHORIZED"} & environment.keys()
    _, _, _, offline = broker.launch_spec(["/usr/bin/git", "--version"])
    assert "GIT_SSL_CAINFO" not in offline


@native
async def test_gui_shaped_environment_stages_and_reuses_native_public_anchors(broker, monkeypatch):
    from meecp.pi_trust import native_anchor_pem
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent-inherited-ca")
    monkeypatch.setenv("GIT_SSL_CAINFO", "/nonexistent-inherited-git-ca")
    calls = []
    def recorded_export():
        calls.append(True)
        return native_anchor_pem()
    monkeypatch.setattr("meecp.pi_trust.native_anchor_pem", recorded_export)
    _, _, _, environment = broker.launch_spec(["/usr/bin/git", "--version"], network=True)
    staged = broker.workspace / ".trust/ca.pem"
    content = staged.read_text()
    assert 1 <= content.count("-----BEGIN CERTIFICATE-----") <= MAX_ANCHORS
    assert len(content.encode("ascii")) <= MAX_BUNDLE_BYTES and "PRIVATE KEY" not in content
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert environment["GIT_SSL_CAINFO"] == environment["SSL_CERT_FILE"] == str(staged)
    assert "securityd" not in broker.launch_spec(["/usr/bin/git", "--version"], network=True)[1][1]
    assert calls == [True]


@native
async def test_native_anchor_failure_keeps_verification_closed(broker, monkeypatch):
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    def failed_export():
        raise NativeTrustError()
    monkeypatch.setattr("meecp.pi_trust.native_anchor_pem", failed_export)
    with pytest.raises(PiToolError, match="^trust_configuration_unavailable$"):
        broker.launch_spec(["/usr/bin/git", "--version"], network=True)
    assert not (broker.workspace / ".trust/ca.pem").exists()


def packet_line(value):
    return f"{len(value) + 4:04x}".encode("ascii") + value


@native
async def test_native_git_staged_ca_succeeds_and_rejects_untrusted_or_wrong_hostname(broker, monkeypatch, tls_certificate):
    certificate, key = tls_certificate
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    commit = b"1" * 40
    body = (packet_line(b"# service=git-upload-pack\n") + b"0000"
        + packet_line(commit + b" HEAD\0symref=HEAD:refs/heads/main\n")
        + packet_line(commit + b" refs/heads/main\n") + b"0000")
    response = (b"HTTP/1.1 200 OK\r\nContent-Type: application/x-git-upload-pack-advertisement\r\n"
        + b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\nConnection: close\r\n\r\n" + body)
    requests, handlers = [], set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            requests.append(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5))
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    try:
        for hostname, trust in [("downloads.example.com", "native"), ("downloads.example.com", "omit_git_ca"),
                ("downloads.example.com", "configured"), ("other.example.com", "configured")]:
            if trust == "native":
                monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
            else:
                monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(certificate))
            argv = ["/usr/bin/git", "-c", "http.version=HTTP/1.1", "-c",
                f"http.curloptResolve={hostname}:{port}:127.0.0.1", "ls-remote", f"https://{hostname}:{port}/test.git", "HEAD"]
            executable, arguments, cwd, environment = broker.launch_spec(argv, network=True)
            if trust == "omit_git_ca":
                environment.pop("GIT_SSL_CAINFO")
            result = await asyncio.to_thread(subprocess.run, [str(executable), *arguments],
                cwd=cwd, env=environment, capture_output=True, timeout=15)
            if hostname == "downloads.example.com" and trust == "configured":
                assert result.returncode == 0 and result.stdout == commit + b"\tHEAD\n" and not result.stderr
            else:
                assert result.returncode != 0 and not result.stdout
                assert b"certificate" in result.stderr.lower()
            if trust == "native":
                # Separate launch workspaces select one trust policy; reset this
                # fixture's staged file before exercising its configured policy.
                (broker.workspace / ".trust/ca.pem").unlink()
        assert len(requests) == 1
        assert requests[0].startswith(b"GET /test.git/info/refs?service=git-upload-pack HTTP/1.1\r\n")
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 5)
