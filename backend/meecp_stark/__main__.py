"""Descriptor-only entrypoint for a private Stark scoring engine."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from meecp_jevk5.__main__ import Server, read_config
from .engine import make_app
from .worker import StarkWorker, _settle


async def watch_worker(worker, server):
    while not server.should_exit:
        # Uvicorn marks started only after lifespan startup has loaded the
        # owned worker and the HTTP listener is ready.
        if server.started and not worker.is_ready():
            server.should_exit = True
            return
        await asyncio.sleep(0.25)


async def run_engine(config: dict):
    root = Path(__file__).resolve().parents[2]
    token = config.get("api_key")
    if token is None:
        with open(config["api_key_file"], "r", encoding="utf-8") as stream:
            token = stream.read(65537).strip()
    if (not isinstance(token, str) or not token or len(token) > 65536
            or type(config["endpoint"]["port"]) is not int or not 1 <= config["endpoint"]["port"] <= 65535
            or type(config["generation"]) is not int or not isinstance(config["nonce"], str)):
        raise ValueError("invalid_engine_config")
    worker = StarkWorker(root, config["model"])
    app = make_app(worker, token, port=config["endpoint"]["port"],
                   generation=config["generation"], nonce=config["nonce"])
    server = Server(uvicorn.Config(app, host="127.0.0.1", port=config["endpoint"]["port"],
                                  log_config=None, access_log=False, log_level="critical",
                                  timeout_graceful_shutdown=5))
    monitor = asyncio.create_task(watch_worker(worker, server))
    try:
        await server.serve()
    finally:
        monitor.cancel()
        await _settle(asyncio.gather(monitor, return_exceptions=True))
        await _settle(worker.close())


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    class SafeParser(argparse.ArgumentParser):
        def error(self, _message):
            raise ValueError("invalid_engine_arguments")
    try:
        parser = SafeParser()
        parser.add_argument("role", choices=["engine"], nargs="?", default="engine")
        parser.add_argument("--meecp-config-fd", "--mcpme-config-fd", dest="meecp_config_fd", type=int, required=True)
        args = parser.parse_args()
        asyncio.run(run_engine(read_config(args.meecp_config_fd)))
    except (Exception, KeyboardInterrupt):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
