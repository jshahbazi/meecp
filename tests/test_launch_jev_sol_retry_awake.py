"""Awake launcher checks: all process work and clocks are mocked."""
import ctypes
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import launch_jev_sol_retry_awake as launch


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real process work forbidden")
    for name in ("run", "Popen"):
        monkeypatch.setattr(launch.subprocess, name, forbidden)
    monkeypatch.setattr(launch.os, "kill", forbidden)
    monkeypatch.setattr(launch, "resolved_executable", forbidden)
    monkeypatch.setattr(launch.signal, "signal", lambda *args: None)
    monkeypatch.setattr(launch.time, "sleep", lambda *args: None)
    monkeypatch.setattr(launch, "continuous_ns", lambda: 100 * launch.NANOSECONDS)
    monkeypatch.setattr(launch.time, "monotonic_ns", lambda: 100 * launch.NANOSECONDS)
    monkeypatch.setattr(launch.os, "getuid", lambda: 501)


def test_native_clock_signatures_and_integer_conversion(monkeypatch):
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)
    def timebase(pointer):
        pointer._obj.numer, pointer._obj.denom = 4_000_000_001, 3
        return 0
    api = SimpleNamespace(mach_continuous_time=Function(lambda: 2**64 - 1),
                          mach_timebase_info=Function(timebase))
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    monkeypatch.setattr(launch.ctypes, "CDLL", lambda *args, **kwargs: api)
    clock = launch.ContinuousClock()
    assert clock.now_ns() == (2**64 - 1) * 4_000_000_001 // 3
    assert api.mach_continuous_time.restype is ctypes.c_uint64
    assert api.mach_timebase_info.argtypes == [ctypes.POINTER(launch.Timebase)]
    assert launch.Timebase._fields_ == [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


@pytest.mark.parametrize("gap,valid", [(0, True), (2_000_000_000, True),
                                     (2_000_000_001, False), (-2_000_000_001, False)])
def test_sleep_gap_predeclared_threshold(monkeypatch, gap, valid):
    continuous, active = [0], [0]
    monkeypatch.setattr(launch, "continuous_ns", lambda: continuous[0])
    monkeypatch.setattr(launch.time, "monotonic_ns", lambda: active[0])
    window = launch.ClockWindow()
    continuous[0], active[0] = 10_000_000_000 + gap, 10_000_000_000
    receipt = window.receipt()
    assert receipt["clock_continuity_passed"] is valid
    assert receipt["continuous_minus_active_ns"] == gap


@pytest.mark.parametrize("readable", [False, True])
def test_receive_rechecks_sleep_inclusive_deadline_after_wake(monkeypatch, readable):
    clock = [100 * launch.NANOSECONDS]
    monkeypatch.setattr(launch, "continuous_ns", lambda: clock[0])
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def register(self, *args):
            pass
        def select(self, timeout):
            assert timeout <= 1
            clock[0] += 1500 * launch.NANOSECONDS  # Active clock stays unchanged.
            return [("readable", 1)] if readable else []
    monkeypatch.setattr(launch.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(launch.os, "read", lambda *args: pytest.fail("late message read"))
    assert launch.receive_event(object(), 1290 * launch.NANOSECONDS) == ("deadline", None)


def test_receive_poll_timeout_continues_until_message(monkeypatch):
    polls = []
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def register(self, *args):
            pass
        def select(self, timeout):
            polls.append(timeout)
            return [("readable", 1)] if len(polls) == 3 else []
    monkeypatch.setattr(launch.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(launch.os, "read", lambda *args: b'{"operation":"release"}\n')
    assert launch.receive_event(SimpleNamespace(fileno=lambda: 1), 200 * launch.NANOSECONDS) \
        == ("message", {"operation": "release"})
    assert polls == [1, 1, 1]


@pytest.fixture
def target(monkeypatch):
    expected = {"pid": 123, "uid": 501, "starttime": "Sun Oct 11 12:34:56 2026",
                "executable": "/Applications" + launch.RENDERER_SUFFIX}
    current = {**expected, "state": "S", "age_seconds": 120}
    monkeypatch.setattr(launch, "observe", lambda pid: dict(current))
    return expected, current


@pytest.mark.parametrize("event", ["message", "disconnect", "deadline"])
def test_watchdog_records_actual_restore_trigger(target, tmp_path, monkeypatch, event):
    expected, current = target
    tmp_path.chmod(0o700)
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "deadline_ns": 1290 * launch.NANOSECONDS,
                      "receipt": str(tmp_path / "WATCHDOG.json")}, {"operation": "stop"}])
    monkeypatch.setattr(launch, "receive", lambda *args: next(messages))
    monkeypatch.setattr(launch, "receive_event", lambda *args:
        (event, {"operation": "release"} if event == "message" else None))
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    signals = []
    def send(pid, sig):
        signals.append(sig)
        current["state"] = "T" if sig == signal.SIGSTOP else "S"
    monkeypatch.setattr(launch.os, "kill", send)
    assert launch.watchdog() == 0
    assert signals == [signal.SIGSTOP, signal.SIGCONT]
    receipt = json.loads((tmp_path / "WATCHDOG.json").read_text())
    assert receipt["reason"] == ("release" if event == "message" else event)
    assert receipt["clock_continuity_passed"] is True


