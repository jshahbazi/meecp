"""Synthetic provider connection checks stay outside the admitted preparation adapter."""
import asyncio
import json
from pathlib import Path
import uuid

import httpx
import pytest

from mcpme.preparation_agent_providers import AgentProviderRegistry, AgentProviderError
from mcpme.preparation_provider_test import ProviderConnectionTester, ProviderTestError, provider_test_routes
from mcpme.grants import read_credential
from mcpme.supervisor import Supervisor, management_app
from test_preparation_agent_providers import choices, update, SYNTHETIC


def connection_registry(tmp_path, handler):
    registry = AgentProviderRegistry(tmp_path / "providers")
    tester = ProviderConnectionTester(registry, transport=httpx.MockTransport(handler))
    row = registry.create(choices(models=["first-model", "second-model"]))["provider"]
    payload = {"schema_version": 1, "expected_generation": row["generation"], "model": "second-model"}
    return registry, tester, row, payload


def connection_response():
    return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 8, "output_tokens": 1},
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]})


async def test_owner_connection_route_uses_only_saved_model_and_synthetic_input(tmp_path, caplog):
    from starlette.applications import Starlette
    from mcpme.policy import LocalPolicy
    from mcpme.preparation_agent_providers import agent_provider_routes
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer " + SYNTHETIC
        assert json.loads(request.content) == {"model": "second-model", "store": False, "background": False,
            "max_output_tokens": 64, "tools": [], "tool_choice": "none", "truncation": "disabled", "input": "Reply with OK."}
        return connection_response()
    registry, tester, row, payload = connection_registry(tmp_path, handler)
    before = registry.path.read_bytes()
    policy = LocalPolicy(Starlette(), "owner", 19384,
        authenticate=lambda _: {"owner": True, "grant_id": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider_test_routes(agent_provider_routes(policy, registry), registry, tester=tester)),
            base_url="http://127.0.0.1:19384") as client:
        result = await client.post("/v1/agent-providers/" + row["provider_id"] + "/test", json=payload)
    assert result.status_code == 200
    assert result.json() == {"api_version": 1, "schema_version": 1, "provider_id": row["provider_id"],
        "generation": row["generation"], "model": "second-model", "status": "connected"}
    assert len(seen) == 1 and registry.path.read_bytes() == before
    assert not registry.list()["live_execution_enabled"] and not tester._test_active
    assert SYNTHETIC not in result.text and "OK" not in result.text and not caplog.records


async def test_connection_route_preserves_owner_authentication_host_and_origin(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                base_url="http://127.0.0.1:19384") as client:
            target = "/v1/agent-providers/" + str(uuid.uuid4()) + "/test"
            for mode in ("read", "prepare"):
                grant = host.grants.issue({"mode": mode})
                token = read_credential(Path(grant["credential_file"]))["token"]
                response = await client.post(target, headers={"Authorization": "Bearer " + token}, json={})
                assert response.status_code == 403 and response.json()["error"]["code"] == "grant_scope"
            for headers in ({}, {"Authorization": "Bearer owner", "Host": "elsewhere.invalid"},
                    {"Authorization": "Bearer owner", "Origin": "https://elsewhere.invalid"}):
                assert (await client.post(target, headers=headers, json={})).status_code in {401, 403}
            result = await client.post(target, headers={"Authorization": "Bearer owner"}, json={})
            assert result.status_code == 400 and result.json()["error"]["code"] == "agent_provider_request_rejected"
        assert not host.agent_preparations.tasks and not host.preparations.list()
    finally:
        await host.close()


@pytest.mark.parametrize("change", [{"schema_version": True}, {"model": "unconfigured-model"},
    {"expected_generation": str(uuid.uuid4())}, {"prompt": SYNTHETIC}, {"credentials": {"api_key": SYNTHETIC}}])
async def test_connection_rejects_stale_or_unreviewed_inputs_before_dispatch(tmp_path, change):
    def forbidden(_):
        raise AssertionError("invalid test dispatched")
    registry, tester, row, payload = connection_registry(tmp_path, forbidden)
    with pytest.raises(AgentProviderError):
        await tester.test_connection(row["provider_id"], payload | change, owner_authorized=True)
    with pytest.raises(AgentProviderError, match="owner_required"):
        await tester.test_connection(row["provider_id"], payload)
    assert not tester._test_active


