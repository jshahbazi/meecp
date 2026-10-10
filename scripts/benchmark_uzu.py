"""Compare isolated Metal engines on synthetic text; persist metrics only.

Each engine has a native foreground owner and is reaped before the other loads.
No prompts, generated text, token IDs, credentials or exception dumps are saved.
The two exported checkpoints use different eight-bit quantizations.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import secrets
import socket
import statistics
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)
from meecp.processes import ProcessOwner

PROFILE = "qwen3.5-0.8b-mirai-l-uzu-metal-text-v1"
BUILD = "8d01581474760bd1fc7f4a547ff1d02f7dada437"


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(value, code="verification_failed"):
    if not value:
        raise ValueError(code)


def model_declaration(binary, model):
    return {"artifact": str((model / "model.safetensors").relative_to(ROOT)),
        "model_id": "Qwen3.5-0.8B-Mirai-L", "sha256": file_hash(model / "model.safetensors"),
        "profile": PROFILE, "context_tokens": 4096,
        "engine_identity": {"runtime": "uzu", "version": "0.6.1", "build": BUILD,
                            "binary_sha256": file_hash(binary)},
        "parameters": {"max_output_tokens": 256, "queue_limit": 4, "context_guard": 8}}


def prompt(text):
    # Both pinned Qwen 3.5 templates apply Jinja's trim filter to user content.
    # The shortest workload begins with a newline when the repeated prefix is
    # empty, so mirror that rendering before comparing hashes/tokenization.
    text = text.strip()
    return ("<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"
            "<|im_start|>user\n" + text + "<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def workload(size):
    # Short/medium/long prefill; force a useful long generation without ignoring
    # EOS. Output counts are recorded so early termination stays visible.
    return ("The blue notebook contains a numbered inventory. " * size
            + "\nContinue a numbered list of distinct ordinary household objects, "
              "starting at 1. Write at least 100 entries, one per line.")


def address():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return {"host": "127.0.0.1", "port": port}, f"http://127.0.0.1:{port}"


async def launch(owner, kind, args, scratch):
    endpoint, url = address()
    key = secrets.token_urlsafe(32)
    nonce = secrets.token_hex(32)
    env = {"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin",
           "LANG": "C", "RUST_LOG": "off"}
    start = time.perf_counter()
    if kind == "uzu":
        declaration = model_declaration(args.uzu_binary, args.uzu_model)
        config = {"endpoint": endpoint, "api_key": key, "generation": 1,
                  "nonce": nonce, "model": declaration, "dependencies": {}}
        read, write = os.pipe()
        try:
            os.write(write, json.dumps(config).encode())
        finally:
            os.close(write)
        try:
            process = await owner.launch(args.uzu_binary, ["--meecp-config-fd", str(read)],
                ROOT, env, inherited_fds=(read,), io_mode="fds", owned_group=True, stop_grace=.5)
        finally:
            os.close(read)
        readiness = "/health"
    else:
        keyfile = scratch / "llama.key"
        keyfile.write_text(key)
        keyfile.chmod(0o600)
        executable = args.llama_binary
        process = await owner.launch(executable, ["--model", str(args.gguf),
            "--host", "127.0.0.1", "--port", str(endpoint["port"]),
            "--api-key-file", str(keyfile), "--ctx-size", "4096", "--parallel", "1",
            "--n-gpu-layers", "99", "--flash-attn", "on", "--batch-size", "2048",
            "--ubatch-size", "512", "--no-context-shift", "--no-webui", "--log-disable"],
            executable.parent / "plugins", env, io_mode="fds", owned_group=True, stop_grace=.5)
        readiness = "/props"
    process.close_stdio()
    client = httpx.AsyncClient(base_url=url, headers={"authorization": "Bearer " + key},
                              trust_env=False, timeout=120)
    try:
        async with asyncio.timeout(180):
            while True:
                require(process.returncode is None and not process.ownership_lost, "startup_failed")
                try:
                    response = await client.get(readiness, timeout=1)
                    if response.status_code == 200:
                        props = response.json()
                        if kind == "uzu":
                            require(props["ready"] is True and props["generation"] == 1
                                    and props["nonce"] == nonce, "readiness_failed")
                        else:
                            require(props["total_slots"] == 1
                                    and props["default_generation_settings"]["n_ctx"] == 4096)
                        return process, client, time.perf_counter() - start
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.05)
    except BaseException:
        await client.aclose()
        await process.stop()
        raise


async def request(kind, client, text, maximum):
    start = time.perf_counter()
    rendered = prompt(text)
    if kind == "uzu":
        response = await client.post("/generate", json={"text": text, "max_output_tokens": maximum})
        require(response.status_code == 200, "uzu_generation_failed")
        value = response.json()
        metrics = value["metrics"]
        count_in, count_out = value["input_tokens"], value["output_tokens"]
        if "prompt_sha256" in metrics:
            require(metrics["prompt_sha256"] == hashlib.sha256(rendered.encode()).hexdigest(),
                    "prompt_mismatch")
        row = {"input_tokens": count_in, "output_tokens": count_out,
               "prefill_tps": metrics["prefill_tps"], "decode_tps": metrics["decode_tps"],
               "engine_ttft_seconds": metrics["ttft_seconds"]}
    else:
        tokenized = await client.post("/tokenize", json={"content": rendered,
                                                       "add_special": False, "parse_special": True})
        require(tokenized.status_code == 200)
        tokens = tokenized.json()["tokens"]
        # Match Uzu's fresh state: do not permit prefix/prompt-cache reuse.
        response = await client.post("/completion", json={"prompt": tokens, "n_predict": maximum,
            "temperature": 0, "cache_prompt": False, "stream": False,
            "ignore_eos": False, "return_tokens": False})
        require(response.status_code == 200, "llama_generation_failed")
        value = response.json()
        timing = value["timings"]
        count_in, count_out = len(tokens), value["tokens_predicted"]
        require(value["truncated"] is False and value["tokens_evaluated"] == count_in)
        row = {"input_tokens": count_in, "output_tokens": count_out,
               "prefill_tps": timing["prompt_per_second"],
               "decode_tps": timing["predicted_per_second"],
               "engine_ttft_seconds": None}
        # Retain only the identity of the sequence; never persist token IDs.
        token_bytes = b"".join(token.to_bytes(4, "little") for token in tokens)
        row["prompt_tokens_sha256"] = hashlib.sha256(token_bytes).hexdigest()
    require(type(count_in) is int and 0 < count_in < 4096)
    require(type(count_out) is int and 0 < count_out <= maximum)
    require(type(value["text" if kind == "uzu" else "content"]) is str)
    row["wall_seconds"] = time.perf_counter() - start
    if kind == "uzu":
        row["prompt_tokens_sha256"] = metrics["prompt_tokens_sha256"]
    return row


async def benchmark(args, report):
    owner = ProcessOwner(args.reaper)
    with tempfile.TemporaryDirectory(prefix="meecp-uzu-benchmark-") as directory:
        scratch = Path(directory)
        try:
            for round_index in range(args.rounds):
                order = ("llama.cpp", "uzu") if round_index % 2 == 0 else ("uzu", "llama.cpp")
                for kind in order:
                    report["stage"] = f"round_{round_index + 1}_{kind}"
                    process, client, startup = await launch(owner, kind, args, scratch)
                    run = {"engine": kind, "round": round_index + 1,
                           "startup_seconds": startup, "samples": []}
                    try:
                        await request(kind, client, workload(0), 32)
                        for name, size in (("short", 0), ("medium", 48), ("long", 144)):
                            for _ in range(args.trials):
                                row = await request(kind, client, workload(size), args.output_tokens)
                                row["workload"] = name
                                run["samples"].append(row)
                    finally:
                        await client.aclose()
                        await process.stop()
                    require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
                    run["peak_rss_bytes"] = process.peak_memory_bytes
                    report["runs"].append(run)
                    print(json.dumps({"stage": report["stage"], "samples": len(run["samples"]),
                                      "startup_seconds": round(startup, 3)}), flush=True)
            for name in ("short", "medium", "long"):
                counts = {row["input_tokens"] for run in report["runs"] for row in run["samples"]
                          if row["workload"] == name}
                require(len(counts) == 1, "tokenization_mismatch")
                hashes = {row["prompt_tokens_sha256"] for run in report["runs"] for row in run["samples"]
                          if row["workload"] == name}
                require(len(hashes) == 1, "tokenization_mismatch")
            report["summary"] = []
            for kind in ("llama.cpp", "uzu"):
                for name in ("short", "medium", "long"):
                    rows = [row for run in report["runs"] if run["engine"] == kind
                            for row in run["samples"] if row["workload"] == name]
                    report["summary"].append({"engine": kind, "workload": name, "samples": len(rows),
                        **{key: statistics.median(row[key] for row in rows) for key in
                           ("wall_seconds", "prefill_tps", "decode_tps", "input_tokens", "output_tokens")}})
            report["stage"] = "complete"
        finally:
            await owner.close()
            report["ownership_settled"] = not owner.instances


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uzu-binary", type=Path, default=ROOT / "runtime/uzu-rust/target/release/meecp-uzu-worker")
    parser.add_argument("--uzu-model", type=Path, default=ROOT / "models/uzu-experiment/uzu")
    parser.add_argument("--gguf", type=Path, default=ROOT / "models/uzu-experiment/gguf/Qwen3.5-0.8B-Q8_0.gguf")
    parser.add_argument("--llama-binary", type=Path, default=ROOT / "runtime/llama-relocated/llama/llama-server")
    parser.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    parser.add_argument("--rounds", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--trials", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--output-tokens", type=int, choices=range(32, 257), default=256)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"stage": "start", "runs": [], "hardware": platform.machine(),
              "os_version": platform.mac_ver()[0], "harness_sha256": file_hash(__file__),
              "method": {"rounds": args.rounds, "trials": args.trials,
                  "maximum_output_tokens": args.output_tokens, "context_tokens": 4096,
                  "sampling": "greedy", "thinking": "disabled", "cache": "fresh state",
                  "quantizations": "Mirai-L and Q8_0; not identical weights"}}
    code = 0
    try:
        for name in ("uzu_binary", "uzu_model", "gguf", "llama_binary", "reaper"):
            setattr(args, name, getattr(args, name).resolve())
        report["artifacts"] = {"uzu_binary_sha256": file_hash(args.uzu_binary),
            "llama_binary_sha256": file_hash(args.llama_binary),
            "gguf_sha256": file_hash(args.gguf),
            "uzu_weights_sha256": file_hash(args.uzu_model / "model.safetensors")}
        provenance = json.loads((ROOT / "experiments/uzu-rust/MODEL_PROVENANCE.json").read_text())
        for engine in ("uzu", "gguf"):
            for record in provenance[engine]["files"]:
                path = args.gguf if engine == "gguf" else args.uzu_model / record["source"]
                require(path.stat().st_size == record["size"] and file_hash(path) == record["sha256"],
                        "verification_failed")
        asyncio.run(benchmark(args, report))
    except BaseException as error:
        report["error_type"] = type(error).__name__
        # Only explicitly authored error codes, never upstream exception text.
        if isinstance(error, ValueError) and error.args and error.args[0] in {
                "verification_failed", "startup_failed", "readiness_failed", "prompt_mismatch",
                "uzu_generation_failed", "llama_generation_failed", "ownership_unsettled", "tokenization_mismatch"}:
            report["error_code"] = error.args[0]
        code = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"stage": report["stage"], "ownership_settled": report.get("ownership_settled"),
                      "error_code": report.get("error_code"), "report": str(args.report)}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
