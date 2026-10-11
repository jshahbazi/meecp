"""Safety checks for the isolated optimized scoring project projection."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/uzu-rust"))
import build_optimized_scoring as builder


@pytest.fixture
def synthetic_project(tmp_path, monkeypatch):
    scoring = tmp_path / "scoring"
    scoring.mkdir()
    manifest = scoring / "Cargo.toml"
    manifest.write_text(
        '[package]\nname = "synthetic"\n'
        '[[bin]]\nname = "worker"\npath = "../src/main.rs"\n'
        '[dependencies]\n'
        'uzu-engine = { path = "../../../runtime/uzu-rust/upstream/crates/uzu-engine" }\n'
        'hanashi = { path = "../../../runtime/uzu-rust/upstream/crates/legacy/hanashi" }\n'
        'shoji = { path = "../../../runtime/uzu-rust/upstream/crates/legacy/shoji" }\n'
    )
    lock = scoring / "Cargo.lock"
    lock.write_bytes(b"synthetic lock\x00bytes\n")
    experiment = tmp_path / "experiment"
    probe = experiment / "probes/score_digest.rs"
    probe.parent.mkdir(parents=True)
    probe.write_text("fn main() {}\n")
    upstream = tmp_path / "upstream"
    for path in ("crates/uzu-engine", "crates/legacy/hanashi", "crates/legacy/shoji"):
        (upstream / path).mkdir(parents=True)
    monkeypatch.setattr(builder.base, "SCORING", scoring)
    monkeypatch.setattr(builder, "EXPERIMENT", experiment)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    return tmp_path, manifest, lock, upstream, probe


def test_prepare_project_isolated_projection_preserves_inputs(synthetic_project):
    root, manifest, lock, upstream, probe = synthetic_project
    original_manifest = manifest.read_bytes()
    original_lock = lock.read_bytes()

    generated = builder.prepare_project(root / "project", upstream)

    assert manifest.read_bytes() == original_manifest
    assert lock.read_bytes() == original_lock
    assert (generated.parent / "Cargo.lock").read_bytes() == original_lock
    text = generated.read_text()
    assert str(builder.EXPERIMENT / "src/main.rs") in text
    for path in ("crates/uzu-engine", "crates/legacy/hanashi", "crates/legacy/shoji"):
        assert str(upstream / path) in text
        assert "../../../runtime/uzu-rust/upstream/" + path not in text
    assert str(probe) in text


def test_prepare_project_rejects_missing_expected_dependency_override(synthetic_project):
    root, manifest, _, upstream, _ = synthetic_project
    manifest.write_text(manifest.read_text().replace(
        'shoji = { path = "../../../runtime/uzu-rust/upstream/crates/legacy/shoji" }\n',
        ""))

    with pytest.raises(RuntimeError, match="optimization_build_identity_invalid"):
        builder.prepare_project(root / "project", upstream)


def test_prepare_project_rejects_missing_probe_source(synthetic_project):
    root, _, _, upstream, probe = synthetic_project
    probe.unlink()

    with pytest.raises(RuntimeError, match="optimization_build_identity_invalid"):
        builder.prepare_project(root / "project", upstream)


def test_variant_registry_keeps_baseline_separate_from_patched_variants():
    assert builder.VARIANTS["baseline"] == ()
    assert builder.VARIANTS["combined"] == ("topology", "state-init")
    assert builder.VARIANTS["baseline"] != builder.VARIANTS["combined"]