@pytest.mark.parametrize("phase", ["arm", "identity_check"])
def test_watchdog_never_stops_after_sleep_inclusive_deadline(target, tmp_path, monkeypatch, phase):
    expected, current = target
    tmp_path.chmod(0o700)
    clock, reads = [100 * launch.NANOSECONDS], [0]
    monkeypatch.setattr(launch, "continuous_ns", lambda: clock[0])
    def observe(pid):
        reads[0] += 1
        if phase == "identity_check" and reads[0] == 2:
            clock[0] = 1500 * launch.NANOSECONDS
        return dict(current)
    monkeypatch.setattr(launch, "observe", observe)
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "deadline_ns": 1290 * launch.NANOSECONDS,
                      "receipt": str(tmp_path / "WATCHDOG.json")}, {"operation": "stop"}])
    def receive(*args):
        message = next(messages)
        if message == {"operation": "stop"} and phase == "arm":
            clock[0] = 1500 * launch.NANOSECONDS
        return message
    monkeypatch.setattr(launch, "receive", receive)
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    assert launch.watchdog() == 1
    assert json.loads((tmp_path / "WATCHDOG.json").read_text())["stop_attempted"] is False


@pytest.mark.parametrize("failure", ["interrupt", "sleep_gap"])
def test_watchdog_restores_after_interrupt_or_sleep_gap(target, tmp_path, monkeypatch, failure):
    expected, current = target
    tmp_path.chmod(0o700)
    clock = [100 * launch.NANOSECONDS]
    monkeypatch.setattr(launch, "continuous_ns", lambda: clock[0])
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "deadline_ns": 1290 * launch.NANOSECONDS,
                      "receipt": str(tmp_path / "WATCHDOG.json")}, {"operation": "stop"}])
    monkeypatch.setattr(launch, "receive", lambda *args: next(messages))
    def final_event(*args):
        if failure == "interrupt":
            raise KeyboardInterrupt
        clock[0] += 3 * launch.NANOSECONDS
        return "message", {"operation": "release"}
    monkeypatch.setattr(launch, "receive_event", final_event)
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    sent = []
    def send(pid, sig):
        sent.append(sig)
        current["state"] = "T" if sig == signal.SIGSTOP else "S"
    monkeypatch.setattr(launch.os, "kill", send)
    assert launch.watchdog() == 1
    assert sent == [signal.SIGSTOP, signal.SIGCONT]
    result = json.loads((tmp_path / "WATCHDOG.json").read_text())
    assert result["reason"] == ("watchdog_failed_or_interrupted" if failure == "interrupt" else "release")
    assert result["clock_continuity_passed"] is (failure != "sleep_gap")


