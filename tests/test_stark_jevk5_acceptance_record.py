"""Retire installed acceptance when its source, engine or model changes."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def record():
    return json.loads((ROOT / "engines/STARK_JEVK5_VALIDATION.json").read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_installed_acceptance_binds_current_consumer_and_pin():
    result = record()["integration"]
    lock = json.loads((ROOT / "engines/stark.lock.json").read_bytes())
    bindings = result["source_bindings_sha256"]
    assert result["accepted"] and result["source_bindings_unchanged"]
    assert bindings["dependency_lock"] == digest(ROOT / "engines/stark.lock.json")
    assert bindings["harness"] == digest(ROOT / "scripts/validate_stark_jevk5_integration.py")
    assert bindings["dependency_helper"] == digest(ROOT / "scripts/stark_dependency.py")
    for name, expected in bindings["consumer_source"].items():
        assert digest(ROOT / name) == expected
    assert result["package_id"] == "local.jevk5.stark"
    assert result["profile"] == "jevk5-stark-bf16-v1"
    assert lock["revision"] == "aa0c959d097803f5e322dea7bbd4b0301f4c7cea"


def test_complete_native_and_transport_preservation_and_default_off():
    current = record()
    result = current["integration"]
    assert result["private_native"]["case_responses"] == 486
    assert result["private_native"]["boundary_responses"] == 96
    assert result["private_native"]["tokenizations"] == 243
    assert result["private_native"]["candidate_outputs_bit_exact"]
    assert result["private_native"]["maximum_calibration_formula_error"] <= 1e-12
    assert result["api"]["case_responses"] == 486 and result["api"]["cache_hits"] == 243
    assert result["api"]["typed_answers_bit_exact"]
    for name in ("http_mcp", "stdio_mcp"):
        assert result[name]["decisions"] == 12 and result[name]["typed_answers_bit_exact"]
    assert result["ownership_settled"] and result["lifecycle"]["restart_cache_cold"]
    assert result["queue"]["completed_server_decisions"] == 2
    assert result["queue"]["shared_engine_unchanged"] and result["queue"]["pending_drained"]
    assert result["queue"]["server_operation_cancelled"] is False
    smoke = current["default_off_and_native_loss"]["result"]
    assert smoke["accepted"] and smoke["native_cache_hits"] == 0
    assert smoke["decisions"] == 2 and smoke["typed_answers_bit_exact"]
    assert smoke["host_observed_state"] == "failed" and smoke["host_error_code"] == "runtime_crashed"
    assert smoke["ownership_settled"] and smoke["gpu_reservation_released"]
    for name, expected in smoke["source_sha256"].items():
        assert digest(ROOT / name) == expected


def test_public_record_has_only_metadata_and_hashes():
    forbidden = {"input_token_ids", "candidate_token_ids", "logits", "log_probabilities",
                 "candidate_probabilities", "probabilities", "prompt", "state", "response", "question"}
    def check(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        else:
            assert value is None or type(value) in (str, bool, int, float)
            if isinstance(value, str):
                assert not value.startswith(("/Users/", "/private/", "/Volumes/", "file://"))
    check(record())
