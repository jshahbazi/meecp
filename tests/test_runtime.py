import asyncio
import threading
import time

import pytest

from jevk5_app.contracts import ServiceError
from jevk5_app.runtime import ModelManager
from jevk5_app.server import parent_watchdog


QUESTION = {"type": "noul", "instructions": "Synthetic proposition?"}


class Child:
    pid = 123456

    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


class Scorer:
    last_seconds = 0.01
    cache_hit = False

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self.cleared = 0

    def clear(self):
        self.cleared += 1

    def decide(self, state, question):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        self.release.wait(5)
        self.active -= 1
        return {"type": "noul", "noul": 0.5, "confidence": 0.5, "input_tokens": 3}


def ready_manager(tmp_path, limit=8):
    manager = ModelManager(tmp_path, tmp_path, queue_limit=limit)
    manager.state = "ready"
    manager.child = Child()
    manager.scorer = Scorer()
    return manager


async def wait_entered(scorer):
    for _ in range(100):
        if scorer.entered.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker did not start")


@pytest.mark.asyncio
async def test_serialized_calls_bounded_queue_and_pause(tmp_path):
    manager = ready_manager(tmp_path, 2)
    scorer, child = manager.scorer, manager.child
    first = asyncio.create_task(manager.decide("public", QUESTION))
    await wait_entered(scorer)
    second = asyncio.create_task(manager.decide("public", QUESTION))
    await asyncio.sleep(0)
    with pytest.raises(ServiceError) as caught:
        await manager.decide("public", QUESTION)
    assert caught.value.code == "queue_full"
    pause = asyncio.create_task(manager.control("pause"))
    await asyncio.sleep(0.02)
    assert not pause.done() and not child.terminated
    scorer.release.set()
    await first
    with pytest.raises(ServiceError, match="paused"):
        await second
    await pause
    assert scorer.max_active == 1
    assert child.terminated
    assert manager.pending == 0
    assert manager.status()["state"] == "paused"


@pytest.mark.asyncio
async def test_cancelled_client_keeps_lock_until_worker_finishes(tmp_path):
    manager = ready_manager(tmp_path)
    scorer, child = manager.scorer, manager.child
    call = asyncio.create_task(manager.decide("public", QUESTION))
    await wait_entered(scorer)
    call.cancel()
    pause = asyncio.create_task(manager.control("pause"))
    await asyncio.sleep(0.02)
    assert not child.terminated and not pause.done()
    scorer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    await pause
    assert child.terminated and scorer.cleared >= 1


@pytest.mark.asyncio
async def test_shutdown_terminates_only_owned_child_and_clears_key(tmp_path):
    manager = ready_manager(tmp_path)
    child = manager.child
    manager.key_file = tmp_path / "private-key"
    manager.key_file.write_text("test-key")
    await manager.shutdown()
    assert child.terminated
    assert not (tmp_path / "private-key").exists()
    assert manager.scorer is None


@pytest.mark.asyncio
async def test_parent_exit_requests_server_shutdown(monkeypatch):
    class Server:
        should_exit = False

    def gone(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr("jevk5_app.server.os.kill", gone)
    monkeypatch.setattr("jevk5_app.server.os.getppid", lambda: 987654)
    server = Server()
    await parent_watchdog(987654, server)
    assert server.should_exit


@pytest.mark.asyncio
async def test_startup_owns_expected_command_and_reload_clears_cache(tmp_path, monkeypatch):
    manifest = {"model_id": "test", "sha256": "a" * 64, "profile": "m5-benchmark-v1", "context_tokens": 8192,
                "temperature": 1.22, "knockout_temperature": 0.93}
    commands = []
    children = []

    def popen(command, **kwargs):
        commands.append((command, kwargs))
        child = Child()
        children.append(child)
        return child

    class Health:
        status_code = 200

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers):
            assert headers["Authorization"].startswith("Bearer ")
            return Health()

    monkeypatch.setattr("jevk5_app.runtime.verified_model", lambda r, s: (manifest, tmp_path / "test.gguf"))
    monkeypatch.setattr("jevk5_app.runtime.subprocess.Popen", popen)
    monkeypatch.setattr("jevk5_app.runtime.httpx.AsyncClient", Client)
    manager = ModelManager(tmp_path, tmp_path)
    await manager.start()
    await manager.load_task
    assert manager.status()["ready"]
    command, kwargs = commands[0]
    assert command[0] == str(tmp_path / "llama" / "llama-server")
    assert command[command.index("--ctx-size") + 1] == "8192"
    assert command[command.index("--parallel") + 1] == "1"
    assert "--no-context-shift" in command
    assert "GGML_BACKEND_PATH" not in kwargs["env"]
    assert kwargs["cwd"] == tmp_path / "llama" / "plugins"
    assert manager.scorer.previous_ids is None
    await manager.control("pause")
    assert children[0].terminated
    await manager.control("resume")
    await manager.load_task
    assert manager.status()["ready"] and manager.scorer.previous_ids is None
    await manager.shutdown()
    assert children[1].terminated
