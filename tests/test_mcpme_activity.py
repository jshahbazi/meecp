"""Counters measure actual outcomes without retaining request or response data."""
import asyncio
import contextlib
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from mcpme.activity import Activity, ActivityReporter, RequestActivity
from mcpme.supervisor import Supervisor, Worker, management_app
from test_mcpme_gateway import HEADERS, client, initialize_raw, live_gateway
from test_mcpme_host import install, prepared, tool_call
from test_mcpme_http_gateway import public_proxy, sdk_call, stateless_runtime, public_client


def call(value="ok", rid=1, name="echo"):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": {"value": value}}}


def test_activity_rate_window_and_storage_are_bounded():
    now = [100.9]
    activity = Activity(clock=lambda: now[0], wall_clock=lambda: "2026-10-02T12:00:00.000Z")
    for _ in range(10000):
        activity.record("received")
        activity.record("succeeded", 2.125)
    assert len(activity.buckets) == 1
    assert activity.snapshot()["requests_last_minute"] == 10000
    now[0] = 159.99
    activity.record("received")
    activity.record("failed", 4)
    assert activity.snapshot()["requests_last_minute"] == 10001
    now[0] = 160
    assert activity.snapshot()["requests_last_minute"] == 1
    for second in range(160, 300):
        now[0] = second
        activity.record("received")
        activity.record("succeeded", 0)
    assert len(activity.buckets) == 60
    assert activity.snapshot()["requests_last_minute"] == 60
    now[0] = 360
    snapshot = activity.snapshot()
    assert snapshot["requests_last_minute"] == 0
    assert snapshot["received"] == snapshot["succeeded"] + snapshot["failed"] == 10141
    assert snapshot["in_flight"] == 0
    assert set(snapshot) == {"available", "received", "succeeded", "failed", "in_flight", "requests_last_minute",
                             "last_latency_ms", "last_request_at", "started_at"}


async def test_cancellation_records_one_terminal_outcome():
    local, host = Activity(), Activity()
    entered, release = asyncio.Event(), asyncio.Event()

    def report(event, latency):
        host.record(event, latency)

    request = RequestActivity(local, report)
    async def run():
        try:
            request.begin()
            entered.set()
            await release.wait()
        finally:
            request.finish(False)
    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    request.finish(True)  # A second close/outcome cannot double-count.
    for collector in (local, host):
        assert collector.received == collector.failed == 1
        assert collector.succeeded == collector.in_flight == 0


async def test_gateway_counts_rejections_failures_and_disconnected_work(monkeypatch):
    import test_mcpme_gateway as fixture
    monkeypatch.setattr(fixture, "CHILD", fixture.CHILD.replace("'isError':False", "'isError':value=='tool-error'"))
    async with live_gateway() as (endpoint, _, gateway), client(endpoint) as http:
        assert (await http.post(endpoint, json=call(), headers={**HEADERS, "authorization": "Bearer wrong"})).status_code == 401
        assert (await http.post(endpoint, json=call(), headers={**HEADERS, "origin": "https://invalid.example"})).status_code == 403
        assert gateway.activity.received == 0
        assert (await http.post(endpoint, json=call(), headers=HEADERS)).status_code == 404
        _, headers = await initialize_raw(http, endpoint)
        assert gateway.activity.received == gateway.activity.failed == 1
        assert (await http.post(endpoint, json=call(rid=2), headers=headers)).status_code == 200
        assert (await http.post(endpoint, json=call("tool-error", rid=3), headers=headers)).status_code == 200
        assert (await http.post(endpoint, json=call(rid=3), headers=headers)).status_code == 429
        assert (await http.post(endpoint, json=call(rid=4, name="jevk5_status"), headers=headers)).status_code == 200
        task = asyncio.create_task(http.post(endpoint, json=call("slow", rid=5), headers=headers))
        async with asyncio.timeout(2):
            while gateway.activity.in_flight != 1:
                await asyncio.sleep(0.005)
        assert gateway.activity.received == 6
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        async with asyncio.timeout(2):
            while gateway.activity.in_flight:
                await asyncio.sleep(0.005)
        snapshot = gateway.activity.snapshot()
        assert snapshot["succeeded"] == 3 and snapshot["failed"] == 3
        assert snapshot["last_latency_ms"] >= 250
        assert "tool-error" not in json.dumps(snapshot)


