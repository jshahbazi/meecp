"""Post-run top-40 protocol supplement; the sealed retry evaluator stays immutable."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "experiments/uzu-rust/jev-scoring"
DEFAULT_REPORT = PUBLIC / "SOL_BF16_READOUT_AUDIT.json"
MODE = "sol-uzu-control-llama-bf16-full"


class AuditError(ValueError):
    """Authored diagnostic codes only."""


def require(value, code):
    if not value:
        raise AuditError(code)


def ordinary(path):
    path = Path(path).absolute()
    require(".." not in path.parts and path.is_file()
            and not any(p.is_symlink() for p in (path, *path.parents)), "file_invalid")
    return path


def file_hash(path):
    with ordinary(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read(path, private=False):
    path = ordinary(path)
    require(not private or path.stat().st_mode & 0o077 == 0, "private_file_invalid")
    return json.loads(path.read_bytes())


def audit(args):
    directory = Path(args.output).absolute()
    require(directory.is_relative_to(ROOT / "test-artifacts") and ".." not in directory.parts
            and directory.is_dir() and directory.stat().st_mode & 0o077 == 0
            and not any(p.is_symlink() for p in (directory, *directory.parents)), "private_directory_invalid")
    config_path = directory / "configuration.json"
    pre_path, post_path = directory / "pre-run-seal.json", directory / "post-run-seal.json"
    config, pre, post = (read(path, True) for path in (config_path, pre_path, post_path))
    report, previous_audit = read(args.source_report), read(args.independent_audit)
    digest, post_hash = sha(encoded(config)), file_hash(post_path)
    profile = config["historical_runtime_binding"]["profile"]
    require(type(config["llama_n_probs"]) is int and config["llama_n_probs"] == 40
            and type(profile["llama_n_probs"]) is int and profile["llama_n_probs"] == 40,
            "top_40_required")
    require(config["schema_version"] == 1 and config["mode"] == MODE
            and config["temperature"] == profile["temperature"] == 1.22
            and type(config["context_tokens"]) is int
            and type(profile["context_tokens"]) is int
            and config["context_tokens"] == profile["context_tokens"] == 8192
            and profile["llama_reference"] == "bf16"
            and config["uzu_probabilities"] == "native-reported"
            and config["fresh_then_identical_repeat"] is True
            and config["candidate_receipt"]["variant"] == "combined"
            and config["control_receipt"]["variant"] == "all", "configuration_invalid")
    require(pre["configuration_sha256"] == post["configuration_sha256"] == digest
            and ordinary(directory / "dataset.json").stat().st_mode & 0o077 == 0
            and pre["dataset_sha256"] == post["dataset_sha256"] == file_hash(directory / "dataset.json")
            and post["ownership_settled"] is True, "seal_invalid")
    # Verify the small private record closure; models/runtime artifacts remain the
    # responsibility of the existing full independent audit, which is bound below.
    names = {f"block-{i:02d}.json" for i in range(1, 13)}
    require(set(post["blocks"]) == names
            and {p.name for p in directory.glob("block-*.json")} == names, "blocks_incomplete")
    for name in sorted(names):
        path = directory / name
        require(ordinary(path).stat().st_mode & 0o077 == 0
                and file_hash(path) == post["blocks"][name], "block_seal_invalid")
    source_paths = [ROOT / "scripts/audit_jev_sol_retry.py",
                    ROOT / "scripts/benchmark_jev_sol_retry.py",
                    PUBLIC / "SOL_BF16_PROTOCOL.md", PUBLIC / "SOL_BF16_RETRY_PROTOCOL.md"]
    for path in source_paths:
        require(file_hash(path) == config["files"][str(path)], "bound_source_changed")
    require(report["schema_version"] == 1 and report["status"] == "complete" and report["mode"] == MODE
            and report["configuration_sha256"] == digest
            and report["post_run_seal_sha256"] == post_hash
            and report["candidate_variant"] == "combined"
            and report["candidate_receipt_sha256"] == sha(encoded(config["candidate_receipt"]))
            and report["uzu_outputs_exactly_equal_to_reference"] is True
            and report["uzu_quality_exactly_equal_to_reference"] is True
            and report["measured_responses"] == 5832 and report["checked_warmups"] == 36,
            "timing_report_invalid")
    require(previous_audit["schema_version"] == 1 and previous_audit["status"] == "passed"
            and previous_audit["source_report_sha256"] == file_hash(args.source_report)
            and previous_audit["audit_source_sha256"] == file_hash(source_paths[0])
            and previous_audit["configuration_sha256"] == digest
            and previous_audit["post_run_seal_sha256"] == post_hash
            and previous_audit["whole_vocabulary_acceptance_preserved"] is True
            and previous_audit["public_scalar_groups_exactly_reproduced"] is True
            and previous_audit["measured_responses"] == 5832
            and previous_audit["checked_warmups"] == 36
            and previous_audit["exact_native_uzu_responses"] == 3888
            and previous_audit["exact_native_uzu_warmups"] == 24,
            "independent_audit_invalid")
    return {"schema_version": 1, "status": "passed", "mode": MODE,
        "scope": "Post-run top-40 protocol supplement to the existing full independent audit.",
        "supplement_source_sha256": file_hash(__file__),
        "configuration_sha256": digest, "configuration_file_sha256": file_hash(config_path),
        "pre_run_seal_sha256": file_hash(pre_path), "post_run_seal_sha256": post_hash,
        "source_report_sha256": file_hash(args.source_report),
        "independent_audit_sha256": file_hash(args.independent_audit),
        "independent_audit_source_sha256": file_hash(source_paths[0]),
        "benchmark_source_sha256": file_hash(source_paths[1]),
        "protocol_sha256": file_hash(source_paths[2]), "retry_protocol_sha256": file_hash(source_paths[3]),
        "llama_n_probs": 40, "historical_llama_n_probs": 40,
        "sealed_blocks_verified": 12, "measured_responses": 5832, "checked_warmups": 36,
        "limits": "Relies on the bound full independent audit for native equality, host isolation, arithmetic and model/runtime closure."}


def write_report(path, result):
    path = Path(path).absolute()
    require(path.is_relative_to(PUBLIC) and ".." not in path.parts
            and not path.exists() and not any(p.is_symlink() for p in (path, *path.parents)),
            "report_destination_invalid")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded(result))
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--independent-audit", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    try:
        write_report(args.report, audit(args))
        print(json.dumps({"stage": "top_40_protocol_supplement_complete", "status": "passed"}), flush=True)
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"stage": "failed", "error": "top_40_protocol_supplement_failed"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
