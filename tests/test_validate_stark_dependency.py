"""Model-free acceptance checks using sealed synthetic scalar-only artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_stark_dependency as acceptance


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_write(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


def reseal(directory):
    config_hash = digest(directory / "configuration.json")
    private_write(directory / "pre-run-seal.json", {"configuration_sha256": config_hash})
    private_write(directory / "post-run-seal.json", {
        "configuration_sha256": config_hash,
        "ownership_settled": True,
        "records": {name: digest(directory / name) for name in
                    ("worker.json", "digest.json", "sequences.json", "REPORT.json")},
    })


@pytest.fixture
def native_report(tmp_path):
    directory = tmp_path / "acceptance"
    directory.mkdir(mode=0o700)
    worker, probe = tmp_path / "worker", tmp_path / "probe"
    worker.write_bytes(b"synthetic worker executable")
    probe.write_bytes(b"synthetic probe executable")
    configuration = {
        "schema_version": 1, "worker": str(worker), "probe": str(probe),
        "reference_configuration_sha256": "a" * 64,
        "files": {str(worker): digest(worker), str(probe): digest(probe)},
    }
    report = {**acceptance.COUNTS, **{name: True for name in acceptance.CHECKS},
              "status": "passed", "worker_sha256": digest(worker),
              "probe_sha256": digest(probe), "reference_configuration_sha256": "a" * 64}
    private_write(directory / "configuration.json", configuration)
    private_write(directory / "REPORT.json", report)
    for name in ("worker.json", "digest.json", "sequences.json"):
        private_write(directory / name, {"synthetic_scalar_record": True})
    reseal(directory)
    return directory, worker, probe


def audit(fixture):
    return acceptance.audit_native_report(*fixture)


def edit_report(fixture, **changes):
    directory, _, _ = fixture
    report = json.loads((directory / "REPORT.json").read_text())
    report.update(changes)
    private_write(directory / "REPORT.json", report)
    reseal(directory)


def test_full_sealed_acceptance_returns_only_aggregate_scalars(native_report):
    result = audit(native_report)
    assert result["status"] == "passed"
    assert result["cases"] == 243
    assert result["case_responses"] == 486
    assert all(type(value) in (bool, int, str) for value in result.values())
    assert all(str(native_report[0].parent) not in str(value) for value in result.values())


@pytest.mark.parametrize("change", [
    {"cases": 242}, {"case_responses": 485}, {"cases": True},
    {"ownership_settled": False}, {"bit_exact_raw_outputs": False},
    {"bit_exact_vocabulary_digests": False}, {"status": "partial"},
])
def test_partial_or_failed_claims_rejected_even_when_resealed(native_report, change):
    edit_report(native_report, **change)
    with pytest.raises(acceptance.AcceptanceFailure):
        audit(native_report)


def test_record_modified_after_seal_is_rejected(native_report):
    directory, _, _ = native_report
    private_write(directory / "worker.json", {"synthetic_scalar_record": False})
    with pytest.raises(acceptance.AcceptanceFailure, match="acceptance_seal_invalid"):
        audit(native_report)


def test_binary_changed_after_run_is_rejected(native_report):
    _, worker, _ = native_report
    worker.write_bytes(b"different synthetic executable")
    with pytest.raises(acceptance.AcceptanceFailure):
        audit(native_report)


@pytest.mark.parametrize("name", [
    "REPORT.json", "configuration.json", "pre-run-seal.json", "post-run-seal.json",
    "worker.json", "digest.json", "sequences.json",
])
def test_every_private_artifact_rejects_public_permissions(native_report, name):
    directory, _, _ = native_report
    (directory / name).chmod(0o644)
    with pytest.raises(acceptance.AcceptanceFailure, match="acceptance_privacy_invalid"):
        audit(native_report)


def test_private_records_cannot_be_replaced_with_symlinks(native_report):
    directory, _, _ = native_report
    target = directory.parent / "external-record.json"
    target.write_bytes((directory / "worker.json").read_bytes())
    target.chmod(0o600)
    (directory / "worker.json").unlink()
    (directory / "worker.json").symlink_to(target)
    with pytest.raises(acceptance.AcceptanceFailure):
        audit(native_report)


@pytest.mark.parametrize("value", ["not-a-hash", [1, 2], {"unexpected": True}, "B" * 64, "b" * 64])
def test_reference_identity_cannot_inject_non_hash_or_mismatched_public_data(native_report, value):
    edit_report(native_report, reference_configuration_sha256=value)
    with pytest.raises(acceptance.AcceptanceFailure):
        audit(native_report)


def test_failed_native_process_cannot_write_success_receipt(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir()
    source = build / "source"
    tool = source / "scripts/validate_scoring_rename.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# synthetic pinned validator\n")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text("{}\n")
    for name in ("DEPENDENCY_LOCK.json", "BUILD_RECEIPT.json"):
        (build / name).write_text("{}\n")
    reaper = tmp_path / "reaper"
    reaper.write_bytes(b"synthetic reaper")
    reference = tmp_path / "reference"
    reference.mkdir()
    for name in ("REFERENCE.json", "MANIFEST.json"):
        private_write(reference / name, {"synthetic_scalar_reference": True})
    lock = {"validation_tools_sha256": {"scripts/validate_scoring_rename.py": digest(tool)}}
    monkeypatch.setattr(acceptance.dependency, "load_lock", lambda _: lock)
    monkeypatch.setattr(acceptance.dependency, "verify_build", lambda *_: {"source": {"development_only": False}})
    monkeypatch.setattr(acceptance.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=9))
    output = tmp_path / "output"
    args = SimpleNamespace(build=build, lock=lock_path, reaper=reaper,
                           reference_directory=reference, model=tmp_path / "model",
                           output_root=output)
    with pytest.raises(acceptance.AcceptanceFailure, match="stark_native_acceptance_failed"):
        acceptance.validate_build(args)
    assert not (output / "DEPENDENCY_ACCEPTANCE.json").exists()


def test_altered_validation_tool_is_rejected_before_native_process(tmp_path, monkeypatch):
    build = tmp_path / "build"
    tool = build / "source/scripts/validate_scoring_rename.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# altered synthetic validator\n")
    lock = {"validation_tools_sha256": {"scripts/validate_scoring_rename.py": "0" * 64}}
    monkeypatch.setattr(acceptance.dependency, "load_lock", lambda _: lock)
    monkeypatch.setattr(acceptance.dependency, "verify_build", lambda *_: {"source": {"development_only": True}})
    monkeypatch.setattr(acceptance.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("native launch forbidden"))
    args = SimpleNamespace(build=build, lock=tmp_path / "lock.json")
    with pytest.raises(acceptance.AcceptanceFailure, match="acceptance_tool_mismatch"):
        acceptance.validate_build(args)


@pytest.mark.parametrize("development", [False, True])
def test_valid_build_publishes_bound_private_receipt_only_after_full_report_checks(
        native_report, tmp_path, monkeypatch, development):
    directory, worker, probe = native_report
    build = tmp_path / "build"
    (build / "bin").mkdir(parents=True)
    selected_worker = build / "bin/stark-score"
    selected_probe = build / "bin/stark-score-digest"
    selected_worker.write_bytes(worker.read_bytes())
    selected_probe.write_bytes(probe.read_bytes())
    config = json.loads((directory / "configuration.json").read_text())
    config.update(worker=str(selected_worker), probe=str(selected_probe))
    config["files"] = {str(selected_worker): digest(selected_worker), str(selected_probe): digest(selected_probe)}
    private_write(directory / "configuration.json", config)
    reseal(directory)
    stash = directory.with_name("prepared-native-report")
    directory.rename(stash)
    tool = build / "source/scripts/validate_scoring_rename.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# synthetic pinned validator\n")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text("{}\n")
    for name in ("DEPENDENCY_LOCK.json", "BUILD_RECEIPT.json"):
        (build / name).write_text("{}\n")
    reaper = tmp_path / "reaper"
    reaper.write_bytes(b"synthetic reaper")
    reference = tmp_path / "reference"
    reference.mkdir()
    for name in ("REFERENCE.json", "MANIFEST.json"):
        private_write(reference / name, {"synthetic_scalar_reference": True})
    config = json.loads((stash / "configuration.json").read_text())
    config.update(reference_directory=str(reference), model=str(tmp_path / "model"))
    config["files"][str(reaper)] = digest(reaper)
    private_write(stash / "configuration.json", config)
    reseal(stash)
    lock = {"validation_tools_sha256": {"scripts/validate_scoring_rename.py": digest(tool)}}
    monkeypatch.setattr(acceptance.dependency, "load_lock", lambda _: lock)
    receipt = {"source": {"development_only": development, "revision": "c" * 40,
                          "tree": "d" * 40, "dirty": development}}
    verifications = []

    def verify(*_):
        verifications.append(True)
        return receipt.copy()

    def run(_command, **kwargs):
        assert kwargs["stdout"] == acceptance.subprocess.DEVNULL
        assert kwargs["stderr"] == acceptance.subprocess.DEVNULL
        assert "PYTHONPATH" not in kwargs["env"]
        stash.rename(directory)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(acceptance.dependency, "verify_build", verify)
    monkeypatch.setattr(acceptance.subprocess, "run", run)
    args = SimpleNamespace(build=build, lock=lock_path, reaper=reaper,
                           reference_directory=reference, model=tmp_path / "model",
                           output_root=directory)
    result = acceptance.validate_build(args)
    assert len(verifications) == 2
    assert result["numerical_acceptance_passed"] is True
    assert result["development_only"] is development
    saved = directory / "DEPENDENCY_ACCEPTANCE.json"
    assert json.loads(saved.read_text()) == result
    assert saved.stat().st_mode & 0o077 == 0
