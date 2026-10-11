"""Renderer launcher checks; every process observation/signal/spawn is mocked."""
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import launch_jev_sol_retry as launch


@pytest.fixture(autouse=True)
def prohibit_real_process_work(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real process work forbidden")
    monkeypatch.setattr(launch.subprocess, "run", forbidden)
    monkeypatch.setattr(launch.subprocess, "Popen", forbidden)
    monkeypatch.setattr(launch.os, "kill", forbidden)
    monkeypatch.setattr(launch, "resolved_executable", forbidden)
    monkeypatch.setattr(launch.signal, "signal", lambda *args: None)
    monkeypatch.setattr(launch.time, "sleep", lambda *args: None)
    monkeypatch.setattr(launch.time, "monotonic", lambda: 100.)
    monkeypatch.setattr(launch.os, "getuid", lambda: 501)


@pytest.fixture
def target(monkeypatch):
    expected = {"pid": 123, "uid": 501, "starttime": "Sun Oct 11 12:34:56 2026",
                "executable": "/Applications" + launch.RENDERER_SUFFIX}
    current = {**expected, "state": "S", "age_seconds": 120}
    monkeypatch.setattr(launch, "observe", lambda pid: dict(current))
    return expected, current


@pytest.mark.parametrize("text,result", [("01:02", 62), ("00:01:02", 62), ("2-01:02:03", 176523)])
def test_elapsed_time_parsing(text, result):
    assert launch.elapsed_seconds(text) == result


@pytest.mark.parametrize("text", ["", "2", "x:y", "00:61", "00:61:00"])
def test_elapsed_time_invalid(text):
    with pytest.raises(launch.LauncherError):
        launch.elapsed_seconds(text)


def test_private_ps_observation_checks_role_and_identity(monkeypatch):
    executable = "/Applications" + launch.RENDERER_SUFFIX
    metadata = f"123 501 Sun Oct 11 12:34:56 2026 S 02:00 {executable}"
    responses = iter([metadata, executable + " --type=renderer --private=never-record", metadata])
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=next(responses))
    monkeypatch.setattr(launch.subprocess, "run", run)
    monkeypatch.setattr(launch, "resolved_executable", lambda pid: executable)
    result = launch.observe(123)
    assert result["executable"] == executable and result["age_seconds"] == 120
    assert "private" not in json.dumps(result) and "arguments" not in result
    assert len(calls) == 3 and all(c[1]["timeout"] == 2 for c in calls)


def test_non_renderer_and_mid_observation_reuse_rejected(monkeypatch):
    executable = "/Applications" + launch.RENDERER_SUFFIX
    first = f"123 501 Sun Oct 11 12:34:56 2026 S 02:00 {executable}"
    second = first.replace("12:34:56", "12:36:56")
    responses = iter([first, executable + " --type=renderer", second])
    monkeypatch.setattr(launch, "ps", lambda *args: next(responses))
    monkeypatch.setattr(launch, "resolved_executable", lambda pid: executable)
    with pytest.raises(launch.LauncherError, match="identity_changed"):
        launch.observe(123)
    responses = iter([first, executable + " --type=gpu-process", first])
    with pytest.raises(launch.LauncherError, match="renderer_role_mismatch"):
        launch.observe(123)


@pytest.mark.parametrize("field,value", [("pid", 124), ("uid", 502), ("starttime", "changed"),
                                       ("executable", "/different/path")])
def test_resume_never_signals_changed_identity(target, field, value):
    expected, current = target
    current.update(state="T", **{field: value})
    assert launch.resume_target(expected) == "identity_unavailable"


def test_resume_never_signals_missing_or_already_running_target(target, monkeypatch):
    expected, _ = target
    assert launch.resume_target(expected) == "already_running"
    monkeypatch.setattr(launch, "observe", lambda pid: None)
    assert launch.resume_target(expected) == "identity_unavailable"


@pytest.mark.parametrize("field,value", [("age_seconds", 59), ("state", "T"), ("state", "Z")])
def test_initial_target_rejects_young_stopped_or_zombie(target, field, value):
    expected, current = target
    current[field] = value
    with pytest.raises(launch.LauncherError, match="target_not_eligible"):
        launch.validate_target(expected, initial=True)


