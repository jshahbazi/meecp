from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import socket
import stat
import struct
import threading
import zipfile
from pathlib import Path

import pytest

import mcpme.downloads as downloads
from mcpme.contracts import validate_package
from mcpme.downloads import DownloadError, DownloadManager, PinnedHTTPSNetwork, normalize_spec
from mcpme.private import read_private_json, write_private_json
from test_mcpme_packages import make_package


class SyntheticNetwork:
    def __init__(self, data: bytes, *, blocked: asyncio.Event | None = None):
        self.data = data
        self.blocked = blocked
        self.calls = []
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    @contextlib.asynccontextmanager
    async def stream(self, url):
        self.calls.append(url)
        self.entered.set()

        async def chunks():
            for offset in range(0, len(self.data), downloads.CHUNK_BYTES):
                yield self.data[offset:offset + downloads.CHUNK_BYTES]
                if self.blocked is not None:
                    await self.blocked.wait()
        try:
            yield chunks()
        finally:
            self.closed.set()


def spec(data: bytes, **changes):
    return {"url": "https://packages.example/fixture.zip", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), **changes}


def package_zip(tmp_path: Path) -> tuple[bytes, str]:
    package = make_package(tmp_path / "source")
    digest = validate_package(package).digest
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            archive.write(path, str(path.relative_to(package)))
    return output.getvalue(), digest


async def final(manager: DownloadManager, identity: str) -> dict:
    async with asyncio.timeout(5):
        while (record := manager.get(identity))["status"] not in {"completed", "failed", "canceled"}:
            await asyncio.sleep(0.005)
        # Let the completion callback remove the owning task before retries.
        await asyncio.sleep(0)
        return record


@pytest.mark.parametrize("change,code", [
    ({"size": True}, "invalid_download_spec"), ({"size": 0}, "invalid_download_spec"),
    ({"size": downloads.MAX_ARCHIVE_BYTES + 1}, "invalid_download_spec"),
    ({"sha256": "a" * 63}, "invalid_download_spec"), ({"sha256": "A" * 64}, "invalid_download_spec"),
    ({"package_digest": None}, "invalid_download_spec"), ({"extra": "content"}, "invalid_download_spec"),
    ({"url": "http://packages.example/a.zip"}, "invalid_download_url"),
    ({"url": "https://user:password@packages.example/a.zip"}, "invalid_download_url"),
    ({"url": "https://packages.example/a.zip?secret=value"}, "invalid_download_url"),
    ({"url": "https://packages.example/a.zip?"}, "invalid_download_url"),
    ({"url": "https://packages.example/a.zip#secret"}, "invalid_download_url"),
    ({"url": "https://packages.example:8443/a.zip"}, "invalid_download_url"),
    ({"url": "https://packages.example./a.zip"}, "invalid_download_url"),
    ({"url": "https://packages.example/a%0d%0aInjected.zip"}, "invalid_download_url"),
    ({"url": "https://packages.example/a%zz.zip"}, "invalid_download_url"),
    ({"url": "https://127.0.0.1/a.zip"}, "download_network_blocked"),
    ({"url": "https://[::ffff:127.0.0.1]/a.zip"}, "download_network_blocked"),
])
def test_descriptor_rejects_unpinned_or_sensitive_locators(change, code):
    with pytest.raises(DownloadError, match=f"^{code}$"):
        normalize_spec(spec(b"archive", **change))


def test_descriptor_is_normalized_and_copied():
    original = spec(b"archive", url="https://PACKAGES.EXAMPLE:443")
    value = normalize_spec(original)
    assert value["url"] == "https://packages.example/"
    original["sha256"] = "b" * 64
    assert value["sha256"] == hashlib.sha256(b"archive").hexdigest()


