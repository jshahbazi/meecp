"""Captured binary intake preserves original sources and exact package bytes."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meecp.imports import ImportError, ImportStore, _model_return
from meecp.preparation_artifacts import AcquisitionRequestV1, ArtifactStore
from test_meecp_imports import prepared_for, source_folder


def captured(tmp_path):
    source = tmp_path.resolve() / "binary-selections"
    source.mkdir()
    weights = source / "weights.onnx"
    notice = source / "LICENSE"
    weights.write_bytes(b"\x08\x09Synthetic model intake bytes")
    notice.write_text("Synthetic model license\n")
    request = AcquisitionRequestV1(entries=[
        {"name": "weights", "role": "model-weights", "path": str(weights), "format": "onnx", "licenses": ["notice"]},
        {"name": "notice", "role": "license", "path": str(notice), "format": "text"},
    ])
    artifacts = ArtifactStore(tmp_path.resolve() / "captured")
    collection = artifacts.capture(request, disk_ceiling_bytes=1024**2)
    weights.unlink()
    notice.unlink()
    return artifacts, collection


def test_captured_source_survives_original_removal_restart_export_and_revision(tmp_path):
    artifacts, collection = captured(tmp_path)
    root = tmp_path / "state"
    with ImportStore(root) as imports:
        with imports._transaction():
            draft = imports._admit_captured(collection, choices={"name": "Detector", "intent": "Expose image classification"})
        source = draft["source"]
        assert source["kind"] == "captured_artifact_collection"
        assert source["revision"] == "sha256:" + collection.digest
        assert "snapshot_path" not in source and "locator" not in source
        exported = imports.export(draft["draft_id"], 1)
        assert "binary-selections" not in json.dumps(exported)
        assert "captured artifact collection" in exported["agent_instructions"]
        package = prepared_for(tmp_path, draft)
        with pytest.raises(ImportError, match="captured_artifact_mismatch"):
            imports.attach(draft["draft_id"], 1, package)
        revised = imports.revise(draft["draft_id"], 1, name="Renamed detector")
        assert revised["revision"] == 2 and revised["source"] == source
    with ImportStore(root) as restarted:
        current = restarted.inspect(draft["draft_id"])
        assert current["source"] == source and current["name"] == "Renamed detector"
        assert artifacts.verify(collection.digest) == collection
        with artifacts.open_artifact(collection.digest, "weights") as descriptor:
            import os
            assert os.read(descriptor, 1024).startswith(b"\x08\x09")


def test_directory_supplement_preserves_folder_identity_and_invalidates_attachment(tmp_path):
    _, collection = captured(tmp_path)
    with ImportStore(tmp_path / "state") as imports:
        draft = imports.create(source_folder(tmp_path), name="Detector adapter", intent="Classify images")
        package = prepared_for(tmp_path, draft)
        attached = imports.attach(draft["draft_id"], 1, package)
        original = attached["source"].copy()
        with imports._transaction():
            selected = imports._admit_captured(collection, draft_id=draft["draft_id"], revision=attached["revision"])
        assert selected["source"] == original
        assert selected["revision"] == attached["revision"] + 1 and selected["attachment"] is None
        assert selected["captured_artifacts"]["digest"] == collection.digest
        with imports._transaction(), pytest.raises(ImportError, match="stale_import_revision"):
            imports._admit_captured(collection, draft_id=draft["draft_id"], revision=attached["revision"])
        assert imports.inspect(draft["draft_id"])["captured_artifacts"]["digest"] == collection.digest


def test_admission_requires_transaction_and_valid_choices_rollback(tmp_path):
    _, collection = captured(tmp_path)
    with ImportStore(tmp_path / "state") as imports:
        with pytest.raises(ImportError, match="capture_admission_required"):
            imports._admit_captured(collection, choices={"name": "Detector"})
        with pytest.raises(ImportError, match="invalid_start_policy"), imports._transaction():
            imports._admit_captured(collection, choices={"name": "Detector", "start": 1})
        assert imports.list() == []
        with imports._transaction():
            draft = imports._admit_captured(collection, choices={"name": "Detector", "requirements": {"memory_mb": 128}})
        assert draft["requirements"] == {"memory_mb": 128}


def test_returned_package_must_contain_every_captured_byte_and_license(tmp_path):
    _, collection = captured(tmp_path)
    draft = {"source": {"kind": "captured_artifact_collection", "collection": collection.model_dump(mode="json")}}
    artifacts = [SimpleNamespace(sha256=item.sha256, size=item.size) for item in collection.entries]
    _model_return(draft, SimpleNamespace(lock=SimpleNamespace(artifacts=artifacts)))
    for bad in (artifacts[1:], [SimpleNamespace(sha256=item.sha256, size=item.size + 1) for item in collection.entries]):
        with pytest.raises(ImportError, match="captured_artifact_mismatch"):
            _model_return(draft, SimpleNamespace(lock=SimpleNamespace(artifacts=bad)))
