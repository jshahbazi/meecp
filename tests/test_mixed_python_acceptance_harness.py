"""Acceptance inventory rejects additions and mutation before target imports."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_phase3_native_onboarding as harness


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    data = b"bounded trusted synthetic implementation\n"
    (root / "known.py").write_bytes(data)
    (root / "build.json").write_text(json.dumps({"source_commit": "a" * 40, "source_tree_dirty": False,
        "artifact_sha256": {"known.py": hashlib.sha256(data).hexdigest()}}))
    return root


@pytest.mark.parametrize("mutation", ["extra", "bytes", "link", "dirty"])
def test_complete_payload_rejects_mutation_without_importing_target(payload, mutation, monkeypatch):
    monkeypatch.setattr(harness, "_host_imports", lambda _: pytest.fail("target imported before inventory rejection"))
    if mutation == "extra":
        (payload / "unlisted.py").write_bytes(b"unlisted")
    elif mutation == "bytes":
        (payload / "known.py").write_bytes(b"changed")
    elif mutation == "link":
        (payload / "alias").symlink_to(payload / "known.py")
    else:
        path = payload / "build.json"
        value = json.loads(path.read_bytes())
        value["source_tree_dirty"] = True
        path.write_text(json.dumps(value))
    with pytest.raises(harness.AcceptanceError):
        harness.verify_host_payload(payload)


def test_exact_complete_payload_inventory_accepts_only_bound_regular_files(payload):
    checked = harness.verify_host_payload(payload)
    assert checked["source_commit"] == "a" * 40
    assert set(checked["artifact_sha256"]) == {"known.py"}