async def test_gateway_deadline_counts_one_failure():
    async with live_gateway(request_timeout=0.1) as (endpoint, _, gateway), client(endpoint) as http:
        _, headers = await initialize_raw(http, endpoint)
        await http.post(endpoint, json=call("hang"), headers=headers)
        async with asyncio.timeout(2):
            while gateway.activity.in_flight:
                await asyncio.sleep(0.005)
        assert gateway.activity.received == gateway.activity.failed == 1
        assert gateway.activity.succeeded == 0


async def test_failed_reporting_preserves_success_and_session_cleanup():
    def unavailable(event, latency):
        raise OSError("unavailable")
    async with live_gateway(activity_report=unavailable, request_timeout=0.2) as (endpoint, owner, gateway), client(endpoint) as http:
        _, headers = await initialize_raw(http, endpoint)
        response = await http.post(endpoint, json=call(), headers=headers)
        assert response.status_code == 200
        assert '"isError":false' in response.text
        assert gateway.activity.succeeded == 1
        await http.post(endpoint, json=call("hang", rid=2), headers=headers)
        async with asyncio.timeout(2):
            while not owner.closed:
                await asyncio.sleep(0.005)
        assert gateway.activity.available is False
        assert gateway.activity.received == 2 and gateway.activity.failed == 1
        assert gateway.activity.in_flight == 0
        assert len(owner.closed) == 1


async def test_reporting_buffer_saturation_is_nonblocking_and_explicit(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    parent, child = socket.socketpair()
    parent.setblocking(False)
    child.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    worker = Worker(SimpleNamespace(), parent, activity_channel=parent)
    try:
        slot = await install(host, package, "Saturated", start=False)
        reporter = ActivityReporter(child)
        for _ in range(100000):
            if not reporter.report("received", None):
                break
        assert reporter.failed
        assert reporter.report("succeeded", 1) is False
        await asyncio.wait_for(host.serve_activity(slot.id, worker), 2)
        assert host.activity(slot.id).available is False
        assert host.activity(slot.id).in_flight == 0
        assert host.activity(slot.id).received == host.activity(slot.id).failed
    finally:
        parent.close()
        child.close()
        await host.close()


async def test_http_clients_and_legacy_calls_share_counters_without_status_polls():
    async with stateless_runtime() as (private, _), public_proxy(private, legacy_jev=True) as (public, gateway):
        await asyncio.gather(sdk_call(public, "slow"), sdk_call(public, "ok"))
        assert gateway.activity.received == gateway.activity.succeeded == 2
        async with public_client() as http:
            assert (await http.get(public.removesuffix("/mcp") + "/api/status")).status_code == 200
            assert (await http.post(public.removesuffix("/mcp") + "/api/decide", json={})).status_code == 200
            await gateway.configure(None, None)
            assert (await http.post(public.removesuffix("/mcp") + "/api/decide", json={})).status_code == 409
        assert gateway.activity.received == 4
        assert gateway.activity.succeeded == 3 and gateway.activity.failed == 1
        assert gateway.activity.in_flight == 0


async def test_host_status_counts_survive_slot_restart_and_reset_with_host(tmp_path, prepared):
    package, reaper = prepared
    state = tmp_path.resolve() / "host"
    host = Supervisor(state, reaper)
    try:
        slot = await install(host, package, "Activity", start=True)
        assert host.activity(slot.id).received == 0  # Candidate checks and readiness probes.
        await asyncio.gather(tool_call(host, slot.id), tool_call(host, slot.id))
        activity = host.activity(slot.id)
        assert activity.received == activity.succeeded == 2
        await host.control(slot.id, "stop")
        connection = host.connection(slot.id)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(connection["endpoint"], json=call(),
                headers={**HEADERS, "authorization": "Bearer " + connection["token"]})
            assert response.status_code == 409
        assert activity.failed == 1
        await host.control(slot.id, "start")
        await tool_call(host, slot.id)
        app = management_app(host, "manager-key", 19499)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19499",
                                     headers={"authorization": "Bearer manager-key"}) as client:
            responses = [(await client.get(path)).json() for path in
                         ("/v1/status", "/v1/slots", f"/v1/slots/{slot.id}", f"/v1/slots/{slot.id}/details")]
        snapshots = [responses[0]["slots"][0], responses[1]["slots"][0], responses[2], responses[3]["slot"]]
        assert all(value["activity"] == activity.snapshot() for value in snapshots)
        assert activity.received == 4 and activity.succeeded == 3 and activity.failed == 1
        assert activity.available is True
        assert responses[3]["runtime_metrics"] is None
        assert activity.in_flight == 0
    finally:
        await host.close()
    reopened = Supervisor(state, reaper)
    try:
        assert reopened.slot_snapshot(reopened.registry.get_slot(slot.id))["activity"]["received"] == 0
    finally:
        await reopened.close()


