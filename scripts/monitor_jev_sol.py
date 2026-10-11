"""Monitor serial Uzu experiments without altering external processes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"llama-server", "meecp-uzu-worker", "meecp-uzu-score-digest", "mlx-serve"}
COMPILERS = {"cargo", "rustc", "clang", "clang++", "cc1", "ld"}
HARNESSES = {"benchmark_jev_sol.py"}
BACKGROUND_CPU_LIMIT = 200.0


def generic_inference(name, arguments):
    """Classify privately inspected argv; never retain or emit command text."""
    if not (name.lower().startswith("python") or name in {"uv", "mlx_lm.server"}):
        return False
    return bool(re.search(r"(?:^|\s)--(?:model|model-path|model_path|score-model-path)(?:=|\s)", arguments)
                or re.search(r"(?:^|[/\s])(?:train|inference|benchmark|evaluate|serve)[^/\s]*\.py(?:\s|$)", arguments))


def sample(child=None):
    raw = subprocess.run(["ps", "-ww", "-axo", "pid=,ppid=,state=,pcpu=,rss=,comm="],
                         capture_output=True, text=True, check=True).stdout
    rows = {}
    for line in raw.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6:
            rows[int(fields[0])] = {"parent": int(fields[1]), "state": fields[2],
                "cpu": float(fields[3]), "rss_kb": int(fields[4]), "name": Path(fields[5]).name}
    owned = {os.getpid()} | ({child.pid} if child else set())
    while True:
        children = {pid for pid, row in rows.items() if row["parent"] in owned}
        if children.issubset(owned):
            break
        owned.update(children)
    # Do not classify our uv/tool ancestors as external inference merely because
    # their command line contains the benchmark harness name.
    ancestors, cursor = set(), os.getpid()
    while cursor in rows and rows[cursor]["parent"] > 0:
        cursor = rows[cursor]["parent"]
        if cursor in ancestors:
            break
        ancestors.add(cursor)
    # Generic Python runtimes can conceal a model workload from executable names.
    commands = subprocess.run(["ps", "-ww", "-axo", "pid=,args="], capture_output=True,
                              text=True, check=True).stdout
    generic = 0
    for line in commands.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        row = rows.get(pid)
        if row and pid not in owned | ancestors and "T" not in row["state"]:
            generic += generic_inference(row["name"], fields[1])
    background = [r for pid, r in rows.items() if pid not in owned and "T" not in r["state"]]
    return {"external_models": sum(r["name"] in MODELS for r in background),
        "generic_inference": generic,
        "compilers": sum(r["name"] in COMPILERS for r in background),
        "owned_models": sum(r["name"] in MODELS for pid, r in rows.items() if pid in owned),
        "background_process_max_cpu": max((r["cpu"] for r in background), default=0),
        "background_large_rss": sum(r["rss_kb"] > 1024 * 1024 for r in background)}


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def interruption(signum, frame):
    raise RuntimeError("monitor_interrupted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harness", choices=sorted(HARNESSES), required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    # The controller supplies run/output itself. Only the bound Sol harness runs.
    if not arguments or "--output" in arguments or any(a.startswith("--output=") for a in arguments):
        return 1
    child, code, failure = None, 1, None
    samples = []
    args.output.mkdir(mode=0o700)
    signal.signal(signal.SIGINT, interruption)
    signal.signal(signal.SIGTERM, interruption)
    started = time.monotonic()
    try:
        samples.append({"elapsed_seconds": 0, "completed_blocks": 0, **sample()})
        if (samples[-1]["external_models"] or samples[-1]["compilers"] or samples[-1]["generic_inference"]
                or samples[-1]["background_process_max_cpu"] > BACKGROUND_CPU_LIMIT):
            raise RuntimeError("preflight_contention")
        command = [sys.executable, str(ROOT / "scripts" / args.harness), "run", "--output", str(args.output), *arguments]
        child = subprocess.Popen(command, cwd=ROOT)
        while child.poll() is None:
            blocks = len(list(args.output.glob("block-*.json"))) + len(list(args.output.glob("round-*.json")))
            samples.append({"elapsed_seconds": time.monotonic() - started,
                            "completed_blocks": blocks, **sample(child)})
            if (samples[-1]["external_models"] or samples[-1]["compilers"]
                    or samples[-1]["generic_inference"] or samples[-1]["owned_models"] > 1
                    or samples[-1]["background_process_max_cpu"] > BACKGROUND_CPU_LIMIT):
                raise RuntimeError("contention_detected")
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        code = child.returncode
    except BaseException as error:
        failure = type(error).__name__
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        final = sample()
        samples.append({"elapsed_seconds": time.monotonic() - started, **final})
        sample_path = args.output / "HOST_SAMPLES.json"
        private_json(sample_path, samples)
        result = {"schema_version": 1, "benchmark_exit": code, "failure_type": failure,
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "host_samples_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
            "sampling_interval_seconds": 5, "samples": len(samples), "suspended_processes": 0,
            "external_model_samples": sum(r["external_models"] > 0 for r in samples),
            "generic_inference_samples": sum(r["generic_inference"] > 0 for r in samples),
            "compiler_samples": sum(r["compilers"] > 0 for r in samples),
            "background_cpu_limit_percent": BACKGROUND_CPU_LIMIT,
            "high_background_cpu_samples": sum(r["background_process_max_cpu"] > BACKGROUND_CPU_LIMIT for r in samples),
            "max_owned_models": max(r["owned_models"] for r in samples),
            "final_active_models": final["external_models"] + final["owned_models"] + final["generic_inference"],
            "maximum_background_process_cpu": max(r["background_process_max_cpu"] for r in samples),
            "maximum_background_large_rss": max(r["background_large_rss"] for r in samples),
            "limits": "Sampled process metadata only; generic process names can conceal workloads. No continuous GPU or thermal measurement."}
        private_json(args.output / "HOST_ISOLATION.json", result)
        print(json.dumps({"stage": "host_monitor_complete", **result}), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
