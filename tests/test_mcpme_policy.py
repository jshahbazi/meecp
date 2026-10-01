import httpx
import pytest
from starlette.responses import JSONResponse

from mcpme.policy import LocalPolicy


@pytest.fixture
def guarded():
    async def app(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)
    return LocalPolicy(app, "secret", 19386, max_bytes=256)


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"authorization": "Bearer wrong"}, 401),
    ({"authorization": "Bearer secret", "host": "evil.test"}, 403),
    ({"authorization": "Bearer secret", "origin": "http://evil.test"}, 403),
    ([("authorization", "Bearer secret"), ("authorization", "Bearer secret")], 403),
    ([("authorization", "Bearer secret"), ("mcp-session-id", "a"), ("mcp-session-id", "b")], 403),
    ({"authorization": "Bearer secret", "content-length": "-1"}, 400),
    ({"authorization": "Bearer secret", "content-length": "257"}, 413),
])
async def test_policy_rejects_bad_authority_and_framing(guarded, headers, status):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://127.0.0.1:19386") as client:
        response = await client.get("/mcp", headers=headers)
    assert response.status_code == status
    assert "secret" not in response.text


@pytest.mark.parametrize("content", [b'{"private": "do not echo"}',
                                    b'{"jsonrpc":"2.0","method":"ping","id":1,"id":2}',
                                    b'[]', b'not-json'])
async def test_policy_sanitizes_envelope_errors(guarded, content):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://127.0.0.1:19386") as client:
        response = await client.post("/mcp", content=content, headers={"authorization": "Bearer secret"})
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_protocol"}}


async def test_policy_body_limit_without_length(guarded):
    async def chunks():
        yield b"x" * 200
        yield b"y" * 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://127.0.0.1:19386") as client:
        response = await client.post("/mcp", content=chunks(), headers={"authorization": "Bearer secret"})
    assert response.status_code == 413


async def test_policy_accepts_exact_local_authority(guarded):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://127.0.0.1:19386") as client:
        response = await client.get("/mcp", headers={"authorization": "Bearer secret", "origin": "http://127.0.0.1:19386"})
    assert response.status_code == 200
