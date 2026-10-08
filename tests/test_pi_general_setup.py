"""Generic Pi setup questions, private verification, and resource admission."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import uuid

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from meecp.contracts import PackageError, validate_package
from meecp.grants import read_credential
from meecp.pi_questions import PiQuestionError
from meecp.pi_setup import PiSetupError
from meecp.processes import ProcessOwner
from meecp.supervisor import HostError, Supervisor, management_app
from test_meecp_host import prepared
from test_meecp_host import finish
from test_meecp_imports import source_folder
from test_meecp_packages import edit_manifest, make_package
from test_pi_setup import configure, _relock_probe_package, _PROBE_SERVER
from test_preparation_processes import gone, group_reaper


@pytest.fixture
def host(tmp_path):
    for directory in ("python/bin", "preparation-toolchain/bin", "pi-runtime/bin"):
        (tmp_path / "host" / directory).mkdir(parents=True)
    value = Supervisor(tmp_path.resolve() / "state", tmp_path / "host/bin/unused-reaper")
    yield value
    if not value.closing:
        value.imports.close()
        value.registry.close()
        os.close(value.lock_fd)


async def question_work(host, tmp_path, monkeypatch, *, kind="text", wait_after=False, declared_secret=False):
    manager = host.pi_setup
    configure(manager)
    monkeypatch.setattr(manager, "_runtime", lambda: (Path("/unused-node"), Path("/unused-worker")))
    replies = []
    answered = asyncio.Event()
    release = asyncio.Event()
    payload = {"kind": kind, "question": "Synthetic calendar question for this live run", "options": ["Work", "Personal"]}
    if kind == "secret":
        payload = {"kind": "secret", "question": "Synthetic app credential question", "credential_name": "TEST_API_KEY"}

    async def agent(record, workspace, broker, runtime, settings, key):
        replies.append(await manager._ask(record, payload))
        answered.set()
        if wait_after:
            await release.wait()
        package = make_package(workspace / "package")
        changes = {"source_revision": record["source_revision"]}
        if declared_secret:
            changes["access"] = {"secrets": ["TEST_API_KEY"]}
        edit_manifest(package, **changes)

    async def verify(package, directory):
        return None

    monkeypatch.setattr(manager, "_run_agent", agent)
    monkeypatch.setattr(manager, "_verify", verify)
    draft = host.imports.create(source_folder(tmp_path), name="Calendar", intent="Expose a calendar")
    job = await manager.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    async with asyncio.timeout(3):
        while manager.public_job(job["id"])["question"] is None:
            await asyncio.sleep(.01)
    return job, draft, replies, answered, release


def answer_body(job, question, value):
    return {"question_id": question["id"], "import_id": job["import_id"], "revision": job["revision"], "value": value}


async def test_owner_question_routes_are_ephemeral_and_keep_exact_revision(host, tmp_path, monkeypatch, capfd, caplog):
    manager = host.pi_setup
    job, draft, replies, _, _ = await question_work(host, tmp_path, monkeypatch)
    owner, port = "synthetic-owner", 19886
    grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
    grant_token = read_credential(Path(grant["credential_file"]))["token"]
    route = "/v1/pi/jobs/" + job["id"]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                                    base_url=f"http://127.0.0.1:{port}") as client:
            assert (await client.get(route)).status_code == 401
            assert (await client.get(route, headers={"Authorization": "Bearer " + grant_token})).status_code == 403
            headers = {"Authorization": "Bearer " + owner}
            value = (await client.get(route, headers=headers)).json()
            question = value["question"]
            assert value["status"] == "running" and value["stage"] == "waiting_for_user"
            assert question["job_id"] == job["id"] and question["revision"] == draft["revision"]
            journal = manager.jobs_root / (job["id"] + ".json")
            assert question["question"] not in journal.read_text() and "question" not in manager.get(job["id"])
            payload = answer_body(job, question, "Personal")
            assert (await client.post(route + "/answer", headers={"Authorization": "Bearer " + grant_token}, json=payload)).status_code == 403
            for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
                assert (await client.post(route + "/answer", headers={**headers, **extra}, json=payload)).status_code == 403
            changed = {**payload, "revision": job["revision"] + 1}
            rejected = await client.post(route + "/answer", headers=headers, json=changed)
            assert rejected.status_code == 400 and rejected.json()["error"]["code"] == "pi_question_changed"
            assert (await client.get(route, headers=headers)).json()["question"]["id"] == question["id"]
            accepted = await client.post(route + "/answer", headers=headers, json=payload)
            assert accepted.status_code == 200 and accepted.json()["question"] is None
            await manager.tasks[job["id"]]
            assert manager.get(job["id"])["status"] == "ready"
            assert json.loads(replies[0]["output"]) == {"kind": "text", "answer": "Personal"}
            assert "Personal" not in journal.read_text() and question["question"] not in journal.read_text()
            assert manager.public_job(job["id"])["question"] is None
            assert question["question"] not in caplog.text + "".join(capfd.readouterr())
    finally:
        await manager.close()


async def test_changed_source_draft_cannot_answer_pending_question(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    job, draft, _, _, _ = await question_work(host, tmp_path, monkeypatch)
    question = manager.public_job(job["id"])["question"]
    host.imports.revise(draft["draft_id"], draft["revision"], intent="A different integration")
    try:
        with pytest.raises(ValueError, match="^stale_import_revision$"):
            manager.answer(job["id"], answer_body(job, question, "Personal"))
        assert manager.public_job(job["id"])["question"]["id"] == question["id"]
        assert (await manager.cancel(job["id"]))["status"] == "canceled"
        assert manager.public_job(job["id"])["question"] is None
    finally:
        await manager.close()


async def test_human_wait_pauses_deadline_and_answer_resumes_it(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    monkeypatch.setattr("meecp.pi_setup.DEADLINE", .25)
    job, _, _, answered, _ = await question_work(host, tmp_path, monkeypatch, wait_after=True)
    try:
        assert manager.deadlines[job["id"]].when() is None
        await asyncio.sleep(.35)
        pending = manager.public_job(job["id"])
        assert pending["status"] == "running" and pending["question"] is not None
        manager.answer(job["id"], answer_body(job, pending["question"], "Personal"))
        await asyncio.wait_for(answered.wait(), 1)
        assert manager.deadlines[job["id"]].when() is not None
        await asyncio.wait_for(manager.tasks[job["id"]], 2)
        final = manager.public_job(job["id"])
        assert final["status"] == "failed" and final["code"] == "pi_deadline_exceeded" and final["question"] is None
        assert job["id"] not in manager.deadlines
    finally:
        await manager.close()


async def test_cancel_waiting_question_clears_public_state_and_budget(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    job, _, _, _, _ = await question_work(host, tmp_path, monkeypatch)
    question = manager.public_job(job["id"])["question"]
    final = await manager.cancel(job["id"])
    assert final["status"] == "canceled" and final["code"] == "pi_canceled"
    assert manager.public_job(job["id"])["question"] is None and job["id"] not in manager.deadlines
    with pytest.raises(PiQuestionError, match="^pi_question_changed$"):
        manager.answer(job["id"], answer_body(job, question, "Too late"))
    await manager.close()


async def test_credential_reference_is_bound_to_live_job_then_installation_draft(host, tmp_path, monkeypatch):
    manager = host.pi_setup
    job, draft, replies, answered, release = await question_work(host, tmp_path, monkeypatch, kind="secret", wait_after=True, declared_secret=True)
    try:
        question = manager.public_job(job["id"])["question"]
        value = "synthetic-private-app-credential"
        result = manager.answer(job["id"], answer_body(job, question, value))
        await asyncio.wait_for(answered.wait(), 1)
        assert value not in json.dumps(result) + json.dumps(replies) + json.dumps(manager.get(job["id"]))
        opaque = json.loads(replies[0]["output"])
        assert set(opaque) == {"kind", "credential_name", "secret_id"}
        reference = opaque["secret_id"]
        assert manager._resolve_credentials(job["id"], {"TEST_API_KEY": reference}) == {"TEST_API_KEY": value}
        unrelated = host.secrets.create("Unrelated", "synthetic-unrelated-secret")["secret_id"]
        for disallowed in (unrelated, manager._settings()["credential"]):
            with pytest.raises(PiQuestionError, match="^pi_credentials_unavailable$"):
                manager._resolve_credentials(job["id"], {"TEST_API_KEY": disallowed})
        release.set()
        await manager.tasks[job["id"]]
        assert manager.get(job["id"])["status"] == "ready"
        attached = host.imports.inspect(draft["draft_id"])
        assert attached["requirements"]["secret_references"] == {"TEST_API_KEY": reference}
        assert value not in json.dumps(attached)
        assert manager.questions.credentials(job["id"]) == {}
    finally:
        release.set()
        await manager.close()


@pytest.mark.parametrize("memory,gpu,sessions,accepted", [(8192, False, 2, True), (131072, False, 1, True),
    (8192, True, 1, True), (8192, True, 2, False)])
def test_stdio_resource_declarations_cover_large_cpu_and_single_gpu(tmp_path, memory, gpu, sessions, accepted):
    root = make_package(tmp_path / "package")
    edit_manifest(root, resources={"memory_mb": memory, "cpu_only": not gpu, "gpu": gpu}, lifecycle={"max_sessions": sessions})
    if accepted:
        manifest = validate_package(root).manifest
        assert manifest.resources.memory_mb == memory and manifest.resources.gpu == gpu
    else:
        with pytest.raises(PackageError, match="^invalid_resource_declaration$"):
            validate_package(root)


def large_package(tmp_path, prepared, *, gpu=False, sessions=2):
    original, reaper = prepared
    root = tmp_path / "package"
    shutil.copytree(original, root)
    path = root / "meecp.package.json"
    manifest = json.loads(path.read_bytes())
    manifest["resources"] = {"memory_mb": 8192, "cpu_only": not gpu, "gpu": gpu}
    manifest["lifecycle"]["max_sessions"] = sessions
    path.write_text(json.dumps(manifest))
    lock_path = root / "meecp.lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock))
    return validate_package(root), reaper


def slot_for(host, package, port=19495):
    return host.registry.add_slot(package_digest=package.digest, package_path=str(package.root),
                                  name="Extended stdio fixture", public_port=port)


@pytest.mark.parametrize("gpu,sessions", [(False, 2), (True, 1)])
async def test_extended_stdio_admission_retains_live_cost_then_releases(host, tmp_path, prepared, gpu, sessions):
    package, reaper = large_package(tmp_path, prepared, gpu=gpu, sessions=sessions)
    host.reaper = reaper
    expected = 8192 * sessions + 128
    host.resource_policy = {"memory_budget_mb": expected * 2 if gpu else expected, "headroom_mb": 128, "gpu_policy": "exclusive-residency"}
    first, second = slot_for(host, package), slot_for(host, package, 19496)
    try:
        process = await host.package_process(first)
        assert host.reservations[first.id] == {"memory_mb": expected, "gpu": gpu}
        await host.release_model(first.id)
        assert first.id in host.reservations
        with pytest.raises(HostError, match="^resource_blocked$"):
            await host.package_process(second)
        assert second.id not in host.reservations and not host.owner(second.id).instances
        await host.reap(first.id, process)
        await host.release_model(first.id)
        assert first.id not in host.reservations and not (host.slot_directory(first.id) / "reservation.json").exists()
        other = await host.package_process(second)
        assert second.id in host.reservations
        await host.reap(second.id, other)
        await host.release_model(second.id)
        assert not host.reservations
    finally:
        await host.close()


async def test_extended_stdio_launch_failure_releases_admission(host, tmp_path, prepared, monkeypatch):
    package, reaper = large_package(tmp_path, prepared, sessions=1)
    host.reaper = reaper
    host.resource_policy = {"memory_budget_mb": 10000, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
    slot = slot_for(host, package)
    async def fail(*args, **kwargs):
        raise HostError("runtime_start_failed")
    monkeypatch.setattr(host, "launch_package", fail)
    try:
        with pytest.raises(HostError, match="^runtime_start_failed$"):
            await host.package_process(slot)
        assert slot.id not in host.reservations and not (host.slot_directory(slot.id) / "reservation.json").exists()
    finally:
        await host.close()


_SERVICE_LAUNCHER = r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    pid_t child = fork();
    if (child < 0) return 2;
    if (!child) { for (;;) pause(); }
    char path[4096];
    const char *home = getenv("HOME");
    if (!home || snprintf(path, sizeof path, "%s/child.pid", home) >= (int)sizeof path) return 3;
    FILE *output = fopen(path, "w");
    if (!output) return 4;
    fprintf(output, "%ld\n", (long)child);
    fclose(output);
    execl("./json-utilities", "./json-utilities", (char *)NULL);
    return 5;
}
'''


async def test_installed_stdio_launcher_reaps_child_service_with_session(host, tmp_path, prepared):
    """An installed launcher cannot leave its own foreground child behind."""
    package, reaper = large_package(tmp_path, prepared, sessions=1)
    root = package.root
    source = tmp_path / "launcher.c"
    launcher = root / "bin/launcher"
    launcher.parent.mkdir(mode=0o700)
    source.write_text(_SERVICE_LAUNCHER)
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(launcher)],
                   check=True, capture_output=True, timeout=15)
    launcher.chmod(0o700)
    manifest_path = root / "meecp.package.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["entrypoint"] = {"executable": "bin/launcher", "args": [], "cwd": "."}
    manifest_path.write_text(json.dumps(manifest))
    lock_path = root / "meecp.lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    data = launcher.read_bytes()
    lock["artifacts"].append({"path": "bin/launcher", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                              "executable": True, "source": "fixture", "provenance": "Synthetic owned launcher",
                              "license": manifest["licenses"][0]})
    lock_path.write_text(json.dumps(lock))
    validate_package(root)
    host.reaper = reaper
    host.resource_policy = {"memory_budget_mb": 10000, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
    child = None
    try:
        plan = host.plan_install({"package": str(root), "start": True})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as client:
            async with streamable_http_client(connection["endpoint"], http_client=client) as (incoming, outgoing, _):
                async with ClientSession(incoming, outgoing) as session:
                    await session.initialize()
                    result = await session.call_tool("validate_json", {"document": '{"synthetic":true}'})
                    assert result.structuredContent == {"valid": True, "kind": "object"}
                    child = int((host.data_directory(slot) / "child.pid").read_text())
                    os.kill(child, 0)
        async with asyncio.timeout(5):
            while True:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.02)
        await host.control(slot.id, "stop")
        gateway = host.workers[slot.id].process
        assert set(host.owner(slot.id).instances) == {gateway.instance_id}
        assert set(host.ledger(slot.id)) == {gateway.instance_id}
        assert slot.id not in host.reservations
    finally:
        await host.close()
        # Bound cleanup of the freshly spawned synthetic child if this
        # regression fails before native ownership has reaped it.
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("blocked_by", ["memory", "gpu"])
async def test_probe_resource_admission_blocks_before_launch(host, tmp_path, monkeypatch, blocked_by):
    manager = host.pi_setup
    root = make_package(tmp_path / "probe-package")
    gpu = blocked_by == "gpu"
    edit_manifest(root, resources={"memory_mb": 8192, "cpu_only": not gpu, "gpu": gpu}, lifecycle={"max_sessions": 1})
    (root / "verification.json").write_text(json.dumps([{"tool": "validate_json", "arguments": {}, "expected": {"valid": True}}]))
    _relock_probe_package(root)
    package = validate_package(root)
    host.resource_policy = {"memory_budget_mb": 20000 if gpu else 8191, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
    held_slot = str(uuid.uuid4())
    if gpu:
        host.reservations[held_slot] = {"memory_mb": 128, "gpu": False}
        host.preparation_reservations["held-cpu-prepare"] = {"memory_mb": 32, "disk_bytes": 1024, "policy_digest": "synthetic"}
        manager.probe_reservations["held-gpu-probe"] = {"memory_mb": 128, "gpu": True}
    existing_probes = dict(manager.probe_reservations)
    launched = False
    async def forbidden(*args, **kwargs):
        nonlocal launched
        launched = True
        pytest.fail("resource_blocked_probe_launched")
    monkeypatch.setattr(ProcessOwner, "launch", forbidden)
    try:
        with pytest.raises(PiSetupError, match="^pi_resource_blocked$"):
            await manager._verify(package, tmp_path / "blocked-probe")
        assert not launched and manager.probe_reservations == existing_probes
    finally:
        host.reservations.pop(held_slot, None)
        host.preparation_reservations.pop("held-cpu-prepare", None)
        manager.probe_reservations.pop("held-gpu-probe", None)
        await manager.close()


@pytest.mark.parametrize("blocked_by", ["memory", "gpu"])
async def test_live_probe_cost_blocks_runtime_admission_and_releases(host, tmp_path, blocked_by):
    root = make_package(tmp_path / "package")
    gpu = blocked_by == "gpu"
    edit_manifest(root, resources={"memory_mb": 64, "cpu_only": not gpu, "gpu": gpu}, lifecycle={"max_sessions": 1})
    package = validate_package(root)
    slot = slot_for(host, package)
    host.resource_policy = {"memory_budget_mb": 1000 if gpu else 127, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
    host.pi_setup.probe_reservations["held-probe"] = {"memory_mb": 64, "gpu": gpu}
    try:
        with pytest.raises(HostError, match="^resource_blocked$"):
            await host.reserve_model(slot, package)
        assert slot.id not in host.reservations
        host.pi_setup.probe_reservations.pop("held-probe")
        await host.reserve_model(slot, package)
        assert host.reservations[slot.id] == {"memory_mb": 64, "gpu": gpu}
        await host.release_model(slot.id)
        assert not host.reservations
    finally:
        host.pi_setup.probe_reservations.pop("held-probe", None)
        await host.pi_setup.close()


_PRIVATE_SERVER = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(void) {
    char settings[8192] = {0};
    const char *descriptor = getenv("MCPME_SETTINGS_FD");
    int configured = 0;
    if (descriptor) {
        int fd = (int)strtol(descriptor, NULL, 10);
        size_t total = 0;
        ssize_t count;
        while (total < sizeof settings - 1 && (count = read(fd, settings + total, sizeof settings - total - 1)) > 0)
            total += (size_t)count;
        close(fd);
        configured = strstr(settings, "\"TEST_API_KEY\"") && strstr(settings, "\"Retries\": 3") && !getenv("TEST_API_KEY");
        memset(settings, 0, sizeof settings);
    }
    char line[16384];
    while (fgets(line, sizeof line, stdin)) {
        const char *id_start = strstr(line, "\"id\":");
        if (!id_start) continue;
        long id = strtol(id_start + 5, NULL, 10);
        const char *result = NULL;
        if (strstr(line, "\"initialize\""))
            result = "{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{\"tools\":{}},\"serverInfo\":{\"name\":\"private-fixture\",\"version\":\"1.0.0\"}}";
        else if (strstr(line, "\"tools/list\""))
            result = "{\"tools\":[{\"name\":\"validate_json\",\"description\":\"Validate synthetic JSON\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}}]}";
        else if (strstr(line, "\"tools/call\""))
            result = configured ? "{\"content\":[],\"structuredContent\":{\"valid\":true},\"isError\":false}"
                                : "{\"content\":[],\"structuredContent\":{\"valid\":false},\"isError\":false}";
        if (result) { printf("{\"jsonrpc\":\"2.0\",\"id\":%ld,\"result\":%s}\n", id, result); fflush(stdout); }
    }
    return 0;
}
'''


@pytest.fixture(scope="module")
def private_server(tmp_path_factory):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not platform.mac_ver()[0].startswith("26."):
        pytest.skip("native private verification requires macOS 26 arm64")
    root = tmp_path_factory.mktemp("pi-private-fd-fixture")
    source, executable = root / "server.c", root / "service"
    source.write_text(_PRIVATE_SERVER)
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
                   check=True, capture_output=True, timeout=15)
    return executable


async def test_independent_verifier_passes_private_settings_fd_without_artifact_credentials(host, tmp_path, private_server, group_reaper, monkeypatch, capfd, caplog):
    manager = host.pi_setup
    draft = host.imports.create(source_folder(tmp_path), name="Private fixture", intent="Expose a private service")
    job_id = str(uuid.uuid4())
    record = {"id": job_id, "import_id": draft["draft_id"], "revision": draft["revision"], "source_revision": draft["source"]["revision"],
              "status": "running", "stage": "thinking", "created_at": 1, "updated_at": 1, "code": None, "package": None}
    manager._update(record)
    question_task = asyncio.create_task(manager._ask(record, {"kind": "secret", "question": "Synthetic verification credential", "credential_name": "TEST_API_KEY"}))
    await asyncio.sleep(0)
    question = manager.public_job(job_id)["question"]
    value = "synthetic-verification-private-value"
    manager.answer(job_id, answer_body(record, question, value))
    await question_task
    root = make_package(tmp_path / "private-package")
    shutil.copyfile(private_server, root / "bin/service")
    (root / "bin/service").chmod(0o700)
    edit_manifest(root, source_revision=record["source_revision"], access={"secrets": ["TEST_API_KEY"]},
                  configuration={"fields": {"Retries": {"type": "integer", "default": 3}}})
    (root / "verification.json").write_text(json.dumps([{"tool": "validate_json", "arguments": {}, "expected": {"valid": True}}]))
    _relock_probe_package(root)
    package = validate_package(root)
    host.reaper = group_reaper
    launched_reservations = []
    original_launch = ProcessOwner.launch
    async def owned_launch(owner, *args, **kwargs):
        launched_reservations.append(dict(manager.probe_reservations))
        return await original_launch(owner, *args, **kwargs)
    monkeypatch.setattr(ProcessOwner, "launch", owned_launch)
    try:
        await manager._verify(package, tmp_path / "verification")
        assert launched_reservations == [{"verification": {"memory_mb": 64 + host.resource_policy["headroom_mb"], "gpu": False}}]
        assert not manager.probe_reservations
        assert value not in b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file()).decode(errors="ignore")
        assert value not in caplog.text + "".join(capfd.readouterr()) + json.dumps(manager.get(job_id))
    finally:
        manager.questions.forget(job_id)
        manager._update(record, status="canceled", code="pi_canceled")
        await manager.close()


_LISTENER_SERVER = _PROBE_SERVER.replace("#include <stdio.h>", "#include <stdio.h>\n#include <arpa/inet.h>\n#include <sys/socket.h>").replace(
    "    char line[16384];", r'''
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(!strcmp(mode, "wildcard") ? INADDR_ANY : INADDR_LOOPBACK);
    if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof address) || listen(listener, 1)) return 6;
    char line[16384];''')


@pytest.fixture(scope="module")
def listener_server(tmp_path_factory):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not platform.mac_ver()[0].startswith("26."):
        pytest.skip("native listener verification requires macOS 26 arm64")
    root = tmp_path_factory.mktemp("pi-listener-fixture")
    source, executable = root / "server.c", root / "service"
    source.write_text(_LISTENER_SERVER)
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
                   check=True, capture_output=True, timeout=15)
    return executable


@pytest.mark.parametrize("mode", ["loopback", "wildcard"])
async def test_independent_network_probe_audits_live_listeners(host, tmp_path, listener_server, group_reaper, monkeypatch, mode):
    root = make_package(tmp_path / "listener-package")
    shutil.copyfile(listener_server, root / "bin/service")
    (root / "bin/service").chmod(0o700)
    edit_manifest(root, entrypoint={"executable": "bin/service", "args": [mode], "cwd": "."},
                  access={"network": ["Synthetic local service"]})
    (root / "verification.json").write_text(json.dumps([{"tool": "validate_json", "arguments": {}, "expected": {"valid": True}}]))
    _relock_probe_package(root)
    package = validate_package(root)
    host.reaper = group_reaper
    launched = []
    original = ProcessOwner.launch
    async def observed(owner, *args, **kwargs):
        process = await original(owner, *args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(ProcessOwner, "launch", observed)
    try:
        if mode == "wildcard":
            with pytest.raises(PiSetupError, match="^pi_listener_not_loopback$"):
                await host.pi_setup._verify(package, tmp_path / "listener-probe")
        else:
            await host.pi_setup._verify(package, tmp_path / "listener-probe")
        assert len(launched) == 1 and not launched[0].owner.instances
        await gone(launched[0].pid)
        assert not host.pi_setup.probe_reservations
    finally:
        await host.pi_setup.close()


async def test_unregistered_stdio_candidate_with_wildcard_listener_fails_and_reaps(host, tmp_path, listener_server, group_reaper):
    root = make_package(tmp_path / "wildcard-candidate")
    shutil.copyfile(listener_server, root / "bin/service")
    (root / "bin/service").chmod(0o700)
    edit_manifest(root, entrypoint={"executable": "bin/service", "args": ["wildcard"], "cwd": "."},
                  access={"network": ["Synthetic local service"]})
    (root / "verification.json").write_text("[]")
    _relock_probe_package(root)
    host.reaper = group_reaper
    try:
        plan = host.plan_install({"package": str(root), "start": True})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "failed" and operation.error_code == "listener_not_loopback"
        assert not host.registry.list_slots() and not host.workers
        assert not host.owner(operation.slot_id).instances and not host.ledger(operation.slot_id)
        assert not getattr(host, "stdio_audits", {})
    finally:
        await host.close()


async def test_host_shutdown_joins_active_stdio_listener_audit(host, tmp_path, listener_server, group_reaper):
    root = make_package(tmp_path / "loopback-runtime")
    shutil.copyfile(listener_server, root / "bin/service")
    (root / "bin/service").chmod(0o700)
    edit_manifest(root, entrypoint={"executable": "bin/service", "args": ["loopback"], "cwd": "."},
                  access={"network": ["Synthetic local service"]})
    package = validate_package(root)
    slot = slot_for(host, package)
    host.reaper = group_reaper
    process = await host.package_process(slot)
    try:
        await asyncio.sleep(.2)
        assert process.instance_id in host.stdio_audits and not process._done.done()
    finally:
        await host.close()
    await gone(process.pid)
    assert not host.owner(slot.id).instances and not host.stdio_audits and not host.tasks


async def test_shutdown_during_listener_reap_does_not_schedule_late_registry_cleanup(host, tmp_path, listener_server, group_reaper, monkeypatch):
    root = make_package(tmp_path / "wildcard-runtime")
    shutil.copyfile(listener_server, root / "bin/service")
    (root / "bin/service").chmod(0o700)
    edit_manifest(root, entrypoint={"executable": "bin/service", "args": ["wildcard"], "cwd": "."},
                  access={"network": ["Synthetic local service"]})
    package = validate_package(root)
    slot = slot_for(host, package)
    host.reaper = group_reaper
    entered, release = asyncio.Event(), asyncio.Event()
    original = host.reap
    async def held(slot_id, process, *, join_audit=True):
        if not join_audit:
            entered.set()
            await release.wait()
        return await original(slot_id, process, join_audit=join_audit)
    monkeypatch.setattr(host, "reap", held)
    cleaned = []
    async def forbidden(*args):
        cleaned.append(True)
        pytest.fail("late_registry_cleanup_scheduled")
    monkeypatch.setattr(host, "fail_stdio_listener_slot", forbidden)
    process = await host.package_process(slot)
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(host.close())
    try:
        async with asyncio.timeout(5):
            while not host.closing or not host.stdio_audits[process.instance_id].cancelling():
                await asyncio.sleep(.01)
        release.set()
        await asyncio.wait_for(closing, 10)
        await gone(process.pid)
        assert not cleaned and not host.owner(slot.id).instances and not host.stdio_audits and not host.tasks
    finally:
        release.set()
        if not closing.done():
            await closing


_LATE_LISTENER_LAUNCHER = r'''
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    pid_t child = fork();
    if (child < 0) return 3;
    if (!child) {
        sleep(1);
        int listener = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in address = {0};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(!strcmp(argv[1], "wildcard") ? INADDR_ANY : INADDR_LOOPBACK);
        if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof address) || listen(listener, 1)) return 4;
        char path[4096];
        const char *home = getenv("HOME");
        if (!home || snprintf(path, sizeof path, "%s/listener.pid", home) >= (int)sizeof path) return 5;
        FILE *output = fopen(path, "w");
        if (!output) return 6;
        fprintf(output, "%ld\n", (long)getpid());
        fclose(output);
        for (;;) pause();
    }
    execl("./json-utilities", "./json-utilities", (char *)NULL);
    return 7;
}
'''


@pytest.mark.parametrize("mode", ["loopback", "wildcard"])
async def test_installed_stdio_audits_late_child_listener_and_closes_admission(host, tmp_path, prepared, mode):
    package, reaper = large_package(tmp_path, prepared, sessions=1)
    root, source = package.root, tmp_path / "late-listener.c"
    launcher = root / "bin/launcher"
    launcher.parent.mkdir(mode=0o700)
    source.write_text(_LATE_LISTENER_LAUNCHER)
    subprocess.run(["/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(launcher)],
                   check=True, capture_output=True, timeout=15)
    launcher.chmod(0o700)
    manifest_path = root / "meecp.package.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["entrypoint"] = {"executable": "bin/launcher", "args": [mode], "cwd": "."}
    manifest["access"] = {"network": ["Synthetic local child service"]}
    manifest_path.write_text(json.dumps(manifest))
    lock_path = root / "meecp.lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    data = launcher.read_bytes()
    lock["artifacts"].append({"path": "bin/launcher", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                              "executable": True, "source": "fixture", "provenance": "Synthetic late local service",
                              "license": package.manifest.licenses[0]})
    lock_path.write_text(json.dumps(lock))
    validate_package(root)
    host.reaper = reaper
    host.resource_policy = {"memory_budget_mb": 10000, "headroom_mb": 0, "gpu_policy": "exclusive-residency"}
    child = None
    try:
        plan = host.plan_install({"package": str(root), "start": True})
        operation = await finish(host, await host.apply_install(plan["plan_id"]))
        assert operation.status == "completed", operation.error_code
        slot = host.registry.get_slot(operation.slot_id)
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as client:
            async with streamable_http_client(connection["endpoint"], http_client=client) as (incoming, outgoing, _):
                async with ClientSession(incoming, outgoing) as session:
                    await session.initialize()
                    assert (await session.call_tool("validate_json", {"document": '{"synthetic":true}'})).structuredContent["valid"]
                    async with asyncio.timeout(5):
                        while not (host.data_directory(slot) / "listener.pid").is_file():
                            await asyncio.sleep(.02)
                    child = int((host.data_directory(slot) / "listener.pid").read_text())
                    if mode == "loopback":
                        await asyncio.sleep(.3)
                        os.kill(child, 0)
                        assert (await session.call_tool("validate_json", {"document": '{}'})).structuredContent["valid"]
                        assert host.registry.get_slot(slot.id).observed_state == "ready"
                    else:
                        async with asyncio.timeout(5):
                            while host.registry.get_slot(slot.id).error_code != "listener_not_loopback":
                                await asyncio.sleep(.02)
                        assert slot.id not in host.workers and not host.owner(slot.id).instances
                        assert not host.ledger(slot.id) and slot.id not in host.reservations
            await gone(child)
        if mode == "wildcard":
            assert host.registry.get_slot(slot.id).observed_state == "failed"
            assert not getattr(host, "stdio_audits", {})
            async with httpx.AsyncClient(timeout=.25, trust_env=False,
                                         headers={"Authorization": "Bearer " + connection["token"]}) as retry:
                try:
                    response = await retry.post(connection["endpoint"], json={"jsonrpc": "2.0", "id": 1,
                        "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                                              "clientInfo": {"name": "synthetic-retry", "version": "1"}}})
                    assert response.status_code >= 400
                except httpx.HTTPError:
                    pass
            assert not host.owner(slot.id).instances and slot.id not in host.workers
        else:
            await host.control(slot.id, "stop")
    finally:
        await host.close()
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
