"""Pi setup authority, private settings, cancellation and exact draft binding."""
import asyncio
import hashlib
import json
import platform
from pathlib import Path
import shutil
import subprocess
import threading

import httpx
import pytest

from meecp.grants import read_credential
from meecp.contracts import validate_package
from meecp.pi_setup import PiSetup, PiSetupError
from meecp.processes import OwnershipLost
from meecp.supervisor import Supervisor, management_app
from test_meecp_imports import source_folder
from test_meecp_packages import edit_manifest, make_package
from test_preparation_processes import group_reaper


@pytest.fixture
def host(tmp_path):
    for directory in ("python/bin", "preparation-toolchain/bin", "pi-runtime/bin"):
        (tmp_path / "host" / directory).mkdir(parents=True)
    value = Supervisor(tmp_path.resolve() / "state", tmp_path / "host/bin/unused-reaper")
    yield value
    value.imports.close()
    value.registry.close()
    import os
    os.close(value.lock_fd)


def configure(manager):
    return manager.save_settings({"provider": "openai", "model": "gpt-5.4-mini", "api_key": "synthetic-provider-secret"})


def test_settings_are_private_and_do_not_export_keys(host):
    manager = host.pi_setup
    assert not manager.settings()["configured"]
    settings = configure(manager)
    assert settings["configured"] and "synthetic-provider-secret" not in json.dumps(settings)
    assert not manager.settings_path.stat().st_mode & 0o077
    assert "synthetic-provider-secret" not in manager.settings_path.read_text()
    first = manager._settings()["credential"]
    manager.save_settings({"provider": "openai", "model": "gpt-5.3-codex"})
    assert manager._settings()["credential"] == first
    manager.save_settings({"provider": "openai", "model": "gpt-5.4-mini", "api_key": "replacement-synthetic-secret"})
    assert manager.secrets.get(first)["status"] == "revoked"
    assert manager.secrets.resolve({"key": manager._settings()["credential"]}) == {"key": "replacement-synthetic-secret"}


@pytest.mark.parametrize("payload", [
    {"provider": "openai", "model": "model", "base_url": "https://other.test"},
    {"provider": "openai-compatible", "model": "model", "base_url": "http://remote.test"},
    {"provider": "openai-compatible", "model": "model", "base_url": "https://user:password@example.test"},
    {"provider": "openai-compatible", "model": "model", "base_url": "https://example.test?q=key"},
    {"provider": "not-a-provider", "model": "model", "api_key": "synthetic"},
    {"provider": "openai", "model": "private\nmodel", "api_key": "synthetic"},
    {"provider": "openai", "model": "model", "api_key": "synthetic secret"},
])
def test_settings_reject_ambiguous_destinations_and_content(host, payload):
    with pytest.raises(PiSetupError, match="^pi_settings_invalid$"):
        host.pi_setup.save_settings(payload)
    assert not host.pi_setup.settings()["configured"]


def test_explicit_local_model_needs_no_cloud_key(host):
    value = host.pi_setup.save_settings({"provider": "openai-compatible", "model": "local/model",
                                        "base_url": "http://127.0.0.1:11434/v1"})
    assert value["configured"] and host.pi_setup._settings()["credential"] is None


def test_runtime_requires_complete_hashed_inventory(host, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bin").mkdir()
    files = []
    for name in ("bin/node", "worker.mjs"):
        path = runtime / name
        path.write_text("synthetic runtime")
        path.chmod(0o700 if name == "bin/node" else 0o600)
        files.append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "size": path.stat().st_size, "mode": path.stat().st_mode & 0o777})
    (runtime / "inventory.json").write_text(json.dumps({"schema_version": 1, "node": "bin/node", "worker": "worker.mjs", "files": files}))
    host.pi_setup.runtime_root = runtime
    assert host.pi_setup._runtime() == (runtime / "bin/node", runtime / "worker.mjs")
    (runtime / "worker.mjs").write_text("changed runtime")
    with pytest.raises(PiSetupError, match="^pi_runtime_unavailable$"):
        host.pi_setup._runtime()


def enable_fake_work(manager, monkeypatch, *, started=None, release=None):
    configure(manager)
    monkeypatch.setattr(manager, "_runtime", lambda: (Path("/unused-node"), Path("/unused-worker")))
    async def agent(record, workspace, broker, runtime, settings, key):
        assert key == "synthetic-provider-secret"
        if started:
            started.set()
        if release:
            await release.wait()
        package = make_package(workspace / "package")
        edit_manifest(package, source_revision=record["source_revision"])
    async def verify(package, directory):
        return None
    monkeypatch.setattr(manager, "_run_agent", agent)
    monkeypatch.setattr(manager, "_verify", verify)