@pytest.mark.parametrize("field,value", [("pid", 124), ("uid", 502), ("starttime", "reused"),
                                       ("executable", "/different/path")])
def test_resume_rejects_identity_change(target, field, value):
    expected, current = target
    current.update(state="T", **{field: value})
    assert launch.resume_target(expected) == "identity_unavailable"


def test_private_receipt_atomic_create_only(tmp_path):
    path = tmp_path / "RECEIPT.json"
    launch.private_json(path, {"value": 1})
    with pytest.raises(FileExistsError):
        launch.private_json(path, {"value": 2})
    assert json.loads(path.read_bytes()) == {"value": 1}
    assert path.stat().st_mode & 0o077 == 0
    assert list(tmp_path.iterdir()) == [path]


@pytest.fixture
def args(target, tmp_path, monkeypatch):
    expected, _ = target
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    (tmp_path / "test-artifacts").mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    public = tmp_path / "experiments/uzu-rust/jev-scoring"
    public.mkdir(parents=True)
    monkeypatch.setattr(launch, "__file__", str(scripts / "launch_jev_sol_retry_awake.py"))
    monkeypatch.setattr(launch, "PUBLIC", public)
    monkeypatch.setattr(launch, "MONITOR", scripts / "monitor_jev_sol_retry.py")
    monkeypatch.setattr(launch, "HARNESS", scripts / "benchmark_jev_sol_retry.py")
    for path in (Path(launch.__file__), scripts / "launch_jev_sol_retry.py", launch.MONITOR,
                 launch.HARNESS, public / "SOL_BF16_PROTOCOL.md", public / "SOL_BF16_RETRY_PROTOCOL.md",
                 public / "SOL_VALIDATION_RESULTS.json", public / "SOL_VALIDATION_AUDIT.json"):
        path.write_text("immutable fixture")
    return SimpleNamespace(output=tmp_path / "test-artifacts/fresh", max_seconds=1200,
        suspend_renderer=True, renderer_pid=expected["pid"], expected_starttime=expected["starttime"],
        expected_executable=expected["executable"], dry_run=False)


class Child:
    def __init__(self, name, events, exit_status=0):
        self.name, self.events, self.exit_status = name, events, exit_status
        self.pid, self.returncode = 456, None
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
    def poll(self):
        if self.name == "monitor":
            self.returncode = self.exit_status
        return self.returncode
    def wait(self, timeout):
        self.events.append((self.name, "wait", timeout))
        self.returncode = self.exit_status
        return self.returncode
    def terminate(self):
        self.events.append((self.name, "terminate"))
    def kill(self):
        self.events.append((self.name, "kill"))


def mock_children(monkeypatch, *, fail_caffeinate=False):
    events, children = [], []
    def popen(command, **kwargs):
        name = "caffeinate" if command[0] == "/usr/bin/caffeinate" else \
            "watchdog" if "--watchdog" in command else "monitor"
        events.append((name, "spawn", command, kwargs))
        if name == "caffeinate" and fail_caffeinate:
            raise OSError("private launch error")
        child = Child(name, events)
        children.append(child)
        return child
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    messages = iter([{"stage": "watchdog_ready"}, {"stage": "renderer_stop_observed"}])
    monkeypatch.setattr(launch, "receive", lambda *args: next(messages))
    monkeypatch.setattr(launch, "notify", lambda stream, value: events.append(("notify", value)))
    return events, children