async def test_worker_channel_loss_accounts_for_unfinished_requests(tmp_path, prepared):
    package, reaper = prepared
    host = Supervisor(tmp_path.resolve() / "host", reaper)
    parent, child = socket.socketpair()
    parent.setblocking(False)
    worker = Worker(SimpleNamespace(), parent)
    worker.activity_channel = parent
    try:
        slot = await install(host, package, "Disconnected", start=False)
        service = asyncio.create_task(host.serve_activity(slot.id, worker))
        reporter = ActivityReporter(child)
        reporter.report("received", None)
        reporter.report("received", None)
        reporter.report("succeeded", 5)
        async with asyncio.timeout(2):
            while host.activity(slot.id).succeeded != 1:
                await asyncio.sleep(0)
        assert host.activity(slot.id).in_flight == 1
        worker.closing = True
        child.close()
        await asyncio.wait_for(service, 2)
        assert host.activity(slot.id).snapshot()["failed"] == 1
        assert host.activity(slot.id).available is False
        assert host.activity(slot.id).in_flight == worker.activity_in_flight == 0
    finally:
        parent.close()
        child.close()
        await host.close()


@pytest.mark.parametrize("payload,expected", [
    ({"counts": {"decisions": 4, "cache_hits": 2, "errors": 1, "secret": "private"},
      "queue_depth": 3, "prompt": "never-display", "token": "never-display"},
     {"counts": {"decisions": 4, "cache_hits": 2, "errors": 1}, "queue_depth": 3}),
    ({"counts": {"decisions": True, "cache_hits": -1, "errors": "secret"}, "queue_depth": False}, None),
    ({"counts": {"decisions": 1}, "extra": "x" * 32768}, None),
])
async def test_runtime_metrics_expose_only_bounded_allowlisted_numbers(tmp_path, monkeypatch, payload, expected):
    host = Supervisor(tmp_path.resolve() / "host", tmp_path / "unused")
    package = SimpleNamespace(manifest=SimpleNamespace(profile="model-service", tools=[
        SimpleNamespace(name="jevk5_decide"), SimpleNamespace(name="jevk5_status")]))
    slot = SimpleNamespace(id="test-slot")
    monkeypatch.setattr(host, "model_runtime", lambda _: {"endpoint": "http://127.0.0.1:19999/mcp", "key": "private"})
    original_client = httpx.AsyncClient
    def client_factory(**kwargs):
        async def respond(request):
            assert request.url.path == "/api/status"
            return httpx.Response(200, json=payload)
        return original_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    try:
        assert await host.runtime_metrics(slot, package) == expected
    finally:
        await host.close()
