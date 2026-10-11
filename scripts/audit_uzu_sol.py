"""Independent standard-library audit of Sol exact-output native acceptance."""
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
CONDITIONS = ("fresh", "identical_repeat")
IDENTITY = ("input_token_ids", "candidate_token_ids", "logits", "log_probabilities", "probabilities", "decision")
REVISION = "8d01581474760bd1fc7f4a547ff1d02f7dada437"
CONTROL_HASH = "b2204b9a48fa3d78ab9de63cc58f59032c23578781cde0f09bbba960809edf4e"

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

def token_digest(tokens):
    require(type(tokens) is list and 0 < len(tokens) <= 8192
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


def candidate_identity(config):
    receipt = config["candidate_receipt"]
    variants = {"rope": ["rope"], "gdn": ["gdn"], "attention": ["attention"],
        "rope-gdn": ["rope", "gdn"], "rope-attention": ["rope", "attention"],
        "gdn-attention": ["gdn", "attention"], "combined": ["rope", "gdn", "attention"]}
    feature_names = {"rope": "uzu-engine/sol-rope-cache", "gdn": "uzu-engine/compact-delta-conv",
        "attention": "uzu-engine/sol-attention-kv-only"}
    selected = variants.get(receipt.get("variant"))
    require(selected and receipt.get("schema_version") == 3 and receipt.get("upstream_revision") == REVISION
            and receipt.get("cache_policy") == "fresh-state", "candidate_identity_invalid")
    expected_features = ["probability-scoring", "uzu-engine/deferred-state-init",
        "uzu-engine/scoring-last-layer-pruning", "uzu-engine/direct-delta-conv"]
    require(receipt.get("features") == expected_features + [feature_names[n] for n in selected],
            "candidate_features_invalid")
    patch_names = ["uzu-next-token-scoring", "uzu-scoring-topology", "uzu-scoring-state-init",
        "uzu-deep-submission", "uzu-deep-last-layer", "uzu-deep-metal"]
    expected_patches = [f"experiments/uzu-rust/{n}.patch" for n in patch_names]
    expected_patches += [f"experiments/uzu-rust/patches/uzu-sol-{n}.patch" for n in selected]
    chain = receipt["patch_chain"]
    require(type(chain) is list and [r.get("path") for r in chain] == expected_patches,
            "candidate_patch_chain_invalid")
    for record in chain:
        require(set(record) == {"path", "sha256"} and file_hash(ROOT / record["path"]) == record["sha256"]
                == config["files"][str(ROOT / record["path"])], "candidate_patch_hash_invalid")
    for field, expected_sources in (
        ("worker_sources", {"experiments/uzu-rust/src/main.rs", "experiments/uzu-rust/src/scoring.rs"}),
        ("probe_sources", {"experiments/uzu-rust/probes/score_digest.rs"}),
    ):
        require(set(receipt[field]) == expected_sources, "candidate_source_set_invalid")
        for name, digest in receipt[field].items():
            require(file_hash(ROOT / name) == digest == config["files"][str(ROOT / name)], "candidate_source_hash_invalid")
    binary, probe = ordinary(config["candidate_binary"]), ordinary(config["candidate_digest_binary"])
    receipt_path = binary.parent.parent / "BUILD_RECEIPT.json"
    require(probe.parent == binary.parent and exact(read(receipt_path, False), receipt)
            and file_hash(binary) == receipt["binary_sha256"]
            and file_hash(probe) == receipt["digest_binary_sha256"], "candidate_binary_identity_invalid")
    control = ROOT / "runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-worker"
    control_receipt_path = control.parent.parent / "BUILD_RECEIPT.json"
    control_receipt = read(control_receipt_path, False)
    require(file_hash(control) == receipt["control_binary_sha256"] == control_receipt["binary_sha256"] == CONTROL_HASH
            and file_hash(control_receipt_path) == receipt["control_receipt_sha256"], "control_identity_invalid")
    require(receipt["upstream_archive_sha256"] == control_receipt["upstream_archive_sha256"]
            and receipt["upstream_license_sha256"] == control_receipt["upstream_license_sha256"]
            and receipt["scoring_patch_sha256"] == chain[0]["sha256"], "candidate_upstream_identity_invalid")
    expected_recipes = {
        "build_recipe_sha256": ROOT / "experiments/uzu-rust/build_sol_scoring.py",
        "deep_build_recipe_sha256": ROOT / "experiments/uzu-rust/build_deep_scoring.py",
        "base_build_recipe_sha256": ROOT / "experiments/uzu-rust/build_scoring.py",
        "original_scoring_manifest_sha256": ROOT / "experiments/uzu-rust/scoring/Cargo.toml",
        "cargo_manifest_sha256": receipt_path.parent / "project/Cargo.toml",
        "cargo_lock_sha256": receipt_path.parent / "project/Cargo.lock",
    }
    for key, path in expected_recipes.items():
        require(file_hash(path) == receipt[key] == config["files"][str(path)], "candidate_recipe_identity_invalid")
    require(receipt["cargo_lock_sha256"] == file_hash(ROOT / "experiments/uzu-rust/scoring/Cargo.lock"),
            "candidate_lock_invalid")
    return receipt


def validate_response(value, tokens, candidates, vocab_size):
    digest = token_digest(tokens)
    require(all(t < vocab_size for t in tokens) and type(candidates) is list and 2 <= len(candidates) <= 16
            and len(set(candidates)) == len(candidates)
            and all(type(t) is int and 0 <= t < vocab_size for t in candidates), "candidate_tokens_invalid")
    require(value.get("input_tokens") == len(tokens) and value.get("prompt_tokens_sha256") == digest
            and exact(value.get("candidate_token_ids"), candidates) and value.get("temperature") == 1.22
            and value.get("truncated") is False and value.get("cache_hit") is False, "response_identity_invalid")
    probabilities = value["candidate_probabilities"]
    require(type(probabilities) is list and len(probabilities) == len(candidates)
            and all(finite(p) and p <= 1 for p in probabilities)
            and abs(math.fsum(probabilities) - 1) <= 1e-12, "probabilities_invalid")
    errors = []
    for key in ("logits", "log_probabilities"):
        vector = value[key]
        require(type(vector) is list and len(vector) == len(candidates)
                and all(type(v) in (int, float) and math.isfinite(v) for v in vector), "scores_invalid")
        if key == "log_probabilities":
            require(all(v <= 1e-12 for v in vector), "log_probabilities_invalid")
        errors.extend(abs(p - q) for p, q in zip(probabilities, recalibrate(vector), strict=True))
    require(max(errors) <= 1e-12, "calibration_invalid")
    metrics = value["metrics"]
    require(type(metrics) is dict and all(finite(v) for v in metrics.values())
            and {"state_creation_seconds", "prefill_seconds", "readout_seconds", "score_seconds", "total_seconds"}
            <= set(metrics), "native_metrics_invalid")
    return max(errors)


def normalized_row(row):
    response = row["response"]
    return {"input_token_ids": row["input_token_ids"], "candidate_token_ids": response["candidate_token_ids"],
        "logits": response["logits"], "log_probabilities": response["log_probabilities"],
        "probabilities": response["candidate_probabilities"], "decision": row["decision"]}


def validate_worker_row(row, case, expected, vocab_size):
    require(row.get("case_id") == case["id"], "case_identity_invalid")
    tokens, value = row["input_token_ids"], row["response"]
    require(len(value["candidate_token_ids"]) == len(option_keys(case)), "candidate_coverage_invalid")
    error = validate_response(value, tokens, expected["candidate_token_ids"], vocab_size)
    normalized = normalized_row(row)
    require(all(exact(normalized[k], expected[k]) for k in IDENTITY), "candidate_output_bits_invalid")
    require(exact(row["decision"], decision(case, value["candidate_probabilities"], len(tokens))), "decision_invalid")
    return error


def validate_readiness(value, mode, vocab_size):
    require(value.get("ready") is True and value.get("mode") == mode
            and value.get("cache_policy") == "fresh-state" and value.get("upstream_revision") == REVISION
            and value.get("scoring_patch_sha256") == file_hash(ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch")
            and type(value.get("vocab_size")) is int and value["vocab_size"] == vocab_size, "readiness_invalid")


def validate_digest(value, tokens, expected, vocab_size):
    require(value.get("input_tokens") == len(tokens) and value.get("prompt_tokens_sha256") == token_digest(tokens)
            and value.get("cache_hit") is False and value.get("truncated") is False
            and value.get("vocab_size") == vocab_size, "digest_identity_invalid")
    digest = value["vocab_logits_sha256"]
    require(type(digest) is str and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
            "vocabulary_digest_invalid")
    require(all(exact(value[k], expected[k]) for k in
            ("input_tokens", "prompt_tokens_sha256", "vocab_logits_sha256", "vocab_size")), "vocabulary_bits_invalid")


def audit(args):
    directory = private_directory(args.output)
    config, pre, post = (read(directory / name) for name in
        ("configuration.json", "pre-run-seal.json", "post-run-seal.json"))
    require(config.get("schema_version") == 3 and config.get("mode") == "untimed-exact-acceptance"
            and config.get("temperature") == 1.22 and config.get("context_tokens") == 8192
            and config.get("warmups_per_process") == 3 and config.get("case_responses") == 486
            and config.get("boundary_responses") == 48 and config.get("digest_responses") == 534
            and config.get("timing_deferred") is True and config.get("timing_not_reported") is True,
            "configuration_invalid")
    require(str(Path(__file__).resolve()) in config["files"], "audit_source_not_bound")
    for path, expected in config["files"].items():
        require(file_hash(path) == expected, "bound_artifact_changed")
    receipt = candidate_identity(config)
    digest = sha(encoded(config))
    snapshot = directory / "artifact-snapshot.json"
    require(pre.get("configuration_sha256") == post.get("configuration_sha256") == digest
            and pre.get("artifact_snapshot_sha256") == post.get("artifact_snapshot_sha256") == file_hash(snapshot)
            and exact(read(snapshot), {"files": config["files"]}) and post.get("ownership_settled") is True,
            "seals_invalid")
    require(set(post["records"]) == {"worker.json", "digest.json"}, "record_set_invalid")
    records = {}
    for name, expected in post["records"].items():
        require(file_hash(directory / name) == expected, "record_hash_invalid")
        record = read(directory / name)
        require(record.get("complete") is True and record.get("ownership_settled") is True
                and record.get("configuration_sha256") == digest, "record_invalid")
        records[name] = record
    cases, fresh_reference = reference_data(config["reference"])
    reference_dir = private_directory(config["reference"])
    reference_config = read(reference_dir / "configuration.json")
    reference = read(reference_dir / "round-1-baseline.json")
    reference_probe = read(reference_dir / "digest-baseline.json")
    require(config["model"] == reference_config["model"] and config["case_ids"] == [c["id"] for c in cases],
            "reference_population_invalid")
    worker, probe = records["worker.json"], records["digest.json"]
    vocab_size = reference_probe["readiness"]["vocab_size"]
    validate_readiness(worker["readiness"], "exact-token-scoring-v1", vocab_size)
    validate_readiness(probe["readiness"], "exact-token-digest-v1", vocab_size)
    require(len(worker["warmups"]) == len(probe["warmups"]) == 3 and len(worker["rows"]) == 486
            and len(worker["boundaries"]) == 48 and len(probe["outputs"]) == 534, "counts_invalid")
    require([(r["case_id"], r["condition"]) for r in worker["rows"]]
            == [(c["id"], condition) for c in cases for condition in CONDITIONS], "row_order_invalid")
    errors = []
    for row in worker["warmups"]:
        require(row.get("condition") == "warmup", "warmup_invalid")
        errors.append(validate_worker_row(row, cases[0], reference["rows"][0], vocab_size))
    for i, row in enumerate(worker["rows"]):
        errors.append(validate_worker_row(row, cases[i // 2], reference["rows"][i], vocab_size))
    for row, expected in zip(worker["boundaries"], reference["boundaries"], strict=True):
        require(all(exact(row[k], expected[k]) for k in ("length", "kind", "tokens")), "boundary_identity_invalid")
        errors.append(validate_response(row["response"], row["tokens"], expected["response"]["candidate_token_ids"], vocab_size))
        require(all(exact(row["response"][k], expected["response"][k]) for k in
            ("candidate_token_ids", "logits", "log_probabilities", "candidate_probabilities")), "boundary_bits_invalid")
    requests = [r["input_token_ids"] for r in reference["rows"]] + [r["tokens"] for r in reference["boundaries"]]
    for value in probe["warmups"]:
        validate_digest(value, requests[0], reference_probe["outputs"][0], vocab_size)
    for value, tokens, expected in zip(probe["outputs"], requests, reference_probe["outputs"], strict=True):
        validate_digest(value, tokens, expected, vocab_size)
    quality = {}
    for tier in ("public", "synthetic"):
        current, prior = [], []
        for i, case in enumerate(cases):
            if (case["tier"] != "synthetic") == (tier == "public"):
                current.append(quality_row(case, normalized_row(worker["rows"][i * 2])))
                prior.append(quality_row(case, fresh_reference[case["id"]]))
        require(exact(current, prior), "quality_rows_invalid")
        quality[tier] = quality_summary(current)
    report = read(args.source_report, False)
    expected_groups = {"worker_case_responses": 486, "worker_boundary_responses": 48,
        "whole_vocabulary_responses": 534, "worker_warmups": 3, "digest_warmups": 3,
        "maximum_calibration_formula_error": max(errors), "candidate_outputs_exactly_equal": True,
        "whole_vocabulary_logits_exactly_equal": True, "quality_equals_reference": True, "quality": quality}
    require(set(report) == set(expected_groups) | {"schema_version", "status", "configuration",
            "configuration_sha256", "post_run_seal_sha256", "whole_vocabulary_scope", "timing_status"},
            "aggregate_schema_invalid")
    require(all(exact(report[k], v) for k, v in expected_groups.items()), "aggregate_scalars_invalid")
    public_config = report["configuration"]
    require(set(public_config) == {"schema_version", "mode", "artifacts", "candidate_variant",
            "candidate_receipt_sha256", "upstream_revision", "features", "case_count", "ordered_case_ids_sha256",
            "context_tokens", "temperature", "warmups_per_process", "timing_deferred", "timing_not_reported"},
            "aggregate_configuration_schema_invalid")
    require(report["status"] == "complete" and report["schema_version"] == 3
            and report["configuration_sha256"] == digest
            and report["post_run_seal_sha256"] == file_hash(directory / "post-run-seal.json")
            and public_config["candidate_receipt_sha256"] == sha(encoded(receipt))
            and public_config["candidate_variant"] == receipt["variant"]
            and public_config["ordered_case_ids_sha256"] == sha(encoded(config["case_ids"]))
            and public_config["features"] == receipt["features"] and public_config["case_count"] == 243
            and public_config["schema_version"] == 3 and public_config["mode"] == "untimed-exact-acceptance"
            and public_config["upstream_revision"] == REVISION and public_config["warmups_per_process"] == 3
            and public_config["temperature"] == 1.22 and public_config["context_tokens"] == 8192
            and public_config["timing_deferred"] is True and public_config["timing_not_reported"] is True,
            "aggregate_identity_invalid")
    require(report["whole_vocabulary_scope"] == "Candidate probe versus preserved original-core probe on all case and boundary requests; historical scoring-worker equality covers its exposed candidate outputs."
            and report["timing_status"] == "Untimed correctness acceptance; durations do not establish a speed gain.",
            "aggregate_scope_invalid")
    artifacts = []
    for name, expected in config["files"].items():
        path = Path(name)
        item = {"sha256": expected}
        if path.is_absolute() and path.is_relative_to(ROOT):
            item["path"] = str(path.relative_to(ROOT))
        else:
            item["external_artifact"] = True
        artifacts.append(item)
    require(exact(public_config["artifacts"], artifacts), "public_artifacts_invalid")
    for path, expected in config["files"].items():
        require(file_hash(path) == expected, "bound_artifact_changed")
    return {"schema_version": 1, "status": "passed",
        "method": "Independent standard-library calculations; no evaluator or aggregate helpers imported.",
        "audit_source_sha256": file_hash(__file__), "source_report_sha256": file_hash(args.source_report),
        "configuration_sha256": digest, "post_run_seal_sha256": file_hash(directory / "post-run-seal.json"),
        "bound_artifacts_rehashed": len(config["files"]), "candidate_variant": receipt["variant"],
        "exact_case_responses": 486, "exact_boundary_responses": 48, "exact_vocabulary_digests": 534,
        "checked_worker_warmups": 3, "checked_digest_warmups": 3,
        "maximum_independent_calibration_error": max(errors), "quality": quality,
        "public_scalar_groups_exactly_reproduced": True,
        "whole_vocabulary_logits_exactly_equal": True, "candidate_float_bits_equal": True,
        "public_report_matches_independent_recalculation": True,
        "limits": "Untimed exact-output acceptance; no latency or generation acceptance claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
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
        print(json.dumps({"stage": "independent_sol_audit_complete", "status": "passed", "responses": 534}), flush=True)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "sol_native_audit_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
