"""Retry monitoring privacy and failure cleanup checks without subprocesses."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import monitor_jev_sol_retry as monitor


def snapshot(cpu=0.):
    return {"external_models": 0, "generic_inference": 0, "compilers": 0,
        "owned_models": 0, "background_process_max_cpu": cpu,
        "background_peak_pid": 987, "background_peak_executable": "fixture-peak",
        "background_large_rss": 0}


def test_peak_identity_is_basename_and_argv_is_not_retained(monkeypatch):
    processes = ("99 1 S 0 100 /fixture/Python\n"
        "100 99 S 0 100 /fixture/monitor\n"
        "101 100 S 0 100 /fixture/Python\n"
        "102 101 S 35 100 /fixture/meecp-uzu-worker\n"
        "200 1 S 265.1 100 /private/fixture/HighCPU\n"
        "201 1 T 600 100 /fixture/llama-server\n")
    arguments = ("99 uv run benchmark_jev_sol_retry.py --model /fixture\n"
        "101 python benchmark_jev_sol_retry.py --model /fixture\n"
        "200 HighCPU --private-fixture-text\n")
    monkeypatch.setattr(monitor.os, "getpid", lambda: 100)
    monkeypatch.setattr(monitor.subprocess, "run", lambda command, **kwargs:
        SimpleNamespace(stdout=arguments if command[-1] == "pid=,args=" else processes))
    result = monitor.sample(SimpleNamespace(pid=101))
    assert result["background_peak_pid"] == 200
    assert result["background_peak_executable"] == "HighCPU"
    assert result["background_process_max_cpu"] == 265.1
    assert result["owned_models"] == 1
    assert result["external_models"] == result["generic_inference"] == 0
    assert "private-fixture-text" not in json.dumps(result)
    assert "/private/fixture" not in json.dumps(result)


@pytest.mark.parametrize("preflight", [True, False])
def test_cpu_guard_retains_private_identity_and_cleans_up(monkeypatch, tmp_path, capsys, preflight):
    output = tmp_path / "monitor-output"
    monkeypatch.setattr(sys, "argv", ["monitor", "--output", str(output), "--harness",
        "benchmark_jev_sol_retry.py", "--", "--acceptance", "fixture"])
    monkeypatch.setattr(monitor.signal, "signal", lambda *args: None)
    samples = iter([snapshot(265.1), snapshot()] if preflight else
                   [snapshot(), snapshot(265.1), snapshot()])
    monkeypatch.setattr(monitor, "sample", lambda child=None: next(samples))

    class Child:
        pid = 123
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

        def kill(self):
            raise AssertionError("graceful cleanup should complete")

    child = Child()
    def launch(*args, **kwargs):
        assert not preflight, "preflight contention must prevent launch"
        return child
    monkeypatch.setattr(monitor.subprocess, "Popen", launch)
    assert monitor.main() != 0
    if not preflight:
        assert child.terminated
    private = json.loads((output / "HOST_SAMPLES.json").read_bytes())
    public = json.loads((output / "HOST_ISOLATION.json").read_bytes())
    stdout = json.loads(capsys.readouterr().out)
    assert any(s["background_peak_pid"] == 987 for s in private)
    assert all("background_peak_executable" in s for s in private)
    assert public["high_background_cpu_samples"] == 1
    assert public["background_cpu_limit_percent"] == 200
    assert public["failure_type"] is not None
    for exported in (public, stdout):
        encoded = json.dumps(exported)
        assert "background_peak_pid" not in encoded
        assert "background_peak_executable" not in encoded
        assert "fixture-peak" not in encoded
    assert (output / "HOST_SAMPLES.json").stat().st_mode & 0o077 == 0
    assert (output / "HOST_ISOLATION.json").stat().st_mode & 0o077 == 0
    assert output.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("stage", ["live_sample", "final_sample", "samples_write", "receipt_write"])
@pytest.mark.parametrize("force_kill", [False, True])
def test_sampling_and_receipt_failures_sanitize_and_cleanup(monkeypatch, tmp_path, capsys, stage, force_kill):
    output = tmp_path / "failure-output"
    monkeypatch.setattr(sys, "argv", ["monitor", "--output", str(output), "--harness",
        "benchmark_jev_sol_retry.py", "--", "--acceptance", "fixture"])
    monkeypatch.setattr(monitor.signal, "signal", lambda *args: None)
    calls = 0
    def sample(child=None):
        nonlocal calls
        calls += 1
        if (stage == "live_sample" and calls == 2) or (stage == "final_sample" and calls == 3):
            raise RuntimeError("private-fixture-must-never-appear")
        return snapshot(265.1 if calls == 2 else 0.)
    monkeypatch.setattr(monitor, "sample", sample)
    writer = monitor.private_json
    def write(path, value):
        if (stage == "samples_write" and path.name == "HOST_SAMPLES.json") or (
                stage == "receipt_write" and path.name == "HOST_ISOLATION.json"):
            raise OSError("private-fixture-must-never-appear")
        writer(path, value)
    monkeypatch.setattr(monitor, "private_json", write)

    class Child:
        pid = 123
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            if timeout == 30 and force_kill:
                raise monitor.subprocess.TimeoutExpired("fixture", 30)
            self.returncode = -9 if self.killed else -15
            return self.returncode

        def kill(self):
            self.killed = True

    child = Child()
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda *args, **kwargs: child)
    assert monitor.main() == 1
    assert child.terminated and child.returncode is not None
    assert child.killed is force_kill
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "private-fixture-must-never-appear" not in captured.out
    assert "background_peak_pid" not in captured.out
    assert "background_peak_executable" not in captured.out
    exported = json.loads(captured.out)
    if stage == "live_sample":
        assert exported["stage"] == "host_monitor_complete"
        assert exported["benchmark_exit"] == 1 and exported["failure_type"] is not None
        receipt = json.loads((output / "HOST_ISOLATION.json").read_bytes())
        assert "background_peak_pid" not in receipt and "background_peak_executable" not in receipt
    else:
        assert exported == {"stage": "host_monitor_failed", "error": "isolation_receipt_failed"}
        assert not (output / "HOST_ISOLATION.json").exists()
