"""Installed Stark JevK5 acceptance and a short native-before/host-after snapshot.

This command installs a prepared ``local.jevk5.stark`` package through the real
Supervisor into a fresh private state directory. The portable frozen reference
is read in memory. Requests, replies, tokens, probability vectors, credentials,
private absolute paths and exception text are never saved or printed. The host's
normal owner-only connection/configuration files remain necessary runtime state.

Frozen scope: 243 tokenize checks, 243 private score cold/repeat pairs, 243
public API cold/repeat pairs, 48 private boundary pairs, twelve HTTP MCP and
twelve stdio SDK decisions, transport rejection checks, bounded concurrent
admission and SDK client-wait cancellation with preserved server execution,
stop/restart and owner/resource settlement. Default-off
repeats must remain misses; actual hit acceptance requires an explicitly enabled
package and ``--expect-cache enabled``. This is native Stark preservation, not
llama.cpp equivalence. Full-vocabulary digest acceptance is a separate native
dependency gate and is not claimed by these candidate-field integration checks.

The twelve timing cases use the existing length-only selection rule: four
midpoint quartiles in each of [0,256), [256,1024), [1024,4096). One native-before
block followed by API-after and MCP-after blocks provides a simple short
snapshot; its order, sample size and uncontrolled background activity preclude
a precise performance or quality claim. Startup is reported separately.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import platform
import statistics
import struct
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
logging.disable(logging.CRITICAL)
for directory in (ROOT / "backend", ROOT / "vendor", ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

CONTEXT = 8192
TEMPERATURE = 1.22
PROFILE = "jevk5-stark-bf16-v1"
PACKAGE_ID = "local.jevk5.stark"
BUCKETS = (("short", 0, 256), ("medium", 256, 1024), ("long", 1024, 4096))
CONDITIONS = ("fresh", "identical_repeat")
FIELDS = ("candidate_token_ids", "logits", "log_probabilities", "candidate_probabilities", "temperature")
LIMIT = 1024 * 1024
FAILURE_CODES = frozenset("""
apple_silicon_required arguments_invalid artifact_file_invalid
boundary_cache_expectation_failed bundled_interpreter_required
cache_expectation_failed cache_metadata_invalid calibration_formula_failed
cancellation_not_observed error_cache_invalidation_failed final_ownership_unsettled
host_directory_invalid host_runtime_missing http_status_invalid integration_failed
installed_native_cache_expectation_failed installed_package_failed
installed_snapshot_invalid installed_status_invalid installed_tokenization_equality_failed
installed_tools_changed invalid_question_rejection_failed llama_candidate_not_single_token
llama_control_arguments_invalid llama_control_binding_changed llama_control_identity_invalid
llama_ownership_unsettled llama_repeat_equality_failed llama_request_identity_mismatch
llama_runtime_invalid llama_shared_checkpoint_invalid logits_invalid native_build_changed
native_cache_expectation_failed native_descendants_unobserved native_validator_identity_invalid
native_validator_unavailable package_cache_policy_mismatch package_profile_invalid
package_worker_mismatch private_auth_rejection_failed private_directory_exists
private_error_schema_invalid private_health_invalid private_path_invalid private_rejection_failed
probabilities_invalid process_inventory_invalid process_inventory_unavailable
public_context_rejection_failed queue_not_saturated queue_rejection_invalid queue_response_invalid
queue_engine_identity_changed queue_execution_not_preserved
renderer_reference_mismatch report_path_invalid reset_ack_invalid resident_identity_invalid
response_limit_invalid response_schema_invalid restart_generation_invalid restart_reaping_failed
result_identity_invalid sdk_capabilities_invalid sdk_decision_failed sdk_status_invalid
selection_bucket_too_small selection_not_unique slot_ownership_unsettled slot_resource_unsettled
source_binding_changed source_package_changed stop_reaping_failed timing_blocks_invalid
timing_population_invalid timing_values_invalid transport_rejection_failed
typed_answer_bit_equality_failed typed_reference_invalid
""".split())
STAGES = frozenset("""
arguments runtime_binding package_reference_binding source_binding native_before llama_before
supervisor_install installed_health private_native api transport_rejections api_timing
http_mcp_sdk http_mcp_timing queue_and_cancellation stdio_mcp_sdk timing_aggregation
lifecycle_stop lifecycle_start lifecycle_restart ownership_verification source_verification
report_write complete
""".split())
CURRENT_STAGE = "arguments"


class IntegrationFailure(Exception):
    """Only authored, content-free failure codes escape the runner."""

    def __init__(self, code):
        # The error class must not become a channel for input-derived text.
        self.code = code if type(code) is str and code in FAILURE_CODES else "integration_failed"
        super().__init__(self.code)


def sanitized_failure_code(error):
    """Find only our allowlisted code through native TaskGroup/SDK wrappers."""
    pending = [error]
    for _ in range(1024):
        if not pending:
            break
        current = pending.pop()
        if type(current) is IntegrationFailure and current.code in FAILURE_CODES:
            return current.code
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
    return "integration_failed"


def set_stage(stage):
    global CURRENT_STAGE
    # Call sites pass fixed labels. Unknown or caller-derived values are never
    # printed, including when a private debug caller uses this helper.
    CURRENT_STAGE = stage if type(stage) is str and stage in STAGES else "arguments"


def require(condition, code):
    if not condition:
        raise IntegrationFailure(code)


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise IntegrationFailure("arguments_invalid")


def exact(left, right):
    """Compare decoded types and f64 bits, including signed zero."""
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return struct.pack("!d", left) == struct.pack("!d", right)
    if isinstance(left, list):
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(exact(left[key], right[key]) for key in left)
    return left == right


def file_hash(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "artifact_file_invalid")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def new_private_directory(path):
    path = Path(path).absolute()
    require(".." not in path.parts and not path.exists(), "private_directory_exists")
    require(not any(item.is_symlink() for item in (path, *path.parents)), "private_path_invalid")
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def write_report(path, report):
    # Only the deliberately assembled scalar report reaches disk.
    path = Path(path).absolute()
    require(not path.exists() and not any(item.is_symlink() for item in (path, *path.parents)), "report_path_invalid")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(report))
        stream.flush()
        os.fsync(stream.fileno())


def calibration_error(logits, probabilities, temperature=TEMPERATURE):
    require(type(logits) is list and 2 <= len(logits) <= 16
            and all(type(item) is float and math.isfinite(item) for item in logits), "logits_invalid")
    require(type(probabilities) is list and len(probabilities) == len(logits)
            and all(type(item) is float and math.isfinite(item) and 0 <= item <= 1 for item in probabilities)
            and abs(math.fsum(probabilities) - 1) <= 1e-12, "probabilities_invalid")
    weights = [math.exp((item - max(logits)) / temperature) for item in logits]
    total = math.fsum(weights)
    error = max(abs(probability - weight / total) for probability, weight in zip(probabilities, weights, strict=True))
    require(error <= 1e-12, "calibration_formula_failed")
    return error


def selected_cases(cases, rows):
    """The frozen length-only rule; selection never depends on latency/quality."""
    fresh = {row["case_id"]: row for row in rows if row["condition"] == "fresh"}
    chosen = []
    for bucket, lower, upper in BUCKETS:
        eligible = [index for index, case in enumerate(cases) if case["tier"] != "synthetic"
                    and lower <= fresh[case["id"]]["input_tokens"] < upper]
        eligible.sort(key=lambda index: fresh[cases[index]["id"]]["input_tokens"])
        require(len(eligible) >= 4, "selection_bucket_too_small")
        quartiles = [(2 * index + 1) * len(eligible) // 8 for index in range(4)]
        require(len(set(quartiles)) == 4, "selection_not_unique")
        chosen.extend({"index": eligible[index], "bucket": bucket,
                       "input_tokens": fresh[cases[eligible[index]]["id"]]["input_tokens"]} for index in quartiles)
    return chosen


def distribution(values):
    require(values and all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in values),
            "timing_values_invalid")
    ordered = sorted(values)
    return {"samples": len(values), "mean_ms": statistics.mean(values), "p50_ms": statistics.median(values),
            "p95_ms": ordered[math.ceil(.95 * len(ordered)) - 1], "minimum_ms": ordered[0], "maximum_ms": ordered[-1]}


def timing_summary(blocks):
    require(set(blocks) in ({"native_before", "api_after", "mcp_after"},
                           {"native_before", "llama_before", "api_after", "mcp_after"}), "timing_blocks_invalid")
    result = {}
    for condition in CONDITIONS:
        groups = {name: [row["wall_ms"] for row in rows if row["condition"] == condition]
                  for name, rows in blocks.items()}
        require(all(len(values) == 12 for values in groups.values()), "timing_population_invalid")
        result[condition] = {name: distribution(values) for name, values in groups.items()}
        for after in ("api_after", "mcp_after"):
            for before in ("native_before", "llama_before"):
                if before in groups:
                    result[condition][after]["after_over_" + before + "_ratio_of_means"] = (
                        statistics.mean(groups[after]) / statistics.mean(groups[before])
                        if statistics.mean(groups[before]) > 0 else None)
    return {"mode": "short-sequential-before-after-snapshot", "order": list(blocks), "selected_cases": 12,
            "warmups_per_block": 3, "precise_performance_claim": False,
            "background_activity_controlled": False,
            "measurement_scope": {"native_before": "Exact-token native scoring IPC; excludes rendering/tokenization.",
                "llama_before": "Vendored rendering, tokenization, native one-token letter readout and typed answer.",
                "api_after": "Prepared package rendering, tokenization, native scoring, typed answer and authenticated HTTP.",
                "mcp_after": "API-equivalent decision through an initialized official SDK HTTP MCP session."},
            "results": result}


def host_paths(host):
    host = Path(host).absolute()
    if host.suffix == ".app":
        host /= "Contents/Resources/host"
    elif (host / "host/python").is_dir():
        host /= "host"
    require(host.is_dir() and not host.is_symlink(), "host_directory_invalid")
    python = (host / "python/bin/python3").resolve()
    reaper = host / "bin/meecp-reap"
    if not reaper.is_file():
        reaper = host / "native/meecp-reap"
    require(python.is_file() and reaper.is_file() and not reaper.is_symlink(), "host_runtime_missing")
    require(Path(sys.executable).resolve() == python, "bundled_interpreter_required")
    return host, python, reaper


def validation_module(build, lock):
    import stark_dependency as dependency
    receipt = dependency.verify_build(build, lock)
    require(receipt.get("validation_tools_match_lock") is True, "native_validator_identity_invalid")
    source = build / "source/scripts"
    for name, expected in dependency.load_lock(lock)["validation_tools_sha256"].items():
        require(file_hash(build / "source" / name) == expected, "native_validator_identity_invalid")
    # The validator and its minimal fixture/process dependencies are admitted by
    # the build lock. Import their source snapshot rather than a mutable sibling.
    for name in ("scoring_fixture", "scoring_process", "validate_scoring_rename"):
        spec = importlib.util.spec_from_file_location(name, source / (name + ".py"))
        require(spec is not None and spec.loader is not None, "native_validator_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules["validate_scoring_rename"], receipt


def package_and_reference(args):
    from meecp.contracts import validate_package
    package = validate_package(args.package)
    model = package.manifest.model
    require(package.manifest.package_id == PACKAGE_ID and package.manifest.profile == "model-service"
            and model is not None and model.profile == PROFILE and model.context_tokens == CONTEXT
            and model.parameters.get("temperature") == TEMPERATURE
            and model.parameters.get("knockout_temperature") == .93
            and model.engine_identity.get("name") == "stark", "package_profile_invalid")
    native, receipt = validation_module(args.build, args.lock)
    model_directory = package.root.joinpath(model.artifact).parent
    native_args = SimpleNamespace(reference_directory=args.reference_directory, model=model_directory,
                                  worker=args.build / "bin/stark-score", probe=args.build / "bin/stark-score-digest",
                                  reaper=args.native_reaper)
    reference = native.load_reference(native_args)
    packed_workers = [package.root / artifact.path for artifact in package.lock.artifacts
                      if Path(artifact.path).name == "stark-score"]
    require(len(packed_workers) == 1 and file_hash(packed_workers[0]) == file_hash(native_args.worker),
            "package_worker_mismatch")
    from jevk5.prompt import answer, decision_options, prompt_text
    for case, row in zip(reference["cases"], reference["worker"]["rows"][::2], strict=True):
        options = decision_options(case["question"])
        rendered = prompt_text(case["state"], case["question"]["instructions"], [value for _, value in options])
        require(rendered == native.prompt.prompt_text(case["state"], case["question"]["instructions"],
                                                    [value for _, value in options]), "renderer_reference_mismatch")
        require(exact(answer(case["question"], dict(zip((key for key, _ in options), row["probabilities"], strict=True)),
                             row["input_tokens"]), row["decision"]), "typed_reference_invalid")
        calibration_error(row["logits"], row["probabilities"])
    return package, native, native_args, reference, receipt


def source_bindings(args, package, host, python, reaper, reference):
    paths = {"harness": Path(__file__), "dependency_helper": ROOT / "scripts/stark_dependency.py",
             "package_manifest": package.root / package.manifest_name, "package_lock": package.root / package.lock_name,
             "host_python": python, "host_reaper": reaper, "native_reaper": args.native_reaper,
             "dependency_lock": args.lock, "native_build_receipt": args.build / "BUILD_RECEIPT.json",
             "native_worker": args.build / "bin/stark-score", "native_probe": args.build / "bin/stark-score-digest",
             "reference_manifest": args.reference_directory / "MANIFEST.json",
             "reference": args.reference_directory / "REFERENCE.json"}
    bindings = {name: file_hash(path) for name, path in paths.items()}
    consumers = [ROOT / "vendor/jevk5/prompt.py", *sorted((ROOT / "backend/meecp_jevk5").glob("*.py")),
                 *sorted((ROOT / "backend/meecp_stark").glob("*.py")),
                 *sorted((ROOT / "backend/jevk5_app").glob("*.py")), ROOT / "backend/meecp/supervisor.py",
                 ROOT / "backend/meecp/gateway.py", ROOT / "backend/meecp/http_gateway.py",
                 ROOT / "backend/meecp/http_service.py", ROOT / "backend/meecp/processes.py",
                 ROOT / "backend/meecp/bridge.py", ROOT / "backend/meecp/worker.py",
                 ROOT / "scripts/prepare_stark_jevk5_package.py",
                 ROOT / "templates/jevk5-stark-v1/prepare.py",
                 ROOT / "templates/jevk5-stark-v1/launcher.c",
                 ROOT / "templates/jevk5-stark-v1/THIRD_PARTY_NOTICES.md",
                 ROOT / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json",
                 *([ROOT / "experiments/uzu-rust/jev-scoring/BF16_CONTROL_PROVENANCE.json"]
                   if args.llama_binary is not None else []),
                 ROOT / "tests/test_stark_jevk5_integration_harness.py"]
    bindings["consumer_source"] = {str(path.relative_to(ROOT)): file_hash(path) for path in consumers}
    bindings["model_files"] = {str(Path(path).relative_to(package.root)): digest
                              for path, digest in reference["files"].items() if Path(path).is_relative_to(package.root)}
    return bindings


def llama_identity(args, package):
    """Bind the optional BF16 GGUF to the same pre-conversion source weights."""
    if args.llama_binary is None and args.llama_gguf is None:
        return None
    require(args.llama_binary is not None and args.llama_gguf is not None, "llama_control_arguments_invalid")
    model_manifest = ROOT / "experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json"
    control_manifest = ROOT / "experiments/uzu-rust/jev-scoring/BF16_CONTROL_PROVENANCE.json"
    model = json.loads(model_manifest.read_bytes())
    control = json.loads(control_manifest.read_bytes())
    reference = control.get("llama_reference", {})
    conversion = control.get("llama_conversion", {})
    source_hash = model.get("conversion", {}).get("source_weights_sha256")
    require(control.get("schema_version") == 1 and control.get("profile") == "bf16-same-checkpoint"
            and control.get("uzu_model_manifest_sha256") == file_hash(model_manifest)
            and control.get("source") == model.get("source")
            and control.get("calibration") == {"temperature": TEMPERATURE, "knockout_temperature": .93}
            and reference.get("dtype") == "bfloat16" and reference.get("quantization") == "none"
            and source_hash is not None and reference.get("source_weights_sha256") == source_hash
            and conversion.get("source_weights_sha256") == source_hash and conversion.get("status") == "complete",
            "llama_control_identity_invalid")
    exported = {record["name"]: record["sha256"] for record in model.get("conversion", {}).get("files", [])}
    require(exported.get("model.safetensors") == package.manifest.model.sha256
            and args.llama_gguf.stat().st_size == reference.get("bytes")
            and file_hash(args.llama_gguf) == reference.get("sha256"), "llama_shared_checkpoint_invalid")
    closure = {str(path.relative_to(args.llama_binary.parent)): file_hash(path)
               for path in sorted(args.llama_binary.parent.rglob("*"))
               if path.is_file() and (path == args.llama_binary or path.suffix in {".dylib", ".so", ".metallib"})}
    require(closure and args.llama_binary.name in closure, "llama_runtime_invalid")
    return {"model_manifest_sha256": file_hash(model_manifest), "bf16_control_manifest_sha256": file_hash(control_manifest),
            "gguf_sha256": reference["sha256"], "source_weights_sha256": source_hash,
            "runtime_closure_sha256": closure, "n_probs": 40,
            "harness_sha256": file_hash(ROOT / "scripts/benchmark_jev_uzu.py"),
            "launch_helpers_sha256": file_hash(ROOT / "scripts/benchmark_uzu.py")}


async def llama_before(args, reaper, reference, selected):
    import benchmark_jev_uzu as baseline
    from meecp.processes import ProcessOwner
    owner = ProcessOwner(reaper)
    measurements, differences = [], []
    typed_differences = 0
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-stark-llama-before-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            launch_args = SimpleNamespace(llama_binary=args.llama_binary, gguf=args.llama_gguf, n_probs=40)
            process, client, startup = await baseline.launch(owner, "llama.cpp", launch_args, scratch)
            try:
                candidates = []
                for letter in baseline.LETTERS:
                    tokens, _ = await baseline.tokenize("llama.cpp", client, letter, False)
                    require(len(tokens) == 1, "llama_candidate_not_single_token")
                    candidates.append(tokens[0])
                for _ in range(3):
                    await baseline.measure("llama.cpp", client, reference["cases"][selected[0]["index"]], None,
                                           candidates, launch_args, False)
                for selected_row in selected:
                    index = selected_row["index"]
                    expected = reference["worker"]["rows"][index * 2]
                    previous, fresh = None, None
                    for condition in CONDITIONS:
                        row, previous = await baseline.measure("llama.cpp", client, reference["cases"][index], previous,
                                                               candidates, launch_args, condition == "identical_repeat")
                        require(exact(row["input_token_ids"], expected["input_token_ids"])
                                and exact(row["candidate_token_ids"], expected["candidate_token_ids"]),
                                "llama_request_identity_mismatch")
                        if condition == "fresh":
                            fresh = row
                            differences.extend(abs(left - right) for left, right in zip(
                                row["probabilities"], expected["probabilities"], strict=True))
                            typed_differences += int(not exact(row["decision"], expected["decision"]))
                        else:
                            require(exact(row["probabilities"], fresh["probabilities"])
                                    and exact(row["decision"], fresh["decision"]), "llama_repeat_equality_failed")
                        measurements.append({"condition": condition, "wall_ms": row["wall_seconds"] * 1000})
            finally:
                await client.aclose()
                await process.stop()
            require(not process.ownership_lost and not owner.instances, "llama_ownership_unsettled")
    finally:
        await owner.close()
    return measurements, {"startup_ms": startup * 1000, "cases": 12, "responses": 24,
                          "same_input_and_candidate_tokens": True, "own_repeats_bit_exact": True,
                          "maximum_probability_difference_from_native_stark": max(differences),
                          "typed_answers_different_from_native_stark": typed_differences,
                          "native_stark_equality_required": False}


async def native_before(native, native_args, reference, selected, enabled):
    owner = native.ProcessOwner(native_args.reaper)
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-stark-before-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            started = time.perf_counter()
            process, client = await native.launch(owner, native_args, reference, scratch, enabled=enabled)
            startup_ms = (time.perf_counter() - started) * 1000
            try:
                first = reference["worker"]["rows"][selected[0]["index"] * 2]
                for _ in range(3):
                    await client.reset()
                    value = await client.post(native.score_request(first["input_token_ids"], first["candidate_token_ids"]))
                    native.validate_score(value, native.score_request(first["input_token_ids"], first["candidate_token_ids"]),
                                          native.row_expected(first), hit=False)
                for selected_row in selected:
                    expected = reference["worker"]["rows"][selected_row["index"] * 2]
                    request = native.score_request(expected["input_token_ids"], expected["candidate_token_ids"])
                    await client.reset()
                    for condition in CONDITIONS:
                        started = time.perf_counter()
                        value = await client.post(request)
                        wall_ms = (time.perf_counter() - started) * 1000
                        hit = enabled and condition == "identical_repeat"
                        native.validate_score(value, request, native.row_expected(expected), hit=hit)
                        require(value.get("cache_hit") is hit, "native_cache_expectation_failed")
                        rows.append({"condition": condition, "wall_ms": wall_ms})
            finally:
                await process.stop()
            native.settled(owner, process)
    finally:
        await owner.close()
    return rows, startup_ms


def score_payload(native, row):
    return {key: value for key, value in native.score_request(row["input_token_ids"], row["candidate_token_ids"]).items()
            if key != "operation"}


def validate_result(result, expected, *, hit=None):
    require(type(result) is dict and exact(result.get("answer"), expected["decision"]), "typed_answer_bit_equality_failed")
    require(result.get("input_tokens") == expected["input_tokens"]
            and result.get("model", {}).get("profile") == PROFILE
            and result.get("model", {}).get("context_tokens") == CONTEXT, "result_identity_invalid")
    cache = result.get("cache")
    require(type(cache) is dict and type(cache.get("hit")) is bool, "cache_metadata_invalid")
    if hit is not None:
        require(cache["hit"] is hit, "cache_expectation_failed")


async def checked_json(client, method, path, *, payload=None, expected_status=200, headers=None, content=None):
    kwargs = {"headers": headers} if headers is not None else {}
    if content is not None:
        kwargs["content"] = content
    elif payload is not None:
        kwargs["json"] = payload
    response = await client.request(method, path, **kwargs)
    require(response.status_code == expected_status, "http_status_invalid")
    require(len(response.content) < LIMIT, "response_limit_invalid")
    value = response.json()
    require(type(value) is dict, "response_schema_invalid")
    return value


async def reset(engine):
    value = await checked_json(engine, "POST", "/reset_cache", payload={})
    require(value.get("reset") is True or value.get("cache_reset") is True, "reset_ack_invalid")


async def private_checks(engine, native, reference, enabled):
    from jevk5.prompt import decision_options, prompt_text
    errors = []
    count = hits = 0
    for case, expected in zip(reference["cases"], reference["worker"]["rows"][::2], strict=True):
        rendered = prompt_text(case["state"], case["question"]["instructions"],
                               [value for _, value in decision_options(case["question"])])
        tokenized = await checked_json(engine, "POST", "/tokenize", payload={"text": rendered, "add_special_tokens": False})
        require(exact(tokenized.get("token_ids"), expected["input_token_ids"]), "installed_tokenization_equality_failed")
        await reset(engine)
        request = score_payload(native, expected)
        for condition in CONDITIONS:
            value = await checked_json(engine, "POST", "/score", payload=request)
            hit = enabled and condition == "identical_repeat"
            native.validate_score(value, {"operation": "score", **request}, native.row_expected(expected), hit=hit)
            require(value.get("cache_hit") is hit, "installed_native_cache_expectation_failed")
            errors.append(calibration_error(value["logits"], value["candidate_probabilities"]))
            count += 1
            hits += int(hit)
        if count % 80 == 0:
            print(json.dumps({"stage": "private_score_validation", "responses": count}), flush=True)
    boundary_count = 0
    for boundary in reference["worker"]["boundaries"]:
        await reset(engine)
        request = {key: value for key, value in native.score_request(boundary["tokens"],
                   boundary["response"]["candidate_token_ids"]).items() if key != "operation"}
        for condition in CONDITIONS:
            value = await checked_json(engine, "POST", "/score", payload=request)
            hit = enabled and condition == "identical_repeat"
            native.validate_score(value, {"operation": "score", **request}, boundary["response"], hit=hit)
            require(value.get("cache_hit") is hit, "boundary_cache_expectation_failed")
            errors.append(calibration_error(value["logits"], value["candidate_probabilities"]))
            boundary_count += 1
    # Invalid exact-token requests must fail safely and invalidate remembered
    # native cache identity; valid recovery after every rejection is checked.
    expected = reference["worker"]["rows"][0]
    valid = score_payload(native, expected)
    rejected = 0
    for invalid in ({**valid, "input_token_ids": []}, {**valid, "input_token_ids": [reference["vocab_size"]]},
                    {**valid, "candidate_token_ids": [1, 1]}, {**valid, "temperature": 0},
                    {**valid, "input_token_ids": [0] * (CONTEXT + 1)}):
        await reset(engine)
        await checked_json(engine, "POST", "/score", payload=valid)
        response = await engine.post("/score", json=invalid)
        require(response.status_code in {400, 422} and len(response.content) < 4096, "private_rejection_failed")
        require(type(response.json().get("error")) in (str, dict), "private_error_schema_invalid")
        recovery = await checked_json(engine, "POST", "/score", payload=valid)
        native.validate_score(recovery, {"operation": "score", **valid}, native.row_expected(expected), hit=False)
        require(recovery.get("cache_hit") is False, "error_cache_invalidation_failed")
        rejected += 1
    return {"tokenizations": 243, "case_responses": count, "cache_hits": hits,
            "boundary_responses": boundary_count, "rejected_requests": rejected,
            "maximum_calibration_formula_error": max(errors), "candidate_outputs_bit_exact": True,
            "full_vocabulary_digest_revalidated": False}


async def api_decision(client, case):
    return await checked_json(client, "POST", "/api/decide", payload={"state": case["state"], "question": case["question"]})


async def public_checks(public, engine, reference, enabled):
    responses = hits = 0
    for case, expected in zip(reference["cases"], reference["worker"]["rows"][::2], strict=True):
        await reset(engine)
        for condition in CONDITIONS:
            hit = enabled and condition == "identical_repeat"
            validate_result(await api_decision(public, case), expected, hit=hit)
            responses += 1
            hits += int(hit)
        if responses % 80 == 0:
            print(json.dumps({"stage": "api_answer_validation", "responses": responses}), flush=True)
    return {"case_responses": responses, "cache_hits": hits, "typed_answers_bit_exact": True}


async def transport_rejections(public, engine, reference):
    marker = "integration-private-rejection-marker"
    case = reference["cases"][0]
    body = {"state": marker, "question": case["question"]}
    count = 0
    for method, path, kwargs, statuses in (
        ("GET", "/api/status", {"headers": {"Authorization": ""}}, {401}),
        ("GET", "/api/status", {"headers": {"Host": "invalid.example"}}, {403}),
        ("GET", "/api/status", {"headers": {"Origin": "https://invalid.example"}}, {403}),
        ("POST", "/api/decide", {"content": b"{"}, {400}),
        ("POST", "/api/decide", {"json": {"state": marker, "question": {"type": "choice", "criteria": [marker]}}}, {400}),
        ("POST", "/api/decide", {"content": (marker * (LIMIT // len(marker) + 1)).encode()}, {413}),
        ("POST", "/mcp", {"json": {"private_marker": marker}}, {400}),
    ):
        response = await public.request(method, path, **kwargs)
        require(response.status_code in statuses and marker not in response.text and len(response.content) < 4096,
                "transport_rejection_failed")
        count += 1
    # Context rejection is exercised through the public typed decision renderer.
    response = await public.post("/api/decide", json={**body, "state": "fixture " * (CONTEXT + 1)})
    require(response.status_code in {400, 413, 422} and len(response.content) < 4096
            and marker not in response.text, "public_context_rejection_failed")
    count += 1
    for path in ("/health", "/tokenize", "/score", "/reset_cache"):
        response = await engine.request("GET" if path == "/health" else "POST", path,
                                        headers={"Authorization": ""}, json={} if path != "/health" else None)
        require(response.status_code == 401 and len(response.content) < 4096, "private_auth_rejection_failed")
        count += 1
    # An invalid typed question reaches AttachedModel's serialized validation
    # path. Its failure must clear the native cache before A is submitted again.
    await reset(engine)
    validate_result(await api_decision(public, case), reference["worker"]["rows"][0], hit=False)
    response = await public.post("/api/decide", json={"state": marker,
        "question": {"type": "choice", "instructions": "fixture", "criteria": [marker]}})
    require(response.status_code == 400 and marker not in response.text and len(response.content) < 4096,
            "invalid_question_rejection_failed")
    validate_result(await api_decision(public, case), reference["worker"]["rows"][0], hit=False)
    count += 1
    return {"rejected_requests": count, "content_free_errors": True, "invalid_question_cache_cleared": True}


async def sdk_listing(session):
    result = await session.initialize()
    capabilities = result.capabilities.model_dump(exclude_none=True)
    require(result.protocolVersion == "2025-11-25" and set(capabilities) <= {"tools"}, "sdk_capabilities_invalid")
    tools = (await session.list_tools()).tools
    contract = json.loads((ROOT / "backend/meecp/legacy_jevk5_contract.json").read_bytes())
    require(exact([tool.model_dump(by_alias=True, exclude_none=True) for tool in tools], contract["tools"]),
            "installed_tools_changed")
    return len(tools)


async def sdk_decision(session, case):
    result = await session.call_tool("jevk5_decide", {"state": case["state"], "question": case["question"]})
    require(result.isError is not True and type(result.structuredContent) is dict, "sdk_decision_failed")
    return result.structuredContent


async def sdk_cases(session, engine, reference, selected, enabled):
    tools = await sdk_listing(session)
    for item in selected:
        index = item["index"]
        await reset(engine)
        result = await sdk_decision(session, reference["cases"][index])
        validate_result(result, reference["worker"]["rows"][index * 2], hit=False)
    status = await session.call_tool("jevk5_status", {})
    require(status.isError is not True and status.structuredContent.get("engine") == "stark", "sdk_status_invalid")
    return {"initialized": True, "tools": tools, "decisions": 12, "typed_answers_bit_exact": True,
            "resources_advertised": False, "prompts_advertised": False}


async def measure_after(call, engine, reference, selected, enabled):
    rows = []
    case = reference["cases"][selected[0]["index"]]
    for _ in range(3):
        await reset(engine)
        validate_result(await call(case), reference["worker"]["rows"][selected[0]["index"] * 2], hit=False)
    for item in selected:
        index = item["index"]
        await reset(engine)
        for condition in CONDITIONS:
            started = time.perf_counter()
            result = await call(reference["cases"][index])
            wall_ms = (time.perf_counter() - started) * 1000
            validate_result(result, reference["worker"]["rows"][index * 2], hit=enabled and condition == "identical_repeat")
            rows.append({"condition": condition, "wall_ms": wall_ms})
    return rows


async def queue_checks(public, session, reference, engine, *, enabled):
    index = max(range(len(reference["cases"])), key=lambda i: reference["worker"]["rows"][i * 2]["input_tokens"])
    case = reference["cases"][index]
    expected = reference["worker"]["rows"][index * 2]
    await reset(engine)
    # More callers than the attached queue's declared capacity. Actual 429s
    # and successful answer preservation are counted; no timing assumption is
    # used to turn a run without saturation into passing queue acceptance.
    async def call():
        response = await public.post("/api/decide", json={"state": case["state"], "question": case["question"]})
        require(response.status_code in {200, 429}, "queue_response_invalid")
        if response.status_code == 200:
            validate_result(response.json(), expected)
        else:
            require(response.json().get("error", {}).get("code") == "queue_full", "queue_rejection_invalid")
        return response.status_code
    values = await asyncio.gather(*(call() for _ in range(16)))
    require(200 in values and 429 in values, "queue_not_saturated")
    await reset(engine)
    before_wait = await checked_json(public, "GET", "/api/status")
    require(before_wait.get("ready") is True
            and type(before_wait.get("engine_instance_id")) is str
            and type(before_wait.get("generation")) is int
            and type(before_wait.get("counts", {}).get("decisions")) is int,
            "queue_engine_identity_changed")
    pending = asyncio.create_task(sdk_decision(session, case))
    # Observe the shared adapter's pending count before cancelling the client.
    async with asyncio.timeout(10):
        while (await checked_json(public, "GET", "/api/status")).get("queue_depth", 0) == 0:
            require(not pending.done(), "cancellation_not_observed")
            await asyncio.sleep(.005)
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending
    # The locked SDK cancels this caller's wait without sending an operation
    # cancellation. The gateway deliberately keeps an already-admitted request
    # running. Its completed native sequence therefore remains cached. This
    # check covers wait cancellation, preserved execution and queue settlement;
    # AttachedModel's operation-cancellation path is covered by its unit tests.
    validate_result(await api_decision(public, case), expected, hit=enabled)
    async with asyncio.timeout(30):
        while True:
            after_wait = await checked_json(public, "GET", "/api/status")
            if after_wait.get("queue_depth") == 0:
                break
            await asyncio.sleep(.01)
    require(after_wait.get("ready") is True
            and after_wait.get("engine_instance_id") == before_wait["engine_instance_id"]
            and after_wait.get("generation") == before_wait["generation"], "queue_engine_identity_changed")
    require(after_wait.get("counts", {}).get("decisions") == before_wait["counts"]["decisions"] + 2,
            "queue_execution_not_preserved")
    return {"concurrent_requests": len(values), "completed": values.count(200), "queue_rejections": values.count(429),
            "sdk_client_wait_cancellations": 1, "server_operation_cancelled": False,
            "engine_execution_preserved": True, "completed_server_decisions": 2,
            "shared_engine_unchanged": True, "recovery_cache_hit": enabled,
            "recovery_answer_bit_exact": True, "pending_drained": True}


def slot_settled(host, slot_id, *, gateway_stopped=False):
    require(not host.residents.get(slot_id) and slot_id not in host.reservations
            and not host.ledgers.get(slot_id), "slot_resource_unsettled")
    owner = host.owners.get(slot_id)
    if gateway_stopped:
        require(slot_id not in host.workers and (owner is None or not owner.instances), "slot_ownership_unsettled")


async def process_parents():
    """Inspect only process IDs and parent IDs; never command lines or content."""
    process = await asyncio.create_subprocess_exec("/bin/ps", "-axo", "pid=,ppid=",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async with asyncio.timeout(5):
        raw, _ = await process.communicate()
    require(process.returncode == 0 and len(raw) < LIMIT, "process_inventory_unavailable")
    result = {}
    for line in raw.splitlines():
        items = line.split()
        require(len(items) == 2 and all(item.isdigit() for item in items), "process_inventory_invalid")
        result[int(items[0])] = int(items[1])
    return result


def descendants(parents, roots):
    result = set(roots)
    while True:
        found = result | {pid for pid, parent in parents.items() if parent in result}
        if found == result:
            return result
        result = found


async def resident_descendants(residents):
    roots = {value.process.pid for value in residents}
    require(roots and all(type(pid) is int and pid > 1 for pid in roots), "resident_identity_invalid")
    values = descendants(await process_parents(), roots)
    # A Stark engine facade owns both a nested native reaper and scorer. Merely
    # waiting for the facade PID would leave that ownership unverified.
    require(len(values) >= len(roots) + 2, "native_descendants_unobserved")
    return values


async def descendants_reaped(identities):
    async with asyncio.timeout(10):
        while identities & (await process_parents()).keys():
            await asyncio.sleep(.025)


async def install(host, package):
    started = time.perf_counter()
    plan = await host.plan_install_async({"package": str(package.root), "start": True, "start_policy": "manual"},
                                         legacy_jev=True, public_port=0)
    result = await host.apply_install(plan["plan_id"])
    async with asyncio.timeout(600):
        while True:
            operation = host.registry.get_operation(result["operation_id"])
            if operation.status not in {"pending", "running"}:
                break
            await asyncio.sleep(.05)
    require(operation.status == "completed", "installed_package_failed")
    slot = host.registry.get_slot(plan["slot_id"])
    require(slot.observed_state == "ready" and Path(slot.package_path) != package.root
            and host.package(slot).digest == package.digest, "installed_snapshot_invalid")
    return slot, (time.perf_counter() - started) * 1000


async def run(args):
    import httpx
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    from meecp.supervisor import Supervisor
    import stark_dependency as dependency
    set_stage("runtime_binding")
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "apple_silicon_required")
    host_directory, python, reaper = host_paths(args.host)
    set_stage("package_reference_binding")
    package, native, native_args, reference, receipt = package_and_reference(args)
    set_stage("source_binding")
    bindings = source_bindings(args, package, host_directory, python, reaper, reference)
    selected = selected_cases(reference["cases"], reference["worker"]["rows"])
    enabled = args.expect_cache == "enabled"
    require(package.manifest.model.parameters.get("consecutive_cache", False) is enabled,
            "package_cache_policy_mismatch")
    optional_llama = llama_identity(args, package)
    set_stage("native_before")
    baseline, native_startup = await native_before(native, native_args, reference, selected, enabled)
    print(json.dumps({"stage": "native_before_complete", "measurements": len(baseline)}), flush=True)
    set_stage("llama_before")
    llama_rows, llama_report = (await llama_before(args, reaper, reference, selected)
                                if optional_llama is not None else (None, None))
    if llama_rows is not None:
        print(json.dumps({"stage": "llama_before_complete", "measurements": len(llama_rows)}), flush=True)
    state = new_private_directory(args.state)
    host = Supervisor(state, reaper)
    report = {}
    try:
        set_stage("supervisor_install")
        slot, install_ms = await install(host, package)
        connection = host.connection(slot.id)
        endpoint = connection["endpoint"].removesuffix("/mcp")
        declaration = next(process for process in package.manifest.processes if process.role == "engine")
        engine_resident = host.residents[slot.id][declaration.id]
        async with httpx.AsyncClient(base_url=endpoint, headers={"Authorization": "Bearer " + connection["token"]},
                                    timeout=180, follow_redirects=False, trust_env=False) as public:
            async with httpx.AsyncClient(base_url=engine_resident.endpoint,
                                        headers={"Authorization": "Bearer " + engine_resident.key},
                                        timeout=180, follow_redirects=False, trust_env=False) as engine:
                set_stage("installed_health")
                status = await checked_json(public, "GET", "/api/status")
                require(status.get("engine") == "stark" and status.get("ready") is True
                        and status.get("generation") == slot.generation, "installed_status_invalid")
                health = await checked_json(engine, "GET", "/health")
                require(health.get("ready") is True and health.get("generation") == slot.generation
                        and health.get("nonce") == engine_resident.nonce, "private_health_invalid")
                set_stage("private_native")
                report["private_native"] = await private_checks(engine, native, reference, enabled)
                set_stage("api")
                report["api"] = await public_checks(public, engine, reference, enabled)
                set_stage("transport_rejections")
                report["transport"] = await transport_rejections(public, engine, reference)
                timings = {"native_before": baseline}
                if llama_rows is not None:
                    timings["llama_before"] = llama_rows
                    report["llama_control"] = {"identity": optional_llama, **llama_report}
                set_stage("api_timing")
                timings["api_after"] = await measure_after(lambda case: api_decision(public, case), engine, reference, selected, enabled)
                async with streamable_http_client(connection["endpoint"], http_client=public) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        set_stage("http_mcp_sdk")
                        report["http_mcp"] = await sdk_cases(session, engine, reference, selected, enabled)
                        set_stage("http_mcp_timing")
                        timings["mcp_after"] = await measure_after(lambda case: sdk_decision(session, case), engine, reference, selected, enabled)
                        set_stage("queue_and_cancellation")
                        report["queue"] = await queue_checks(public, session, reference, engine, enabled=enabled)
                environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": os.pathsep.join(
                    [str(ROOT / "backend"), str(ROOT / "vendor"), str(host_directory / "python/site-packages")]),
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
                parameters = StdioServerParameters(command=str(python), args=["-P", "-m", "meecp", "--state-dir", str(state),
                                                                           "connect", "--slot", slot.id], env=environment)
                with open(os.devnull, "w") as errors:
                    set_stage("stdio_mcp_sdk")
                    async with stdio_client(parameters, errlog=errors) as (read, write):
                        async with ClientSession(read, write) as session:
                            report["stdio_mcp"] = await sdk_cases(session, engine, reference, selected, enabled)
                set_stage("timing_aggregation")
                report["timing"] = timing_summary(timings)
                report["timing"]["native_startup_ms"] = native_startup
                report["timing"]["install_startup_ms"] = install_ms
                initial_generation = slot.generation
                old_residents = list(host.residents[slot.id].values())
                old_descendants = await resident_descendants(old_residents)
                set_stage("lifecycle_stop")
                await host.control(slot.id, "stop")
                require(all(value.process.returncode is not None and not value.process.ownership_lost for value in old_residents),
                        "stop_reaping_failed")
                await descendants_reaped(old_descendants)
                # An inactive gateway intentionally remains resident after stop.
                await host.stop_gateway(slot.id)
                slot_settled(host, slot.id, gateway_stopped=True)
                set_stage("lifecycle_start")
                await host.control(slot.id, "start")
                current = host.registry.get_slot(slot.id)
                require(current.generation > initial_generation and current.observed_state == "ready", "restart_generation_invalid")
                validate_result(await api_decision(public, reference["cases"][0]), reference["worker"]["rows"][0], hit=False)
                restarted = list(host.residents[slot.id].values())
                restarted_descendants = await resident_descendants(restarted)
                set_stage("lifecycle_restart")
                await host.control(slot.id, "restart")
                require(all(value.process.returncode is not None and not value.process.ownership_lost for value in restarted),
                        "restart_reaping_failed")
                await descendants_reaped(restarted_descendants)
                validate_result(await api_decision(public, reference["cases"][0]), reference["worker"]["rows"][0], hit=False)
                report["lifecycle"] = {"stops": 1, "starts_after_stop": 1, "restarts": 1,
                                       "owned_residents_reaped": len(old_residents) + len(restarted),
                                       "resident_and_native_descendants_reaped": len(old_descendants) + len(restarted_descendants),
                                       "reservations_released_after_stop": True, "restart_cache_cold": True}
    finally:
        await host.close()
    set_stage("ownership_verification")
    require(not host.workers and not host.residents and not host.reservations
            and all(not owner.instances for owner in host.owners.values())
            and all(not ledger for ledger in host.ledgers.values()), "final_ownership_unsettled")
    set_stage("source_verification")
    require(dependency.verify_build(args.build, args.lock) == receipt, "native_build_changed")
    require(source_bindings(args, package, host_directory, python, reaper, reference) == bindings,
            "source_binding_changed")
    require(llama_identity(args, package) == optional_llama, "llama_control_binding_changed")
    from meecp.contracts import validate_package
    require(validate_package(package.root).digest == package.digest, "source_package_changed")
    report.update({"schema_version": 1, "mode": "installed-stark-jevk5-integration", "accepted": True,
                   "package_id": PACKAGE_ID, "package_digest": package.digest, "profile": PROFILE,
                   "engine": "stark", "cache_enabled": enabled, "context_tokens": CONTEXT,
                   "temperature": TEMPERATURE, "knockout_temperature": .93,
                   "source_bindings_sha256": bindings, "source_bindings_unchanged": True,
                   "frozen_native_reference_configuration_sha256": reference["configuration_sha256"],
                   "raw_records_persisted": False, "ownership_settled": True,
                   "quality_claim": False, "llama_equivalence_claim": False})
    set_stage("report_write")
    write_report(args.report, report)
    print(json.dumps({"stage": "integration_complete", "accepted": True, "api_responses": 486,
                      "private_score_responses": 486, "boundary_responses": 96,
                      "cache_enabled": enabled, "ownership_settled": True,
                      "report_sha256": file_hash(args.report)}), flush=True)
    set_stage("complete")
    return report


def main(argv=None):
    parser = Parser(description=__doc__)
    for flag in ("package", "host", "state", "build", "reference-directory", "native-reaper", "report"):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=ROOT / "engines/stark.lock.json")
    parser.add_argument("--expect-cache", choices=("disabled", "enabled"), default="disabled")
    parser.add_argument("--llama-binary", type=Path)
    parser.add_argument("--llama-gguf", type=Path)
    set_stage("arguments")
    try:
        args = parser.parse_args(argv)
        for name in ("package", "host", "state", "build", "reference_directory", "native_reaper", "report", "lock"):
            setattr(args, name, getattr(args, name).absolute())
        for name in ("llama_binary", "llama_gguf"):
            if getattr(args, name) is not None:
                setattr(args, name, getattr(args, name).absolute())
        require(not args.report.exists() and args.report.parent.is_dir(), "report_path_invalid")
        os.umask(0o077)
        asyncio.run(run(args))
        return 0
    except (Exception, KeyboardInterrupt, BaseExceptionGroup) as error:
        print(json.dumps({"accepted": False, "error": sanitized_failure_code(error), "stage": CURRENT_STAGE}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
