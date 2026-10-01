"""Host-owned gateway worker and a narrowly scoped supervisor channel."""
from __future__ import annotations

import argparse
import array
import asyncio
from collections.abc import Callable
import contextlib
import json
import logging
import math
import os
import socket
import time

import uvicorn

from .gateway import SessionPipes, make_app


class RemoteLauncher:
    def __init__(self, channel: socket.socket, timeout: float = 135,
                 on_failure: Callable[[], None] | None = None):
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("invalid_control_timeout")
        self.channel = channel
        self.timeout = timeout
        self.on_failure = on_failure
        self.failed = False
        self.lock = asyncio.Lock()

    def _exchange(self, message: dict) -> tuple[dict, list[int]]:
        descriptors = []
        try:
            deadline = time.monotonic() + self.timeout
            self.channel.settimeout(self.timeout)
            self.channel.send(json.dumps(message).encode())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            self.channel.settimeout(remaining)
            payload, ancillary, flags, _ = self.channel.recvmsg(8192, socket.CMSG_SPACE(2 * array.array("i").itemsize))
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(data[:len(data) - len(data) % values.itemsize])
                    descriptors.extend(values)
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise ValueError()
            result = json.loads(payload)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError()
            if message["action"] == "launch_session":
                if (set(result) != {"instance_id"} or not isinstance(result["instance_id"], str)
                        or not 1 <= len(result["instance_id"]) <= 128 or len(descriptors) != 2):
                    raise ValueError()
            elif result != {"closed": True} or descriptors:
                raise ValueError()
            return result, descriptors
        except Exception:
            for fd in descriptors:
                os.close(fd)
            self.channel.close()
            if not self.failed:
                self.failed = True
                if self.on_failure:
                    # Called inside the exchange thread; the worker supplies a
                    # thread-safe handoff that closes admission and exits HTTP.
                    with contextlib.suppress(Exception):
                        self.on_failure()
            raise RuntimeError("supervisor_unavailable") from None

    @staticmethod
    async def _settle(task):
        """Drain one thread result while retaining the channel lock, even on recancellation."""
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise

    async def _exchange_async(self, message):
        task = asyncio.create_task(asyncio.to_thread(self._exchange, message))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A canceled coroutine cannot cancel recvmsg in its worker thread.
            # Consume that response before any new exchange may use this socket.
            try:
                result, fds = await self._settle(task)
            except Exception:
                pass
            else:
                for fd in fds:
                    os.close(fd)
                if message["action"] == "launch_session":
                    cleanup = asyncio.create_task(asyncio.to_thread(self._exchange,
                        {"action": "close_session", "instance_id": result["instance_id"]}))
                    try:
                        await self._settle(cleanup)
                    except Exception:
                        pass
            raise

    async def launch(self, session_id: str) -> SessionPipes:
        async with self.lock:
            result, fds = await self._exchange_async({"action": "launch_session", "session_id": session_id})
            return SessionPipes(instance_id=result["instance_id"], stdin_fd=fds[0], stdout_fd=fds[1])

    async def close(self, instance_id: str) -> None:
        async with self.lock:
            _, fds = await self._exchange_async({"action": "close_session", "instance_id": instance_id})
        for fd in fds:
            os.close(fd)


async def run(config_fd: int, listener_fd: int, channel_fd: int):
    with os.fdopen(config_fd, "rb") as stream:
        config = json.loads(stream.read(65537))
    listener = socket.socket(fileno=listener_fd)
    channel = socket.socket(fileno=channel_fd)
    launcher = RemoteLauncher(channel, timeout=config["control_timeout"])
    app, gateway = make_app(
        token=config["token"], port=config["port"],
        launch_session=launcher.launch, close_session=launcher.close,
        max_sessions=config["max_sessions"], idle_timeout=config["idle_timeout"],
        request_timeout=config["request_timeout"], active=lambda: config["active"],
    )
    server = uvicorn.Server(uvicorn.Config(
        app, access_log=False, log_config=None, log_level="critical",
        timeout_graceful_shutdown=2,
    ))
    loop = asyncio.get_running_loop()
    def fail_control_channel():
        gateway.stopping = True
        server.should_exit = True
    launcher.on_failure = lambda: loop.call_soon_threadsafe(fail_control_channel)
    try:
        await server.serve(sockets=[listener])
    finally:
        try:
            await gateway.shutdown()
        finally:
            channel.close()
            listener.close()


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-fd", type=int, required=True)
    parser.add_argument("--listener-fd", type=int, required=True)
    parser.add_argument("--channel-fd", type=int, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.config_fd, args.listener_fd, args.channel_fd))
    except (Exception, KeyboardInterrupt):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
