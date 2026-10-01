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


async def serve(resources: Path, state_dir: Path, parent_pid: int):
    private = PrivateState(state_dir)
    manager = ModelManager(resources, state_dir)
    app, _ = make_app(manager, private.token)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, access_log=False,
                                         log_config=None, log_level="critical", timeout_graceful_shutdown=130))
    watchdog = asyncio.create_task(parent_watchdog(parent_pid, server))
    try:
        await server.serve()
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await manager.shutdown()
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
