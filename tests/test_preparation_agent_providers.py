"""Synthetic credentials and isolated state only; no live provider I/O."""
import asyncio
import json
import os
from pathlib import Path
import uuid

import httpx
import pytest

from mcpme.grants import read_credential
from mcpme.preparation_agent_host import AgentPreparationHost
from mcpme.preparation_agent_providers import AgentProviderRegistry, AgentProviderError, OpenAIResponsesAdapter
from mcpme.preparation_contracts import PreparationError
from mcpme.supervisor import Supervisor, management_app
from test_preparation_agent_host import foundation, selection, completed, handler_for


SYNTHETIC = "synthetic-not-a-real-provider-credential"


def choices(**changes):
    value = {"schema_version": 1, "adapter_id": "openai-responses-v1", "name": "Synthetic account",
        "models": ["explicit-test-model"], "configuration": {}, "credentials": {"api_key": SYNTHETIC}}
    value.update(changes)
    return value


def update(record, **changes):
    value = {"schema_version": 1, "expected_generation": record["generation"], "name": record["name"],
        "models": record["models"], "configuration": record["configuration"], "credentials": {}}
    value.update(changes)
    return value


def test_retention_rotation_removal_and_metadata_privacy(tmp_path, caplog):
    registry = AgentProviderRegistry(tmp_path / "providers")
    row = registry.create(choices())["provider"]
    original = registry.configuration(row["provider_id"])
    assert original.backend._resolve(original.credential_ref) == SYNTHETIC
    assert row["credential_status"] == "configured" and not row["execution_available"]
    assert SYNTHETIC not in json.dumps(row) and "credential_references" not in row
    assert SYNTHETIC not in registry.path.read_text()
    assert os.stat(registry.root).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in registry.root.rglob("*") if path.is_file())
    reopened = AgentProviderRegistry(registry.root)
    assert reopened.list()["providers"] == [row]
    assert reopened.configuration(row["provider_id"]).digest == original.digest
    edited = reopened.update(row["provider_id"], update(row, name="Renamed"))["provider"]
    assert edited["generation"] != row["generation"] and edited["credential_generation"] == row["credential_generation"]
    with pytest.raises(AgentProviderError, match="configuration_stale"):
        original.backend._resolve(original.credential_ref)
    old_ref = reopened._read().providers[0].credential_references["api_key"]
    new_key = "synthetic-rotated-provider-credential"
    rotated = reopened.update(row["provider_id"], update(edited, credentials={"api_key": new_key}))["provider"]
    assert rotated["credential_generation"] != edited["credential_generation"]
    assert reopened.secrets.get(old_ref)["status"] == "revoked"
    config = reopened.configuration(row["provider_id"])
    assert config.backend._resolve(config.credential_ref) == new_key
    removed = reopened.remove(row["provider_id"], {"schema_version": 1, "expected_generation": rotated["generation"]})["provider"]
    assert removed["status"] == "removed" and removed["credential_status"] == "unavailable"
    assert not reopened.configurations()
    with pytest.raises(AgentProviderError, match="unavailable"):
        reopened.configuration(row["provider_id"])
    with pytest.raises(AgentProviderError, match="configuration_stale"):
        config.backend._resolve(config.credential_ref)
    assert new_key not in json.dumps(reopened.list())
    assert all("value" not in json.loads(path.read_bytes()) for path in reopened.secrets.records.iterdir())
    assert not caplog.records


