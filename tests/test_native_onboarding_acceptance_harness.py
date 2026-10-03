"""The installed acceptance harness must join children and preserve lost ownership."""
import importlib
from pathlib import Path
import shutil
import sqlite3

import pytest


@pytest.fixture
def harness_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_native_onboarding")


@pytest.mark.asyncio
async def test_timed_out_child_is_reaped_before_workspace_cleanup(harness_module):
    module, report = harness_module, {}
    with module.isolated_workspace(report) as (root, lease):
        harness = module.Harness(root, root, root, lease)
        with pytest.raises(module.AcceptanceError, match="acceptance_child_timeout"):
            await harness.run(["/bin/sleep", "30"], {"PATH": "/usr/bin:/bin"}, root, timeout=0.01)
        assert not harness.children
        await harness.cleanup()
    assert not root.exists() and "isolated_state_retained" not in report


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["installed", "preparation"])
async def test_shutdown_retains_owned_state_when_settlement_is_unproved(harness_module, ledger):
    module, report = harness_module, {}
    root = None
    try:
        with module.isolated_workspace(report) as (root, lease):
            state = root / "state"
            state.mkdir(mode=0o700)
            harness = module.Harness(root, root, root, lease)
            if ledger == "installed":
                directory = state / "slots" / "synthetic"
                directory.mkdir(parents=True, mode=0o700)
                evidence = directory / "ownership.json"
                module.write_private_json(evidence, {"synthetic": {}})
                code = "installed_ownership_retained"
            else:
                evidence = state / "imports.sqlite"
                connection = sqlite3.connect(evidence)
                try:
                    connection.execute("CREATE TABLE preparation_ownership (job_id TEXT)")
                    connection.execute("INSERT INTO preparation_ownership VALUES ('synthetic')")
                    connection.commit()
                finally:
                    connection.close()
                evidence.chmod(0o600)
                code = "preparation_ownership_retained"
            before = module.digest(evidence)
            child = harness.spawn(["/usr/bin/true"], {"PATH": "/usr/bin:/bin"}, root)
            with pytest.raises(module.AcceptanceError, match=code):
                await harness.shutdown(child, state)
            assert child.returncode is not None and not harness.children and not lease["safe_to_remove"]
            await harness.cleanup()
        assert report["isolated_state_retained"] and Path(report["retained_state_directory"]) == root
        assert evidence.is_file() and module.digest(evidence) == before
    finally:
        if root is not None:
            shutil.rmtree(root)
