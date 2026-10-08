"""Private descriptor entrypoints for the engine and its separate MCP adapter."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path

import uvicorn

from jevk5_app.server import make_app
from .runtime import AttachedModel


def read_config(descriptor: int):
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError()
    value = json.loads(raw)
    if not isinstance(value, dict) or value["endpoint"]["host"] != "127.0.0.1":
        raise ValueError()
    return value


def run_engine(config: dict):
    root = Path(__file__).resolve().parents[2]
    model = root / config["model"]["artifact"]
    executable = root / "llama/llama-server.real"
    # exec replaces this bootstrap, preserving the reaper's directly owned child.
    arguments = [str(executable), "--model", str(model), "--host", "127.0.0.1",
                 "--port", str(config["endpoint"]["port"]), "--api-key-file", config["api_key_file"],
                 "--ctx-size", "8192", "--n-gpu-layers", "99", "--parallel", "1",
                 "--flash-attn", "auto", "--batch-size", "2048", "--ubatch-size", "512",
                 "--no-context-shift"]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "PYTHONHOME", "GGML_BACKEND_PATH"}}
    os.chdir(root / "llama/plugins")
    os.execve(executable, arguments, environment)


class Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The outer finally must run before process exit; Uvicorn's default
        # signal replay bypasses it on SIGTERM with the locked version.
        import signal
        originals = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in originals.items():
                signal.signal(sig, handler)


async def run_adapter(config: dict):
    manager = AttachedModel(config)
    app, _ = make_app(manager, config["api_key"], port=config["endpoint"]["port"], readiness=manager.health,
                      tools_only=True)
    server = Server(uvicorn.Config(app, host="127.0.0.1", port=config["endpoint"]["port"],
                                  log_config=None, access_log=False, log_level="critical",
                                  timeout_graceful_shutdown=5))
    try:
        await server.serve()
    finally:
        await manager.shutdown()


def main():
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["engine", "adapter"])
    parser.add_argument("--meecp-config-fd", "--mcpme-config-fd", dest="meecp_config_fd", type=int, required=True)
    args = parser.parse_args()
    try:
        config = read_config(args.meecp_config_fd)
        if args.role == "engine":
            run_engine(config)
        else:
            asyncio.run(run_adapter(config))
    except (Exception, KeyboardInterrupt):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
