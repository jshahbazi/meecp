"""Private, sealed comparison of deeper fresh-state Uzu scoring implementations."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_jev_uzu as base
import benchmark_uzu_scoring_optimization as original

ROOT = base.ROOT
PROTOCOL = base.EXPERIMENT / "DEEP_OPTIMIZATION_PROTOCOL.md"
REVISION = "8d01581474760bd1fc7f4a547ff1d02f7dada437"
BASELINE_SHA = "af047d5ae3fcf0c088443aad40fbe0f3d35cf137e4cb15e390a1f922103ea04a"
LENGTHS = (1, 2, 255, 256, 257, 1023, 1024, 1025, 2048, 2049, 4096, 8192)
METRICS = ("state_creation_seconds", "prefill_seconds", "readout_seconds", "score_seconds", "total_seconds")
IDENTITY = ("input_token_ids", "candidate_token_ids", "logits", "log_probabilities", "probabilities", "decision")
require = base.require


def ordinary_file(path):
    path = Path(path).absolute()
    require(path.is_file() and not path.is_symlink(), "artifact_file_invalid")
    return path


def repository_file(name):
    require(type(name) is str and not Path(name).is_absolute() and ".." not in Path(name).parts,
            "receipt_path_invalid")
    path = ordinary_file(ROOT / name)
    require(path.resolve().is_relative_to(ROOT.resolve()), "receipt_path_invalid")
    return path


def candidate_identity(binary, receipt_path, *, allow_baseline=False):
    binary, receipt_path = ordinary_file(binary), ordinary_file(receipt_path)
    value = json.loads(receipt_path.read_bytes())
    require(value.get("schema_version") == 2 and value.get("upstream_revision") == REVISION
            and value.get("binary_sha256") == base.file_hash(binary), "candidate_receipt_invalid")
    variants = {
        "submission": ["topology", "state-init", "submission"],
        "last-layer": ["topology", "state-init", "last-layer"],
        "metal": ["topology", "state-init", "metal"],
        "submission-last-layer": ["topology", "state-init", "submission", "last-layer"],
        "all": ["topology", "state-init", "submission", "last-layer", "metal"],
    }
    variant = value.get("variant")
    require(variant in variants, "candidate_variant_invalid")
    feature_map = {"state-init": "uzu-engine/deferred-state-init",
        "last-layer": "uzu-engine/scoring-last-layer-pruning",
        "metal": "uzu-engine/direct-delta-conv"}
    expected_features = ["probability-scoring"] + [feature_map[n] for n in variants[variant] if n in feature_map]
    require(value.get("features") == expected_features, "candidate_features_invalid")
    chain = value.get("patch_chain")
    expected_names = ["experiments/uzu-rust/uzu-next-token-scoring.patch"] + [
        f"experiments/uzu-rust/uzu-{'scoring' if n in ('topology', 'state-init') else 'deep'}-{n}.patch" for n in variants[variant]]
    require(type(chain) is list and [r.get("path") for r in chain] == expected_names,
            "candidate_patch_chain_invalid")
    paths = []
    for record in chain:
        require(type(record) is dict and set(record) == {"path", "sha256"}, "candidate_patch_chain_invalid")
        path = repository_file(record["path"])
        require(base.file_hash(path) == record["sha256"], "candidate_patch_hash_invalid")
        paths.append(path)
    require(paths[0] == ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch"
            and len(set(paths)) == len(paths), "candidate_patch_chain_invalid")
    sources = value.get("worker_sources")
    require(type(sources) is dict and set(sources) == {"experiments/uzu-rust/src/main.rs",
            "experiments/uzu-rust/src/scoring.rs"}, "candidate_sources_invalid")
    for name, digest in sources.items():
        path = repository_file(name)
        require(base.file_hash(path) == digest, "candidate_source_hash_invalid")
        paths.append(path)
    probes = value.get("probe_sources")
    require(type(probes) is dict and set(probes) == {"experiments/uzu-rust/probes/score_digest.rs"},
            "probe_sources_invalid")
    for name, digest in probes.items():
        path = repository_file(name)
        require(base.file_hash(path) == digest, "probe_source_hash_invalid")
        paths.append(path)
    for key, path in {
        "build_recipe_sha256": ROOT / "experiments/uzu-rust/build_deep_scoring.py",
        "base_build_recipe_sha256": ROOT / "experiments/uzu-rust/build_scoring.py",
        "original_scoring_manifest_sha256": base.SCORING_CARGO_MANIFEST,
        "cargo_manifest_sha256": receipt_path.parent / "project/Cargo.toml",
        "cargo_lock_sha256": receipt_path.parent / "project/Cargo.lock",
    }.items():
        require(value.get(key) == base.file_hash(ordinary_file(path)), "build_recipe_mismatch")
        paths.append(path)
    require(value["cargo_lock_sha256"] == base.file_hash(base.SCORING_CARGO_LOCK), "build_lock_mismatch")
    return value, [binary, receipt_path, *paths]


def model_identity(model):
    model = Path(model).absolute()
    require(model.is_dir() and not model.is_symlink(), "model_location_invalid")
    manifest_path = base.EXPERIMENT / "MODEL_PROVENANCE.json"
    manifest = json.loads(manifest_path.read_bytes())
    pinned = base.model_acquirer.provenance()
    require(manifest.get("schema_version") == 1 and manifest.get("source") == pinned["source"]
            and manifest.get("calibration") == pinned["calibration"], "model_identity_invalid")
    conversion = manifest.get("conversion", {})
    require(conversion.get("status") == "complete" and conversion.get("dtype") == "bfloat16"
            and conversion.get("quantization") == "none"
            and conversion.get("revision") == "831accb8252a6b6606c378048caeb44178f1a050"
            and conversion.get("source_weights_sha256") == next(
                r["sha256"] for r in pinned["source"]["files"] if r["name"] == "model.safetensors"),
            "model_conversion_invalid")
    records = conversion.get("files")
    require(type(records) is list and records, "model_files_invalid")
    names, paths = set(), [manifest_path]
    for record in records:
        name = record.get("name")
        require(type(name) is str and Path(name).name == name and name not in (".", "..")
                and name not in names, "model_files_invalid")
        names.add(name)
        path = ordinary_file(model / name)
        require(path.stat().st_size == record.get("bytes") and base.file_hash(path) == record.get("sha256"),
                "model_file_mismatch")
        paths.append(path)
    require({"model.safetensors", "config.json", "tokenizer.json", "jevk5_config.json"}.issubset(names)
            and {p.name for p in model.iterdir()} == names, "model_coverage_invalid")
    require(json.loads((model / "jevk5_config.json").read_bytes()) == pinned["calibration"],
            "model_calibration_invalid")
    historical_path = base.EXPERIMENT / "RESULTS.json"
    historical = json.loads(historical_path.read_bytes())
    require(historical.get("status") == "complete" and historical["configuration"]["model_provenance"]
            ["manifest_sha256"] == base.file_hash(manifest_path), "historical_model_binding_changed")
    paths.append(historical_path)
    return paths


def configuration(args):
    _, cases = base.load_dataset(args.dataset)
    baseline_paths = [Path(original.__file__)]
    if getattr(args, "baseline_receipt", None):
        baseline_receipt, baseline_files = original.candidate_identity(args.baseline_binary, args.baseline_receipt, allow_baseline=True)
        require(baseline_receipt["variant"] == "combined", "deep_baseline_variant_invalid")
        baseline_paths += baseline_files
    else:
        baseline_receipt = None
        require(base.file_hash(ordinary_file(args.baseline_binary)) == BASELINE_SHA, "baseline_binary_changed")
        base.native_build_identity(args.baseline_binary)
    receipt, candidate_paths = candidate_identity(args.candidate_binary, args.candidate_receipt)
    paths = model_identity(args.model) + baseline_paths + candidate_paths + [
        ROOT / "scripts/monitor_uzu_experiment.py",
        args.baseline_binary, args.dataset, args.reaper, PROTOCOL, Path(__file__), Path(base.__file__),
        ROOT / "scripts/benchmark_uzu.py", ROOT / "scripts/acquire_jev_uzu_models.py",
        ROOT / "backend/meecp/processes.py", ROOT / "vendor/jevk5/prompt.py",
        ROOT / "runtime/uzu-rust/SCORING_BUILD_RECEIPT.json",
        ROOT / "experiments/uzu-rust/SCORING_EXTENSION.json", base.SCORING_CARGO_LOCK,
        base.SCORING_CARGO_MANIFEST, ROOT / "experiments/uzu-rust/build_scoring.py",
        ROOT / "experiments/uzu-rust/build_deep_scoring.py", base.EXPERIMENT / "DATASET_PROVENANCE.json"]
    probes = {}
    require(bool(args.baseline_digest_binary) == bool(args.candidate_digest_binary), "digest_pair_required")
    if args.baseline_digest_binary:
        for engine in ("baseline", "candidate"):
            binary = ordinary_file(getattr(args, engine + "_digest_binary"))
            receipt_path = ordinary_file(binary.parent.parent / "BUILD_RECEIPT.json")
            if engine == "baseline":
                probe_receipt, probe_paths = original.candidate_identity(binary.parent / "meecp-uzu-worker", receipt_path, allow_baseline=True)
            else:
                probe_receipt, probe_paths = candidate_identity(binary.parent / "meecp-uzu-worker", receipt_path)
            require(probe_receipt.get("schema_version") == (1 if engine == "baseline" else 2) and probe_receipt.get("upstream_revision") == REVISION
                    and probe_receipt.get("digest_binary_sha256") == base.file_hash(binary), "digest_receipt_invalid")
            expected_chain = receipt["patch_chain"] if engine == "candidate" else (baseline_receipt["patch_chain"] if baseline_receipt else receipt["patch_chain"][:1])
            require(probe_receipt.get("patch_chain") == expected_chain, "digest_patch_chain_invalid")
            require(engine != "candidate" or receipt_path == args.candidate_receipt,
                    "digest_candidate_receipt_mismatch")
            probes[engine] = str(binary)
            paths += [binary, *probe_paths]
    files = {str(ordinary_file(p)): base.file_hash(p) for p in paths}
    selected = [c for c in cases if not args.smoke or c["tier"] == "synthetic"]
    return {"schema_version": 1, "files": files, "smoke": args.smoke,
            "rounds": 1 if args.smoke else 2, "case_ids": [c["id"] for c in selected],
            "warmups": 3, "boundary_lengths": list(LENGTHS), "temperature": base.TEMPERATURE,
            "context_tokens": base.CONTEXT, "candidate_receipt": receipt, "baseline_receipt": baseline_receipt,
            "model": str(args.model), "baseline_binary": str(args.baseline_binary),
            "candidate_binary": str(args.candidate_binary), "digest_binaries": probes,
            "machine": base.machine_information()}


def verify_files(config):
    require(type(config.get("files")) is dict and config["files"], "configuration_invalid")
    for name, digest in config["files"].items():
        require(base.file_hash(ordinary_file(name)) == digest, "artifact_changed")


def validate_response(value, tokens, candidates):
    base.validate_uzu_response(value, tokens, candidates)
    require(set(METRICS).issubset(value["metrics"]), "metrics_missing")
    require(all(type(value["metrics"][k]) in (int, float) and math.isfinite(value["metrics"][k])
                and value["metrics"][k] >= 0 for k in METRICS), "metrics_invalid")


async def score(client, tokens, candidates):
    value = await client.post({"operation": "score", "input_token_ids": tokens,
        "candidate_token_ids": candidates, "context_tokens": base.CONTEXT, "temperature": base.TEMPERATURE})
    validate_response(value, tokens, candidates)
    return value


async def measure(client, case, candidates, condition):
    started = time.perf_counter()
    tokens, tokenize_seconds = await base.tokenize("uzu", client, base.rendered(case), True)
    require(0 < len(tokens) <= base.CONTEXT, "context_limit")
    selected = candidates[:len(base.canonical_option_keys(case["question"]))]
    scoring = time.perf_counter()
    value = await score(client, tokens, selected)
    score_seconds = time.perf_counter() - scoring
    probs = value["candidate_probabilities"]
    return {"case_id": case["id"], "condition": condition, "input_tokens": len(tokens),
        "input_token_ids": tokens, "candidate_token_ids": selected, "logits": value["logits"],
        "log_probabilities": value["log_probabilities"], "probabilities": probs,
        "decision": base.typed_decision(case["question"], probs, len(tokens)), "cache_hit": False,
        "truncated": False, "tokenization_seconds": tokenize_seconds, "score_request_seconds": score_seconds,
        "wall_seconds": time.perf_counter() - started, "engine_metrics": value["metrics"]}


async def boundaries(client, candidates):
    seed, _ = await base.tokenize("uzu", client, "A quiet garden contains green leaves and small stones.", False)
    require(len(seed) >= 2 and len(set(seed)) >= 2, "boundary_seed_invalid")
    rows = []
    for length in LENGTHS:
        tokens = (seed * (length // len(seed) + 1))[:length]
        changed = tokens.copy()
        changed[-1] = next(t for t in seed if t != changed[-1])
        for kind, ids in (("fresh", tokens), ("repeat", tokens), ("changed", changed), ("return", tokens)):
            value = await score(client, ids, candidates[:2])
            rows.append({"length": length, "kind": kind, "tokens": ids, "response": value})
    return rows


def validate_digest(value, tokens, vocab_size):
    require(value.get("input_tokens") == len(tokens)
            and value.get("prompt_tokens_sha256") == base.token_digest(tokens)
            and value.get("cache_hit") is False and value.get("truncated") is False
            and value.get("vocab_size") == vocab_size
            and type(value.get("vocab_logits_sha256")) is str
            and len(value["vocab_logits_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in value["vocab_logits_sha256"]), "digest_response_invalid")


async def digest_block(owner, binary, model, scratch, reference):
    env = {"HOME": str(scratch), "TMPDIR": str(scratch), "PATH": "/usr/bin:/bin", "LANG": "C", "RUST_LOG": "off"}
    process = await owner.launch(Path(binary), ["--score-model-path", str(model)], ROOT,
                                env, io_mode="streams", owned_group=True, stop_grace=.5)
    try:
        require(process.stdout is not None, "digest_io_unavailable")
        async with asyncio.timeout(240):
            raw = await process.stdout.readline()
        require(raw and len(raw) < 1024 * 1024, "digest_readiness_invalid")
        ready = json.loads(raw)
        require(ready.get("ready") is True and ready.get("mode") == "exact-token-digest-v1"
                and ready.get("cache_policy") == "fresh-state"
                and ready.get("upstream_revision") == REVISION
                and ready.get("scoring_patch_sha256") == base.file_hash(ROOT / "experiments/uzu-rust/uzu-next-token-scoring.patch")
                and type(ready.get("vocab_size")) is int and ready["vocab_size"] > 0, "digest_readiness_invalid")
        client = base.RustClient(process, ready)
        requests = [(r["input_token_ids"], r["candidate_token_ids"]) for r in reference["rows"]]
        requests += [(r["tokens"], r["response"]["candidate_token_ids"]) for r in reference["boundaries"]]
        outputs = []
        for tokens, candidates in requests:
            value = await client.post({"operation": "score", "input_token_ids": tokens,
                "candidate_token_ids": candidates, "context_tokens": base.CONTEXT, "temperature": base.TEMPERATURE})
            validate_digest(value, tokens, ready["vocab_size"])
            outputs.append(value)
    finally:
        await process.stop()
    require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
    return {"complete": True, "ownership_settled": True, "readiness": ready, "outputs": outputs}


async def run(args):
    config = configuration(args)
    dataset, all_cases = base.load_dataset(args.dataset)
    cases = [c for c in all_cases if c["id"] in config["case_ids"]]
    directory = base.secure_directory(args.output)
    require(not list(directory.iterdir()), "run_directory_not_empty")
    base.private_json(directory / "configuration.json", config)
    base.private_json(directory / "dataset.json", dataset)
    digest = base.sha(base.encoded(config))
    base.private_json(directory / "pre-run-seal.json", {"configuration_sha256": digest,
        "dataset_sha256": base.file_hash(directory / "dataset.json")})
    owner = base.ProcessOwner(args.reaper)
    try:
        with tempfile.TemporaryDirectory(prefix="meecp-uzu-opt-") as temporary:
            scratch = Path(temporary)
            scratch.chmod(0o700)
            for round_index in range(config["rounds"]):
                order = ("baseline", "candidate") if round_index == 0 else ("candidate", "baseline")
                for engine in order:
                    launch_args = SimpleNamespace(uzu_binary=getattr(args, engine + "_binary"), uzu_model=args.model)
                    process, client, startup = await base.launch(owner, "uzu", launch_args, scratch)
                    try:
                        candidates = []
                        for letter in base.LETTERS:
                            ids, _ = await base.tokenize("uzu", client, letter, False)
                            require(len(ids) == 1, "candidate_not_single_token")
                            candidates.append(ids[0])
                        require(len(set(candidates)) == 16, "candidate_duplicate")
                        for _ in range(config["warmups"]):
                            await measure(client, cases[0], candidates, "warmup")
                        rows = []
                        for index, case in enumerate(cases):
                            for condition in ("fresh", "identical_repeat"):
                                rows.append(await measure(client, case, candidates, condition))
                            if (index + 1) % 20 == 0:
                                print(json.dumps({"stage": "scoring", "round": round_index + 1,
                                    "engine": engine, "cases": index + 1}), flush=True)
                        boundary_rows = await boundaries(client, candidates)
                    finally:
                        await process.stop()
                    require(not process.ownership_lost and not owner.instances, "ownership_unsettled")
                    base.private_json(directory / f"round-{round_index + 1}-{engine}.json", {
                        "complete": True, "configuration_sha256": digest, "round": round_index + 1,
                        "engine": engine, "ownership_settled": True, "startup_seconds": startup,
                        "warmup_count": config["warmups"], "peak_rss_bytes": process.peak_memory_bytes,
                        "readiness": client.readiness, "rows": rows, "boundaries": boundary_rows})
                    print(json.dumps({"stage": "block_complete", "round": round_index + 1,
                        "engine": engine, "cases": len(cases)}), flush=True)
            if config["digest_binaries"]:
                reference = base.read_private(directory / "round-1-baseline.json")
                for engine, binary in config["digest_binaries"].items():
                    block = await digest_block(owner, binary, args.model, scratch, reference)
                    block["configuration_sha256"] = digest
                    base.private_json(directory / f"digest-{engine}.json", block)
                    print(json.dumps({"stage": "digest_complete", "engine": engine,
                        "measurements": len(block["outputs"])}), flush=True)
        verify_files(config)
        blocks = {p.name: base.file_hash(p) for p in sorted(directory.glob("round-*.json"))}
        base.private_json(directory / "post-run-seal.json", {"configuration_sha256": digest,
            "dataset_sha256": base.file_hash(directory / "dataset.json"), "blocks": blocks,
            "digests": {p.name: base.file_hash(p) for p in sorted(directory.glob("digest-*.json"))},
            "ownership_settled": not owner.instances})
    finally:
        await owner.close()


def validate_row(row, case):
    tokens, candidates = row["input_token_ids"], row["candidate_token_ids"]
    require(type(tokens) is list and 0 < len(tokens) <= base.CONTEXT
            and row["input_tokens"] == len(tokens), "row_tokens_invalid")
    base.token_digest(tokens)
    require(len(candidates) == len(base.canonical_option_keys(case["question"]))
            and all(type(t) is int and 0 <= t < 2**32 for t in candidates)
            and len(set(candidates)) == len(candidates), "row_candidates_invalid")
    value = {"input_tokens": len(tokens), "prompt_tokens_sha256": base.token_digest(tokens),
        "candidate_token_ids": candidates, "temperature": base.TEMPERATURE, "truncated": row["truncated"],
        "cache_hit": row["cache_hit"], "logits": row["logits"], "log_probabilities": row["log_probabilities"],
        "candidate_probabilities": row["probabilities"], "metrics": row["engine_metrics"]}
    validate_response(value, tokens, candidates)
    require(row["decision"] == base.typed_decision(case["question"], row["probabilities"], len(tokens)),
            "typed_decision_invalid")
    base.timing_summary([row])
    require(row["wall_seconds"] > 0 and row["score_request_seconds"] > 0, "timing_invalid")


def equal_rows(left, right):
    require(all(left[k] == right[k] for k in IDENTITY), "output_equality_failed")


def load_blocks(directory, config, cases):
    digest = base.sha(base.encoded(config))
    pre = base.read_private(directory / "pre-run-seal.json")
    post = base.read_private(directory / "post-run-seal.json")
    expected = {f"round-{r}-{e}.json" for r in range(1, config["rounds"] + 1) for e in ("baseline", "candidate")}
    require(set(post.get("blocks", {})) == expected
            and {p.name for p in directory.glob("round-*.json")} == expected, "blocks_incomplete")
    require(pre.get("configuration_sha256") == post.get("configuration_sha256") == digest
            and pre.get("dataset_sha256") == post.get("dataset_sha256")
            == base.file_hash(directory / "dataset.json") and post.get("ownership_settled") is True,
            "seal_mismatch")
    by_case = {c["id"]: c for c in cases}
    require(config["case_ids"] == [c["id"] for c in cases], "case_selection_invalid")
    blocks = {}
    for name in sorted(expected):
        path = directory / name
        require(base.file_hash(path) == post["blocks"][name], "block_seal_mismatch")
        block = base.read_private(path)
        key = (block["round"], block["engine"])
        require(name == f"round-{key[0]}-{key[1]}.json" and block.get("complete") is True
                and block.get("configuration_sha256") == digest and block.get("ownership_settled") is True
                and block.get("warmup_count") == 3, "block_invalid")
        rows = block["rows"]
        require([(r["case_id"], r["condition"]) for r in rows]
                == [(c["id"], cond) for c in cases for cond in ("fresh", "identical_repeat")], "rows_incomplete")
        for row in rows:
            validate_row(row, by_case[row["case_id"]])
        boundary = block["boundaries"]
        require([(r["length"], r["kind"]) for r in boundary]
                == [(n, k) for n in LENGTHS for k in ("fresh", "repeat", "changed", "return")],
                "boundaries_incomplete")
        for row in boundary:
            require(len(row["tokens"]) == row["length"], "boundary_length_invalid")
            validate_response(row["response"], row["tokens"], row["response"]["candidate_token_ids"])
        for start in range(0, len(boundary), 4):
            fresh, repeat, changed, returned = boundary[start:start + 4]
            require(changed["tokens"] != fresh["tokens"], "boundary_change_missing")
            for other in (repeat, returned):
                require(fresh["tokens"] == other["tokens"], "boundary_repeat_invalid")
        blocks[key] = block
    reference = blocks[(1, "baseline")]
    for block in blocks.values():
        for index, row in enumerate(block["rows"]):
            equal_rows(reference["rows"][index - index % 2], row)
        for index, row in enumerate(block["boundaries"]):
            expected_index = index if row["kind"] == "changed" else index - index % 4
            expected_row = reference["boundaries"][expected_index]
            require(row["tokens"] == expected_row["tokens"], "boundary_tokens_differ")
            for field in ("candidate_token_ids", "logits", "log_probabilities", "candidate_probabilities"):
                require(row["response"][field] == expected_row["response"][field], "boundary_equality_failed")
    return blocks


def timing_group(pairs):
    return {"pairs": len(pairs), "baseline": base.timing_summary([a for a, _ in pairs]),
        "candidate": base.timing_summary([b for _, b in pairs]),
        "median_baseline_over_candidate_wall_ratio": base.percentile([a["wall_seconds"] / b["wall_seconds"]
            for a, b in pairs], .5),
        "median_baseline_over_candidate_score_request_ratio": base.percentile([
            a["score_request_seconds"] / b["score_request_seconds"] for a, b in pairs], .5)}


def verify_digests(directory, config, reference):
    post = base.read_private(directory / "post-run-seal.json")
    expected = {f"digest-{engine}.json" for engine in config.get("digest_binaries", {})}
    require(set(post.get("digests", {})) == expected
            and {p.name for p in directory.glob("digest-*.json")} == expected, "digest_blocks_invalid")
    if not expected:
        return False
    require(expected == {"digest-baseline.json", "digest-candidate.json"}, "digest_pair_required")
    requests = [r["input_token_ids"] for r in reference["rows"]] + [r["tokens"] for r in reference["boundaries"]]
    blocks = []
    for name in sorted(expected):
        require(base.file_hash(directory / name) == post["digests"][name], "digest_seal_mismatch")
        block = base.read_private(directory / name)
        require(block.get("complete") is True and block.get("ownership_settled") is True
                and block.get("configuration_sha256") == base.sha(base.encoded(config))
                and len(block["outputs"]) == len(requests), "digest_block_invalid")
        vocab = reference["readiness"]["vocab_size"]
        require(block["readiness"]["vocab_size"] == vocab, "digest_vocab_invalid")
        for value, tokens in zip(block["outputs"], requests, strict=True):
            validate_digest(value, tokens, vocab)
        blocks.append(block)
    fields = ("vocab_logits_sha256", "vocab_size", "prompt_tokens_sha256", "input_tokens")
    require(all(all(a[k] == b[k] for k in fields) for a, b in zip(blocks[0]["outputs"], blocks[1]["outputs"], strict=True)),
            "vocabulary_equality_failed")
    for block in blocks:
        outputs = block["outputs"]
        timed_count = len(reference["rows"])
        for index in range(0, timed_count, 2):
            require(outputs[index]["vocab_logits_sha256"] == outputs[index + 1]["vocab_logits_sha256"],
                    "vocabulary_stability_failed")
        for index in range(timed_count, len(outputs), 4):
            require(outputs[index]["vocab_logits_sha256"] == outputs[index + 1]["vocab_logits_sha256"]
                    == outputs[index + 3]["vocab_logits_sha256"], "vocabulary_stability_failed")
    return True


def exported_configuration(config):
    """Export allowlisted metadata; host paths and individual case IDs stay private."""
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
    return {"schema_version": config["schema_version"], "smoke": config["smoke"],
        "rounds": config["rounds"], "case_count": len(config["case_ids"]),
        "ordered_case_ids_sha256": base.sha(base.encoded(config["case_ids"])),
        "warmups": config["warmups"], "boundary_lengths": config["boundary_lengths"],
        "temperature": config["temperature"], "context_tokens": config["context_tokens"],
        "artifacts": artifacts, "candidate_variant": receipt["variant"],
        "candidate_receipt_sha256": base.sha(base.encoded(receipt)),
        "upstream_revision": receipt["upstream_revision"], "features": receipt["features"],
        "baseline_variant": config["baseline_receipt"]["variant"] if config.get("baseline_receipt") else "historical",
        "whole_vocabulary_probes_supplied": bool(config["digest_binaries"])}


def aggregate(args):
    directory = Path(args.output).absolute()
    require(directory.is_relative_to(ROOT / "test-artifacts") and not directory.is_symlink()
            and directory.stat().st_mode & 0o077 == 0, "private_directory_invalid")
    config = base.read_private(directory / "configuration.json")
    verify_files(config)
    _, all_cases = base.load_dataset(directory / "dataset.json")
    require(config.get("rounds") == (1 if config.get("smoke") is True else 2)
            and config.get("temperature") == base.TEMPERATURE and config.get("context_tokens") == base.CONTEXT
            and config.get("boundary_lengths") == list(LENGTHS), "configuration_invalid")
    cases = [c for c in all_cases if not config["smoke"] or c["tier"] == "synthetic"]
    blocks = load_blocks(directory, config, cases)
    whole_vocabulary_verified = verify_digests(directory, config, blocks[(1, "baseline")])
    timing = {}
    for condition in ("fresh", "identical_repeat"):
        pairs = [(a, b) for r in range(1, config["rounds"] + 1)
            for a, b in zip(blocks[(r, "baseline")]["rows"], blocks[(r, "candidate")]["rows"], strict=True)
            if a["condition"] == condition]
        timing[condition] = {"all": timing_group(pairs),
            "by_round": {str(r): timing_group([(a, b) for a, b in zip(blocks[(r, "baseline")]["rows"],
                blocks[(r, "candidate")]["rows"], strict=True) if a["condition"] == condition])
                for r in range(1, config["rounds"] + 1)},
            "by_token_bucket": {name: timing_group([(a, b) for a, b in pairs if lo <= a["input_tokens"] < hi])
                for name, lo, hi in base.TOKEN_BUCKETS}}
    quality = {}
    for engine in ("baseline", "candidate"):
        rows = blocks[(1, engine)]["rows"][::2]
        public = [base.quality_row(c, r) for c, r in zip(cases, rows, strict=True) if c["tier"] != "synthetic"]
        synthetic = [base.quality_row(c, r) for c, r in zip(cases, rows, strict=True) if c["tier"] == "synthetic"]
        quality[engine] = {"public": base.quality_summary(public) if public else None,
                           "synthetic": base.quality_summary(synthetic)}
    report = {"schema_version": 1, "status": "complete", "mode": "smoke" if config["smoke"] else "full",
        "configuration_sha256": base.sha(base.encoded(config)), "configuration": exported_configuration(config),
        "post_run_seal_sha256": base.file_hash(directory / "post-run-seal.json"),
        "measurements": len(cases) * 2 * 2 * config["rounds"],
        "boundary_measurements": len(LENGTHS) * 4 * 2 * config["rounds"],
        "candidate_outputs_exactly_equal": True, "whole_vocabulary_logits_verified": whole_vocabulary_verified,
        "whole_vocabulary_scope": "Untimed rebuilt original-core probe versus optimized-core probe; the preserved historical worker exposes candidate outputs only. Its exact candidate outputs bridge the timed comparison.",
        "equality_scope": "Candidate logits, full-vocabulary-normalized candidate log probabilities, candidate probabilities, typed decisions and exact input/candidate token IDs.",
        "timing_limits": "Serial resident blocks with reversed order; startup, three warmups and boundary checks excluded. Local timing variation is not a precise steady-state speed guarantee.",
        "timing": timing, "quality": quality}
    verify_files(config)
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
    runner.add_argument("--baseline-digest-binary", type=Path)
    runner.add_argument("--candidate-digest-binary", type=Path)
    runner.add_argument("--baseline-receipt", type=Path)
    runner.add_argument("--baseline-binary", type=Path, default=ROOT / "runtime/uzu-rust/scoring-target/release/meecp-uzu-worker")
    runner.add_argument("--model", type=Path, default=ROOT / "models/uzu-jevk5/bf16")
    runner.add_argument("--dataset", type=Path, default=ROOT / "test-artifacts/jev-uzu-screen-bf16-resumed/dataset.json")
    runner.add_argument("--reaper", type=Path, default=ROOT / "runtime/uzu-rust/meecp-reap")
    runner.add_argument("--smoke", action="store_true")
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
        print(json.dumps({"stage": "failed", "error": "deep_evaluation_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