async def test_download_is_static_private_closed_and_durable(tmp_path, monkeypatch):
    data, digest = package_zip(tmp_path)
    def forbid(*args, **kwargs):
        pytest.fail("download intake must never execute package code")
    monkeypatch.setattr("subprocess.Popen", forbid)
    network = SyntheticNetwork(data)
    manager = DownloadManager(tmp_path / "state", network=network)
    try:
        requested = spec(data, package_digest=digest)
        started = await manager.start(requested)
        requested["sha256"] = "b" * 64
        record = await final(manager, started["download_id"])
        assert record["status"] == "completed", record
        assert record["package_digest"] == digest
        assert record["received_bytes"] == len(data)
        path = Path(record["package_path"])
        assert validate_package(path).digest == digest
        assert network.calls == ["https://packages.example/fixture.zip"]
        for item in [manager.root, manager.records_root, manager.packages, path, path / "bin"]:
            assert stat.S_IMODE(item.stat().st_mode) == 0o700
        for item in [path / "LICENSE", path / "mcpme.lock.json", path / "mcpme.package.json"]:
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
        assert stat.S_IMODE((path / "bin/service").stat().st_mode) == 0o700
        assert list(manager.staging.iterdir()) == []
        summary = manager.list()[0]
        assert "descriptor" not in summary and "package_path" not in summary
        assert read_private_json(manager.records_root / (started["download_id"] + ".json")) == record
    finally:
        await manager.close()
    recovered = DownloadManager(tmp_path / "state", network=SyntheticNetwork(b"unused"))
    try:
        assert recovered.get(record["download_id"]) == record
        assert await recovered.start(spec(data, package_digest=digest)) == record
        assert validate_package(Path(record["package_path"])).digest == digest
    finally:
        await recovered.close()


async def test_concurrent_identical_fetches_create_one_job_and_transfer(tmp_path):
    data, _ = package_zip(tmp_path)
    gate = asyncio.Event()
    network = SyntheticNetwork(data, blocked=gate)
    manager = DownloadManager(tmp_path / "state", network=network)
    try:
        records = await asyncio.gather(*(manager.start(spec(data)) for _ in range(24)))
        assert len({record["download_id"] for record in records}) == 1
        await network.entered.wait()
        assert len(manager.list()) == 1 and len(network.calls) == 1
        gate.set()
        assert (await final(manager, records[0]["download_id"]))["status"] == "completed"
    finally:
        await manager.close()


@pytest.mark.parametrize("kind,code", [
    ("short", "download_size_mismatch"), ("long", "download_size_mismatch"),
    ("hash", "download_hash_mismatch"), ("package_digest", "download_package_digest_mismatch"),
    ("invalid_package", "download_package_invalid"),
])
async def test_pin_failures_never_publish_package(tmp_path, kind, code):
    data, _ = package_zip(tmp_path)
    chosen = spec(data)
    transmitted = data
    if kind == "short":
        transmitted = data[:-1]
    elif kind == "long":
        transmitted = data + b"x"
    elif kind == "hash":
        chosen["sha256"] = "b" * 64
    elif kind == "package_digest":
        chosen["package_digest"] = "b" * 64
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("private.txt", "Content must stay out of metadata")
        transmitted = output.getvalue()
        chosen = spec(transmitted)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(transmitted))
    try:
        job = await manager.start(chosen)
        result = await final(manager, job["download_id"])
        assert result["status"] == "failed" and result["error_code"] == code
        assert "package_path" not in result and list(manager.packages.iterdir()) == []
        assert list(manager.staging.iterdir()) == []
        assert b"Content must stay" not in next(manager.records_root.iterdir()).read_bytes()
    finally:
        await manager.close()


