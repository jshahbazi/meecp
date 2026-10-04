"""Actual synthetic loopback requests; no model, provider or real credential."""
import asyncio
import json

import pytest

from mcpme_service.protocol import completion, ServiceFailure


@pytest.mark.parametrize("fault,code", [(None, None), ("auth", "service_http_rejected"),
    ("redirect", "service_http_rejected"), ("slow_headers", "service_deadline_exceeded"),
    ("slow_body", "service_deadline_exceeded"), ("oversize", "service_response_limit"),
    ("duplicate", "service_response_invalid"), ("model", "service_response_invalid"),
    ("encoding", "service_response_invalid"), ("chunked", "service_response_invalid")])
async def test_actual_loopback_subset_and_whole_operation_deadline(monkeypatch, fault, code):
    tasks = set(); seen = []
    async def client(reader, writer):
        task = asyncio.current_task(); tasks.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.split(b"\r\n"); headers = dict(line.split(b": ", 1) for line in lines[1:] if b": " in line)
            body = json.loads(await reader.readexactly(int(headers[b"Content-Length"])))
            seen.append((lines[0], headers[b"Authorization"], body))
            response = {"model": "synthetic", "choices": [{"message": {"role": "assistant", "content": "Synthetic greeting"}, "finish_reason": "stop"}]}
            if fault == "model": response["model"] = "different"
            raw = json.dumps(response).encode()
            if fault == "duplicate": raw = b'{"model":"synthetic","model":"synthetic"}'
            status = b"401 Unauthorized" if fault == "auth" else b"302 Found" if fault == "redirect" else b"200 OK"
            if fault == "slow_headers": await asyncio.sleep(2)
            extra = b"Content-Encoding: gzip\r\n" if fault == "encoding" else b"Transfer-Encoding: chunked\r\n" if fault == "chunked" else b""
            length = 1048577 if fault == "oversize" else len(raw)
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(length).encode() + b"\r\n" + extra + b"\r\n")
            await writer.drain()
            if fault == "slow_body": await asyncio.sleep(2)
            writer.write(raw); await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass
            tasks.discard(task)
    server = await asyncio.start_server(client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cfg = {"endpoint": {"scheme": "http", "host": "127.0.0.1", "port": port, "base_path": "/v1"},
        "model": "synthetic", "limits": {"timeout_seconds": 1, "max_input_bytes": 16384,
            "max_output_tokens": 32, "max_response_bytes": 1048576}}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.2:1")
    def forbidden_dns(*args, **kwargs):
        raise AssertionError("service_dns_forbidden")
    monkeypatch.setattr("socket.getaddrinfo", forbidden_dns)
    try:
        if code:
            with pytest.raises(ServiceFailure) as failure:
                await completion(cfg, "synthetic-nonsecret-token", {"prompt": "Synthetic greeting"})
            assert failure.value.code == code
        else:
            assert await completion(cfg, "synthetic-nonsecret-token", {"prompt": "Synthetic greeting"}) == {
                "text": "Synthetic greeting", "finish_reason": "stop"}
        assert len(seen) == 1
        assert seen[0] == (b"POST /v1/chat/completions HTTP/1.1", b"Bearer synthetic-nonsecret-token",
            {"model": "synthetic", "messages": [{"role": "user", "content": "Synthetic greeting"}],
             "stream": False, "max_tokens": 32, "temperature": 0})
    finally:
        server.close(); await server.wait_closed()
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


async def test_cancel_closes_owned_connection_without_claiming_upstream_settlement():
    connected = asyncio.Event(); disconnected = asyncio.Event()
    async def client(reader, writer):
        await reader.readuntil(b"\r\n\r\n"); connected.set()
        await reader.read(); disconnected.set(); writer.close(); await writer.wait_closed()
    server = await asyncio.start_server(client, "127.0.0.1", 0)
    cfg = {"endpoint": {"scheme": "http", "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1], "base_path": "/v1"},
        "model": "synthetic", "limits": {"timeout_seconds": 60, "max_input_bytes": 10,
            "max_output_tokens": 16, "max_response_bytes": 1024}}
    task = asyncio.create_task(completion(cfg, "synthetic", {"prompt": "hello"}))
    try:
        await asyncio.wait_for(connected.wait(), 2); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await asyncio.wait_for(disconnected.wait(), 2)
    finally:
        server.close(); await server.wait_closed()
