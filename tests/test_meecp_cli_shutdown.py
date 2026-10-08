"""Actual CLI signals must complete the supervisor's outer cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.skipif(
    platform.system() != "Darwin" or platform.machine() != "arm64"
    or int(platform.mac_ver()[0].split(".")[0]) < 26,
    reason="CLI supervisor requires macOS 26 on Apple silicon",
)
@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
def test_cli_signal_removes_discovery_and_exits_cleanly(tmp_path: Path, shutdown_signal: signal.Signals):
    state = (tmp_path / "state").resolve()
    backend = Path(__file__).resolve().parents[1] / "backend"
    process = subprocess.Popen(
        [sys.executable, "-m", "meecp", "--state-dir", str(state), "serve",
         "--reaper", str((tmp_path / "unused-reaper").resolve())],
        cwd=tmp_path.resolve(), env={**os.environ, "PYTHONPATH": str(backend)},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    discovery = state / "manager-connection.json"
    try:
        # An authenticated successful request also proves uvicorn has installed
        # its signal handlers; discovery is written immediately before serve().
        deadline = time.monotonic() + 10
        with httpx.Client(timeout=0.25, trust_env=False) as client:
            while True:
                if process.poll() is not None:
                    pytest.fail("supervisor_exited_before_ready")
                if time.monotonic() >= deadline:
                    pytest.fail("supervisor_startup_timeout")
                if discovery.exists():
                    connection = json.loads(discovery.read_text())
                    try:
                        response = client.get(connection["endpoint"] + "/v1/capabilities",
                                              headers={"Authorization": "Bearer " + connection["token"]})
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                time.sleep(0.02)
        process.send_signal(shutdown_signal)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        assert not discovery.exists()
        assert not stdout, "unexpected_cli_stdout"
        assert not stderr, "unexpected_cli_stderr"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