@pytest.mark.parametrize("mutation", ["models", "rotate", "remove", "revoke"])
async def test_config_change_invalidates_review_and_old_credential_before_dispatch(foundation, tmp_path, mutation):
    _, _, preparations, draft, descriptor = foundation
    registry = AgentProviderRegistry(tmp_path / "providers")
    row = registry.create(choices())["provider"]
    host = AgentPreparationHost(preparations, tmp_path / "agent", provider_registry=registry)
    try:
        scope = selection(draft, descriptor); scope["provider_id"] = row["provider_id"]
        plan = await host.plan(scope)
        request = host._request(host._plan(plan["plan_id"]), draft)
        assert request.provider.provider_id == row["provider_id"]
        assert request.provider.configuration_generation == row["generation"]
        assert request.provider.credential_ref == row["credential_generation"]
        assert not plan["execution_available"]
        if mutation == "models":
            registry.update(row["provider_id"], update(row, models=["explicit-test-model", "second-model"]))
        elif mutation == "rotate":
            registry.update(row["provider_id"], update(row, credentials={"api_key": "synthetic-replacement"}))
        elif mutation == "remove":
            registry.remove(row["provider_id"], {"schema_version": 1, "expected_generation": row["generation"]})
        else:
            registry.secrets.revoke(registry._read().providers[0].credential_references["api_key"])
            assert registry.list()["providers"][0]["credential_status"] == "unavailable"
        with pytest.raises(PreparationError, match="agent_provider_(changed|unavailable)"):
            await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
                "idempotency_key": str(uuid.uuid4())})
        assert not host.tasks and not preparations.list()
    finally:
        await host.close()


async def test_same_provider_generation_and_review_survive_restart(foundation, tmp_path):
    _, _, preparations, draft, descriptor = foundation
    root = tmp_path / "providers"
    registry = AgentProviderRegistry(root); row = registry.create(choices())["provider"]
    host = AgentPreparationHost(preparations, tmp_path / "agent", provider_registry=registry)
    scope = selection(draft, descriptor); scope["provider_id"] = row["provider_id"]
    plan = await host.plan(scope)
    await host.close()
    host = AgentPreparationHost(preparations, tmp_path / "agent", provider_registry=AgentProviderRegistry(root))
    try:
        request, _, config = host._validate(host._plan(plan["plan_id"]))
        assert request.provider.configuration_generation == row["generation"] and config.credential_ref == row["credential_generation"]
        with pytest.raises(PreparationError, match="execution_disabled"):
            await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
                "idempotency_key": str(uuid.uuid4())})
    finally:
        await host.close()


async def test_rotation_during_mock_dispatch_cannot_publish_or_adopt(foundation, tmp_path):
    from mcpme.preparation_agent_backend import OpenAIResponsesBackend
    from test_preparation_agent import role_of
    imports, _, preparations, draft, descriptor = foundation
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def transport(request):
        assert request.headers["Authorization"] == "Bearer " + SYNTHETIC
        calls.append(role_of(request)); entered.set()
        await release.wait()
        return handler_for(descriptor)(request)
    class SyntheticTransportAdapter(OpenAIResponsesAdapter):
        def backend(self, resolve, configuration):
            return OpenAIResponsesBackend(lambda generation: resolve(generation)["api_key"],
                transport=httpx.MockTransport(transport))
    registry = AgentProviderRegistry(tmp_path / "providers", adapters=(SyntheticTransportAdapter(),))
    row = registry.create(choices())["provider"]
    # Private test-only injection; the shipped Supervisor always leaves this false.
    host = AgentPreparationHost(preparations, tmp_path / "agent", provider_registry=registry, live_enabled=True)
    try:
        scope = selection(draft, descriptor); scope["provider_id"] = row["provider_id"]
        plan = await host.plan(scope)
        job = await host.start({"action": "analyze", "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
            "idempotency_key": str(uuid.uuid4())})
        await asyncio.wait_for(entered.wait(), 2)
        registry.update(row["provider_id"], update(row, credentials={"api_key": "synthetic-rotated"}))
        release.set()
        ended = await completed(host, job)
        assert ended["state"] == "failed" and ended["local_io_settled"] and ended["candidate"] is None
        assert "adapter" not in calls and not host.tasks
        assert preparations.selected_descriptor(draft["draft_id"]) is None and imports.inspect(draft["draft_id"])["revision"] == 1
    finally:
        release.set()
        await host.close()


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"endpoint": "https://unregistered.invalid"}, {"adapter_id": "unregistered-v1"},
    {"configuration": {"endpoint": "https://unregistered.invalid"}}, {"models": []}, {"models": ["x", "x"]},
    {"models": ["x y"]}, {"models": [str(n) for n in range(17)]}, {"name": " name "}, {"name": "bad\nname"},
    {"credentials": {}}, {"credentials": {"api_key": ""}}, {"credentials": {"api_key": "has whitespace"}},
    {"credentials": {"api_key": "a" * 4097}}, {"credentials": {"api_key": "a", "other": "b"}},
    {"credentials": {"api_key": "nonascii-\u00e9"}}, {"live_enabled": True}, {"credential_ref": str(uuid.uuid4())},
], ids=["bool-version", "endpoint", "adapter", "endpoint-config", "no-model", "duplicate-model", "invalid-model",
    "model-limit", "name-space", "name-control", "no-key", "empty-key", "key-space", "key-limit", "extra-key",
    "key-encoding", "gate", "arbitrary-reference"])
