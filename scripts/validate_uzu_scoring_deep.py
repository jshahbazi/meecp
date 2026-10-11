"""Untimed exact-output acceptance for deeper Uzu scoring; private records only."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import tempfile

import benchmark_uzu_scoring_deep as deep
import benchmark_uzu_scoring_latency as latency
import benchmark_uzu_scoring_optimization as original

base = original.base
ROOT = base.ROOT
PROTOCOL = base.EXPERIMENT / "DEEP_VALIDATION_PROTOCOL.md"
require = base.require
RECORD_FILES = ("worker.json", "digest.json")


def reference_data(path):
    config, cases, rows, paths = latency.reference_data(path)
    directory = Path(path)
    worker = base.read_private(directory / "round-1-baseline.json")
    digest = base.read_private(directory / "digest-baseline.json")
    require(len(cases) == 243 and len(rows) == 486 and len(worker["boundaries"]) == 48
            and len(digest["outputs"]) == 534, "reference_counts_invalid")
    return config, cases, worker, digest, paths


def configuration(args):
    prior, cases, worker, digest, reference_paths = reference_data(args.reference)
    receipt, candidate_paths = deep.candidate_identity(args.candidate_binary, args.candidate_receipt)
    probe = deep.ordinary_file(args.candidate_digest_binary)
    require(probe.parent.parent / "BUILD_RECEIPT.json" == args.candidate_receipt
            and receipt.get("digest_binary_sha256") == base.file_hash(probe), "digest_receipt_invalid")
    require(str(args.model) == prior["model"], "model_reference_mismatch")
    paths = [*candidate_paths, probe, *original.model_identity(args.model), *reference_paths,
        Path(__file__), PROTOCOL, Path(deep.__file__), Path(original.__file__), Path(latency.__file__),
        Path(base.__file__), ROOT / "scripts/benchmark_uzu.py", ROOT / "scripts/acquire_jev_uzu_models.py",
        ROOT / "backend/meecp/processes.py", ROOT / "vendor/jevk5/prompt.py", args.reaper,
        base.EXPERIMENT / "DATASET_PROVENANCE.json"]
    files = dict(prior["files"])
    files.update({str(deep.ordinary_file(p)): base.file_hash(p) for p in paths})
    return {"schema_version": 3, "mode": "untimed-exact-acceptance", "files": files,
        "reference": str(args.reference), "model": str(args.model), "candidate_binary": str(args.candidate_binary),
        "candidate_digest_binary": str(probe), "candidate_receipt": receipt,
        "case_ids": [c["id"] for c in cases], "temperature": base.TEMPERATURE, "context_tokens": base.CONTEXT,
        "warmups_per_process": 3, "case_responses": 486, "boundary_responses": 48, "digest_responses": 534,
        "timing_deferred": True, "background_brekko_left_running": True}, cases, worker, digest


async def launch(owner, binary, model, scratch, mode):
    env = {"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin", "LANG": "C", "RUST_LOG": "off"}
    process = await owner.launch(Path(binary), ["--score-model-path", str(model)], ROOT, env,
                                io_mode="streams", owned_group=True, stop_grace=.5)
    try:
        require(process.stdout is not None, "native_io_unavailable")
        async with asyncio.timeout(240):
            raw = await process.stdout.readline()
        require(raw and len(raw) < 1024 * 1024, "native_readiness_invalid")
        ready = json.loads(raw)
        require(ready.get("ready") is True and ready.get("mode") == mode
                and ready.get("cache_policy") == "fresh-state"
                and ready.get("upstream_revision") == original.REVISION
                and ready.get("scoring_patch_sha256") == base.file_hash(ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch")
                and type(ready.get("vocab_size")) is int and ready["vocab_size"] > 0, "native_readiness_invalid")
        return process, base.RustClient(process, ready)
    except BaseException:
        await process.stop()
        raise


async def tokenize(client, text):
    value = await client.post({"operation": "tokenize", "text": text, "add_special_tokens": False})
    ids = value["token_ids"]
    require(value.get("token_count") == len(ids) and value.get("token_ids_sha256") == base.token_digest(ids),
            "tokenization_invalid")
    return ids


async def score(client, tokens, candidates):
    value = await client.post({"operation": "score", "input_token_ids": tokens,
        "candidate_token_ids": candidates, "context_tokens": base.CONTEXT, "temperature": base.TEMPERATURE})
    original.validate_response(value, tokens, candidates)
    return value


def normalized_row(row):
    response = row["response"]
    return {"input_token_ids": row["input_token_ids"], "candidate_token_ids": response["candidate_token_ids"],
        "logits": response["logits"], "log_probabilities": response["log_probabilities"],
        "probabilities": response["candidate_probabilities"], "decision": row["decision"]}


def calibration_error(response):
    probabilities = response["candidate_probabilities"]
    return max(abs(a - b) for calculated in (base.calibrated(response["logits"]),
        base.calibrated(response["log_probabilities"])) for a, b in zip(calculated, probabilities, strict=True))


def validate_worker_row(row, case, expected):
    require(row.get("case_id") == case["id"], "case_identity_invalid")
    tokens = row["input_token_ids"]
    require(0 < len(tokens) <= base.CONTEXT, "context_limit")
    original.validate_response(row["response"], tokens, expected["candidate_token_ids"])
    require(row["decision"] == base.typed_decision(case["question"], row["response"]["candidate_probabilities"], len(tokens)),
            "typed_decision_invalid")
    original.equal_rows(expected, normalized_row(row))
    return calibration_error(row["response"])


def validate_boundary(row, expected):
    require(all(row.get(k) == expected[k] for k in ("length", "kind", "tokens")), "boundary_identity_invalid")
    original.validate_response(row["response"], row["tokens"], expected["response"]["candidate_token_ids"])
    require(all(row["response"][k] == expected["response"][k] for k in (
        "candidate_token_ids", "logits", "log_probabilities", "candidate_probabilities")), "boundary_equality_failed")
    return calibration_error(row["response"])


async def case_row(client, case, candidates, condition, expected):
    tokens = await tokenize(client, base.rendered(case))
    selected = candidates[:len(base.canonical_option_keys(case["question"]))]
    response = await score(client, tokens, selected)
    row = {"case_id": case["id"], "condition": condition, "input_token_ids": tokens, "response": response,
        "decision": base.typed_decision(case["question"], response["candidate_probabilities"], len(tokens))}
    validate_worker_row(row, case, expected)
    return row


def requests(reference):
    return [(r["input_token_ids"], r["candidate_token_ids"]) for r in reference["rows"]] + [
        (r["tokens"], r["response"]["candidate_token_ids"]) for r in reference["boundaries"]]


def validate_digest(value, tokens, expected, vocab_size):
    original.validate_digest(value, tokens, vocab_size)
    require(all(value[k] == expected[k] for k in (
        "vocab_logits_sha256", "vocab_size", "prompt_tokens_sha256", "input_tokens")), "vocabulary_equality_failed")


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
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-uzu-validation-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            process, client = await launch(owner, args.candidate_binary, args.model, scratch, "exact-token-scoring-v1")
            try:
                require(client.readiness["vocab_size"] == reference["readiness" if "readiness" in reference else "native_readiness"]["vocab_size"],
                        "vocab_size_mismatch")
                candidates = []
                for letter in base.LETTERS:
                    ids = await tokenize(client, letter)
                    require(len(ids) == 1, "candidate_not_single_token")
                    candidates.append(ids[0])
                require(len(set(candidates)) == 16, "candidate_duplicate")
                warmups = [await case_row(client, cases[0], candidates, "warmup", reference["rows"][0]) for _ in range(3)]
                rows = []
                for index, case in enumerate(cases):
                    for offset, condition in enumerate(("fresh", "identical_repeat")):
                        rows.append(await case_row(client, case, candidates, condition, reference["rows"][index * 2 + offset]))
                    if (index + 1) % 20 == 0:
                        print(json.dumps({"stage": "worker_validation", "cases": index + 1}), flush=True)
                boundary_rows = []
                for expected in reference["boundaries"]:
                    row = {k: expected[k] for k in ("length", "kind", "tokens")}
                    row["response"] = await score(client, row["tokens"], expected["response"]["candidate_token_ids"])
                    validate_boundary(row, expected)
                    boundary_rows.append(row)
            finally:
                await process.stop()
            require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
            base.private_json(directory / "worker.json", {"complete": True, "configuration_sha256": digest,
                "ownership_settled": True, "readiness": client.readiness, "warmups": warmups,
                "rows": rows, "boundaries": boundary_rows})
            process, client = await launch(owner, args.candidate_digest_binary, args.model, scratch, "exact-token-digest-v1")
            try:
                all_requests = requests(reference)
                async def checked(request, expected):
                    tokens, candidates = request
                    value = await client.post({"operation": "score", "input_token_ids": tokens,
                        "candidate_token_ids": candidates, "context_tokens": base.CONTEXT, "temperature": base.TEMPERATURE})
                    validate_digest(value, tokens, expected, client.readiness["vocab_size"])
                    return value
                probe_warmups = [await checked(all_requests[0], reference_digest["outputs"][0]) for _ in range(3)]
                outputs = []
                for index, (request, expected) in enumerate(zip(all_requests, reference_digest["outputs"], strict=True)):
                    outputs.append(await checked(request, expected))
                    if (index + 1) % 40 == 0:
                        print(json.dumps({"stage": "digest_validation", "responses": index + 1}), flush=True)
            finally:
                await process.stop()
            require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
            base.private_json(directory / "digest.json", {"complete": True, "configuration_sha256": digest,
                "ownership_settled": True, "readiness": client.readiness, "warmups": probe_warmups, "outputs": outputs})
        original.verify_files(config)
        base.private_json(directory / "post-run-seal.json", {"configuration_sha256": digest,
            "artifact_snapshot_sha256": base.file_hash(directory / "artifact-snapshot.json"), "ownership_settled": not owner.instances,
            "records": {name: base.file_hash(directory / name) for name in RECORD_FILES}})
        print(json.dumps({"stage": "validation_complete", "worker_responses": 534, "digest_responses": 534}), flush=True)
    finally:
        await owner.close()


def validate_records(directory, config, cases, reference, reference_digest):
    require(config.get("schema_version") == 3 and config.get("mode") == "untimed-exact-acceptance"
            and config.get("case_ids") == [c["id"] for c in cases] and len(cases) == 243
            and config.get("warmups_per_process") == 3 and config.get("temperature") == base.TEMPERATURE
            and config.get("context_tokens") == base.CONTEXT and config.get("case_responses") == 486
            and config.get("boundary_responses") == 48 and config.get("digest_responses") == 534
            and config.get("timing_deferred") is True and config.get("background_brekko_left_running") is True,
            "configuration_invalid")
    pre = base.read_private(directory / "pre-run-seal.json")
    post = base.read_private(directory / "post-run-seal.json")
    digest = base.sha(base.encoded(config))
    require(pre.get("configuration_sha256") == post.get("configuration_sha256") == digest
            and pre.get("artifact_snapshot_sha256") == post.get("artifact_snapshot_sha256")
            == base.file_hash(directory / "artifact-snapshot.json")
            and base.read_private(directory / "artifact-snapshot.json") == {"files": config["files"]}
            and post.get("ownership_settled") is True and set(post.get("records", {})) == set(RECORD_FILES), "seal_mismatch")
    records = {}
    for name in RECORD_FILES:
        require(base.file_hash(directory / name) == post["records"][name], "record_seal_mismatch")
        value = base.read_private(directory / name)
        require(value.get("complete") is True and value.get("ownership_settled") is True
                and value.get("configuration_sha256") == digest, "record_invalid")
        records[name] = value
    worker, probe = records["worker.json"], records["digest.json"]
    require(len(worker["warmups"]) == len(probe["warmups"]) == 3 and len(worker["rows"]) == 486
            and len(worker["boundaries"]) == 48 and len(probe["outputs"]) == 534, "records_incomplete")
    require([(r["case_id"], r["condition"]) for r in worker["rows"]]
            == [(c["id"], condition) for c in cases for condition in ("fresh", "identical_repeat")], "rows_incomplete")
    errors = []
    for row in worker["warmups"]:
        require(row.get("condition") == "warmup", "warmup_invalid")
        errors.append(validate_worker_row(row, cases[0], reference["rows"][0]))
    for index, row in enumerate(worker["rows"]):
        errors.append(validate_worker_row(row, cases[index // 2], reference["rows"][index]))
    for row, expected in zip(worker["boundaries"], reference["boundaries"], strict=True):
        errors.append(validate_boundary(row, expected))
    all_requests = requests(reference)
    vocab_size = reference_digest["readiness"]["vocab_size"]
    require(worker["readiness"]["vocab_size"] == probe["readiness"]["vocab_size"] == vocab_size, "vocab_size_mismatch")
    for value in probe["warmups"]:
        validate_digest(value, all_requests[0][0], reference_digest["outputs"][0], vocab_size)
    for value, request, expected in zip(probe["outputs"], all_requests, reference_digest["outputs"], strict=True):
        validate_digest(value, request[0], expected, vocab_size)
    quality = {}
    for tier, predicate in (("public", lambda c: c["tier"] != "synthetic"), ("synthetic", lambda c: c["tier"] == "synthetic")):
        current = [base.quality_row(case, normalized_row(worker["rows"][index * 2]))
                   for index, case in enumerate(cases) if predicate(case)]
        prior = [base.quality_row(case, reference["rows"][index * 2])
                 for index, case in enumerate(cases) if predicate(case)]
        require(current == prior, "quality_reference_mismatch")
        quality[tier] = base.quality_summary(current)
    return {"worker_case_responses": 486, "worker_boundary_responses": 48, "whole_vocabulary_responses": 534,
        "worker_warmups": 3, "digest_warmups": 3, "maximum_calibration_formula_error": max(errors),
        "candidate_outputs_exactly_equal": True, "whole_vocabulary_logits_exactly_equal": True,
        "quality_equals_reference": True, "quality": quality}


def exported_configuration(config):
    artifacts = []
    for name, digest in config["files"].items():
        path = Path(name)
        record = {"sha256": digest}
        if path.is_absolute() and path.is_relative_to(ROOT):
            record["path"] = str(path.relative_to(ROOT))
        else:
            record["external_artifact"] = True
        artifacts.append(record)
    receipt = config["candidate_receipt"]
    return {"schema_version": 3, "mode": "untimed-exact-acceptance", "artifacts": artifacts,
        "candidate_variant": receipt["variant"], "candidate_receipt_sha256": base.sha(base.encoded(receipt)),
        "upstream_revision": receipt["upstream_revision"], "features": receipt["features"],
        "case_count": len(config["case_ids"]), "ordered_case_ids_sha256": base.sha(base.encoded(config["case_ids"])),
        "context_tokens": config["context_tokens"], "temperature": config["temperature"],
        "warmups_per_process": 3, "timing_deferred": True, "background_brekko_left_running": True}


def aggregate(args):
    directory = Path(args.output).absolute()
    require(directory.is_relative_to(ROOT / "test-artifacts") and not directory.is_symlink()
            and directory.stat().st_mode & 0o077 == 0, "private_directory_invalid")
    config = base.read_private(directory / "configuration.json")
    original.verify_files(config)
    _, cases, reference, reference_digest, _ = reference_data(config["reference"])
    result = validate_records(directory, config, cases, reference, reference_digest)
    report = {"schema_version": 3, "status": "complete", "configuration": exported_configuration(config),
        "configuration_sha256": base.sha(base.encoded(config)),
        "post_run_seal_sha256": base.file_hash(directory / "post-run-seal.json"), **result,
        "whole_vocabulary_scope": "Candidate probe versus preserved original-core probe on all case and boundary requests; historical scoring-worker equality covers its exposed candidate outputs.",
        "timing_status": "Deferred. Background Brekko was left working. No timing isolation, duration analysis, or speed claim is made."}
    original.verify_files(config)
    destination = Path(args.report).absolute()
    require(not destination.exists() and not destination.is_symlink(), "report_exists")
    base.private_json(destination, report)
    print(json.dumps({"stage": "aggregate_complete", "case_responses": 486, "boundary_responses": 48,
                      "digest_responses": 534}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument("--candidate-binary", type=Path, default=ROOT / "runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-worker")
    runner.add_argument("--candidate-digest-binary", type=Path, default=ROOT / "runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-score-digest")
    runner.add_argument("--candidate-receipt", type=Path, default=ROOT / "runtime/uzu-rust/deep-optimization/all/BUILD_RECEIPT.json")
    runner.add_argument("--model", type=Path, default=ROOT / "models/uzu-jevk5/bf16")
    runner.add_argument("--reference", type=Path, default=ROOT / "test-artifacts/uzu-optimization-combined")
    runner.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    aggregation = commands.add_parser("aggregate")
    aggregation.add_argument("--output", type=Path, required=True)
    aggregation.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        for key, value in vars(args).items():
            if isinstance(value, Path):
                setattr(args, key, value.absolute())
        if args.command == "run":
            asyncio.run(run(args))
        else:
            aggregate(args)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "deep_validation_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
