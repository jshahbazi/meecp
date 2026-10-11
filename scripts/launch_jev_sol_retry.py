"""Opt-in bounded Codex renderer pause around the unchanged Sol retry monitor.

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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


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


def receive(stream, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(remaining) or time.monotonic() >= deadline:
            return None
        raw = os.read(stream.fileno(), 4096)
    if not raw:
        return None
    require(len(raw) < 4096 and raw.endswith(b"\n") and raw.count(b"\n") == 1, "control_message_invalid")
    return json.loads(raw)


def notify(stream, value):
    stream.write(encoded(value))
    stream.flush()


def watchdog():
    """Detached process; launcher owns the only write end of its control pipe."""
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    expected, receipt_path, restore_needed = None, None, False
    status, reason = "not_stopped", "failed"
    try:
        request = receive(sys.stdin.buffer, time.monotonic() + 5)
        require(type(request) is dict and request["suspend_renderer"] is True
                and type(request["max_seconds"]) is int
                and RESTORATION_RESERVE_SECONDS < request["max_seconds"] <= MAX_SECONDS, "watchdog_request_invalid")
        expected = request["identity"]
        receipt_path = Path(request["receipt"])
        require(receipt_path.is_absolute() and not receipt_path.exists()
                and receipt_path.parent.is_dir() and receipt_path.parent.stat().st_mode & 0o077 == 0
                and not any(p.is_symlink() for p in (receipt_path, *receipt_path.parents)), "watchdog_receipt_invalid")
        validate_target(expected, initial=True)
        deadline = time.monotonic() + request["max_seconds"] - RESTORATION_RESERVE_SECONDS
        notify(sys.stdout.buffer, {"stage": "watchdog_ready"})
        require(receive(sys.stdin.buffer, deadline) == {"operation": "stop"}, "watchdog_not_armed")
        require(time.monotonic() < deadline, "stop_deadline_expired")
        validate_target(expected, initial=True)  # Recheck immediately before STOP.
        require(time.monotonic() < deadline, "stop_deadline_expired")
        restore_needed = True
        os.kill(expected["pid"], signal.SIGSTOP)
        stopped_deadline = min(deadline, time.monotonic() + 2)
        while "T" not in validate_target(expected)["state"]:
            require(time.monotonic() < stopped_deadline, "stop_not_observed")
            time.sleep(.05)
        require(time.monotonic() < deadline, "stop_deadline_expired")
        notify(sys.stdout.buffer, {"stage": "renderer_stop_observed"})
        message = receive(sys.stdin.buffer, deadline)
        require(message is None or message == {"operation": "release"}, "watchdog_message_invalid")
        reason = "release_or_disconnect_or_deadline"
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
                private_json(receipt_path, {"schema_version": 1, "reason": reason,
                    "resume_status": status, "stop_attempted": restore_needed,
                    "restoration_reserve_seconds": RESTORATION_RESERVE_SECONDS,
                    "watchdog_source_sha256": digest(__file__)})
            except BaseException:
                return 1
    return 0 if status in ("resume_signal_sent", "already_running") \
        and reason == "release_or_disconnect_or_deadline" else 1


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
        print(json.dumps({"stage": "bounded_retry_dry_run", "suspension_requested": expected is not None,
            "identity_verified": expected is not None, "max_seconds": args.max_seconds,
            "restoration_reserve_seconds": RESTORATION_RESERVE_SECONDS,
            "signals_sent": 0, "fresh_output": True}), flush=True)
        return 0
    sources = [Path(__file__), MONITOR, HARNESS, PUBLIC / "SOL_BF16_PROTOCOL.md",
        PUBLIC / "SOL_BF16_RETRY_PROTOCOL.md", PUBLIC / "SOL_VALIDATION_RESULTS.json",
        PUBLIC / "SOL_VALIDATION_AUDIT.json"]
    source_hashes = {str(path.relative_to(ROOT)): digest(path) for path in sources}
    sidecar.mkdir(mode=0o700)
    watch, monitor, code, monitor_exit, cleanup = None, None, 1, None, "not_started"
    deadline = time.monotonic() + args.max_seconds - RESTORATION_RESERVE_SECONDS
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if expected is not None:
            watch = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog", "--suspend-renderer"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, cwd=ROOT)
            notify(watch.stdin, {"identity": expected, "suspend_renderer": True, "max_seconds": args.max_seconds,
                "receipt": str(sidecar / "WATCHDOG.json")})
            require(receive(watch.stdout, min(deadline, time.monotonic() + 5))
                    == {"stage": "watchdog_ready"}, "watchdog_not_ready")
            notify(watch.stdin, {"operation": "stop"})
            require(receive(watch.stdout, min(deadline, time.monotonic() + 5))
                    == {"stage": "renderer_stop_observed"}, "renderer_stop_unconfirmed")
        progress_fd = os.open(sidecar / "PROGRESS.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(progress_fd, "wb") as progress:
            monitor = subprocess.Popen(monitor_command(output), cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=progress, stderr=subprocess.DEVNULL, close_fds=True)
            monitor_exit = monitor.wait(timeout=max(.01, deadline - time.monotonic()))
            code = monitor_exit
        cleanup = "monitor_exited"
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if monitor is not None and monitor.poll() is None:
            monitor.terminate()  # Owned monitor uses its unchanged descendant cleanup.
            try:
                monitor_exit = monitor.wait(timeout=45)
                cleanup = "owned_monitor_cleanup_completed"
            except subprocess.TimeoutExpired:
                cleanup = "owned_monitor_cleanup_unsettled"
        if watch is not None:
            try:
                notify(watch.stdin, {"operation": "release"})
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    watch.stdin.close()  # EOF also triggers automatic resumption.
                except (BrokenPipeError, OSError):
                    pass
            try:
                watch.wait(timeout=7)
            except subprocess.TimeoutExpired:
                code = 1  # Detached watchdog still owns its original deadline.
            if watch.returncode != 0:
                code = 1
        unchanged = all(digest(ROOT / path) == value for path, value in source_hashes.items())
        if not unchanged or cleanup == "owned_monitor_cleanup_unsettled":
            code = 1
        private_json(sidecar / "LAUNCHER.json", {"schema_version": 1, "monitor_exit": monitor_exit,
            "launcher_exit": code,
            "suspension_requested": expected is not None, "max_seconds": args.max_seconds,
            "restoration_reserve_seconds": RESTORATION_RESERVE_SECONDS,
            "source_hashes": source_hashes, "bound_sources_unchanged": unchanged,
            "monitor_cleanup": cleanup,
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
