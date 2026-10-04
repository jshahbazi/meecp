"""Fault-control authority survives cleanup even after driver children are joined."""
import importlib
from pathlib import Path
import shutil

import pytest


@pytest.fixture
def command_harness_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_native_command")


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["legacy", "reservation", "command", "malformed",
    "wrong_empty_version", "unproved_settled"])
async def test_control_state_is_retained_after_held_child_cleanup(command_harness_module, authority):
    module, report = command_harness_module, {}
    root = None
    try:
        with module.onboarding.isolated_workspace(report) as (root, lease):
            harness = module.CommandHarness(root, root, root, lease, fixture=root / "unused-fixture")
            state = root / "injected-control" / "state"
            slot = state / "slots" / "synthetic"
            slot.mkdir(parents=True, mode=0o700)
            harness.states.append(state)
            if authority == "legacy":
                evidence, value = slot / "ownership.json", {"synthetic": {}}
            elif authority == "reservation":
                evidence, value = slot / "command-reservations.json", {"version": 1, "entries": [{"synthetic": True}]}
            else:
                evidence = slot / "command-v3" / "command-v2.json"
                evidence.parent.mkdir(mode=0o700)
                if authority == "command":
                    value = {"version": 3, "entries": [{"state": "unknown"}]}
                elif authority == "wrong_empty_version":
                    value = {"version": 2, "entries": []}
                elif authority == "unproved_settled":
                    value = {"version": 3, "entries": [{"instance": "a" * 32, "nonce": "b" * 64,
                        "helper_digest": "c" * 64, "policy_digest": "d" * 64, "contract_digest": "e" * 64,
                        "units": 1, "state": "settled", "receipt_failed": False}]}
                else:
                    value = {"unexpected": True}
            module.onboarding.write_private_json(evidence, value)
            before = module.digest(evidence)
            child = harness.spawn(["/bin/sleep", "30"], {"PATH": "/usr/bin:/bin"}, root)
            await harness.cleanup()
            assert child.returncode is not None and not harness.children
            assert lease["safe_to_remove"] is False and module.digest(evidence) == before
        assert report["isolated_state_retained"] is True
        assert Path(report["retained_state_directory"]) == root and evidence.is_file()
    finally:
        # These fixtures contain no actual owner or durable process identity.
        if root is not None:
            shutil.rmtree(root)


@pytest.mark.asyncio
async def test_settled_control_and_timed_out_child_can_be_removed(command_harness_module):
    module, report = command_harness_module, {}
    with module.onboarding.isolated_workspace(report) as (root, lease):
        harness = module.CommandHarness(root, root, root, lease, fixture=root / "unused-fixture")
        state = root / "injected-control" / "state"
        slot = state / "slots" / "synthetic"
        slot.mkdir(parents=True, mode=0o700)
        (slot / "command-v3").mkdir(mode=0o700)
        harness.states.append(state)
        module.onboarding.write_private_json(slot / "ownership.json", {})
        module.onboarding.write_private_json(slot / "command-reservations.json", {"version": 1, "entries": []})
        module.onboarding.write_private_json(slot / "command-v3" / "command-v2.json",
            {"version": 3, "entries": []})
        with pytest.raises(module.onboarding.AcceptanceError, match="acceptance_child_timeout"):
            await harness.run(["/bin/sleep", "30"], {"PATH": "/usr/bin:/bin"}, root, timeout=0.01)
        await harness.cleanup()
        assert not harness.children and lease["safe_to_remove"] is True
    assert not root.exists() and "isolated_state_retained" not in report
