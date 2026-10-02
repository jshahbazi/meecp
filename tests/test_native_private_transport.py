"""Cross-language transport regression using the actual Swift client and Uvicorn."""
import asyncio
from pathlib import Path
import platform
import subprocess
import unicodedata

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcpme.policy import LocalPolicy
from mcpme_local_transport import bind_private_listener, socket_path

ROOT = Path(__file__).resolve().parents[1]

SWIFT_CLIENT = '''import Foundation
import Darwin
let state = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let url = URL(string: "http://127.0.0.1:19385/v1/status")!
var request = URLRequest(url: url)
request.timeoutInterval = 2
request.setValue("Bearer synthetic-owner-token", forHTTPHeaderField: "Authorization")
let done = DispatchSemaphore(value: 0)
var success = false
PrivateHTTP.request(request, stateDirectory: state, service: "manager") { data, response, error in
    if let data = data, response?.statusCode == 200, error == nil,
       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        success = json["api_version"] as? Int == 1
    }
    done.signal()
}
guard done.wait(timeout: .now() + 3) == .success, success else { exit(1) }
exit(0)
'''


@pytest.fixture(scope="module")
def native_client(tmp_path_factory):
    if platform.system() != "Darwin":
        pytest.skip("native transport is macOS-only")
    directory = tmp_path_factory.mktemp("native-private-client")
    main = directory / "main.swift"
    main.write_text(SWIFT_CLIENT)
    executable = directory / "client"
    subprocess.run(["/usr/bin/swiftc", str(ROOT / "Sources/PrivateTransport/PrivateHTTP.swift"), str(main),
                    "-o", str(executable)], check=True, capture_output=True)
    return executable


@pytest.mark.parametrize("spelling", ["alias", "canonical", "nfc", "nfd"])
async def test_native_client_reaches_python_owner_socket(tmp_path, native_client, spelling):
    name = "state" if spelling in {"alias", "canonical"} else unicodedata.normalize(spelling.upper(), "café")
    state = tmp_path / name
    state.mkdir(mode=0o700)
    if spelling == "alias":
        state = Path(str(state.resolve()).replace("/private/var/", "/var/", 1))
    elif spelling == "canonical":
        state = state.resolve()
    listener = bind_private_listener(state, "manager")
    path = socket_path(state, "manager")
    received = []
    async def reply(request):
        received.append(request.headers["Authorization"])
        return JSONResponse({"api_version": 1})
    app = LocalPolicy(Starlette(routes=[Route("/v1/status", reply)]), "synthetic-owner-token", 19385)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(str(native_client), str(state),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await asyncio.wait_for(process.wait(), 5) == 0
        assert received == ["Bearer synthetic-owner-token"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
        path.unlink(missing_ok=True)
        path.parent.rmdir()