def test_launcher_owns_idle_assertion_and_separates_power_acceptance(args, monkeypatch):
    events, children = mock_children(monkeypatch)
    assert launch.launch(args) == 0
    command = events[0][2]
    assert command == ["/usr/bin/caffeinate", "-i", "-w", str(launch.os.getpid())]
    assert children[0].returncode == 0
    assert ("caffeinate", "terminate") in events
    sidecar = args.output.with_name("fresh-launcher")
    power = json.loads((sidecar / "POWER.json").read_text())
    launcher = json.loads((sidecar / "LAUNCHER.json").read_text())
    assert power["power_status"] == "completed" and power["performance_window_eligible"] is True
    assert power["native_probability_acceptance_evaluated_here"] is False
    assert launcher["monitor_exit"] == launcher["launcher_exit"] == 0
    assert "scripts/launch_jev_sol_retry.py" in launcher["source_hashes"]
    assert sidecar.stat().st_mode & 0o077 == 0


def test_keepawake_launch_failure_never_stops_or_launches_monitor(args, monkeypatch):
    events, children = mock_children(monkeypatch, fail_caffeinate=True)
    with pytest.raises(OSError):
        launch.launch(args)
    assert children == [] and len(events) == 1
    sidecar = args.output.with_name("fresh-launcher")
    receipt = json.loads((sidecar / "POWER.json").read_text())
    assert receipt["power_status"] == "excluded" and receipt["keepawake_cleanup"] == "not_started"
    assert "private launch error" not in (sidecar / "LAUNCHER.json").read_text()


def test_launcher_excludes_suspend_gap_even_when_monitor_passed(args, monkeypatch):
    events, children = mock_children(monkeypatch)
    clock = [100 * launch.NANOSECONDS]
    monkeypatch.setattr(launch, "continuous_ns", lambda: clock[0])
    def wait(*args):
        clock[0] += 3 * launch.NANOSECONDS
        children[-1].returncode = 0
        return 0
    monkeypatch.setattr(launch, "wait_monitor", wait)
    assert launch.launch(args) == 1
    sidecar = args.output.with_name("fresh-launcher")
    assert json.loads((sidecar / "POWER.json").read_text())["performance_window_eligible"] is False
    receipt = json.loads((sidecar / "LAUNCHER.json").read_text())
    assert receipt["monitor_exit"] == 0 and receipt["launcher_exit"] == 1
    assert ("notify", {"operation": "release"}) in events


def test_monitor_deadline_includes_sleep_and_preserves_cleanup(args, monkeypatch):
    events, children = mock_children(monkeypatch)
    clock = [100 * launch.NANOSECONDS]
    monkeypatch.setattr(launch, "continuous_ns", lambda: clock[0])
    def sleep(duration):
        assert duration <= 1
        clock[0] += 1300 * launch.NANOSECONDS
    monkeypatch.setattr(launch.time, "sleep", sleep)
    old_poll = Child.poll
    monkeypatch.setattr(Child, "poll", lambda self: None if self.name == "monitor" else old_poll(self))
    with pytest.raises(launch.LauncherError, match="launcher_deadline_expired"):
        launch.launch(args)
    release = events.index(("notify", {"operation": "release"}))
    terminate = events.index(("monitor", "terminate"))
    assert release < terminate
    assert children[1].stdin.closed
    assert ("caffeinate", "terminate") in events


def test_completed_monitor_poll_cannot_cross_continuous_deadline(monkeypatch):
    clock = [100 * launch.NANOSECONDS]
    monkeypatch.setattr(launch, "continuous_ns", lambda: clock[0])
    class Monitor:
        def poll(self):
            clock[0] = 1300 * launch.NANOSECONDS
            return 0
    keepawake = SimpleNamespace(poll=lambda: None)
    with pytest.raises(launch.LauncherError, match="launcher_deadline_expired"):
        launch.wait_monitor(Monitor(), keepawake, 1290 * launch.NANOSECONDS)


