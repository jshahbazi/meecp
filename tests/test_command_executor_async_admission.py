"""Awaited host authority must preserve executor close fences."""
import asyncio

import pytest

from mcpme.command_execution_v2 import CommandExecutionError
from test_command_execution_v2 import binaries, executor, spec, pytestmark


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["launch", "result"])
async def test_executor_close_wins_awaited_admission(binaries, tmp_path, boundary):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def admit(command):
        nonlocal calls
        calls += 1
        if calls == (1 if boundary == "launch" else 2):
            entered.set()
            await release.wait()
        return True

    runtime = executor(binaries, tmp_path / "state", admit=admit)
    work = asyncio.create_task(runtime.execute(spec(binaries), b"synthetic"))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert runtime.closed and not closing.done()
        release.set()
        result = (await asyncio.gather(work, return_exceptions=True))[0]
        await closing
        assert isinstance(result, CommandExecutionError)
        assert len(runtime.journal["entries"]) == (0 if boundary == "launch" else 1)
        assert runtime.reconcile()["pending"] == 0
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        if closing is not None:
            await closing
        await runtime.close()
