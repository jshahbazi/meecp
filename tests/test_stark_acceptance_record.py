"""Prevent a dependency bump from silently retaining an older acceptance claim.

Public CI checks these source bindings. Model-dependent acceptance itself must
be replayed privately; the public record contains only aggregates and hashes.
"""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records():
    return (json.loads((ROOT / "engines/stark.lock.json").read_bytes()),
            json.loads((ROOT / "engines/STARK_VALIDATION.json").read_bytes()))


def test_public_acceptance_is_bound_to_current_dependency_and_consumer_tools():
    lock, record = records()
    assert record["dependency_lock_sha256"] == digest(ROOT / "engines/stark.lock.json")
    assert record["source_revision"] == lock["revision"]
    assert record["source_tree"] == lock["tree"]
    assert record["dependency_helper_sha256"] == digest(ROOT / "scripts/stark_dependency.py")
    assert record["validator_wrapper_sha256"] == digest(ROOT / "scripts/validate_stark_dependency.py")


def test_both_build_paths_have_complete_native_preservation_records():
    lock, record = records()
    for name, development in (("pinned", False), ("local_checkout_override", True)):
        result = record["native_acceptance"][name]
        assert result["status"] == "passed"
        assert result["source_revision"] == lock["revision"]
        assert result["dependency_lock_sha256"] == record["dependency_lock_sha256"]
        assert result["development_only"] is development
        assert result["numerical_acceptance_passed"] is True
        assert result["cases"] == 243 and result["case_responses"] == 486
        assert result["boundary_requests"] == 48 and result["boundary_responses"] == 96
        assert result["digest_requests"] == 534 and result["digest_responses"] == 1068
        assert result["bit_exact_raw_outputs"] is True
        assert result["bit_exact_vocabulary_digests"] is True
        assert result["ownership_settled"] is True
        assert result["timing_not_reported"] is True
        assert result["dependency_helper_sha256"] == record["dependency_helper_sha256"]
        assert result["validator_wrapper_sha256"] == record["validator_wrapper_sha256"]
    assert (record["native_acceptance"]["pinned"]["reference_configuration_sha256"]
            == record["native_acceptance"]["local_checkout_override"]["reference_configuration_sha256"])


def test_public_acceptance_contains_only_scalar_metadata_and_hashes():
    _, record = records()
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
            assert value is None or type(value) in (str, bool, int)
            if type(value) is str:
                assert not value.startswith(("/Users/", "/private/", "file://"))

    check(record)
