"""Host restart retains historical agent inputs without registering old executors."""
import json
from pathlib import Path
import time
import uuid

import pytest

from mcpme.imports import ImportStore
from mcpme.preparation_admission import PublicPreparationAdmission
from mcpme.preparation_components import PreparationDescriptorV2
from mcpme.preparation_contracts import PreparationError
from mcpme.supervisor import Supervisor
from test_preparation_agent_adapter import TOOL


@pytest.mark.parametrize("stem", ["agent-python-adapter", "agent-unlocked-python-adapter"])
@pytest.mark.parametrize("version", ["1.0.0", "1.0.1"])
async def test_public_host_recovers_completed_historical_agent_job_without_old_executor(tmp_path, stem, version):
    state = tmp_path / "state"
    reaper = tmp_path / "unused-reaper"
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def double(n): return n * 2\n")
    first = Supervisor(state, reaper)
    try:
        suffix = "-v" + version.replace(".", "-")
        recipe = first.recipes.add(Path(__file__).resolve().parents[1] / "templates/builtin-recipes" / (stem + suffix))
        first.recipes.enable(recipe["recipe_id"], version)
        draft = first.imports.create(source, name="Historical app", intent="Double a value")
        keys = frozenset(key for key in first.preparations.families if key[0].startswith("agent-"))
    finally:
        await first.close()
    admission = PublicPreparationAdmission(keys, frozenset(frozenset({key}) for key in keys))
    step = recipe["manifest"]["steps"][0]
    descriptor = PreparationDescriptorV2.model_validate({"schema_version": 2,
        "recipe": {"id": recipe["recipe_id"], "version": version, "digest": recipe["digest"]},
        "package": {"id": "historical-app", "version": "1.0.0", "name": "Historical app"},
        "components": [{"component": {"id": step["component"], "version": step["version"], "digest": step["digest"]},
            "input": {"derivation_id": str(uuid.uuid4()), "derivation_digest": "a" * 64}}],
        "tools": [TOOL], "inputs": {}, "artifacts": [], "access": {}, "resources": {"memory_mb": 128},
        "lifecycle": {"max_request_bytes": 1024 * 1024},
        "validation": [{"id": "fixture", "tool": "double", "arguments": {"n": 3},
            "expected": {"value": 6}, "synthetic": True}]})
    identity = str(uuid.uuid4())
    job = {"job_id": identity, "managed": True, "state": "succeeded", "workspace": None,
        "ownership_settled": True, "reservation_retained": False, "error_code": None, "progress": None,
        "updated_at": time.time(), "binding": {"import_id": draft["draft_id"], "revision": 1,
            "source_revision": draft["source"]["revision"]}}
    # Durable rows model the completed job left by the previous host version.
    with ImportStore(state) as imports, imports._transaction():
        imports._connection.execute("INSERT INTO preparation_jobs VALUES (?,?,?,?)",
            (identity, str(uuid.uuid4()), "b" * 64, json.dumps(job)))
        imports._connection.execute("INSERT INTO preparation_job_inputs VALUES (?,?)",
            (identity, json.dumps({"descriptor": descriptor.model_dump(mode="json")})))
    restarted = Supervisor(state, reaper, public_admission=admission)
    try:
        assert restarted.preparation_runner.enabled_families == keys
        assert descriptor.components[0].component.key not in restarted.preparations.families
        with pytest.raises(PreparationError, match="unknown_preparation_component"):
            restarted.preparations.family_for(descriptor)
        await restarted.recover_preparations()
        saved = restarted.preparations.get(identity)
        assert saved["state"] == "succeeded" and saved["ownership_settled"] and not saved["reservation_retained"]
        assert restarted.recipes.inspect(recipe["recipe_id"], version)["state"] == "enabled"
        assert restarted.preparation_runner.tasks == {}
        assert restarted.imports.inspect(draft["draft_id"])["attachment"] is None
        raw = descriptor.model_dump(mode="json")
        raw["components"][0]["component"]["digest"] = "0" * 64
        with restarted.imports._transaction():
            restarted.preparations.db.execute("UPDATE preparation_job_inputs SET data=? WHERE job_id=?",
                (json.dumps({"descriptor": raw}), identity))
        with pytest.raises(PreparationError, match="unknown_preparation_component"):
            restarted.preparations.release_family_inputs(identity)
        with restarted.imports._transaction():
            restarted.preparations.db.execute("UPDATE preparation_job_inputs SET data=? WHERE job_id=?",
                (json.dumps({"descriptor": descriptor.model_dump(mode="json")}), identity))
            saved.update(ownership_settled=False, reservation_retained=True)
            restarted.preparations._save(saved)
        with pytest.raises(PreparationError, match="preparation_ownership_unsettled"):
            restarted.preparations.release_family_inputs(identity)
    finally:
        # Cleanup only this synthetic row even when testing the unfixed host.
        with restarted.imports._transaction():
            restarted.preparations.db.execute("DELETE FROM preparation_job_inputs WHERE job_id=?", (identity,))
            restarted.preparations.db.execute("DELETE FROM preparation_jobs WHERE job_id=?", (identity,))
        await restarted.close()
