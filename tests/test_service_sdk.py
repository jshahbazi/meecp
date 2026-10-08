"""Real MCP SDK and owned synthetic HTTP fixture; no model or provider."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys

import pytest
from mcp import ClientSession

from meecp.preparation_service import tools
from meecp.preparation_service_contracts import ServiceConnectionV1
from meecp.preparation_verifier import _owned_transport
from test_service_source_contracts import connection


@asynccontextmanager
async def adapter(tmp_path, configuration, credential):
    root = tmp_path / "generated"; root.mkdir()
    (root / "service.json").write_text(json.dumps(configuration.portable()))
    (root / "meecp.package.json").write_text(json.dumps({"tools": [item.model_dump(exclude_none=True) for item in tools(configuration)]}))
    sender, receiver = socket.socketpair(); sender.setblocking(False)
    backend = str(Path(__file__).resolve().parents[1] / "backend")
    code = "import sys;sys.path.insert(0,sys.argv[1]);from meecp_service.server import main;main(sys.argv[2])"
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", "-c", code, backend, str(root),
        env={"PATH":"/usr/bin:/bin", "MCPME_SETTINGS_FD":str(receiver.fileno())}, pass_fds=(receiver.fileno(),),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.get_running_loop().sock_sendall(sender, json.dumps({"schema_version":1,"configuration":{},"secrets":{"api_key":credential}}).encode())
        sender.shutdown(socket.SHUT_WR)
        async with _owned_transport(process) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as client:
                yield client
    finally:
        sender.close(); receiver.close()
        if process.returncode is None: process.terminate()
        try: await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill(); await process.wait()
        assert process.returncode is not None


@pytest.mark.parametrize("wrong_secret,large", [(False, False), (True, False), (False, True)])
async def test_generated_adapter_real_sdk_protocol_and_auth(tmp_path, wrong_secret, large):
    requests = []; tasks = set()
    async def service(reader, writer):
        task = asyncio.current_task(); tasks.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(b": ", 1) for line in raw.split(b"\r\n")[1:] if b": " in line)
            payload = json.loads(await reader.readexactly(int(headers[b"Content-Length"])))
            requests.append(payload)
            authorized = headers.get(b"Authorization") == b"Bearer synthetic-sdk-token"
            body = json.dumps({"model":"synthetic-model","choices":[{"message":{"role":"assistant","content":("x" * 600000 if large else "synthetic-result")},"finish_reason":"stop"}]}).encode()
            writer.write(b"HTTP/1.1 " + (b"200 OK" if authorized else b"401 Unauthorized") + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close(); await writer.wait_closed(); tasks.discard(task)
    server = await asyncio.start_server(service, "127.0.0.1", 0)
    selected = connection(); selected["endpoint"]["port"] = server.sockets[0].getsockname()[1]
    configuration = ServiceConnectionV1.model_validate(selected)
    try:
        async with asyncio.timeout(10):
            async with adapter(tmp_path, configuration, "synthetic-wrong-token" if wrong_secret else "synthetic-sdk-token") as client:
                initialized = await client.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                assert set(initialized.capabilities.model_dump(exclude_none=True)) == {"tools"}
                listing = await client.list_tools()
                assert [item.model_dump(by_alias=True, exclude_none=True) for item in listing.tools] == [item.mcp_schema() for item in tools(configuration)]
                assert requests == []
                checked = await client.call_tool("check_connection", {})
                assert checked.isError is wrong_secret
                if wrong_secret:
                    assert checked.structuredContent is None
                    assert len(requests) == 1
                else:
                    assert checked.structuredContent == {"compatible": True}
                    assert requests[0]["messages"] == [{"role":"user","content":"Hi"}]
                    generated = await client.call_tool("generate", {"prompt":"Synthetic input","max_output_tokens":8})
                    if large:
                        assert generated.isError and generated.structuredContent is None
                        assert generated.content[0].text == "service_response_limit"
                    else:
                        assert not generated.isError and generated.structuredContent == {"text":"synthetic-result","finish_reason":"stop"}
                    assert len(requests) == 2
                    denied = await client.call_tool("generate", {"prompt":"Synthetic input","endpoint":"http://127.0.0.2"})
                    assert denied.isError and len(requests) == 2
    finally:
        server.close(); await server.wait_closed()
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


async def test_actual_sdk_busy_admission_never_sends_second_request(tmp_path):
    entered = asyncio.Event(); resume = asyncio.Event(); tasks = set(); calls = []
    async def service(reader, writer):
        task = asyncio.current_task(); tasks.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(b": ", 1) for line in raw.split(b"\r\n")[1:] if b": " in line)
            await reader.readexactly(int(headers[b"Content-Length"]))
            calls.append(True); entered.set(); await resume.wait()
            body = json.dumps({"model":"synthetic-model","choices":[{"message":{"role":"assistant","content":"synthetic-result"},"finish_reason":"stop"}]}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close(); await writer.wait_closed(); tasks.discard(task)
    server = await asyncio.start_server(service, "127.0.0.1", 0)
    value = connection(); value["endpoint"]["port"] = server.sockets[0].getsockname()[1]
    try:
        async with asyncio.timeout(10):
            async with adapter(tmp_path, ServiceConnectionV1.model_validate(value), "synthetic-sdk-token") as client:
                await client.initialize()
                first = asyncio.create_task(client.call_tool("generate", {"prompt":"Synthetic input"}))
                await entered.wait()
                denied = await client.call_tool("check_connection", {})
                assert denied.isError and denied.content[0].text == "service_busy" and calls == [True]
                resume.set(); assert not (await first).isError
    finally:
        resume.set(); server.close(); await server.wait_closed()
        for task in tuple(tasks): task.cancel()
        await asyncio.gather(*tuple(tasks), return_exceptions=True)
