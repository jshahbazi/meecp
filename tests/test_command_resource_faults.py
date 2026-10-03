"""Pinned helper fault variants exercise fail-closed native control paths."""
from pathlib import Path
import subprocess

import pytest

from mcpme.command_resource_execution_v1 import CommandExecutionError, read_resource_completion
from mcpme.command_resource_policy_v1 import ResourcePolicyV1
from test_command_resource_v1 import ROOT, binaries, executor, spec, pytestmark


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,reason", [("metric", "metric_unavailable"), ("identity", "identity_changed"),
    ("membership", "membership_changed"), ("overflow", "arithmetic_overflow"),
    ("identity_capacity", "identity_capacity_exceeded"), ("enumeration_full", "process_threshold")])
async def test_native_sampler_errors_latch_failure_and_settle_exact_tree(binaries, tmp_path, fault, reason):
    header = (ROOT / "scripts/mcpme-command-resource-v1.h").read_text()
    replacements = {
        "metric": ("proc_pid_rusage(pid, RUSAGE_INFO_V1, (rusage_info_t *)&first)", "(errno = EIO, -1)"),
        "identity": ("    if (!held_exit && (before.pbi_pid", "    after.pbi_start_tvusec ^= 1;\n    if (!held_exit && (before.pbi_pid"),
        "membership": ("    if (after != count", "    if (after > 0) second[0] += 1;\n    if (after != count"),
        "overflow": ("    uint64_t ticks, cpu;", "    second.ri_user_time = UINT64_MAX; second.ri_system_time = 1;\n    uint64_t ticks, cpu;"),
        "identity_capacity": ("    uint64_t index = 0;", "    m->report.identities = m->policy.identities;\n    uint64_t index = 0;"),
        "enumeration_full": ("    int count = proc_listpgrppids(leader, members, sizeof(pid_t) * (RESOURCE_MEMBERS + 1));",
            "    int count = RESOURCE_MEMBERS + 1;\n    for (int i = 0; i < count; ++i) members[i] = leader + i;"),
    }
    before, after = replacements[fault]
    assert header.count(before) == 1
    (tmp_path / "mcpme-command-resource-v1.h").write_text(header.replace(before, after))
    source = tmp_path / "helper.c"
    source.write_bytes((ROOT / "scripts/mcpme-command-resource-reap.c").read_bytes())
    helper = tmp_path / "helper"
    compiled = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(source), "-o", str(helper)],
        capture_output=True, timeout=30)
    assert compiled.returncode == 0
    pair = (helper, binaries[1])
    owner = executor(pair, tmp_path / "state")
    try:
        result = await owner.execute(spec(pair, "echo"))
        assert result.report.reason == reason and not result.report.sample_windows_valid
        assert not result.result_admissible and owner.reconcile()["pending"] == 0
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,reason", [(ResourcePolicyV1(interval_ns=1_000_000, sample_gap_ns=1_000_000), "sample_gap_exceeded"),
    (ResourcePolicyV1(sample_capacity=1), "sample_capacity_exceeded")])
async def test_actual_sampler_gap_and_pass_budget_cancel(binaries, tmp_path, policy, reason):
    owner = executor(binaries, tmp_path / "state")
    try:
        result = await owner.execute(spec(binaries, "echo", policy))
        assert result.report.reason == reason and not result.report.sample_windows_valid
        assert result.report.samples == 1 and owner.reconcile()["pending"] == 0
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_native_unknown_policy_fails_before_source_execution(binaries, tmp_path, monkeypatch):
    import mcpme.command_resource_execution_v1 as module
    owner = executor(binaries, tmp_path / "state")
    monkeypatch.setattr(module, "POLICY", "unsupported-policy")
    try:
        with pytest.raises(CommandExecutionError):
            await owner.execute(spec(binaries, "echo"))
        assert owner.reconcile()["pending"] == 0
        valid, code, report = read_resource_completion(owner.state, owner.journal["entries"][0])
        assert valid and code == 0 and report.samples == 0 and not report.sample_windows_valid
        assert not report.result_admissible
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_leader_exit_between_waitid_and_live_usage_read(binaries, tmp_path, fallback):
    header = (ROOT / "scripts/mcpme-command-resource-v1.h").read_text()
    line = "        int result = resource_observe(m, first[i], leader, leader_exited && first[i] == leader, &footprint);"
    assert header.count(line) == 1
    # After a real first sample and source release, retain the outer live
    # observation while the actual original child exits. Never synthesize
    # cached counters, PID identity, or native exit status.
    barrier = '''        if (m->report.samples && first[i] == leader && !leader_exited) {
            siginfo_t proof = {0};
            for (int n = 0; n < 60 && proof.si_pid != leader; ++n) {
                if (waitid(P_PID, (id_t)leader, &proof, WEXITED | WNOHANG | WNOWAIT)) return resource_fail(m, RR_METRIC);
                usleep(1000);
            }
            if (proof.si_pid != leader) return resource_fail(m, RR_METRIC);
        }
'''
    header = header.replace(line, barrier + line)
    if not fallback:
        condition = "if ((result == RR_METRIC || result == RR_IDENTITY) && first[i] == leader && !leader_exited)"
        assert header.count(condition) == 1
        header = header.replace(condition, "if (0)")
    (tmp_path / "mcpme-command-resource-v1.h").write_text(header)
    source = tmp_path / "helper.c"
    source.write_bytes((ROOT / "scripts/mcpme-command-resource-reap.c").read_bytes())
    helper = tmp_path / "helper"
    compiled = subprocess.run(["clang", "-Wall", "-Wextra", "-Werror", "-O2", str(source), "-o", str(helper)],
        capture_output=True, timeout=30)
    assert compiled.returncode == 0
    pair = helper, binaries[1]
    owner = executor(pair, tmp_path / "state")
    try:
        policy = ResourcePolicyV1(read_window_ns=100_000_000, sample_gap_ns=1_000_000_000)
        result = await owner.execute(spec(pair, "echo", policy))
        assert result.report.reason == ("exited" if fallback else "metric_unavailable")
        assert result.report.sample_windows_valid is fallback
        assert not result.result_admissible
        assert owner.reconcile()["pending"] == 0
    finally:
        await owner.close()
