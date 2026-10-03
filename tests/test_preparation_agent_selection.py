"""Exact checked-object authority and joined off-loop candidate admission."""
import asyncio
from dataclasses import replace
import threading

import pytest

from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_contracts import PreparationError
from test_preparation_agent_host import foundation, provider, handler_for, planned_start, completed


def test_checked_selection_rejects_forgery_and_requires_exact_true_in_transaction(foundation):
    imports, _, store, draft, descriptor = foundation
    checked = store.prepare_descriptor_selection(draft["draft_id"], 1, descriptor, candidate_id="a" * 64, form_digest="b" * 64)
    with pytest.raises(PreparationError, match="checked_descriptor_selection_required"):
        store.commit_descriptor_selection(replace(checked), check_current=lambda: True)
    called = []
    def denied():
        called.append(imports._connection.in_transaction)
        return 1
    with pytest.raises(PreparationError, match="authority_denied"):
        store.commit_descriptor_selection(checked, check_current=denied)
    assert called == [True] and imports.inspect(draft["draft_id"])["revision"] == 1
    committed = store.commit_descriptor_selection(checked, check_current=lambda: True)
    with pytest.raises(PreparationError, match="checked_descriptor_selection_required"):
        store.commit_descriptor_selection(checked, check_current=lambda: True)
    with pytest.raises(PreparationError, match="committed_descriptor_selection_required"):
        store.cleanup_descriptor_selection(replace(committed, cleanup_path="/outside"))
    assert store.cleanup_descriptor_selection(committed)["revision"] == 2


def test_metadata_commit_performs_no_source_or_recipe_hashing(foundation, monkeypatch):
    imports, recipes, store, draft, descriptor = foundation
    checked = store.prepare_descriptor_selection(draft["draft_id"], 1, descriptor, candidate_id="a" * 64, form_digest="b" * 64)
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected_static_work")
    monkeypatch.setattr(store, "_bound_descriptor", forbidden)
    monkeypatch.setattr(recipes, "resolve", forbidden)
    committed = store.commit_descriptor_selection(checked, check_current=lambda: imports._connection.in_transaction is True)
    assert store.cleanup_descriptor_selection(committed)["revision"] == 2


@pytest.mark.parametrize("action", ["cancel", "revision", "recipe", "caller_cancel"])
async def test_adoption_hash_worker_is_responsive_and_joined_before_denied_commit(foundation, tmp_path, monkeypatch, action):
    imports, recipes, store, draft, descriptor = foundation
    agent = AgentPreparationHost(store, tmp_path / "agent", providers=(provider(handler_for(descriptor)),), simulated_enabled=True)
    entered, release = threading.Event(), threading.Event()
    original = store.prepare_descriptor_selection
    try:
        _, _, job = await planned_start(agent, draft, descriptor)
        job = await completed(agent, job)
        def held(*args, **kwargs):
            checked = original(*args, **kwargs)
            entered.set(); release.wait(3)
            return checked
        monkeypatch.setattr(store, "prepare_descriptor_selection", held)
        adoption = asyncio.create_task(agent.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]}))
        assert await asyncio.to_thread(entered.wait, 2)
        # An actual independent event-loop turn and metadata status read finish
        # while the static worker is held. No static proof runs in get/options.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: True), 0.5)
        assert agent.get(job["job_id"])["state"] == "adopting"
        cancel = None
        if action == "cancel":
            cancel = asyncio.create_task(agent.cancel(job["job_id"]))
        elif action == "caller_cancel":
            adoption.cancel(); adoption.cancel()
        elif action == "revision":
            imports.revise(draft["draft_id"], 1, intent="Changed intent")
        else:
            recipes.disable(descriptor["recipe"]["id"], descriptor["recipe"]["version"])
        await asyncio.sleep(0.02)
        assert not adoption.done() and not agent.get(job["job_id"])["local_io_settled"]
        release.set()
        result = await asyncio.gather(adoption, return_exceptions=True)
        assert isinstance(result[0], BaseException)
        if cancel is not None:
            assert (await cancel)["state"] == "canceled"
        assert imports.inspect(draft["draft_id"])["revision"] == (2 if action == "revision" else 1)
        assert store.selected_descriptor(draft["draft_id"]) is None and store.list() == []
        assert not agent.tasks and agent.get(job["job_id"])["local_io_settled"]
    finally:
        release.set(); await agent.close()


