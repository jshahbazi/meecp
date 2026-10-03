"""Relocation verification keeps ownership evidence and bounds cleanup errors."""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def harness_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_onnx")


@pytest.mark.asyncio
async def test_independent_probe_cleanup_retains_unsettled_owner_evidence(tmp_path, monkeypatch, harness_module):
    from mcpme.processes import OwnershipLost
    workspace = tmp_path / "probe"
    workspace.mkdir()
    class Owner:
        instances = {"owned": object()}
        def __init__(self, reaper):
            pass
        async def close(self):
            raise OwnershipLost()
    async def verify(*args, **kwargs):
        assert isinstance(kwargs["owner"], Owner)
        raise OwnershipLost()
    monkeypatch.setattr("mcpme.processes.ProcessOwner", Owner)
    monkeypatch.setattr("mcpme.preparation_verifier.verify_prepared_package", verify)
    with pytest.raises(OwnershipLost):
        await harness_module.relocation_probe(tmp_path / "package", object(), tmp_path / "reaper", workspace)
    assert (tmp_path / "ownership-unsettled").is_file()


def test_failed_acceptance_retains_workspace_without_removing_evidence(tmp_path, monkeypatch, capsys, harness_module):
    from mcpme.processes import OwnershipLost
    base = tmp_path / "owned-proof"
    base.mkdir()
    async def verify(*args):
        (base / "receipt").write_text("Synthetic retained ownership receipt.\n")
        raise OwnershipLost()
    monkeypatch.setattr(harness_module, "verify", verify)
    monkeypatch.setattr(harness_module.tempfile, "mkdtemp", lambda **kw: str(base))
    monkeypatch.setattr("sys.argv", ["verify_phase3_onnx.py", "--host", str(tmp_path / "host"),
                                    "--model", str(tmp_path / "model")])
    assert harness_module.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is False and result["failure_artifacts_retained"]
    assert Path(result["private_artifact_directory"]) == base and (base / "receipt").is_file()


def test_workspace_cleanup_failure_emits_fixed_diagnostic(tmp_path, monkeypatch, capsys, harness_module):
    base = tmp_path / "cleanup-proof"
    base.mkdir()
    async def verify(*args):
        return {"verified": True}
    def fail_cleanup(path):
        raise OSError("Synthetic diagnostic must remain private")
    monkeypatch.setattr(harness_module, "verify", verify)
    monkeypatch.setattr(harness_module.shutil, "rmtree", fail_cleanup)
    monkeypatch.setattr(harness_module.tempfile, "mkdtemp", lambda **kw: str(base))
    monkeypatch.setattr("sys.argv", ["verify_phase3_onnx.py", "--host", str(tmp_path / "host"),
                                    "--model", str(tmp_path / "model")])
    assert harness_module.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"verified": False, "error": "phase3_onnx_cleanup_failed", "stage": "closing",
                      "exception_type": "OSError", "private_artifact_directory": str(base)}
    assert base.exists()