def test_closed_choices_never_create_secret_or_authority(tmp_path, change):
    registry = AgentProviderRegistry(tmp_path / "providers")
    with pytest.raises(AgentProviderError):
        registry.create(choices(**change))
    assert registry.list()["providers"] == [] and registry.secrets.count() == 0


def test_stale_edit_and_removed_record_cannot_resurrect(tmp_path):
    registry = AgentProviderRegistry(tmp_path / "providers"); row = registry.create(choices())["provider"]
    latest = registry.update(row["provider_id"], update(row))["provider"]
    with pytest.raises(AgentProviderError, match="stale"):
        registry.update(row["provider_id"], update(row, credentials={"api_key": "synthetic-do-not-store"}))
    removed = registry.remove(latest["provider_id"], {"schema_version": 1, "expected_generation": latest["generation"]})["provider"]
    with pytest.raises(AgentProviderError, match="stale"):
        registry.update(row["provider_id"], update(removed))
    assert registry.secrets.count() == 1


def test_bounded_registry_limit_and_unsafe_private_state_fail_closed(tmp_path):
    registry = AgentProviderRegistry(tmp_path / "providers")
    for _ in range(16):
        registry.create(choices())
    with pytest.raises(AgentProviderError, match="limit"):
        registry.create(choices())
    assert registry.secrets.count() == 16
    registry.path.chmod(0o644)
    with pytest.raises(AgentProviderError, match="unavailable"):
        registry.list()
    with pytest.raises(AgentProviderError, match="unavailable"):
        AgentProviderRegistry(registry.root)


