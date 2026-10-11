"""Run focused CPU/Metal correctness tests with sanitized scalar receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
FEATURES = "cpu,metal,deferred-state-init,scoring-last-layer-pruning,direct-delta-conv,sol-rope-cache,compact-delta-conv,sol-attention-kv-only"
FILTERS = (("rope_cache_", 2), ("compact_delta_prefill_preserves_", 2),
           ("compact_projection_preserves_prefill_bits", 1),
           ("direct_delta_convolution_preserves_packed_bits", 1),
           ("test_kv_only_prepare_preserves_packed_rope_bits_and_prefix", 1),
           ("test_projected_key_value_norm_retains_offsets_and_dead_channels", 1),
           ("submission_drain_", 2), ("cache_only_requires_an_unobserved_actual_final_layer", 1),
           ("direct_scoring_topology_matches_legacy_flat_trie", 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="combined")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        directory = ROOT / "runtime/uzu-rust/sol-optimization/builds" / args.variant
        receipt_path = directory / "BUILD_RECEIPT.json"
        receipt = json.loads(receipt_path.read_bytes())
        assert receipt["variant"] == "combined" and not args.report.exists()
        manifest = directory / "source/upstream/Cargo.toml"
        env = os.environ.copy()
        env["RUSTC_BOOTSTRAP"] = "uzu_engine"  # Custom upstream test framework only.
        env["CARGO_TARGET_DIR"] = str(ROOT / "runtime/uzu-rust/sol-native-tests-target")
        cargo = str(Path.home() / ".cargo/bin/cargo")
        env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
        results = []
        for name, expected in FILTERS:
            print(json.dumps({"stage": "focused_native_test", "filter": name}), flush=True)
            result = subprocess.run([cargo, "test", "--release", "--locked", "--manifest-path", str(manifest),
                "-p", "uzu-engine", "--lib", "--no-default-features", "--features", FEATURES, name,
                "--", "--test-threads=1"], cwd=ROOT, env=env, capture_output=True)
            raw = result.stdout + result.stderr
            counts = re.findall(rb"test result: ok\. (\d+) passed; 0 failed;", raw)
            passed = sum(int(n) for n in counts)
            results.append({"filter": name, "exit_code": result.returncode, "passed": passed,
                            "output_sha256": hashlib.sha256(raw).hexdigest()})
            if result.returncode or passed != expected:
                print(json.dumps({"stage": "focused_native_test_failed", "filter": name,
                                  "exit_code": result.returncode, "passed": passed}), flush=True)
                return 1
        report = {"schema_version": 1, "status": "complete", "backend": "CPU and Metal",
            "features": FEATURES, "test_only_environment": {"RUSTC_BOOTSTRAP": "uzu_engine"},
            "tests_passed": sum(r["passed"] for r in results), "results": results,
            "candidate_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "test_recipe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "scope": "Focused upstream library tests; production worker/probe builds use ordinary stable Rust."}
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print(json.dumps({"stage": "focused_native_tests_complete", "passed": report["tests_passed"]}), flush=True)
        return 0
    except Exception:
        print(json.dumps({"stage": "failed", "error": "focused_native_tests_failed"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
