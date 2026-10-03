"""Actual tiny public-API capability probe; results never imply aggregate limits."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64", reason="Public Darwin Mach API")
def test_owned_self_footprint_api_has_explicit_scope_and_permission_result(tmp_path):
    source = ROOT / "tests/fixtures/command_footprint_api_probe.c"
    helper = tmp_path / "probe"
    compiled = subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        str(source), "-o", str(helper)], capture_output=True, timeout=20)
    assert compiled.returncode == 0
    actual = subprocess.run([str(helper)], capture_output=True, timeout=5)
    assert actual.returncode == 0 and not actual.stderr
    report = json.loads(actual.stdout)
    assert not report["hard_aggregate_enforced"] and not report["lifetime_coverage_complete"]
    if report["initial_result"] == 0:
        assert report["child_joined"] == 1 and report["child_usage_ok"] == 1
        assert report["child_terminal_peak_bytes"] > 0
    else:
        assert report["child_joined"] == 0 and report["child_terminal_peak_bytes"] == 0
    sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, timeout=5)
    assert sdk.returncode == 0
    header = Path(sdk.stdout.decode().strip()) / "usr/include/mach/task.h"
    record = {"report": report, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "sdk_header_sha256": hashlib.sha256(header.read_bytes()).hexdigest(),
        "os": platform.mac_ver()[0], "kernel": platform.version(), "architecture": platform.machine()}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    receipt.chmod(0o600)
