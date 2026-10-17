"""Lightweight real-process tests of the shared model-service launch graph."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ListToolsResult, Tool

from meecp.contracts import PackageError, validate_package
from meecp.processes import OwnershipLost
from meecp.private import ensure_private_directory, write_private_json
from meecp.supervisor import HostError, Supervisor


SOURCE = '''import argparse,asyncio,json,os,signal
from contextlib import asynccontextmanager,contextmanager
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('role');p.add_argument('--meecp-config-fd',type=int,required=True);a=p.parse_args()
with os.fdopen(a.meecp_config_fd,'rb') as f: c=json.loads(f.read())
if a.role=='engine':
 class H(BaseHTTPRequestHandler):
  def do_GET(self):
   ok=self.headers.get('Authorization')=='Bearer '+c['api_key'];self.send_response(200 if ok else 401);self.end_headers();self.wfile.write(b'{}')
  def log_message(self,*args):pass
 HTTPServer(('127.0.0.1',c['endpoint']['port']),H).serve_forever()
else:
 import uvicorn
 from mcp.server.fastmcp import FastMCP
 from mcp.server.transport_security import TransportSecuritySettings
 from mcp.types import ServerCapabilities
 from starlette.applications import Starlette
 from starlette.responses import JSONResponse
 from starlette.routing import Route,Mount
 m=FastMCP('fixture',stateless_http=True,json_response=True,transport_security=TransportSecuritySettings(allowed_hosts=['127.0.0.1:'+str(c['endpoint']['port'])]))
 original=m._mcp_server.get_capabilities
 m._mcp_server.get_capabilities=lambda *a,**k:ServerCapabilities(tools=original(*a,**k).tools)
 @m.tool(description='Report a synthetic engine identity')
 async def model_status():return {'engine_pid':c['dependencies']['engine']['pid'],'instance_id':c['dependencies']['engine']['instance_id'],'generation':c['generation']}
 async def health(request):return JSONResponse({'ready':True,'generation':c['generation'],'nonce':c['nonce']})
 sdk=m.streamable_http_app()
 @asynccontextmanager
 async def lifespan(app):
  async with sdk.router.lifespan_context(sdk):yield
 app=Starlette(routes=[Route('/health',health),Mount('/',app=sdk)],lifespan=lifespan)
 class Auth:
  async def __call__(self,scope,receive,send):
   if scope['type']=='http' and dict(scope['headers']).get(b'authorization')!=('Bearer '+c['api_key']).encode():
    return await JSONResponse({},status_code=401)(scope,receive,send)
   await app(scope,receive,send)
 class S(uvicorn.Server):
  @contextmanager
  def capture_signals(self):
   old={s:signal.signal(s,self.handle_exit) for s in (signal.SIGTERM,signal.SIGINT)}
   try:yield
   finally:
    for s,h in old.items():signal.signal(s,h)
 asyncio.run(S(uvicorn.Config(Auth(),host='127.0.0.1',port=c['endpoint']['port'],log_config=None,log_level='critical',access_log=False)).serve())
'''


def relock(root):
    manifest = json.loads((root / "meecp.package.json").read_bytes())
    artifacts = []
    for relative in ("bin/service", "weights.bin", "LICENSE"):
        path = root / relative
        artifacts.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "size": path.stat().st_size, "executable": bool(path.stat().st_mode & 0o111),
                          "source": "fixture", "provenance": "synthetic", "license": "LICENSE"})
    lock = {"schema_version": 1, "package_id": manifest["package_id"], "version": manifest["version"],
            "manifest_sha256": hashlib.sha256((root / "meecp.package.json").read_bytes()).hexdigest(),
            "artifacts": artifacts, "build": {"recipe": "fixture", "builder": "tests", "toolchain": "python"}}
    (root / "meecp.lock.json").write_text(json.dumps(lock))


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("model-service-fixture").resolve()
    reaper = directory / "meecp-reap"
    subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                    str(Path(__file__).resolve().parents[1] / "scripts/meecp-reap.c"), "-o", str(reaper)],
                   capture_output=True, check=True)
    package = directory / "package"
    (package / "bin").mkdir(parents=True)
    (package / "bin/service").write_text("#!" + sys.executable + "\n" + SOURCE)
    (package / "bin/service").chmod(0o700)
    (package / "weights.bin").write_bytes(b"synthetic weights")
    (package / "LICENSE").write_text("Synthetic fixture license")
    manifest = {"schema_version": 1, "package_id": "local.model-fixture", "version": "1.0.0",
                "source_revision": "sha256:" + "a" * 64, "name": "Synthetic shared model",
                "profile": "model-service", "platform": {"os": "macos", "architecture": "arm64", "minimum_version": "26.0"},
                "protocol": {"version": "2025-11-25", "capabilities": ["tools"]},
                "processes": [{"id": "engine", "role": "engine", "entrypoint": {"executable": "bin/service", "args": ["engine"]},
                               "readiness": {"path": "/props", "echo_generation": False}},
                              {"id": "adapter", "role": "adapter", "entrypoint": {"executable": "bin/service", "args": ["adapter"]},
                               "depends_on": ["engine"], "readiness": {"path": "/health", "echo_generation": True}}],
                "adapter_process": "adapter", "model": {"artifact": "weights.bin", "model_id": "synthetic",
                    "sha256": hashlib.sha256((package / "weights.bin").read_bytes()).hexdigest(),
                    "profile": "fixture", "context_tokens": 8192, "engine_identity": {"runtime": "fixture"}},
                "resources": {"memory_mb": 64, "cpu_only": False, "gpu": True},
                "lifecycle": {"startup_timeout_seconds": 10, "stop_grace_seconds": 1},
                "tools": [{"name": "model_status", "description": "Report a synthetic engine identity",
                           "input_schema": {"properties": {}, "title": "model_statusArguments", "type": "object"}}],
                "licenses": ["LICENSE"]}
    (package / "meecp.package.json").write_text(json.dumps(manifest))
    relock(package)
    return package, reaper


async def install(host, package):
    plan = await host.plan_install_async({"package": str(package), "start": True})
    operation = await host.apply_install(plan["plan_id"])
    async with asyncio.timeout(20):
        while True:
            value = host.registry.get_operation(operation["operation_id"])
            if value.status not in {"pending", "running"}:
                assert value.status == "completed", value.error_code
                return host.registry.get_slot(value.slot_id)
            await asyncio.sleep(0.02)


async def call(host, slot_id):
    connection = host.connection(slot_id)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + connection["token"]}, trust_env=False) as http:
        async with streamable_http_client(connection["endpoint"], http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("model_status", {})
                # No output schema: the SDK intentionally uses text content here.
                return json.loads(result.content[0].text)


async def test_model_shared_lifetime_restart_and_resource_admission(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        slot = await install(host, package)
        first, second = await asyncio.gather(call(host, slot.id), call(host, slot.id))
        assert first == second
        assert len(host.owner(slot.id).instances) == 3
        assert first["engine_pid"] == host.residents[slot.id]["engine"].process.pid
        other = host.registry.add_slot(package_digest=slot.package_digest, package_path=slot.package_path,
                                       name="Blocked second GPU", public_port=19495)
        with pytest.raises(HostError, match="resource_blocked"):
            await host.control(other.id, "start")
        assert await call(host, slot.id) == first
        await host.control(slot.id, "stop")
        assert slot.id not in host.residents and slot.id not in host.reservations
        await host.control(slot.id, "start")
        restarted = await call(host, slot.id)
        assert restarted["instance_id"] != first["instance_id"]
        assert restarted["generation"] > first["generation"]
    finally:
        await host.close()
    assert not host.reservations
    assert all(not value for value in host.ledgers.values())


async def test_legacy_config_flag_package_starts_and_restarts(tmp_path, prepared):
    import shutil
    source, reaper = prepared
    package = tmp_path.resolve() / "legacy-package"
    shutil.copytree(source, package)
    service = package / "bin/service"
    service.write_text(service.read_text().replace("--meecp-config-fd", "--mcpme-config-fd")
                       .replace("a.meecp_config_fd", "a.mcpme_config_fd"))
    relock(package)
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        slot = await install(host, package)
        first = await call(host, slot.id)
        # Failed renamed-flag launches have been reaped; only the gateway and
        # the authenticated engine/adapter graph remain owned.
        assert len(host.owner(slot.id).instances) == 3
        await host.control(slot.id, "stop")
        await host.control(slot.id, "start")
        second = await call(host, slot.id)
        assert second["generation"] > first["generation"]
        assert second["instance_id"] != first["instance_id"]
        assert len(host.owner(slot.id).instances) == 3
    finally:
        await host.close()
    assert not host.reservations
    assert all(not value for value in host.ledgers.values())


async def test_model_reaper_loss_retains_gpu_and_reaps_adapter(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    target = None
    try:
        slot = await install(host, package)
        engine = host.residents[slot.id]["engine"]
        adapter = host.residents[slot.id]["adapter"]
        target = engine.process.pid
        os.kill(engine.process.reaper_pid, signal.SIGKILL)
        with pytest.raises(OwnershipLost):
            await asyncio.wait_for(engine.process.wait(), 5)
        await asyncio.wait_for(adapter.process.wait(), 5)
        async with asyncio.timeout(5):
            while slot.id in host.workers:
                await asyncio.sleep(0.01)
        assert host.reservations[slot.id]["gpu"]
        assert engine.key_file.exists()
        assert host.registry.get_slot(slot.id).observed_state == "ownership_lost"
        with pytest.raises(HostError, match="ownership_lost"):
            await host.control(slot.id, "start")
    finally:
        await host.close()
        if target is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(target, signal.SIGKILL)


@pytest.mark.parametrize("mutation", ["cycle", "unknown_dependency", "model_digest", "adapter_nonce", "unconnected_engine"])
def test_model_static_graph_and_identity_rejections(tmp_path, prepared, mutation):
    import shutil
    source, _ = prepared
    package = tmp_path.resolve() / "package"
    shutil.copytree(source, package)
    path = package / "meecp.package.json"
    manifest = json.loads(path.read_bytes())
    if mutation == "cycle":
        manifest["processes"][0]["depends_on"] = ["adapter"]
    elif mutation == "unknown_dependency":
        manifest["processes"][1]["depends_on"] = ["missing"]
    elif mutation == "model_digest":
        manifest["model"]["sha256"] = "b" * 64
    elif mutation == "adapter_nonce":
        manifest["processes"][1]["readiness"]["echo_generation"] = False
    else:
        manifest["processes"][1]["depends_on"] = []
    path.write_text(json.dumps(manifest))
    relock(package)
    with pytest.raises(PackageError):
        validate_package(package)


async def test_invalid_resource_policy_releases_owner_lock(tmp_path, prepared):
    _, reaper = prepared
    state = ensure_private_directory(tmp_path.resolve() / "host")
    write_private_json(state / "resource-policy.json", {"memory_budget_mb": -1, "headroom_mb": 1024, "gpu_policy": "exclusive-residency"})
    with pytest.raises(HostError, match="invalid_resource_policy"):
        Supervisor(state, reaper)
    write_private_json(state / "resource-policy.json", {"memory_budget_mb": 16384, "headroom_mb": 1024, "gpu_policy": "exclusive-residency"})
    repaired = Supervisor(state, reaper)
    await repaired.close()


async def test_migration_blocking_and_active_transfer_guard(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    try:
        validated = validate_package(package)
        slot = host.registry.add_slot(package_digest=validated.digest, package_path=package,
                                      name="Blocked transfer", public_port=19496)
        host.migration_blocked.add(slot.id)
        with pytest.raises(HostError, match="migration_recovery_required"):
            await host.control(slot.id, "start")
        with pytest.raises(HostError, match="migration_recovery_required"):
            await host.legacy_model_control(slot.id, "resume")
        host.migration_blocked.clear()
        write_private_json(host.root / "transfers" / "active.json", {"slot_id": slot.id, "status": "running"})
        with pytest.raises(HostError, match="operation_in_progress"):
            await host.control(slot.id, "start")
        with pytest.raises(HostError, match="operation_in_progress"):
            await host.legacy_model_control(slot.id, "resume")
        assert not host.owner(slot.id).instances
    finally:
        await host.close()


@pytest.mark.parametrize("slow_probe", [False, True])
async def test_graph_and_protocol_probe_share_one_startup_deadline(tmp_path, prepared, monkeypatch, slow_probe):
    package_path, reaper = prepared
    package = validate_package(package_path)
    package = SimpleNamespace(manifest=package.manifest.model_copy(update={
        "lifecycle": package.manifest.lifecycle.model_copy(update={"startup_timeout_seconds": 1})}))
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    slot = SimpleNamespace(id="deadline-slot")
    monkeypatch.setattr(host, "package", lambda _: package)
    monkeypatch.setattr(host, "reserve_model", AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(host, "stop_residents", cleanup)
    launched = []
    async def launch(_slot, _package, declaration, *, deadline):
        assert deadline > asyncio.get_running_loop().time()
        launched.append(declaration.id)
        await asyncio.sleep(0.35 if slow_probe else 0.65)
    probe = AsyncMock(side_effect=lambda *_, **kwargs: None)
    async def check(*_, **kwargs):
        await asyncio.sleep(0.65)
    if slow_probe:
        probe = AsyncMock(side_effect=check)
    monkeypatch.setattr(host, "launch_resident", launch)
    monkeypatch.setattr(host, "check_http_candidate", probe)
    start = asyncio.get_running_loop().time()
    try:
        with pytest.raises(TimeoutError):
            await host.start_residents(slot)
        elapsed = asyncio.get_running_loop().time() - start
        assert 0.9 <= elapsed < 1.4
        assert len(launched) == 2 and probe.await_count == int(slow_probe)
        cleanup.assert_awaited_once_with(slot.id)
    finally:
        await host.close()


async def test_native_ack_uses_remaining_graph_deadline(tmp_path, prepared, monkeypatch):
    package_path, reaper = prepared
    package = validate_package(package_path)
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    slot = SimpleNamespace(id=str(__import__("uuid").uuid4()), generation=1, public_port=19496,
                           settings_digest=None, settings_path=None, data_path=None)
    engine = package.manifest.processes[0]
    captured = []
    async def launch(_slot, **kwargs):
        captured.append(kwargs["startup_timeout"])
        raise HostError("synthetic_launch_failure")
    monkeypatch.setattr(host, "launch", launch)
    try:
        with pytest.raises(HostError, match="synthetic_launch_failure"):
            await host.launch_resident(slot, package, engine, deadline=asyncio.get_running_loop().time() + 0.2)
        assert len(captured) == 1 and 0 < captured[0] <= 0.2
        assert not list((host.slot_directory(slot.id) / "runtime").glob("*.key"))
    finally:
        await host.close()


@pytest.mark.parametrize("delete_status,ping_status,error", [(204, 404, None),
    (500, 404, "session_termination_failed"), (204, 200, "session_termination_unverified")])
async def test_stateful_candidate_must_prove_session_termination(tmp_path, prepared, monkeypatch, delete_status, ping_status, error):
    import mcp
    from mcp.client import streamable_http
    package_path, reaper = prepared
    package = validate_package(package_path)
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    slot = SimpleNamespace(id="stateful-probe")
    runtime = {"endpoint": "http://127.0.0.1:19500/mcp", "key": "synthetic-private-key"}
    monkeypatch.setattr(host, "model_runtime", lambda _: runtime)
    @contextlib.asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, lambda: "private-session"
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    class Session:
        def __init__(self, *_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def initialize(self):
            return SimpleNamespace(protocolVersion="2025-11-25", capabilities=SimpleNamespace(model_dump=lambda **_: {"tools": {}}))
        async def list_tools(self):
            return ListToolsResult(tools=[Tool.model_validate(tool.mcp_schema()) for tool in package.manifest.tools])
    monkeypatch.setattr(mcp, "ClientSession", Session)
    requests = []
    def respond(request):
        requests.append(request.method)
        assert request.headers["authorization"] == "Bearer synthetic-private-key"
        assert request.headers["mcp-session-id"] == "private-session"
        assert request.headers["mcp-protocol-version"] == "2025-11-25"
        return httpx.Response(delete_status if request.method == "DELETE" else ping_status)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    try:
        if error:
            with pytest.raises(HostError, match=error):
                await host.check_http_candidate(slot, package)
        else:
            await host.check_http_candidate(slot, package)
        assert requests == (["DELETE"] if delete_status == 500 else ["DELETE", "POST"])
    finally:
        await host.close()
