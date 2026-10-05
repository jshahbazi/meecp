"""Textgen acceptance harness retains ownership evidence and sanitizes cleanup."""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def harness_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_textgen_managed")


@pytest.mark.asyncio
async def test_relocation_probe_retains_unjoined_owner_evidence(tmp_path, monkeypatch, harness_module):
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
    monkeypatch.setattr("mcpme.preparation_textgen_verifier.verify_textgen_package", verify)
    with pytest.raises(OwnershipLost):
        await harness_module.relocation_probe(tmp_path / "package", object(), tmp_path / "reaper", workspace)
    assert (tmp_path / "ownership-unsettled").is_file()


def test_failed_acceptance_retains_private_artifacts(tmp_path, monkeypatch, capsys, harness_module):
    from mcpme.processes import OwnershipLost

    base = tmp_path / "owned-proof"
    base.mkdir()

    async def verify(*args, **kwargs):
        (base / "receipt").write_text("Synthetic retained ownership receipt.\n")
        raise OwnershipLost()

    monkeypatch.setattr(harness_module, "verify", verify)
    monkeypatch.setattr(harness_module.tempfile, "mkdtemp", lambda **kw: str(base))
    monkeypatch.setattr("sys.argv", ["verify_phase3_textgen_managed.py", "--host", str(tmp_path / "host"),
                                    "--model", str(tmp_path / "model"), "--source-mode", "folder"])
    assert harness_module.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["verified"] is False and result["failure_artifacts_retained"]
    assert Path(result["private_artifact_directory"]) == base and (base / "receipt").is_file()


def test_cleanup_failure_emits_fixed_diagnostic_without_exception_text(tmp_path, monkeypatch, capsys, harness_module):
    base = tmp_path / "cleanup-proof"
    base.mkdir()

    async def verify(*args, **kwargs):
        return {"verified": True}

    def fail_cleanup(path):
        raise OSError("Synthetic diagnostic must remain private")

    monkeypatch.setattr(harness_module, "verify", verify)
    monkeypatch.setattr(harness_module.shutil, "rmtree", fail_cleanup)
    monkeypatch.setattr(harness_module.tempfile, "mkdtemp", lambda **kw: str(base))
    monkeypatch.setattr("sys.argv", ["verify_phase3_textgen_managed.py", "--host", str(tmp_path / "host"),
                                    "--model", str(tmp_path / "model"), "--install-lifecycle"])
    assert harness_module.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"verified": False, "error": "phase3_textgen_cleanup_failed", "stage": "closing",
                      "exception_type": "OSError", "private_artifact_directory": str(base)}
    assert "Synthetic diagnostic" not in json.dumps(result)
    assert base.exists()
