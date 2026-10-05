"""Actual AppKit configuration and transfer handlers; synthetic owner replies."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_actual_provider_configuration_and_transfer_ownership_handlers(tmp_path):
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [sys.executable, "-B", str(root / "scripts/verify_agent_preparation_view.py")],
        cwd=root,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        output, _ = process.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError("agent_provider_native_owned_deadline") from None
    assert process.returncode == 0, "agent_provider_native_contract_failed"
    assert output == b"stage=complete code=agent_view_handlers_passed\n", "agent_provider_native_receipt_invalid"
