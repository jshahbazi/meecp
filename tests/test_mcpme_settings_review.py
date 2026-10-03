"""Independent regressions for configuration integration authority and delivery."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from mcpme.contracts import validate_package
from mcpme.private import write_private_json
from mcpme.settings import SettingsError, normalize_settings, settings_digest
from mcpme.supervisor import Supervisor
from test_mcpme_packages import edit_manifest, make_package


def configured_slot(host, tmp_path):
    root = make_package(tmp_path / "package")
    edit_manifest(root, configuration={"fields": {"enabled": {"type": "boolean", "default": True}}},
                  access={"secrets": ["service_key"]})
    package = validate_package(root)
    secret = host.secrets.create("Service", "selected-synthetic-value")
    settings = normalize_settings(package.manifest, {"configuration": {},
        "secret_references": {"service_key": secret["secret_id"]}})
    slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root, name="Fixture",
                                  public_port=19887, settings_digest=settings_digest(settings))
    write_private_json(host.slot_directory(slot.id) / "settings.json", settings)
    return slot, package, secret


async def test_revocation_during_spawn_ack_blocks_undelivered_material(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    receiver = None
    try:
        slot, package, secret = configured_slot(host, tmp_path)
        process = SimpleNamespace(instance_id="independent-fixture")
        reaped = []
        async def launch(_slot_id, **kwargs):
            nonlocal receiver
            receiver = socket.socket(fileno=os.dup(int(kwargs["env"]["MCPME_SETTINGS_FD"])))
            host.secrets.revoke(secret["secret_id"])
            await asyncio.sleep(0)
            return process
        async def reap(slot_id, owned):
            reaped.append((slot_id, owned))
        monkeypatch.setattr(host, "launch", launch)
        monkeypatch.setattr(host, "reap", reap)
        with pytest.raises(SettingsError, match="^secret_reference_unavailable$"):
            await host.launch_package(slot, package, env={}, args=[], executable=Path("unused"))
        assert reaped == [(slot.id, process)]
        assert receiver.recv(4096) == b""
    finally:
        if receiver is not None:
            receiver.close()
        await host.close()


async def test_legacy_empty_secret_reference_list_remains_plannable(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        package = make_package(tmp_path / "package")
        draft = host.imports.create(package, name="Legacy fixture")
        revised = host.imports.revise(draft["draft_id"], draft["revision"],
                                      requirements={"secret_references": []})
        revised = host.imports.attach(draft["draft_id"], revised["revision"], package)
        assert revised["state"] == "ready_to_plan"
        plan = await host.plan_install_async({"import_id": draft["draft_id"], "revision": revised["revision"]})
        assert plan["settings"]["secret_references"] == {}
    finally:
        await host.close()


async def test_descriptor_delivery_cancellation_settles_reap_before_closing(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    receiver = None
    try:
        slot, package, _ = configured_slot(host, tmp_path)
        process = SimpleNamespace(instance_id="cancel-fixture")
        sending, reaping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        inherited = None
        async def launch(_slot_id, **kwargs):
            nonlocal receiver, inherited
            inherited = int(kwargs["env"]["MCPME_SETTINGS_FD"])
            receiver = socket.socket(fileno=os.dup(inherited))
            return process
        async def send(*args):
            sending.set()
            await asyncio.Event().wait()
        async def reap(slot_id, owned):
            assert slot_id == slot.id and owned is process
            reaping.set()
            await release.wait()
        monkeypatch.setattr(host, "launch", launch)
        monkeypatch.setattr(host, "reap", reap)
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_sendall", send)
        task = asyncio.create_task(host.launch_package(slot, package, env={}, args=[], executable=Path("unused")))
        await sending.wait()
        task.cancel()
        await reaping.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert os.fstat(inherited)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(OSError):
            os.fstat(inherited)
        assert receiver.recv(4096) == b""
    finally:
        if receiver is not None:
            receiver.close()
        await host.close()


async def test_delivery_failure_reaps_before_returning_sanitized_lifecycle_failure(tmp_path, monkeypatch):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    receiver = None
    try:
        slot, package, _ = configured_slot(host, tmp_path)
        process = SimpleNamespace(instance_id="send-failure-fixture")
        reaped = []
        async def launch(_slot_id, **kwargs):
            nonlocal receiver
            receiver = socket.socket(fileno=os.dup(int(kwargs["env"]["MCPME_SETTINGS_FD"])))
            return process
        async def send(*args):
            raise BrokenPipeError()
        async def reap(slot_id, owned):
            reaped.append((slot_id, owned))
        monkeypatch.setattr(host, "launch", launch)
        monkeypatch.setattr(host, "reap", reap)
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_sendall", send)
        with pytest.raises(BrokenPipeError):
            await host.launch_package(slot, package, env={}, args=[], executable=Path("unused"))
        assert reaped == [(slot.id, process)]
        assert receiver.recv(4096) == b""
    finally:
        if receiver is not None:
            receiver.close()
        await host.close()


async def test_legacy_slot_ignores_unbound_settings_file(tmp_path):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    try:
        package = validate_package(make_package(tmp_path / "package"))
        slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root,
                                      name="Legacy", public_port=19887)
        write_private_json(host.slot_directory(slot.id) / "settings.json", {
            "configuration": {"unexpected": "unbound"}, "secret_references": {}})
        assert host.package_settings(slot) == {"schema_version": 1, "configuration": {}, "secret_references": {}}
    finally:
        await host.close()


@pytest.mark.parametrize("material", ["\U0001f600" * 1024, "\x01" * 4096], ids=["unicode", "escaped-controls"])
async def test_maximum_valid_secret_values_fit_bounded_runtime_delivery(tmp_path, monkeypatch, material):
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    receiver = None
    reading = None
    try:
        root = make_package(tmp_path / "package")
        names = ["secret" + str(index) for index in range(16)]
        edit_manifest(root, access={"secrets": names})
        package = validate_package(root)
        references = {name: host.secrets.create(name, material)["secret_id"] for name in names}
        settings = normalize_settings(package.manifest, {"configuration": {}, "secret_references": references})
        plan = host.plan_install({"package": str(root), "settings": settings})
        assert plan["settings"] == settings
        slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root, name="Maximum",
                                      public_port=19887, settings_digest=settings_digest(settings))
        write_private_json(host.slot_directory(slot.id) / "settings.json", settings)
        async def read_payload():
            payload = bytearray()
            while chunk := await asyncio.get_running_loop().sock_recv(receiver, 4096):
                payload.extend(chunk)
            return json.loads(payload)
        async def launch(_slot_id, **kwargs):
            nonlocal receiver, reading
            receiver = socket.socket(fileno=os.dup(int(kwargs["env"]["MCPME_SETTINGS_FD"])))
            receiver.setblocking(False)
            reading = asyncio.create_task(read_payload())
            return SimpleNamespace(instance_id="maximum-fixture")
        async def reap(*args):
            pass
        monkeypatch.setattr(host, "launch", launch)
        monkeypatch.setattr(host, "reap", reap)
        await host.launch_package(slot, package, env={}, args=[], executable=Path("unused"))
        payload = await reading
        assert payload["secrets"] == dict.fromkeys(names, material)
    finally:
        if reading is not None and not reading.done():
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
        if receiver is not None:
            receiver.close()
        await host.close()
