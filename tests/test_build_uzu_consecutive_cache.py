"""Bounded cache build recipe checks; native build/model work is prohibited."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/uzu-rust"))
import build_consecutive_cache_scoring as build


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: pytest.fail("real process work forbidden"))


def test_complete_sol_patch_chain_has_no_new_engine_patch():
    names = [path.name for path in build.patch_chain()]
    assert names == ["uzu-next-token-scoring.patch", "uzu-scoring-topology.patch", "uzu-scoring-state-init.patch",
        "uzu-deep-submission.patch", "uzu-deep-last-layer.patch", "uzu-deep-metal.patch",
        "uzu-sol-rope.patch", "uzu-sol-gdn.patch", "uzu-sol-attention.patch"]


def test_exact_candidate_and_calibration_math_preserved_from_bound_original():
    original = (build.base.EXPERIMENT / "src/scoring.rs").read_text()
    candidate = (build.WORKER / "src/scoring.rs").read_text()
    def body(text, name):
        start = text.index("fn " + name + "(")
        return text[start:text.index("\n}\n", start) + 3]
    assert body(original, "candidate_readout") == body(candidate, "candidate_readout")
    assert body(original, "calibrated_probabilities") == body(candidate, "calibrated_probabilities")


def test_project_uses_source_copies_and_exact_locked_manifest(tmp_path):
    bindings = build.source_bindings()
    destination = tmp_path / "fresh"
    destination.mkdir()
    (destination / "source").mkdir()
    upstream = destination / "source/upstream"
    manifest_path = build.prepare_project(destination, upstream, bindings)
    manifest = manifest_path.read_text()
    assert str(destination / "source/worker/src/main.rs") in manifest
    assert str(destination / "source/worker/probes/score_digest.rs") in manifest
    assert str(build.WORKER / "src/main.rs") not in manifest
    for source in ("crates/uzu-engine", "crates/legacy/hanashi", "crates/legacy/shoji"):
        assert str(upstream / source) in manifest
    assert (destination / "project/Cargo.lock").read_bytes() == (build.WORKER / "Cargo.lock").read_bytes()
    assert (build.WORKER / "Cargo.lock").read_bytes() == (build.base.SCORING / "Cargo.lock").read_bytes()


def test_changed_worker_source_rejected_before_native_build(tmp_path):
    bindings = build.source_bindings()
    bindings[build.relative(build.WORKER / "src/cache.rs")] = "0" * 64
    destination = tmp_path / "fresh"
    (destination / "source").mkdir(parents=True)
    with pytest.raises(ValueError, match="cache_build_identity_invalid"):
        build.prepare_project(destination, destination / "source/upstream", bindings)


def test_receipts_are_private_create_only(tmp_path):
    receipt = tmp_path / "RECEIPT.json"
    build.private_json(receipt, {"status": "complete"})
    with pytest.raises(FileExistsError):
        build.private_json(receipt, {"status": "changed"})
    assert json.loads(receipt.read_text())["status"] == "complete"
    assert receipt.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("name", ["../overwrite", "", "UPPER", "a/b", ".hidden"])
def test_invalid_build_name_fails_before_process_work(name):
    with pytest.raises(ValueError, match="cache_build_identity_invalid"):
        build.build(name, None, True)


def test_existing_destination_rejected_before_native_work(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "OUTPUT", tmp_path)
    (tmp_path / "fresh").mkdir()
    with pytest.raises(ValueError, match="cache_build_identity_invalid"):
        build.build("fresh", None, True)


def test_original_published_sol_stack_still_matches_receipt():
    if not build.SOL_CONTROL.exists() and not build.SOL_CONTROL.is_symlink():
        pytest.skip("published SOL control artifact unavailable")
    result = build.verify_sol_control()
    assert result["binary_sha256"] == "384310c592ef1f217e148dea51f8182cc95d5c14e724cb38afde2bc22b0840d1"
