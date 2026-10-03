"""Owner-generated setup questions, durable selections and stale plan barriers."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from mcpme.grants import read_credential
from mcpme.preparation_contracts import PreparationError
from mcpme.recipe_sdk import scaffold_recipe
from mcpme.supervisor import Supervisor, management_app


def prepare_source(host, root, *, custom_recipe=True):
    source = root / "source"
    source.mkdir()
    marker = root / "unexpected-execution"
    (source / "worker.py").write_text("from pathlib import Path\n" + f"Path({str(marker)!r}).touch()\n"
        "def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n")
    (source / "LICENSE").write_text("Synthetic permission notice.\n")
    (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
    (source / "uv.lock").write_text("version=1\nrevision=3\n")
    if custom_recipe:
        scaffold_recipe(root / "recipe", "test.generated")
        record = host.recipes.add(root / "recipe")
        host.recipes.enable(record["recipe_id"], record["version"])
    return host.imports.create(source, name="Synthetic double", intent="Expose the selected transformation"), marker


def request_body(form, **values):
    return {key: form[key] for key in ("import_id", "revision", "candidate_id", "recipe", "form_digest")} | {
        "answers": {field["id"]: values[field["label"]] for field in form["fields"] if field["label"] in values}}


async def test_owner_questions_and_review_bind_exact_selection_without_execution(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    owner, port = "synthetic-owner-authorization", 19886
    try:
        draft, marker = prepare_source(host, tmp_path)
        grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
        scoped = read_credential(Path(grant["credential_file"]))["token"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + owner}) as client:
            query = {"import_id": draft["draft_id"], "revision": 1}
            for route in ("form", "plan-form"):
                response = await client.post("/v1/preparations/" + route, json=query,
                    headers={"Authorization": "Bearer " + scoped})
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            response = await client.post("/v1/preparations/form", json=query)
            assert response.status_code == 200
            form = response.json()
            body = request_body(form, **{"Expected test result": {"value": 2}, "Memory budget (MiB)": 192})
            reviewed = await client.post("/v1/preparations/plan-form", json=body)
            assert reviewed.status_code == 200
            review = reviewed.json()
            assert review["revision"] == review["draft"]["revision"] == 2
            assert review["source_revision"] == form["source_revision"]
            assert review["form_digest"] == form["form_digest"]
            assert review["execution_available"] is False
            assert review["plan_id"] is review["plan_digest"] is review["expires_at"] is None
            assert review["tools"][0]["name"] == "double" and review["resources"]["memory_mb"] == 192
            assert review["blockers"] == ["preparation_runner_unavailable", "independent_runtime_verifier_unavailable"]
            selected = host.preparations.selected_descriptor(draft["draft_id"])
            assert selected["descriptor_digest"] == review["descriptor_digest"] and selected["revision"] == 2
            assert host.preparations.plan(draft["draft_id"], 2, selected["descriptor"])["execution_available"] is False
            changed = json.loads(json.dumps(selected["descriptor"]))
            changed["resources"]["memory_mb"] = 256
            with pytest.raises(PreparationError, match="preparation_selection_mismatch"):
                host.preparations.plan(draft["draft_id"], 2, changed)
            stale = await client.post("/v1/preparations/plan-form", json=body)
            assert stale.status_code == 400 and stale.json()["error"]["code"] == "stale_import_revision"
            refreshed = (await client.post("/v1/preparations/form", json={**query, "revision": 2})).json()
            restored = {field["label"]: field["value"] for field in refreshed["fields"]}
            assert restored["Expected test result"] == {"value": 2} and restored["Memory budget (MiB)"] == 192
            identical = (await client.post("/v1/preparations/plan-form", json=request_body(refreshed))).json()
            assert identical["revision"] == 2
            assert not marker.exists() and host.preparations.list() == []
            assert not host.registry.list_slots() and not host.registry.list_operations()
            if os.environ.get("MCPME_FORM_FIXTURE"):
                Path(os.environ["MCPME_FORM_FIXTURE"]).write_text(json.dumps({"form": form, "review": review}))
    finally:
        await host.close()
    restarted = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        assert restarted.preparations.selected_descriptor(draft["draft_id"]) == selected
        assert restarted.preparations.plan(draft["draft_id"], 2, selected["descriptor"])["revision"] == 2
        refreshed = restarted.preparation_forms.form(draft["draft_id"], 2)
        value, _ = restarted.preparation_forms.resolve(**request_body(refreshed))
        changed = value.model_dump(exclude_none=True)
        changed["resources"]["memory_mb"] = 256
        new = restarted.preparations.select_descriptor(draft["draft_id"], 2, changed,
            candidate_id=refreshed["candidate_id"], form_digest=refreshed["form_digest"])
        assert new["revision"] == 3
        assert restarted.preparations.selected_descriptor(draft["draft_id"])["descriptor"]["resources"]["memory_mb"] == 256
        renamed = restarted.imports.revise(draft["draft_id"], 3, name="Renamed app", requirements={"memory_mb": 64})
        renamed_form = restarted.preparation_forms.form(draft["draft_id"], renamed["revision"])
        defaults = {field["label"]: field["value"] for field in renamed_form["fields"]}
        assert defaults["App name"] == "Renamed app" and defaults["Memory budget (MiB)"] == 64
        assert defaults["Expected test result"] == {"value": 2}
    finally:
        await restarted.close()


async def test_invalid_form_answers_and_policy_do_not_advance_revision(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft, marker = prepare_source(host, tmp_path)
        form = host.preparation_forms.form(draft["draft_id"], 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19886)),
                base_url="http://127.0.0.1:19886", headers={"Authorization": "Bearer owner"}) as client:
            valid = request_body(form, **{"Expected test result": {"value": 2}})
            for body, code in ((request_body(form), "preparation_form_answer_required"),
                    ({**valid, "answers": {"interface.module": "override"}}, "unknown_preparation_form_field"),
                    ({**valid, "form_digest": "f" * 64}, "stale_preparation_form"),
                    ({**valid, "execution_policy": {}}, "invalid_preparation_policy"),
                    ({**valid, "extra": True}, "invalid_preparation_request")):
                response = await client.post("/v1/preparations/plan-form", json=body)
                assert response.status_code == 400 and response.json()["error"]["code"] == code
            assert host.imports.inspect(draft["draft_id"])["revision"] == 1
            assert host.preparations.selected_descriptor(draft["draft_id"]) is None
            assert not marker.exists()
    finally:
        await host.close()


async def test_normal_cold_host_needs_no_user_recipe_and_preserves_disable(tmp_path):
    host = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        draft, marker = prepare_source(host, tmp_path, custom_recipe=False)
        form = host.preparation_forms.form(draft["draft_id"], 1)
        assert form["recipe"]["id"] == "mcpme.python-callable" and form["blockers"] == []
        value, _ = host.preparation_forms.resolve(**request_body(form, **{"Expected test result": {"value": 2}}))
        assert value.inputs == {} and not marker.exists()
        assert all(record["runnable"] is False for record in host.recipes.list())
        host.recipes.disable(form["recipe"]["id"], form["recipe"]["version"])
    finally:
        await host.close()
    restarted = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        assert restarted.builtin_recipe_metadata["recipes"][0]["ready"] is False
        assert restarted.recipes.list()[0]["state"] == "disabled"
        questions = restarted.preparation_forms.form(draft["draft_id"], 1)
        assert questions["recipe"] is None and "compatible_recipe_required" in questions["blockers"]
    finally:
        await restarted.close()
