"""Private service and client adapter command entry points."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from .bridge import run_bridge
from .runtime import ModelManager
from .server import make_app, parent_watchdog
from .state import HOST, PORT, PrivateState
from mcpme_local_transport import bind_private_listener


async def serve(resources: Path, state_dir: Path, parent_pid: int):
    private = PrivateState(state_dir)
    listener = None
    manager = None
    watchdog = None
    try:
        # Reserve TCP before advertising the private socket. Both share policy.
        import socket
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, PORT))
        listener.listen(128)
        listener.setblocking(False)
        private_listener = bind_private_listener(state_dir, "jevk5")
        try:
            manager = ModelManager(resources, state_dir)
            app, _ = make_app(manager, private.token)
            server = uvicorn.Server(uvicorn.Config(app, access_log=False,
                log_config=None, log_level="critical", timeout_graceful_shutdown=130))
            watchdog = asyncio.create_task(parent_watchdog(parent_pid, server))
            await server.serve(sockets=[listener, private_listener])
        finally:
            private_listener.close()
    finally:
        if watchdog:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        if manager:
            await manager.shutdown()
        if listener:
            listener.close()
        private.close()


def main(argv=None):
    # Dependency logs can contain exception inputs; disable them at process scope.
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="jevk5-app")
    subparsers = parser.add_subparsers(dest="command", required=True)
    service = subparsers.add_parser("serve")
    service.add_argument("--resources", type=Path, required=True)
    service.add_argument("--state-dir", type=Path, required=True)
    service.add_argument("--parent-pid", type=int, required=True)
    bridge = subparsers.add_parser("bridge")
    bridge.add_argument("--resources", type=Path, required=True)
    bridge.add_argument("--state-dir", type=Path, required=True)
    bridge.add_argument("--app", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            if args.parent_pid <= 1:
                return 2
            asyncio.run(serve(args.resources.resolve(), args.state_dir.resolve(), args.parent_pid))
        else:
            asyncio.run(run_bridge(args.state_dir.resolve(), args.app.resolve()))
    except (Exception, KeyboardInterrupt):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
