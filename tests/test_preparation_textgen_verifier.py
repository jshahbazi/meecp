import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meecp import preparation_textgen_verifier as verifier
from meecp.preparation_contracts import PreparationError
from test_preparation_textgen import descriptor, draft


@pytest.mark.parametrize("limit", [True, 0, -1, 3601, float("nan"), float("inf")])
async def test_invalid_probe_deadlines_never_hash_or_launch(limit):
    with pytest.raises(PreparationError, match="invalid_preparation_probe_limit"):
        await verifier.verify_textgen_package(Path("/missing"), None, Path("/missing"), Path("/missing"), timeout_seconds=limit)


@pytest.mark.parametrize("with_graph", [False, True])
async def test_repeated_cancel_retains_both_instances_until_native_settlement(tmp_path, monkeypatch, with_graph):
    closing, settle, launched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    package = SimpleNamespace(root=tmp_path, digest="a" * 64)
    async def validated(path): return package
    monkeypatch.setattr(verifier, "_validate_joined", validated)
    monkeypatch.setattr(verifier, "validate_model_package", lambda *args, **kwargs: None)

    class Process:
        def __init__(self, owner, config, identifier):
            self.owner, self.config, self.instance_id = owner, config, identifier
            self.pid, self.returncode, self.ownership_lost = 123, None, False
            self._done = asyncio.get_running_loop().create_future()
        def close_stdio(self): pass
        async def stop(self):
            closing.set()
            await settle.wait()
            self.owner.instances.pop(self.instance_id, None)
            if not self._done.done(): self._done.set_result(None)

    class Owner:
        def __init__(self):
            self.instances = {}
            self.graph_accounting = object() if with_graph else None
        async def launch(self, executable, args, cwd, env, **kwargs):
            assert kwargs.get("graph_role") == (executable.name.removeprefix("textgen-") if with_graph else None)
            config = json.loads(os.read(kwargs["inherited_fds"][0], 4096))
            process = Process(self, config, executable.name)
            self.instances[process.instance_id] = process
            if len(self.instances) == 2: launched.set()
            return process
        async def close(self):
            await asyncio.gather(*(p.stop() for p in tuple(self.instances.values())))
    owner = Owner()

    async def response(client, method, url, **kwargs):
        if url.endswith("/props"): return {}
        adapter = owner.instances["textgen-adapter"]
        return {"ready": True, "generation": 1, "nonce": adapter.config["nonce"]}
    monkeypatch.setattr(verifier, "bounded_json", response)
    # Stop at the first real probe HTTP request, after both owned spawns.
    async def waiting_get(*args, **kwargs): await asyncio.Event().wait()
    monkeypatch.setattr(verifier.httpx.AsyncClient, "get", waiting_get)
    task = asyncio.create_task(verifier.verify_textgen_package(tmp_path, descriptor(draft()), tmp_path / "reaper",
                                                              tmp_path, owner=owner, graph_accounting=owner.graph_accounting))
    await asyncio.wait_for(launched.wait(), 2)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 2)
    for _ in range(3):
        task.cancel(); await asyncio.sleep(0)
        assert not task.done() and len(owner.instances) == 2
    settle.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert not owner.instances
