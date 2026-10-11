"""Sealed full-population optimized Uzu versus llama.cpp BF16 experiment."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics
import struct
import tempfile
from types import SimpleNamespace

import benchmark_jev_uzu as base
import benchmark_uzu_scoring_deep as deep
import benchmark_uzu_scoring_latency as reference_helper

ROOT = base.ROOT
PROTOCOL = base.EXPERIMENT / "OPTIMIZED_BF16_PROTOCOL.md"
MONITOR = ROOT / "scripts/monitor_jev_optimized.py"
CONDITIONS = ("fresh", "identical_repeat")
require = base.require


def exact(a, b):
    if type(a) is not type(b):
        return False
    if type(a) is float:
        return struct.pack(">d", a) == struct.pack(">d", b)
    if type(a) is dict:
        return a.keys() == b.keys() and all(exact(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(exact(x, y) for x, y in zip(a, b))
    return a == b


def schedule():
    blocks = []
    for quartet, order in enumerate((("llama.cpp", "uzu", "uzu", "llama.cpp"),
                                      ("uzu", "llama.cpp", "llama.cpp", "uzu")), 1):
        blocks.extend({"quartet": quartet, "position": i, "engine": engine}
                      for i, engine in enumerate(order, 1))
    return [{"block": i, **item} for i, item in enumerate(blocks, 1)]


def reference_data(path):
    config, cases, rows, paths = reference_helper.reference_data(path)
    require(len(cases) == 243 and len(rows) == 486, "full_reference_required")
    return config, cases, {r["case_id"]: r for r in rows if r["condition"] == "fresh"}, paths


def configuration(args):
    prior, cases, reference, paths = reference_data(args.reference)
    historical = SimpleNamespace(**vars(args))
    historical.uzu_binary = ROOT / "runtime/uzu-rust/scoring-target/release/meecp-uzu-worker"
    historical.dataset = args.reference / "dataset.json"
    historical.rounds, historical.llama_reference = 2, "bf16"
    runtime = base.configuration(historical)
    receipt, candidate_paths = deep.candidate_identity(args.uzu_binary, args.candidate_receipt)
    require(receipt["variant"] == "all" and str(args.uzu_model) == prior["model"], "candidate_scope_invalid")
    acceptance_path = base.EXPERIMENT / "DEEP_VALIDATION_RESULTS.json"
    acceptance = json.loads(acceptance_path.read_bytes())
    require(acceptance["status"] == "complete" and acceptance["candidate_outputs_exactly_equal"] is True
            and acceptance["whole_vocabulary_logits_exactly_equal"] is True
            and acceptance["configuration"]["candidate_receipt_sha256"] == base.sha(base.encoded(receipt)),
            "candidate_acceptance_mismatch")
    paths += [*candidate_paths, args.llama_binary, args.gguf, args.reaper, acceptance_path,
        base.EXPERIMENT / "DEEP_VALIDATION_AUDIT.json", PROTOCOL, MONITOR, Path(__file__),
        Path(base.__file__), Path(deep.__file__), Path(reference_helper.__file__),
        base.EXPERIMENT / "BF16_CONTROL_PROVENANCE.json", base.EXPERIMENT / "RESULTS_BF16.json",
        base.EXPERIMENT / "PROTOCOL_BF16.md", base.EXPERIMENT / "DATASET_PROVENANCE.json",
        ROOT / "scripts/benchmark_uzu.py", ROOT / "scripts/acquire_jev_uzu_models.py",
        ROOT / "scripts/convert_jev_uzu_model.py", ROOT / "scripts/convert_jev_llama_bf16.py",
        ROOT / "backend/meecp/processes.py", ROOT / "vendor/jevk5/prompt.py",
        ROOT / "scripts/audit_jev_optimized.py"]
    paths += [args.llama_binary.parent / name for name in runtime["artifacts"]["llama_runtime_closure"]]
    paths += list(deep.model_identity(args.uzu_model))
    files = dict(prior["files"])
    files.update({str(deep.ordinary_file(p)): base.file_hash(p) for p in paths})
    return {"schema_version": 1, "mode": "optimized-uzu-llama-bf16-full", "files": files,
        "reference": str(args.reference), "historical_runtime_binding": runtime,
        "candidate_receipt": receipt, "schedule": schedule(), "warmups_per_block": 3,
        "case_ids": [c["id"] for c in cases], "case_count": 243, "measured_responses": 3888,
        "temperature": base.TEMPERATURE, "context_tokens": base.CONTEXT,
        "llama_n_probs": args.n_probs, "uzu_probabilities": "native-reported",
        "fresh_then_identical_repeat": True}, cases, reference


def validate_row(row, case, reference):
    require(row["case_id"] == case["id"] and row["engine"] in base.ENGINES
            and row["condition"] in (*CONDITIONS, "warmup") and row["truncated"] is False,
            "row_identity_invalid")
    require(row["prompt_sha256"] == base.sha(base.rendered(case).encode())
            and row["prompt_tokens_sha256"] == base.token_digest(row["input_token_ids"])
            and row["input_tokens"] == len(row["input_token_ids"]) < base.CONTEXT
            and row["input_token_ids"] == reference["input_token_ids"]
            and row["candidate_token_ids"] == reference["candidate_token_ids"], "token_parity_invalid")
    require(exact(row["decision"], base.typed_decision(case["question"], row["probabilities"], row["input_tokens"])),
            "typed_decision_invalid")
    expected_cache = row["engine"] == "llama.cpp" and row["condition"] == "identical_repeat"
    require(row["cache_hit"] is expected_cache, "cache_policy_invalid")
    errors = [abs(a - b) for a, b in zip(base.calibrated(row["log_probabilities"]), row["probabilities"], strict=True)]
    if row["engine"] == "uzu":
        require(all(exact(row[k], reference[k]) for k in deep.IDENTITY), "uzu_preservation_failed")
        errors += [abs(a - b) for a, b in zip(base.calibrated(row["logits"]), row["probabilities"], strict=True)]
        deep.validate_response({"input_tokens": row["input_tokens"], "prompt_tokens_sha256": row["prompt_tokens_sha256"],
            "candidate_token_ids": row["candidate_token_ids"], "temperature": base.TEMPERATURE,
            "cache_hit": row["cache_hit"], "truncated": row["truncated"], "logits": row["logits"],
            "log_probabilities": row["log_probabilities"], "candidate_probabilities": row["probabilities"],
            "metrics": row["engine_metrics"]}, row["input_token_ids"], row["candidate_token_ids"])
    else:
        require(row["logits"] is None, "llama_logits_invalid")
        evaluated = row["engine_metrics"]["prompt_tokens_evaluated"]
        require(type(evaluated) is int and (0 < evaluated < row["input_tokens"] if expected_cache
                                          else evaluated == row["input_tokens"]), "llama_evaluation_count_invalid")
    require(max(errors) <= base.FORMULA_TOLERANCE, "calibration_formula_invalid")
    base.timing_summary([row])
    require(row["wall_seconds"] > 0 and row["score_request_seconds"] > 0, "timing_invalid")
    return max(errors)


async def measure(engine, client, case, previous, candidates, args, condition):
    if engine == "uzu":
        row = await deep.measure(client, case, candidates, condition)
        row.update(engine=engine, prompt_sha256=base.sha(base.rendered(case).encode()),
                   prompt_tokens_sha256=base.token_digest(row["input_token_ids"]))
        return row, row["input_token_ids"]
    row, tokens = await base.measure(engine, client, case, previous, candidates, args,
                                     condition == "identical_repeat")
    row["condition"] = condition
    return row, tokens


async def run(args):
    config, cases, reference = configuration(args)
    directory = base.secure_directory(args.output)
    require(not list(directory.iterdir()), "run_directory_not_empty")
    digest = base.sha(base.encoded(config))
    base.private_json(directory / "configuration.json", config)
    base.private_json(directory / "dataset.json", base.read_private(args.reference / "dataset.json"))
    base.private_json(directory / "pre-run-seal.json", {"configuration_sha256": digest,
        "dataset_sha256": base.file_hash(directory / "dataset.json")})
    owner = base.ProcessOwner(args.reaper)
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-optimized-bf16-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            for item in config["schedule"]:
                engine = item["engine"]
                process, client, startup = await base.launch(owner, engine, args, scratch)
                try:
                    candidates = []
                    for letter in base.LETTERS:
                        ids, _ = await base.tokenize(engine, client, letter, False)
                        require(len(ids) == 1, "candidate_not_single_token")
                        candidates.append(ids[0])
                    require(len(set(candidates)) == 16, "candidate_token_duplicate")
                    warmups = []
                    for _ in range(3):
                        row, _ = await measure(engine, client, cases[0], None, candidates, args, "warmup")
                        validate_row(row, cases[0], reference[cases[0]["id"]])
                        warmups.append(row)
                    previous, rows = None, []
                    for index, case in enumerate(cases):
                        for condition in CONDITIONS:
                            row, previous = await measure(engine, client, case, previous, candidates, args, condition)
                            validate_row(row, case, reference[case["id"]])
                            rows.append(row)
                        if (index + 1) % 40 == 0:
                            print(json.dumps({"stage": "full_scoring", "block": item["block"],
                                "engine": engine, "cases": index + 1}), flush=True)
                finally:
                    if engine == "llama.cpp":
                        await client.aclose()
                    await process.stop()
                require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
                base.private_json(directory / f"block-{item['block']:02d}.json", {**item,
                    "configuration_sha256": digest, "complete": True, "ownership_settled": True,
                    "startup_seconds": startup, "peak_rss_bytes": process.peak_memory_bytes,
                    "warmups": warmups, "rows": rows})
                print(json.dumps({"stage": "block_complete", "block": item["block"], "total_blocks": 8}), flush=True)
        deep.verify_files(config)
        base.private_json(directory / "post-run-seal.json", {"configuration_sha256": digest,
            "dataset_sha256": base.file_hash(directory / "dataset.json"), "ownership_settled": not owner.instances,
            "blocks": {p.name: base.file_hash(p) for p in sorted(directory.glob("block-*.json"))}})
    finally:
        await owner.close()


def load_blocks(directory, config, cases, reference):
    require(config["schedule"] == schedule() and config["case_ids"] == [c["id"] for c in cases]
            and config["case_count"] == len(cases) == 243 and config["warmups_per_block"] == 3
            and config["measured_responses"] == 3888 and config["uzu_probabilities"] == "native-reported"
            and config["temperature"] == base.TEMPERATURE and config["context_tokens"] == base.CONTEXT,
            "configuration_invalid")
    pre, post = base.read_private(directory / "pre-run-seal.json"), base.read_private(directory / "post-run-seal.json")
    digest = base.sha(base.encoded(config))
    require(pre["configuration_sha256"] == post["configuration_sha256"] == digest
            and pre["dataset_sha256"] == post["dataset_sha256"] == base.file_hash(directory / "dataset.json")
            and post["ownership_settled"] is True, "seal_invalid")
    expected = {f"block-{i:02d}.json" for i in range(1, 9)}
    require(set(post["blocks"]) == expected and {p.name for p in directory.glob("block-*.json")} == expected,
            "blocks_incomplete")
    ordering = [(c["id"], condition) for c in cases for condition in CONDITIONS]
    blocks, errors = [], []
    by_case = {c["id"]: c for c in cases}
    for item in schedule():
        path = directory / f"block-{item['block']:02d}.json"
        require(base.file_hash(path) == post["blocks"][path.name], "block_hash_mismatch")
        block = base.read_private(path)
        require(all(block[k] == v for k, v in item.items()) and block["configuration_sha256"] == digest
                and block["complete"] is True and block["ownership_settled"] is True
                and len(block["warmups"]) == 3, "block_invalid")
        require([(r["case_id"], r["condition"]) for r in block["rows"]] == ordering, "rows_incomplete")
        for row in block["warmups"]:
            require(row["engine"] == item["engine"] and row["condition"] == "warmup", "warmup_invalid")
            errors.append(validate_row(row, cases[0], reference[cases[0]["id"]]))
        for row in block["rows"]:
            require(row["engine"] == item["engine"], "row_engine_invalid")
            errors.append(validate_row(row, by_case[row["case_id"]], reference[row["case_id"]]))
        blocks.append(block)
    return blocks, max(errors)


def quartet_metrics(blocks, condition, case_ids):
    groups = {engine: [[r for r in b["rows"] if r["condition"] == condition and r["case_id"] in case_ids]
                      for b in blocks if b["engine"] == engine] for engine in base.ENGINES}
    require(all(len(g) == 2 for g in groups.values()), "quartet_unbalanced")
    left, right = groups["llama.cpp"], groups["uzu"]
    require(all([r["case_id"] for r in g] == [r["case_id"] for r in left[0]] for g in (*left, *right)),
            "quartet_pairing_invalid")
    if not left[0]:
        return {"cases": 0, "wall_ratio_of_means": None, "score_ratio_of_means": None, "median_paired_wall_ratio": None}
    means = {engine: [{k: statistics.mean(g[i][k] for g in group) for k in ("wall_seconds", "score_request_seconds")}
                      for i in range(len(group[0]))] for engine, group in groups.items()}
    a, b = means["llama.cpp"], means["uzu"]
    return {"cases": len(a), "wall_ratio_of_means": statistics.mean(r["wall_seconds"] for r in a)
            / statistics.mean(r["wall_seconds"] for r in b),
        "score_ratio_of_means": statistics.mean(r["score_request_seconds"] for r in a)
            / statistics.mean(r["score_request_seconds"] for r in b),
        "median_paired_wall_ratio": base.percentile([x["wall_seconds"] / y["wall_seconds"] for x, y in zip(a, b)], .5)}


def comparison_summary(pairs):
    if not pairs:
        return {"cases": 0}
    diffs = [base.probability_difference(a["probabilities"], b["probabilities"]) for a, b in pairs]
    return {"cases": len(pairs), "argmax_agreement_count": sum(d["argmax_agrees"] for d in diffs),
        "argmax_agreement": statistics.mean(d["argmax_agrees"] for d in diffs),
        "strict_probability_match_count": sum(d["max_absolute"] <= 1e-3 for d in diffs),
        "close_probability_match_count": sum(d["max_absolute"] <= 1e-2 for d in diffs),
        "maximum_option_absolute_difference": max(d["max_absolute"] for d in diffs),
        "mean_option_absolute_difference": sum(d["mean_absolute"] * len(a["probabilities"]) for d, (a, _) in zip(diffs, pairs))
            / sum(len(a["probabilities"]) for a, _ in pairs),
        "mean_l1": statistics.mean(d["l1"] for d in diffs),
        "mean_total_variation": statistics.mean(d["total_variation"] for d in diffs),
        "maximum_confidence_difference": max(abs(a["decision"]["confidence"] - b["decision"]["confidence"]) for a, b in pairs),
        "maximum_noul_difference": max((abs(a["decision"]["noul"] - b["decision"]["noul"]) for a, b in pairs
                                        if a["decision"]["type"] == "noul"), default=None),
        "maximum_expected_score_difference": max((abs(a["decision"]["score"] - b["decision"]["score"]) for a, b in pairs
                                                  if a["decision"]["type"] == "score"), default=None)}


def aggregate(args):
    directory = base.secure_directory(args.output)
    config = base.read_private(directory / "configuration.json")
    deep.verify_files(config)
    _, cases, reference, _ = reference_data(config["reference"])
    require(base.read_private(directory / "dataset.json") == base.read_private(Path(config["reference"]) / "dataset.json"),
            "dataset_changed")
    blocks, error = load_blocks(directory, config, cases, reference)
    first = {engine: {r["case_id"]: r for r in next(b for b in blocks if b["engine"] == engine)["rows"]
                      if r["condition"] == "fresh"} for engine in base.ENGINES}
    public = [c for c in cases if c["tier"] != "synthetic"]
    quality = {engine: {tier: base.quality_summary([base.quality_row(c, first[engine][c["id"]]) for c in cases
                            if (c["tier"] != "synthetic") == (tier == "public")]) for tier in ("public", "synthetic")}
               for engine in base.ENGINES}
    reference_quality = base.quality_summary([base.quality_row(c, reference[c["id"]]) for c in public])
    require(exact(quality["uzu"]["public"], reference_quality), "uzu_quality_changed")
    timing, quartets = {}, {}
    for condition in CONDITIONS:
        timing[condition] = {engine: base.timing_summary([r for b in blocks if b["engine"] == engine
            for r in b["rows"] if r["condition"] == condition]) for engine in base.ENGINES}
        quartets[condition] = []
        for q in (1, 2):
            group = [b for b in blocks if b["quartet"] == q]
            quartets[condition].append({"quartet": q, "all": quartet_metrics(group, condition, {c["id"] for c in cases}),
                "public": quartet_metrics(group, condition, {c["id"] for c in public}),
                "token_buckets": {name: quartet_metrics(group, condition, {c["id"] for c in cases
                    if low <= len(reference[c["id"]]["input_token_ids"]) < high}) for name, low, high in base.TOKEN_BUCKETS}})
    stability = {engine: {"comparisons": 1944, "maximum_option_difference": max(
        base.probability_difference(first[engine][r["case_id"]]["probabilities"], r["probabilities"])["max_absolute"]
        for b in blocks if b["engine"] == engine for r in b["rows"])} for engine in base.ENGINES}
    report = {"schema_version": 1, "status": "complete", "mode": config["mode"],
        "configuration_sha256": base.sha(base.encoded(config)), "post_run_seal_sha256": base.file_hash(directory / "post-run-seal.json"),
        "artifacts": [{"path": str(Path(p).relative_to(ROOT)), "sha256": h} for p, h in config["files"].items()],
        "candidate_variant": "all", "candidate_receipt_sha256": base.sha(base.encoded(config["candidate_receipt"])),
        "machine": config["historical_runtime_binding"]["machine"], "cases": 243, "public_cases": 231,
        "synthetic_cases": 12, "blocks": 8, "quartets": 2, "measured_responses": 3888, "checked_warmups": 24,
        "temperature": base.TEMPERATURE, "context_tokens": base.CONTEXT,
        "uzu_outputs_exactly_equal_to_reference": True, "uzu_quality_exactly_equal_to_reference": True,
        "maximum_calibration_formula_error": error, "quality": quality,
        "uzu_minus_llama": {k: quality["uzu"]["public"][k] - quality["llama.cpp"]["public"][k]
                            for k in ("argmax_accuracy", "brier", "ece_10_bins")},
        "probability_comparison": {tier: comparison_summary([(first["llama.cpp"][c["id"]], first["uzu"][c["id"]])
            for c in cases if (c["tier"] != "synthetic") == (tier == "public")]) for tier in ("public", "synthetic")},
        "timing": timing, "per_quartet": quartets, "within_engine_stability": stability,
        "per_block_timing": [{"block": b["block"], "engine": b["engine"],
            "conditions": {c: base.timing_summary([r for r in b["rows"] if r["condition"] == c]) for c in CONDITIONS}}
                             for b in blocks],
        "loads": [{k: b[k] for k in ("block", "quartet", "engine", "startup_seconds", "peak_rss_bytes", "ownership_settled")} for b in blocks],
        "limits": ["Two full-population quartets; describe local timing and block drift, not precise steady-state significance.",
            "Same source checkpoint and BF16 matrix weights; export layouts, retained F32 parameters and arithmetic differ.",
            "llama.cpp generates one token for log probabilities and reuses consecutive identical inputs; Uzu uses fresh-state readout.",
            "Full-vocabulary acceptance is preserved for the unchanged Uzu binary; no new probe inference in this timing run.",
            "Public development cases do not establish production calibration or universal quality."]}
    deep.verify_files(config)
    destination = Path(args.report)
    require(destination.is_relative_to(base.EXPERIMENT) and not destination.exists(), "report_destination_invalid")
    base.private_json(destination, report)
    print(json.dumps({"stage": "aggregate_complete", "measured_responses": 3888}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--uzu-binary", type=Path, default=ROOT / "runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-worker")
    runner.add_argument("--candidate-receipt", type=Path, default=ROOT / "runtime/uzu-rust/deep-optimization/all/BUILD_RECEIPT.json")
    runner.add_argument("--uzu-model", type=Path, default=ROOT / "models/uzu-jevk5/bf16")
    runner.add_argument("--gguf", type=Path, default=ROOT / "models/uzu-jevk5/JevK5-4B-v0.3-BF16.gguf")
    runner.add_argument("--llama-binary", type=Path, default=ROOT / "runtime/llama-relocated/llama/llama-server")
    runner.add_argument("--reference", type=Path, default=ROOT / "test-artifacts/uzu-optimization-combined")
    runner.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    runner.add_argument("--n-probs", type=int, choices=(40, 256, 2048), default=40)
    aggregation = commands.add_parser("aggregate")
    aggregation.add_argument("--output", type=Path, required=True)
    aggregation.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        for key, value in vars(args).items():
            if isinstance(value, Path):
                setattr(args, key, value.absolute())
        asyncio.run(run(args)) if args.command == "run" else aggregate(args)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "optimized_bf16_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
