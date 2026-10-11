"""Inherited-descriptor bootstrap for the host-owned Uzu MCP adapter only."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
import signal

import uvicorn

from .service import AttachedUzuModel, make_app


def read_config(descriptor):
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("invalid_descriptor")
    config = json.loads(raw)
    if (type(config) is not dict or config["endpoint"]["host"] != "127.0.0.1"
            or type(config["endpoint"]["port"]) is not int
            or not 1 <= config["endpoint"]["port"] <= 65535
            or type(config["api_key"]) is not str or not config["api_key"]):
        raise ValueError("invalid_descriptor")
    return config


class Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        originals = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in originals.items():
                signal.signal(sig, handler)


async def run_adapter(config):
    manager = AttachedUzuModel(config)
    app, _ = make_app(manager, config["api_key"], port=config["endpoint"]["port"])
    server = Server(uvicorn.Config(app, host="127.0.0.1", port=config["endpoint"]["port"],
        log_config=None, access_log=False, log_level="critical", timeout_graceful_shutdown=5))

    async def fence_exit():
        await manager.fatal.wait()
        server.should_exit = True

    watcher = asyncio.create_task(fence_exit())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await manager.shutdown()
    return 2 if manager.fatal.is_set() else 0


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("adapter",), nargs="?", default="adapter")
    parser.add_argument("--meecp-config-fd", "--mcpme-config-fd", dest="meecp_config_fd", type=int, required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(run_adapter(read_config(args.meecp_config_fd)))
    except (Exception, KeyboardInterrupt):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
