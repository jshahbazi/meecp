"""Host proposal-admission races; these are not real generation acceptance."""
from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from mcpme.preparation_agent_coordinator import AgentCoordinator
from mcpme.preparation_contracts import PreparationError
from test_preparation_agent import backend, request_for, response, role_of, store


def test_unverified_cli_backend_is_not_selectable():
    with pytest.raises(ValidationError):
        request_for(provider={"backend": "codex-cli-v1", "model": "any-model", "credential_ref": "cli-session"})


async def test_late_adapter_response_after_cancellation_cannot_publish(store):
    adapter_started = asyncio.Event()
    adapter_settled = asyncio.Event()

    async def handler(http_request):
        role = role_of(http_request)
        if role == "adapter":
            adapter_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Simulate a response already in flight when cancellation wins.
                adapter_settled.set()
        return response(role)

    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(handler), lambda _: None)
    attempt = coordinator.create(request)
    run = asyncio.create_task(coordinator.run(attempt["attempt_id"], request, source))
    await asyncio.wait_for(adapter_started.wait(), 2)
    record = await coordinator.cancel(attempt["attempt_id"])
    assert run.done() and adapter_settled.is_set()
    assert record["state"] == "canceled" and record["local_io_settled"]
    assert store.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert len(record["calls"]) == 4 and record["calls"][-1]["usage"] is None


async def test_publication_winner_remains_completed_when_cancel_arrives_later(store):
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(lambda req: response(role_of(req))), lambda _: None)
    attempt = coordinator.create(request)
    result = await coordinator.run(attempt["attempt_id"], request, source)
    record = await coordinator.cancel(attempt["attempt_id"])
    assert record["state"] == "succeeded"
    assert store.proposal(attempt["attempt_id"], request, lambda _: None) == result


async def test_expired_creation_does_not_create_attempt_or_dispatch(store):
    calls = []
    request, _ = request_for(expires_at=int(time.time()) - 1)
    coordinator = AgentCoordinator(store, backend(lambda req: calls.append(req)), lambda _: None)
    with pytest.raises(PreparationError, match="authority_expired"):
        coordinator.create(request)
    assert not calls and store.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


async def test_revoked_authority_at_publication_does_not_persist_artifact(store):
    calls = 0
    def validate(_):
        nonlocal calls
        calls += 1
        # create, run, four pre/post request checks, then publication.
        if calls == 11:
            raise PreparationError("authority_revoked")
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(lambda req: response(role_of(req))), validate)
    attempt = coordinator.create(request)
    with pytest.raises(PreparationError, match="attempt_failed"):
        await coordinator.run(attempt["attempt_id"], request, source)
    assert store.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert store.get(attempt["attempt_id"])["state"] == "failed"
