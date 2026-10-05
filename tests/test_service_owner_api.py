"""Production owner handlers: static service creation, old-grant denial, no calls."""
import json
from pathlib import Path

import httpx
import pytest

from mcpme.grants import read_credential
from mcpme.supervisor import Supervisor, management_app
from mcpme import __main__ as cli


async def test_owner_service_intake_auth_and_revoke_are_static(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        secret = host.secrets.create("Synthetic", "synthetic-nonsecret-token")
        app = management_app(host, "synthetic-owner", 19384)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:19384") as client:
            owner = {"Authorization": "Bearer synthetic-owner"}
            catalog = await client.get("/v1/service-connectors", headers=owner)
            assert catalog.status_code == 200
            selected = catalog.json()["connectors"][0]
            connection = {"schema_version": 1, **{key: selected[key] for key in ("protocol", "transport_auth", "task")},
                "endpoint": {"scheme": "http", "host": "127.0.0.1", "port": 1, "base_path": "/v1"},
                "model": "synthetic-model", "secret_references": {"api_key": secret["secret_id"]},
                "limits": {"timeout_seconds": 1, "max_input_bytes": 64, "max_output_tokens": 16, "max_response_bytes": 4096}}
            body = {"schema_version": 1, "name": "Service", "intent": "Use local service", "start": False,
                "start_policy": "manual", "connection": connection}
            assert (await client.post("/v1/imports/service", json=body)).status_code == 401
            assert (await client.post("/v1/imports/service", headers={**owner,"Origin":"http://evil.invalid"}, json=body)).status_code == 403
            assert (await client.post("/v1/imports/service", headers={**owner,"Host":"evil.invalid"}, json=body)).status_code == 403
            for mode in ("read", "prepare"):
                grant = host.grants.issue({"mode": mode})
                token = read_credential(Path(grant["credential_file"]))["token"]
                headers = {"Authorization": "Bearer " + token}
                for method, path, payload in (("GET", "/v1/service-connectors", None), ("POST", "/v1/imports/service", body)):
                    denied = await client.request(method, path, headers=headers, json=payload)
                    assert denied.status_code == 403
            created = await client.post("/v1/imports/service", headers=owner, json=body)
            assert created.status_code == 200
            draft = created.json()
            assert draft["source"]["kind"] == "service_connection" and "locator" not in draft["source"]
            assert not (host.imports.staging / draft["draft_id"]).exists()
            assert "synthetic-nonsecret-token" not in json.dumps(draft)
            form = await client.post("/v1/preparations/form", headers=owner, json={"import_id":draft["draft_id"],"revision":draft["revision"]})
            assert form.status_code == 200
            assert form.json()["recipe"]["id"] == "mcpme.local-openai-text"
            assert [item["name"] for item in form.json()["tools"]] == ["generate", "check_connection"]
            assert form.json()["execution_available"] is False
            changed = await client.post("/v1/imports/" + draft["draft_id"] + "/service", headers=owner,
                json={"revision": 1, "connection": {**connection, "model": "other-synthetic"}})
            assert changed.status_code == 200 and changed.json()["revision"] == 2
            assert changed.json()["source"]["revision"] != draft["source"]["revision"]
            revoked = await client.post("/v1/secrets/" + secret["secret_id"] + "/revoke", headers=owner, json={})
            assert revoked.status_code == 200
            assert (await client.post("/v1/imports/service", headers=owner, json=body)).status_code != 200
            assert host.preparations.list() == [] and host.registry.list_operations() == []
    finally:
        await host.close()


async def test_cli_service_uses_registry_and_reference_only(monkeypatch, tmp_path):
    from mcpme.preparation_service_routes import registry
    import uuid
    calls = []; secret = str(uuid.uuid4())
    async def call(state, method, path, body=None):
        calls.append((method, path, body))
        return registry() if path == "/v1/service-connectors" else {"created": True}
    monkeypatch.setattr(cli, "call", call)
    args = cli.parser().parse_args(["--state-dir", str(tmp_path), "imports", "create-service", "--name", "Service",
        "--intent", "Use service", "--port", "8080", "--model", "synthetic", "--secret-reference", secret])
    assert await cli.execute(args) == {"created": True}
    assert [item[:2] for item in calls] == [("GET", "/v1/service-connectors"), ("POST", "/v1/imports/service")]
    assert calls[1][2]["connection"]["secret_references"] == {"api_key": secret}
    assert "source" not in calls[1][2]


async def test_service_capability_requires_exact_admitted_ready_closure(tmp_path):
    from mcpme.preparation_admission import PublicPreparationAdmission
    from mcpme.preparation_service import references
    from mcpme.supervisor import capabilities
    refs = references()
    assert "service_connection" in capabilities()["import_sources"]
    assert capabilities()["service_intake"]["execution"] is False
    admission = PublicPreparationAdmission(frozenset({refs[-1].key}), frozenset({frozenset(ref.key for ref in refs)}))
    host = Supervisor(tmp_path / "state", tmp_path / "unused", public_admission=admission)
    try:
        assert capabilities(host)["service_intake"]["execution"] is True
        host.recipes.disable("mcpme.local-openai-text", "1.0.0")
        assert capabilities(host)["service_intake"]["execution"] is False
        assert host.preparations.list() == []
    finally:
        await host.close()
