import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from jevk5_app.contracts import validate_decision
from jevk5_app.server import SafeMCP, make_app
from jevk5_app.state import ENDPOINT


TOKEN = "x" * 64


class Manager:
    def __init__(self):
        self.calls = 0
        self.last_state = None

    def status(self):
        return {"ready": False, "backend_pid": 1}

    async def start(self):
        pass

    async def shutdown(self):
        pass

    async def control(self, action):
        self.calls += 1
        return {"state": action}

    async def decide(self, state, question):
        validate_decision(state, question)
        self.calls += 1
        self.last_state = state
        return {"answer": {"type": "noul", "noul": 0.25}}


@pytest.mark.asyncio
async def test_auth_host_origin_apply_to_every_route():
    manager = Manager()
    app, _ = make_app(manager, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ENDPOINT) as client:
        for path in ["/api/status", "/api/control", "/api/decide", "/mcp", "/unknown"]:
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer " + TOKEN, "Origin": "https://external.example"})).status_code == 403
            assert (await client.get(path, headers={"Authorization": "Bearer " + TOKEN, "Host": "external.example"})).status_code == 403
        allowed = await client.get("/api/status", headers={"Authorization": "Bearer " + TOKEN})
        assert allowed.status_code == 200
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_bounded_and_invalid_bodies_never_reach_model():
    manager = Manager()
    app, _ = make_app(manager, TOKEN)
    headers = {"Authorization": "Bearer " + TOKEN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ENDPOINT, headers=headers) as client:
        for path in ["/api/decide", "/mcp", "/api/control"]:
            response = await client.post(path, content=b"x" * (1024 * 1024 + 1))
            assert response.status_code == 413
        invalid = await client.post("/api/decide", content=b"sensitive-invalid-json")
        assert invalid.status_code == 400
        assert "sensitive" not in invalid.text
        invalid = await client.post("/api/decide", json={"state": "secret evidence", "question": {"type": "choice", "instructions": "x", "criteria": ["sensitive", "sensitive"]}})
        assert invalid.status_code == 400
        assert "secret" not in invalid.text and "sensitive" not in invalid.text
    assert manager.calls == 0


@pytest.mark.asyncio
async def test_sdk_validation_failure_does_not_echo_inputs():
    mcp = SafeMCP("test")

    @mcp.tool()
    async def typed(value: int):
        return value

    with pytest.raises(Exception) as caught:
        await mcp.call_tool("typed", {"value": "sensitive caller data"})
    assert "sensitive" not in str(caught.value)
    assert "invalid_tool_call" in str(caught.value)


@pytest.mark.asyncio
async def test_real_sdk_http_negotiation_and_typed_tool():
    manager = Manager()
    policy, app = make_app(manager, TOKEN)

    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(policy), headers={"Authorization": "Bearer " + TOKEN}
    ) as http_client:
        async with streamable_http_client(ENDPOINT + "/mcp", http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "JevK5"
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == {"jevk5_decide", "jevk5_status"}
                result = await client.call_tool("jevk5_decide", {"state": "Public synthetic evidence", "question": {"type": "noul", "instructions": "Allowed?"}})
                assert not result.isError
                assert result.structuredContent["answer"]["noul"] == 0.25
                for literal in ['{"public":"literal JSON string"}', '["literal", "string"]', "null"]:
                    repeated = await client.call_tool("jevk5_decide", {"state": literal, "question": {"type": "noul", "instructions": "Allowed?"}})
                    assert not repeated.isError
                    assert manager.last_state == literal
                malformed = await client.call_tool("jevk5_decide", {"state": "sensitive data", "question": {"type": "private-criteria", "instructions": "secret"}})
                assert malformed.isError
                assert all("sensitive" not in item.text and "private-criteria" not in item.text for item in malformed.content)


@pytest.mark.asyncio
async def test_invalid_mcp_envelope_is_sanitized():
    policy, _ = make_app(Manager(), TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(policy), base_url=ENDPOINT,
                                headers={"Authorization": "Bearer " + TOKEN}) as client:
        result = await client.post("/mcp", json={"jsonrpc": "sensitive-envelope", "method": 7, "secret": "private"})
        assert result.status_code == 400
        assert "sensitive" not in result.text and "private" not in result.text
