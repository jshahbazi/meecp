"""Exercise the additive protocol gate with private sealed files; no model work."""
import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_jev_sol_readout as audit


def put(path, value):
    path.write_bytes(audit.encoded(value))
    path.chmod(0o600)


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    public = tmp_path / "experiments/uzu-rust/jev-scoring"
    public.mkdir(parents=True)
    monkeypatch.setattr(audit, "PUBLIC", public)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    sources = [scripts / "audit_jev_sol_retry.py", scripts / "benchmark_jev_sol_retry.py",
               public / "SOL_BF16_PROTOCOL.md", public / "SOL_BF16_RETRY_PROTOCOL.md"]
    for path in sources:
        path.write_text("immutable fixture source\n")
    directory = tmp_path / "test-artifacts/retry"
    directory.mkdir(parents=True, mode=0o700)
    put(directory / "dataset.json", {"cases": "private fixture"})
    for i in range(1, 13):
        put(directory / f"block-{i:02d}.json", {"fixture_block": i})
    config = {"schema_version": 1, "mode": audit.MODE, "llama_n_probs": 40,
        "temperature": 1.22, "context_tokens": 8192, "uzu_probabilities": "native-reported",
        "fresh_then_identical_repeat": True, "candidate_receipt": {"variant": "combined"},
        "control_receipt": {"variant": "all"},
        "historical_runtime_binding": {"profile": {"llama_n_probs": 40, "temperature": 1.22,
                                                    "context_tokens": 8192, "llama_reference": "bf16"}},
        "files": {str(path): audit.file_hash(path) for path in sources}}
    args = SimpleNamespace(output=directory, source_report=public / "TIMING.json",
                           independent_audit=public / "AUDIT.json")

    def bind():
        put(directory / "configuration.json", config)
        digest = audit.sha(audit.encoded(config))
        pre = {"configuration_sha256": digest, "dataset_sha256": audit.file_hash(directory / "dataset.json")}
        post = {**pre, "ownership_settled": True,
                "blocks": {p.name: audit.file_hash(p) for p in sorted(directory.glob("block-*.json"))}}
        put(directory / "pre-run-seal.json", pre)
        put(directory / "post-run-seal.json", post)
        post_hash = audit.file_hash(directory / "post-run-seal.json")
        report = {"schema_version": 1, "status": "complete", "mode": audit.MODE, "configuration_sha256": digest,
            "post_run_seal_sha256": post_hash, "candidate_variant": "combined",
            "candidate_receipt_sha256": audit.sha(audit.encoded(config["candidate_receipt"])),
            "uzu_outputs_exactly_equal_to_reference": True, "uzu_quality_exactly_equal_to_reference": True,
            "measured_responses": 5832, "checked_warmups": 36}
        put(args.source_report, report)
        previous = {"schema_version": 1, "status": "passed", "source_report_sha256": audit.file_hash(args.source_report),
            "audit_source_sha256": audit.file_hash(sources[0]), "configuration_sha256": digest,
            "post_run_seal_sha256": post_hash, "whole_vocabulary_acceptance_preserved": True,
            "public_scalar_groups_exactly_reproduced": True, "measured_responses": 5832,
            "checked_warmups": 36, "exact_native_uzu_responses": 3888, "exact_native_uzu_warmups": 24}
        put(args.independent_audit, previous)
    bind()
    return SimpleNamespace(args=args, config=config, bind=bind, sources=sources, public=public)


def test_bound_top_40_run_passes_and_exports_only_scalar_provenance(sealed):
    result = audit.audit(sealed.args)
    assert result["status"] == "passed"
    assert result["llama_n_probs"] == result["historical_llama_n_probs"] == 40
    assert result["sealed_blocks_verified"] == 12
    assert all(type(value) in (str, int) for value in result.values())
    assert "private fixture" not in json.dumps(result)
    assert result["source_report_sha256"] == audit.file_hash(sealed.args.source_report)
    assert result["independent_audit_sha256"] == audit.file_hash(sealed.args.independent_audit)


@pytest.mark.parametrize("where", ["config", "historical"])
@pytest.mark.parametrize("count", [256, 2048, True, 40.0])
def test_non_top_40_rejected_even_with_rebound_passing_previous_audit(sealed, where, count):
    target = sealed.config if where == "config" else sealed.config["historical_runtime_binding"]["profile"]
    target["llama_n_probs"] = count
    sealed.bind()
    assert audit.read(sealed.args.independent_audit)["status"] == "passed"
    with pytest.raises(audit.AuditError, match="top_40_required"):
        audit.audit(sealed.args)


@pytest.mark.parametrize("name,field,value,error", [
    ("config", "temperature", 1.0, "configuration_invalid"),
    ("config", "context_tokens", 4096, "configuration_invalid"),
    ("config", "uzu_probabilities", "recalculated", "configuration_invalid"),
    ("config", "fresh_then_identical_repeat", False, "configuration_invalid"),
    ("pre-run-seal.json", "configuration_sha256", "wrong", "seal_invalid"),
    ("post-run-seal.json", "ownership_settled", False, "seal_invalid"),
    ("post-run-seal.json", "blocks", {}, "blocks_incomplete"),
])
def test_configuration_and_completion_constraints(sealed, name, field, value, error):
    if name == "config":
        sealed.config[field] = value
        sealed.bind()
    else:
        path = sealed.args.output / name
        record = audit.read(path)
        record[field] = value
        put(path, record)
    with pytest.raises(audit.AuditError, match=error):
        audit.audit(sealed.args)


