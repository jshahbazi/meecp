"""Passive HTTP reconnects must not extend a quiet MCP session's idle lease."""

import asyncio

import httpx

from mcpme.bridge import Bridge
from mcpme.gateway import PROTOCOL_VERSION
from test_mcpme_gateway import HEADERS, INIT, client, initialize_raw, live_gateway


async def test_bridge_notification_read_timeouts_release_idle_capacity_and_reap_child():
    idle_timeout = 0.8
    get_statuses = []

    async def record_response(response):
        if response.request.method == "GET":
            get_statuses.append(response.status_code)

    async with live_gateway(max_sessions=1, idle_timeout=idle_timeout) as (endpoint, owner, gateway):
        async with client(endpoint) as http, httpx.AsyncClient(
            timeout=httpx.Timeout(5, read=0.075), trust_env=False,
            event_hooks={"response": [record_response]},
        ) as notification_http:
            sid, headers = await initialize_raw(http, endpoint)
            child = owner.processes[sid]
            bridge = Bridge(endpoint, "secret", client=notification_http)
            bridge.session_id = sid
            bridge.protocol_version = PROTOCOL_VERSION
            notifications = asyncio.create_task(bridge.notifications())
            try:
                assert (await http.post(endpoint, json=INIT, headers=HEADERS)).status_code == 429
                # A real read timeout and the bridge's retry pause yield several GETs
                # before the idle lease and its bounded sweeper delay have elapsed.
                await asyncio.wait({notifications}, timeout=idle_timeout * 2)
                assert 409 not in get_statuses
                assert get_statuses.count(200) >= 4
                assert sid not in gateway.sessions
                assert child.returncode is not None
                assert sid in owner.closed
                assert sid not in owner.processes
                assert notifications.done()
                notifications.result()
                assert bridge.stop.is_set()
                assert get_statuses[-1] == 404
                assert (await http.get(endpoint, headers=headers)).status_code == 404
                replacement, _ = await initialize_raw(http, endpoint)
                assert replacement != sid
                assert owner.launched == 2
            finally:
                notifications.cancel()
                await asyncio.gather(notifications, return_exceptions=True)


async def test_terminal_result_last_event_id_replays_do_not_renew_idle_lease():
    idle_timeout = 0.8
    async with live_gateway(max_sessions=1, idle_timeout=idle_timeout) as (endpoint, owner, gateway):
        async with client(endpoint) as http:
            sid, headers = await initialize_raw(http, endpoint)
            child = owner.processes[sid]
            result = await http.post(endpoint, headers=headers,
                json={"jsonrpc": "2.0", "id": "completed-ping", "method": "ping"})
            assert result.status_code == 200
            event_ids = [line[4:] for line in result.text.splitlines() if line.startswith("id: ")]
            assert event_ids
            replay_headers = {**headers, "last-event-id": event_ids[-1]}
            deadline = asyncio.get_running_loop().time() + idle_timeout * 2
            replay_count = 0
            status = None
            while asyncio.get_running_loop().time() < deadline:
                replay = await http.get(endpoint, headers=replay_headers)
                status = replay.status_code
                if status == 404:
                    break
                assert status == 200
                assert replay.headers["content-type"] == "text/event-stream"
                assert replay.content == b""
                replay_count += 1
                await asyncio.sleep(0.1)
            assert replay_count >= 3
            assert status == 404
            assert sid not in gateway.sessions
            await asyncio.wait_for(child.wait(), 1)
            assert sid in owner.closed
            assert sid not in owner.processes
            replacement, _ = await initialize_raw(http, endpoint)
            assert replacement != sid
            assert owner.launched == 2
