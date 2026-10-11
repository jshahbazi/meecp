"""Validate a built Stark dependency against the private, frozen BF16 corpus.

The dependency receipt and pinned validation tools are checked before and after
the native run. Raw records stay in the supplied private output directory; only
aggregate counts, booleans and hashes may reach stdout. This is untimed scoring
acceptance, separate from installed-package or host integration acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import stark_dependency as dependency


class AcceptanceFailure(Exception):
    """A fixed failure code, never an upstream exception or native output."""


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def file_hash(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "acceptance_file_invalid")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_json(path):
    path = Path(path)
    require(".." not in path.parts and not any(parent.is_symlink() for parent in path.parents),
            "acceptance_privacy_invalid")
    require(path.is_file() and not path.is_symlink()
            and path.stat().st_mode & 0o077 == 0, "acceptance_privacy_invalid")
    return json.loads(path.read_bytes())


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


COUNTS = {
    "cases": 243, "case_responses": 486,
    "boundary_requests": 48, "boundary_responses": 96,
    "digest_requests": 534, "digest_responses": 1068,
    "worker_native_score_calls": 291, "worker_cache_hits": 291,
    "probe_native_score_calls": 534, "probe_cache_hits": 534,
    "tokenization_cases": 243, "candidate_tokens": 16,
    "sequence_steps": 20, "default_off_responses": 2,
}
CHECKS = (
    "bit_exact_raw_outputs", "bit_exact_vocabulary_digests",
    "readiness_identity_valid", "ownership_settled", "clean_eof_valid",
    "restart_cache_cold", "timing_not_reported",
)


def audit_native_report(directory, worker, probe, *, reference=None, model=None, reaper=None):
    """Check sealed completion and finite, full-corpus acceptance claims."""
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink()
            and directory.stat().st_mode & 0o077 == 0
            and ".." not in directory.parts
            and not any(parent.is_symlink() for parent in directory.parents), "acceptance_privacy_invalid")
    report = private_json(directory / "REPORT.json")
    configuration = private_json(directory / "configuration.json")
    pre = private_json(directory / "pre-run-seal.json")
    post = private_json(directory / "post-run-seal.json")
    config_hash = file_hash(directory / "configuration.json")
    require(pre.get("configuration_sha256") == post.get("configuration_sha256") == config_hash
            and post.get("ownership_settled") is True, "acceptance_seal_invalid")
    require(set(post.get("records", {})) == {"worker.json", "digest.json", "sequences.json", "REPORT.json"},
            "acceptance_records_invalid")
    for name, expected in post["records"].items():
        require((directory / name).stat().st_mode & 0o077 == 0, "acceptance_privacy_invalid")
        require(file_hash(directory / name) == expected, "acceptance_seal_invalid")
    require(report.get("status") == "passed"
            and all(type(report.get(name)) is int and report[name] == expected for name, expected in COUNTS.items())
            and all(report.get(name) is True for name in CHECKS), "acceptance_incomplete")
    worker_hash, probe_hash = file_hash(worker), file_hash(probe)
    require(report.get("worker_sha256") == worker_hash and report.get("probe_sha256") == probe_hash
            and configuration.get("worker") == str(worker) and configuration.get("probe") == str(probe),
            "acceptance_binary_mismatch")
    reference_hash = report.get("reference_configuration_sha256")
    require(type(reference_hash) is str and re.fullmatch(r"[0-9a-f]{64}", reference_hash) is not None
            and configuration.get("reference_configuration_sha256") == reference_hash,
            "acceptance_reference_mismatch")
    if reference is not None:
        require(configuration.get("reference_directory") == str(reference), "acceptance_reference_mismatch")
    if model is not None:
        require(configuration.get("model") == str(model), "acceptance_model_mismatch")
    files = configuration.get("files")
    require(type(files) is dict and str(worker) in files and str(probe) in files,
            "acceptance_bindings_invalid")
    if reaper is not None:
        require(files.get(str(reaper)) == file_hash(reaper), "acceptance_reaper_mismatch")
    for name, expected in files.items():
        require(file_hash(name) == expected, "acceptance_artifact_changed")
    return {**COUNTS, **{key: True for key in CHECKS}, "status": "passed",
            "worker_sha256": worker_hash, "probe_sha256": probe_hash,
            "reference_configuration_sha256": report["reference_configuration_sha256"],
            "native_configuration_sha256": config_hash,
            "native_report_sha256": file_hash(directory / "REPORT.json"),
            "native_post_run_seal_sha256": file_hash(directory / "post-run-seal.json")}


def validate_build(args):
    build = args.build.absolute()
    lock_path = args.lock.absolute()
    lock = dependency.load_lock(lock_path)
    receipt = dependency.verify_build(build, lock)
    source = build / "source"
    # A development override may change the engine. It cannot change its judge.
    for name, expected in lock["validation_tools_sha256"].items():
        require(file_hash(source / name) == expected, "acceptance_tool_mismatch")
    worker, probe = build / "bin/stark-score", build / "bin/stark-score-digest"
    validator = source / "scripts/validate_scoring_rename.py"
    reaper = args.reaper.absolute()
    reference, model, output = (value.absolute() for value in
                                (args.reference_directory, args.model, args.output_root))
    require(not output.exists() and not output.is_symlink(), "acceptance_output_exists")
    bound = {str(path): file_hash(path) for path in
             (lock_path, build / "DEPENDENCY_LOCK.json", build / "BUILD_RECEIPT.json",
              Path(__file__).absolute(), Path(dependency.__file__).absolute(), reaper,
              reference / "REFERENCE.json", reference / "MANIFEST.json")}
    environment = {"HOME": os.environ.get("HOME", str(build)), "PATH": "/usr/bin:/bin",
                   "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1", "RUST_LOG": "off"}
    process = subprocess.run(
        [sys.executable, str(validator), "--reference-directory", str(reference),
         "--reaper", str(reaper), "--worker", str(worker), "--probe", str(probe),
         "--model", str(model), "--output-root", str(output)],
        cwd=source, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    require(process.returncode == 0, "stark_native_acceptance_failed")
    result = audit_native_report(output, worker, probe, reference=reference, model=model, reaper=reaper)
    require(dependency.verify_build(build, lock) == receipt, "acceptance_build_changed")
    for name, expected in bound.items():
        require(file_hash(name) == expected, "acceptance_artifact_changed")
    result.update({"schema_version": 1, "mode": "untimed-stark-dependency-acceptance",
                   "dependency_lock_sha256": bound[str(lock_path)],
                   "dependency_build_receipt_sha256": bound[str(build / "BUILD_RECEIPT.json")],
                   "validator_wrapper_sha256": bound[str(Path(__file__).absolute())],
                   "dependency_helper_sha256": bound[str(Path(dependency.__file__).absolute())],
                   "source_revision": receipt["source"]["revision"],
                   "source_tree": receipt["source"]["tree"],
                   "development_only": receipt["source"]["development_only"],
                   "source_dirty": receipt["source"]["dirty"],
                   "numerical_acceptance_passed": True})
    write_private(output / "DEPENDENCY_ACCEPTANCE.json", result)
    return result


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise AcceptanceFailure("acceptance_arguments_invalid")


def main():
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--build", type=Path, required=True)
        parser.add_argument("--lock", type=Path, default=dependency.DEFAULT_LOCK)
        for flag in ("reference-directory", "reaper", "model", "output-root"):
            parser.add_argument("--" + flag, type=Path, required=True)
        result = validate_build(parser.parse_args())
    except (Exception, KeyboardInterrupt) as failure:
        code = str(failure) if isinstance(failure, AcceptanceFailure) else "stark_dependency_acceptance_failed"
        print(json.dumps({"status": "failed", "error": code, "timing_not_reported": True}), flush=True)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
