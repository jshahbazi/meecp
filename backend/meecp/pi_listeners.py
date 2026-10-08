"""Observe TCP listeners owned by a native package process group.

This is a detection and cleanup boundary. Inspection does not prevent a bind
between checks; callers retain native ownership until cleanup has completed.
"""
from __future__ import annotations

import asyncio
import contextlib

from .processes import _await_ownership


class ListenerError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


async def nonloopback_listener(process):
    if process.returncode is not None or process.pid is None:
        return False
    inspector = None
    try:
        async with asyncio.timeout(3):
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                "/usr/sbin/lsof", "-nP", "-a", "-g", str(process.pid),
                "-iTCP", "-sTCP:LISTEN", "-Fn", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"}))
            try:
                inspector = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                inspector = await _await_ownership(spawning)
                raise
            data = await inspector.stdout.read(65537)
            await inspector.wait()
            if inspector.returncode not in (0, 1):
                raise ListenerError("listener_inspection_failed")
            if len(data) > 65536:
                raise ListenerError("listener_inspection_failed")
            names = [line[1:] for line in data.splitlines() if line.startswith(b"n")]
            return any(not (name.startswith(b"127.0.0.1:") or name.startswith(b"[::1]:")) for name in names)
    except (OSError, TimeoutError):
        raise ListenerError("listener_inspection_failed") from None
    finally:
        if inspector is not None and inspector.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                inspector.kill()
            await _await_ownership(inspector.wait())


async def assert_loopback(process):
    if await nonloopback_listener(process):
        raise ListenerError("listener_not_loopback")


async def monitor_listeners(process):
    """Monitor until native completion; every inspector is joined by its owner."""
    while not process._done.done():
        await assert_loopback(process)
        try:
            await asyncio.wait_for(asyncio.shield(process._done), .1)
        except TimeoutError:
            pass


async def watch_listeners(process, action):
    """Run an action with continuous inspection and a final live-process check."""
    task = asyncio.ensure_future(action)
    audit = asyncio.create_task(monitor_listeners(process))
    try:
        await asyncio.wait((task, audit), return_when=asyncio.FIRST_COMPLETED)
        if audit.done():
            await audit
        result = await task
        await assert_loopback(process)
        return result
    finally:
        for pending in (task, audit):
            if not pending.done():
                pending.cancel()
        await _await_ownership(asyncio.gather(task, audit, return_exceptions=True))
