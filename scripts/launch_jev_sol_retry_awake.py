"""Versioned awake-window renderer pause around the unchanged Sol retry monitor.

No renderer is signaled without explicit --suspend-renderer and exact supplied
identity. A detached watchdog resumes on control-pipe EOF or its deadline. PID
checks cannot atomically condition kill(2); the check-to-signal race remains.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "experiments/uzu-rust/jev-scoring"
MONITOR = ROOT / "scripts/monitor_jev_sol_retry.py"
HARNESS = ROOT / "scripts/benchmark_jev_sol_retry.py"
RENDERER_SUFFIX = "/Codex.app/Contents/Frameworks/Codex Helper (Renderer).app/Contents/MacOS/Codex Helper (Renderer)"
FRAMEWORK_RENDERER_PATTERN = (
    r"/Applications/ChatGPT\.app/Contents/Frameworks/Codex Framework\.framework/Versions/"
    r"[0-9]+(?:\.[0-9]+)*/Helpers/Codex \(Renderer\)\.app/Contents/MacOS/Codex \(Renderer\)")
MAX_SECONDS = 1200
RESTORATION_RESERVE_SECONDS = 10
NANOSECONDS = 1_000_000_000
MAX_SUSPEND_GAP_NS = 2 * NANOSECONDS
POLL_SECONDS = 1
CLOCK_IDENTITY = "mach_continuous_time/mach_timebase_info integer nanoseconds"
_clock = None


class Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class ContinuousClock:
    def __init__(self):
        require(sys.platform == "darwin", "continuous_clock_unavailable")
        self.api = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.api.mach_continuous_time.argtypes = []
        self.api.mach_continuous_time.restype = ctypes.c_uint64
        self.api.mach_timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        self.api.mach_timebase_info.restype = ctypes.c_int
        self.info = Timebase()
        require(self.api.mach_timebase_info(ctypes.byref(self.info)) == 0
                and self.info.numer > 0 and self.info.denom > 0, "continuous_timebase_invalid")

    def now_ns(self):
        # Multiply in Python integers before division: uint64 native ticks can
        # overflow in native arithmetic and lose nanoseconds through float.
        return int(self.api.mach_continuous_time()) * self.info.numer // self.info.denom


def continuous_ns():
    global _clock
    if _clock is None:
        _clock = ContinuousClock()
    return _clock.now_ns()


class ClockWindow:
    def __init__(self):
        self.continuous_start = continuous_ns()
        self.active_start = time.monotonic_ns()

    def receipt(self):
        continuous_elapsed = continuous_ns() - self.continuous_start
        active_elapsed = time.monotonic_ns() - self.active_start
        gap = continuous_elapsed - active_elapsed
        valid = continuous_elapsed >= 0 and active_elapsed >= 0 and abs(gap) <= MAX_SUSPEND_GAP_NS
        return {"clock_identity": CLOCK_IDENTITY, "active_clock_identity": "time.monotonic_ns",
                "continuous_elapsed_ns": continuous_elapsed, "active_elapsed_ns": active_elapsed,
                "continuous_minus_active_ns": gap, "max_suspend_gap_ns": MAX_SUSPEND_GAP_NS,
                "clock_status": "continuous" if valid else "discontinuity",
                "clock_continuity_passed": valid}


class LauncherError(ValueError):
    """Only fixed authored codes leave this module."""


def require(value, code):
    if not value:
        raise LauncherError(code)


def encoded(value):
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_json(path, value):
    path = Path(path)
    temporary = path.with_name(".receipt-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)  # Atomic create-only publication.
    finally:
        temporary.unlink()


def elapsed_seconds(text):
    match = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text)
    require(match is not None, "observation_invalid")
    days, hours, minutes, seconds = (int(n or 0) for n in match.groups())
    require(minutes < 60 and seconds < 60, "observation_invalid")
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def ps(pid, fields):
    result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", fields],
        capture_output=True, text=True, timeout=2, env={**os.environ, "LC_ALL": "C"})
    if result.returncode == 1 and not result.stdout.strip():
        return None
    require(result.returncode == 0, "observation_failed")
    return result.stdout.strip()


def metadata(pid):
    raw = ps(pid, "pid=,uid=,lstart=,state=,etime=,comm=")
    if raw is None:
        return None
    fields = raw.split(maxsplit=9)
    require(len(fields) == 10 and fields[0].isdigit() and fields[1].isdigit(), "observation_invalid")
    return {"pid": int(fields[0]), "uid": int(fields[1]), "starttime": " ".join(fields[2:7]),
        "state": fields[7], "age_seconds": elapsed_seconds(fields[8]), "executable": resolved_executable(pid)}


def resolved_executable(pid):
    # macOS SDK libproc.h: int proc_pidpath(int, void *, uint32_t).
    # ps comm can be changed by the renderer and is not executable identity.
    require(sys.platform == "darwin", "native_path_unavailable")
    api = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    api.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    api.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    count = api.proc_pidpath(pid, buffer, len(buffer))
    require(0 < count < len(buffer), "native_path_unavailable")
    path = os.fsdecode(buffer.value)
    require(Path(path).is_absolute(), "native_path_invalid")
    return path


def identity(value):
    return {key: value[key] for key in ("pid", "uid", "starttime", "executable")}


def observe(pid):
    before = metadata(pid)
    if before is None:
        return None
    arguments = ps(pid, "args=")  # Inspected privately, never retained in records.
    after = metadata(pid)
    if arguments is None or after is None:
        return None
    require(identity(before) == identity(after) and after["pid"] == pid,
            "identity_changed_during_observation")
    require(re.search(r"(?:^|\s)--type=renderer(?:\s|$)", arguments) is not None,
            "renderer_role_mismatch")
    return after


def validate_target(expected, *, initial=False):
    require(type(expected["pid"]) is int and expected["pid"] > 1
            and expected["uid"] == os.getuid()
            and Path(expected["executable"]).is_absolute()
            and (expected["executable"].endswith(RENDERER_SUFFIX)
                 or re.fullmatch(FRAMEWORK_RENDERER_PATTERN, expected["executable"]) is not None),
            "target_identity_invalid")
    current = observe(expected["pid"])
    require(current is not None and identity(current) == expected, "target_identity_mismatch")
    if initial:
        require(current["age_seconds"] >= 60 and "T" not in current["state"]
                and "Z" not in current["state"], "target_not_eligible")
    return current


def resume_target(expected):
    # Never issue SIGCONT when an observation shows exit/reuse/identity mismatch.
    try:
        current = validate_target(expected)
    except LauncherError:
        return "identity_unavailable"
    if "T" not in current["state"]:
        return "already_running"
    os.kill(expected["pid"], signal.SIGCONT)
    return "resume_signal_sent"


def interrupted(signum, frame):
    raise LauncherError("interrupted")


def receive_event(stream, deadline):
    """Return message, EOF, or deadline distinctly; never wait over one second."""
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - continuous_ns()
            if remaining <= 0:
                return "deadline", None
            ready = selector.select(min(POLL_SECONDS, remaining / NANOSECONDS))
            if continuous_ns() >= deadline:
                return "deadline", None
            if ready:
                raw = os.read(stream.fileno(), 4096)
                break
    if not raw:
        return "disconnect", None
    require(len(raw) < 4096 and raw.endswith(b"\n") and raw.count(b"\n") == 1, "control_message_invalid")
    return "message", json.loads(raw)


def receive(stream, deadline):
    return receive_event(stream, deadline)[1]


def notify(stream, value):
    stream.write(encoded(value))
    stream.flush()


def watchdog():
    """Detached process; launcher owns the only write end of its control pipe."""
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    expected, receipt_path, restore_needed = None, None, False
    status, reason = "not_stopped", "failed"
    window = ClockWindow()
    try:
        request = receive(sys.stdin.buffer, continuous_ns() + 5 * NANOSECONDS)
        require(type(request) is dict and request["suspend_renderer"] is True
                and type(request["max_seconds"]) is int
                and RESTORATION_RESERVE_SECONDS < request["max_seconds"] <= MAX_SECONDS, "watchdog_request_invalid")
        expected = request["identity"]
        receipt_path = Path(request["receipt"])
        require(receipt_path.is_absolute() and not receipt_path.exists()
                and receipt_path.parent.is_dir() and receipt_path.parent.stat().st_mode & 0o077 == 0
                and not any(p.is_symlink() for p in (receipt_path, *receipt_path.parents)), "watchdog_receipt_invalid")
        validate_target(expected, initial=True)
        deadline = request["deadline_ns"]
        require(type(deadline) is int and 0 < deadline - continuous_ns()
                <= (request["max_seconds"] - RESTORATION_RESERVE_SECONDS) * NANOSECONDS,
                "watchdog_deadline_invalid")
        notify(sys.stdout.buffer, {"stage": "watchdog_ready"})
        require(receive(sys.stdin.buffer, deadline) == {"operation": "stop"}, "watchdog_not_armed")
        require(continuous_ns() < deadline, "stop_deadline_expired")
        validate_target(expected, initial=True)  # Recheck immediately before STOP.
        require(continuous_ns() < deadline, "stop_deadline_expired")
        restore_needed = True
        os.kill(expected["pid"], signal.SIGSTOP)
        stopped_deadline = min(deadline, continuous_ns() + 2 * NANOSECONDS)
        while "T" not in validate_target(expected)["state"]:
            require(continuous_ns() < stopped_deadline, "stop_not_observed")
            time.sleep(.05)
        require(continuous_ns() < deadline, "stop_deadline_expired")
        notify(sys.stdout.buffer, {"stage": "renderer_stop_observed"})
        event, message = receive_event(sys.stdin.buffer, deadline)
        require(message is None or message == {"operation": "release"}, "watchdog_message_invalid")
        reason = "release" if event == "message" else event
    except BaseException:
        reason = "watchdog_failed_or_interrupted"
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if restore_needed:
            try:
                status = resume_target(expected)
            except BaseException:
                status = "resume_failed"
        if receipt_path is not None:
            try:
                clock_receipt = window.receipt()
                private_json(receipt_path, {"schema_version": 2, "reason": reason,
                    "resume_status": status, "stop_attempted": restore_needed,
                    "restoration_reserve_seconds": RESTORATION_RESERVE_SECONDS,
                    **clock_receipt,
                    "watchdog_source_sha256": digest(__file__)})
            except BaseException:
                return 1
    return 0 if status in ("resume_signal_sent", "already_running") \
        and reason in ("release", "disconnect", "deadline") and clock_receipt["clock_continuity_passed"] else 1


def wait_monitor(monitor, keepawake, deadline):
    while True:
        require(keepawake.poll() is None, "keepawake_exited")
        require(continuous_ns() < deadline, "launcher_deadline_expired")
        result = monitor.poll()
        require(continuous_ns() < deadline, "launcher_deadline_expired")
        if result is not None:
            return result
        time.sleep(min(POLL_SECONDS, max(0, (deadline - continuous_ns()) / NANOSECONDS)))


def cleanup_keepawake(child):
    if child is None:
        return "not_started"
    if child.poll() is not None:
        child.wait(timeout=1)
        return "exited_before_cleanup"
    child.terminate()
    try:
        child.wait(timeout=2)
        return "owned_child_reaped"
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=2)
            return "owned_child_killed_and_reaped"
        except subprocess.TimeoutExpired:
            return "owned_child_cleanup_unsettled"


def monitor_command(output):
    return [sys.executable, str(MONITOR), "--output", str(output),
        "--harness", HARNESS.name, "--", "--acceptance", str(PUBLIC / "SOL_VALIDATION_RESULTS.json"),
        "--acceptance-audit", str(PUBLIC / "SOL_VALIDATION_AUDIT.json")]


def launch(args):
    require(sys.platform == "darwin" and type(args.max_seconds) is int
            and RESTORATION_RESERVE_SECONDS < args.max_seconds <= MAX_SECONDS, "platform_or_bound_invalid")
    output = args.output.absolute()
    sidecar = output.with_name(output.name + "-launcher")
    require(output.is_relative_to(ROOT / "test-artifacts") and ".." not in output.parts
            and not output.exists() and output.parent.is_dir()
            and not sidecar.exists()
            and not any(p.is_symlink() for p in (output, sidecar, *output.parents)), "fresh_output_required")
    supplied = (args.renderer_pid, args.expected_starttime, args.expected_executable)
    require(all(v is not None for v in supplied) if args.suspend_renderer else all(v is None for v in supplied),
            "explicit_suspension_identity_required")
    expected = None
    if args.suspend_renderer:
        expected = {"pid": args.renderer_pid, "uid": os.getuid(),
            "starttime": " ".join(args.expected_starttime.split()), "executable": args.expected_executable}
        validate_target(expected, initial=True)
    if args.dry_run:
        continuous_ns()  # Verify native clock availability without launching children.
        print(json.dumps({"stage": "bounded_retry_dry_run", "suspension_requested": expected is not None,
            "identity_verified": expected is not None, "max_seconds": args.max_seconds,
            "restoration_reserve_seconds": RESTORATION_RESERVE_SECONDS,
            "clock_identity": CLOCK_IDENTITY, "max_suspend_gap_ns": MAX_SUSPEND_GAP_NS,
            "signals_sent": 0, "fresh_output": True}), flush=True)
        return 0
    sources = [Path(__file__), MONITOR, HARNESS, PUBLIC / "SOL_BF16_PROTOCOL.md",
        PUBLIC / "SOL_BF16_RETRY_PROTOCOL.md", PUBLIC / "SOL_VALIDATION_RESULTS.json",
        PUBLIC / "SOL_VALIDATION_AUDIT.json", ROOT / "scripts/launch_jev_sol_retry.py"]
    source_hashes = {str(path.relative_to(ROOT)): digest(path) for path in sources}
    sidecar.mkdir(mode=0o700)
    watch, monitor, keepawake = None, None, None
    code, monitor_exit, cleanup = 1, None, "not_started"
    completion_reason = "launcher_failed"
    keepawake_pid, keepawake_cleanup, keepawake_running = None, "not_started", False
    window = ClockWindow()
    deadline = window.continuous_start + (args.max_seconds - RESTORATION_RESERVE_SECONDS) * NANOSECONDS
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        keepawake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
        keepawake_pid = keepawake.pid
        require(keepawake.poll() is None, "keepawake_start_failed")
        if expected is not None:
            watch = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog", "--suspend-renderer"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, cwd=ROOT)
            notify(watch.stdin, {"identity": expected, "suspend_renderer": True, "max_seconds": args.max_seconds,
                "receipt": str(sidecar / "WATCHDOG.json"), "deadline_ns": deadline})
            require(receive(watch.stdout, min(deadline, continuous_ns() + 5 * NANOSECONDS))
                    == {"stage": "watchdog_ready"}, "watchdog_not_ready")
            notify(watch.stdin, {"operation": "stop"})
            require(receive(watch.stdout, min(deadline, continuous_ns() + 5 * NANOSECONDS))
                    == {"stage": "renderer_stop_observed"}, "renderer_stop_unconfirmed")
        progress_fd = os.open(sidecar / "PROGRESS.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(progress_fd, "wb") as progress:
            monitor = subprocess.Popen(monitor_command(output), cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=progress, stderr=subprocess.DEVNULL, close_fds=True)
            monitor_exit = wait_monitor(monitor, keepawake, deadline)
            code = monitor_exit
        cleanup = "monitor_exited"
        completion_reason = "monitor_completed" if monitor_exit == 0 else "monitor_failed"
    except BaseException as error:
        code = 1
        completion_reason = str(error) if isinstance(error, LauncherError) else "launcher_failed_or_interrupted"
        raise
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Release promptly; monitor cleanup may take 45 seconds and must not
        # delay the explicit restoration request.
        if watch is not None:
            try:
                notify(watch.stdin, {"operation": "release"})
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    watch.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
        if monitor is not None and monitor.poll() is None:
            try:
                monitor.terminate()  # Owned monitor uses its unchanged descendant cleanup.
                monitor_exit = monitor.wait(timeout=45)
                cleanup = "owned_monitor_cleanup_completed"
            except BaseException:
                cleanup = "owned_monitor_cleanup_unsettled"
        if watch is not None:
            try:
                watch.wait(timeout=7)
            except BaseException:
                code = 1  # Detached watchdog still owns its original deadline.
            if watch.returncode != 0:
                code = 1
        keepawake_running = keepawake is not None and keepawake.poll() is None
        try:
            keepawake_cleanup = cleanup_keepawake(keepawake)
        except BaseException:
            keepawake_cleanup = "owned_child_cleanup_failed"
        clock_receipt = window.receipt()
        power_passed = clock_receipt["clock_continuity_passed"] and keepawake_running \
            and keepawake_cleanup in ("owned_child_reaped", "owned_child_killed_and_reaped")
        if not power_passed:
            code = 1
        unchanged = all(digest(ROOT / path) == value for path, value in source_hashes.items())
        if not unchanged or cleanup == "owned_monitor_cleanup_unsettled":
            code = 1
        private_json(sidecar / "POWER.json", {"schema_version": 1,
            **clock_receipt, "keepawake_command": "/usr/bin/caffeinate -i -w launcher_pid",
            "keepawake_pid": keepawake_pid, "keepawake_running_before_cleanup": keepawake_running,
            "keepawake_cleanup": keepawake_cleanup,
            "power_status": "completed" if power_passed else "excluded",
            "power_reason": "completed" if power_passed else "clock_discontinuity"
                if not clock_receipt["clock_continuity_passed"] else "keepawake_lifecycle_failed",
            "performance_window_eligible": power_passed and code == 0 and monitor_exit == 0,
            "native_probability_acceptance_evaluated_here": False})
        private_json(sidecar / "LAUNCHER.json", {"schema_version": 2, "monitor_exit": monitor_exit,
            "launcher_exit": code,
            "completion_reason": completion_reason,
            "suspension_requested": expected is not None, "max_seconds": args.max_seconds,
            "restoration_reserve_seconds": RESTORATION_RESERVE_SECONDS,
            "source_hashes": source_hashes, "bound_sources_unchanged": unchanged,
            "monitor_cleanup": cleanup,
            "power_status": "completed" if power_passed else "excluded",
            "limits": "Identity checking and signaling are separate syscalls. Watchdog loss, machine shutdown or failed observation can prevent resumption. Launcher SIGKILL can leave the owned benchmark running; the detached watchdog only restores the renderer."})
        require(unchanged and cleanup != "owned_monitor_cleanup_unsettled", "launcher_completion_invalid")
    return code


def main():
    if sys.argv[1:] == ["--watchdog", "--suspend-renderer"]:
        return watchdog()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    parser.add_argument("--suspend-renderer", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--renderer-pid", type=int)
    parser.add_argument("--expected-starttime")
    parser.add_argument("--expected-executable")
    args = parser.parse_args()
    try:
        code = launch(args)
        print(json.dumps({"stage": "bounded_retry_launcher_complete", "exit_code": code}), flush=True)
        return code
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "bounded_retry_launcher_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