async def test_job_attaches_package_without_recipe_or_installing(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    enable_fake_work(manager, monkeypatch)
    recipes = host.recipes.list_summaries()
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    await manager.tasks[job["id"]]
    completed = manager.get(job["id"])
    assert completed["status"] == "ready" and completed["package"]["source_revision"] == draft["source"]["revision"]
    attached = host.imports.inspect(draft["draft_id"])
    assert attached["revision"] == draft["revision"] and attached["attachment"] is not None
    assert host.registry.list_slots() == [] and host.registry.list_operations() == []
    assert host.recipes.list_summaries() == recipes
    assert "synthetic-provider-secret" not in json.dumps(completed)
    await manager.close()


async def test_changed_draft_cannot_accept_agent_result(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    started, release = asyncio.Event(), asyncio.Event()
    enable_fake_work(manager, monkeypatch, started=started, release=release)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    await asyncio.wait_for(started.wait(), 3)
    host.imports.revise(draft["draft_id"], draft["revision"], intent="Different tool")
    release.set()
    await manager.tasks[job["id"]]
    assert manager.get(job["id"])["code"] == "pi_source_changed"
    assert host.imports.inspect(draft["draft_id"])["attachment"] is None
    await manager.close()


async def test_cancel_and_duplicate_start_are_owned(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    started, release = asyncio.Event(), asyncio.Event()
    enable_fake_work(manager, monkeypatch, started=started, release=release)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    payload = {"import_id": draft["draft_id"], "revision": draft["revision"]}
    job = await manager.start(payload)
    assert (await manager.start(payload))["id"] == job["id"]
    await asyncio.wait_for(started.wait(), 3)
    assert (await manager.cancel(job["id"]))["status"] == "canceled"
    assert manager.tasks[job["id"]].done() and host.imports.inspect(draft["draft_id"])["attachment"] is None
    await manager.close()


async def test_immediate_cancel_terminalizes_unstarted_task(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    enable_fake_work(manager, monkeypatch)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    payload = {"import_id": draft["draft_id"], "revision": draft["revision"]}
    job = await manager.start(payload)
    assert (await manager.cancel(job["id"]))["status"] == "canceled"
    replacement = await manager.start(payload)
    assert replacement["id"] != job["id"]
    await manager.close()
    assert manager.get(replacement["id"])["status"] == "canceled"


def test_description_and_url_create_immutable_instruction_draft(host):
    draft = host.pi_setup.create_brief({"intent": "Make a text cleaner", "source_url": "https://example.com/app"})
    assert draft["document_role"] == "instructions" and draft["intent"] == "Make a text cleaner"
    snapshot = Path(draft["source"]["snapshot_path"]) / "app.md"
    assert snapshot.read_text() == "Make a text cleaner\n\nSelected project URL: https://example.com/app"
    assert not list((host.pi_setup.root / "briefs").iterdir())


async def test_source_snapshot_mutation_is_rejected_before_model(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    enable_fake_work(manager, monkeypatch)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    (Path(draft["source"]["snapshot_path"]) / "validator.py").write_text("changed selected source")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    await manager.tasks[job["id"]]
    assert manager.get(job["id"])["code"] == "pi_source_changed"
    await manager.close()


async def test_pi_routes_require_owner_host_and_origin(host):
    port, owner = 19886, "synthetic-owner"
    grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
    token = read_credential(Path(grant["credential_file"]))["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                                 base_url=f"http://127.0.0.1:{port}") as client:
        for route in ("/v1/pi/settings", "/v1/pi/jobs"):
            assert (await client.get(route)).status_code == 401
            assert (await client.get(route, headers={"Authorization": "Bearer " + token})).status_code == 403
            assert (await client.get(route, headers={"Authorization": "Bearer " + owner})).status_code == 200
            for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
                assert (await client.get(route, headers={"Authorization": "Bearer " + owner, **extra})).status_code == 403
        result = await client.put("/v1/pi/settings", headers={"Authorization": "Bearer " + owner},
                                 json={"provider": "openai", "model": "gpt-5.4-mini", "api_key": "synthetic-provider-secret"})
        assert result.status_code == 200 and "synthetic-provider-secret" not in result.text
        credential = host.pi_setup._settings()["credential"]
        changed = await client.put("/v1/pi/settings", headers={"Authorization": "Bearer " + owner},
                                  json={"provider": "openai", "model": "gpt-6.1-sol", "base_url": None})
        assert changed.status_code == 200 and changed.json()["configured"]
        assert changed.json()["model"] == "gpt-6.1-sol"
        assert host.pi_setup._settings()["credential"] == credential
        assert host.pi_setup.secrets.resolve({"key": credential}) == {"key": "synthetic-provider-secret"}
        assert "synthetic-provider-secret" not in changed.text and credential not in changed.text
        assert (await client.get("/v1/pi/settings", headers={"Authorization": "Bearer " + owner})).json()["model"] == "gpt-6.1-sol"
    await host.pi_setup.close()


def _fixture_package(root, fixture_bytes):
    """Seal synthetic probe fixtures without relying on an agent or network."""
    make_package(root)
    (root / "verification.json").write_bytes(fixture_bytes)
    _relock_probe_package(root)
    return validate_package(root)


def _relock_probe_package(root):
    manifest = (root / "meecp.package.json").read_bytes()
    lock = json.loads((root / "meecp.lock.json").read_text())
    lock["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    lock["artifacts"] = []
    for name in ("LICENSE", "bin/service", "verification.json"):
        path = root / name
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        lock["artifacts"].append({"path": name, "sha256": digest, "size": len(data),
            "executable": bool(path.stat().st_mode & 0o111), "source": "prepared:sha256:" + digest,
            "provenance": "Synthetic native probe fixture", "license": "LICENSE"})
    (root / "meecp.lock.json").write_text(json.dumps(lock))


@pytest.mark.parametrize("data", [b"", b"{", b"{}", b"[]",
    b'[{"tool":"unexpected","arguments":{},"expected":{}}]',
    b'[{"tool":"validate_json","arguments":[],"expected":{}}]',
    b'[{"tool":"validate_json","arguments":{},"expected":{},"extra":true}]',
])
async def test_probe_rejects_malformed_or_incomplete_fixtures_before_launch(host, tmp_path, monkeypatch, data):
    package = _fixture_package(tmp_path / "package", data)
    async def forbidden(*args, **kwargs):
        pytest.fail("invalid_fixtures_executed_package")
    monkeypatch.setattr("meecp.processes.ProcessOwner.launch", forbidden)
    with pytest.raises(PiSetupError, match="^pi_fixture_required$"):
        await host.pi_setup._verify(package, tmp_path / "verification")


async def test_probe_rejects_oversized_fixtures_without_unbounded_read(host, tmp_path, monkeypatch):
    package = _fixture_package(tmp_path / "package", b" " * (1024 * 1024 + 1))
    original = Path.read_bytes
    def bounded_only(path):
        if path == package.root / "verification.json":
            pytest.fail("verification_fixture_unbounded_read")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", bounded_only)
    async def forbidden(*args, **kwargs):
        pytest.fail("oversized_fixtures_executed_package")
    monkeypatch.setattr("meecp.processes.ProcessOwner.launch", forbidden)
    with pytest.raises(PiSetupError, match="^pi_fixture_required$"):
        await host.pi_setup._verify(package, tmp_path / "verification")


_PROBE_SERVER = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "success";
    char line[16384];
    while (fgets(line, sizeof line, stdin)) {
        char *id_start = strstr(line, "\"id\":");
        if (!id_start) continue;
        long id = strtol(id_start + 5, NULL, 10);
        const char *result = NULL;
        if (strstr(line, "\"initialize\"")) {
            if (!strcmp(mode, "malformed")) {
                puts("not-json"); fflush(stdout); continue;
            }
            result = !strcmp(mode, "capabilities")
                ? "{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{\"tools\":{},\"resources\":{}},\"serverInfo\":{\"name\":\"fixture\",\"version\":\"1.0.0\"}}"
                : !strcmp(mode, "protocol")
                ? "{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"fixture\",\"version\":\"1.0.0\"}}"
                : "{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"fixture\",\"version\":\"1.0.0\"}}";
        } else if (strstr(line, "\"tools/list\"")) {
            result = !strcmp(mode, "schema")
                ? "{\"tools\":[{\"name\":\"validate_json\",\"description\":\"Validate synthetic JSON\",\"inputSchema\":{\"type\":\"object\",\"properties\":{\"unexpected\":{\"type\":\"string\"}}}}]}"
                : "{\"tools\":[{\"name\":\"validate_json\",\"description\":\"Validate synthetic JSON\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}}]}";
        } else if (strstr(line, "\"tools/call\"")) {
            result = !strcmp(mode, "fixture")
                ? "{\"content\":[],\"structuredContent\":{\"valid\":false},\"isError\":false}"
                : !strcmp(mode, "boolean-number")
                ? "{\"content\":[],\"structuredContent\":{\"valid\":1},\"isError\":false}"
                : !strcmp(mode, "float-int")
                ? "{\"content\":[],\"structuredContent\":{\"valid\":1.0},\"isError\":false}"
                : "{\"content\":[],\"structuredContent\":{\"valid\":true},\"isError\":false}";
        }
        if (result) {
            printf("{\"jsonrpc\":\"2.0\",\"id\":%ld,\"result\":%s}\n", id, result);
            fflush(stdout);
        }
    }
    return 0;
}
'''


@pytest.fixture(scope="module")
def native_probe_server(tmp_path_factory):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not platform.mac_ver()[0].startswith("26."):
        pytest.skip("native independent MCP probe requires macOS 26 arm64")
    if not Path("/usr/bin/cc").is_file():
        pytest.skip("native synthetic MCP probe compiler unavailable")
    root = tmp_path_factory.mktemp("pi-native-mcp-fixture")
    source, executable = root / "server.c", root / "service"
    source.write_text(_PROBE_SERVER)
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
        check=True, capture_output=True, timeout=15)
    return executable


@pytest.mark.parametrize("mode,error,expected", [("success", None, True), ("fixture", "pi_fixture_failed", True),
    ("capabilities", "pi_probe_failed", True), ("schema", "pi_probe_failed", True),
    ("protocol", "pi_probe_failed", True), ("malformed", "pi_probe_failed", True),
    ("boolean-number", "pi_fixture_failed", True), ("float-int", "pi_fixture_failed", 1)])
async def test_independent_native_mcp_probe_checks_actual_service(host, tmp_path, native_probe_server, group_reaper,
        capfd, caplog, mode, error, expected):
    fixtures = json.dumps([{"tool": "validate_json", "arguments": {}, "expected": {"valid": expected}}]).encode()
    package = _fixture_package(tmp_path / "package", fixtures)
    shutil.copyfile(native_probe_server, package.root / "bin/service")
    (package.root / "bin/service").chmod(0o700)
    edit_manifest(package.root, entrypoint={"executable": "bin/service", "args": [mode], "cwd": "."})
    _relock_probe_package(package.root)
    package = validate_package(package.root)
    host.reaper = group_reaper
    directory = tmp_path / "verification"; directory.mkdir(mode=0o700)
    if error is None:
        assert await host.pi_setup._verify(package, directory) is None
    else:
        with pytest.raises(PiSetupError, match=f"^{error}$") as caught:
            await host.pi_setup._verify(package, directory)
        assert str(caught.value) == error
        assert "not-json" not in "".join(capfd.readouterr()) + caplog.text


async def test_attachment_timeout_revokes_admission_before_commit(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    enable_fake_work(manager, monkeypatch)
    staged, release = threading.Event(), threading.Event()
    original = host.imports.stage_attachment
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        staged.set()
        assert release.wait(5), "attachment_test_release_missing"
        return result
    monkeypatch.setattr(host.imports, "stage_attachment", delayed)
    monkeypatch.setattr("meecp.pi_setup.DEADLINE", .2)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    try:
        assert await asyncio.to_thread(staged.wait, 3), "attachment_test_never_staged"
        await asyncio.sleep(.25)
    finally:
        release.set()
    await manager.tasks[job["id"]]
    completed = manager.get(job["id"])
    assert completed["status"] == "failed" and completed["code"] == "pi_deadline_exceeded"
    assert host.imports.inspect(draft["draft_id"])["attachment"] is None
    await manager.close()


@pytest.mark.parametrize("boundary", ["cancel", "timeout"])
async def test_cancellation_after_attachment_commit_reports_completed_package(host, tmp_path, monkeypatch, boundary):
    manager = host.pi_setup
    enable_fake_work(manager, monkeypatch)
    committed, release = threading.Event(), threading.Event()
    original = host.imports.admit_attachment
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        committed.set()
        assert release.wait(5), "attachment_test_release_missing"
        return result
    monkeypatch.setattr(host.imports, "admit_attachment", delayed)
    if boundary == "timeout":
        monkeypatch.setattr("meecp.pi_setup.DEADLINE", .2)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    cancellation = None
    try:
        assert await asyncio.to_thread(committed.wait, 3), "attachment_test_never_committed"
        if boundary == "cancel":
            cancellation = asyncio.create_task(manager.cancel(job["id"]))
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(.25)
    finally:
        release.set()
    await manager.tasks[job["id"]]
    if cancellation:
        assert (await cancellation)["status"] == "ready"
    completed = manager.get(job["id"])
    attachment = host.imports.inspect(draft["draft_id"])["attachment"]
    assert completed["status"] == "ready" and completed["code"] is None
    assert completed["package"] == attachment
    await manager.close()


async def test_cleanup_ownership_loss_overrides_cancellation_and_blocks_shutdown(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    started, release = asyncio.Event(), asyncio.Event()
    enable_fake_work(manager, monkeypatch, started=started, release=release)
    class LostBroker:
        def __init__(self, *args, **kwargs):
            pass
        async def close(self):
            raise OwnershipLost()
    monkeypatch.setattr("meecp.pi_tools.PiToolBroker", LostBroker)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    await asyncio.wait_for(started.wait(), 3)
    completed = await manager.cancel(job["id"])
    assert completed["status"] == "failed" and completed["code"] == "pi_ownership_lost"
    assert manager.closed
    assert host.imports.inspect(draft["draft_id"])["attachment"] is None
    with pytest.raises(OwnershipLost):
        await manager.close()