def unsafe_zip(kind: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        if kind in {"traversal", "absolute", "backslash", "drive", "dot", "empty_component", "nul"}:
            name = {"traversal": "../escape", "absolute": "/escape", "backslash": "bin\\escape",
                    "drive": "C:/escape", "dot": "a/./escape", "empty_component": "a//escape",
                    "nul": "escape\x00hidden"}[kind]
            archive.writestr(name, "Synthetic contents")
        elif kind in {"symlink", "fifo", "socket", "device", "setuid"}:
            item = zipfile.ZipInfo("unsafe")
            item.create_system = 3
            mode = {"symlink": stat.S_IFLNK | 0o777, "fifo": stat.S_IFIFO | 0o600,
                    "socket": stat.S_IFSOCK | 0o600, "device": stat.S_IFCHR | 0o600,
                    "setuid": stat.S_IFREG | 0o4700}[kind]
            item.external_attr = mode << 16
            archive.writestr(item, "outside")
        elif kind in {"duplicate", "case", "unicode", "parent_case", "file_parent", "parent_file"}:
            first, second = {"duplicate": ("a", "a"), "case": ("A", "a"),
                             "unicode": ("caf\u00e9", "cafe\u0301"),
                             "parent_case": ("Bin/a", "bin/b"), "file_parent": ("a", "a/b"),
                             "parent_file": ("a/b", "a")}[kind]
            with pytest.warns(UserWarning) if kind == "duplicate" else contextlib.nullcontext():
                archive.writestr(first, "one")
                archive.writestr(second, "two")
        elif kind == "bomb":
            archive.writestr("large", b"a" * (1024 * 1024), compress_type=zipfile.ZIP_DEFLATED)
        elif kind == "unsupported_compression":
            archive.writestr("a", b"contents", compress_type=zipfile.ZIP_BZIP2)
        elif kind == "extra_metadata":
            item = zipfile.ZipInfo("a")
            item.extra = struct.pack("<HH", 0xCAFE, 4096) + b"x" * 4096
            archive.writestr(item, "one")
        elif kind == "archive_comment":
            archive.writestr("a", "one")
            archive.comment = b"Undeclared metadata"
    data = output.getvalue()
    if kind == "nul":
        # ZipInfo silently truncates writer-provided NUL; mutate both header names
        # without changing sizes to exercise the raw archive decoding boundary.
        data = data.replace(b"escape", b"es\x00ape")
    return data


@pytest.mark.parametrize("kind", [
    "traversal", "absolute", "backslash", "drive", "dot", "empty_component", "nul", "symlink", "fifo",
    "socket", "device", "setuid", "duplicate", "case", "unicode", "parent_case", "file_parent", "parent_file",
    "bomb", "unsupported_compression", "extra_metadata", "archive_comment",
])
async def test_unsafe_archive_is_rejected_before_publication(tmp_path, kind):
    data = unsafe_zip(kind)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    try:
        job = await manager.start(spec(data))
        result = await final(manager, job["download_id"])
        assert result["status"] == "failed", result
        assert result["error_code"] == ("download_archive_limit" if kind == "bomb" else "download_archive_unsafe")
        assert list(manager.packages.iterdir()) == [] and list(manager.staging.iterdir()) == []
        assert not (tmp_path / "escape").exists()
    finally:
        await manager.close()


def test_central_directory_limits_precede_zipfile_allocation(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ZipFile must not allocate unchecked central metadata")
    monkeypatch.setattr(downloads.zipfile, "ZipFile", forbidden)
    for entries, metadata in [(downloads.MAX_ARCHIVE_ENTRIES + 1, 0), (1, downloads.MAX_ZIP_METADATA_BYTES + 1)]:
        path = tmp_path / f"archive-{entries}-{metadata}.zip"
        path.write_bytes(b"PK\x03\x04" + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, entries, entries, metadata, 4, 0))
        with pytest.raises(DownloadError, match="^download_archive_limit$"):
            downloads._archive_entries(path)


def test_valid_bounded_zip64_directory(tmp_path):
    data, _ = package_zip(tmp_path)
    values = struct.unpack("<4s4H2LH", data[-22:])
    _, _, _, _, count, central_bytes, offset, _ = values
    end = len(data) - 22
    zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, central_bytes, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 65535, 65535, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    path = tmp_path / "zip64.zip"
    path.write_bytes(data[:-22] + zip64 + locator + eocd)
    source, entries, total = downloads._archive_entries(path)
    source.close()
    assert len(entries) == count and total > 0


async def test_cancel_during_stream_and_retry_coalesces(tmp_path):
    data, _ = package_zip(tmp_path)
    gate = asyncio.Event()
    network = SyntheticNetwork(data, blocked=gate)
    manager = DownloadManager(tmp_path / "state", network=network)
    try:
        job = await manager.start(spec(data))
        await network.entered.wait()
        result = await asyncio.wait_for(manager.cancel(job["download_id"]), 0.2)
        assert result["cancel_requested"] and result["stage"] == "canceling"
        result = await final(manager, job["download_id"])
        assert result["status"] == "canceled" and result["error_code"] == "download_canceled"
        assert network.closed.is_set() and list(manager.staging.iterdir()) == []
        assert (await manager.cancel(job["download_id"])) == result
        gate.set()
        attempts = await asyncio.gather(*(manager.start(spec(data)) for _ in range(12)))
        assert {value["attempt"] for value in attempts} == {2}
        assert (await final(manager, job["download_id"]))["status"] == "completed"
        assert len(network.calls) == 2
    finally:
        await manager.close()


async def test_pending_cancel_never_starts_network(tmp_path):
    data, _ = package_zip(tmp_path)
    network = SyntheticNetwork(data)
    manager = DownloadManager(tmp_path / "state", network=network)
    try:
        job = await manager.start(spec(data))
        await manager.cancel(job["download_id"])
        assert (await final(manager, job["download_id"]))["status"] == "canceled"
        assert network.calls == [] and list(manager.staging.iterdir()) == []
    finally:
        await manager.close()


async def test_cancel_and_close_keep_ownership_until_thread_settles(tmp_path, monkeypatch):
    data, _ = package_zip(tmp_path)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    entered, release = threading.Event(), threading.Event()
    original = manager._extract
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(manager, "_extract", paused)
    job = await manager.start(spec(data))
    async with asyncio.timeout(3):
        while not entered.is_set():
            await asyncio.sleep(0.005)
    await manager.cancel(job["download_id"])
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0.02)
    assert not closing.done()
    closing.cancel()
    await asyncio.sleep(0.02)
    assert not closing.done()
    closing.cancel()
    await asyncio.sleep(0.02)
    assert not closing.done()
    with pytest.raises(DownloadError, match="^downloads_unavailable$"):
        DownloadManager(tmp_path / "state")
    with pytest.raises(DownloadError, match="^downloads_unavailable$"):
        await manager.start(spec(data, url="https://packages.example/other.zip"))
    assert list(manager.staging.iterdir())
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    recovered = DownloadManager(tmp_path / "state")
    try:
        assert recovered.get(job["download_id"])["status"] == "canceled"
        assert list(recovered.staging.iterdir()) == [] and list(recovered.packages.iterdir()) == []
    finally:
        await recovered.close()


