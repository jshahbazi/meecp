"""Bounded simulation and actual compiled fixture admission, never a live provider."""
import importlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from test_recipe_native_management_acceptance import native_recipe_driver, private_json, file_identity


@pytest.fixture
def agent_harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_native_agent")


def test_fixture_factory_does_not_enable_other_families_or_live_dispatch(tmp_path, monkeypatch, agent_harness):
    from mcpme.supervisor import Supervisor

    observed = {}

    async def serve(state, reaper, parent, **factories):
        host = Supervisor(state, reaper, **factories)
        try:
            selected = next(f.component.reference.key for f in host.preparations.families.values()
                if f.component.reference.id == "agent-unlocked-python-adapter-v1")
            observed.update(enabled=host.preparation_runner.enabled,
                exact_family=host.preparation_runner.enabled_families == frozenset({selected}),
                resolution=host.resolution_runner.enabled, live=host.agent_preparations.live_enabled,
                simulation=host.agent_preparations.simulated_enabled,
                providers=list(host.agent_preparations.providers), jobs=host.preparations.list())
        finally:
            await host.close()

    monkeypatch.setattr("mcpme.supervisor.serve", serve)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(sys, "argv", ["fixture", str(root / "backend"), str(root / "backend"),
        str(tmp_path / "state"), str(tmp_path / "unused-reaper"), "0", str(root / "scripts/agent_acceptance_fixture.py")])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(logging.root.manager, "disable", logging.root.manager.disable)
    exec(agent_harness.ENABLED_HOST, {})
    assert observed == dict(enabled=True, exact_family=True, resolution=True, live=False,
        simulation=True, providers=[agent_harness.PROVIDER_ID], jobs=[])


@pytest.mark.parametrize("factor,requirement", [(2, False), (3, True)])
async def test_simulation_derives_all_role_outputs_from_encoded_review(monkeypatch, factor, requirement):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from agent_acceptance_fixture import request_only_unlocked_handler
    from mcpme.preparation_agent_guides import builtin_analysis_guides
    from test_preparation_agent import request_for, backend
    guide = next(g for g in builtin_analysis_guides() if g.component.id == "agent-unlocked-python-adapter-v1")
    code = f"def multiply(n):\n return n*{factor}\n"
    if requirement:
        code = f"import humanize\ndef multiply(n):\n return int(humanize.intcomma(n*{factor}).replace(',', ''))\n"
    request, captured = request_for(source={"arbitrary.py": code.encode()}, recipe={
        "id": "mcpme.agent-unlocked-python-adapter", "version": "1.0.0", "digest": "a" * 64},
        guide=guide.model_dump(), selected_metadata=[{"path": "LICENSE", "bytes": 7, "sha256": "b" * 64}])
    transport = backend(request_only_unlocked_handler)
    proposals = {}
    for role in guide.roles:
        encoded = transport.encode(request, role.role, request.selected_source(captured),
            [proposals[name] for name in role.depends_on])
        proposals[role.role] = (await transport.complete(request, role.role, encoded)).proposal
    assert json.loads(proposals["dependencies"].files[0].content) == {"requirements": ["humanize"] if requirement else []}
    assert json.loads(proposals["fixtures"].files[0].content) == {"arguments": {"n": 3}, "expected": {"value": 3 * factor}}
    assert "from arbitrary import multiply" in proposals["adapter"].files[1].content


@pytest.mark.parametrize("fault", ["unknown_field", "scope_extra", "scope_overlap", "scope_duplicate",
    "scope_escape", "scope_recipe", "wrong_nonce", "expired", "manager_replaced", "marker_hardlink",
    "fixture_mode", "claimed_nonce", "existing_output", "dangling_output"])
def test_compiled_agent_fixture_rejects_unowned_scope_before_claim(native_recipe_driver, tmp_path, fault):
    workspace = tmp_path.resolve() / ("mcpme-native-onboarding-" + str(uuid.uuid4()))
    root = workspace / "agent"
    state, home, temporary = (root / name for name in ("state", "home", "tmp"))
    for directory in (workspace, root, state, home, temporary):
        directory.mkdir(mode=0o700)
    manager = state / "manager-connection.json"
    private_json(manager, {"synthetic": True})
    nonce = str(uuid.uuid4())
    marker = state / ("native-agent-" + nonce + ".json")
    proof = {"schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 120,
        "manager_file": file_identity(manager)}
    output, fixture = root / "receipt.json", root / "case.json"
    scope = {"provider_id": "synthetic", "model": "synthetic", "recipe": {
        "id": "mcpme.agent-unlocked-python-adapter", "version": "1.0.0", "digest": "a" * 64},
        "selected_paths": ["worker.py"], "selected_metadata_paths": ["LICENSE"]}
    case = {"schema_version": 1, "state_dir": str(state), "output_path": str(output),
        "fixture_nonce": nonce, "draft_id": str(uuid.uuid4()), "agent_scope": scope, "answers_by_label": {}}
    if fault == "unknown_field": case["unknown"] = True
    elif fault == "scope_extra": scope["api_key"] = "synthetic"
    elif fault == "scope_overlap": scope["selected_metadata_paths"] = ["worker.py"]
    elif fault == "scope_duplicate": scope["selected_paths"] *= 2
    elif fault == "scope_escape": scope["selected_paths"] = ["../worker.py"]
    elif fault == "scope_recipe": scope["recipe"]["digest"] = "invalid"
    elif fault == "wrong_nonce": proof["fixture_nonce"] = str(uuid.uuid4())
    elif fault == "expired": proof["expires_at"] = int(time.time()) - 1
    private_json(marker, proof); private_json(fixture, case)
    claimed = state / ("native-agent-" + nonce + ".claimed")
    if fault == "manager_replaced":
        replacement = state / "replacement.json"; private_json(replacement, {"synthetic": True}); replacement.replace(manager)
    elif fault == "marker_hardlink": os.link(marker, state / "second-marker.json")
    elif fault == "fixture_mode": fixture.chmod(0o644)
    elif fault == "claimed_nonce": private_json(claimed, {"consumed": True})
    elif fault == "existing_output": private_json(output, {"existing": True})
    elif fault == "dangling_output": output.symlink_to(root / "absent")
    before = claimed.read_bytes() if claimed.exists() else None
    old_output = output.read_bytes() if output.is_file() else None
    result = subprocess.run([str(native_recipe_driver), "--agent-native-acceptance", str(fixture)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary)},
        capture_output=True, timeout=10)
    assert result.returncode == 1
    assert result.stdout == b"preparation_native_acceptance_failed\n" and not result.stderr
    assert (claimed.read_bytes() if claimed.exists() else None) == before
    assert (output.read_bytes() if output.is_file() else None) == old_output


async def test_agent_unsettled_ledger_retains_workspace(agent_harness, tmp_path):
    import sqlite3
    root = tmp_path.resolve(); state = root / "state"; state.mkdir(mode=0o700)
    directory = state / "agent-preparations"; directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "attempts.sqlite3")
    for table in ("agent_host_jobs", "attempts"):
        db.execute("CREATE TABLE " + table + " (data TEXT)")
        db.execute("INSERT INTO " + table + " VALUES (?)", (json.dumps({"local_io_settled": False}),))
    db.commit(); db.close()
    lease = {"safe_to_remove": True}
    harness = agent_harness.Harness(root, root, root, lease)
    child = harness.spawn(["/usr/bin/true"], {"PATH": "/usr/bin:/bin"}, root)
    with pytest.raises(agent_harness.common.AcceptanceError, match="agent_ownership_retained"):
        await harness.shutdown(child, state)
    assert not harness.children and lease["safe_to_remove"] is False
