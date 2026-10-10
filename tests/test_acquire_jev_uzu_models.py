"""Offline range-resume integrity checks with small authored byte strings."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acquire_jev_uzu_models_test", ROOT / "scripts/acquire_jev_uzu_models.py")
acquirer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acquirer)

DATA = b"abcdefghijklmnopqrstuvwxyz12345"
URL = "https://example.invalid/pinned/synthetic-artifact"


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def receipt(partial, data=DATA, url=URL, chunk_size=8):
    value = {"identity": {"url": url, "size": len(data), "chunk_size": chunk_size},
             "completed": {str(start): checksum(data[start:start + chunk_size])
                           for start in range(0, len(data), chunk_size)}}
    path = partial.with_suffix(partial.suffix + ".receipt.json")
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def transport(monkeypatch):
    """Forbid curl and replace urllib; every requested byte comes from memory."""
    monkeypatch.setattr(acquirer.shutil, "which", lambda _: None)
    monkeypatch.setattr(acquirer.time, "sleep", lambda _: None)
    def install(data=DATA, wrong_range=False):
        calls = []
        lock = threading.Lock()
        class Response:
            status = 206
            def __init__(self, start, end):
                self.start, self.end = start, end
                self.headers = {"Content-Range": f"bytes {start}-{end}/{len(data) + int(wrong_range)}"}
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                return False
            def read(self, maximum):
                return data[self.start:self.end + 1][:maximum]
        def open_request(request, timeout):
            assert request.full_url.startswith("https://example.invalid/")
            assert timeout == 120
            span = request.get_header("Range").removeprefix("bytes=")
            start, end = map(int, span.split("-"))
            with lock:
                calls.append((start, end))
            return Response(start, end)
        monkeypatch.setattr(acquirer.urllib.request, "urlopen", open_request)
        return calls
    return install


def test_preallocated_partial_without_completed_receipts_fetches_every_chunk(tmp_path, transport):
    partial = tmp_path / "artifact.partial"
    with partial.open("wb") as stream:
        stream.truncate(len(DATA))
    calls = transport()
    acquirer.ranged_download(URL, partial, len(DATA), chunk_size=8)
    assert sorted(calls) == [(0, 7), (8, 15), (16, 23), (24, 30)]
    assert partial.read_bytes() == DATA
    recovered = json.loads(partial.with_suffix(".partial.receipt.json").read_text())
    assert set(recovered["completed"]) == {"0", "8", "16", "24"}
    assert recovered["completed"]["24"] == checksum(DATA[24:])


def test_completed_receipts_skip_verified_bytes_and_refetch_corrupt_chunk(tmp_path, transport):
    partial = tmp_path / "artifact.partial"
    partial.write_bytes(DATA)
    receipt_path = receipt(partial)
    descriptor = os.open(partial, os.O_RDWR)
    try:
        os.pwrite(descriptor, b"!", 9)
    finally:
        os.close(descriptor)
    calls = transport()
    acquirer.ranged_download(URL, partial, len(DATA), chunk_size=8)
    assert calls == [(8, 15)]
    assert partial.read_bytes() == DATA
    recovered = json.loads(receipt_path.read_text())
    assert recovered["completed"]["8"] == checksum(DATA[8:16])


def test_fully_verified_completed_receipts_make_no_requests(tmp_path, transport):
    partial = tmp_path / "artifact.partial"
    partial.write_bytes(DATA)
    receipt(partial)
    calls = transport()
    acquirer.ranged_download(URL, partial, len(DATA), chunk_size=8)
    assert calls == []
    assert partial.read_bytes() == DATA


@pytest.mark.parametrize("changed", ["url", "size", "chunk_size"])
def test_changed_range_identity_cannot_reuse_previous_receipts(tmp_path, transport, changed):
    partial = tmp_path / "artifact.partial"
    partial.write_bytes(DATA)
    receipt_path = receipt(partial)
    new_url = URL + "/different" if changed == "url" else URL
    new_data = DATA + b"6789" if changed == "size" else DATA
    chunk_size = 4 if changed == "chunk_size" else 8
    calls = transport(new_data)
    acquirer.ranged_download(new_url, partial, len(new_data), chunk_size=chunk_size)
    assert sorted(calls) == [(start, min(start + chunk_size, len(new_data)) - 1)
                             for start in range(0, len(new_data), chunk_size)]
    assert partial.read_bytes() == new_data
    assert json.loads(receipt_path.read_text())["identity"] == {
        "url": new_url, "size": len(new_data), "chunk_size": chunk_size}


def test_wrong_content_range_fails_after_three_attempts_without_completing_chunk(tmp_path, transport):
    partial = tmp_path / "artifact.partial"
    data = b"abcdefgh"
    calls = transport(data, wrong_range=True)
    with pytest.raises(ValueError, match="^artifact_range_failed$"):
        acquirer.ranged_download(URL, partial, len(data), chunk_size=8)
    assert calls == [(0, 7)] * 3
    assert partial.read_bytes() == b"\0" * len(data)
    assert json.loads(partial.with_suffix(".partial.receipt.json").read_text())["completed"] == {}


def test_exclusive_partial_descriptor_lock_rejects_concurrent_acquirer(tmp_path, transport):
    partial = tmp_path / "artifact.partial"
    partial.write_bytes(DATA)
    calls = transport()
    descriptor = os.open(partial, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            acquirer.ranged_download(URL, partial, len(DATA), chunk_size=8)
        assert calls == []
        assert partial.read_bytes() == DATA
        assert not partial.with_suffix(".partial.receipt.json").exists()
    finally:
        os.close(descriptor)


def test_failure_drains_bounded_window_and_receipts_successes_before_retry(tmp_path, transport, monkeypatch):
    data = bytes(range(80))
    partial = tmp_path / "artifact.partial"
    calls = transport(data)
    normal_open = acquirer.urllib.request.urlopen
    original_wait = acquirer.wait
    original_pool = acquirer.ThreadPoolExecutor
    submitted = []
    failed_future = []
    class ObservedPool(original_pool):
        def submit(self, fn, start):
            submitted.append(start)
            future = super().submit(fn, start)
            if start == 0:
                failed_future.append(future)
            return future
    def fail_first_offset(request, timeout):
        if request.get_header("Range") == "bytes=0-7":
            # Record retries through the same mocked transport, then fail.
            normal_open(request, timeout)
            raise OSError("synthetic range failure")
        return normal_open(request, timeout)
    def failure_completes_first(pending, **kwargs):
        # Fix a deterministic valid completion order: the failed initial
        # worker finishes before replacement submissions could be admitted.
        assert len(pending) <= 8
        failed_future[0].exception(timeout=5)
        return original_wait(pending, **kwargs)
    monkeypatch.setattr(acquirer, "ThreadPoolExecutor", ObservedPool)
    monkeypatch.setattr(acquirer, "wait", failure_completes_first)
    monkeypatch.setattr(acquirer.urllib.request, "urlopen", fail_first_offset)
    with pytest.raises(ValueError, match="^artifact_range_failed$"):
        acquirer.ranged_download(URL, partial, len(data), chunk_size=8)
    assert submitted == list(range(0, 64, 8))
    assert calls.count((0, 7)) == 3
    assert set(calls) == {(start, start + 7) for start in range(0, 64, 8)}
    state = json.loads(partial.with_suffix(".partial.receipt.json").read_text())
    assert set(state["completed"]) == {str(start) for start in range(8, 64, 8)}
    for start in range(8, 64, 8):
        assert state["completed"][str(start)] == checksum(data[start:start + 8])
    # Resume must fetch only the failed range and never-submitted ranges.
    monkeypatch.setattr(acquirer, "ThreadPoolExecutor", original_pool)
    monkeypatch.setattr(acquirer, "wait", original_wait)
    resumed_calls = transport(data)
    acquirer.ranged_download(URL, partial, len(data), chunk_size=8)
    assert sorted(resumed_calls) == [(0, 7), (64, 71), (72, 79)]
    assert partial.read_bytes() == data


@pytest.mark.parametrize("offline", [True, False])
def test_full_pinned_partial_promoted_without_network_or_chunk_receipt(tmp_path, monkeypatch, offline):
    target = tmp_path / "synthetic.bin"
    partial = target.with_suffix(".bin.partial")
    partial.write_bytes(DATA)
    stale_receipt = partial.with_suffix(".partial.receipt.json")
    stale_receipt.write_text("not a usable receipt")
    def no_network(*unused, **kwargs):
        pytest.fail("A cryptographically complete partial must not use the network")
    monkeypatch.setattr(acquirer.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(acquirer, "ranged_download", no_network)
    acquirer.acquire(tmp_path, offline=offline, files=[("synthetic.bin", len(DATA), checksum(DATA))])
    assert target.read_bytes() == DATA
    assert not partial.exists()
    assert not stale_receipt.exists()


def test_wrong_hash_partial_cannot_be_promoted_offline(tmp_path, monkeypatch):
    target = tmp_path / "synthetic.bin"
    partial = target.with_suffix(".bin.partial")
    partial.write_bytes(b"x" * len(DATA))
    monkeypatch.setattr(acquirer.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("offline network request"))
    with pytest.raises(ValueError, match="^artifact_unavailable$"):
        acquirer.acquire(tmp_path, offline=True, files=[("synthetic.bin", len(DATA), checksum(DATA))])
    assert not target.exists()
    assert partial.exists()


def test_complete_partial_promotion_respects_active_acquirer_lock(tmp_path, monkeypatch):
    target = tmp_path / "synthetic.bin"
    partial = target.with_suffix(".bin.partial")
    partial.write_bytes(DATA)
    descriptor = os.open(partial, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            acquirer.acquire(tmp_path, offline=True, files=[("synthetic.bin", len(DATA), checksum(DATA))])
        assert not target.exists()
        assert partial.read_bytes() == DATA
    finally:
        os.close(descriptor)