async def test_shutdown_retains_interrupted_record_and_allows_retry(tmp_path):
    data, _ = package_zip(tmp_path)
    network = SyntheticNetwork(data, blocked=asyncio.Event())
    manager = DownloadManager(tmp_path / "state", network=network)
    job = await manager.start(spec(data))
    await network.entered.wait()
    await manager.close()
    recovered = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    try:
        record = recovered.get(job["download_id"])
        assert record["status"] == "failed" and record["error_code"] == "download_interrupted"
        assert list(recovered.staging.iterdir()) == []
        retried = await recovered.start(spec(data))
        assert retried["attempt"] == 2
        assert (await final(recovered, job["download_id"]))["status"] == "completed"
    finally:
        await recovered.close()


async def test_recovery_reclaims_only_partial_staging_and_retains_renamed_package(tmp_path):
    data, _ = package_zip(tmp_path)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    job = await manager.start(spec(data))
    record = await final(manager, job["download_id"])
    path = manager.records_root / (job["download_id"] + ".json")
    stage = manager.staging / (job["download_id"] + ".1")
    await manager.close()
    record.update(status="running", stage="verifying")
    record.pop("package_path")
    record.pop("package_digest")
    write_private_json(path, record)
    stage.mkdir(mode=0o700)
    (stage / "partial.zip").write_bytes(b"partial")
    recovered = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    try:
        assert recovered.get(job["download_id"])["error_code"] == "download_interrupted"
        assert not stage.exists() and (recovered.packages / job["download_id"]).is_dir()
        assert (await recovered.start(spec(data)))["attempt"] == 2
        assert (await final(recovered, job["download_id"]))["status"] == "completed"
    finally:
        await recovered.close()


