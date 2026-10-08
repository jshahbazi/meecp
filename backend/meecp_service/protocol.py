"""Bounded nonstreaming local Chat Completions subset, with no logging."""
from __future__ import annotations

import asyncio
import json
import socket


class ServiceFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def strict_json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ServiceFailure("service_response_invalid")
            value[key] = item
        return value
    try:
        value = json.loads(data, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        pending = [(value, 0)]; count = 0
        while pending:
            item, depth = pending.pop(); count += 1
            if depth > 32 or count > 65536:
                raise ValueError()
            if isinstance(item, dict): pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list): pending.extend((v, depth + 1) for v in item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ServiceFailure("service_response_invalid") from None


async def completion(configuration, credential, arguments):
    """Cancel/deadline closes our connection; upstream computation is not owned."""
    endpoint, limits = configuration["endpoint"], configuration["limits"]
    if (endpoint != {"scheme": "http", "host": "127.0.0.1", "port": endpoint.get("port"), "base_path": "/v1"}
            or type(endpoint["port"]) is not int or not 1 <= endpoint["port"] <= 65535
            or type(credential) is not str or not 1 <= len(credential) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in credential)):
        raise ServiceFailure("service_configuration_invalid")
    if type(arguments) is not dict or set(arguments) - {"prompt", "max_output_tokens"}:
        raise ServiceFailure("service_arguments_invalid")
    prompt = arguments.get("prompt"); maximum = arguments.get("max_output_tokens", limits["max_output_tokens"])
    if (type(prompt) is not str or not 1 <= len(prompt.encode("utf-8")) <= limits["max_input_bytes"]
            or type(maximum) is not int or not 1 <= maximum <= limits["max_output_tokens"]):
        raise ServiceFailure("service_arguments_invalid")
    body = json.dumps({"model": configuration["model"], "messages": [{"role": "user", "content": prompt}],
        "stream": False, "max_tokens": maximum, "temperature": 0}, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()
    if len(body) > 32768:
        raise ServiceFailure("service_request_limit")
    request = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{endpoint['port']}\r\n"
        f"Authorization: Bearer {credential}\r\nContent-Type: application/json\r\nAccept: application/json\r\n"
        f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n").encode("ascii") + body
    writer = None; sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM); sock.setblocking(False)
    try:
        async with asyncio.timeout(limits["timeout_seconds"]):
            # Numeric IPv4 only. No resolver, environment proxies or URL parser.
            await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", endpoint["port"]))
            reader, writer = await asyncio.open_connection(sock=sock, limit=16384)
            writer.write(request); await writer.drain()
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw[:-4].split(b"\r\n")
            if len(raw) > 16384 or len(lines) > 64 or lines[0].split(b" ")[:2] != [b"HTTP/1.1", b"200"]:
                raise ServiceFailure("service_http_rejected")
            headers = {}
            for line in lines[1:]:
                key, separator, value = line.partition(b":")
                key = key.lower()
                if not separator or key in headers or not key or any(c not in b"abcdefghijklmnopqrstuvwxyz0123456789-" for c in key):
                    raise ServiceFailure("service_response_invalid")
                headers[key] = value.strip()
            if (headers.get(b"content-type", b"").split(b";")[0].lower() != b"application/json"
                    or b"transfer-encoding" in headers or b"content-encoding" in headers):
                raise ServiceFailure("service_response_invalid")
            length = headers.get(b"content-length", b"")
            if not length.isdigit() or len(length) > 8 or not 1 <= int(length) <= limits["max_response_bytes"]:
                raise ServiceFailure("service_response_limit")
            value = strict_json(await reader.readexactly(int(length)))
            if (type(value) is not dict or value.get("model") != configuration["model"]
                    or type(value.get("choices")) is not list or len(value["choices"]) != 1):
                raise ServiceFailure("service_response_invalid")
            choice = value["choices"][0]
            if type(choice) is not dict or choice.get("finish_reason") not in {"stop", "length"}:
                raise ServiceFailure("service_response_invalid")
            message = choice.get("message")
            if (type(message) is not dict or message.get("role") != "assistant" or "tool_calls" in message
                    or type(message.get("content")) is not str or not message["content"]
                    or len(message["content"].encode()) > limits["max_response_bytes"]):
                raise ServiceFailure("service_response_invalid")
            return {"text": message["content"], "finish_reason": choice["finish_reason"]}
    except TimeoutError:
        raise ServiceFailure("service_deadline_exceeded") from None
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, UnicodeError):
        raise ServiceFailure("service_transport_failed") from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError, asyncio.CancelledError):
                pass
        sock.close()
