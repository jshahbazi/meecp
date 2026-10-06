"""Real HTTP/locked-SDK cancellation with a bounded controlled inference thread."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import socket
import threading

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import ClientNotification, CancelledNotification, CancelledNotificationParams
import pytest
import uvicorn

from mcpme.processes import _await_ownership
from mcpme_textgen.adapter import BoundedTextGenerator
from mcpme_textgen.service import AttachedTextModel, make_app


class ControlledTransport:
    def __init__(self):
        self.started, self.release = threading.Event(), threading.Event()
        self.active, self.calls = False, 0

    def __call__(self, path, arguments):
        if path == "/tokenize":
            return {"tokens": [1]}
        assert path == "/completion"
        self.active, self.calls = True, self.calls + 1
        self.started.set()
        try:
            assert self.release.wait(10), "controlled_inference_deadline"
            return {"tokens": [2], "tokens_predicted": 1, "content": "ok", "stop_type": "limit",
                "truncated": False, "stop": True, "tokens_evaluated": 1}
        finally:
            self.active = False


class ControlledModel(AttachedTextModel):
    def __init__(self):
        self.config = {"generation": "synthetic-generation", "nonce": "synthetic-nonce"}
        self.engine = {"instance_id": "synthetic-owned-engine"}
        self.transport = ControlledTransport()
        self.adapter = BoundedTextGenerator(self.transport, self.fence)
        self.ready, self.start_count = False, 0
        self.fatal = asyncio.Event()

    async def start(self):
        self.start_count += 1
        self.ready = True


class ObservedWrite:
    def __init__(self, stream):
        self.stream, self.request_ids = stream, []

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *arguments):
        return await self.stream.__aexit__(*arguments)

    async def send(self, message):
        root = message.message.root
        await self.stream.send(message)
        if getattr(root, "method", None) == "tools/call":
            self.request_ids.append(root.id)


@asynccontextmanager
async def live_service(model):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app, auxiliary = make_app(model, "synthetic-service-key", port=port)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="critical",
        timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                assert not task.done(), "controlled_service_start_failed"
                await asyncio.sleep(.005)
        yield f"http://127.0.0.1:{port}/mcp", auxiliary
    finally:
        model.transport.release.set()
        server.should_exit = True
        await _await_ownership(task)
        listener.close()
        assert not model.transport.active and model.adapter.pending == 0


@asynccontextmanager
async def sdk_client(endpoint):
    async with httpx.AsyncClient(headers={"Authorization": "Bearer synthetic-service-key"},
            trust_env=False, timeout=5) as http:
        async with streamable_http_client(endpoint, http_client=http) as (read, write, session_id):
            observed = ObservedWrite(write)
            async with ClientSession(read, observed) as client:
                initialized = await client.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                yield client, observed, session_id, http


async def started(model):
    async with asyncio.timeout(3):
        while not model.transport.started.is_set():
            await asyncio.sleep(.005)


async def settled(model):
    async with asyncio.timeout(3):
        while model.adapter.pending:
            await asyncio.sleep(.005)
    assert model.adapter.ownership_settled and not model.transport.active


async def test_actual_http_sdk_cancellation_keeps_inference_owned_until_join():
    model = ControlledModel()
    active = None
    async with live_service(model) as (endpoint, _):
        async with sdk_client(endpoint) as (client, observed, session_id, _):
            try:
                active = asyncio.create_task(client.call_tool("text_generate", {"text": "synthetic", "max_output_tokens": 1}))
                await started(model)
                assert model.adapter.pending == 1 and not model.adapter.ownership_settled
                await client.send_notification(ClientNotification(CancelledNotification(
                    params=CancelledNotificationParams(requestId=observed.request_ids[-1]))))
                async with asyncio.timeout(1):
                    with pytest.raises(McpError):
                        await asyncio.shield(active)
                # The SDK error is not proof that the controlled engine thread
                # has joined. A second real session shares the same generation.
                assert model.transport.active and model.adapter.pending == 1
                assert not model.adapter.ownership_settled and model.adapter.completed == 0
                async with sdk_client(endpoint) as (other, _, other_id, _):
                    assert session_id() and other_id() and session_id() != other_id()
                    result = await other.call_tool("text_status", {})
                    assert result.structuredContent["engine_instance_id"] == model.engine["instance_id"]
                    assert result.structuredContent["queue_depth"] == 1
                model.transport.release.set()
                await settled(model)
                assert model.adapter.completed == 1 and model.transport.calls == 1 and model.start_count == 1
                result = await client.call_tool("text_generate", {"text": "synthetic", "max_output_tokens": 1})
                assert not result.isError and result.structuredContent["output_tokens"] == 1
            finally:
                model.transport.release.set()
                if active is not None:
                    await _await_ownership(asyncio.gather(active, return_exceptions=True))


async def test_actual_session_delete_cancels_request_and_preserves_shared_engine():
    model = ControlledModel()
    active = None
    async with live_service(model) as (endpoint, _):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer synthetic-service-key",
                "Accept": "application/json, text/event-stream"}, trust_env=False, timeout=5) as http:
            try:
                initialized = await http.post(endpoint, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "controlled-session", "version": "1"}}})
                assert initialized.status_code == 200
                headers = {"mcp-session-id": initialized.headers["mcp-session-id"], "mcp-protocol-version": "2025-11-25"}
                assert (await http.post(endpoint, headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})).status_code == 202
                active = asyncio.create_task(http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 2,
                    "method": "tools/call", "params": {"name": "text_generate",
                        "arguments": {"text": "synthetic", "max_output_tokens": 1}}}))
                await started(model)
                response = await http.delete(endpoint, headers=headers)
                assert response.status_code == 200
                assert model.transport.active and model.adapter.pending == 1 and not model.adapter.ownership_settled
                model.transport.release.set()
                await settled(model)
                await asyncio.wait_for(active, 3)
                async with sdk_client(endpoint) as (other, _, _, _):
                    result = await other.call_tool("text_status", {})
                    assert result.structuredContent["ready"] and model.start_count == 1
            finally:
                model.transport.release.set()
                if active is not None:
                    await _await_ownership(asyncio.gather(active, return_exceptions=True))


async def test_actual_stateful_routes_preserve_auth_host_origin_and_deleted_session():
    model = ControlledModel()
    async with live_service(model) as (endpoint, _):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as http:
            for method, headers, expected in (("POST", {}, 401), ("DELETE", {}, 401),
                    ("GET", {"Authorization": "Bearer synthetic-service-key", "Host": "foreign.invalid"}, 403),
                    ("POST", {"Authorization": "Bearer synthetic-service-key", "Origin": "https://foreign.invalid"}, 403)):
                response = await http.request(method, endpoint, headers=headers)
                assert response.status_code == expected
        async with sdk_client(endpoint) as (_, _, session_id, http):
            assert session_id()
            headers = {"mcp-session-id": session_id(), "mcp-protocol-version": "2025-11-25"}
            assert (await http.delete(endpoint, headers=headers)).status_code == 200
            response = await http.post(endpoint, headers=headers,
                json={"jsonrpc": "2.0", "id": 22, "method": "ping"})
            assert response.status_code == 404


async def test_many_private_clients_share_bounded_execution_and_independent_cancellation():
    model = ControlledModel()
    active = []
    async with live_service(model) as (endpoint, auxiliary):
        manager = auxiliary.state.mcp.session_manager
        assert manager.max_sessions is None
        assert manager.session_idle_timeout == 300 and manager.max_request_body_size == 32768
        async with AsyncExitStack() as clients:
            connected = [await clients.enter_async_context(sdk_client(endpoint)) for _ in range(12)]
            ids = {session_id() for _, _, session_id, _ in connected}
            assert len(ids) == 12 and ids == set(manager._server_instances)
            try:
                for index in range(4):
                    active.append(asyncio.create_task(connected[index][0].call_tool("text_generate",
                        {"text": "synthetic", "max_output_tokens": 1})))
                    async with asyncio.timeout(3):
                        while model.adapter.pending != index + 1:
                            await asyncio.sleep(.005)
                await started(model)
                assert model.transport.calls == 1 and model.adapter.queue_limit == 4
                overflow = await connected[8][0].call_tool("text_generate", {"text": "synthetic", "max_output_tokens": 1})
                assert overflow.isError and overflow.content[0].text == "queue_full"
                assert model.adapter.pending == 4 and model.transport.calls == 1
                status = await connected[9][0].call_tool("text_status", {})
                assert status.structuredContent["queue_depth"] == 4
                canceling, observed, _, _ = connected[1]
                await canceling.send_notification(ClientNotification(CancelledNotification(
                    params=CancelledNotificationParams(requestId=observed.request_ids[-1]))))
                with pytest.raises(McpError):
                    await asyncio.wait_for(asyncio.shield(active[1]), 3)
                async with asyncio.timeout(3):
                    while model.adapter.pending != 3:
                        await asyncio.sleep(.005)
                assert model.transport.active and model.transport.calls == 1
                _, _, deleted_id, http = connected[10]
                response = await http.delete(endpoint, headers={"mcp-session-id": deleted_id(),
                    "mcp-protocol-version": "2025-11-25"})
                assert response.status_code == 200
                assert set(manager._server_instances) == ids - {deleted_id()}
                assert (await connected[11][0].call_tool("text_status", {})).structuredContent["ready"]
                model.transport.release.set()
                results = await asyncio.wait_for(asyncio.gather(active[0], active[2], active[3]), 3)
                assert all(not result.isError for result in results)
                await settled(model)
                assert model.transport.calls == model.adapter.completed == 3 and model.start_count == 1
                result = await canceling.call_tool("text_generate", {"text": "synthetic", "max_output_tokens": 1})
                assert not result.isError and model.transport.calls == 4
            finally:
                model.transport.release.set()
                await _await_ownership(asyncio.gather(*active, return_exceptions=True))
        assert not manager._server_instances