async def test_job_active_and_disk_limits_are_checked_before_transfer(tmp_path, monkeypatch):
    data, _ = package_zip(tmp_path)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data, blocked=asyncio.Event()))
    try:
        monkeypatch.setattr(downloads, "MAX_ACTIVE_DOWNLOADS", 1)
        first = await manager.start(spec(data))
        with pytest.raises(DownloadError, match="^download_limit$"):
            await manager.start(spec(data, url="https://packages.example/second.zip"))
        await manager.cancel(first["download_id"])
        await final(manager, first["download_id"])
        monkeypatch.setattr(downloads, "MAX_DOWNLOADS", 1)
        with pytest.raises(DownloadError, match="^download_limit$"):
            await manager.start(spec(data, url="https://packages.example/second.zip"))
        monkeypatch.setattr(downloads, "MAX_DOWNLOADS", 4096)
        monkeypatch.setattr(downloads.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 0})())
        with pytest.raises(DownloadError, match="^download_disk_unavailable$"):
            await manager.start(spec(data, url="https://packages.example/second.zip"))
    finally:
        await manager.close()


async def test_expanded_disk_requirement_is_checked_before_extraction(tmp_path, monkeypatch):
    data, _ = package_zip(tmp_path)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        expanded = sum(item.file_size for item in archive.infolist())
    assert expanded > len(data) + 1
    available = len(data) + downloads.DISK_HEADROOM_BYTES + 1
    monkeypatch.setattr(downloads.shutil, "disk_usage", lambda path: type("Usage", (), {"free": available})())
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    try:
        job = await manager.start(spec(data))
        result = await final(manager, job["download_id"])
        assert result["error_code"] == "download_disk_unavailable"
        assert list(manager.packages.iterdir()) == [] and list(manager.staging.iterdir()) == []
    finally:
        await manager.close()


@pytest.mark.parametrize("exception", [ValueError("Synthetic exception details"), DownloadError("Synthetic exception details")])
async def test_unexpected_transport_errors_never_enter_metadata(tmp_path, exception):
    class FailingNetwork:
        @contextlib.asynccontextmanager
        async def stream(self, url):
            raise exception
            yield
    manager = DownloadManager(tmp_path / "state", network=FailingNetwork())
    try:
        job = await manager.start(spec(b"archive"))
        record = await final(manager, job["download_id"])
        assert record["error_code"] == "download_failed"
        assert "Synthetic exception details" not in json.dumps(record)
        assert b"Synthetic exception details" not in next(manager.records_root.iterdir()).read_bytes()
    finally:
        await manager.close()


async def test_discovery_is_bounded_and_reports_retained_count(tmp_path):
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(b"not a zip"))
    try:
        identities = []
        for index in range(105):
            job = await manager.start(spec(b"not a zip", url=f"https://packages.example/package-{index}.zip"))
            identities.append(job["download_id"])
            assert (await final(manager, job["download_id"]))["status"] == "failed"
        summaries = manager.list()
        assert manager.count() == 105 and len(summaries) == 100
        assert summaries[0]["download_id"] == identities[-1]
        assert summaries[-1]["download_id"] == identities[5]
        assert len(json.dumps(summaries).encode()) < 1024 * 1024
        assert manager.get(identities[0])["download_id"] == identities[0]
    finally:
        await manager.close()


async def test_recovery_removes_recognized_private_atomic_write_temporary(tmp_path):
    data, _ = package_zip(tmp_path)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    job = await manager.start(spec(data))
    record = await final(manager, job["download_id"])
    records_root = manager.records_root
    await manager.close()
    temporary = records_root / (job["download_id"] + ".json." + "a" * 24 + ".tmp")
    temporary.write_bytes(b"An interrupted partial JSON write")
    temporary.chmod(0o600)
    recovered = DownloadManager(tmp_path / "state")
    try:
        assert recovered.get(job["download_id"]) == record
        assert not temporary.exists()
    finally:
        await recovered.close()


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"cancel_requested": 0}, {"stage": "unknown"},
    {"unexpected": "Synthetic data must never enter an API snapshot"},
    {"error_code": "arbitrary exception text"}, {"created_at": True},
    {"updated_at": float("inf")}, {"updated_at": -1}, {"updated_at": 10 ** 10000},
    {"received_bytes": True}, {"attempt": False}, {"size": True},
])
async def test_corrupt_records_fail_closed_content_free(tmp_path, change):
    data, _ = package_zip(tmp_path)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    job = await manager.start(spec(data))
    record = await final(manager, job["download_id"])
    path = manager.records_root / (job["download_id"] + ".json")
    await manager.close()
    record.update(change)
    # Deliberately malformed private fixture, including values normal writers reject.
    if change.get("updated_at") == 10 ** 10000:
        encoded = json.dumps({**record, "updated_at": 0}).replace('"updated_at": 0', '"updated_at": ' + "1" * 10000)
    else:
        encoded = json.dumps(record)
    path.write_text(encoded)
    path.chmod(0o600)
    with pytest.raises(DownloadError, match="^downloads_unavailable$"):
        DownloadManager(tmp_path / "state")


