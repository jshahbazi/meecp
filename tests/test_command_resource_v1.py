"""Small actual-native monitored accounting; all hard release claims stay false."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import platform
import subprocess

import pytest

from mcpme import command_resource_execution_v1 as resource_execution
from mcpme.command_resource_execution_v1 import CommandExecutionError, CommandResourceExecutorV1, CommandResourceSpecV1, _platform_policy
from mcpme.command_resource_policy_v1 import ResourcePolicyV1

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(platform.system() != "Darwin" or platform.machine() != "arm64" or
    platform.mac_ver()[0].split(".")[0] != "26", reason="macOS26 arm64 resource checkpoint")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    root = tmp_path_factory.mktemp("command-resource")
    package = root / "package"
    package.mkdir(mode=0o700)
    paths = (root / "helper", package / "target")
    for source, output in zip(("scripts/mcpme-command-resource-reap.c", "tests/fixtures/command_resource_target.c"), paths):
        compiled = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(ROOT / source), "-o", str(output)],
            capture_output=True, timeout=30)
        assert compiled.returncode == 0
    return paths


def executor(binaries, state):
    # Development fixture binding is not release acceptance of this platform.
    return CommandResourceExecutorV1(binaries[0], digest(binaries[0]), state,
        expected_platform_policy=resource_execution._platform_policy(), admit=lambda spec: True)


@pytest.mark.parametrize("expected", ["0" * 64, "", None])
def test_constructor_rejects_unaccepted_actual_platform_before_state_or_launch(binaries, tmp_path, monkeypatch, expected):
    state = tmp_path / "state"
    current = _platform_policy()
    assert current != expected
    launched = False

    def forbidden_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("unexpected_native_launch")

    monkeypatch.setattr(subprocess, "Popen", forbidden_launch)
    with pytest.raises(CommandExecutionError, match="^command_platform_policy_mismatch$"):
        CommandResourceExecutorV1(binaries[0], digest(binaries[0]), state,
            expected_platform_policy=expected, admit=lambda spec: True)
    assert not state.exists() and not launched


def spec(binaries, mode, policy=None):
    target = binaries[1]
    return CommandResourceSpecV1(target.parent, target, target.parent, digest(target), (mode,), deadline_ms=2000,
        resource_policy=policy or ResourcePolicyV1())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["echo", "overlap", "short"])
async def test_actual_windowed_report_never_claims_complete_lifetime_or_result(binaries, tmp_path, mode):
    owner = executor(binaries, tmp_path / "state")
    try:
        result = await owner.execute(spec(binaries, mode))
        report = result.report
        assert report.samples > 0 and report.identities_seen >= 2
        assert report.max_scope_window_footprint_bytes >= report.max_group_window_footprint_bytes > 0
        assert report.observed_own_cpu_ns > 0 and report.timebase_numer > 0 and report.timebase_denom > 0
        assert not report.lifetime_coverage_complete and not report.hard_aggregate_enforced
        assert not result.result_admissible and not hasattr(result, "stdout")
        if mode == "overlap":
            assert report.max_group_members == 3
            assert report.max_group_window_footprint_bytes >= 20 * 1024**2
        assert owner.reconcile()["pending"] == 0
        assert b"synthetic" not in owner.journal_path.read_bytes()
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,policy,reason", [
    ("overlap", ResourcePolicyV1(footprint_bytes=16 * 1024**2), "footprint_threshold"),
    ("overlap", ResourcePolicyV1(group_members=1), "process_threshold"),
    ("cpu", ResourcePolicyV1(cpu_ns=20_000_000), "cpu_threshold"),
    ("echo", ResourcePolicyV1(read_window_ns=1), "read_window_exceeded"),
])
async def test_actual_threshold_or_invalid_window_cancels_and_settles(binaries, tmp_path, mode, policy, reason):
    owner = executor(binaries, tmp_path / "state")
    try:
        result = await owner.execute(spec(binaries, mode, policy))
        assert result.report.reason == reason
        assert owner.reconcile() == {"pending": 0, "reserved_units": 0}
        assert not result.result_admissible
    finally:
        await owner.close()


def test_actual_cpu_timebase_matches_independent_process_clock_and_overflow_rejects(tmp_path):
    executable = tmp_path / "cpu-probe"
    result = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-O2",
        str(ROOT / "tests/fixtures/command_resource_cpu_probe.c"), "-o", str(executable)], capture_output=True, timeout=30)
    assert result.returncode == 0
    completed = subprocess.run([str(executable)], capture_output=True, timeout=3)
    assert completed.returncode == 0 and not completed.stderr and len(completed.stdout) < 1024
    value = json.loads(completed.stdout)
    assert value["converted_ns"] == (value["raw"] * value["numer"] + value["denom"] - 1) // value["denom"]
    assert .8 <= value["converted_ns"] / value["clock_cpu_ns"] <= 1.2
    assert value["overflow_rejected"] == 1 and value["addition_rejected"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["setsid", "setpgid", "spawn-group", "spawn-session", "fork-exec", "fds", "effects", "tree"])
async def test_new_helper_keeps_actual_confined_topology_effect_and_fd_boundaries(binaries, tmp_path, mode):
    package = tmp_path / "package"
    package.mkdir(mode=0o700)
    target = package / "target"
    result = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2",
        str(ROOT / "tests/fixtures/command_v2_target.c"), "-o", str(target)], capture_output=True, timeout=30)
    assert result.returncode == 0
    pair = (binaries[0], target)
    owner = executor(pair, tmp_path / "state")
    try:
        result = await owner.execute(spec(pair, mode, ResourcePolicyV1(interval_ns=100_000_000, sample_gap_ns=200_000_000)))
        assert result.returncode == 0 and result.report.reason == "exited"
        assert not result.result_admissible and owner.reconcile()["pending"] == 0
    finally:
        await owner.close()
