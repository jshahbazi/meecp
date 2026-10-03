"""Actual exact-fixture LLM compatibility, with bounded payload-free receipts.

Development spike only: this does not install or advertise a generic LLM recipe.
The selected weights must already exist; this harness never acquires artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "vendor")]
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)

from mcpme.model_intake import copy_artifact
from mcpme_textgen.adapter import BoundedTextGenerator, LoopbackTransport, TextGenerationError, render_prompt
from mcpme_textgen.fixture import (EMBEDDED_TEMPLATE_SHA256, ENGINE_BUILD, LICENSE_SHA256,
    MODEL_BYTES, MODEL_FILENAME, MODEL_SHA256, TEMPLATE_SHA256, TOKENIZER_SHA256,
    digest, file_hash, tokenizer_metadata)


def require(condition):
    if not condition:
        raise ValueError("verification_failed")


class Engine:
    def __init__(self, root, weights, *, jevk5=False):
        self.root, self.weights, self.jevk5 = root, weights, jevk5
        self.process = None
        self.key = secrets.token_urlsafe(32)
        self.keyfile = root / ("jevk5.key" if jevk5 else "text.key")
        self.keyfile.write_text(self.key)
        self.keyfile.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.endpoint = "http://127.0.0.1:" + str(port)
        self.transport = LoopbackTransport(self.endpoint, self.key, timeout=120)
        self.port = port

    def request(self, path, payload=None):
        request = urllib.request.Request(self.endpoint + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        with self.transport.opener.open(request, timeout=120) as response:
            data = response.read(1024 * 1024 + 1)
        require(len(data) <= 1024 * 1024)
        return json.loads(data)

    def start(self):
        command = [str(self.root / "llama/llama-server"), "-m", str(self.weights),
                   "-c", "8192" if self.jevk5 else "512", "-np", "1",
                   "-ngl", "99" if self.jevk5 else "0",
                   "-b", "2048" if self.jevk5 else "128",
                   "-ub", "512" if self.jevk5 else "128",
                   "--host", "127.0.0.1", "--port", str(self.port),
                   "--api-key-file", str(self.keyfile), "--no-context-shift",
                   "--no-webui", "--log-disable"]
        if self.jevk5:
            command += ["-fa", "auto"]
        else:
            command += ["--device", "none", "--no-kv-offload", "-t", "2"]
        self.process = subprocess.Popen(command, cwd=self.root / "llama/plugins",
            env={"HOME": str(self.root), "PATH": "/usr/bin:/bin", "LANG": "C",
                 "TMPDIR": str(self.root)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            require(self.process.poll() is None)
            try:
                props = self.request("/props")
                require(props["total_slots"] == 1)
                require(props["default_generation_settings"]["n_ctx"] == (8192 if self.jevk5 else 512))
                if not self.jevk5:
                    require(hashlib.sha256(props["chat_template"].encode()).hexdigest() == TEMPLATE_SHA256)
                return
            except (OSError, ValueError):
                time.sleep(.05)
        raise ValueError("engine_timeout")

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=10)
        self.keyfile.unlink(missing_ok=True)
        return True

    async def retire(self):
        return await asyncio.to_thread(self.stop)


async def actual_text(engine, report):
    checks = report["checks"]
    report["case"] = "template_and_generation"
    adapter = BoundedTextGenerator(engine.transport, engine.retire)
    fixtures = ["Reply with the word blue.", "List integers starting at one.",
                "Repeat this Unicode label: café.\nA second line."]
    input_counts, output_counts = [], []
    for text in fixtures:
        rendered = engine.request("/apply-template", {
            "messages": [{"role": "user", "content": text}], "add_generation_prompt": True})["prompt"]
        require(rendered == render_prompt(text))
        actual = engine.transport("/tokenize", {"content": rendered, "add_special": False,
                                                 "parse_special": True})["tokens"]
        unparsed = engine.transport("/tokenize", {"content": rendered, "add_special": False,
                                                   "parse_special": False})["tokens"]
        require(actual != unparsed and actual.count(151644) == 3 and actual.count(151645) == 2)
        for maximum in (1, 8, 64):
            result = await adapter.generate({"text": text, "max_output_tokens": maximum})
            require(result["input_tokens"] == len(actual) and 0 < result["output_tokens"] <= maximum)
            input_counts.append(result["input_tokens"])
            output_counts.append(result["output_tokens"])
    checks["actual_cpu_inference"] = checks["template_token_parity"] = checks["output_bounds"] = True
    report["inference_cases"] = len(output_counts)
    report["input_token_counts"], report["output_token_counts"] = input_counts, output_counts

    # An exact last-admissible prompt for the largest advertised output budget.
    # Find by tokenizer count, not characters, then test the next token boundary.
    report["case"] = "context_boundary"
    maximum_text = None
    for count in range(380, 501):
        text = "x " * count
        tokens = engine.transport("/tokenize", {"content": render_prompt(text),
                    "add_special": False, "parse_special": True})["tokens"]
        if len(tokens) == 440:
            maximum_text = text
            break
    require(maximum_text is not None)
    boundary = await adapter.generate({"text": maximum_text, "max_output_tokens": 64})
    require(boundary["input_tokens"] == 440 and boundary["output_tokens"] <= 64)
    try:
        await adapter.generate({"text": maximum_text + "x ", "max_output_tokens": 64})
    except TextGenerationError as error:
        require(error.code == "context_limit")
    else:
        raise ValueError("context_bound_missing")
    require(adapter.ownership_settled and not adapter.closed)
    checks["maximum_context_boundary"] = True

    tokens = engine.transport("/tokenize", {"content": render_prompt(fixtures[0]),
                 "add_special": False, "parse_special": True})["tokens"]
    report["case"] = "forced_eos"
    eos = engine.transport("/completion", {"prompt": tokens, "n_predict": 4,
        "temperature": 0, "return_tokens": True, "cache_prompt": False,
        "logit_bias": [[151645, 100]], "ignore_eos": False})
    report["forced_eos"] = {"eos_stop": eos["stop_type"] == "eos", "returned_token_count": len(eos["tokens"]),
                            "predicted_token_count": eos["tokens_predicted"], "token_match": eos["tokens"] == [151645]}
    require(eos["stop_type"] == "eos" and eos["tokens"] == [151645])
    report["case"] = "forced_stop_word"
    endoftext = engine.transport("/completion", {"prompt": tokens, "n_predict": 4,
        "temperature": 0, "return_tokens": True, "cache_prompt": False,
        "logit_bias": [[151643, 100]], "ignore_eos": False})
    require(endoftext["stop_type"] == "eos" and endoftext["tokens"] == [151643])
    word_ids = engine.transport("/tokenize", {"content": "blue", "add_special": False,
                                               "parse_special": False})["tokens"]
    require(len(word_ids) == 1)
    word = engine.transport("/completion", {"prompt": tokens, "n_predict": 4,
        "temperature": 0, "return_tokens": True, "cache_prompt": False,
        "logit_bias": [[word_ids[0], 100]], "ignore_eos": False, "stop": ["blue"]})
    require(word["stop_type"] == "word" and word["stopping_word"] == "blue"
            and "blue" not in word["content"])
    checks["actual_eos_and_stop_tokens"] = True

    report["case"] = "cancel_queue"
    entered = threading.Event()
    active = 0
    peak = 0

    def measured(path, payload):
        nonlocal active, peak
        if path == "/completion":
            active += 1
            peak = max(peak, active)
            entered.set()
        try:
            return engine.transport(path, payload)
        finally:
            if path == "/completion":
                active -= 1

    queued = BoundedTextGenerator(measured, engine.retire, queue_limit=2)
    first = asyncio.create_task(queued.generate({"text": maximum_text, "max_output_tokens": 64}))
    while not entered.is_set():
        await asyncio.sleep(.001)
    first.cancel()
    second = asyncio.create_task(queued.generate({"text": fixtures[0], "max_output_tokens": 8}))
    await asyncio.sleep(0)
    require(not first.done() and not second.done() and not queued.ownership_settled)
    first.cancel()
    try:
        await queued.generate({"text": fixtures[0], "max_output_tokens": 8})
    except TextGenerationError as error:
        require(error.code == "queue_full")
    else:
        raise ValueError("queue_bound_missing")
    with contextlib.suppress(asyncio.CancelledError):
        await first
    await second
    require(peak == 1 and active == 0 and queued.pending == 0 and queued.ownership_settled)
    checks["actual_cancellation_serialization"] = checks["queue_bound"] = True
    report["peak_concurrent_completion_calls"] = peak

    # A real request interrupted by transport timeout must retire and reap the
    # exact owned generation before allowing its reservation to settle.
    report["case"] = "transport_timeout"
    short = LoopbackTransport(engine.endpoint, engine.key, timeout=.001)
    def interrupted(path, payload):
        return (short if path == "/completion" else engine.transport)(path, payload)
    failed = BoundedTextGenerator(interrupted, engine.retire)
    try:
        await failed.generate({"text": maximum_text, "max_output_tokens": 64})
    except TextGenerationError as error:
        require(error.code == "inference_failed")
    else:
        raise ValueError("transport_timeout_not_exercised")
    require(failed.closed and failed.ownership_settled and engine.process.returncode is not None)
    checks["actual_transport_timeout_reaped"] = True


def jevk5_parity(engine, report):
    from jevk5.gguf import JevK5GGUF
    from jevk5_app.scorer import LocalScorer

    class Reference(JevK5GGUF):
        def _post(self, path, payload):
            return engine.transport(path, payload)

    reference = Reference(temperature=1.22, knockout_temperature=.93)
    scorer = LocalScorer(engine.endpoint, engine.key, 8192, temperature=1.22, knockout_temperature=.93)
    questions = [
        {"type": "choice", "instructions": "Choose the synthetic category.", "criteria": ["object", "array"]},
        {"type": "noul", "instructions": "The synthetic object has a count field."},
        {"type": "score", "instructions": "Rate the synthetic count.", "criteria": ["zero", "one", "two"]},
    ]

    def difference(a, b):
        require(type(a) is type(b))
        if isinstance(a, dict):
            require(a.keys() == b.keys())
            return max((difference(a[k], b[k]) for k in a), default=0)
        if isinstance(a, float):
            return abs(a - b)
        require(a == b)
        return 0

    delta = 0
    for question in questions:
        evidence = '{"count":1}'
        expected = reference.decide(evidence, question)
        fresh = scorer.decide(evidence, question)
        require(not scorer.cache_hit)
        repeated = scorer.decide(evidence, question)
        require(scorer.cache_hit)
        delta = max(delta, difference(expected, fresh), difference(fresh, repeated))
        scorer.decide('{"count":2}', question)
        require(not scorer.cache_hit)
    require(delta <= 1e-5 and reference.missing == 0)
    report["jevk5_max_abs_difference"] = delta
    report["checks"]["jevk5_independent_upstream_parity"] = True
    report["checks"]["jevk5_fresh_repeat_changed_cache"] = True


async def verify(args, report):
    report["implementation_sha256"] = {
        relative: file_hash(ROOT / relative) for relative in (
            "backend/mcpme_textgen/adapter.py", "backend/mcpme_textgen/fixture.py",
            "scripts/verify_phase3_llm.py")}
    source = args.engine_build.resolve()
    if (source / "Contents/Resources").is_dir():
        source = source / "Contents/Resources"
    expected = json.loads((ROOT / "tests/fixtures/text_generation/engine-closure.json").read_text())
    require(file_hash(ROOT / "vendor/qwen2.5-0.5b/LICENSE") == LICENSE_SHA256)
    temporary = Path(tempfile.mkdtemp(prefix="mcpme-llm-compatibility-")).resolve()
    temporary.chmod(0o700)
    engine = None
    settled = True
    try:
        report["stage"] = "copy_exact_fixture"
        selected, relocated = temporary / "selected", temporary / "relocated private engine"
        for relative, identity in expected.items():
            copy_artifact(source / relative, selected / relative, limit=128 * 1024**2,
                role="runtime-file", expected_sha256=identity["sha256"], expected_size=identity["bytes"])
        copy_artifact(args.weights.resolve(), selected / MODEL_FILENAME, limit=MODEL_BYTES,
                      role="model-weights", expected_sha256=MODEL_SHA256, expected_size=MODEL_BYTES)
        shutil.move(str(selected), relocated)
        require(not selected.exists())
        for relative, identity in expected.items():
            require(file_hash(relocated / relative) == identity["sha256"])
        metadata = tokenizer_metadata(relocated / MODEL_FILENAME)
        require(digest(metadata) == TOKENIZER_SHA256)
        require(hashlib.sha256(metadata["tokenizer.chat_template"].encode()).hexdigest() == EMBEDDED_TEMPLATE_SHA256)
        require(metadata["tokenizer.ggml.eos_token_id"] == 151645)
        report["model_sha256"], report["engine_build"] = MODEL_SHA256, ENGINE_BUILD
        report["engine_closure_sha256"] = digest(expected)
        report["tokenizer_sha256"] = TOKENIZER_SHA256
        report["checks"]["pinned_relocated_closure"] = True
        report["stage"] = "actual_cpu_engine"
        engine = Engine(relocated, relocated / MODEL_FILENAME)
        settled = False
        await asyncio.to_thread(engine.start)
        await actual_text(engine, report)
        await engine.retire()
        settled = True
        if args.jevk5_weights:
            report["stage"] = "jevk5_independent_regression"
            identity = json.loads((ROOT / "model-manifest.json").read_text())
            weights = relocated / identity["file"]
            copy_artifact(args.jevk5_weights.resolve(), weights, limit=identity["bytes"],
                          role="model-weights", expected_sha256=identity["sha256"], expected_size=identity["bytes"])
            engine = Engine(relocated, weights, jevk5=True)
            settled = False
            await asyncio.to_thread(engine.start)
            await asyncio.to_thread(jevk5_parity, engine, report)
            await engine.retire()
            settled = True
        report["stage"] = "complete"
        report["a4_installed_gate_passed"] = False
    finally:
        if engine is not None and not settled:
            settled = await engine.retire()
        if settled:
            shutil.rmtree(temporary)
        report["ownership_settled"] = settled


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-build", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--jevk5-weights", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"stage": "start", "checks": {}}
    code = 0
    try:
        asyncio.run(verify(args, report))
    except BaseException as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, TextGenerationError):
            report["error_code"] = error.code
        code = 1
    serialized = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.write_text(serialized)
    print(serialized, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
