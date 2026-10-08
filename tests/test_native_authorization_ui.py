"""Exercise the shipped AppKit coordinator and Pi coexistence contracts."""
from pathlib import Path
import platform
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def native_authorization_binary():
    if platform.system() != "Darwin":
        pytest.skip("AppKit authorization is macOS-only")
    result = subprocess.run(["swift", "build", "--product", "MeecpMenuBar"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    assert result.returncode == 0, "native_build_failed"
    return ROOT / ".build/debug/MeecpMenuBar"


@pytest.mark.parametrize("flag,receipt", [
    ("--authorization-ui-self-test", "authorization_ui_contracts_passed"),
    ("--pi-setup-ui-self-test", "pi_setup_ui_contracts_passed"),
])
def test_authorization_and_pi_presentation_contracts(native_authorization_binary, flag, receipt):
    result = subprocess.run([str(native_authorization_binary), flag], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20)
    assert result.returncode == 0
    assert result.stdout.strip() == receipt