@pytest.mark.parametrize("last_message", [{"operation": "release"}, None])
def test_detached_watchdog_resumes_on_release_or_eof_or_deadline(target, tmp_path, monkeypatch, last_message):
    expected, current = target
    tmp_path.chmod(0o700)
    receipt = tmp_path / "WATCHDOG.json"
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "receipt": str(receipt)}, {"operation": "stop"}, last_message])
    monkeypatch.setattr(launch, "receive", lambda *args: next(messages))
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    sent = []
    def send(pid, sig):
        sent.append((pid, sig))
        current["state"] = "T" if sig == signal.SIGSTOP else "S"
    monkeypatch.setattr(launch.os, "kill", send)
    assert launch.watchdog() == 0
    assert sent == [(123, signal.SIGSTOP), (123, signal.SIGCONT)]
    result = json.loads(receipt.read_bytes())
    assert result["resume_status"] == "resume_signal_sent" and receipt.stat().st_mode & 0o077 == 0
    assert "executable" not in result and "starttime" not in result


def test_watchdog_rechecks_before_stop_and_does_not_resume_reused_pid(target, tmp_path, monkeypatch):
    expected, current = target
    tmp_path.chmod(0o700)
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "receipt": str(tmp_path / "WATCHDOG.json")}, {"operation": "stop"}, None])
    def receive(*args):
        message = next(messages)
        if message is None:
            current["starttime"] = "reused PID"
        return message
    monkeypatch.setattr(launch, "receive", receive)
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    sent = []
    def send(pid, sig):
        sent.append(sig)
        current["state"] = "T"
    monkeypatch.setattr(launch.os, "kill", send)
    assert launch.watchdog() == 1
    assert sent == [signal.SIGSTOP]


def test_watchdog_interruption_after_stop_still_attempts_resume(target, tmp_path, monkeypatch):
    expected, current = target
    tmp_path.chmod(0o700)
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "receipt": str(tmp_path / "WATCHDOG.json")}, {"operation": "stop"}])
    def receive(*args):
        try:
            return next(messages)
        except StopIteration:
            raise KeyboardInterrupt
    monkeypatch.setattr(launch, "receive", receive)
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    sent = []
    def send(pid, sig):
        sent.append(sig)
        current["state"] = "T" if sig == signal.SIGSTOP else "S"
    monkeypatch.setattr(launch.os, "kill", send)
    assert launch.watchdog() == 1
    assert sent == [signal.SIGSTOP, signal.SIGCONT]


@pytest.mark.parametrize("expired_at", ["arm", "identity_check"])
def test_watchdog_never_stops_after_monotonic_deadline(target, tmp_path, monkeypatch, expired_at):
    expected, current = target
    tmp_path.chmod(0o700)
    clock, observed = [100.], [0]
    monkeypatch.setattr(launch.time, "monotonic", lambda: clock[0])
    def observe(pid):
        observed[0] += 1
        if expired_at == "identity_check" and observed[0] == 2:
            clock[0] = 1290.
        return dict(current)
    monkeypatch.setattr(launch, "observe", observe)
    messages = iter([{"identity": expected, "suspend_renderer": True, "max_seconds": 1200,
                      "receipt": str(tmp_path / "WATCHDOG.json")}, {"operation": "stop"}])
    def receive(stream, deadline):
        message = next(messages)
        if message == {"operation": "stop"}:
            assert deadline == 1290.  # Ten seconds reserved from the 1200-second budget.
            if expired_at == "arm":
                clock[0] = deadline
        return message
    monkeypatch.setattr(launch, "receive", receive)
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    assert launch.watchdog() == 1  # Autouse kill mock rejects any real or attempted signal.
    receipt = json.loads((tmp_path / "WATCHDOG.json").read_bytes())
    assert receipt["stop_attempted"] is False and receipt["resume_status"] == "not_stopped"


