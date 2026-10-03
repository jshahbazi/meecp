"""Capture/cleanup failures must retain inspectable state after joining children."""
import importlib
from pathlib import Path
import shutil
import sqlite3

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_native_onnx")


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", ["job", "reader", "reference", "gc"])
async def test_shutdown_joins_child_and_retains_capture_evidence(module, retained):
    report, root = {}, None
    try:
        with module.isolated_workspace(report) as (root, lease):
            state = root / "state"; state.mkdir(mode=0o700)
            path = state / "imports.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE preparation_ownership (job_id TEXT)")
            db.execute("CREATE TABLE artifact_captures (data TEXT)")
            db.execute("CREATE TABLE captured_artifact_readers (data TEXT)")
            db.execute("CREATE TABLE captured_artifact_references (kind TEXT)")
            db.execute("CREATE TABLE captured_artifact_gc (data TEXT)")
            if retained == "job":
                db.execute("INSERT INTO artifact_captures VALUES (?)", ('{"ownership_settled":false,"reservation_retained":true}',))
            elif retained == "reader":
                db.execute("INSERT INTO captured_artifact_readers VALUES ('{}')")
            elif retained == "reference":
                db.execute("INSERT INTO captured_artifact_references VALUES ('job')")
            else:
                db.execute("INSERT INTO captured_artifact_gc VALUES ('{}')")
            db.commit(); db.close(); path.chmod(0o600)
            before = module.common.digest(path)
            harness = module.Harness(root, root, root, lease, root)
            child = harness.spawn(["/usr/bin/true"], {"PATH": "/usr/bin:/bin"}, root)
            with pytest.raises(module.common.AcceptanceError, match="capture_ownership_retained"):
                await harness.shutdown(child, state)
            assert child.returncode is not None and not harness.children
            assert not lease["safe_to_remove"] and module.common.digest(path) == before
        assert report["isolated_state_retained"] and Path(report["retained_state_directory"]) == root
        assert path.exists()
    finally:
        if root is not None:
            shutil.rmtree(root)


def test_workspace_cleanup_error_preserves_bounded_location(module, monkeypatch):
    report, root = {}, None
    try:
        with pytest.raises(module.common.AcceptanceError, match="acceptance_workspace_cleanup_failed"):
            with module.isolated_workspace(report) as (root, _):
                monkeypatch.setattr(module.tempfile.TemporaryDirectory, "cleanup", lambda _: (_ for _ in ()).throw(OSError()))
        assert report["isolated_state_retained"] and Path(report["retained_state_directory"]) == root
        assert root.is_dir()
    finally:
        if root is not None:
            shutil.rmtree(root)
