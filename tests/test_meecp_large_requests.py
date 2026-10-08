"""Large request transport checks through the real locked SDK stdio bridge."""
import json
from pathlib import Path
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import test_meecp_gateway as fixtures
from meecp.contracts import Lifecycle


SMALL_RESPONSE_CHILD = "\n".join([
    "import sys,json",
    "tool=" + repr(fixtures.TOOL),
    "def send(x): print(json.dumps(x,separators=(',',':')),flush=True)",
    "for line in sys.stdin:",
    " m=json.loads(line); method=m.get('method'); rid=m.get('id')",
    " if 'id' not in m: continue",
    " if method=='initialize': result={'protocolVersion':'2025-11-25','capabilities':{'tools':{'listChanged':True}},'serverInfo':{'name':'fixture','version':'1'}}",
    " elif method=='tools/list': result={'tools':[tool]}",
    " elif method=='tools/call': result={'content':[{'type':'text','text':'ok'}],'structuredContent':{'value':len(m.get('params',{}).get('arguments',{}).get('value',''))},'isError':False}",
    " else: result={}",
    " send({'jsonrpc':'2.0','id':rid,'result':result})",
])


@pytest.mark.parametrize("size", [2 * 1024 * 1024, 16 * 1024 * 1024])
async def test_stdio_bridge_accepts_large_requests_with_small_response(monkeypatch, size):
    monkeypatch.setattr(fixtures, "CHILD", SMALL_RESPONSE_CHILD)
    async with fixtures.live_gateway(max_request_bytes=17 * 1024 * 1024) as (endpoint, owner, gateway):
        program = ("import asyncio; from pathlib import Path; from meecp.bridge import run_bridge; asyncio.run(run_bridge("
                   + repr(endpoint) + ",'secret',socket_path=Path(" + repr(str(owner.socket_path)) + "),max_request_bytes=17*1024*1024))" )
        parameters = StdioServerParameters(command=sys.executable, args=["-c", program],
                    env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend")})
        with open("/dev/null", "w") as errlog:
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    output = await session.call_tool("echo", {"value": "x" * size})
                    assert output.structuredContent == {"value": size}


async def test_request_over_17_mib_is_rejected_before_child_delivery():
    async with fixtures.live_gateway(max_request_bytes=17 * 1024 * 1024) as (endpoint, owner, gateway), fixtures.client(endpoint) as http:
        sid, headers = await fixtures.initialize_raw(http, endpoint)
        body = json.dumps({"jsonrpc":"2.0", "id":"too-large", "method":"tools/call",
                           "params":{"name":"echo", "arguments":{"value":"x" * (18 * 1024 * 1024)}}}).encode()
        response = await http.post(endpoint, content=body,
                                   headers={**headers, "content-length": str(len(body))})
        assert response.status_code == 413
        assert owner.launched == 1
        assert not gateway.sessions[sid].pending


async def test_default_one_mib_request_limit_remains_enforced():
    async with fixtures.live_gateway() as (endpoint, owner, gateway), fixtures.client(endpoint) as http:
        sid, headers = await fixtures.initialize_raw(http, endpoint)
        body = json.dumps({"jsonrpc":"2.0", "id":"default-too-large", "method":"tools/call",
                           "params":{"name":"echo", "arguments":{"value":"x" * (2 * 1024 * 1024)}}}).encode()
        response = await http.post(endpoint, content=body, headers={**headers, "content-length": str(len(body))})
        assert response.status_code == 413
        assert not gateway.sessions[sid].pending


@pytest.mark.parametrize("value", [0, 17 * 1024 * 1024 + 1, True, 1.5])
def test_lifecycle_request_limit_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        Lifecycle(max_request_bytes=value)


def test_lifecycle_request_limit_accepts_configured_cap():
    assert Lifecycle(max_request_bytes=17 * 1024 * 1024).max_request_bytes == 17 * 1024 * 1024
