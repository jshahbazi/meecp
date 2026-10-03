"""Exact settings authority, owner API policy, and inherited descriptor lifetime."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import uuid

import httpx
import pytest

from mcpme.contracts import validate_package
from mcpme.grants import read_credential
from mcpme.private import read_private_json, write_private_json
from mcpme.registry import Registry, RegistryError
from mcpme.settings import SettingsError, normalize_settings, settings_digest
from mcpme.supervisor import Supervisor, management_app
from test_mcpme_packages import edit_manifest, make_package
from test_mcpme_imports import prepared_for, source_folder
from mcpme.imports import ImportError as ImportDraftError


def configured_package(tmp_path):
    package = make_package(tmp_path / "package")
    edit_manifest(package, configuration={"schema_version": 1, "fields": {
        "greeting": {"type": "string", "required": True, "max_length": 64},
        "enabled": {"type": "boolean", "default": True}}}, access={"secrets": ["service_key"]})
    return package


def choices(secret_id):
    return {"configuration": {"greeting": "Hello"}, "secret_references": {"service_key": secret_id}}


async def test_plans_bind_effective_settings_and_revocation_blocks_first_apply(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    material = "private_synthetic_settings_value"
    try:
        package = configured_package(tmp_path)
        with pytest.raises(SettingsError, match="^configuration_required$"):
            host.plan_install({"package": str(package)})
        secret = host.secrets.create("Service", material)
        selected = choices(secret["secret_id"])
        plan = host.plan_install({"package": str(package), "settings": selected})
        assert plan["settings"]["configuration"] == {"greeting": "Hello", "enabled": True}
        assert plan["settings_digest"] == settings_digest(plan["settings"])
        selected["configuration"]["greeting"] = "Changed"
        inspected = host.inspect_plan(plan["plan_id"])
        assert inspected["settings"]["configuration"]["greeting"] == "Hello"
        assert inspected["plan_digest"] == plan["plan_digest"]
        assert material not in json.dumps(plan) and material not in json.dumps(inspected)
        assert material.encode() not in host.registry.path.read_bytes()
        host.secrets.revoke(secret["secret_id"])
        with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
            await host.apply_install(plan["plan_id"])
        assert host.registry.list_operations() == []
    finally:
        await host.close()


async def test_edited_plan_settings_fail_before_operation_intent(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        package = configured_package(tmp_path)
        secret = host.secrets.create("Service", "private_synthetic_value")
        plan = host.plan_install({"package": str(package), "settings": choices(secret["secret_id"])})
        path = host.root / "plans" / (plan["plan_id"] + ".json")
        stored = read_private_json(path)
        stored["settings"]["configuration"]["greeting"] = "Changed"
        write_private_json(path, stored)
        with pytest.raises(SettingsError, match="^settings_digest_mismatch$"):
            await host.apply_install(plan["plan_id"])
        assert host.registry.list_operations() == []
    finally:
        await host.close()


async def test_all_grants_deny_secret_store_and_owner_outputs_have_no_material(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    material = "private_synthetic_owner_value"
    try:
        package = make_package(tmp_path / "package")
        plan = host.plan_install({"package": str(package)})
        slot = host.registry.add_slot(package_digest=plan["package_digest"], package_path=package,
                                      name="Synthetic", public_port=19888)
        grants = [host.grants.issue({"mode": mode}) for mode in ("read", "prepare")]
        grants += [host.grants.issue({"mode": "lifecycle", "slot_ids": [slot.id], "actions": ["stop"]}),
                   host.grants.issue({"mode": "install", "plan_id": plan["plan_id"]}, plan=plan)]
        target = "/v1/secrets/" + str(uuid.uuid4())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                                    base_url="http://127.0.0.1:19384") as api:
            for grant in grants:
                headers = {"Authorization": "Bearer " + read_credential(Path(grant["credential_file"]))["token"]}
                for method, route, body in (("GET", "/v1/secrets", None), ("GET", target, None),
                        ("POST", "/v1/secrets", {"name": "Service", "value": material}),
                        ("POST", target + "/revoke", {})):
                    response = await api.request(method, route, headers=headers, json=body)
                    assert response.status_code == 403
                    assert response.json()["error"]["code"] == "grant_scope"
                    assert material not in response.text
            assert (await api.get("/v1/secrets")).status_code == 401
            owner = {"Authorization": "Bearer owner"}
            created = await api.post("/v1/secrets", json={"name": "Service", "value": material}, headers=owner)
            assert created.status_code == 200 and "value" not in created.json()
            secret_id = created.json()["secret_id"]
            for response in (created, await api.get("/v1/secrets", headers=owner),
                    await api.get("/v1/secrets/" + secret_id, headers=owner),
                    await api.post("/v1/secrets/" + secret_id + "/revoke", json={}, headers=owner)):
                assert material not in response.text and '"value"' not in response.text
        assert host.secrets.count() == 1
    finally:
        await host.close()


@pytest.mark.parametrize("extra", [{"Host": "external.example"}, {"Origin": "https://external.example"}])
async def test_secret_store_preserves_transport_policy(tmp_path, extra):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, "owner", 19384)),
                base_url="http://127.0.0.1:19384") as api:
            response = await api.post("/v1/secrets", json={"name": "Service", "value": "synthetic"},
                                      headers={"Authorization": "Bearer owner", **extra})
            assert response.status_code == 403
        assert host.secrets.count() == 0
    finally:
        await host.close()


async def test_descriptor_delivers_only_selected_values_and_closes_after_spawn(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    receiver = None
    try:
        package = validate_package(configured_package(tmp_path))
        material = "private_synthetic_selected_value"
        secret = host.secrets.create("Service", material)
        host.secrets.create("Unselected", "private_synthetic_other_value")
        settings = normalize_settings(package.manifest, choices(secret["secret_id"]))
        slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root, name="Configured",
                                      public_port=19888, settings_digest=settings_digest(settings))
        write_private_json(host.slot_directory(slot.id) / "settings.json", settings)
        launched = []
        async def launch(_slot_id, **kwargs):
            nonlocal receiver
            launched.append(kwargs)
            fd = int(kwargs["env"]["MCPME_SETTINGS_FD"])
            assert fd in kwargs["inherited_fds"]
            receiver = socket.socket(fileno=os.dup(fd))
            return SimpleNamespace(instance_id="synthetic")
        monkeypatch.setattr(host, "launch", launch)
        result = await host.launch_package(slot, package, env={"PATH": "/usr/bin:/bin"}, args=[], executable=Path("unused"))
        assert result.instance_id == "synthetic"
        payload = bytearray()
        while chunk := receiver.recv(4096):
            payload.extend(chunk)
        assert json.loads(payload) == {"schema_version": 1, "configuration": {"greeting": "Hello", "enabled": True},
                                      "secrets": {"service_key": material}}
        assert material not in json.dumps(launched[0]["env"]) and material not in json.dumps(launched[0]["args"])
        with pytest.raises(OSError):
            os.fstat(int(launched[0]["env"]["MCPME_SETTINGS_FD"]))
        host.secrets.revoke(secret["secret_id"])
        with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
            await host.launch_package(slot, package, env={}, args=[])
        assert len(launched) == 1
    finally:
        if receiver is not None:
            receiver.close()
        await host.close()


async def test_settings_digest_survives_restart_and_rejects_changed_slot_file(tmp_path):
    state = tmp_path.resolve() / "state"
    host = Supervisor(state, tmp_path / "unused")
    package = validate_package(configured_package(tmp_path))
    secret = host.secrets.create("Service", "private_synthetic_restart_value")
    settings = normalize_settings(package.manifest, choices(secret["secret_id"]))
    slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root, name="Configured",
                                  public_port=19888, settings_digest=settings_digest(settings))
    path = host.slot_directory(slot.id) / "settings.json"
    write_private_json(path, settings)
    await host.close()
    host = Supervisor(state, tmp_path / "unused")
    try:
        assert host.package_settings(host.registry.get_slot(slot.id)) == settings
        settings["configuration"]["greeting"] = "Changed"
        write_private_json(path, settings)
        with pytest.raises(SettingsError, match="^settings_digest_mismatch$"):
            host.package_settings(host.registry.get_slot(slot.id))
    finally:
        await host.close()


def test_registry_migrates_real_version_two_slots_without_settings(tmp_path):
    state = tmp_path / "state"
    with Registry(state) as registry:
        slot = registry.add_slot(package_digest="a" * 64, package_path=tmp_path / "package", name="Old", public_port=19888)
        registry._connection.execute("ALTER TABLE slots DROP COLUMN settings_digest")
        registry._connection.execute("PRAGMA user_version=2")
    with Registry(state) as registry:
        assert registry.get_slot(slot.id) == slot and registry.get_slot(slot.id).settings_digest is None
        with pytest.raises(RegistryError, match="^generation_required$"):
            registry.update_slot(slot.id, settings_digest="b" * 64)
        updated = registry.update_slot(slot.id, generation=2, settings_digest="b" * 64)
        assert updated.settings_digest == "b" * 64


async def test_import_revision_binds_configuration_and_only_opaque_references(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    material = "private_synthetic_import_value"
    try:
        secret = host.secrets.create("Service", material)
        draft = host.imports.create(source_folder(tmp_path), name="Configured", intent="Expose validator")
        draft = host.imports.revise(draft["draft_id"], 1, requirements=choices(secret["secret_id"]))
        package = prepared_for(tmp_path, draft)
        edit_manifest(package, configuration={"fields": {"greeting": {"type": "string", "required": True},
                       "enabled": {"type": "boolean", "default": True}}}, access={"secrets": ["service_key"]})
        draft = host.imports.attach(draft["draft_id"], 2, package)
        assert draft["state"] == "ready_to_plan"
        exported = host.imports.export(draft["draft_id"], 2)
        assert material not in json.dumps(exported)
        plan = await host.plan_install_async({"import_id": draft["draft_id"], "revision": 2})
        assert plan["settings"]["configuration"] == {"greeting": "Hello", "enabled": True}
        assert plan["settings"]["secret_references"] == {"service_key": secret["secret_id"]}
        host.imports.revise(draft["draft_id"], 2, requirements={**choices(secret["secret_id"]),
                                                                "configuration": {"greeting": "Changed"}})
        with pytest.raises(ImportDraftError):
            await host.apply_install(plan["plan_id"])
        assert host.registry.list_operations() == []
        assert material.encode() not in host.imports.path.read_bytes()
    finally:
        await host.close()


async def test_partial_attached_draft_can_resume_but_unavailable_secret_cannot_plan(tmp_path):
    state = tmp_path.resolve() / "state"
    host = Supervisor(state, tmp_path / "unused")
    material = "private_synthetic_partial_import_value"
    try:
        package = configured_package(tmp_path)
        secret = host.secrets.create("Service", material)
        draft = host.imports.create(package, name="Configured")
        partial = host.imports.revise(draft["draft_id"], 1, carry_attachment=True, name="Continue later",
            requirements={"configuration": {"enabled": False}, "secret_references": {"service_key": secret["secret_id"]}})
        assert partial["attachment"] == draft["attachment"] and partial["source"] == draft["source"]
        with pytest.raises(ImportDraftError, match="^import_not_ready$"):
            await host.plan_install_async({"import_id": draft["draft_id"], "revision": 2})
        assert host.registry.list_operations() == []
        assert material.encode() not in host.imports.path.read_bytes()
    finally:
        await host.close()

    host = Supervisor(state, tmp_path / "unused")
    try:
        assert host.imports.inspect(draft["draft_id"]) == partial
        host.secrets.revoke(secret["secret_id"])
        configured = host.imports.revise(draft["draft_id"], 2, carry_attachment=True,
            requirements={"configuration": {"enabled": False, "greeting": "Hello"},
                          "secret_references": {"service_key": secret["secret_id"]}})
        assert configured["state"] == "ready_to_plan"  # Opaque references are resolved by the owner at plan/apply.
        with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
            await host.plan_install_async({"import_id": draft["draft_id"], "revision": 3})
        assert host.registry.list_operations() == []
        replacement = host.secrets.create("Replacement", "private_synthetic_replacement_value")
        complete = host.imports.revise(draft["draft_id"], 3, carry_attachment=True,
            requirements={**configured["requirements"], "secret_references": {"service_key": replacement["secret_id"]}})
        plan = await host.plan_install_async({"import_id": draft["draft_id"], "revision": complete["revision"]})
        assert plan["settings"] == {"schema_version": 1, "configuration": {"enabled": False, "greeting": "Hello"},
                                    "secret_references": {"service_key": replacement["secret_id"]}}
        host.secrets.revoke(replacement["secret_id"])
        with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
            await host.apply_install(plan["plan_id"])
        assert host.registry.list_operations() == []
    finally:
        await host.close()


async def test_legacy_empty_secret_reference_list_can_still_plan(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        draft = host.imports.create(source_folder(tmp_path), name="Legacy", intent="Expose validator")
        draft = host.imports.revise(draft["draft_id"], 1, requirements={"secret_references": []})
        draft = host.imports.attach(draft["draft_id"], 2, prepared_for(tmp_path, draft))
        plan = await host.plan_install_async({"import_id": draft["draft_id"], "revision": 2})
        assert plan["settings"]["secret_references"] == {}
    finally:
        await host.close()