async def test_default_supervisor_owner_crud_and_review_keep_execution_gates_closed(tmp_path, monkeypatch, caplog):
    from mcpme.preparation_agent_backend import OpenAIResponsesBackend
    async def forbidden(*args, **kwargs):
        raise AssertionError("configuration attempted dispatch")
    monkeypatch.setattr(OpenAIResponsesBackend, "complete", forbidden)
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        source = tmp_path / "source"; source.mkdir(); (source / "worker.py").write_text("def echo(value): return value\n")
        draft = host.imports.create(source, name="Synthetic", intent="Review selected function")
        record = next(row for row in host.recipes.list() if row["state"] == "enabled")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                base_url="http://127.0.0.1:19384") as api:
            headers = {"Authorization": "Bearer owner"}
            initial = await api.get("/v1/agent-providers", headers=headers)
            assert initial.status_code == 200 and initial.json()["providers"] == []
            assert [item["adapter_id"] for item in initial.json()["adapters"]] == ["openai-responses-v1"]
            response = await api.post("/v1/agent-providers", headers=headers, json=choices())
            assert response.status_code == 200
            row = response.json()["provider"]
            options = (await api.post("/v1/agent-preparations/plans", headers=headers,
                json={"import_id": draft["draft_id"], "revision": 1})).json()
            assert options["providers"][0]["provider_id"] == row["provider_id"]
            assert options["providers"][0]["name"] == row["name"] and not options["providers"][0]["execution_available"]
            response = await api.post("/v1/agent-preparations/plans", headers=headers, json={
                "import_id": draft["draft_id"], "revision": 1,
                "recipe": {"id": record["recipe_id"], "version": record["version"], "digest": record["digest"]},
                "provider_id": row["provider_id"], "model": row["models"][0], "selected_paths": ["worker.py"]})
            assert response.status_code == 200
            plan = response.json(); assert not plan["execution_available"]
            assert (await api.post("/v1/agent-preparations/start", headers=headers, json={"action": "analyze",
                "plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())})).status_code == 400
            rotated = await api.post("/v1/agent-providers/" + row["provider_id"], headers=headers,
                json=update(row, credentials={"api_key": "synthetic-rotated-api"}))
            assert rotated.status_code == 200
            assert (await api.post("/v1/agent-providers/" + row["provider_id"], headers=headers, json=update(row))).status_code == 409
            current = rotated.json()["provider"]
            removed = await api.post("/v1/agent-providers/" + row["provider_id"] + "/remove", headers=headers,
                json={"schema_version": 1, "expected_generation": current["generation"]})
            assert removed.status_code == 200 and removed.json()["provider"]["status"] == "removed"
            assert SYNTHETIC not in response.text + json.dumps(options) + rotated.text + removed.text
        assert not host.agent_preparations.live_enabled and not host.preparation_runner.enabled and not host.resolution_runner.enabled
        assert not host.agent_preparations.tasks and not host.preparations.list()
        assert host.secrets.count() == 0
        assert not any(SYNTHETIC in record.getMessage() for record in caplog.records)
    finally:
        await host.close()


async def test_owner_policy_rejects_old_grants_auth_origin_and_unknown_inputs(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                base_url="http://127.0.0.1:19384") as api:
            target = "/v1/agent-providers/" + str(uuid.uuid4())
            for mode in ("read", "prepare"):
                grant = host.grants.issue({"mode": mode})
                scoped = read_credential(Path(grant["credential_file"]))["token"]
                for method, route in (("GET", "/v1/agent-providers"), ("POST", "/v1/agent-providers"),
                        ("POST", target), ("POST", target + "/remove")):
                    result = await api.request(method, route, headers={"Authorization": "Bearer " + scoped},
                        json=None if method == "GET" else choices())
                    assert result.status_code == 403 and result.json()["error"]["code"] == "grant_scope"
            for headers in ({}, {"Authorization": "Bearer owner", "Host": "elsewhere.invalid"},
                    {"Authorization": "Bearer owner", "Origin": "https://elsewhere.invalid"}):
                assert (await api.post("/v1/agent-providers", headers=headers, json=choices())).status_code in {401, 403}
            headers = {"Authorization": "Bearer owner"}
            rejected = await api.post("/v1/agent-providers", headers=headers, json=choices(endpoint=SYNTHETIC))
            assert rejected.status_code == 400 and SYNTHETIC not in rejected.text
            missing = await api.post(target + "/remove", headers=headers,
                json={"schema_version": 1, "expected_generation": str(uuid.uuid4())})
            assert missing.status_code == 404
        assert host.agent_provider_registry.list()["providers"] == [] and host.agent_provider_registry.secrets.count() == 0
    finally:
        await host.close()


async def test_new_trusted_adapter_uses_same_registry_and_host_seam(foundation, tmp_path):
    from types import SimpleNamespace
    class SyntheticAdapter(OpenAIResponsesAdapter):
        adapter_id = "synthetic-test-v1"
        destination = "https://synthetic.invalid/fixed"
        def backend(self, resolve, configuration):
            return SimpleNamespace(backend_id=self.adapter_id)
    _, _, preparations, draft, descriptor = foundation
    registry = AgentProviderRegistry(tmp_path / "providers", adapters=(SyntheticAdapter(),))
    row = registry.create(choices(adapter_id="synthetic-test-v1"))["provider"]
    host = AgentPreparationHost(preparations, tmp_path / "agent", provider_registry=registry)
    try:
        scope = selection(draft, descriptor); scope["provider_id"] = row["provider_id"]
        plan = await host.plan(scope)
        assert plan["destination"] == "https://synthetic.invalid/fixed"
        request = host._request(host._plan(plan["plan_id"]), draft)
        assert request.provider.backend == "synthetic-test-v1" and not plan["execution_available"]
    finally:
        await host.close()
