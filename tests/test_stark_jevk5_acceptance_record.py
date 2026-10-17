"""Preserve historical evidence and retire changed installed-source acceptance.

An explicit, exhaustively pinned retirement can acknowledge a reviewed source
change. It never transfers the historical native acceptance to that source.
Undeclared additional drift still fails the model-free consistency gate.
"""
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_RECORD_SHA256 = "1b63e9f9d5e811fd5a3fb91640e6c942024371a2ea48d14d7b69eccac329bcf6"


def record():
    return json.loads((ROOT / "engines/STARK_JEVK5_VALIDATION.json").read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_scope():
    return json.loads((ROOT / "engines/STARK_JEVK5_CURRENT_SCOPE.json").read_bytes())


def check_retired_scope(scope, accepted, current):
    assert set(scope) == {"schema_version", "scope", "historical_source_commit", "historical_record_sha256",
                          "installed_integration_accepted", "native_reacceptance_required", "current_source_deviations"}
    assert type(scope["schema_version"]) is int and scope["schema_version"] == 1
    assert scope["scope"] == "experimental-jevk5-stark-bf16-installed-integration"
    assert scope["historical_source_commit"] == "893ca7289fcaf0634a0fa9f49390c37deb033821"
    assert scope["historical_record_sha256"] == HISTORICAL_RECORD_SHA256
    assert digest(ROOT / "engines/STARK_JEVK5_VALIDATION.json") == HISTORICAL_RECORD_SHA256
    assert scope["installed_integration_accepted"] is False
    assert scope["native_reacceptance_required"] is True
    assert accepted.keys() == current.keys()
    deviations = {name: {"accepted_sha256": expected, "current_sha256": current[name]}
                  for name, expected in accepted.items() if current[name] != expected}
    assert deviations and scope["current_source_deviations"] == deviations


def test_current_installed_consumer_explicitly_requires_native_reacceptance():
    result = record()["integration"]
    lock = json.loads((ROOT / "engines/stark.lock.json").read_bytes())
    bindings = result["source_bindings_sha256"]
    assert result["accepted"] and result["source_bindings_unchanged"]
    assert bindings["dependency_lock"] == digest(ROOT / "engines/stark.lock.json")
    assert bindings["harness"] == digest(ROOT / "scripts/validate_stark_jevk5_integration.py")
    assert bindings["dependency_helper"] == digest(ROOT / "scripts/stark_dependency.py")
    accepted = bindings["consumer_source"]
    check_retired_scope(current_scope(), accepted, {name: digest(ROOT / name) for name in accepted})
    assert result["package_id"] == "local.jevk5.stark"
    assert result["profile"] == "jevk5-stark-bf16-v1"
    assert lock["revision"] == "aa0c959d097803f5e322dea7bbd4b0301f4c7cea"


@pytest.mark.parametrize("damage", ["claim_accepted", "waive_reacceptance", "additional_source_change",
                                  "missing_deviation", "extra_deviation", "changed_historical_record"])
def test_retirement_cannot_claim_acceptance_or_allow_undeclared_drift(damage):
    scope = current_scope()
    accepted = record()["integration"]["source_bindings_sha256"]["consumer_source"]
    current = {name: digest(ROOT / name) for name in accepted}
    if damage == "claim_accepted":
        scope["installed_integration_accepted"] = True
    elif damage == "waive_reacceptance":
        scope["native_reacceptance_required"] = False
    elif damage == "additional_source_change":
        current["backend/meecp_stark/engine.py"] = "0" * 64
    elif damage == "missing_deviation":
        scope["current_source_deviations"].clear()
    elif damage == "extra_deviation":
        scope["current_source_deviations"]["backend/meecp_stark/engine.py"] = {
            "accepted_sha256": accepted["backend/meecp_stark/engine.py"], "current_sha256": "0" * 64}
    else:
        scope["historical_record_sha256"] = "0" * 64
    with pytest.raises(AssertionError):
        check_retired_scope(scope, accepted, current)


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
    check(current_scope())