def test_receive_does_not_accept_buffered_control_message_after_deadline(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(launch.time, "monotonic", lambda: clock[0])
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def register(self, *args):
            pass
        def select(self, timeout):
            assert timeout == 1.
            clock[0] = 101.
            return [("readable", 1)]
    monkeypatch.setattr(launch.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(launch.os, "read", lambda *args: pytest.fail("late message must not be read"))
    assert launch.receive(object(), 101.) is None


def test_native_path_identity_ignores_rewritten_ps_comm(monkeypatch):
    executable = ("/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/"
        "Versions/155.0.8059.40/Helpers/Codex (Renderer).app/Contents/MacOS/Codex (Renderer)")
    monkeypatch.setattr(launch, "ps", lambda *args: "123 501 Sun Oct 11 12:34:56 2026 S 02:00 Codex (Renderer)")
    monkeypatch.setattr(launch, "resolved_executable", lambda pid: executable)
    assert launch.metadata(123)["executable"] == executable
    expected = launch.identity(launch.metadata(123))
    monkeypatch.setattr(launch, "observe", lambda pid: launch.metadata(pid))
    assert launch.validate_target(expected, initial=True)["executable"] == executable


@pytest.fixture
def args(target, tmp_path, monkeypatch):
    expected, _ = target
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    (tmp_path / "test-artifacts").mkdir()
    return SimpleNamespace(output=tmp_path / "test-artifacts/fresh", max_seconds=1200,
        suspend_renderer=True, renderer_pid=expected["pid"], expected_starttime=expected["starttime"],
        expected_executable=expected["executable"], dry_run=True)


def test_dry_run_verifies_without_any_process_spawn_signal_or_file_creation(args, capsys):
    assert launch.launch(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["identity_verified"] is True and result["signals_sent"] == 0
    assert not args.output.exists() and not args.output.with_name("fresh-launcher").exists()


@pytest.mark.parametrize("bound", [0, -1, 10, 1201, 1200.0, True])
def test_invalid_bound_rejected_before_process_work(args, bound):
    args.max_seconds = bound
    with pytest.raises(launch.LauncherError, match="platform_or_bound_invalid"):
        launch.launch(args)


def test_opt_in_and_fresh_output_are_required(args):
    args.suspend_renderer = False
    with pytest.raises(launch.LauncherError, match="explicit_suspension_identity_required"):
        launch.launch(args)
    args.suspend_renderer = True
    args.output.mkdir()
    with pytest.raises(launch.LauncherError, match="fresh_output_required"):
        launch.launch(args)


def test_original_monitor_and_acceptance_command_unchanged():
    command = launch.monitor_command(Path("/private/fresh"))
    assert command[1] == str(launch.MONITOR)
    assert command[command.index("--harness") + 1] == "benchmark_jev_sol_retry.py"
    assert command[command.index("--acceptance") + 1].endswith("SOL_VALIDATION_RESULTS.json")
    assert command[command.index("--acceptance-audit") + 1].endswith("SOL_VALIDATION_AUDIT.json")
    assert "--n-probs" not in command and "200" not in command


@pytest.mark.parametrize("watchdog_exit", [0, 1])
def test_launcher_spawns_detached_watchdog_and_keeps_exit_codes_separate(args, monkeypatch, watchdog_exit):
    args.dry_run = False
    root = launch.ROOT
    scripts = root / "scripts"
    scripts.mkdir()
    public = root / "experiments/uzu-rust/jev-scoring"
    public.mkdir(parents=True)
    source = scripts / "launch_jev_sol_retry.py"
    source.write_text("fixture launcher")
    monkeypatch.setattr(launch, "__file__", str(source))
    monkeypatch.setattr(launch, "PUBLIC", public)
    monkeypatch.setattr(launch, "MONITOR", scripts / "monitor_jev_sol_retry.py")
    monkeypatch.setattr(launch, "HARNESS", scripts / "benchmark_jev_sol_retry.py")
    for path in (launch.MONITOR, launch.HARNESS, public / "SOL_BF16_PROTOCOL.md",
                 public / "SOL_BF16_RETRY_PROTOCOL.md", public / "SOL_VALIDATION_RESULTS.json",
                 public / "SOL_VALIDATION_AUDIT.json"):
        path.write_text("immutable fixture")
    events = []
    class Child:
        def __init__(self, name):
            self.name, self.returncode = name, None
            self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        def wait(self, timeout):
            events.append((self.name, "wait"))
            self.returncode = watchdog_exit if self.name == "watchdog" else 0
            return self.returncode
        def poll(self):
            return self.returncode
    children = []
    def popen(command, **kwargs):
        child = Child("watchdog" if "--watchdog" in command else "monitor")
        children.append((child, command, kwargs))
        return child
    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    messages = iter([{"stage": "watchdog_ready"}, {"stage": "renderer_stop_observed"}])
    monkeypatch.setattr(launch, "receive", lambda *args: next(messages))
    writes = []
    monkeypatch.setattr(launch, "notify", lambda stream, value: writes.append(value))
    assert launch.launch(args) == watchdog_exit
    assert children[0][2]["start_new_session"] is True and children[0][2]["close_fds"] is True
    assert children[1][2]["close_fds"] is True
    assert writes[-1] == {"operation": "release"} and children[0][0].stdin.closed
    assert events == [("monitor", "wait"), ("watchdog", "wait")]
    sidecar = args.output.with_name("fresh-launcher")
    assert (sidecar / "PROGRESS.jsonl").stat().st_mode & 0o077 == 0
    receipt = json.loads((sidecar / "LAUNCHER.json").read_bytes())
    assert receipt["bound_sources_unchanged"] is True
    assert receipt["monitor_exit"] == 0 and receipt["launcher_exit"] == watchdog_exit


def test_main_failure_is_fixed_and_content_free(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["launcher", "--output", "/irrelevant"])
    def fail(args):
        raise RuntimeError("private details")
    monkeypatch.setattr(launch, "launch", fail)
    assert launch.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"stage": "failed", "error": "bounded_retry_launcher_failed"}
    assert captured.err == ""
