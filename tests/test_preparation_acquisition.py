from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import time

import pytest

from mcpme.downloads import DownloadError
from mcpme.preparation_acquisition import WheelAcquirer
from mcpme.preparation_builder import WheelRequest
from mcpme.preparation_contracts import PreparationError


class Network:
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure
        self.urls = []

    @asynccontextmanager
    async def stream(self, url):
        self.urls.append(url)
        async def body():
            for chunk in self.chunks:
                yield chunk
            if self.failure:
                raise self.failure
        yield body()


def request(data=b"wheel", *, host="files.pythonhosted.org", filename="fixture-1.0-py3-none-any.whl"):
    return WheelRequest("fixture", "1.0", filename, "https://"+host+"/"+filename,
                        hashlib.sha256(data).hexdigest(), len(data))


async def acquire(tmp_path, network, requests=None, **changes):
    destination = tmp_path / "wheels"
    destination.mkdir(mode=0o700)
    policy = {"allowed_hosts": frozenset({"files.pythonhosted.org"}), "disk_bytes": 1024, "wall_seconds": 10}
    policy.update(changes)
    await WheelAcquirer(network).acquire(requests or (request(),), destination, **policy)
    return destination


async def test_exact_wheel_identity_and_private_sink(tmp_path):
    network = Network([b"wh", b"eel"])
    path = await acquire(tmp_path, network)
    assert (path / request().filename).read_bytes() == b"wheel"
    assert (path / request().filename).stat().st_mode & 0o777 == 0o600
    assert network.urls == [request().url]


@pytest.mark.parametrize("chunks,code", [([b"too-many"], "dependency_size_mismatch"),
                                        ([b"short"], "dependency_artifact_mismatch"),
                                        ([b"wh"], "dependency_artifact_mismatch")])
async def test_failed_identity_removes_partial_file(tmp_path, chunks, code):
    with pytest.raises(PreparationError, match=code):
        await acquire(tmp_path, Network(chunks))
    assert list((tmp_path / "wheels").iterdir()) == []


async def test_origin_policy_precedes_network(tmp_path):
    network = Network([b"wheel"])
    with pytest.raises(PreparationError, match="dependency_origin_not_authorized"):
        await acquire(tmp_path, network, (request(host="example.com"),))
    assert network.urls == []


async def test_disk_budget_precedes_network(tmp_path):
    network = Network([b"wheel"])
    with pytest.raises(PreparationError, match="dependency_disk_limit"):
        await acquire(tmp_path, network, disk_bytes=4)
    assert network.urls == []


@pytest.mark.parametrize("failure,code", [(DownloadError("download_redirect_blocked"), "dependency_redirect_blocked"),
                                         (DownloadError("download_network_blocked"), "dependency_network_blocked")])
async def test_transport_failures_remain_content_free(tmp_path, failure, code):
    with pytest.raises(PreparationError, match=code):
        await acquire(tmp_path, Network([b"wh"], failure))
    assert list((tmp_path / "wheels").iterdir()) == []


async def test_cancel_after_bytes_removes_partial_file(tmp_path):
    count = 0
    def canceled():
        nonlocal count
        count += 1
        if count == 3:
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await acquire(tmp_path, Network([b"wh", b"eel"]), check_canceled=canceled)
    assert list((tmp_path / "wheels").iterdir()) == []


async def test_success_inside_callers_exception_handler_retains_wheels(tmp_path):
    try:
        raise ValueError("synthetic caller context")
    except ValueError:
        path = await acquire(tmp_path, Network([b"wheel"]))
    assert (path / request().filename).read_bytes() == b"wheel"


async def test_buffered_transport_cannot_bypass_elapsed_deadline(tmp_path):
    class Buffered(Network):
        @asynccontextmanager
        async def stream(self, url):
            async def body():
                time.sleep(0.05)
                yield b"wheel"
            yield body()
    with pytest.raises(PreparationError, match="dependency_acquisition_timeout"):
        await acquire(tmp_path, Buffered([]), wall_seconds=0.01)
    assert list((tmp_path / "wheels").iterdir()) == []
