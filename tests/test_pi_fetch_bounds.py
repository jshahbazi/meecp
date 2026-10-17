"""Large artifact fetch bounds and exact declared-size publication."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from meecp import pi_tools
from meecp.pi_tools import PiToolBroker


class Network:
    def __init__(self, chunks):
        self.chunks, self.calls = chunks, []

    @asynccontextmanager
    async def stream(self, url, **kwargs):
        self.calls.append(kwargs)

        async def consume():
            for chunk in self.chunks:
                yield chunk

        yield consume()


@pytest.fixture
async def broker(tmp_path):
    broker = PiToolBroker(tmp_path / "workspace", Path("/unused"))
    try:
        yield broker
    finally:
        await broker.close()


@pytest.mark.parametrize("size", [True, 0, -1, 16 * 1024**3 + 1, 2.5, "100"])
async def test_invalid_expected_size_never_fetches_or_creates_output(broker, size):
    broker.network = Network([b"data"])
    result = await broker.invoke(
        "fetch_url", {"url": "https://example.com/artifact", "path": "artifact", "size": size}
    )
    assert result == {"success": False, "output": "invalid_download_size"}
    assert broker.network.calls == [] and not (broker.workspace / "artifact").exists()


@pytest.mark.parametrize("size,success", [(3, False), (4, True), (5, False)])
async def test_exact_size_is_checked_before_atomic_publish(broker, size, success):
    broker.network = Network([b"da", b"ta"])
    result = await broker.invoke(
        "fetch_url", {"url": "https://example.com/artifact", "path": "artifact", "size": size}
    )
    assert result["success"] is success
    if success:
        assert json.loads(result["output"])["size"] == 4
        assert (broker.workspace / "artifact").read_bytes() == b"data"
    else:
        assert result["output"] == "download_size_mismatch" and not (broker.workspace / "artifact").exists()
    assert not list(broker.workspace.glob(".pi-tool-*"))


async def test_per_file_and_session_transfer_bounds_include_rejected_bytes(broker, monkeypatch):
    monkeypatch.setattr(pi_tools, "MAX_FETCH", 4)
    monkeypatch.setattr(pi_tools, "MAX_FETCH_TOTAL", 7)
    broker.network = Network([b"data"])
    assert (await broker.invoke("fetch_url", {"url": "https://example.com/a", "path": "a"}))["success"]
    result = await broker.invoke("fetch_url", {"url": "https://example.com/b", "path": "b"})
    assert result == {"success": False, "output": "download_size_limit"}
    assert not (broker.workspace / "b").exists() and broker._fetch_bytes == 8


def test_large_artifact_bounds_match_host_profile():
    assert pi_tools.MAX_FETCH == 16 * 1024**3
    assert pi_tools.MAX_FETCH_TOTAL == 32 * 1024**3
    assert pi_tools.FETCH_TIMEOUT == 900
