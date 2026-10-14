"""Real automatic intake and compiled AppKit selection over an owner Unix socket."""
import asyncio
import json
import os

import pytest
import uvicorn

from meecp.supervisor import Supervisor, management_app
from meecp_local_transport import bind_private_listener, socket_path
from test_native_directory_acceptance_guards import context
from test_recipe_native_management_acceptance import file_identity, private_json


@pytest.mark.parametrize("public,kind,accepted", [
    (True, "callable", True), (True, "dynamic_mcp", True), (True, "mixed", True),
    (True, "prepared_package", False), (True, "document", False),
    (True, "invalid_draft", False),
    (False, "callable", False), (False, "mixed", True),
])
async def test_actual_intake_selection_preserves_closed_directory_kinds(native_recipe_driver, tmp_path, public, kind, accepted):
    root, state, source, home, temporary, manager, marker, fixture, output, nonce, proof, case = context(tmp_path, public)
    (source / "worker.py").write_text("def double(n: int) -> dict[str, int]:\n    return {'value': n * 2}\n")
    (source / "LICENSE").write_text("Synthetic fixture dedicated to the public domain under CC0.\n")
    if kind == "dynamic_mcp":
        (source / "pyproject.toml").write_text('[project]\nname="synthetic"\nversion="1.0.0"\ndependencies=["mcp==1.30.0"]\n')
    if kind == "mixed":
        (source / "unknown.bin").write_bytes(b"\x00\xff")
    host = Supervisor(state, root / "unused-reaper")
    token = "synthetic-native-intake-owner-token"
    manager.unlink()
    private_json(manager, {"api_version": 1, "supervisor_pid": os.getpid(),
        "endpoint": "http://127.0.0.1:19384", "token": token})
    proof["manager_file"] = file_identity(manager)
    private_json(marker, proof); private_json(fixture, case)
    owner = management_app(host, token, 19384)
    routes = []
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await owner(scope, receive, send)
        routes.append((scope["method"], scope["path"]))
        rewrite_import = (kind in {"prepared_package", "document", "invalid_draft"}
            and scope["method"] == "POST" and scope["path"] == "/v1/imports")
        response_start = None
        response_body = bytearray()
        async def observe(message):
            nonlocal response_start
            # Preserve HTTP framing so these controls exercise source-kind and
            # draft-envelope rejection rather than a transport parsing failure.
            if rewrite_import:
                if message["type"] == "http.response.start":
                    response_start = message
                    return
                if message["type"] == "http.response.body":
                    response_body.extend(message["body"])
                    if message.get("more_body", False):
                        return
                    value = json.loads(response_body)
                    if kind == "invalid_draft":
                        value.pop("draft_id")
                    else:
                        value["source"]["kind"] = kind
                    body = json.dumps(value).encode()
                    assert response_start is not None
                    headers = [(k, v) for k, v in response_start["headers"] if k.lower() != b"content-length"]
                    headers.append((b"content-length", str(len(body)).encode()))
                    await send({**response_start, "headers": headers})
                    message = {**message, "body": body}
            await send(message)
        await owner(scope, receive, observe)
    listener = bind_private_listener(state, "manager"); socket = socket_path(state, "manager")
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[listener])); child = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        flag = "--public-directory-native-acceptance" if public else "--directory-native-acceptance"
        child = await asyncio.create_subprocess_exec(str(native_recipe_driver), flag, str(fixture),
            env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(temporary), "MCPME_STATE_DIR": str(state)},
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # Rejection must settle promptly; the native stage's 30-second deadline
        # must not substitute for a completed callback with no usable draft.
        await asyncio.wait_for(child.wait(), 50 if accepted else 10)
        assert child.returncode == 1  # Source-only host keeps all execution closed.
        receipt = json.loads(output.read_bytes())
        assert receipt.get("native_directory_selection", False) is accepted
        assert receipt["passed"] is False
        assert (receipt["stage"] != "source_selection") is accepted
        assert len(host.imports.list()) == 1
        draft = host.imports.list()[0]
        actual = host.imports.inspect(draft["draft_id"])
        assert actual["source"]["kind"] == ("application_directory" if kind == "mixed" else "local_folder")
        assert ("POST", "/v1/imports") in routes
        assert not host.preparations.list() and not host.resolutions.list()
        assert not any(path.endswith("/start") for _, path in routes)
    finally:
        if child is not None and child.returncode is None:
            child.kill(); await child.wait()
        server.should_exit = True; await asyncio.wait_for(task, 5)
        listener.close(); socket.unlink(missing_ok=True); socket.parent.rmdir()
