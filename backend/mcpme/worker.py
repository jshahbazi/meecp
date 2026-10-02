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
import signal
import socket
import time

import uvicorn

from .gateway import SessionPipes, make_app
from .limits import DEFAULT_REQUEST_BYTES
from .activity import ActivityReporter


class WorkerServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # Consume supervisor shutdown so run() can finish cleanup and publish
        # its final activity marker before exiting the owned worker process.
        originals = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in originals.items():
                signal.signal(sig, handler)


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
            if not isinstance(result, dict):
                raise ValueError()
            if result.get("error"):
                if message["action"] == "slot_control" and set(result) == {"error"} and not descriptors:
                    return result, descriptors
                raise ValueError()
            if message["action"] == "launch_session":
                if (set(result) != {"instance_id"} or not isinstance(result["instance_id"], str)
                        or not 1 <= len(result["instance_id"]) <= 128 or len(descriptors) != 2):
                    raise ValueError()
            elif message["action"] == "slot_control":
                if set(result) != {"status", "runtime"} or not isinstance(result["status"], dict) or descriptors:
                    raise ValueError()
                runtime = result["runtime"]
                if runtime is not None and (not isinstance(runtime, dict) or set(runtime) != {"endpoint", "key", "nonce", "generation"}
                        or any(not isinstance(runtime[key], str) or not 1 <= len(runtime[key]) <= 256
                               for key in ("endpoint", "key", "nonce"))
                        or type(runtime["generation"]) is not int or runtime["generation"] < 1):
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

    async def slot_control(self, action: str) -> dict:
        if action not in {"status", "pause", "resume"}:
            raise RuntimeError("invalid_slot_control")
        async with self.lock:
            result, _ = await self._exchange_async({"action": "slot_control", "control": action})
        if "error" in result:
            raise RuntimeError("slot_control_failed")
        return result


async def run(config_fd: int, listener_fd: int, channel_fd: int, activity_fd: int, private_listener_fds=()):
    with os.fdopen(config_fd, "rb") as stream:
        config = json.loads(stream.read(65537))
    listener = socket.socket(fileno=listener_fd)
    private_listeners = [socket.socket(fileno=fd) for fd in private_listener_fds]
    channel = socket.socket(fileno=channel_fd)
    launcher = RemoteLauncher(channel, timeout=config["control_timeout"])
    activity = ActivityReporter(socket.socket(fileno=activity_fd))
    if config.get("profile", "mcp-stdio") == "model-service":
        from .http_gateway import make_http_app
        legacy_routes = config.get("legacy_routes", [])
        if legacy_routes and set(legacy_routes) != {"/api/status", "/api/decide", "/api/control"}:
            raise ValueError("invalid_legacy_routes")

        async def slot_control(action):
            if action != "status":
                gateway.runtime_active = False
            result = await launcher.slot_control(action)
            runtime = result["runtime"]
            endpoint, token = (runtime["endpoint"], runtime["key"]) if runtime else (None, None)
            if endpoint != gateway.upstream_endpoint or token != gateway.upstream_token:
                await gateway.configure(endpoint, token)
            else:
                gateway.runtime_active = endpoint is not None
            return result["status"]

        app, gateway = make_http_app(
            token=config["token"], port=config["port"], upstream_endpoint=config.get("upstream_endpoint"),
            upstream_token=config.get("upstream_key"), max_sessions=config["max_sessions"],
            idle_timeout=config["idle_timeout"], request_timeout=config["request_timeout"],
            max_request_bytes=config.get("max_request_bytes", DEFAULT_REQUEST_BYTES),
            active=lambda: config["active"], legacy_jev=bool(legacy_routes), control=slot_control,
            # The scoped RPC has its own startup/cleanup deadline; generation
            # replacement then permits at most two seconds for private DELETE.
            control_timeout=config["control_timeout"] + 3,
            activity_report=activity.report,
        )
    else:
        app, gateway = make_app(
            token=config["token"], port=config["port"],
            launch_session=launcher.launch, close_session=launcher.close,
            max_sessions=config["max_sessions"], idle_timeout=config["idle_timeout"],
            request_timeout=config["request_timeout"], active=lambda: config["active"],
            max_request_bytes=config.get("max_request_bytes", DEFAULT_REQUEST_BYTES),
            activity_report=activity.report,
        )
    server = WorkerServer(uvicorn.Config(
        app, access_log=False, log_config=None, log_level="critical",
        timeout_graceful_shutdown=2,
    ))
    loop = asyncio.get_running_loop()
    def fail_control_channel():
        gateway.stopping = True
        server.should_exit = True
    launcher.on_failure = lambda: loop.call_soon_threadsafe(fail_control_channel)
    try:
        await server.serve(sockets=[listener, *private_listeners])
    finally:
        try:
            await gateway.shutdown()
        finally:
            activity.close()
            channel.close()
            listener.close()
            for private in private_listeners:
                private.close()


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-fd", type=int, required=True)
    parser.add_argument("--listener-fd", type=int, required=True)
    parser.add_argument("--channel-fd", type=int, required=True)
    parser.add_argument("--activity-fd", type=int, required=True)
    parser.add_argument("--private-listener-fd", type=int, action="append", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.config_fd, args.listener_fd, args.channel_fd, args.activity_fd, args.private_listener_fd))
    except (Exception, KeyboardInterrupt):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
