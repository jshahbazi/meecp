import asyncio
from contextlib import asynccontextmanager
import gzip
import hashlib
import json
from pathlib import Path
import sys

import httpx
import pytest

from meecp.downloads import DownloadError, PinnedHTTPSNetwork
from meecp.preparation_contracts import PreparationError
from meecp.preparation_resolution_network import ResolutionIndexBroker, sandbox_profile
from meecp.preparation_processes import PreparationExecutor
from test_preparation_processes import group_reaper


class Network:
    def __init__(self, values):
        self.values, self.calls = values, []

    @asynccontextmanager
    async def stream(self, url, **kwargs):
        self.calls.append(url)
        async def chunks():
            yield self.values[url]
        yield chunks()


def fixture_index(metadata=b"Metadata-Version: 2.4\nName: synthetic\nVersion: 1.0\n"):
    wheel = b"synthetic wheel bytes"
    url = "https://files.pythonhosted.org/packages/synthetic-1.0-py3-none-any.whl"
    index = {"files": [{"filename": "synthetic-1.0-py3-none-any.whl", "url": url,
        "hashes": {"sha256": hashlib.sha256(wheel).hexdigest()}, "size": len(wheel),
        "core-metadata": {"sha256": hashlib.sha256(metadata).hexdigest()}}]}
    return Network({"https://pypi.org/simple/synthetic/": json.dumps(index).encode(), url: wheel, url + ".metadata": metadata})


async def test_authenticated_exact_broker_mapping_and_canonical_lock(tmp_path):
    network = fixture_index()
    async with ResolutionIndexBroker(tmp_path.resolve() / "index", byte_limit=1024**2, check=lambda:None, network=network) as broker:
        async with httpx.AsyncClient(trust_env=False) as client:
            denied = await client.get(broker.base + "/simple/synthetic/")
            assert denied.status_code == 401 and network.calls == []
            headers = {"Authorization": broker.authorization}
            denied = await client.get(broker.base + "/simple/synthetic/", headers={**headers, "Origin": "null"})
            assert denied.status_code == 401 and network.calls == []
            response = await client.get(broker.base + "/simple/synthetic/", headers=headers)
            file = response.json()["files"][0]
            assert file["url"].startswith(broker.base) and broker.password not in response.text
            data = await client.get(file["url"] + ".metadata", headers=headers)
            assert data.status_code == 200 and data.content.startswith(b"Metadata-Version")
            text = 'version=1\n[[package]]\nname="synthetic"\nversion="1.0"\nsource={registry=' + json.dumps(broker.base + '/simple') + '}\nwheels=[{url=' + json.dumps(file['url']) + ',hash="sha256:' + file['hashes']['sha256'] + '",size=' + str(file['size']) + '}]\n'
            canonical = broker.canonical_lock(text.encode())
            assert b"https://pypi.org/simple" in canonical and b"https://files.pythonhosted.org" in canonical
            assert b"127.0.0.1" not in canonical and broker.password.encode() not in canonical


async def test_transitive_direct_requirement_is_rejected_before_uv_sees_metadata(tmp_path):
    network = fixture_index(b"Metadata-Version: 2.4\nRequires-Dist: other @ https://example.com/a.whl\n")
    async with ResolutionIndexBroker(tmp_path.resolve() / "index", byte_limit=1024**2, check=lambda:None, network=network) as broker:
        data = json.loads(await broker._index("synthetic"))
        path = data["files"][0]["url"][len(broker.base):]
        with pytest.raises(PreparationError, match="unsupported_resolution_dependency"):
            await broker._file(path + ".metadata")


@pytest.mark.parametrize("damage", ["url", "hash", "size", "unknown"])
async def test_broker_rejects_unbound_acquisition(tmp_path, damage):
    network = fixture_index()
    async with ResolutionIndexBroker(tmp_path.resolve() / "index", byte_limit=1024**2, check=lambda:None, network=network) as broker:
        data = json.loads(await broker._index("synthetic"))
        path = data["files"][0]["url"][len(broker.base):]
        if damage == "url": broker.catalog[path]["url"] = "https://example.com/foreign.whl"
        if damage == "hash": broker.catalog[path]["sha256"] = "f" * 64
        if damage == "size": broker.catalog[path]["size"] += 1
        if damage == "unknown": path += "wrong"
        with pytest.raises(PreparationError):
            await broker._file(path)


@pytest.mark.parametrize("damage", ["truncated", "concatenated", "limit", "invalid"])
async def test_index_gzip_is_bounded_and_single_framed(damage):
    data, limit = gzip.compress(b"synthetic" * 100), 1024
    if damage == "truncated": data = data[:-1]
    if damage == "concatenated": data += gzip.compress(b"other")
    if damage == "limit": limit = 10
    if damage == "invalid": data = b"invalid"
    async def chunks(): yield data
    with pytest.raises(DownloadError, match="download_response_invalid"):
        async for _ in PinnedHTTPSNetwork()._gunzip(chunks(), limit): pass


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox contract")
async def test_actual_sandbox_denies_network_secret_reads_writes_and_forks(tmp_path, group_reaper):
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    source, home = workspace / "source", workspace / "home"
    source.mkdir(); home.mkdir()
    secret = workspace / "receipt"
    secret.write_text("synthetic-private-receipt")
    listener = await asyncio.start_server(lambda r,w:w.close(), "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    python = Path(sys.executable).resolve()
    runtime = python.parents[1]
    profile = sandbox_profile(workspace, runtime, python, read_paths=[source, home], write_paths=[home])
    # A synthetic subprocess reports booleans only; actual sandbox behavior is
    # tested independently of profile text and without network data transfer.
    code = "import json,os,socket; results=[]\n"
    code += "for action in [lambda:open(" + repr(str(secret)) + ").read(),lambda:open(" + repr(str(source / 'write')) + ",'w'),lambda:socket.create_connection(('127.0.0.1'," + str(port) + "),.1),os.fork]:\n"
    code += " try: action(); results.append(False)\n except OSError: results.append(True)\nprint(json.dumps(results))\n"
    executor = PreparationExecutor(group_reaper)
    try:
        result = await executor.execute(Path("/usr/bin/sandbox-exec"), ["-p", profile, str(python), "-I", "-S", "-B", "-c", code],
            source, {"PATH":"/usr/bin:/bin","HOME":str(home),"TMPDIR":str(home)}, timeout_seconds=10)
        assert json.loads(result.stdout) == [True, True, True, True]
    finally:
        await executor.close()
        listener.close()
        await listener.wait_closed()
