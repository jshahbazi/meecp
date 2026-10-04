#!/usr/bin/env python3
"""Small Bonsai prompt lab. Runtime inputs and responses stay in memory."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "experiments" / "prompt_lab"
MODEL = ROOT / "models/bonsai-1.7b/Ternary-Bonsai-1.7B-Q2_0_g64.gguf"
MODEL_SHA256 = "6d0ecb3d9055969b5cde332b6fdb60e67ed3599e9f73e56b977731e1467e5c91"
ENGINE = Path("/opt/homebrew/bin/llama-completion")
ENGINE_BUILD = "build 11146, commit 7fe450e19"
ENGINE_SHA256 = "3ea8c0a6fc8dd1d9f577211321d07daecaa073cd28e139dd533dd4a69a6c7a88"


class LabError(ValueError):
    """A fixed diagnostic code, never input or inference content."""


def digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def strict_json(text: str):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite_number")

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite_number")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        if isinstance(value, list):
            for item in value:
                finite(item)

    value = json.loads(text, object_pairs_hook=object_pairs, parse_constant=constant)
    finite(value)
    return value


def same(actual, expected) -> bool:
    # JSON numbers may be rendered as 1 or 1.0; booleans are never numbers.
    if type(actual) in (int, float) and type(expected) in (int, float):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(same(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(same(a, e) for a, e in zip(actual, expected))
    return actual == expected


def grade(response: str, case: dict) -> dict:
    result = {"json_valid": False, "schema_valid": False, "passed": False, "failure": None,
              "mismatched_fields": []}
    try:
        actual = strict_json(response)
    except (ValueError, RecursionError):
        return {**result, "failure": "invalid_json"}
    result["json_valid"] = True
    if not Draft202012Validator(case["schema"]).is_valid(actual):
        return {**result, "failure": "schema_mismatch"}
    result["schema_valid"] = True
    if not same(actual, case["expected"]):
        # Report authored field names, never generated values or unknown generated keys.
        fields = [key for key, value in case["expected"].items() if not same(actual.get(key), value)]
        return {**result, "failure": "wrong_answer", "mismatched_fields": fields}
    return {**result, "passed": True}


def load_cases(data: str) -> list[dict]:
    cases = strict_json(data)
    if not isinstance(cases, list) or not cases:
        raise LabError("invalid_cases")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "prompt", "schema", "expected"}:
            raise LabError("invalid_case")
        if not isinstance(case["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", case["id"]):
            raise LabError("invalid_case_id")
        if case["id"] in seen or not isinstance(case["prompt"], str) or not case["prompt"].strip():
            raise LabError("invalid_case")
        seen.add(case["id"])
        Draft202012Validator.check_schema(case["schema"])
        # A schema sent to the model describes shape only, never the gold answer.
        def shape_only(node):
            if not isinstance(node, dict):
                raise LabError("schema_must_describe_shape_only")
            if set(node) - {"type", "properties", "required", "additionalProperties", "items"}:
                raise LabError("schema_must_describe_shape_only")
            if node.get("type") == "object":
                if node.get("additionalProperties") is not False or set(node.get("required", [])) != set(node.get("properties", {})):
                    raise LabError("schema_must_be_closed")
            for child in node.get("properties", {}).values():
                shape_only(child)
            if "items" in node:
                shape_only(node["items"])
        shape_only(case["schema"])
        if case["schema"].get("type") != "object" or not isinstance(case["expected"], dict):
            raise LabError("case_must_return_object")
        if not grade(json.dumps(case["expected"]), case)["passed"]:
            raise LabError("invalid_expected_answer")
    return cases


def chat(system: str, prompt: str) -> str:
    # Reused from bonsai_onboarding/benchmark.py at 70ec097. Matches the GGUF template.
    return ("<|im_start|>system\n" + system + "<|im_end|>\n"
            "<|im_start|>user\n" + prompt + "<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def infer(rendered: str, args, schema: dict | None) -> tuple[str, dict]:
    command = ["/usr/bin/time", "-l", str(args.engine), "-m", str(args.model), "-f", "/dev/stdin",
               "-c", str(args.context), "-n", str(args.tokens), "-ngl", "99", "-fa", "on", "-t", "6",
               "--no-conversation", "--no-jinja", "--no-display-prompt", "--simple-io", "--color", "off",
               "--seed", str(args.seed), "--temp", str(args.temperature), "--top-k", "40",
               "--top-p", "0.95", "--min-p", "0.0", "--no-context-shift"]
    if schema is not None:
        command.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
    start = time.perf_counter()
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors="replace", start_new_session=True)
        stdout, stderr = process.communicate(rendered, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        stop(process)
        return "", {"runtime_ok": False, "runtime_failure": "timeout", "wall_seconds": time.perf_counter() - start}
    except KeyboardInterrupt:
        stop(process)
        raise
    except OSError:
        return "", {"runtime_ok": False, "runtime_failure": "launch_failed", "wall_seconds": time.perf_counter() - start}
    metadata = {"runtime_ok": process.returncode == 0, "returncode": process.returncode,
                "wall_seconds": time.perf_counter() - start}
    patterns = {
        "max_rss_bytes": r"(\d+)\s+maximum resident set size",
        "prefill_tokens": r"prompt eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens",
        "decode_steps": r"(?<!prompt )eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*runs",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, stderr)
        if match:
            metadata[key] = int(match.group(1))
    # Strip only the fixed launcher suffix, never prose, fences, or extra JSON.
    response = re.sub(r"\s+\[end of text\]\s*\Z", "", stdout)
    metadata.update(output_sha256=digest(response), output_bytes=len(response.encode()),
                    budget_exhausted=metadata.get("decode_steps", 0) >= args.tokens)
    return response, metadata


def stop(process) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate(timeout=5)


def preflight(args) -> dict:
    if file_digest(args.model) != MODEL_SHA256:
        raise LabError("model_hash_mismatch")
    engine_sha = file_digest(args.engine)
    if engine_sha != ENGINE_SHA256:
        raise LabError("runtime_hash_mismatch")
    version = subprocess.run([str(args.engine), "--version"], text=True, capture_output=True, timeout=10)
    if version.returncode != 0 or ENGINE_BUILD not in version.stdout + version.stderr:
        raise LabError("runtime_version_mismatch")
    return {"model_sha256": MODEL_SHA256, "engine_sha256": engine_sha, "engine_build": ENGINE_BUILD}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases", type=Path, default=FIXTURES / "cases.json")
    p.add_argument("--system-file", type=Path, default=FIXTURES / "system.txt")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list fixture IDs")
    show = commands.add_parser("show", help="show the authored system/user prompts and independent gold")
    show.add_argument("case")
    run = commands.add_parser("run", help="run and grade prompts; retain only metadata")
    run.add_argument("--case", action="append", dest="selected", help="case ID; repeat to select several")
    run.add_argument("--prompt-file", type=Path, help="replace one selected case's user prompt; gold stays fixed")
    run.add_argument("--mode", choices=("plain", "schema", "both"), default="both")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--context", type=int, default=2048)
    run.add_argument("--tokens", type=int, default=256)
    run.add_argument("--timeout", type=float, default=30)
    run.add_argument("--model", type=Path, default=MODEL, help="relocated copy of the pinned model")
    run.add_argument("--engine", type=Path, default=ENGINE, help="copy of the pinned runtime build")
    run.add_argument("--output", type=Path, help="metadata-only JSON report")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    cases_bytes = args.cases.read_bytes()
    system_bytes = args.system_file.read_bytes()
    cases = load_cases(cases_bytes.decode())
    system = system_bytes.decode().strip()
    if not system:
        raise LabError("empty_system_prompt")
    if args.command == "list":
        for case in cases:
            print(case["id"])
        return 0
    if args.command == "show":
        selected = [c for c in cases if c["id"] == args.case]
        if not selected:
            raise LabError("unknown_case")
        print(json.dumps({"system": system, **selected[0]}, indent=2))
        return 0
    if not (1 <= args.repeat <= 100 and 0 <= args.seed <= 2**32 - args.repeat
            and 64 <= args.context <= 32768 and 1 <= args.tokens < args.context
            and math.isfinite(args.temperature) and 0 <= args.temperature <= 2
            and math.isfinite(args.timeout) and 0 < args.timeout <= 60):
        raise LabError("invalid_run_settings")
    if args.selected:
        if set(args.selected) - {c["id"] for c in cases}:
            raise LabError("unknown_case")
        cases = [c for c in cases if c["id"] in args.selected]
    if args.prompt_file:
        if len(cases) != 1:
            raise LabError("prompt_override_needs_one_case")
        cases = [{**cases[0], "prompt": args.prompt_file.read_text()}]
    if args.output:
        inputs = [args.cases, args.system_file, args.model, args.engine, Path(__file__)]
        if args.prompt_file:
            inputs.append(args.prompt_file)
        if args.output.resolve() in {p.resolve() for p in inputs}:
            raise LabError("report_would_overwrite_input")
    harness_sha256 = file_digest(Path(__file__))
    identity = preflight(args)
    modes = ("plain", "schema") if args.mode == "both" else (args.mode,)
    trials = []
    for case in cases:
        rendered = chat(system, case["prompt"])
        for repetition in range(args.repeat):
            # Same seed in each paired mode; increment across repetitions.
            seed = args.seed
            args.seed = seed + repetition
            for mode in modes:
                response, metadata = infer(rendered, args, case["schema"] if mode == "schema" else None)
                result = grade(response, case)
                if not metadata["runtime_ok"] or metadata.get("budget_exhausted", False):
                    result.update(passed=False, failure="runtime_failed" if not metadata["runtime_ok"] else "token_limit")
                trial = {"case": case["id"], "mode": mode, "repetition": repetition + 1, "seed": args.seed,
                         "prompt_sha256": digest(rendered), "schema_sha256": digest(json.dumps(case["schema"], sort_keys=True)),
                         "expected_sha256": digest(json.dumps(case["expected"], sort_keys=True)), **metadata, **result}
                trials.append(trial)
                print(f"{case['id']:32} {mode:6} {'PASS' if result['passed'] else 'FAIL':4} "
                      f"{metadata['wall_seconds']:.3f}s {result['failure'] or ''} "
                      f"{','.join(result['mismatched_fields'])}", flush=True)
            args.seed = seed
    summary = {mode: {"passed": sum(t["passed"] for t in trials if t["mode"] == mode),
                      "total": sum(t["mode"] == mode for t in trials),
                      "median_wall_seconds": statistics.median(t["wall_seconds"] for t in trials if t["mode"] == mode)}
               for mode in modes}
    report = {"report_version": 1, **identity, "cases_sha256": digest(cases_bytes), "system_file_sha256": digest(system_bytes),
              "system_sha256": digest(system), "harness_sha256": harness_sha256,
              "python_version": sys.version.split()[0], "uv_lock_sha256": file_digest(ROOT / "uv.lock"),
              "settings": {"context": args.context, "tokens": args.tokens, "temperature": args.temperature,
                           "timeout": args.timeout, "repeat": args.repeat, "gpu_layers": 99, "threads": 6,
                           "top_k": 40, "top_p": 0.95, "min_p": 0.0, "template": "embedded_default"},
              "summary": summary, "trials": trials}
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if all(t["passed"] for t in trials) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("prompt_lab: interrupted", file=sys.stderr)
        raise SystemExit(130)
    except LabError as error:
        print(f"prompt_lab: {error}", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        # No source paths, exception dumps, prompts, responses, or engine diagnostics.
        print("prompt_lab: invalid input or unavailable pinned runtime; check paths and fixture schema", file=sys.stderr)
        raise SystemExit(2)
