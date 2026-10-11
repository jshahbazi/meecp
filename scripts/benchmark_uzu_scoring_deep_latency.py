"""Balanced latency comparison for deeper Uzu scoring variants."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import statistics
import tempfile
from types import SimpleNamespace

import benchmark_uzu_scoring_deep as opt
import benchmark_uzu_scoring_optimization as original

base = opt.base
ROOT = opt.ROOT
PROTOCOL = base.EXPERIMENT / "DEEP_LATENCY_PROTOCOL.md"
BUCKETS = (("short", 0, 256), ("medium", 256, 1024), ("long", 1024, 4096))
CONDITIONS = ("fresh", "identical_repeat")
require = base.require


def selected_cases(cases, baseline_rows):
    """Length-only midpoint quartiles; original dataset order breaks ties."""
    rows = {r["case_id"]: r for r in baseline_rows if r["condition"] == "fresh"}
    selected = []
    for bucket, lower, upper in BUCKETS:
        eligible = [c for c in cases if c["tier"] != "synthetic"
                    and lower <= rows[c["id"]]["input_tokens"] < upper]
        eligible.sort(key=lambda c: rows[c["id"]]["input_tokens"])
        require(len(eligible) >= 4, "selection_bucket_too_small")
        indices = [(2 * i + 1) * len(eligible) // 8 for i in range(4)]
        require(len(set(indices)) == 4, "selection_not_unique")
        for index in indices:
            case = eligible[index]
            selected.append({"case_id": case["id"], "bucket": bucket,
                             "input_tokens": rows[case["id"]]["input_tokens"]})
    return selected


def schedule(quartets):
    require(quartets in (2, 4), "quartet_count_invalid")
    blocks = []
    for quartet in range(1, quartets + 1):
        blocks.append({"quartet": quartet, "phase": "control", "engine": "baseline", "position": 0})
        order = ("baseline", "candidate", "candidate", "baseline") if quartet % 2 else (
            "candidate", "baseline", "baseline", "candidate")
        blocks.extend({"quartet": quartet, "phase": "comparison", "engine": engine, "position": position}
                      for position, engine in enumerate(order, 1))
    return [{"block": index, **block} for index, block in enumerate(blocks, 1)]


def reference_data(directory):
    directory = Path(directory).absolute()
    require(directory.is_relative_to(ROOT / "test-artifacts") and not directory.is_symlink()
            and directory.stat().st_mode & 0o077 == 0, "reference_privacy_invalid")
    config = base.read_private(directory / "configuration.json")
    require(config.get("smoke") is False and config.get("rounds") == 2, "full_reference_required")
    original.verify_files(config)
    _, cases = base.load_dataset(directory / "dataset.json")
    blocks = original.load_blocks(directory, config, cases)
    require(original.verify_digests(directory, config, blocks[(1, "baseline")]), "full_digest_reference_required")
    paths = [p for p in directory.iterdir() if p.is_file()]
    return config, cases, blocks[(1, "baseline")]["rows"], paths


def configuration(args):
    reference, cases, baseline_rows, reference_paths = reference_data(args.reference)
    binding_args = SimpleNamespace(**vars(args), smoke=False, baseline_digest_binary=None, candidate_digest_binary=None)
    binding = opt.configuration(binding_args)
    require(binding["model"] == reference["model"],
            "reference_runtime_changed")
    chosen = selected_cases(cases, baseline_rows)
    extra_paths = [Path(__file__), Path(original.__file__), PROTOCOL, *reference_paths]
    binding["files"].update({str(opt.ordinary_file(p)): base.file_hash(p) for p in extra_paths})
    return {"schema_version": 1, "binding": binding, "quartets": args.quartets,
            "selection": chosen, "schedule": schedule(args.quartets), "warmups": 3,
            "reference": str(args.reference)}, cases, baseline_rows


def block_name(item):
    return f"block-{item['block']:02d}.json"


async def run(args):
    config, cases, baseline_rows = configuration(args)
    by_case = {c["id"]: c for c in cases}
    reference = {r["case_id"]: r for r in baseline_rows if r["condition"] == "fresh"}
    selected = [by_case[c["case_id"]] for c in config["selection"]]
    directory = base.secure_directory(args.output)
    require(not list(directory.iterdir()), "run_directory_not_empty")
    base.private_json(directory / "configuration.json", config)
    digest = base.sha(base.encoded(config))
    base.private_json(directory / "pre-run-seal.json", {"configuration_sha256": digest})
    owner = base.ProcessOwner(args.reaper)
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-uzu-latency-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            for item in config["schedule"]:
                launch_args = SimpleNamespace(uzu_binary=getattr(args, item["engine"] + "_binary"), uzu_model=args.model)
                process, client, startup = await base.launch(owner, "uzu", launch_args, scratch)
                try:
                    candidates = []
                    for letter in base.LETTERS:
                        ids, _ = await base.tokenize("uzu", client, letter, False)
                        require(len(ids) == 1, "candidate_not_single_token")
                        candidates.append(ids[0])
                    require(len(set(candidates)) == 16, "candidate_duplicate")
                    for _ in range(config["warmups"]):
                        warmup = await opt.measure(client, selected[0], candidates, "warmup")
                        opt.equal_rows(reference[selected[0]["id"]], warmup)
                    rows = []
                    for case in selected:
                        for condition in CONDITIONS:
                            row = await opt.measure(client, case, candidates, condition)
                            opt.equal_rows(reference[case["id"]], row)
                            rows.append(row)
                finally:
                    await process.stop()
                require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
                base.private_json(directory / block_name(item), {**item, "complete": True,
                    "configuration_sha256": digest, "warmups": config["warmups"], "startup_seconds": startup,
                    "ownership_settled": True, "peak_rss_bytes": process.peak_memory_bytes, "rows": rows})
                print(json.dumps({"stage": "block_complete", "block": item["block"],
                                  "total_blocks": len(config["schedule"]), "measurements": len(rows)}), flush=True)
        opt.verify_files(config["binding"])
        base.private_json(directory / "post-run-seal.json", {"configuration_sha256": digest,
            "ownership_settled": not owner.instances,
            "blocks": {p.name: base.file_hash(p) for p in sorted(directory.glob("block-*.json"))}})
    finally:
        await owner.close()


def load_blocks(directory, config, cases, baseline_rows):
    require(config["schedule"] == schedule(config["quartets"]) and config["warmups"] == 3
            and config["selection"] == selected_cases(cases, baseline_rows), "configuration_invalid")
    digest = base.sha(base.encoded(config))
    pre = base.read_private(directory / "pre-run-seal.json")
    post = base.read_private(directory / "post-run-seal.json")
    expected = {block_name(item) for item in config["schedule"]}
    require(pre.get("configuration_sha256") == post.get("configuration_sha256") == digest
            and post.get("ownership_settled") is True, "seal_mismatch")
    require(set(post.get("blocks", {})) == expected
            and {p.name for p in directory.glob("block-*.json")} == expected, "blocks_incomplete")
    by_case = {c["id"]: c for c in cases}
    reference = {r["case_id"]: r for r in baseline_rows if r["condition"] == "fresh"}
    ordering = [(s["case_id"], condition) for s in config["selection"] for condition in CONDITIONS]
    blocks = []
    for item in config["schedule"]:
        path = directory / block_name(item)
        require(base.file_hash(path) == post["blocks"][path.name], "block_seal_mismatch")
        block = base.read_private(path)
        require(all(block.get(k) == v for k, v in item.items()) and block.get("complete") is True
                and block.get("ownership_settled") is True and block.get("warmups") == 3
                and block.get("configuration_sha256") == digest, "block_invalid")
        require([(r["case_id"], r["condition"]) for r in block["rows"]] == ordering, "rows_incomplete")
        for row in block["rows"]:
            opt.validate_row(row, by_case[row["case_id"]])
            opt.equal_rows(reference[row["case_id"]], row)
        blocks.append(block)
    return blocks


def distribution(values):
    return {"samples": len(values), "p50": base.percentile(values, .5), "p95": base.percentile(values, .95),
            "minimum": min(values) if values else None, "maximum": max(values) if values else None}


def quartet_summary(blocks, condition):
    """The experimental replication unit is a quartet, not its individual requests."""
    require(len(blocks) == 4 and all(b["phase"] == "comparison" for b in blocks), "quartet_incomplete")
    groups = {engine: [b for b in blocks if b["engine"] == engine] for engine in ("baseline", "candidate")}
    require(all(len(v) == 2 for v in groups.values()), "quartet_unbalanced")
    rows = {engine: [[r for r in b["rows"] if r["condition"] == condition] for b in group]
            for engine, group in groups.items()}
    require(all([[r["case_id"] for r in group] for group in groups_rows]
                == [[r["case_id"] for r in rows["baseline"][0]]] * 2 for groups_rows in rows.values()),
            "quartet_pairing_invalid")
    metrics = {}
    for metric in ("wall_seconds", "score_request_seconds", *opt.METRICS):
        def value(row):
            return row[metric] if metric in ("wall_seconds", "score_request_seconds") else row["engine_metrics"][metric]
        averages = {engine: [statistics.mean(value(group[i]) for group in groups_rows)
                    for i in range(len(groups_rows[0]))] for engine, groups_rows in rows.items()}
        left, right = averages["baseline"], averages["candidate"]
        metrics[metric] = {"baseline_case_means": distribution(left), "candidate_case_means": distribution(right),
            "baseline_over_candidate_ratio_of_means": statistics.mean(left) / statistics.mean(right)
                if statistics.mean(right) > 0 else None,
            "paired_case_ratio": distribution([a / b for a, b in zip(left, right, strict=True) if b > 0]),
            "paired_candidate_minus_baseline_seconds": distribution([b - a for a, b in zip(left, right, strict=True)])}
    return {"cases": len(rows["baseline"][0]), "metrics": metrics}


def control_summary(blocks, condition):
    controls = [b for b in blocks if b["phase"] == "control"]
    rows = [[r for r in b["rows"] if r["condition"] == condition] for b in controls]
    result = {}
    for metric in ("wall_seconds", "score_request_seconds"):
        spans, relative_spans = [], []
        for index in range(len(rows[0])):
            values = [group[index][metric] for group in rows]
            spans.append(max(values) - min(values))
            relative_spans.append((max(values) - min(values)) / statistics.mean(values))
        result[metric] = {"between_process_case_range_seconds": distribution(spans),
                          "between_process_case_range_over_mean": distribution(relative_spans)}
    return {"independent_processes": len(controls), "metrics": result,
        "per_block": [{"block": b["block"], "timing": base.timing_summary(group)}
                      for b, group in zip(controls, rows, strict=True)]}


def exported_configuration(config):
    """Only aggregate selection identity and approved hardware fields are public."""
    binding = config["binding"]
    machine = {}
    patterns = {"architecture": r"(?:arm64|aarch64|x86_64)", "os_version": r"[0-9.]+",
                "chip_model": r"Apple [A-Za-z0-9 -]+", "hardware_model": r"[A-Za-z]+[0-9]+,[0-9]+"}
    for key, pattern in patterns.items():
        value = binding.get("machine", {}).get(key)
        if type(value) is str and len(value) <= 160 and re.fullmatch(pattern, value):
            machine[key] = value
    ram = binding.get("machine", {}).get("physical_ram_bytes")
    if type(ram) is int and ram > 0:
        machine["physical_ram_bytes"] = ram
    return {"runtime_binding": opt.exported_configuration(binding), "machine": machine,
            "quartets": config["quartets"], "warmups_per_block": config["warmups"],
            "selected_case_count": len(config["selection"]),
            "ordered_selection_sha256": base.sha(base.encoded(config["selection"])),
            "schedule": config["schedule"]}


def aggregate(args):
    directory = Path(args.output).absolute()
    require(directory.is_relative_to(ROOT / "test-artifacts") and not directory.is_symlink()
            and directory.stat().st_mode & 0o077 == 0, "private_directory_invalid")
    config = base.read_private(directory / "configuration.json")
    opt.verify_files(config["binding"])
    _, cases, reference, _ = reference_data(config["reference"])
    blocks = load_blocks(directory, config, cases, reference)
    summaries = {condition: [quartet_summary([b for b in blocks if b["quartet"] == q
        and b["phase"] == "comparison"], condition) for q in range(1, config["quartets"] + 1)]
        for condition in CONDITIONS}
    report = {"schema_version": 1, "status": "complete", "mode": "full" if config["quartets"] == 4 else "smoke",
        "configuration_sha256": base.sha(base.encoded(config)),
        "post_run_seal_sha256": base.file_hash(directory / "post-run-seal.json"),
        "configuration": exported_configuration(config),
        "selection": {"cases": len(config["selection"]), "selection_sha256": base.sha(base.encoded(config["selection"])),
            "rule": "Midpoint length quartiles within each public token bucket; ties use original dataset order.",
            "bucket_token_counts": {name: [s["input_tokens"] for s in config["selection"] if s["bucket"] == name]
                                    for name, _, _ in BUCKETS}},
        "quartets": config["quartets"], "blocks": len(blocks), "measurements": sum(len(b["rows"]) for b in blocks),
        "comparison_measurements": sum(len(b["rows"]) for b in blocks if b["phase"] == "comparison"),
        "control_measurements": sum(len(b["rows"]) for b in blocks if b["phase"] == "control"),
        "candidate_outputs_exactly_equal_to_full_baseline": True,
        "whole_vocabulary_validation": "No new whole-vocabulary inference. Prior sealed full-run probes validate their bound binaries; alternate candidate builds require their own validation.",
        "per_quartet": summaries,
        "quartet_level_wall_ratio_distribution": {condition: distribution([s["metrics"]["wall_seconds"]
            ["baseline_over_candidate_ratio_of_means"] for s in summaries[condition]]) for condition in CONDITIONS},
        "baseline_only_process_noise": {condition: control_summary(blocks, condition) for condition in CONDITIONS},
        "timing_limits": "Three warmups and process startup excluded. Quartets are the replication units. Four baseline-only processes describe local process/time variation. No request-level bootstrap confidence interval or precise steady-state speed claim."}
    opt.verify_files(config["binding"])
    destination = Path(args.report).absolute()
    require(not destination.exists() and not destination.is_symlink(), "report_exists")
    base.private_json(destination, report)
    print(json.dumps({"stage": "aggregate_complete", "measurements": report["measurements"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--candidate-binary", type=Path, required=True)
    runner.add_argument("--candidate-receipt", type=Path, required=True)
    runner.add_argument("--baseline-receipt", type=Path)
    runner.add_argument("--baseline-binary", type=Path, default=ROOT / "runtime/uzu-rust/scoring-target/release/meecp-uzu-worker")
    runner.add_argument("--model", type=Path, default=ROOT / "models/uzu-jevk5/bf16")
    runner.add_argument("--dataset", type=Path, default=ROOT / "test-artifacts/jev-uzu-screen-bf16-resumed/dataset.json")
    runner.add_argument("--reference", type=Path, default=ROOT / "test-artifacts/uzu-optimization-combined")
    runner.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    runner.add_argument("--quartets", type=int, choices=(2, 4), default=4)
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
        print(json.dumps({"stage": "failed", "error": "latency_evaluation_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
