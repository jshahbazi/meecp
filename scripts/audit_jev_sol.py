"""Independent standard-library audit of Sol Uzu, accepted Uzu and llama.cpp BF16."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import struct

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "experiments/uzu-rust/jev-scoring"
ENGINES = ("llama.cpp", "uzu-control", "uzu")
CONDITIONS = ("fresh", "identical_repeat")
BUCKETS = (("under_256", 0, 256), ("256_to_1023", 256, 1024),
           ("1024_to_4095", 1024, 4096), ("4096_plus", 4096, 8192))
IDENTITY = ("input_token_ids", "candidate_token_ids", "logits", "log_probabilities", "probabilities", "decision")


class AuditError(ValueError):
    """Only authored diagnostic codes may reach stdout."""


def require(value, code):
    if not value:
        raise AuditError(code)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def ordinary(path):
    path = Path(path).absolute()
    require(".." not in path.parts and path.is_file()
            and not any(p.is_symlink() for p in (path, *path.parents)), "file_invalid")
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
    if type(left) is not type(right):
        return False
    if type(left) is float:
        return struct.pack("<d", left) == struct.pack("<d", right)
    if type(left) is list:
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right))
    if type(left) is dict:
        return list(left) == list(right) and all(exact(left[k], right[k]) for k in left)
    return left == right


def finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def percentile(values, quantile):
    values = sorted(values)
    if not values:
        return None
    at = (len(values) - 1) * quantile
    lo, hi = math.floor(at), math.ceil(at)
    return values[lo] if lo == hi else values[lo] * (hi - at) + values[hi] * (at - lo)


def token_digest(tokens):
    require(type(tokens) is list and 0 < len(tokens) < 8192
            and all(type(t) is int and 0 <= t < 2**32 for t in tokens), "tokens_invalid")
    return sha(b"".join(t.to_bytes(4, "little") for t in tokens))


def option_keys(case):
    question = case["question"]
    if question["type"] == "noul":
        return ["true", "false"]
    if question["type"] == "score":
        return [str(i) for i in range(len(question["criteria"]))]
    require(question["type"] == "choice", "question_type_invalid")
    return list(dict.fromkeys(question["criteria"]))


def rendered(case):
    question = case["question"]
    criteria = question.get("criteria")
    if question["type"] == "noul":
        pairs = [(k, (criteria or {}).get(k) or f"The proposition is {k}.") for k in ("true", "false")]
    elif question["type"] == "choice":
        if isinstance(criteria, list):
            criteria = dict.fromkeys(criteria)
        pairs = [(k, v or k) for k, v in criteria.items()]
    else:
        pairs = [(str(i), level) for i, level in enumerate(criteria)]
    payload = {"evidence": case["state"], "criterion": question["instructions"],
        "options": [{"letter": "ABCDEFGHIJKLMNOP"[i], "description": f"{key}: {description}"}
                    for i, (key, description) in enumerate(pairs)]}
    system = ("Apply the supplied criterion to the supplied evidence. Choose exactly one listed option. "
              "Respond with only its uppercase letter, with no explanation or reasoning.")
    user = json.dumps(payload, ensure_ascii=False)
    return (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def recalibrate(values):
    require(type(values) is list and 2 <= len(values) <= 16
            and all(type(z) in (int, float) and math.isfinite(z) for z in values), "scores_invalid")
    weights = [math.exp((z - max(values)) / 1.22) for z in values]
    total = math.fsum(weights)
    return [w / total for w in weights]


def decision(case, probabilities, tokens):
    keys = option_keys(case)
    probs = dict(zip(keys, probabilities, strict=True))
    out = {"type": case["question"]["type"], "confidence": max(probabilities), "input_tokens": tokens}
    if out["type"] == "noul":
        out["noul"] = probabilities[0]
    elif out["type"] == "choice":
        winner = max(range(len(probabilities)), key=probabilities.__getitem__)
        out.update(choice=keys[winner], probabilities=probs)
    else:
        out.update(score=sum(index * p for index, p in enumerate(probabilities)), probabilities=probs)
    return out


def validate_row(row, case, reference, engine, condition):
    tokens, candidates, probabilities = row["input_token_ids"], row["candidate_token_ids"], row["probabilities"]
    require(row["case_id"] == case["id"] and row["condition"] == condition and row["truncated"] is False
            and row["input_tokens"] == len(tokens), "row_identity_invalid")
    digest = token_digest(tokens)
    require(row["prompt_tokens_sha256"] == digest and row["prompt_sha256"] == sha(rendered(case).encode()),
            "prompt_identity_invalid")
    keys = option_keys(case)
    require(2 <= len(keys) <= 16 and len(keys) == len(candidates) == len(probabilities)
            and len(set(candidates)) == len(candidates)
            and all(type(t) is int and 0 <= t < 2**32 for t in candidates), "candidate_identity_invalid")
    require(all(finite(p) and p <= 1 for p in probabilities)
            and abs(math.fsum(probabilities) - 1) <= 1e-12, "probabilities_invalid")
    values = [row["log_probabilities"]] + ([row["logits"]] if engine in ("uzu", "uzu-control") else [])
    errors = [abs(p - q) for vector in values for p, q in zip(probabilities, recalibrate(vector), strict=True)]
    require(max(errors) <= 1e-12 and all(v <= 1e-12 for v in row["log_probabilities"]), "calibration_invalid")
    require(exact(row["decision"], decision(case, probabilities, len(tokens))), "typed_decision_invalid")
    require(row["cache_hit"] is (engine == "llama.cpp" and condition == "identical_repeat"), "cache_policy_invalid")
    require(finite(row["tokenization_seconds"]) and finite(row["score_request_seconds"], True)
            and finite(row["wall_seconds"], True), "timing_invalid")
    require(all(finite(v) for v in row["engine_metrics"].values()), "native_metrics_invalid")
    require(exact(tokens, reference["input_token_ids"]) and exact(candidates, reference["candidate_token_ids"]),
            "reference_tokens_invalid")
    if engine in ("uzu", "uzu-control"):
        require({"state_creation_seconds", "prefill_seconds", "readout_seconds", "score_seconds", "total_seconds"}
                <= set(row["engine_metrics"]), "uzu_native_metrics_incomplete")
        require(all(exact(row[k], reference[k]) for k in IDENTITY), "native_output_bits_invalid")
    else:
        require(row["logits"] is None and set(row["engine_metrics"])
                == {"prefill_seconds", "one_token_seconds", "prompt_tokens_evaluated"}, "llama_native_metrics_invalid")
        evaluated = row["engine_metrics"]["prompt_tokens_evaluated"]
        require(type(evaluated) is int and (evaluated == len(tokens) if condition in ("fresh", "warmup")
                                          else 0 < evaluated < len(tokens)), "llama_cache_evaluation_invalid")
    return max(errors)


def ece(rows):
    bins = [[] for _ in range(10)]
    for row in rows:
        bins[min(int(row["confidence"] * 10), 9)].append(row)
    return math.fsum(len(part) / len(rows) * abs(statistics.mean(r["correct"] for r in part)
                    - statistics.mean(r["confidence"] for r in part)) for part in bins if part)


def quality_row(case, row):
    probabilities = row["probabilities"]
    keys = option_keys(case)
    labels = ["yes", "no"] if case["question"]["type"] == "noul" else keys
    expected = str(case["expected"])
    winner = max(range(len(probabilities)), key=probabilities.__getitem__)
    score = row["decision"].get("score")
    return {"correct": labels[winner] == expected, "confidence": max(probabilities),
        "brier": math.fsum((p - (label == expected))**2 for label, p in zip(labels, probabilities, strict=True)),
        "ordinal_mae": abs(score - int(expected)) if score is not None else None,
        "rounded_ev_correct": round(score) == int(expected) if score is not None else None}


def quality_summary(rows):
    require(rows, "quality_population_empty")
    ordinal = [r for r in rows if r["ordinal_mae"] is not None]
    return {"cases": len(rows), "argmax_correct": sum(r["correct"] for r in rows),
        "argmax_accuracy": statistics.mean(r["correct"] for r in rows),
        "brier": statistics.mean(r["brier"] for r in rows), "ece_10_bins": ece(rows),
        "mean_confidence": statistics.mean(r["confidence"] for r in rows), "ordinal_cases": len(ordinal),
        "ordinal_mae": statistics.mean(r["ordinal_mae"] for r in ordinal) if ordinal else None,
        "rounded_ev_accuracy": statistics.mean(r["rounded_ev_correct"] for r in ordinal) if ordinal else None}


def timing_summary(rows):
    native = {}
    for metric in sorted({k for row in rows for k in row["engine_metrics"] if k.endswith("_seconds")}):
        values = [r["engine_metrics"][metric] for r in rows if metric in r["engine_metrics"]]
        native[metric] = {"samples": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
    return {"samples": len(rows), "cache_hits": sum(r["cache_hit"] for r in rows),
        **{k: {"p50": percentile([r[k] for r in rows], .5), "p95": percentile([r[k] for r in rows], .95)}
           for k in ("tokenization_seconds", "score_request_seconds", "wall_seconds")},
        "engine_metrics_seconds": native}


def schedule():
    orders = (("llama.cpp", "uzu-control", "uzu", "uzu", "uzu-control", "llama.cpp"),
              ("uzu-control", "uzu", "llama.cpp", "llama.cpp", "uzu", "uzu-control"))
    blocks = [{"cycle": cycle, "position": i, "engine": engine}
              for cycle, order in enumerate(orders, 1) for i, engine in enumerate(order, 1)]
    return [{"block": i, **item} for i, item in enumerate(blocks, 1)]


def pair_metrics(blocks, condition, ids, left_engine):
    groups = {engine: [[r for r in b["rows"] if r["condition"] == condition and r["case_id"] in ids]
                      for b in blocks if b["engine"] == engine] for engine in (left_engine, "uzu")}
    require(all(len(v) == 2 for v in groups.values()), "cycle_unbalanced")
    order = [r["case_id"] for r in groups[left_engine][0]]
    require(all([r["case_id"] for r in group] == order for rows in groups.values() for group in rows),
            "cycle_pairing_invalid")
    if not order:
        return {"cases": 0, "wall_ratio_of_means": None, "score_ratio_of_means": None,
                "median_paired_wall_ratio": None}
    means = {engine: [{metric: statistics.mean(group[i][metric] for group in rows)
                      for metric in ("wall_seconds", "score_request_seconds")} for i in range(len(order))]
             for engine, rows in groups.items()}
    left, right = means[left_engine], means["uzu"]
    return {"cases": len(order), "wall_ratio_of_means": statistics.mean(r["wall_seconds"] for r in left)
            / statistics.mean(r["wall_seconds"] for r in right),
        "score_ratio_of_means": statistics.mean(r["score_request_seconds"] for r in left)
            / statistics.mean(r["score_request_seconds"] for r in right),
        "median_paired_wall_ratio": percentile([a["wall_seconds"] / b["wall_seconds"]
                                               for a, b in zip(left, right, strict=True)], .5)}


def checked_acceptance(report_path, audit_path, receipt):
    report, audited = read(report_path, False), read(audit_path, False)
    digest = sha(encoded(receipt))
    require(report["status"] == "complete" and report["candidate_outputs_exactly_equal"] is True
            and report["whole_vocabulary_logits_exactly_equal"] is True
            and report["configuration"]["candidate_receipt_sha256"] == digest,
            "acceptance_receipt_invalid")
    audit_report_hash = audited.get("source_report_sha256", audited.get("report", {}).get("sha256"))
    require(audited["status"] == "passed" and audit_report_hash == file_hash(report_path)
            and audited["configuration_sha256"] == report["configuration_sha256"]
            and audited["whole_vocabulary_logits_exactly_equal"] is True
            and audited["candidate_float_bits_equal"] is True
            and audited["public_report_matches_independent_recalculation"] is True,
            "acceptance_audit_invalid")
    return report



def comparison_summary(pairs):
    require(pairs, "comparison_empty")
    differences = [[abs(x - y) for x, y in zip(a["probabilities"], b["probabilities"], strict=True)]
                   for a, b in pairs]
    agrees = [max(range(len(a["probabilities"])), key=a["probabilities"].__getitem__)
              == max(range(len(b["probabilities"])), key=b["probabilities"].__getitem__) for a, b in pairs]
    return {"cases": len(pairs), "argmax_agreement_count": sum(agrees),
        "argmax_agreement": statistics.mean(agrees),
        "strict_probability_match_count": sum(max(v) <= 1e-3 for v in differences),
        "close_probability_match_count": sum(max(v) <= 1e-2 for v in differences),
        "maximum_option_absolute_difference": max(max(v) for v in differences),
        "mean_option_absolute_difference": sum(statistics.mean(v) * len(v) for v in differences)
            / sum(len(v) for v in differences),
        "mean_l1": statistics.mean(math.fsum(v) for v in differences),
        "mean_total_variation": statistics.mean(math.fsum(v) / 2 for v in differences),
        "maximum_confidence_difference": max(abs(a["decision"]["confidence"] - b["decision"]["confidence"])
                                              for a, b in pairs),
        "maximum_noul_difference": max((abs(a["decision"]["noul"] - b["decision"]["noul"])
                                         for a, b in pairs if a["decision"]["type"] == "noul"), default=None),
        "maximum_expected_score_difference": max((abs(a["decision"]["score"] - b["decision"]["score"])
                                                   for a, b in pairs if a["decision"]["type"] == "score"), default=None)}


def check_dataset(path):
    cases = read(path)["cases"]
    provenance = read(PUBLIC / "DATASET_PROVENANCE.json", False)
    require(len(cases) == 243 and len({c["id"] for c in cases}) == 243
            and file_hash(path) == provenance["normalized_dataset_sha256"]
            and [c["id"] for c in cases] == [r["id"] for r in provenance["ordered_cases"]]
            and [sha(encoded(c)) for c in cases] == [r["case_sha256"] for r in provenance["ordered_cases"]],
            "dataset_provenance_invalid")
    require({tier: sum(c["tier"] == tier for c in cases) for tier in ("easy", "original", "hard", "synthetic")}
            == {"easy": 48, "original": 72, "hard": 111, "synthetic": 12}, "dataset_tiers_invalid")
    return cases


def reference_data(directory):
    directory = private_directory(directory)
    config, pre, post = (read(directory / name) for name in
                         ("configuration.json", "pre-run-seal.json", "post-run-seal.json"))
    digest = sha(encoded(config))
    require(config["smoke"] is False and config["rounds"] == 2
            and pre["configuration_sha256"] == post["configuration_sha256"] == digest
            and pre["dataset_sha256"] == post["dataset_sha256"] == file_hash(directory / "dataset.json")
            and post["ownership_settled"] is True, "reference_seal_invalid")
    cases = check_dataset(directory / "dataset.json")
    require(config["case_ids"] == [c["id"] for c in cases], "reference_population_invalid")
    expected = {f"round-{r}-{e}.json" for r in (1, 2) for e in ("baseline", "candidate")}
    require(set(post["blocks"]) == expected and set(post["digests"])
            == {"digest-baseline.json", "digest-candidate.json"}, "reference_records_invalid")
    records = {}
    for name, expected_hash in {**post["blocks"], **post["digests"]}.items():
        require(file_hash(directory / name) == expected_hash, "reference_record_hash_invalid")
        record = read(directory / name)
        require(record["configuration_sha256"] == digest and record["complete"] is True
                and record["ownership_settled"] is True, "reference_record_invalid")
        records[name] = record
    worker = records["round-1-baseline.json"]
    require(len(worker["rows"]) == 486 and len(worker["boundaries"]) == 48, "reference_counts_invalid")
    ordering = [(c["id"], condition) for c in cases for condition in CONDITIONS]
    for name in expected:
        rows = records[name]["rows"]
        require([(r["case_id"], r["condition"]) for r in rows] == ordering, "reference_order_invalid")
        require(all(all(exact(row[k], worker["rows"][i - i % 2][k]) for k in IDENTITY)
                    for i, row in enumerate(rows)), "reference_outputs_invalid")
    requests = [r["input_token_ids"] for r in worker["rows"]] + [r["tokens"] for r in worker["boundaries"]]
    left, right = records["digest-baseline.json"], records["digest-candidate.json"]
    require(len(left["outputs"]) == len(right["outputs"]) == len(requests) == 534, "reference_digests_incomplete")
    for a, b, tokens in zip(left["outputs"], right["outputs"], requests, strict=True):
        expected_token_digest = sha(b"".join(t.to_bytes(4, "little") for t in tokens))
        require(a["input_tokens"] == len(tokens) and a["prompt_tokens_sha256"] == expected_token_digest
                and all(a[k] == b[k] for k in ("input_tokens", "prompt_tokens_sha256", "vocab_logits_sha256", "vocab_size")),
                "reference_digest_invalid")
    return cases, {r["case_id"]: r for r in worker["rows"] if r["condition"] == "fresh"}


def host_audit(directory, config):
    samples, receipt = read(directory / "HOST_SAMPLES.json"), read(directory / "HOST_ISOLATION.json")
    monitor = ROOT / "scripts/monitor_jev_sol.py"
    require(samples and receipt["benchmark_exit"] == 0 and receipt["failure_type"] is None
            and receipt["host_samples_sha256"] == file_hash(directory / "HOST_SAMPLES.json")
            and receipt["controller_sha256"] == file_hash(monitor) == config["files"][str(monitor)],
            "host_receipt_invalid")
    previous_time, previous_blocks = -1, 0
    for sample in samples:
        require(all(type(sample[k]) is int and sample[k] >= 0 for k in
                    ("external_models", "generic_inference", "compilers", "owned_models", "background_large_rss"))
                and finite(sample["elapsed_seconds"]) and finite(sample["background_process_max_cpu"])
                and sample["elapsed_seconds"] >= previous_time, "host_sample_invalid")
        previous_time = sample["elapsed_seconds"]
        if "completed_blocks" in sample:
            require(type(sample["completed_blocks"]) is int and previous_blocks <= sample["completed_blocks"] <= 12,
                    "host_block_progress_invalid")
            previous_blocks = sample["completed_blocks"]
    expected = {"sampling_interval_seconds": 5, "samples": len(samples), "suspended_processes": 0,
        "external_model_samples": sum(s["external_models"] > 0 for s in samples),
        "generic_inference_samples": sum(s["generic_inference"] > 0 for s in samples),
        "compiler_samples": sum(s["compilers"] > 0 for s in samples),
        "background_cpu_limit_percent": 200,
        "high_background_cpu_samples": sum(s["background_process_max_cpu"] > 200 for s in samples),
        "max_owned_models": max(s["owned_models"] for s in samples),
        "final_active_models": samples[-1]["external_models"] + samples[-1]["generic_inference"] + samples[-1]["owned_models"],
        "maximum_background_process_cpu": max(s["background_process_max_cpu"] for s in samples),
        "maximum_background_large_rss": max(s["background_large_rss"] for s in samples)}
    require(all(receipt[k] == v for k, v in expected.items()) and expected["external_model_samples"] == 0
            and expected["generic_inference_samples"] == 0 and expected["compiler_samples"] == 0
            and expected["high_background_cpu_samples"] == 0
            and expected["max_owned_models"] <= 1 and expected["final_active_models"] == 0,
            "host_isolation_invalid")
    return expected


def audit(args):
    directory = private_directory(args.output)
    config, pre, post = (read(directory / name) for name in
                         ("configuration.json", "pre-run-seal.json", "post-run-seal.json"))
    digest = sha(encoded(config))
    require(config["mode"] == "sol-uzu-control-llama-bf16-full" and config["schedule"] == schedule()
            and config["warmups_per_block"] == 3 and config["measured_responses"] == 5832
            and config["case_count"] == 243 and config["temperature"] == 1.22 and config["context_tokens"] == 8192
            and config["uzu_probabilities"] == "native-reported" and config["fresh_then_identical_repeat"] is True,
            "configuration_invalid")
    require(pre["configuration_sha256"] == post["configuration_sha256"] == digest
            and pre["dataset_sha256"] == post["dataset_sha256"] == file_hash(directory / "dataset.json")
            and post["ownership_settled"] is True, "seal_invalid")
    require(str(Path(__file__).resolve()) in config["files"], "audit_source_not_bound")
    for name, expected in config["files"].items():
        require(file_hash(name) == expected, "bound_artifact_changed")
    historical = read(PUBLIC / "RESULTS.json", False)
    runtime = config["historical_runtime_binding"]
    require(runtime["profile"]["llama_reference"] == "bf16"
            and {k: v for k, v in runtime["artifacts"].items() if k != "gguf_sha256"}
            == {k: v for k, v in historical["configuration"]["artifacts"].items() if k != "gguf_sha256"},
            "historical_runtime_changed")
    bf16_control = read(PUBLIC / "BF16_CONTROL_PROVENANCE.json", False)
    require(runtime["artifacts"]["gguf_sha256"] == bf16_control["llama_reference"]["sha256"], "bf16_gguf_changed")
    require(str(ordinary(args.acceptance)) == config["acceptance_path"]
            and str(ordinary(args.acceptance_audit)) == config["acceptance_audit_path"]
            and str(ordinary(args.acceptance)) in config["files"]
            and str(ordinary(args.acceptance_audit)) in config["files"], "acceptance_path_invalid")
    checked_acceptance(args.acceptance, args.acceptance_audit, config["candidate_receipt"])
    require(config["candidate_receipt"]["schema_version"] == 3
            and config["candidate_receipt"]["variant"] == "combined"
            and config["control_receipt"]["variant"] == "all", "candidate_variant_invalid")
    checked_acceptance(PUBLIC / "DEEP_VALIDATION_RESULTS.json",
                       PUBLIC / "DEEP_VALIDATION_AUDIT.json", config["control_receipt"])
    require(all(str(PUBLIC / name) in config["files"] for name in
                ("DEEP_VALIDATION_RESULTS.json", "DEEP_VALIDATION_AUDIT.json")),
            "control_acceptance_not_bound")
    cases = check_dataset(directory / "dataset.json")
    reference_cases, reference = reference_data(config["reference"])
    require(exact(cases, reference_cases) and config["case_ids"] == [c["id"] for c in cases], "population_invalid")
    expected_names = {f"block-{i:02d}.json" for i in range(1, 13)}
    require(set(post["blocks"]) == expected_names
            and {p.name for p in directory.glob("block-*.json")} == expected_names, "blocks_incomplete")
    blocks, errors = [], []
    ordering = [(c["id"], condition) for c in cases for condition in CONDITIONS]
    for item in schedule():
        path = directory / f"block-{item['block']:02d}.json"
        require(file_hash(path) == post["blocks"][path.name], "block_hash_invalid")
        block = read(path)
        require(all(block[k] == v for k, v in item.items()) and block["configuration_sha256"] == digest
                and block["complete"] is True and block["ownership_settled"] is True
                and finite(block["startup_seconds"], True) and type(block["peak_rss_bytes"]) is int
                and block["peak_rss_bytes"] > 0 and len(block["warmups"]) == 3, "block_invalid")
        require([(r["case_id"], r["condition"]) for r in block["rows"]] == ordering, "row_coverage_invalid")
        for row in block["warmups"]:
            require(row["engine"] == item["engine"], "warmup_engine_invalid")
            errors.append(validate_row(row, cases[0], reference[cases[0]["id"]], item["engine"], "warmup"))
        for i, row in enumerate(block["rows"]):
            case = cases[i // 2]
            require(row["engine"] == item["engine"], "row_engine_invalid")
            errors.append(validate_row(row, case, reference[case["id"]], item["engine"], CONDITIONS[i % 2]))
        blocks.append(block)
    first = {engine: {r["case_id"]: r for r in next(b for b in blocks if b["engine"] == engine)["rows"]
                      if r["condition"] == "fresh"} for engine in ENGINES}
    public = [c for c in cases if c["tier"] != "synthetic"]
    quality = {engine: {tier: quality_summary([quality_row(c, first[engine][c["id"]]) for c in cases
                       if (c["tier"] != "synthetic") == (tier == "public")]) for tier in ("public", "synthetic")}
               for engine in ENGINES}
    require(all(exact(quality[e]["public"], quality_summary([quality_row(c, reference[c["id"]]) for c in public]))
                for e in ("uzu", "uzu-control")),
            "native_reference_quality_invalid")
    timing, cycles = {}, {}
    for condition in CONDITIONS:
        timing[condition] = {engine: timing_summary([r for b in blocks if b["engine"] == engine
                             for r in b["rows"] if r["condition"] == condition]) for engine in ENGINES}
        cycles[condition] = []
        for cycle in (1, 2):
            group = [b for b in blocks if b["cycle"] == cycle]
            comparisons = {}
            for left, label in (("llama.cpp", "llama_over_candidate"), ("uzu-control", "control_over_candidate")):
                comparisons[label] = {
                    "all": pair_metrics(group, condition, {c["id"] for c in cases}, left),
                    "public": pair_metrics(group, condition, {c["id"] for c in public}, left),
                    "token_buckets": {name: pair_metrics(group, condition,
                        {c["id"] for c in cases if low <= len(reference[c["id"]]["input_token_ids"]) < high}, left)
                        for name, low, high in BUCKETS}}
            cycles[condition].append({"cycle": cycle, **comparisons})
    stability = {engine: {"comparisons": 1944, "maximum_option_difference": max(
        abs(p - q) for b in blocks if b["engine"] == engine for r in b["rows"]
        for p, q in zip(first[engine][r["case_id"]]["probabilities"], r["probabilities"], strict=True))}
        for engine in ENGINES}
    probability = {tier: comparison_summary([(first["llama.cpp"][c["id"]], first["uzu"][c["id"]])
                   for c in cases if (c["tier"] != "synthetic") == (tier == "public")]) for tier in ("public", "synthetic")}
    report = read(args.source_report, False)
    expected_scalar_groups = {"quality": quality, "timing": timing, "per_cycle": cycles,
        "within_engine_stability": stability, "maximum_calibration_formula_error": max(errors),
        "uzu_minus_llama": {k: quality["uzu"]["public"][k] - quality["llama.cpp"]["public"][k]
                            for k in ("argmax_accuracy", "brier", "ece_10_bins")},
        "per_block_timing": [{"block": b["block"], "engine": b["engine"],
            "conditions": {c: timing_summary([r for r in b["rows"] if r["condition"] == c]) for c in CONDITIONS}}
                             for b in blocks],
        "loads": [{k: b[k] for k in ("block", "cycle", "engine", "startup_seconds", "peak_rss_bytes", "ownership_settled")}
                  for b in blocks]}
    require(all(exact(report[k], value) for k, value in expected_scalar_groups.items()), "public_scalar_reproduction_failed")
    require(exact(report["probability_comparison"], probability), "probability_reproduction_failed")
    require(report["status"] == "complete" and report["configuration_sha256"] == digest
            and report["mode"] == config["mode"]
            and report["candidate_variant"] == config["candidate_receipt"]["variant"]
            and report["post_run_seal_sha256"] == file_hash(directory / "post-run-seal.json")
            and report["candidate_receipt_sha256"] == sha(encoded(config["candidate_receipt"]))
            and report["uzu_outputs_exactly_equal_to_reference"] is True
            and report["uzu_quality_exactly_equal_to_reference"] is True
            and {k: report[k] for k in ("cases", "public_cases", "synthetic_cases", "blocks", "cycles",
                                       "measured_responses", "checked_warmups", "temperature", "context_tokens")}
            == {"cases": 243, "public_cases": 231, "synthetic_cases": 12, "blocks": 12, "cycles": 2,
                "measured_responses": 5832, "checked_warmups": 36, "temperature": 1.22, "context_tokens": 8192},
            "public_identity_invalid")
    require(report["artifacts"] == [{"path": str(Path(p).relative_to(ROOT)), "sha256": h}
                                   for p, h in config["files"].items()], "public_artifacts_invalid")
    host = host_audit(directory, config)
    return {"schema_version": 1, "status": "passed", "method": "Independent standard-library calculations; no evaluator or aggregate helpers imported.",
        "audit_source_sha256": file_hash(__file__), "source_report_sha256": file_hash(args.source_report),
        "configuration_sha256": digest, "post_run_seal_sha256": file_hash(directory / "post-run-seal.json"),
        "bound_artifacts_rehashed": len(config["files"]), "measured_responses": 5832, "checked_warmups": 36,
        "exact_native_uzu_responses": 3888, "exact_native_uzu_warmups": 24,
        "whole_vocabulary_acceptance_preserved": True, "public_scalar_groups_exactly_reproduced": True,
        "maximum_independent_calibration_error": max(errors), "quality": quality,
        "probability_comparison": probability, "per_cycle": cycles, "within_engine_stability": stability,
        "host": host, "limits": "Two mirrored six-block full-population cycles. Process sampling cannot establish continuous GPU or thermal isolation; no steady-state significance claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--acceptance-audit", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit(args)
        destination = args.report.absolute()
        require(destination.is_relative_to(PUBLIC) and ".." not in destination.parts
                and not destination.exists() and not any(p.is_symlink() for p in destination.parents),
                "report_destination_invalid")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(result))
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"stage": "independent_audit_complete", "status": "passed", "measured_responses": 5832}), flush=True)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "sol_bf16_audit_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
