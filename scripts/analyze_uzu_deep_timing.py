"""Independently audit completed deep timing experiments; never launch inference."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "experiments/uzu-rust/jev-scoring"
RESUMED_PROTOCOL = PUBLIC / "DEEP_TIMING_RESUMED_PROTOCOL.md"
BUCKETS = (("short", 0, 256), ("medium", 256, 1024), ("long", 1024, 4096))
CONDITIONS = ("fresh", "identical_repeat")
NATIVE = ("state_creation_seconds", "prefill_seconds", "readout_seconds", "score_seconds", "total_seconds")
METRICS = ("wall_seconds", "score_request_seconds", *NATIVE)
HISTORICAL = "af047d5ae3fcf0c088443aad40fbe0f3d35cf137e4cb15e390a1f922103ea04a"
COMBINED = "11eb2da42e123a86707ac83cb0d4e0b2c7e0f298554e15f5244703e3ea69111a"
REVISION = "8d01581474760bd1fc7f4a547ff1d02f7dada437"


class AuditError(ValueError):
    pass


def require(value, code):
    if not value:
        raise AuditError(code)


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def ordinary(path):
    path = Path(path).absolute()
    require(path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)), "file_invalid")
    return path


def file_hash(path):
    with ordinary(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path, private=True):
    path = ordinary(path)
    require(not private or path.stat().st_mode & 0o077 == 0, "private_file_invalid")
    return json.loads(path.read_bytes())


def private_directory(path):
    path = Path(path).absolute()
    require(path.is_relative_to(ROOT / "test-artifacts") and ".." not in path.parts
            and path.is_dir() and path.stat().st_mode & 0o077 == 0
            and not any(p.is_symlink() for p in (path, *path.parents)), "private_directory_invalid")
    return path


def exact(left, right):
    """Include signed zero in the equality check for decoded JSON floats."""
    if type(left) is not type(right):
        return False
    if type(left) is float:
        return struct.pack(">d", left) == struct.pack(">d", right)
    if type(left) is dict:
        return left.keys() == right.keys() and all(exact(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right))
    return left == right


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    at = (len(ordered) - 1) * quantile
    lo, hi = math.floor(at), math.ceil(at)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - at) + ordered[hi] * (at - lo)


def distribution(values):
    return {"samples": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95),
            "minimum": min(values) if values else None, "maximum": max(values) if values else None}


def finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def token_digest(values):
    require(type(values) is list and 0 < len(values) <= 8192
            and all(type(v) is int and 0 <= v < 2**32 for v in values), "tokens_invalid")
    return digest(b"".join(v.to_bytes(4, "little") for v in values))


def option_keys(case):
    question = case["question"]
    if question["type"] == "noul":
        return ["true", "false"]
    if question["type"] == "score":
        return [str(i) for i in range(len(question["criteria"]))]
    require(question["type"] == "choice", "question_kind_invalid")
    return list(dict.fromkeys(question["criteria"]))


def validate_row(row, case, reference):
    require(row["case_id"] == case["id"] and row["cache_hit"] is False and row["truncated"] is False,
            "row_identity_invalid")
    token_digest(row["input_token_ids"])
    require(row["input_tokens"] == len(row["input_token_ids"]), "row_token_count_invalid")
    for key in ("input_token_ids", "candidate_token_ids", "logits", "log_probabilities", "probabilities", "decision"):
        require(exact(row[key], reference[key]), "reference_output_mismatch")
    keys = option_keys(case)
    candidates, probabilities = row["candidate_token_ids"], row["probabilities"]
    require(2 <= len(keys) <= 16 and len(candidates) == len(keys) == len(probabilities)
            and len(set(candidates)) == len(candidates)
            and all(type(v) is int and 0 <= v < 2**32 for v in candidates), "candidate_identity_invalid")
    require(all(finite(p) and p <= 1 for p in probabilities)
            and abs(math.fsum(probabilities) - 1) <= 1e-12, "probabilities_invalid")
    errors = []
    for key in ("logits", "log_probabilities"):
        values = row[key]
        require(len(values) == len(keys) and all(type(v) in (int, float) and math.isfinite(v) for v in values),
                "scores_invalid")
        weights = [math.exp((v - max(values)) / 1.22) for v in values]
        errors.extend(abs(p - w / math.fsum(weights)) for p, w in zip(probabilities, weights))
    require(max(errors) <= 1e-12 and all(v <= 1e-12 for v in row["log_probabilities"]), "calibration_invalid")
    probs = dict(zip(keys, probabilities))
    expected = {"type": case["question"]["type"], "confidence": max(probabilities), "input_tokens": row["input_tokens"]}
    if expected["type"] == "noul":
        expected["noul"] = probs["true"]
    elif expected["type"] == "choice":
        expected.update(choice=max(probs, key=probs.get), probabilities=probs)
    else:
        expected.update(score=sum(int(k) * v for k, v in probs.items()), probabilities=probs)
    require(exact(row["decision"], expected), "typed_decision_invalid")
    require(finite(row["tokenization_seconds"]) and all(finite(row[k], True) for k in METRICS[:2]), "timing_invalid")
    require(set(NATIVE) == set(row["engine_metrics"])
            and all(finite(v) for v in row["engine_metrics"].values()), "native_metrics_invalid")
    return max(errors)


def selection(cases, rows):
    lengths = {r["case_id"]: r["input_tokens"] for r in rows if r["condition"] == "fresh"}
    result = []
    for bucket, low, high in BUCKETS:
        eligible = [c for c in cases if c["tier"] != "synthetic" and low <= lengths[c["id"]] < high]
        eligible.sort(key=lambda c: lengths[c["id"]])
        require(len(eligible) >= 4, "selection_bucket_small")
        indices = [(2 * i + 1) * len(eligible) // 8 for i in range(4)]
        require(len(set(indices)) == 4, "selection_duplicate")
        result.extend({"case_id": eligible[i]["id"], "bucket": bucket,
                       "input_tokens": lengths[eligible[i]["id"]]} for i in indices)
    return result


def schedule():
    result = []
    for quartet in range(1, 5):
        order = ("baseline", "candidate", "candidate", "baseline") if quartet % 2 else (
            "candidate", "baseline", "baseline", "candidate")
        result.append({"quartet": quartet, "phase": "control", "engine": "baseline", "position": 0})
        result.extend({"quartet": quartet, "phase": "comparison", "engine": e, "position": p}
                      for p, e in enumerate(order, 1))
    return [{"block": i, **item} for i, item in enumerate(result, 1)]


def metric_value(row, metric):
    return row[metric] if metric in METRICS[:2] else row["engine_metrics"][metric]


def quartet_summary(blocks, condition, ids=None):
    require(len(blocks) == 4 and all(b["phase"] == "comparison" for b in blocks), "quartet_invalid")
    rows = {engine: [[r for r in b["rows"] if r["condition"] == condition
                     and (ids is None or r["case_id"] in ids)]
                    for b in blocks if b["engine"] == engine] for engine in ("baseline", "candidate")}
    require(all(len(group) == 2 for group in rows.values()), "quartet_unbalanced")
    order = [r["case_id"] for r in rows["baseline"][0]]
    require(order and all([r["case_id"] for r in group] == order for groups in rows.values() for group in groups),
            "quartet_pairing_invalid")
    metrics = {}
    for metric in METRICS:
        averages = {engine: [statistics.mean(metric_value(group[i], metric) for group in groups)
                            for i in range(len(order))] for engine, groups in rows.items()}
        left, right = averages["baseline"], averages["candidate"]
        metrics[metric] = {
            "baseline_case_means": distribution(left), "candidate_case_means": distribution(right),
            "baseline_over_candidate_ratio_of_means": statistics.mean(left) / statistics.mean(right)
                if statistics.mean(right) > 0 else None,
            "paired_case_ratio": distribution([a / b for a, b in zip(left, right) if b > 0]),
            "paired_candidate_minus_baseline_seconds": distribution([b - a for a, b in zip(left, right)])}
    return {"cases": len(order), "metrics": metrics}


def timing_summary(rows):
    names = sorted({k for r in rows for k in r["engine_metrics"] if k.endswith("_seconds")})
    native = {}
    for name in names:
        values = [r["engine_metrics"][name] for r in rows if name in r["engine_metrics"]]
        native[name] = {"samples": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
    return {"samples": len(rows), "cache_hits": sum(r["cache_hit"] for r in rows),
            **{k: {"p50": percentile([r[k] for r in rows], .5), "p95": percentile([r[k] for r in rows], .95)}
               for k in ("tokenization_seconds", "score_request_seconds", "wall_seconds")},
            "engine_metrics_seconds": native}


def controls(blocks, condition):
    selected = [b for b in blocks if b["phase"] == "control"]
    rows = [[r for r in b["rows"] if r["condition"] == condition] for b in selected]
    require(len(rows) == 4, "control_count_invalid")
    metrics = {}
    for metric in METRICS[:2]:
        samples = [[group[i][metric] for group in rows] for i in range(len(rows[0]))]
        metrics[metric] = {
            "between_process_case_range_seconds": distribution([max(v) - min(v) for v in samples]),
            "between_process_case_range_over_mean": distribution([(max(v) - min(v)) / statistics.mean(v) for v in samples])}
    return {"independent_processes": 4, "metrics": metrics,
            "per_block": [{"block": b["block"], "timing": timing_summary(group)} for b, group in zip(selected, rows)]}


def bind_files(bindings, verified):
    require(type(bindings) is dict and bindings, "bindings_invalid")
    for name, expected in bindings.items():
        if name not in verified:
            verified[name] = file_hash(name)
        require(verified[name] == expected, "artifact_changed")


def reference_data(directory, verified):
    directory = private_directory(directory)
    config = read(directory / "configuration.json")
    bind_files(config["files"], verified)
    pre, post = read(directory / "pre-run-seal.json"), read(directory / "post-run-seal.json")
    config_sha = digest(encode(config))
    require(config["smoke"] is False and config["rounds"] == 2
            and pre["configuration_sha256"] == post["configuration_sha256"] == config_sha
            and post["ownership_settled"] is True, "reference_seal_invalid")
    require(pre["dataset_sha256"] == post["dataset_sha256"] == file_hash(directory / "dataset.json"), "reference_dataset_invalid")
    cases = read(directory / "dataset.json")["cases"]
    provenance = read(PUBLIC / "DATASET_PROVENANCE.json", False)
    require(len(cases) == 243 and provenance["normalized_dataset_sha256"] == pre["dataset_sha256"]
            and [c["id"] for c in cases] == [c["id"] for c in provenance["ordered_cases"]]
            and [digest(encode(c)) for c in cases] == [c["case_sha256"] for c in provenance["ordered_cases"]],
            "reference_population_invalid")
    expected_blocks = {f"round-{r}-{e}.json" for r in (1, 2) for e in ("baseline", "candidate")}
    require(set(post["blocks"]) == expected_blocks and set(post["digests"]) == {"digest-baseline.json", "digest-candidate.json"},
            "reference_records_invalid")
    records = {}
    for name, expected in {**post["blocks"], **post["digests"]}.items():
        require(file_hash(directory / name) == expected, "reference_record_changed")
        record = read(directory / name)
        require(record["configuration_sha256"] == config_sha and record["complete"] is True
                and record["ownership_settled"] is True, "reference_record_invalid")
        records[name] = record
    worker = records["round-1-baseline.json"]
    require(len(worker["rows"]) == 486 and len(worker["boundaries"]) == 48, "reference_count_invalid")
    expected_order = [(c["id"], condition) for c in cases for condition in CONDITIONS]
    for name in expected_blocks:
        record = records[name]
        require([(r["case_id"], r["condition"]) for r in record["rows"]] == expected_order, "reference_order_invalid")
        for i, row in enumerate(record["rows"]):
            validate_row(row, cases[i // 2], worker["rows"][i - i % 2])
    requests = [r["input_token_ids"] for r in worker["rows"]] + [r["tokens"] for r in worker["boundaries"]]
    left, right = records["digest-baseline.json"], records["digest-candidate.json"]
    require(len(left["outputs"]) == len(right["outputs"]) == len(requests) == 534, "reference_digest_count_invalid")
    for a, b, tokens in zip(left["outputs"], right["outputs"], requests):
        require(a["input_tokens"] == len(tokens) and a["prompt_tokens_sha256"] == token_digest(tokens)
                and all(a[k] == b[k] for k in ("input_tokens", "prompt_tokens_sha256", "vocab_logits_sha256", "vocab_size")),
                "reference_digest_invalid")
    return cases, worker


def host_audit(directory, binding):
    samples = read(directory / "HOST_SAMPLES.json")
    receipt = read(directory / "HOST_ISOLATION.json")
    require(samples and receipt["benchmark_exit"] == 0 and receipt["failure_type"] is None
            and receipt["host_samples_sha256"] == file_hash(directory / "HOST_SAMPLES.json")
            and receipt["controller_sha256"] == file_hash(ROOT / "scripts/monitor_uzu_experiment.py")
            == binding["files"][str(ROOT / "scripts/monitor_uzu_experiment.py")], "host_receipt_invalid")
    for sample in samples:
        require(all(type(sample[k]) is int and sample[k] >= 0 for k in
                    ("external_models", "compilers", "owned_models", "background_large_rss"))
                and finite(sample["elapsed_seconds"]) and finite(sample["background_process_max_cpu"]), "host_sample_invalid")
    expected = {"sampling_interval_seconds": 5, "samples": len(samples), "suspended_processes": 0,
                "external_model_samples": sum(s["external_models"] > 0 for s in samples),
                "compiler_samples": sum(s["compilers"] > 0 for s in samples),
                "max_owned_models": max(s["owned_models"] for s in samples),
                "final_active_models": samples[-1]["external_models"] + samples[-1]["owned_models"],
                "maximum_background_process_cpu": max(s["background_process_max_cpu"] for s in samples),
                "maximum_background_large_rss": max(s["background_large_rss"] for s in samples)}
    require(all(receipt[k] == v for k, v in expected.items()) and expected["external_model_samples"] == 0
            and expected["compiler_samples"] == 0 and expected["max_owned_models"] <= 1
            and expected["final_active_models"] == 0, "host_isolation_failed")
    return {**expected, "limits": "Sampled process names and metadata; no continuous GPU, power, thermal, or clock measurements. Generic process names can conceal workloads."}


def exported_binding(binding):
    artifacts = []
    for name, sha in binding["files"].items():
        path = Path(name)
        artifacts.append({"sha256": sha, **({"path": str(path.relative_to(ROOT))}
            if path.is_absolute() and path.is_relative_to(ROOT) else {"external_artifact": True})})
    receipt = binding["candidate_receipt"]
    return {"schema_version": binding["schema_version"], "smoke": binding["smoke"], "rounds": binding["rounds"],
        "case_count": len(binding["case_ids"]), "ordered_case_ids_sha256": digest(encode(binding["case_ids"])),
        "warmups": binding["warmups"], "boundary_lengths": binding["boundary_lengths"],
        "temperature": binding["temperature"], "context_tokens": binding["context_tokens"], "artifacts": artifacts,
        "candidate_variant": receipt["variant"], "candidate_receipt_sha256": digest(encode(receipt)),
        "upstream_revision": receipt["upstream_revision"], "features": receipt["features"],
        "baseline_variant": binding["baseline_receipt"]["variant"] if binding.get("baseline_receipt") else "historical",
        "whole_vocabulary_probes_supplied": bool(binding["digest_binaries"])}


def exported_configuration(config):
    binding, machine = config["binding"], {}
    for key, pattern in {"architecture": r"(?:arm64|aarch64|x86_64)", "os_version": r"[0-9.]+",
                         "chip_model": r"Apple [A-Za-z0-9 -]+", "hardware_model": r"[A-Za-z]+[0-9]+,[0-9]+"}.items():
        value = binding.get("machine", {}).get(key)
        if type(value) is str and len(value) <= 160 and re.fullmatch(pattern, value):
            machine[key] = value
    ram = binding.get("machine", {}).get("physical_ram_bytes")
    if type(ram) is int and ram > 0:
        machine["physical_ram_bytes"] = ram
    return {"runtime_binding": exported_binding(binding), "machine": machine, "quartets": 4,
            "warmups_per_block": 3, "selected_case_count": 12,
            "ordered_selection_sha256": digest(encode(config["selection"])), "schedule": config["schedule"]}


def source_report(path, config_hash, post_hash):
    candidates = [Path(path)] if path else sorted(PUBLIC.glob("*.json"))
    matches = []
    for candidate in candidates:
        value = read(candidate, False)
        if (type(value) is dict and value.get("configuration_sha256") == config_hash and value.get("post_run_seal_sha256") == post_hash
                and "per_quartet" in value and value.get("status") == "complete"):
            matches.append((candidate, value))
    require(len(matches) == 1, "source_report_not_unique")
    return matches[0]


def validate_public_report(report, config, directory, summaries, control):
    expected = {"schema_version": 1, "status": "complete", "mode": "full",
        "configuration_sha256": digest(encode(config)), "post_run_seal_sha256": file_hash(directory / "post-run-seal.json"),
        "configuration": exported_configuration(config),
        "selection": {"cases": 12, "selection_sha256": digest(encode(config["selection"])),
            "rule": "Midpoint length quartiles within each public token bucket; ties use original dataset order.",
            "bucket_token_counts": {name: [s["input_tokens"] for s in config["selection"] if s["bucket"] == name]
                                    for name, _, _ in BUCKETS}},
        "quartets": 4, "blocks": 20, "measurements": 480, "comparison_measurements": 384, "control_measurements": 96,
        "candidate_outputs_exactly_equal_to_full_baseline": True,
        "whole_vocabulary_validation": "No new whole-vocabulary inference. Prior sealed full-run probes validate their bound binaries; alternate candidate builds require their own validation.",
        "per_quartet": summaries,
        "quartet_level_wall_ratio_distribution": {condition: distribution([s["metrics"]["wall_seconds"]
            ["baseline_over_candidate_ratio_of_means"] for s in summaries[condition]]) for condition in CONDITIONS},
        "baseline_only_process_noise": control,
        "timing_limits": "Three warmups and process startup excluded. Quartets are the replication units. Four baseline-only processes describe local process/time variation. No request-level bootstrap confidence interval or precise steady-state speed claim."}
    require(exact(report, expected), "public_report_mismatch")


def analyze(args):
    directory = private_directory(args.output)
    own_hash = file_hash(__file__)
    protocol_hash = file_hash(RESUMED_PROTOCOL)
    config = read(directory / "configuration.json")
    binding = config["binding"]
    verified = {}
    bind_files(binding["files"], verified)
    require(config["quartets"] == 4 and config["warmups"] == 3 and config["schedule"] == schedule()
            and binding["temperature"] == 1.22 and binding["context_tokens"] == 8192
            and binding["candidate_receipt"]["variant"] == "all", "configuration_invalid")
    receipt = binding.get("baseline_receipt")
    baseline_sha = COMBINED if receipt else HISTORICAL
    require(verified[binding["baseline_binary"]] == baseline_sha
            and (receipt is None or receipt["schema_version"] == 1 and receipt["variant"] == "combined"
                 and receipt["binary_sha256"] == COMBINED), "baseline_identity_invalid")
    candidate = binding["candidate_receipt"]
    require(candidate["schema_version"] == 2 and candidate["upstream_revision"] == REVISION
            and candidate["binary_sha256"] == verified[binding["candidate_binary"]], "candidate_identity_invalid")
    acceptance_path = PUBLIC / "DEEP_VALIDATION_RESULTS.json"
    acceptance_audit_path = PUBLIC / "DEEP_VALIDATION_AUDIT.json"
    acceptance, acceptance_audit = read(acceptance_path, False), read(acceptance_audit_path, False)
    require(acceptance["status"] == "complete" and acceptance["whole_vocabulary_logits_exactly_equal"] is True
            and acceptance["configuration"]["candidate_receipt_sha256"] == digest(encode(candidate))
            and acceptance_audit["status"] == "passed"
            and acceptance_audit["report"]["sha256"] == file_hash(acceptance_path), "candidate_acceptance_invalid")
    cases, reference = reference_data(config["reference"], verified)
    chosen = selection(cases, reference["rows"])
    require(config["selection"] == chosen, "selection_invalid")
    by_case = {c["id"]: c for c in cases}
    by_reference = {r["case_id"]: r for r in reference["rows"] if r["condition"] == "fresh"}
    pre, post = read(directory / "pre-run-seal.json"), read(directory / "post-run-seal.json")
    config_hash = digest(encode(config))
    require(pre["configuration_sha256"] == post["configuration_sha256"] == config_hash
            and post["ownership_settled"] is True, "seal_invalid")
    expected_names = {f"block-{i:02d}.json" for i in range(1, 21)}
    require(set(post["blocks"]) == expected_names and {p.name for p in directory.glob("block-*.json")} == expected_names,
            "blocks_incomplete")
    blocks, errors = [], []
    ordering = [(s["case_id"], condition) for s in chosen for condition in CONDITIONS]
    for item in schedule():
        path = directory / f"block-{item['block']:02d}.json"
        require(file_hash(path) == post["blocks"][path.name], "block_hash_invalid")
        block = read(path)
        require(all(block[k] == v for k, v in item.items()) and block["configuration_sha256"] == config_hash
                and block["complete"] is True and block["ownership_settled"] is True and block["warmups"] == 3
                and finite(block["startup_seconds"]) and type(block["peak_rss_bytes"]) is int
                and block["peak_rss_bytes"] > 0, "block_invalid")
        require([(r["case_id"], r["condition"]) for r in block["rows"]] == ordering, "row_order_invalid")
        for row in block["rows"]:
            errors.append(validate_row(row, by_case[row["case_id"]], by_reference[row["case_id"]]))
        blocks.append(block)
    groups = [[b for b in blocks if b["phase"] == "comparison" and b["quartet"] == q] for q in range(1, 5)]
    summaries = {condition: [quartet_summary(group, condition) for group in groups] for condition in CONDITIONS}
    control = {condition: controls(blocks, condition) for condition in CONDITIONS}
    source_path, report = source_report(args.source_report, config_hash, file_hash(directory / "post-run-seal.json"))
    validate_public_report(report, config, directory, summaries, control)
    host = host_audit(directory, binding)
    buckets = {}
    for bucket, _, _ in (("all", 0, 8193), *BUCKETS):
        subset = chosen if bucket == "all" else [s for s in chosen if s["bucket"] == bucket]
        ids = {s["case_id"] for s in subset}
        conditions = {}
        for condition in CONDITIONS:
            quartets = [quartet_summary(group, condition, ids) for group in groups]
            metrics = {}
            for metric in METRICS:
                ratios = [q["metrics"][metric]["baseline_over_candidate_ratio_of_means"] for q in quartets]
                available = [v for v in ratios if v is not None]
                metrics[metric] = {"quartet_ratios": ratios, "quartet_ratio_distribution": distribution(available),
                    "quartets_favoring_candidate": sum(v > 1 for v in available),
                    "candidate_latency_reduction_fraction": distribution([1 - 1 / v for v in available if v > 0])}
            conditions[condition] = {"per_quartet": quartets, "metrics": metrics}
        buckets[bucket] = {"cases": len(subset), "input_token_lengths": [s["input_tokens"] for s in subset],
            "last_layer_pruning_active_cases": sum(s["input_tokens"] > 1024 for s in subset), "conditions": conditions}
    memory = {engine: {phase: distribution([b["peak_rss_bytes"] for b in blocks if b["engine"] == engine
                                           and b["phase"] == phase]) for phase in ("control", "comparison")}
              for engine in ("baseline", "candidate")}
    provenance_paths = [Path(__file__), RESUMED_PROTOCOL, acceptance_path, acceptance_audit_path,
                        source_path, directory / "configuration.json", directory / "pre-run-seal.json",
                        directory / "post-run-seal.json", directory / "HOST_SAMPLES.json", directory / "HOST_ISOLATION.json"]
    provenance = []
    for path in provenance_paths:
        path = path.absolute()
        provenance.append({"sha256": file_hash(path), **({"path": str(path.relative_to(ROOT))}
            if path.is_relative_to(ROOT) else {"external_artifact": True})})
    result = {"schema_version": 1, "status": "passed", "method": "Independent standard-library source and record audit; no evaluation, aggregate, or model helpers imported.",
        "provenance": provenance, "bound_artifacts_rehashed": len(verified), "original_public_report_exactly_reproduced": True,
        "configuration_sha256": config_hash, "baseline_variant": "combined" if receipt else "historical",
        "baseline_binary_sha256": baseline_sha, "candidate_variant": "all", "candidate_binary_sha256": candidate["binary_sha256"],
        "blocks": 20, "quartets": 4, "measured_responses": 480, "comparison_responses": 384, "control_responses": 96,
        "warmup_calls_checked_during_acquisition": 60, "warmup_responses_retained_for_independent_audit": 0,
        "outputs_equal_to_sealed_reference": True, "floating_equality_scope": "Decoded candidate scores, probabilities and typed-decision floats checked by binary64 bits; exact token identities checked privately.",
        "maximum_calibration_formula_error": max(errors), "by_bucket": buckets,
        "baseline_controls": control, "peak_resident_memory_bytes": memory, "host_isolation": host,
        "limitations": ["Quartets are replication units; four local quartets do not establish a precise steady-state or population-wide gain.",
            "Ratio of case means gives slower cases more influence. Length buckets contain four selected cases each.",
            "No new whole-vocabulary inference was run. Exactness acceptance for the candidate must be cited separately.",
            "Warmups were checked during acquisition but their responses were not retained by the timing harness.",
            "Last-layer pruning is active only above 1024 tokens in this scorer; bucket comparisons cannot isolate component contributions.",
            "Peak RSS is the process-owner high-water estimate, not a direct measurement of model-only or GPU allocation."]}
    require(file_hash(__file__) == own_hash and file_hash(RESUMED_PROTOCOL) == protocol_hash, "analysis_source_changed")
    destination = Path(args.report).absolute()
    require(destination.is_relative_to(PUBLIC) and destination.suffix == ".json"
            and not any(p.is_symlink() for p in (destination, *destination.parents)), "report_destination_invalid")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encode(result))
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"stage": "independent_timing_audit_complete", "blocks": 20, "measurements": 480}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--source-report", type=Path)
    args = parser.parse_args()
    try:
        analyze(args)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "independent_timing_audit_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
