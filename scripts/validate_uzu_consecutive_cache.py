"""Untimed native acceptance for the isolated, opt-in consecutive score cache.

All content-bearing records are private. Historical validators and references
are imported unchanged and their complete helper identities are sealed.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile

import validate_uzu_sol as prior

base, original, deep, exact = prior.base, prior.original, prior.deep, prior.exact
ROOT, require = prior.ROOT, prior.require
PROJECT = ROOT / "experiments/uzu-rust/consecutive-cache"
PROTOCOL = base.EXPERIMENT / "CONSECUTIVE_CACHE_PROTOCOL.md"
BUILD = ROOT / "runtime/uzu-rust/consecutive-cache/builds/sol-cache-v2"
CACHE_POLICY = "consecutive-full-vocabulary-logits-v1"
CACHE_KEY = "exact-token-ids+context-tokens+model-instance"
RECORD_FILES = ("worker.json", "digest.json", "sequences.json")
OUTPUT_FIELDS = ("candidate_token_ids", "logits", "log_probabilities", "candidate_probabilities", "temperature")
SEQUENCE_NAMES = (
    "A_first", "A_repeat", "B_replace", "A_return", "tokenize_preserves", "A_after_tokenize",
    "context_changed", "context_repeat", "context_return", "candidate_order", "candidate_set",
    "temperature_changed", "malformed_request", "A_after_malformed", "invalid_score",
    "A_after_invalid_score", "invalid_tokenize", "A_after_invalid_tokenize",
    "malformed_tokenize", "A_after_malformed_tokenize",
)


class CacheExpectation:
    """A specification oracle keyed by exact tokens/context, never by case ID."""
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.key = None

    def reset(self):
        self.key = None

    def successful_score(self, request):
        key = (tuple(request["input_token_ids"]), request["context_tokens"])
        hit = self.enabled and self.key == key
        self.key = key if self.enabled else None
        return hit

    def error(self):
        self.reset()


def score_request(tokens, candidates, *, context=base.CONTEXT, temperature=base.TEMPERATURE):
    return {"operation": "score", "input_token_ids": list(tokens), "candidate_token_ids": list(candidates),
            "context_tokens": context, "temperature": temperature}


def validate_cache(value, expected_hit):
    require(type(value.get("cache_hit")) is bool and value["cache_hit"] is expected_hit,
            "cache_expectation_failed")
    telemetry = value.get("cache")
    require(type(telemetry) is dict and set(telemetry) == {"enabled", "hits", "native_score_calls"}
            and type(telemetry["enabled"]) is bool
            and all(type(telemetry[k]) is int and telemetry[k] >= 0 for k in ("hits", "native_score_calls")),
            "native_count_invalid")
    require(not expected_hit or telemetry["enabled"], "cache_disabled_hit")
    if "metrics" in value:
        metrics = value["metrics"]
        require(type(metrics) is dict and all(type(v) in (int, float) and math.isfinite(v) and v >= 0
                for v in metrics.values()), "native_metrics_invalid")
        if expected_hit:
            require(metrics.get("state_creation_seconds") == 0.0 and metrics.get("prefill_seconds") == 0.0,
                    "cache_hit_executed_native")


def validate_response(value, request, expected_hit):
    validate_cache(value, expected_hit)
    # The immutable helper requires fresh-state and the original temperature.
    # Its output/calibration checks remain applicable after explicitly checking
    # the real identity; variants are checked against a default-off native call.
    require(exact(value.get("temperature"), request["temperature"]), "temperature_identity_invalid")
    if exact(request["temperature"], base.TEMPERATURE):
        original.validate_response(dict(value, cache_hit=False), request["input_token_ids"],
                                   request["candidate_token_ids"])
    else:
        require(value.get("input_tokens") == len(request["input_token_ids"])
                and value.get("prompt_tokens_sha256") == base.token_digest(request["input_token_ids"])
                and value.get("candidate_token_ids") == request["candidate_token_ids"]
                and value.get("truncated") is False, "score_identity_invalid")
        for key in ("logits", "log_probabilities", "candidate_probabilities"):
            values = value.get(key)
            require(type(values) is list and len(values) == len(request["candidate_token_ids"])
                    and all(type(v) is float and math.isfinite(v) for v in values), "score_values_invalid")
        require(all(p >= 0 for p in value["candidate_probabilities"])
                and abs(sum(value["candidate_probabilities"]) - 1.0) <= base.FORMULA_TOLERANCE,
                "probabilities_invalid")


def validate_control(value, control, request, expected_hit):
    validate_response(value, request, expected_hit)
    validate_response(control, request, False)
    require(all(exact(value[k], control[k]) for k in OUTPUT_FIELDS), "native_control_equality_failed")


def validate_worker_row(row, case, expected, expected_hit):
    request = score_request(row["input_token_ids"], expected["candidate_token_ids"])
    validate_response(row["response"], request, expected_hit)
    adapted = dict(row, response=dict(row["response"], cache_hit=False))
    return prior.validate_worker_row(adapted, case, expected)


def validate_boundary(row, expected, expected_hit):
    validate_response(row["response"], score_request(row["tokens"], expected["response"]["candidate_token_ids"]),
                      expected_hit)
    return prior.validate_boundary(dict(row, response=dict(row["response"], cache_hit=False)), expected)


def validate_digest(value, request, expected, vocab_size, expected_hit):
    validate_cache(value, expected_hit)
    prior.validate_digest(dict(value, cache_hit=False), request["input_token_ids"], expected, vocab_size)


class PrivateClient(base.RustClient):
    def __init__(self, process, readiness):
        super().__init__(process, readiness)
        self.telemetry = CacheTelemetry(readiness["cache_enabled"])
        self.telemetry.validate(readiness, None)

    async def exchange(self, payload=None, *, raw=None):
        """Allow fixed error replies, while keeping all raw protocol data private."""
        process = self.process
        require(process.stdin is not None and process.stdout is not None and process.returncode is None
                and not process.ownership_lost, "native_io_unavailable")
        async with asyncio.timeout(240):
            process.stdin.write(raw if raw is not None else base.encoded(payload))
            await process.stdin.drain()
            reply = await process.stdout.readline()
        require(reply and len(reply) < 1024 * 1024, "native_response_invalid")
        value = json.loads(reply)
        require(type(value) is dict, "native_response_invalid")
        self.telemetry.validate(value, payload)
        return value

    async def post(self, payload):
        value = await self.exchange(payload)
        require("error" not in value, "native_inference_failed")
        return value

    async def reset(self):
        value = await self.post({"operation": "reset_cache"})
        require(set(value) == {"cache_reset", "cache"} and value["cache_reset"] is True, "cache_reset_failed")


class CacheTelemetry:
    """Validate actual cumulative native-call/hit counters for one process."""
    def __init__(self, enabled):
        self.enabled, self.hits, self.calls = enabled, 0, 0

    def validate(self, value, request):
        counters = value.get("cache")
        require(type(counters) is dict and set(counters) == {"enabled", "hits", "native_score_calls"}
                and type(counters["enabled"]) is bool
                and all(type(counters[k]) is int and counters[k] >= 0 for k in ("hits", "native_score_calls")),
                "cache_telemetry_invalid")
        if request and request.get("operation") == "score" and "error" not in value:
            require(type(value.get("cache_hit")) is bool, "cache_hit_invalid")
            self.hits += int(value["cache_hit"])
            self.calls += int(not value["cache_hit"])
        require(value.get("cache") == {"enabled": self.enabled, "hits": self.hits,
                                       "native_score_calls": self.calls}, "cache_telemetry_invalid")


def validate_readiness(ready, *, enabled, digest=False):
    require(ready.get("ready") is True
            and ready.get("mode") == ("exact-token-digest-cache-v1" if digest else "exact-token-scoring-cache-v1")
            and ready.get("cache_policy") == (CACHE_POLICY if enabled else "fresh-state")
            and type(ready.get("cache_enabled")) is bool and ready["cache_enabled"] is enabled
            and ready.get("cache_key") == CACHE_KEY
            and ready.get("upstream_revision") == original.REVISION
            and ready.get("scoring_patch_sha256") == base.file_hash(ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch")
            and type(ready.get("vocab_size")) is int and ready["vocab_size"] > 0, "native_readiness_invalid")
    CacheTelemetry(enabled).validate(ready, None)


async def launch(owner, binary, model, scratch, *, enabled, digest=False):
    env = {"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin", "LANG": "C", "RUST_LOG": "off"}
    arguments = ["--score-model-path", str(model)] + (["--consecutive-cache"] if enabled else [])
    process = await owner.launch(Path(binary), arguments, ROOT, env, io_mode="streams",
                                 owned_group=True, stop_grace=.5)
    try:
        require(process.stdout is not None, "native_io_unavailable")
        async with asyncio.timeout(240):
            raw = await process.stdout.readline()
        require(raw and len(raw) < 1024 * 1024, "native_readiness_invalid")
        ready = json.loads(raw)
        validate_readiness(ready, enabled=enabled, digest=digest)
        return process, PrivateClient(process, ready)
    except BaseException:
        await process.stop()
        raise


def assert_settled(owner, process):
    require(not process.ownership_lost and not owner.instances, "ownership_unsettled")


async def score(client, request):
    return await client.post(request)


async def pair(client, request, expected, *, case=None, boundary=None, vocab_size=None):
    await client.reset()
    oracle = CacheExpectation()
    rows = []
    for offset, condition in enumerate(("fresh", "identical_repeat")):
        value = await score(client, request)
        hit = oracle.successful_score(request)
        if case is not None:
            row = {"case_id": case["id"], "condition": condition, "input_token_ids": request["input_token_ids"],
                   "response": value, "decision": base.typed_decision(case["question"],
                       value["candidate_probabilities"], len(request["input_token_ids"]))}
            validate_worker_row(row, case, expected[offset], hit)
        elif boundary is not None:
            row = {**{k: boundary[k] for k in ("length", "kind", "tokens")}, "condition": condition,
                   "response": value}
            validate_boundary(row, expected, hit)
        else:
            validate_digest(value, request, expected, vocab_size, hit)
            row = value
        rows.append(row)
    return rows


def sequence_plan(tokens, candidates):
    a = list(tokens)
    b = list(tokens)
    b[-1] = next(token for token in candidates if token != b[-1])
    selected = list(candidates[:2])
    request = score_request(a, selected)
    context = max(len(a), base.CONTEXT - 1)
    require(context != base.CONTEXT, "sequence_context_invalid")
    entries = [
        ("A_first", request), ("A_repeat", request), ("B_replace", score_request(b, selected)),
        ("A_return", request),
        ("tokenize_preserves", {"operation": "tokenize", "text": "A", "add_special_tokens": False}),
        ("A_after_tokenize", request),
        ("context_changed", score_request(a, selected, context=context)),
        ("context_repeat", score_request(a, selected, context=context)), ("context_return", request),
        ("candidate_order", score_request(a, list(reversed(selected)))),
        ("candidate_set", score_request(a, list(candidates[2:4]))),
        ("temperature_changed", score_request(a, selected, temperature=base.TEMPERATURE / 2)),
        ("malformed_request", None), ("A_after_malformed", request),
        ("invalid_score", score_request(a, [selected[0], selected[0]])), ("A_after_invalid_score", request),
        ("invalid_tokenize", {"operation": "tokenize", "text": "", "add_special_tokens": False}),
        ("A_after_invalid_tokenize", request),
        ("malformed_tokenize", {"operation": "tokenize", "text": "A", "unexpected": True}),
        ("A_after_malformed_tokenize", request),
    ]
    return [{"name": name, "request": item} for name, item in entries]


ERROR_NAMES = {"malformed_request": "invalid_request", "invalid_score": "invalid_scoring_request",
               "invalid_tokenize": "invalid_tokenization_request", "malformed_tokenize": "invalid_request"}


def validate_sequence_rows(rows, plan):
    require(len(rows) == len(plan) and [row["name"] for row in rows] == list(SEQUENCE_NAMES),
            "sequence_incomplete")
    oracle = CacheExpectation()
    telemetry = CacheTelemetry(True)
    for row, item in zip(rows, plan, strict=True):
        require(exact(row.get("request"), item["request"]), "sequence_request_invalid")
        telemetry.validate(row["response"], item["request"])
        if row["name"] in ERROR_NAMES:
            require(set(row["response"]) == {"error", "cache"}
                    and row["response"]["error"] == ERROR_NAMES[row["name"]], "expected_error_invalid")
            require("control" not in row, "error_control_invalid")
            oracle.error()
        elif item["request"]["operation"] == "tokenize":
            value = row["response"]
            require(type(value.get("token_ids")) is list and value.get("token_count") == len(value["token_ids"])
                    and value.get("token_ids_sha256") == base.token_digest(value["token_ids"]), "tokenization_invalid")
        else:
            hit = oracle.successful_score(item["request"])
            validate_control(row["response"], row["control"], item["request"], hit)


def counts(responses):
    values = list(responses)
    return {"score_responses": len(values), "cache_hits": sum(value["cache_hit"] for value in values),
            "native_score_calls": values[-1]["cache"]["native_score_calls"] if values else 0}


def configuration(args):
    reference_config, cases, worker, digest, reference_paths = prior.reference_data(args.reference)
    require(str(args.model) == reference_config["model"], "model_reference_mismatch")
    receipt, candidate_paths = candidate_identity(args.candidate_binary, args.candidate_digest_binary,
                                                  args.candidate_receipt)
    helpers = [Path(__file__), Path(prior.__file__), Path(prior.sol.__file__), Path(deep.__file__),
        Path(original.__file__), Path(prior.latency.__file__), Path(base.__file__),
        ROOT / "scripts/benchmark_jev_optimized.py", ROOT / "scripts/audit_uzu_sol.py",
        ROOT / "scripts/audit_uzu_consecutive_cache.py",
        ROOT / "tests/test_validate_uzu_consecutive_cache.py",
        ROOT / "tests/test_audit_uzu_consecutive_cache.py",
        ROOT / "scripts/benchmark_uzu.py", ROOT / "scripts/acquire_jev_uzu_models.py",
        ROOT / "backend/meecp/processes.py", ROOT / "vendor/jevk5/prompt.py", PROTOCOL, args.reaper,
        base.EXPERIMENT / "DATASET_PROVENANCE.json"]
    files = dict(reference_config["files"])
    files.update({str(deep.ordinary_file(path)): base.file_hash(path)
                  for path in [*candidate_paths, *reference_paths, *original.model_identity(args.model), *helpers]})
    config = {"schema_version": 1, "mode": "untimed-consecutive-cache-acceptance", "files": files,
        "bound_helper_sha256": {str(path.relative_to(ROOT)): base.file_hash(path) for path in helpers if path.is_relative_to(ROOT)},
        "candidate_receipt": receipt, "candidate_binary": str(args.candidate_binary),
        "candidate_digest_binary": str(args.candidate_digest_binary), "reference": str(args.reference),
        "model": str(args.model), "case_ids": [case["id"] for case in cases],
        "warmups_per_process": 3, "reset_after_warmups": True, "reset_before_reference_pair": True,
        "context_tokens": base.CONTEXT, "temperature": base.TEMPERATURE,
        "case_responses": 486, "boundary_responses": 96, "digest_responses": 1068,
        "sequence_names": list(SEQUENCE_NAMES), "timing_deferred": True, "timing_not_reported": True}
    return config, cases, worker, digest


def candidate_identity(binary, probe, receipt_path):
    builder_path = ROOT / "experiments/uzu-rust/build_consecutive_cache_scoring.py"
    specification = importlib.util.spec_from_file_location("consecutive_cache_build_identity", builder_path)
    builder = importlib.util.module_from_spec(specification)
    sys.path.insert(0, str(builder_path.parent))
    try:
        specification.loader.exec_module(builder)
        binding = builder.candidate_identity(receipt_path.parent)
    finally:
        sys.path.pop(0)
    require(Path(binary) == receipt_path.parent / "bin/meecp-uzu-cache-worker"
            and Path(probe) == receipt_path.parent / "bin/meecp-uzu-cache-score-digest", "binary_receipt_location_invalid")
    receipt = json.loads(receipt_path.read_bytes())
    paths = [binary, probe, receipt_path, receipt_path.parent / "UNIT_TESTS.json", builder_path,
             receipt_path.parent / "project/Cargo.toml", receipt_path.parent / "project/Cargo.lock"]
    for group in ("source_bindings", "prepared_worker_sources", "prepared_upstream_sources"):
        paths.extend(ROOT / name for name in binding[group])
    control = ROOT / binding["sol_control"]["path"]
    paths.extend([control / "BUILD_RECEIPT.json", control / "bin/meecp-uzu-worker",
                  control / "bin/meecp-uzu-score-digest"])
    require(base.file_hash(binary) == binding["binary_sha256"]
            and base.file_hash(probe) == binding["digest_binary_sha256"], "cache_binary_identity_invalid")
    return receipt, paths


async def run(args):
    config, cases, reference, reference_digest = configuration(args)
    directory = base.secure_directory(args.output)
    require(not list(directory.iterdir()), "run_directory_not_empty")
    digest = base.sha(base.encoded(config))
    base.private_json(directory / "configuration.json", config)
    base.private_json(directory / "artifact-snapshot.json", {"files": config["files"]})
    base.private_json(directory / "pre-run-seal.json", {"configuration_sha256": digest,
        "artifact_snapshot_sha256": base.file_hash(directory / "artifact-snapshot.json")})
    owner = base.ProcessOwner(args.reaper)
    common = {"complete": True, "configuration_sha256": digest, "ownership_settled": True}
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-uzu-cache-validation-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            process, client = await launch(owner, args.candidate_binary, args.model, scratch, enabled=True)
            try:
                candidates = []
                for letter in base.LETTERS:
                    ids = await prior.tokenize(client, letter)
                    require(len(ids) == 1, "candidate_not_single_token")
                    candidates.append(ids[0])
                require(len(set(candidates)) == 16, "candidate_duplicate")
                first_tokens = await prior.tokenize(client, base.rendered(cases[0]))
                first_request = score_request(first_tokens, candidates[:len(base.canonical_option_keys(cases[0]["question"]))])
                warmups = []
                oracle = CacheExpectation()
                for _ in range(3):
                    value = await client.post(first_request)
                    row = {"case_id": cases[0]["id"], "condition": "warmup", "input_token_ids": first_tokens,
                           "response": value, "decision": base.typed_decision(cases[0]["question"],
                               value["candidate_probabilities"], len(first_tokens))}
                    validate_worker_row(row, cases[0], reference["rows"][0], oracle.successful_score(first_request))
                    warmups.append(row)
                await client.reset()
                rows = []
                for index, case in enumerate(cases):
                    tokens = await prior.tokenize(client, base.rendered(case))
                    request = score_request(tokens, candidates[:len(base.canonical_option_keys(case["question"]))])
                    rows.extend(await pair(client, request, reference["rows"][index * 2:index * 2 + 2], case=case))
                    if (index + 1) % 20 == 0:
                        print(json.dumps({"stage": "cache_worker_validation", "cases": index + 1}), flush=True)
                boundaries = []
                for expected in reference["boundaries"]:
                    request = score_request(expected["tokens"], expected["response"]["candidate_token_ids"])
                    boundaries.extend(await pair(client, request, expected, boundary=expected))
            finally:
                await process.stop()
            assert_settled(owner, process)
            base.private_json(directory / "worker.json", {**common, "readiness": client.readiness,
                "warmups": warmups, "rows": rows, "boundaries": boundaries})
            print(json.dumps({"stage": "cache_worker_complete", "case_responses": len(rows),
                              "boundary_responses": len(boundaries)}), flush=True)
            process, client = await launch(owner, args.candidate_digest_binary, args.model, scratch,
                                           enabled=True, digest=True)
            try:
                requests = [score_request(tokens, selected) for tokens, selected in prior.requests(reference)]
                warmups, outputs = [], []
                oracle = CacheExpectation()
                for _ in range(3):
                    value = await client.post(requests[0])
                    validate_digest(value, requests[0], reference_digest["outputs"][0], client.readiness["vocab_size"],
                                    oracle.successful_score(requests[0]))
                    warmups.append(value)
                await client.reset()
                for index, (request, expected) in enumerate(zip(requests, reference_digest["outputs"], strict=True)):
                    outputs.extend(await pair(client, request, expected, vocab_size=client.readiness["vocab_size"]))
                    if (index + 1) % 40 == 0:
                        print(json.dumps({"stage": "cache_digest_validation", "reference_requests": index + 1}), flush=True)
            finally:
                await process.stop()
            assert_settled(owner, process)
            base.private_json(directory / "digest.json", {**common, "readiness": client.readiness,
                "warmups": warmups, "outputs": outputs})
            sequences = await run_sequences(owner, args, scratch, first_tokens, candidates)
            base.private_json(directory / "sequences.json", {**common, **sequences})
        original.verify_files(config)
        base.private_json(directory / "post-run-seal.json", {"configuration_sha256": digest,
            "artifact_snapshot_sha256": base.file_hash(directory / "artifact-snapshot.json"),
            "ownership_settled": not owner.instances,
            "records": {name: base.file_hash(directory / name) for name in RECORD_FILES}})
        print(json.dumps({"stage": "cache_validation_complete", "case_responses": 486,
                          "boundary_responses": 96, "digest_responses": 1068, "timing_deferred": True}), flush=True)
    finally:
        await owner.close()


async def run_sequences(owner, args, scratch, tokens, candidates):
    # Processes remain strictly serial. Collect fresh-state native controls first.
    plan = sequence_plan(tokens, candidates)
    process, control = await launch(owner, args.candidate_binary, args.model, scratch, enabled=False)
    try:
        controls = {}
        request = score_request(tokens, candidates[:2])
        default_off = [await control.post(request) for _ in range(2)]
        for value in default_off:
            validate_response(value, request, False)
        require(all(exact(default_off[0][key], default_off[1][key]) for key in OUTPUT_FIELDS), "default_off_equality_failed")
        for item in plan:
            if item["name"] not in ERROR_NAMES and item["request"]["operation"] == "score":
                controls[item["name"]] = await control.post(item["request"])
    finally:
        await process.stop()
    assert_settled(owner, process)
    default_ready = control.readiness
    process, client = await launch(owner, args.candidate_binary, args.model, scratch, enabled=True)
    try:
        await client.reset()
        rows = []
        for item in plan:
            row = dict(item)
            if item["name"] == "malformed_request":
                row["response"] = await client.exchange(raw=b"{\n")
            elif item["name"] in ERROR_NAMES:
                row["response"] = await client.exchange(item["request"])
            else:
                row["response"] = await client.post(item["request"])
                if item["request"]["operation"] == "score":
                    row["control"] = controls[item["name"]]
            rows.append(row)
        validate_sequence_rows(rows, plan)
        # Observe clean EOF, not merely a stop signal, before restarting.
        require(process.stdin is not None, "native_io_unavailable")
        async with asyncio.timeout(30):
            process.stdin.close()
            await process.stdin.wait_closed()
            code = await process.wait()
        require(code == 0, "native_eof_failed")
    finally:
        await process.stop()
    assert_settled(owner, process)
    ready = client.readiness
    process, restarted = await launch(owner, args.candidate_binary, args.model, scratch, enabled=True)
    try:
        restart_response = await restarted.post(request)
        validate_control(restart_response, default_off[0], request, False)
    finally:
        await process.stop()
    assert_settled(owner, process)
    return {"readiness": ready, "tokens": tokens, "candidates": candidates, "rows": rows,
            "default_off": {"readiness": default_ready, "request": request, "responses": default_off},
            "eof_exit_code": 0, "restart": {"readiness": restarted.readiness, "request": request,
                                            "response": restart_response}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-binary", type=Path, default=BUILD / "bin/meecp-uzu-cache-worker")
    parser.add_argument("--candidate-digest-binary", type=Path, default=BUILD / "bin/meecp-uzu-cache-score-digest")
    parser.add_argument("--candidate-receipt", type=Path, default=BUILD / "BUILD_RECEIPT.json")
    parser.add_argument("--model", type=Path, default=ROOT / "models/uzu-jevk5/bf16")
    parser.add_argument("--reference", type=Path, default=ROOT / "test-artifacts/uzu-optimization-combined")
    parser.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    args = parser.parse_args()
    for key, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, key, value.absolute())
    try:
        asyncio.run(run(args))
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "consecutive_cache_validation_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
