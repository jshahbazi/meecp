"""Independent, untimed audit of the versioned Sol distribution-cache worker.

Only historical independent audit helpers are imported. The new evaluator and
build recipe are never imported; content-bearing native records stay private.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

import audit_uzu_sol as old

ROOT, PUBLIC = old.ROOT, old.PUBLIC
require, exact = old.require, old.exact
POLICY = "consecutive-full-vocabulary-logits-v1"
KEY = "exact-token-ids+context-tokens+model-instance"
OUTPUTS = ("candidate_token_ids", "logits", "log_probabilities", "candidate_probabilities", "temperature")
NAMES = ("A_first", "A_repeat", "B_replace", "A_return", "tokenize_preserves", "A_after_tokenize",
         "context_changed", "context_repeat", "context_return", "candidate_order", "candidate_set",
         "temperature_changed", "malformed_request", "A_after_malformed", "invalid_score",
         "A_after_invalid_score", "invalid_tokenize", "A_after_invalid_tokenize",
         "malformed_tokenize", "A_after_malformed_tokenize")
ERRORS = {"malformed_request": "invalid_request", "invalid_score": "invalid_scoring_request",
          "invalid_tokenize": "invalid_tokenization_request", "malformed_tokenize": "invalid_request"}


class Replay:
    """Reconstruct cache identity and counters from requests, never reply flags."""
    def __init__(self, enabled=True):
        self.enabled, self.previous = enabled, None
        self.hits, self.calls = 0, 0

    def clear(self):
        self.previous = None

    def telemetry(self, value):
        expected = {"enabled": self.enabled, "hits": self.hits, "native_score_calls": self.calls}
        require(exact(value.get("cache"), expected), "cache_counters_invalid")

    def score(self, value, request):
        key = (tuple(request["input_token_ids"]), request["context_tokens"])
        hit = bool(self.enabled and self.previous == key)
        self.previous = key if self.enabled else None
        self.hits += int(hit)
        self.calls += int(not hit)
        require(type(value.get("cache_hit")) is bool and value["cache_hit"] is hit, "cache_identity_invalid")
        self.telemetry(value)
        return hit


def request(tokens, candidates, context=8192, temperature=1.22):
    return {"operation": "score", "input_token_ids": list(tokens), "candidate_token_ids": list(candidates),
            "context_tokens": context, "temperature": temperature}


def readiness(value, vocab, enabled=True, digest=False):
    require(value.get("ready") is True and value.get("engine") == "uzu-rust"
            and value.get("version") == "0.6.1"
            and value.get("mode") == ("exact-token-digest-cache-v1" if digest else "exact-token-scoring-cache-v1")
            and value.get("upstream_revision") == old.REVISION
            and value.get("scoring_patch_sha256") == old.file_hash(ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch")
            and value.get("cache_policy") == (POLICY if enabled else "fresh-state")
            and type(value.get("cache_enabled")) is bool and value["cache_enabled"] is enabled
            and value.get("cache_key") == KEY and type(value.get("vocab_size")) is int
            and value["vocab_size"] == vocab, "readiness_invalid")
    Replay(enabled).telemetry(value)


def response(value, req, vocab, hit):
    tokens, candidates = req["input_token_ids"], req["candidate_token_ids"]
    require(0 < len(tokens) <= req["context_tokens"] <= 16384
            and all(type(t) is int and 0 <= t < vocab for t in tokens)
            and 2 <= len(candidates) <= 16 and len(set(candidates)) == len(candidates)
            and all(type(t) is int and 0 <= t < vocab for t in candidates), "request_invalid")
    require(value.get("input_tokens") == len(tokens)
            and value.get("prompt_tokens_sha256") == old.token_digest(tokens)
            and exact(value.get("candidate_token_ids"), candidates)
            and exact(value.get("temperature"), req["temperature"])
            and value.get("truncated") is False and value.get("cache_hit") is hit, "response_identity_invalid")
    vectors = [value.get(k) for k in ("logits", "log_probabilities", "candidate_probabilities")]
    require(all(type(v) is list and len(v) == len(candidates)
                and all(type(z) is float and math.isfinite(z) for z in v) for v in vectors), "scores_invalid")
    logits, logprobs, probs = vectors
    require(all(z <= 1e-12 for z in logprobs) and all(0 <= p <= 1 for p in probs)
            and abs(math.fsum(probs) - 1) <= 1e-12, "probabilities_invalid")
    errors = []
    for values in (logits, logprobs):
        weights = [math.exp((z - max(values)) / req["temperature"]) for z in values]
        total = math.fsum(weights)
        errors.extend(abs(p - w / total) for p, w in zip(probs, weights, strict=True))
    require(max(errors) <= 1e-12, "calibration_invalid")
    metrics = value.get("metrics")
    require(type(metrics) is dict and set(metrics) == {"state_creation_seconds", "prefill_seconds",
            "readout_seconds", "score_seconds", "total_seconds"}
            and all(old.finite(z) for z in metrics.values()), "native_metrics_invalid")
    if hit:
        require(metrics["state_creation_seconds"] == metrics["prefill_seconds"] == 0.0,
                "cache_hit_native_work_invalid")
    return max(errors)


def worker_row(row, case, reference, vocab, replay):
    req = request(row["input_token_ids"], reference["candidate_token_ids"])
    hit = replay.score(row["response"], req)
    error = response(row["response"], req, vocab, hit)
    require(row.get("case_id") == case["id"]
            and all(exact(old.normalized_row(row)[k], reference[k]) for k in old.IDENTITY)
            and exact(row["decision"], old.decision(case, row["response"]["candidate_probabilities"], len(req["input_token_ids"]))),
            "case_output_bits_invalid")
    return error


def vocabulary(value, req, expected, vocab, replay):
    replay.score(value, req)
    require(value.get("truncated") is False and value.get("input_tokens") == len(req["input_token_ids"])
            and value.get("prompt_tokens_sha256") == old.token_digest(req["input_token_ids"])
            and value.get("vocab_size") == vocab
            and all(exact(value.get(k), expected[k]) for k in
                    ("input_tokens", "prompt_tokens_sha256", "vocab_logits_sha256", "vocab_size")),
            "vocabulary_bits_invalid")


def sequence_plan(tokens, candidates):
    a, b, selected = list(tokens), list(tokens), list(candidates[:2])
    b[-1] = next(t for t in candidates if t != b[-1])
    req = request(a, selected)
    smaller = max(len(a), 8191)
    require(smaller != 8192, "sequence_context_invalid")
    return [req, req, request(b, selected), req,
            {"operation": "tokenize", "text": "A", "add_special_tokens": False}, req,
            request(a, selected, smaller), request(a, selected, smaller), req,
            request(a, list(reversed(selected))), request(a, candidates[2:4]),
            request(a, selected, temperature=0.61), None, req,
            request(a, [selected[0], selected[0]]), req,
            {"operation": "tokenize", "text": "", "add_special_tokens": False}, req,
            {"operation": "tokenize", "text": "A", "unexpected": True}, req]


def sequences(record, first_tokens, first_candidates, vocab):
    require(exact(record["tokens"], first_tokens) and record["candidates"][:len(first_candidates)] == first_candidates
            and len(record["candidates"]) == len(set(record["candidates"])) == 16
            and all(type(t) is int and 0 <= t < vocab for t in record["candidates"]), "sequence_identity_invalid")
    readiness(record["readiness"], vocab)
    readiness(record["default_off"]["readiness"], vocab, enabled=False)
    readiness(record["restart"]["readiness"], vocab)
    plan = sequence_plan(first_tokens, record["candidates"])
    require([r["name"] for r in record["rows"]] == list(NAMES), "sequence_order_invalid")
    fresh = record["default_off"]
    require(exact(fresh["request"], plan[0]) and len(fresh["responses"]) == 2, "default_off_invalid")
    control, cached = Replay(False), Replay()
    errors = []
    for value in fresh["responses"]:
        require(not control.score(value, plan[0]), "default_off_hit")
        errors.append(response(value, plan[0], vocab, False))
    require(all(exact(fresh["responses"][0][k], fresh["responses"][1][k]) for k in OUTPUTS), "default_off_bits_invalid")
    for row, req in zip(record["rows"], plan, strict=True):
        require(exact(row.get("request"), req), "sequence_request_invalid")
        value = row["response"]
        if row["name"] in ERRORS:
            require(set(value) == {"error", "cache"} and value["error"] == ERRORS[row["name"]]
                    and "control" not in row, "sequence_error_invalid")
            cached.clear()
            cached.telemetry(value)
        elif req["operation"] == "tokenize":
            cached.telemetry(value)
            require(value.get("token_ids") == [record["candidates"][0]] and value.get("token_count") == 1
                    and value.get("token_ids_sha256") == old.token_digest(value["token_ids"]), "tokenize_identity_invalid")
        else:
            hit = cached.score(value, req)
            errors.append(response(value, req, vocab, hit))
            other = row["control"]
            require(not control.score(other, req), "control_hit")
            errors.append(response(other, req, vocab, False))
            require(all(exact(value[k], other[k]) for k in OUTPUTS), "control_output_bits_invalid")
    restarted = record["restart"]
    require(type(record["eof_exit_code"]) is int and record["eof_exit_code"] == 0
            and exact(restarted["request"], plan[0]), "restart_identity_invalid")
    require(not Replay().score(restarted["response"], plan[0]), "restart_hit")
    errors.append(response(restarted["response"], plan[0], vocab, False))
    require(all(exact(restarted["response"][k], fresh["responses"][0][k]) for k in OUTPUTS), "restart_bits_invalid")
    return errors, {"score_responses": cached.hits + cached.calls, "cache_hits": cached.hits,
                    "native_score_calls": cached.calls, "default_off_native_calls": control.calls,
                    "restart_native_calls": 1, "error_controls": len(ERRORS)}


def build_identity(config):
    receipt = config["candidate_receipt"]
    require(receipt.get("schema_version") == 4 and receipt.get("variant") == "sol-consecutive-cache-v1"
            and receipt.get("upstream_revision") == old.REVISION and receipt.get("cache_policy") == POLICY
            and receipt.get("cache_enabled_by_default") is False and receipt.get("unit_tests_passed") is True
            and receipt.get("cache_flag") == "--consecutive-cache", "build_identity_invalid")
    binary, probe = old.ordinary(config["candidate_binary"]), old.ordinary(config["candidate_digest_binary"])
    directory = binary.parent.parent
    require(directory.is_relative_to(ROOT / "runtime/uzu-rust/consecutive-cache/builds")
            and binary.name == "meecp-uzu-cache-worker" and probe.name == "meecp-uzu-cache-score-digest"
            and binary.parent == probe.parent and exact(old.read(directory / "BUILD_RECEIPT.json", False), receipt)
            and old.file_hash(binary) == receipt["binary_sha256"] == receipt["binaries"][binary.name]
            and old.file_hash(probe) == receipt["digest_binary_sha256"] == receipt["binaries"][probe.name], "build_binary_invalid")
    for group in ("source_bindings", "prepared_worker_sources", "prepared_upstream_sources"):
        require(receipt[group], "build_source_set_empty")
        for name, digest in receipt[group].items():
            path = ROOT / name
            require(path.is_relative_to(ROOT) and old.file_hash(path) == digest == config["files"].get(str(path)),
                    "build_source_binding_invalid")
    require(receipt["sol_control"]["binary_sha256"] == "384310c592ef1f217e148dea51f8182cc95d5c14e724cb38afde2bc22b0840d1",
            "sol_control_invalid")
    control = ROOT / receipt["sol_control"]["path"]
    require(old.file_hash(control / "BUILD_RECEIPT.json") == receipt["sol_control"]["receipt_sha256"]
            and old.file_hash(control / "bin/meecp-uzu-worker") == receipt["sol_control"]["binary_sha256"]
            and old.file_hash(control / "bin/meecp-uzu-score-digest") == receipt["sol_control"]["digest_binary_sha256"],
            "sol_control_changed")
    expected = [f"experiments/uzu-rust/{n}.patch" for n in ("uzu-next-token-scoring", "uzu-scoring-topology",
        "uzu-scoring-state-init", "uzu-deep-submission", "uzu-deep-last-layer", "uzu-deep-metal")]
    expected += [f"experiments/uzu-rust/patches/uzu-sol-{n}.patch" for n in ("rope", "gdn", "attention")]
    require([p["path"] for p in receipt["patch_chain"]] == expected
            and all(old.file_hash(ROOT / p["path"]) == p["sha256"] for p in receipt["patch_chain"]), "patch_chain_invalid")
    expected_features = ["probability-scoring", "uzu-engine/deferred-state-init", "uzu-engine/scoring-last-layer-pruning",
        "uzu-engine/direct-delta-conv", "uzu-engine/sol-rope-cache", "uzu-engine/compact-delta-conv", "uzu-engine/sol-attention-kv-only"]
    require(receipt["features"] == expected_features
            and old.file_hash(directory / "project/Cargo.toml") == receipt["cargo_manifest_sha256"]
            and old.file_hash(directory / "project/Cargo.lock") == receipt["cargo_lock_sha256"]
            and old.file_hash(ROOT / "experiments/uzu-rust/build_consecutive_cache_scoring.py") == receipt["build_recipe_sha256"],
            "build_recipe_invalid")
    tests = old.read(directory / "UNIT_TESTS.json", False)
    require(old.file_hash(directory / "UNIT_TESTS.json") == receipt["unit_test_receipt_sha256"]
            and tests["status"] == "complete" and exact(tests["source_bindings"], receipt["source_bindings"])
            and tests["build_recipe_sha256"] == receipt["build_recipe_sha256"]
            and [r["binary"] for r in tests["results"]] == [binary.name, probe.name]
            and all(type(r["passed"]) is int and r["passed"] >= 17 for r in tests["results"])
            and tests["tests_passed"] == sum(r["passed"] for r in tests["results"]), "native_tests_invalid")
    return receipt, tests["tests_passed"]


def audit(directory):
    directory = old.private_directory(directory)
    config, pre, post = (old.read(directory / name) for name in ("configuration.json", "pre-run-seal.json", "post-run-seal.json"))
    expected = {"schema_version": 1, "mode": "untimed-consecutive-cache-acceptance", "warmups_per_process": 3,
                "reset_after_warmups": True, "reset_before_reference_pair": True, "context_tokens": 8192,
                "temperature": 1.22, "case_responses": 486, "boundary_responses": 96,
                "digest_responses": 1068, "sequence_names": list(NAMES), "timing_deferred": True, "timing_not_reported": True}
    require(all(exact(config.get(k), v) for k, v in expected.items()), "configuration_invalid")
    require(str(Path(__file__).resolve()) in config["files"]
            and str(Path(old.__file__).resolve()) in config["files"], "audit_source_not_bound")
    for path, digest in config["files"].items():
        require(old.file_hash(path) == digest, "bound_artifact_changed")
    receipt, test_count = build_identity(config)
    identity = old.sha(old.encoded(config))
    snapshot = directory / "artifact-snapshot.json"
    require(pre["configuration_sha256"] == post["configuration_sha256"] == identity
            and pre["artifact_snapshot_sha256"] == post["artifact_snapshot_sha256"] == old.file_hash(snapshot)
            and exact(old.read(snapshot), {"files": config["files"]}) and post["ownership_settled"] is True,
            "seals_invalid")
    require(set(post["records"]) == {"worker.json", "digest.json", "sequences.json"}, "record_set_invalid")
    records = {}
    for name, digest in post["records"].items():
        require(old.file_hash(directory / name) == digest, "record_hash_invalid")
        records[name] = old.read(directory / name)
        require(records[name]["complete"] is True and records[name]["ownership_settled"] is True
                and records[name]["configuration_sha256"] == identity, "record_invalid")
    cases, references = old.reference_data(config["reference"])
    reference_dir = old.private_directory(config["reference"])
    frozen = old.read(reference_dir / "round-1-baseline.json")
    frozen_probe = old.read(reference_dir / "digest-baseline.json")
    require(config["case_ids"] == [c["id"] for c in cases]
            and config["model"] == old.read(reference_dir / "configuration.json")["model"], "reference_identity_invalid")
    vocab = frozen_probe["readiness"]["vocab_size"]
    worker, probe = records["worker.json"], records["digest.json"]
    readiness(worker["readiness"], vocab)
    readiness(probe["readiness"], vocab, digest=True)
    require(len(worker["warmups"]) == len(probe["warmups"]) == 3
            and len(worker["rows"]) == 486 and len(worker["boundaries"]) == 96 and len(probe["outputs"]) == 1068,
            "population_incomplete")
    worker_replay, probe_replay, errors = Replay(), Replay(), []
    for row in worker["warmups"]:
        require(row["condition"] == "warmup", "warmup_invalid")
        errors.append(worker_row(row, cases[0], frozen["rows"][0], vocab, worker_replay))
    for index, row in enumerate(worker["rows"]):
        if index % 2 == 0:
            worker_replay.clear()
        require(row["condition"] == old.CONDITIONS[index % 2], "condition_invalid")
        errors.append(worker_row(row, cases[index // 2], frozen["rows"][index], vocab, worker_replay))
    for index, row in enumerate(worker["boundaries"]):
        target = frozen["boundaries"][index // 2]
        if index % 2 == 0:
            worker_replay.clear()
        require(row["condition"] == old.CONDITIONS[index % 2]
                and all(exact(row[k], target[k]) for k in ("length", "kind", "tokens")), "boundary_identity_invalid")
        req = request(row["tokens"], target["response"]["candidate_token_ids"])
        hit = worker_replay.score(row["response"], req)
        errors.append(response(row["response"], req, vocab, hit))
        require(all(exact(row["response"][k], target["response"][k]) for k in OUTPUTS), "boundary_bits_invalid")
    requests = [request(r["input_token_ids"], r["candidate_token_ids"]) for r in frozen["rows"]]
    requests += [request(r["tokens"], r["response"]["candidate_token_ids"]) for r in frozen["boundaries"]]
    for value in probe["warmups"]:
        vocabulary(value, requests[0], frozen_probe["outputs"][0], vocab, probe_replay)
    for index, value in enumerate(probe["outputs"]):
        if index % 2 == 0:
            probe_replay.clear()
        vocabulary(value, requests[index // 2], frozen_probe["outputs"][index // 2], vocab, probe_replay)
    more_errors, sequence_counts = sequences(records["sequences.json"], frozen["rows"][0]["input_token_ids"],
                                             frozen["rows"][0]["candidate_token_ids"], vocab)
    errors.extend(more_errors)
    quality = {}
    for tier in ("public", "synthetic"):
        actual, control = [], []
        for index, case in enumerate(cases):
            if (case["tier"] != "synthetic") == (tier == "public"):
                actual.append(old.quality_row(case, old.normalized_row(worker["rows"][index * 2])))
                control.append(old.quality_row(case, references[case["id"]]))
        require(exact(actual, control), "quality_rows_invalid")
        quality[tier] = old.quality_summary(actual)
    for path, digest in config["files"].items():
        require(old.file_hash(path) == digest, "bound_artifact_changed")
    return {"schema_version": 1, "status": "passed", "method": "Independent replay and binary64 comparison using historical independent audit helpers; new evaluator and builder not imported.",
        "audit_source_sha256": old.file_hash(__file__), "configuration_sha256": identity,
        "post_run_seal_sha256": old.file_hash(directory / "post-run-seal.json"),
        "bound_artifacts_rehashed": len(config["files"]), "worker_sha256": receipt["binary_sha256"],
        "probe_sha256": receipt["digest_binary_sha256"], "candidate_variant": receipt["variant"],
        "cache_policy": POLICY, "cache_enabled_by_default": False, "native_fake_backend_tests": test_count,
        "case_responses_exact": 486, "boundary_responses_exact": 96, "whole_vocabulary_digests_exact": 1068,
        "worker_warmups_checked": 3, "digest_warmups_checked": 3,
        "worker_cache": {"cache_hits": worker_replay.hits, "native_score_calls": worker_replay.calls},
        "digest_cache": {"cache_hits": probe_replay.hits, "native_score_calls": probe_replay.calls},
        "sequence_controls": sequence_counts, "default_off_verified": True, "restart_cold_verified": True,
        "changed_input_context_and_errors_invalidate": True, "candidate_and_temperature_readout_exact": True,
        "candidate_float_bits_equal": True, "whole_vocabulary_logits_exactly_equal": True,
        "quality_equals_reference": True, "quality": quality, "maximum_independent_calibration_error": max(errors),
        "timing_deferred": True, "timing_not_reported": True,
        "timing_status": "Untimed correctness acceptance; precise performance measurement deferred.",
        "limits": "Isolated scoring worker. No production scorer/HTTP/MCP integration or KV-prefix reuse acceptance."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit(args.output)
        destination = args.report.absolute()
        require(destination.is_relative_to(PUBLIC) and ".." not in destination.parts
                and not destination.exists() and not any(p.is_symlink() for p in destination.parents), "report_destination_invalid")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(old.encoded(result))
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"stage": "cache_independent_audit_complete", "status": "passed",
                          "case_responses": result["case_responses_exact"], "timing_deferred": True}), flush=True)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "consecutive_cache_audit_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
