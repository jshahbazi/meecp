"""Owner-only download authority and content-free management responses."""
from __future__ import annotations

from pathlib import Path
import uuid

import httpx
import pytest

from mcpme.grants import read_credential
from mcpme.supervisor import Supervisor, management_app
from test_mcpme_packages import make_package


async def test_all_grant_modes_cannot_fetch_observe_or_cancel_downloads(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    try:
        package = make_package(tmp_path / "package")
        plan = host.plan_install({"package": str(package)})
        slot = host.registry.add_slot(package_digest=plan["package_digest"], package_path=package,
                                      name="Synthetic", public_port=19888)
        grants = [host.grants.issue({"mode": mode}) for mode in ("read", "prepare")]
        grants += [host.grants.issue({"mode": "lifecycle", "slot_ids": [slot.id], "actions": ["stop"]}),
                   host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=plan)]
        target = "/v1/downloads/" + str(uuid.uuid4())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                                     base_url="http://127.0.0.1:19384") as api:
            for grant in grants:
                headers = {"Authorization": "Bearer " + read_credential(Path(grant["credential_file"]))["token"]}
                for method, route, body in (("GET", "/v1/downloads", None), ("GET", target, None),
                                           ("POST", "/v1/downloads", {}), ("POST", target + "/cancel", {})):
                    result = await api.request(method, route, headers=headers, json=body)
                    assert result.status_code == 403
                    assert result.json()["error"]["code"] == "grant_scope"
            assert (await api.get("/v1/downloads")).status_code == 401
            assert (await api.post("/v1/downloads", json={})).status_code == 401
            owner = {"Authorization": "Bearer owner"}
            assert (await api.get("/v1/downloads", headers=owner)).json()["downloads"] == []
            invalid = await api.post("/v1/downloads", json={}, headers=owner)
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "invalid_download_spec"
        assert host.downloads.list() == [] and host.registry.list_operations() == []
    finally:
        await host.close()


@pytest.mark.parametrize("headers", [
    {"Host": "external.example"},
    {"Origin": "https://external.example"},
])
async def test_downloads_preserve_host_and_origin_policy(tmp_path, headers):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                                     base_url="http://127.0.0.1:19384") as api:
            response = await api.post("/v1/downloads", json={}, headers={"Authorization": "Bearer owner", **headers})
            assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
        assert host.downloads.list() == []
    finally:
        await host.close()


async def test_download_cancel_rejects_extra_effects_and_raw_errors_are_absent(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused-reaper")
    async def fail(_):
        raise RuntimeError("synthetic_failure_should_not_escape")
    try:
        monkeypatch.setattr(host.downloads, "start", fail)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                base_url="http://127.0.0.1:19384", headers={"Authorization": "Bearer owner"}) as api:
            failed = await api.post("/v1/downloads", json={})
            assert failed.json()["error"]["code"] == "invalid_request"
            assert "synthetic_failure" not in failed.text
            invalid = await api.post("/v1/downloads/" + str(uuid.uuid4()) + "/cancel", json={"install": True})
            assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "invalid_download_spec"
        assert host.downloads.list() == [] and host.registry.list_operations() == []
    finally:
        await host.close()
