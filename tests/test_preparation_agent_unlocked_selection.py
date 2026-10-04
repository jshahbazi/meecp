"""Synthetic resolver receipts exercise real checked selection fences only."""
from pathlib import Path
import uuid

import pytest

from mcpme.preparation_agent_adapter_store import CODE_PATH
from mcpme.preparation_contracts import PreparationError, contract_digest
from mcpme.preparation_forms import PreparationForms
from mcpme.preparation_resolution import ResolutionRequestV1, result_from_files
from test_preparation_agent_unlocked import foundation, analyzed


async def checked_selection(foundation, tmp_path):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    host, job = await analyzed(foundation,tmp_path)
    try:
        stage = inputs.for_job(job["job_id"],"proposal")
        observed,candidate = resolutions.candidate(draft["draft_id"],1,stage["result_digest"])
        request = ResolutionRequestV1(schema_version=1,import_id=draft["draft_id"],revision=1,
            source_revision=draft["source"]["revision"],candidate_id=stage["result_digest"],candidate_kind="python-callable",
            candidate_digest=contract_digest("preparation-resolution-candidate-v1",candidate),
            policy={"schema_version":1,"operations":["resolve_dependencies"],"network":["pypi.org","files.pythonhosted.org"],
                "wall_seconds":30,"disk_bytes":1024**2,"memory_mb":128},
            runtime={"name":"private-python-toolchain","digest":"c"*64},
            producer={"id":"python-agent-resolution-v1","version":"1.0.0","digest":"f"*64})
        plan = resolutions.plan(request,candidate,expires_in=300)
        resolving = resolutions.authorize_start(plan["plan_id"],plan["plan_digest"],str(uuid.uuid4()))
        workspace = tmp_path/"synthetic-resolution"; workspace.mkdir(mode=0o700)
        directory = workspace/"derived"; directory.mkdir(mode=0o700)
        (directory/"pyproject.toml").write_bytes(resolutions.project(observed,candidate))
        (directory/"uv.lock").write_bytes(b"version=1\nrevision=3\n")
        result = result_from_files(request,directory,(),None,1)
        for path in directory.iterdir():
            path.chmod(0o400)
        resolutions.begin(resolving["job_id"],workspace)
        resolutions.publish(resolving["job_id"],result)
        forms = PreparationForms(preparations)
        context = forms.context(draft["draft_id"],1,resolution_digest=result.digest)
        descriptor,_ = forms.resolve(draft["draft_id"],1,candidate_id=context.form.candidate_id,
            recipe=context.form.recipe.model_dump(),form_digest=context.form.form_digest,resolution_digest=result.digest,answers={})
        checked = preparations.prepare_descriptor_selection(draft["draft_id"],1,descriptor,
            candidate_id=context.form.candidate_id,form_digest=context.form.form_digest,resolution_digest=result.digest)
        return checked,descriptor,inputs.for_job(job["job_id"],"completed"),result
    finally:
        await host.close()


@pytest.mark.parametrize("change",["wrapper","project","original","membership","resolver"])
async def test_commit_rejects_changed_exact_validated_baseline(foundation,tmp_path,change):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    checked,descriptor,item,result = await checked_selection(foundation,tmp_path)
    if change == "original":
        path = Path(draft["source"]["snapshot_path"])/"worker.py"
    elif change == "membership":
        path = inputs.root/item["id"]/"foreign"
    elif change == "resolver":
        path = Path(resolutions.get(item["request"]["resolution_job_id"])["workspace"])/"derived/uv.lock"
    else:
        path = inputs.root/item["id"]/(CODE_PATH if change == "wrapper" else "pyproject.toml")
    if change == "resolver":
        path.chmod(0o600)
    path.write_bytes((path.read_bytes() if path.exists() else b"")+b"# changed\n"); path.chmod(0o600)
    if change == "resolver":
        path.chmod(0o400)
    with pytest.raises(PreparationError):
        preparations.commit_descriptor_selection(checked,check_current=lambda:True)
    assert imports.inspect(draft["draft_id"])["revision"] == 1 and preparations.selected_descriptor(draft["draft_id"]) is None
    assert not imports.inspect(draft["draft_id"])["attachment"] and not preparations.list()


async def test_commit_uses_metadata_only_and_carries_exact_resolution(foundation,tmp_path,monkeypatch):
    imports, recipes, draft, inputs, resolutions, family, preparations, record = foundation
    checked,descriptor,item,result = await checked_selection(foundation,tmp_path)
    def forbidden(*args,**kwargs):
        raise AssertionError("full_byte_validation_in_selection_transaction")
    with monkeypatch.context() as patch:
        patch.setattr("mcpme.preparation_agent_unlocked_store.original_snapshot",forbidden)
        patch.setattr("mcpme.preparation_agent_unlocked_store.read_resolution_inputs",forbidden)
        committed = preparations.commit_descriptor_selection(checked,check_current=lambda:True)
    selected = preparations.cleanup_descriptor_selection(committed)
    assert selected["revision"] == 2 and selected["source"] == draft["source"] and not selected["attachment"]
    assert resolutions.resolve_result(draft["draft_id"],2,result.request.candidate_id,result.digest)[0] == result
    assert family.check_current(descriptor,selected) is True