@pytest.mark.parametrize("source,field,value,error", [
    ("report", "status", "partial", "timing_report_invalid"),
    ("report", "configuration_sha256", "wrong", "timing_report_invalid"),
    ("report", "post_run_seal_sha256", "wrong", "timing_report_invalid"),
    ("report", "uzu_outputs_exactly_equal_to_reference", False, "timing_report_invalid"),
    ("audit", "status", "failed", "independent_audit_invalid"),
    ("audit", "source_report_sha256", "wrong", "independent_audit_invalid"),
    ("audit", "audit_source_sha256", "wrong", "independent_audit_invalid"),
    ("audit", "configuration_sha256", "wrong", "independent_audit_invalid"),
    ("audit", "post_run_seal_sha256", "wrong", "independent_audit_invalid"),
    ("audit", "whole_vocabulary_acceptance_preserved", False, "independent_audit_invalid"),
    ("audit", "public_scalar_groups_exactly_reproduced", False, "independent_audit_invalid"),
    ("audit", "exact_native_uzu_responses", 1, "independent_audit_invalid"),
])
def test_report_and_independent_audit_bindings(sealed, source, field, value, error):
    path = sealed.args.source_report if source == "report" else sealed.args.independent_audit
    record = audit.read(path)
    record[field] = value
    put(path, record)
    with pytest.raises(audit.AuditError, match=error):
        audit.audit(sealed.args)


@pytest.mark.parametrize("name,error", [
    ("dataset.json", "seal_invalid"), ("block-01.json", "block_seal_invalid"),
    ("configuration.json", "seal_invalid"),
])
def test_private_record_tampering(sealed, name, error):
    path = sealed.args.output / name
    record = audit.read(path)
    record["tampered"] = True
    put(path, record)
    with pytest.raises(audit.AuditError, match=error):
        audit.audit(sealed.args)


def test_missing_block_and_changed_bound_source_rejected(sealed):
    path = sealed.args.output / "block-12.json"
    path.unlink()
    with pytest.raises(audit.AuditError, match="blocks_incomplete"):
        audit.audit(sealed.args)
    put(path, {"fixture_block": 12})
    sealed.sources[0].write_text("changed audit source\n")
    with pytest.raises(audit.AuditError, match="bound_source_changed"):
        audit.audit(sealed.args)


@pytest.mark.parametrize("name", ["configuration.json", "dataset.json", "block-01.json"])
def test_private_files_require_owner_only_permissions(sealed, name):
    (sealed.args.output / name).chmod(0o644)
    with pytest.raises(audit.AuditError):
        audit.audit(sealed.args)


def test_supplement_report_is_create_only_private_and_rejects_symlinks(sealed):
    destination = sealed.public / "SUPPLEMENT.json"
    result = audit.audit(sealed.args)
    audit.write_report(destination, result)
    assert destination.stat().st_mode & 0o077 == 0
    with pytest.raises(audit.AuditError, match="report_destination_invalid"):
        audit.write_report(destination, {"changed": True})
    assert audit.read(destination) == result
    linked = sealed.public / "LINKED.json"
    linked.symlink_to(destination)
    with pytest.raises(audit.AuditError, match="report_destination_invalid"):
        audit.write_report(linked, result)


def test_private_directory_and_symlinked_input_rejected(sealed):
    sealed.args.output.chmod(0o755)
    with pytest.raises(audit.AuditError, match="private_directory_invalid"):
        audit.audit(sealed.args)
    sealed.args.output.chmod(0o700)
    path = sealed.args.output / "configuration.json"
    backup = sealed.args.output / "saved-configuration.json"
    path.rename(backup)
    path.symlink_to(backup)
    with pytest.raises(audit.AuditError, match="file_invalid"):
        audit.audit(sealed.args)


def test_unchecked_report_tamper_still_breaks_full_audit_binding(sealed):
    record = audit.read(sealed.args.source_report)
    record["unrelated_scalar"] = 17
    put(sealed.args.source_report, record)
    with pytest.raises(audit.AuditError, match="independent_audit_invalid"):
        audit.audit(sealed.args)


def test_main_failure_emits_only_fixed_error_and_writes_no_report(sealed, monkeypatch, capsys):
    destination = sealed.public / "REJECTED.json"
    monkeypatch.setattr(sys, "argv", ["supplement", "--output", str(sealed.args.output),
        "--source-report", str(sealed.args.source_report),
        "--independent-audit", str(sealed.args.independent_audit), "--report", str(destination)])
    def fail(args):
        raise RuntimeError("private exception detail")
    monkeypatch.setattr(audit, "audit", fail)
    assert audit.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"stage": "failed", "error": "top_40_protocol_supplement_failed"}
    assert captured.err == "" and not destination.exists()


def test_supplement_imports_only_standard_library_and_has_fixed_failures():
    source = Path(audit.__file__).read_text()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            names.add(node.module.split(".")[0])
    assert names <= sys.stdlib_module_names
    assert '"error": "top_40_protocol_supplement_failed"' in source
    assert "traceback" not in source