@pytest.mark.parametrize("mutation", ["edit", "rotate", "remove", "revoke"])
async def test_connection_discards_stale_success_and_bounds_concurrent_requests(tmp_path, mutation):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(_):
        entered.set(); await release.wait()
        return connection_response()
    registry, tester, row, payload = connection_registry(tmp_path, handler)
    pending = asyncio.create_task(tester.test_connection(row["provider_id"], payload, owner_authorized=True))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ProviderTestError, match="test_busy"):
            await tester.test_connection(row["provider_id"], payload, owner_authorized=True)
        if mutation == "edit":
            registry.update(row["provider_id"], update(row, name="Changed"))
        elif mutation == "rotate":
            registry.update(row["provider_id"], update(row, credentials={"api_key": "synthetic-rotated"}))
        elif mutation == "remove":
            registry.remove(row["provider_id"], {"schema_version": 1, "expected_generation": row["generation"]})
        else:
            registry.secrets.revoke(registry._read().providers[0].credential_references["api_key"])
        release.set()
        with pytest.raises(AgentProviderError, match="configuration_stale|unavailable"):
            await pending
        assert not tester._test_active
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("response,code", [
    (httpx.Response(401, text=SYNTHETIC), "agent_provider_authentication_failed"),
    (httpx.Response(403, text=SYNTHETIC), "agent_provider_model_unavailable"),
    (httpx.Response(429, text=SYNTHETIC), "agent_provider_rate_limited"),
    (httpx.Response(302, headers={"location": "https://unregistered.invalid"}), "agent_provider_test_failed"),
    (httpx.Response(200, text="invalid-json"), "agent_provider_test_failed"),
    (httpx.Response(200, content=b"x" * 16385), "agent_provider_test_failed"),
    (httpx.Response(200, json={"status": "incomplete"}), "agent_provider_test_incomplete"),
    (httpx.Response(200, json={"status": "completed", "usage": {}, "output": []}), "agent_provider_test_incomplete"),
])
async def test_connection_failures_are_bounded_sanitized_and_retryable(tmp_path, caplog, response, code):
    calls = []
    def handler(request):
        calls.append(request)
        return response
    registry, tester, row, payload = connection_registry(tmp_path, handler)
    with pytest.raises(ProviderTestError) as failure:
        await tester.test_connection(row["provider_id"], payload, owner_authorized=True)
    assert failure.value.code == code and SYNTHETIC not in str(failure.value)
    assert len(calls) == 1 and not tester._test_active and not caplog.records


async def test_connection_accepts_reasoning_model_reaching_the_small_test_budget(tmp_path):
    def handler(_):
        return httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 8, "output_tokens": 64}, "output": [{"type": "reasoning"}]})
    registry, tester, row, payload = connection_registry(tmp_path, handler)
    result = await tester.test_connection(row["provider_id"], payload, owner_authorized=True)
    assert result["status"] == "connected" and not tester._test_active


async def test_connection_deadline_and_cancellation_settle_local_io(tmp_path, monkeypatch):
    import mcpme.preparation_provider_test as backend
    monkeypatch.setattr(backend, "PROVIDER_TEST_SECONDS", 0.02)
    settled = asyncio.Event()
    async def handler(_):
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()
    registry, tester, row, payload = connection_registry(tmp_path, handler)
    with pytest.raises(ProviderTestError, match="test_timeout"):
        await tester.test_connection(row["provider_id"], payload, owner_authorized=True)
    assert settled.is_set() and not tester._test_active


async def test_connection_cancellation_settles_before_another_test(tmp_path):
    entered, settled = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            return connection_response()
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            settled.set()
    registry, tester, row, payload = connection_registry(tmp_path, handler)
    pending = asyncio.create_task(tester.test_connection(row["provider_id"], payload, owner_authorized=True))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert settled.is_set() and not tester._test_active
    assert (await tester.test_connection(row["provider_id"], payload, owner_authorized=True))["status"] == "connected"