async def test_options_does_not_restore_a_job_from_another_revision(foundation, tmp_path):
    imports, _, store, draft, descriptor = foundation
    agent = AgentPreparationHost(store, tmp_path / "agent", providers=(provider(handler_for(descriptor)),), simulated_enabled=True)
    try:
        _, _, job = await planned_start(agent, draft, descriptor)
        await completed(agent, job)
        assert agent.options(draft["draft_id"], 1)["latest_job"]["job_id"] == job["job_id"]
        changed = imports.revise(draft["draft_id"], 1, intent="Changed intent")
        assert agent.options(draft["draft_id"], changed["revision"])["latest_job"] is None
        assert agent.get(job["job_id"])["candidate"] is None
    finally:
        await agent.close()


async def test_actual_attachment_after_analysis_is_preserved_and_blocks_adoption(foundation, tmp_path):
    from pathlib import Path
    from mcpme.contracts import validate_package
    from test_mcpme_imports import prepared_for
    imports, _, store, draft, descriptor = foundation
    agent = AgentPreparationHost(store, tmp_path / "agent", providers=(provider(handler_for(descriptor)),), simulated_enabled=True)
    try:
        _, _, job = await planned_start(agent, draft, descriptor)
        job = await completed(agent, job)
        attached = imports.attach(draft["draft_id"], 1, prepared_for(tmp_path, draft))
        original = attached["attachment"]
        with pytest.raises(PreparationError, match="agent_unprepared_directory_required"):
            await agent.adopt({"action": "adopt-candidate", "job_id": job["job_id"], "proposal_digest": job["proposal_digest"]})
        assert imports.inspect(draft["draft_id"])["attachment"] == original
        assert validate_package(Path(original["path"])).digest == original["digest"]
        assert imports.inspect(draft["draft_id"])["revision"] == 1
        assert store.selected_descriptor(draft["draft_id"]) is None and store.list() == []
        assert agent.get(job["job_id"])["candidate"] is None
        with pytest.raises(PreparationError, match="agent_unprepared_directory_required"):
            agent.options(draft["draft_id"], 1)
    finally:
        await agent.close()


async def test_repeated_concurrent_shutdown_joins_held_snapshot_before_store_close(foundation, tmp_path, monkeypatch):
    _, _, store, draft, descriptor = foundation
    agent = AgentPreparationHost(store, tmp_path / "agent", providers=(provider(handler_for(descriptor)),), simulated_enabled=True)
    entered, release = threading.Event(), threading.Event()
    original = agent._selected_bytes
    try:
        plan = await agent.plan({"import_id": draft["draft_id"], "revision": 1, "recipe": descriptor["recipe"],
            "provider_id": "configured-openai", "model": "explicit-test-model", "selected_paths": ["validator.py"], "limits": {}})
        def held(*args):
            entered.set(); release.wait(3)
            return original(*args)
        monkeypatch.setattr(agent, "_selected_bytes", held)
        import uuid
        await agent.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})
        assert await asyncio.to_thread(entered.wait, 2)
        first, second = asyncio.create_task(agent.close()), asyncio.create_task(agent.close())
        await asyncio.sleep(0.02); first.cancel(); first.cancel()
        assert not first.done() and not second.done()
        assert agent.db.execute("SELECT COUNT(*) FROM agent_host_jobs").fetchone()[0] == 1
        release.set()
        await asyncio.gather(first, second)
        assert agent._close_task.done() and not agent.tasks and not agent.children
        await agent.close()
    finally:
        release.set(); await agent.close()
