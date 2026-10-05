"""Transport-contract simulations do not establish the real-provider A5 gate."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

import httpx
import pytest
from pydantic import ValidationError

from mcpme.preparation_agent_backend import OpenAIResponsesBackend
from mcpme.preparation_agent_contracts import AnalysisRequestV1, parse_task
from mcpme.preparation_agent_coordinator import AgentAttemptStore, AgentCoordinator
from mcpme.preparation_contracts import PreparationError


def request_for(source=None, **overrides):
    source = source or {"ledger.py": b"def net(credits, debits):\n    return sum(credits) - sum(debits)\n"}
    value = {"import_id": str(uuid.uuid4()), "revision": 1, "source_revision": "a" * 40,
             "authority_id": str(uuid.uuid4()), "expires_at": int(time.time()) + 300,
             "provider": {"backend": "openai-responses-v1", "model": "explicit-test-model",
                          "credential_ref": "test-key"},
             "selected_files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                                  "bytes": len(data)} for path, data in sorted(source.items())],
             "intent": "Expose the selected ledger callable and propose synthetic tests.",
             "permitted_tools": ["read-selected-source", "propose-files"], "limits": {}}
    value.update(overrides)
    return AnalysisRequestV1.model_validate(value), source


def output(role, **changes):
    value = {"role": role, "files": [{"path": f"proposed/{role}/candidate.json", "content": "{}"}],
             "unresolved": []}
    value.update(changes)
    return value


def response(role, **changes):
    value = {"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 40},
             "output": [{"type": "message", "role": "assistant", "content": [
                 {"type": "output_text", "text": json.dumps(output(role))}]}]}
    value.update(changes)
    return httpx.Response(200, json=value)


def role_of(request):
    value = json.loads(request.content)
    return json.loads(value["input"][0]["content"][0]["text"])["role"]


@pytest.fixture
def store(tmp_path):
    value = AgentAttemptStore(tmp_path / "private")
    yield value
    value.close()


def backend(handler):
    return OpenAIResponsesBackend(lambda _: "unit-test-secret", transport=httpx.MockTransport(handler))


async def test_parallel_contract_proposals_integrate_and_usage_is_durable(store, caplog):
    seen = []
    active = 0
    peak = 0
    async def handler(http_request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        payload = json.loads(http_request.content)
        material = json.loads(payload["input"][0]["content"][0]["text"])
        seen.append(material)
        assert str(http_request.url) == "https://api.openai.com/v1/responses"
        assert not payload["store"] and not payload["background"]
        assert payload["tools"] == [] and payload["tool_choice"] == "none"
        assert set(material["selected_source"]) == {"ledger.py"}
        await asyncio.sleep(0.01)
        active -= 1
        return response(material["role"])
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(handler), lambda _: None)
    attempt = coordinator.create(request)
    result = await coordinator.run(attempt["attempt_id"], request, source)
    assert peak == 3 and len(seen) == 4
    assert [item["role"] for item in seen[-1]["prior_proposals"]] == ["interface", "dependencies", "fixtures"]
    assert store.proposal(attempt["attempt_id"], request, lambda _: None) == result
    record = store.get(attempt["attempt_id"])
    assert record["state"] == "succeeded" and record["local_io_settled"]
    assert sum(call["usage"]["output_tokens"] for call in record["calls"]) == 160
    assert "unit-test-secret" not in json.dumps(record) and "def net" not in json.dumps(record)
    assert not caplog.records


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"revision": True}, {"unexpected": 1},
    {"permitted_tools": ["shell"]}, {"limits": {"max_parallel": 4}},
    {"limits": {"monetary_ceiling": 1}},
])
def test_contract_rejects_unbound_authority_and_unsupported_budgets(change):
    with pytest.raises((ValidationError, PreparationError)):
        request_for(**change)


def test_selected_bytes_are_exact_and_credential_like_source_is_rejected():
    request, source = request_for()
    with pytest.raises(PreparationError, match="scope_mismatch"):
        request.selected_source({**source, "private.txt": b"private"})
    with pytest.raises(PreparationError, match="identity_mismatch"):
        request.selected_source({"ledger.py": b"changed"})
    request, source = request_for({"config.py": b"sk-" + b"x" * 30})
    with pytest.raises(PreparationError, match="credentials_detected"):
        request.selected_source(source)


@pytest.mark.parametrize("path", ["../outside", "/outside", "proposed/../outside", "proposed//x", "proposed/./x"])
def test_proposed_paths_never_escape(path):
    with pytest.raises(PreparationError, match="proposal_invalid"):
        parse_task(json.dumps(output("adapter", files=[{"path": path, "content": "x"}])).encode(), "adapter")


async def test_cancel_settles_all_local_io_and_keeps_uncertain_usage(store):
    entered = asyncio.Event()
    active = 0
    settled = 0
    async def handler(http_request):
        nonlocal active, settled
        active += 1
        if active == 3:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled += 1
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(handler), lambda _: None)
    attempt = coordinator.create(request)
    task = asyncio.create_task(coordinator.run(attempt["attempt_id"], request, source))
    await asyncio.wait_for(entered.wait(), 2)
    record = await coordinator.cancel(attempt["attempt_id"])
    assert task.cancelled() and settled == 3
    assert record["state"] == "canceled" and record["local_io_settled"]
    assert all(call["usage"] is None for call in record["calls"])
    assert sum(call["reserved_output_tokens"] for call in record["calls"]) == 3 * 4096
    with pytest.raises(PreparationError, match="unavailable"):
        store.proposal(attempt["attempt_id"], request, lambda _: None)


async def test_stale_revision_after_provider_reply_cannot_publish(store):
    current = True
    def validate(_):
        if not current:
            raise PreparationError("stale_import_revision")
    async def handler(http_request):
        nonlocal current
        if role_of(http_request) == "adapter":
            current = False
        return response(role_of(http_request))
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(handler), validate)
    attempt = coordinator.create(request)
    with pytest.raises(PreparationError, match="attempt_failed"):
        await coordinator.run(attempt["attempt_id"], request, source)
    assert store.get(attempt["attempt_id"])["state"] == "failed"
    assert store.db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


async def test_usage_is_reserved_before_dispatch_and_transfer_limit_stops_calls(store):
    calls = []
    def handler(http_request):
        calls.append(http_request)
        return response(role_of(http_request))
    request, source = request_for(limits={"max_transfer_bytes": 1})
    coordinator = AgentCoordinator(store, backend(handler), lambda _: None)
    attempt = coordinator.create(request)
    with pytest.raises(PreparationError):
        await coordinator.run(attempt["attempt_id"], request, source)
    assert calls == [] and store.get(attempt["attempt_id"])["calls"] == []


async def test_duplicate_start_and_reused_authority_fail(store):
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(lambda req: response(role_of(req))), lambda _: None)
    attempt = coordinator.create(request)
    with pytest.raises(PreparationError, match="authority_consumed"):
        coordinator.create(request)
    await coordinator.run(attempt["attempt_id"], request, source)
    with pytest.raises(PreparationError, match="stale_attempt"):
        await coordinator.run(attempt["attempt_id"], request, source)


def test_restart_fences_old_generation_and_does_not_replay(tmp_path):
    request, _ = request_for()
    root = tmp_path / "private"
    first = AgentAttemptStore(root)
    attempt = first.create(request)
    attempt.update(state="running", local_io_settled=False)
    first.save(attempt)
    first.reserve_call(attempt["attempt_id"], attempt["generation"], request, "interface", 100)
    with pytest.raises(PreparationError, match="already_owned"):
        AgentAttemptStore(root)
    first.close()
    second = AgentAttemptStore(root)
    try:
        recovered = second.get(attempt["attempt_id"])
        assert recovered["state"] == "interrupted" and recovered["local_io_settled"]
        assert recovered["generation"] != attempt["generation"]
        assert recovered["calls"][0]["usage"] is None
        with pytest.raises(PreparationError, match="stale_attempt"):
            second.check(attempt["attempt_id"], attempt["generation"], request)
    finally:
        second.close()


@pytest.mark.parametrize("kind", ["redirect", "incomplete", "oversized", "tool", "usage", "refusal", "malformed"])
async def test_real_transport_rejects_bad_provider_contracts(kind):
    def handler(_):
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://outside.invalid"})
        if kind == "incomplete":
            return response("interface", status="incomplete")
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * (512 * 1024 + 1))
        if kind == "tool":
            return response("interface", output=[{"type": "function_call", "name": "shell"}])
        if kind == "usage":
            return response("interface", usage={"input_tokens": 1, "output_tokens": 10**8})
        if kind == "refusal":
            return response("interface", output=[{"type": "message", "role": "assistant",
                                                  "content": [{"type": "refusal"}]}])
        return httpx.Response(200, content=b"secret-invalid-json")
    request, source = request_for()
    client = backend(handler)
    with pytest.raises(PreparationError) as failure:
        await client.complete(request, "interface", client.encode(request, "interface", request.selected_source(source), []))
    assert "secret" not in str(failure.value)


async def test_provider_disconnect_and_deadline_settle_before_terminal(store):
    settled = []
    async def handler(http_request):
        try:
            await asyncio.sleep(10)
        finally:
            settled.append(role_of(http_request))
    request, source = request_for(limits={"wall_seconds": 1})
    coordinator = AgentCoordinator(store, backend(handler), lambda _: None)
    attempt = coordinator.create(request)
    with pytest.raises(PreparationError, match="deadline"):
        await coordinator.run(attempt["attempt_id"], request, source)
    record = store.get(attempt["attempt_id"])
    assert sorted(settled) == ["dependencies", "fixtures", "interface"]
    assert record["state"] == "failed" and record["local_io_settled"]


async def test_missing_credentials_is_sanitized_and_never_dispatches():
    request, _ = request_for()
    client = OpenAIResponsesBackend(lambda _: None)
    with pytest.raises(PreparationError, match="credentials_unavailable"):
        await client.complete(request, "interface", b"{}")


async def test_disconnect_cancels_siblings_and_cannot_leak_error_body(store):
    started = asyncio.Event()
    entered = 0
    settled = []
    async def handler(http_request):
        nonlocal entered
        entered += 1
        if entered == 3:
            started.set()
        try:
            await started.wait()
            if role_of(http_request) == "interface":
                raise httpx.ReadError("PRIVATE_PROVIDER_RESPONSE")
            await asyncio.Event().wait()
        finally:
            settled.append(role_of(http_request))
    request, source = request_for()
    coordinator = AgentCoordinator(store, backend(handler), lambda _: None)
    attempt = coordinator.create(request)
    with pytest.raises(PreparationError, match="^agent_attempt_failed$"):
        await coordinator.run(attempt["attempt_id"], request, source)
    record = store.get(attempt["attempt_id"])
    assert len(settled) == 3 and record["local_io_settled"]
    assert "PRIVATE_PROVIDER_RESPONSE" not in json.dumps(record)


def test_pending_attempt_bound_and_queued_cancellation(store):
    request, _ = request_for()
    attempts = [store.create(request_for()[0]) for _ in range(4)]
    with pytest.raises(PreparationError, match="pending_limit"):
        store.create(request)
    assert store.cancel(attempts[0]["attempt_id"])["state"] == "canceled"
    assert store.create(request)["state"] == "queued"


@pytest.mark.parametrize("filename", ["owner.lock", "attempts.sqlite3"])
def test_existing_state_files_must_be_private(tmp_path, filename):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = root / filename
    target.touch()
    target.chmod(0o644)
    with pytest.raises(PreparationError, match="state_file_invalid"):
        AgentAttemptStore(root)


async def test_completed_artifacts_survive_restart_and_read_revalidates(tmp_path):
    root = tmp_path / "state"
    first = AgentAttemptStore(root)
    request, source = request_for()
    coordinator = AgentCoordinator(first, backend(lambda req: response(role_of(req))), lambda _: None)
    attempt = coordinator.create(request)
    result = await coordinator.run(attempt["attempt_id"], request, source)
    first.close()
    second = AgentAttemptStore(root)
    try:
        assert second.proposal(attempt["attempt_id"], request, lambda _: None) == result
        def revoked(_):
            raise PreparationError("authority_revoked")
        with pytest.raises(PreparationError, match="revoked"):
            second.proposal(attempt["attempt_id"], request, revoked)
    finally:
        second.close()
