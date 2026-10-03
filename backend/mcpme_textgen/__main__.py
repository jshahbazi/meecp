"""Inherited-descriptor bootstrap; the host directly owns both process PIDs."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import signal

import uvicorn

from .service import AttachedTextModel, make_app, model_declaration


def read_config(descriptor):
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("invalid_descriptor")
    config = json.loads(raw)
    if (config["endpoint"]["host"] != "127.0.0.1"
            or type(config["endpoint"]["port"]) is not int
            or not 1 <= config["endpoint"]["port"] <= 65535
            or json.dumps(config["model"], sort_keys=True) != json.dumps(model_declaration(), sort_keys=True)):
        raise ValueError("invalid_descriptor")
    return config


def run_engine(config):
    root = Path(__file__).resolve().parents[2]
    executable = root / "llama/llama-server.real"
    arguments = [str(executable), "--model", str(root / config["model"]["artifact"]),
                 "--host", "127.0.0.1", "--port", str(config["endpoint"]["port"]),
                 "--api-key-file", config["api_key_file"], "--ctx-size", "512",
                 "--device", "none", "--n-gpu-layers", "0", "--no-kv-offload", "--parallel", "1",
                 "--batch-size", "128", "--ubatch-size", "128", "--threads", "2",
                 "--no-context-shift", "--no-webui", "--log-disable"]
    # Exclude LLAMA_ARG_*, plugin and dyld overrides. No environment setting may
    # broaden the recipe's CPU-only engine policy or select external libraries.
    environment = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG") if key in os.environ}
    environment["PATH"] = "/usr/bin:/bin"
    os.chdir(root / "llama/plugins")
    os.execve(executable, arguments, environment)


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
    manager = AttachedTextModel(config)
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
    parser.add_argument("role", choices=("engine", "adapter"))
    parser.add_argument("--mcpme-config-fd", type=int, required=True)
    args = parser.parse_args()
    try:
        config = read_config(args.mcpme_config_fd)
        return run_engine(config) if args.role == "engine" else asyncio.run(run_adapter(config))
    except (Exception, KeyboardInterrupt):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