async def test_backward_clock_adjustment_keeps_records_recoverable(tmp_path, monkeypatch):
    data, _ = package_zip(tmp_path)
    manager = DownloadManager(tmp_path / "state", network=SyntheticNetwork(data))
    original_time = downloads.time.time()
    monkeypatch.setattr(downloads.time, "time", lambda: original_time)
    job = await manager.start(spec(data))
    monkeypatch.setattr(downloads.time, "time", lambda: original_time - 3600)
    record = await final(manager, job["download_id"])
    assert record["updated_at"] >= record["created_at"] == original_time
    await manager.close()
    recovered = DownloadManager(tmp_path / "state")
    try:
        assert recovered.get(job["download_id"]) == record
    finally:
        await recovered.close()


class FakeWriter:
    def __init__(self):
        self.data = b""
        self.closed = False
    def write(self, data):
        self.data += data
    async def drain(self):
        pass
    def close(self):
        self.closed = True
    async def wait_closed(self):
        pass


def network_fixture(monkeypatch, response: bytes, addresses=None):
    reader = asyncio.StreamReader(limit=8192)
    reader.feed_data(response)
    reader.feed_eof()
    writer = FakeWriter()
    calls = []
    async def resolve(*args, **kwargs):
        return addresses or [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]
    async def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return reader, writer
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(downloads.asyncio, "open_connection", connect)
    return writer, calls


async def collect_network(url="https://packages.example/fixture.zip"):
    async with PinnedHTTPSNetwork().stream(url) as chunks:
        return b"".join([chunk async for chunk in chunks])


async def test_production_transport_pins_ip_retains_tls_hostname_and_ignores_proxy(monkeypatch):
    writer, calls = network_fixture(monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nzip")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    assert await collect_network() == b"zip"
    assert calls[0][0] == ("8.8.8.8", 443)
    assert calls[0][1]["server_hostname"] == "packages.example"
    assert writer.closed
    assert b"Host: packages.example\r\n" in writer.data
    assert b"Authorization" not in writer.data and b"Proxy" not in writer.data


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.1.1", "224.0.0.1", "::1", "fc00::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:0a00:0001::1"])
async def test_dns_private_or_mixed_answers_fail_before_connection(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
                 (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]
    _, calls = network_fixture(monkeypatch, b"", addresses)
    with pytest.raises(DownloadError, match="^download_network_blocked$"):
        await collect_network()
    assert calls == []


@pytest.mark.parametrize("response,code", [
    (b"HTTP/1.1 302 Found\r\nLocation: https://other.example/a\r\n\r\n", "download_redirect_blocked"),
    (b"HTTP/1.1 404 Missing\r\n\r\n", "download_http_failed"),
    (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nzip", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nzipx", "download_response_invalid"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nzi", "download_network_unavailable"),
    (b"HTTP/1.1 200 OK\r\nX: " + b"a" * 8192 + b"\r\n\r\n", "download_network_unavailable"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3;ext=value\r\nzip\r\n0\r\n\r\n", "download_response_invalid"),
])
async def test_response_framing_redirects_and_encodings_fail_closed(monkeypatch, response, code):
    writer, _ = network_fixture(monkeypatch, response)
    with pytest.raises(DownloadError, match=f"^{code}$"):
        await collect_network()
    assert writer.closed


async def test_chunked_body_is_bounded_and_decoded(monkeypatch):
    network_fixture(monkeypatch, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nzi\r\n1\r\np\r\n0\r\n\r\n")
    assert await collect_network() == b"zip"
