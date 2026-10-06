"""Generic tools-only stdio client bridge. No tool-call retries or content logging."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.types import JSONRPCMessage
from mcpme_local_transport import private_transport

from .gateway import (DEFAULT_REQUEST_BYTES, MAX_MESSAGE_BYTES, MAX_OPERATION_BYTES, MAX_PENDING,
                      MAX_REQUEST_BYTES, PROTOCOL_VERSION, encode, error_message)
from .policy import load_json


class RequestTooLarge(ValueError):
    """A local bound or an authenticated host's HTTP size rejection."""


class Bridge:
    def __init__(self, endpoint: str, token: str, *, socket_path: Path | None = None, timeout=65, client=None,
                 max_request_bytes=DEFAULT_REQUEST_BYTES):
        parts = urlsplit(endpoint)
        if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None or
                parts.username or parts.password or parts.path != "/mcp" or parts.query or parts.fragment):
            raise ValueError("invalid_endpoint")
        self.endpoint, self.token, self.timeout = endpoint, token, timeout
        if (not isinstance(max_request_bytes, int) or isinstance(max_request_bytes, bool) or
                not 1 <= max_request_bytes <= MAX_REQUEST_BYTES):
            raise ValueError("invalid_limits")
        self.max_request_bytes = max_request_bytes
        self.socket_path = socket_path
        self.session_id = None
        self.protocol_version = None
        self.client = client
        self.write_lock = asyncio.Lock()
        self.stop = asyncio.Event()
        self.tasks = set()
        self.notification_task = None
        self.notification_event_id = None
        self.writer = None

    def headers(self):
        headers = {"authorization": "Bearer " + self.token,
                   "accept": "application/json, text/event-stream", "content-type": "application/json"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.protocol_version:
            headers["mcp-protocol-version"] = self.protocol_version
        return headers

    async def emit(self, message):
        data = encode(message) + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("output_limit")
        async with self.write_lock:
            async with asyncio.timeout(self.timeout):
                await self.writer(data)

    async def messages(self, response, on_event_id=None):
        """Parse SSE incrementally with independent line, message and operation budgets."""
        buffer = bytearray()
        data_lines = []
        message_bytes = total = 0
        event_id = None
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_OPERATION_BYTES:
                raise ValueError("operation_limit")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                line = line.rstrip(b"\r")
                if len(line) > MAX_MESSAGE_BYTES:
                    raise ValueError("line_limit")
                if not line:
                    if event_id is not None and on_event_id is not None:
                        on_event_id(event_id)
                    if data_lines:
                        value = b"\n".join(data_lines)
                        if value:
                            message = load_json(value)
                            JSONRPCMessage.model_validate(message)
                            yield message
                    data_lines, message_bytes = [], 0
                    event_id = None
                elif line.startswith(b"id:"):
                    event_id = line[3:].lstrip(b" ").decode("ascii")
                    if len(event_id) > 128:
                        raise ValueError("event_id_limit")
                elif line.startswith(b"data:"):
                    data = line[5:]
                    if data.startswith(b" "):
                        data = data[1:]
                    message_bytes += len(data)
                    if message_bytes > MAX_MESSAGE_BYTES:
                        raise ValueError("message_limit")
                    data_lines.append(data)
            if len(buffer) > MAX_MESSAGE_BYTES:
                raise ValueError("line_limit")
        if buffer or any(data_lines):
            raise ValueError("incomplete_event")

    async def post(self, message):
        request_id = message.get("id")
        responded = False
        try:
            body = encode(message)
            if len(body) > self.max_request_bytes:
                raise RequestTooLarge()
            async with asyncio.timeout(self.timeout):
                async with self.client.stream("POST", self.endpoint, headers=self.headers(), content=body) as response:
                    if response.status_code == 413:
                        raise RequestTooLarge()
                    if response.status_code not in {200, 202}:
                        raise ValueError("request_rejected")
                    if message.get("method") == "initialize":
                        sid = response.headers.get("mcp-session-id")
                        if not sid or len(sid) > 128 or not sid.isascii() or not all(33 <= ord(c) <= 126 for c in sid):
                            raise ValueError("invalid_session")
                        self.session_id = sid
                    if response.status_code == 202:
                        if request_id is not None and "method" in message:
                            raise ValueError("missing_response")
                        return
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    matched = False
                    if content_type == "text/event-stream":
                        async for result in self.messages(response):
                            if "id" in result:
                                if matched:
                                    raise ValueError("duplicate_response")
                                if result["id"] != request_id or type(result["id"]) is not type(request_id):
                                    raise ValueError("response_mismatch")
                                matched = True
                                if message.get("method") == "initialize" and "result" in result:
                                    if result["result"].get("protocolVersion") != PROTOCOL_VERSION:
                                        raise ValueError("unsupported_version")
                                    self.protocol_version = PROTOCOL_VERSION
                            await self.emit(result)
                            if "id" in result:
                                responded = True
                    elif content_type == "application/json":
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            body.extend(chunk)
                            if len(body) > MAX_MESSAGE_BYTES:
                                raise ValueError("message_limit")
                        result = load_json(bytes(body))
                        JSONRPCMessage.model_validate(result)
                        if result.get("id") != request_id or type(result.get("id")) is not type(request_id):
                            raise ValueError("response_mismatch")
                        matched = True
                        if message.get("method") == "initialize" and "result" in result:
                            if result["result"].get("protocolVersion") != PROTOCOL_VERSION:
                                raise ValueError("unsupported_version")
                            self.protocol_version = PROTOCOL_VERSION
                        await self.emit(result)
                        responded = True
                    else:
                        raise ValueError("invalid_content_type")
                    if request_id is not None and not matched:
                        raise ValueError("missing_response")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if request_id is not None and not responded:
                if isinstance(error, RequestTooLarge):
                    await self.emit(error_message(request_id,
                        message="The request exceeds this app's image or data size limit."))
                else:
                    await self.emit(error_message(request_id))
            if responded:
                self.stop.set()
            if message.get("method") == "initialize":
                self.stop.set()

    async def notifications(self):
        def remember(event_id):
            self.notification_event_id = event_id

        failures = 0
        while not self.stop.is_set():
            try:
                headers = self.headers()
                if self.notification_event_id:
                    headers["last-event-id"] = self.notification_event_id
                async with self.client.stream("GET", self.endpoint, headers=headers) as response:
                    if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
                        raise ValueError("notification_stream_unavailable")
                    async for message in self.messages(response, remember):
                        if ("method" not in message or ("id" in message and message["method"] != "ping")):
                            raise ValueError("unsupported_server_message")
                        await self.emit(message)
                        failures = 0
            except asyncio.CancelledError:
                raise
            except httpx.ReadTimeout:
                # Reopen the GET stream, retaining its replay cursor. Tool calls are never retried.
                pass
            except httpx.TransportError:
                failures += 1
                if failures > 2:
                    self.stop.set()
                    return
            except Exception:
                self.stop.set()
                return
            await asyncio.sleep(0.1)

    async def delete(self):
        if self.session_id is None:
            return
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                await self.client.delete(self.endpoint, headers=self.headers())

    async def run(self, reader, writer):
        self.writer = writer
        owns_client = self.client is None
        if owns_client:
            # HTTP gateway also uses this class solely as an SSE parser. A
            # network bridge always requires the private transport explicitly.
            if self.socket_path is None:
                raise ValueError("private_transport_required")
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), trust_env=False,
                follow_redirects=False, transport=private_transport(self.socket_path))
        stop_waiter = asyncio.create_task(self.stop.wait())
        try:
            while not self.stop.is_set():
                read = asyncio.create_task(reader.readline())
                done, _ = await asyncio.wait({read, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
                if read not in done:
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                    break
                line = read.result()
                if not line:
                    break
                if len(line) > self.max_request_bytes + 1 or not line.endswith(b"\n"):
                    break
                if len(line[:-1]) > self.max_request_bytes:
                    break
                try:
                    message = load_json(line)
                    JSONRPCMessage.model_validate(message)
                except Exception:
                    break
                if message.get("method") == "initialize":
                    if self.session_id:
                        await self.emit(error_message(message.get("id")))
                        break
                    await self.post(message)
                    continue
                if self.session_id is None or self.protocol_version is None:
                    break
                is_notification_or_response = "id" not in message or "method" not in message
                if len(self.tasks) >= MAX_PENDING and not is_notification_or_response:
                    if "id" in message:
                        await self.emit(error_message(message["id"], message="The bridge request limit was reached."))
                    continue
                # Initialization ordering must reach HTTP before tools are dispatched.
                if message.get("method") == "notifications/initialized":
                    await self.post(message)
                    self.notification_task = asyncio.create_task(self.notifications())
                elif is_notification_or_response:
                    await self.post(message)
                else:
                    task = asyncio.create_task(self.post(message))
                    self.tasks.add(task)
                    task.add_done_callback(self.finished)
        finally:
            await self.delete()
            for task in self.tasks:
                task.cancel()
            if self.notification_task:
                self.notification_task.cancel()
            stop_waiter.cancel()
            await asyncio.gather(*self.tasks, *( [self.notification_task] if self.notification_task else [] ),
                                 stop_waiter, return_exceptions=True)
            if owns_client:
                await self.client.aclose()

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.stop.set()


async def run_bridge(endpoint: str, token: str, *, socket_path: Path, timeout=65,
                     max_request_bytes=DEFAULT_REQUEST_BYTES):
    """Bridge stdin/stdout and delete its HTTP session on EOF, SIGINT or SIGTERM."""
    bridge = Bridge(endpoint, token, socket_path=socket_path, timeout=timeout,
                    max_request_bytes=max_request_bytes)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=max_request_bytes + 1)
    protocol = asyncio.StreamReaderProtocol(reader)
    read_transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    stdout_fd = sys.stdout.fileno()
    os.set_blocking(stdout_fd, False)

    async def write(data):
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(stdout_fd, view):]
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_writer(stdout_fd, lambda: None if ready.done() else ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_writer(stdout_fd)

    handlers = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, bridge.stop.set)
            handlers.append(sig)
    try:
        await bridge.run(reader, write)
    finally:
        read_transport.close()
        for sig in handlers:
            loop.remove_signal_handler(sig)