def test_keepawake_failure_stops_owned_monitor(args, monkeypatch):
    events, children = mock_children(monkeypatch)
    old_poll = Child.poll
    def poll(self):
        if self.name == "caffeinate" and len(children) == 3:
            self.returncode = 1
        return None if self.name == "monitor" else old_poll(self)
    monkeypatch.setattr(Child, "poll", poll)
    with pytest.raises(launch.LauncherError, match="keepawake_exited"):
        launch.launch(args)
    assert ("monitor", "terminate") in events
    assert children[1].stdin.closed


def test_launcher_interrupt_restores_before_owned_cleanup(args, monkeypatch):
    events, children = mock_children(monkeypatch)
    old_poll = Child.poll
    monkeypatch.setattr(Child, "poll", lambda self: None if self.name == "monitor" else old_poll(self))
    def interrupted_wait(*args):
        launch.interrupted(signal.SIGTERM, None)
    monkeypatch.setattr(launch, "wait_monitor", interrupted_wait)
    with pytest.raises(launch.LauncherError, match="interrupted"):
        launch.launch(args)
    assert events.index(("notify", {"operation": "release"})) < events.index(("monitor", "terminate"))
    assert ("caffeinate", "terminate") in events
    result = json.loads(args.output.with_name("fresh-launcher").joinpath("LAUNCHER.json").read_text())
    assert result["completion_reason"] == "interrupted" and result["launcher_exit"] == 1


def test_late_interrupt_after_successful_monitor_never_records_success(args, monkeypatch):
    events, children = mock_children(monkeypatch)
    real_fdopen = launch.os.fdopen
    class InterruptedClose:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self.stream
        def __exit__(self, *args):
            self.stream.close()
            launch.interrupted(signal.SIGINT, None)
    calls = [0]
    def fdopen(fd, mode):
        stream = real_fdopen(fd, mode)
        calls[0] += 1
        return InterruptedClose(stream) if calls[0] == 1 else stream
    monkeypatch.setattr(launch.os, "fdopen", fdopen)
    with pytest.raises(launch.LauncherError, match="interrupted"):
        launch.launch(args)
    sidecar = args.output.with_name("fresh-launcher")
    launcher = json.loads((sidecar / "LAUNCHER.json").read_text())
    power = json.loads((sidecar / "POWER.json").read_text())
    assert launcher["monitor_exit"] == 0 and launcher["launcher_exit"] == 1
    assert power["performance_window_eligible"] is False
    assert children[1].stdin.closed and ("caffeinate", "terminate") in events


def test_caffeinate_cleanup_escalates_only_owned_child():
    events = []
    child = Child("caffeinate", events)
    attempts = [0]
    def wait(timeout):
        attempts[0] += 1
        if attempts[0] == 1:
            raise subprocess.TimeoutExpired("caffeinate", timeout)
        child.returncode = -9
    child.wait = wait
    assert launch.cleanup_keepawake(child) == "owned_child_killed_and_reaped"
    assert events == [("caffeinate", "terminate"), ("caffeinate", "kill")]


def test_dry_run_is_process_and_file_free(args, capsys):
    args.dry_run = True
    assert launch.launch(args) == 0
    assert json.loads(capsys.readouterr().out)["signals_sent"] == 0
    assert not args.output.with_name("fresh-launcher").exists()


@pytest.mark.parametrize("bound", [0, 10, 1201, 1200.0, True])
def test_invalid_maximum_rejected(args, bound):
    args.max_seconds = bound
    with pytest.raises(launch.LauncherError, match="platform_or_bound_invalid"):
        launch.launch(args)


def test_frozen_monitor_invocation():
    command = launch.monitor_command(Path("/private/fresh"))
    assert command[1] == str(launch.MONITOR)
    assert command[command.index("--harness") + 1] == "benchmark_jev_sol_retry.py"
    assert command[command.index("--acceptance") + 1].endswith("SOL_VALIDATION_RESULTS.json")
    assert command[command.index("--acceptance-audit") + 1].endswith("SOL_VALIDATION_AUDIT.json")
    assert "--n-probs" not in command and "200" not in command
