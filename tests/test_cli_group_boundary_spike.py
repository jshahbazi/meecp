"""Native feasibility probes only. No production topology/capability is enabled."""
from __future__ import annotations

import errno
import json
from pathlib import Path
import platform
import subprocess

import pytest

pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="macOS Seatbelt feasibility probe")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-group-probe")
    for name in ("cli_group_boundary", "cli_group_settlement"):
        subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2",
                        str(ROOT / "tests" / "fixtures" / (name + ".c")), "-o", str(root / name)],
                       check=True, capture_output=True, timeout=30)
    return root


def run(binary, *args):
    result = subprocess.run([str(binary), *args], capture_output=True, text=True,
                            timeout=8, start_new_session=True)
    assert result.returncode == 0, "native feasibility probe did not complete"
    return json.loads(result.stdout)


@pytest.mark.parametrize("policy", ["plain", "filtered", "filter-spawn", "restricted"])
@pytest.mark.parametrize("operation", ["fork-only", "setsid", "setpgid", "fork-exec", "spawn-group", "spawn-session"])
def test_actual_nonleader_group_boundary(binaries, policy, operation):
    receipt = run(binaries / "cli_group_boundary", policy, operation)
    if operation == "fork-only":
        assert receipt["succeeded"] and not receipt["group_changed"]
    elif policy == "plain" or (policy == "filtered" and operation.startswith("spawn-")):
        assert receipt["succeeded"] and receipt["group_changed"]
    else:
        assert not receipt["succeeded"] and receipt["errno"] == errno.EPERM
        if not operation.startswith("spawn-"):
            assert not receipt["group_changed"]


@pytest.mark.parametrize("policy", ["plain", "filtered", "filter-spawn", "restricted"])
def test_session_leader_eperm_is_not_independent_sandbox_evidence(binaries, policy):
    receipt = run(binaries / "cli_group_boundary", policy, "leader-setsid")
    assert not receipt["succeeded"] and receipt["errno"] == errno.EPERM


@pytest.mark.parametrize("iteration", range(5))
def test_child_and_grandchild_settle_before_reaping_pinned_leader(binaries, iteration):
    receipt = run(binaries / "cli_group_settlement")
    assert receipt["before_members"] == 3 and receipt["before_signal_zero"] == 0
    assert receipt["witness_closed"] and receipt["leader_still_held"]
    assert receipt["singleton_proved"] and receipt["after_members"] == 1
    # macOS reports EPERM for this zombie-only group. This is observed behavior,
    # never a production settlement rule: EPERM can also mean denied inspection.
    assert receipt["after_signal_zero"] == -1 and receipt["after_signal_errno"] == errno.EPERM


@pytest.mark.parametrize("iteration", range(5))
def test_concurrent_fork_during_group_cleanup_is_followed_until_singleton(binaries, iteration):
    receipt = run(binaries / "cli_group_settlement", "stress")
    assert receipt["before_members"] >= 3
    assert receipt["witness_closed"] and receipt["leader_still_held"]
    assert receipt["singleton_proved"] and receipt["after_members"] == 1
    assert receipt["kill_rounds"] >= 1
