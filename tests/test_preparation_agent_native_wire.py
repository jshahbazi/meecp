"""Production Swift parsing and private transport against the real owner router."""
import asyncio
import json
from pathlib import Path
import platform
import subprocess
import uuid

import pytest
import uvicorn

from mcpme.grants import read_credential
from mcpme.supervisor import Supervisor, management_app
from mcpme_local_transport import bind_private_listener, socket_path
from test_preparation_agent_providers import SYNTHETIC, choices, update

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def agent_native_wire_client(tmp_path_factory):
    if platform.system() != "Darwin":
        pytest.skip("production native transport requires macOS")
    source = (ROOT / "Sources/McpmeMenuBar/main.swift").read_text()
    # Compile the actual production response gate and its two validators, so
    # this control cannot silently replace api_version with schema_version.
    helpers = source[source.index("private func integer("):source.index("private func fallbackConnectionName(")]
    request = source[source.index("    private func request("):]
    parser = request[request.index("            var object:"):request.index("            DispatchQueue.main.async")]
    assert 'integer(parsed["api_version"]) == 1' in parser
    directory = tmp_path_factory.mktemp("agent-native-wire")
    main = directory / "main.swift"
    main.write_text('''import Foundation
import CoreFoundation
import Darwin
private let responseLimit = 1_048_576
''' + helpers + '''
func parse(_ data: Data?, _ response: HTTPURLResponse?) -> ([String: Any]?, String) {
''' + parser + '''
    return (object, code)
}
// These false-version controls must fail under the same production parser.
for json in ["{\\"schema_version\\":1}", "{\\"api_version\\":true}", "{\\"api_version\\":2}"] {
    let reply = HTTPURLResponse(url: URL(string: "http://127.0.0.1:19384")!, statusCode: 200, httpVersion: nil, headerFields: nil)
    guard parse(Data(json.utf8), reply).0 == nil else { exit(2) }
}
let state = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let scenarios = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))) as! [[String: Any]]
for item in scenarios {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:19384" + (item["path"] as! String))!)
    request.httpMethod = item["method"] as? String ?? "POST"
    request.timeoutInterval = 3
    request.setValue("Bearer " + (item["token"] as! String), forHTTPHeaderField: "Authorization")
    if let body = item["body"] {
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    }
    let done = DispatchSemaphore(value: 0)
    var passed = false
    PrivateHTTP.request(request, stateDirectory: state, service: "manager") { data, response, error in
        let result = parse(data, response)
        if error == nil, response?.statusCode == item["status"] as? Int {
            if let expected = item["error"] as? String {
                passed = result.0 == nil && result.1 == expected
            } else if let object = result.0 {
                passed = integer(object["schema_version"]) == 1 && object[item["field"] as! String] != nil
            }
        }
        done.signal()
    }
    guard done.wait(timeout: .now() + 5) == .success, passed else { exit(3) }
}
exit(0)
''')
    executable = directory / "client"
    result = subprocess.run(["/usr/bin/swiftc", str(ROOT / "Sources/PrivateTransport/PrivateHTTP.swift"),
        str(main), "-o", str(executable)], capture_output=True)
    assert result.returncode == 0, "native wire fixture compilation failed"
    return executable


async def test_production_native_parser_consumes_owner_and_denial_envelopes(tmp_path, agent_native_wire_client, monkeypatch):
    from mcpme.preparation_agent_backend import OpenAIResponsesBackend
    async def forbidden(*args, **kwargs):
        raise AssertionError("wire acceptance attempted provider dispatch")
    monkeypatch.setattr(OpenAIResponsesBackend, "complete", forbidden)
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    listener = bind_private_listener(host.root, "manager")
    socket = socket_path(host.root, "manager")
    server = uvicorn.Server(uvicorn.Config(management_app(host, "synthetic-owner", 19384),
        log_config=None, access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        source = tmp_path / "source"; source.mkdir()
        (source / "worker.py").write_text("def echo(value): return value\n")
        draft = host.imports.create(source, name="Synthetic", intent="Review selected function")
        registry = host.agent_provider_registry
        row = registry.create(choices())["provider"]
        remove_row = registry.create(choices(name="Remove fixture"))["provider"]
        recipe = next(value for value in host.recipes.list() if value["state"] == "enabled")
        scope = {"import_id": draft["draft_id"], "revision": 1, "recipe": {
            "id": recipe["recipe_id"], "version": recipe["version"], "digest": recipe["digest"]},
            "provider_id": row["provider_id"], "model": row["models"][0], "selected_paths": ["worker.py"]}
        plan = await host.agent_preparations.plan(scope)
        scenarios = []
        def add(path, *, body=None, field=None, error=None, status=200, method="POST", token="synthetic-owner"):
            value = {"path": path, "method": method, "token": token, "status": status}
            value.update({"error": error} if error else {"field": field})
            if body is not None:
                value["body"] = body
            scenarios.append(value)
        add("/v1/agent-providers", method="GET", field="providers")
        add("/v1/agent-providers", body=choices(name="Native created"), field="provider")
        add("/v1/agent-preparations/plans", body={"import_id": draft["draft_id"], "revision": 1}, field="providers")
        add("/v1/agent-preparations/plans", body=scope, field="plan_id")
        add("/v1/agent-preparations/start", body={"action": "analyze", "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"], "idempotency_key": str(uuid.uuid4())}, error="agent_request_rejected", status=400)
        add("/v1/agent-providers/" + row["provider_id"], body=update(row, name="Native renamed"), field="provider")
        add("/v1/agent-providers/" + row["provider_id"], body=update(row), error="agent_provider_configuration_stale", status=409)
        add("/v1/agent-providers/" + remove_row["provider_id"] + "/remove", body={"schema_version": 1,
            "expected_generation": remove_row["generation"]}, field="provider")
        add("/v1/agent-providers/" + str(uuid.uuid4()) + "/remove", body={"schema_version": 1,
            "expected_generation": str(uuid.uuid4())}, error="agent_provider_not_found", status=404)
        add("/v1/agent-providers", body=choices(endpoint=SYNTHETIC), error="agent_provider_request_rejected", status=400)
        add("/v1/agent-preparations/plans", body={}, error="agent_request_rejected", status=400)
        for mode in ("read", "prepare"):
            token = read_credential(Path(host.grants.issue({"mode": mode})["credential_file"]))["token"]
            add("/v1/agent-providers", method="GET", error="grant_scope", status=403, token=token)
            add("/v1/agent-preparations/plans", body={}, error="grant_scope", status=403, token=token)
        fixture = tmp_path / "synthetic-requests.json"
        fixture.write_text(json.dumps(scenarios)); fixture.chmod(0o600)
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.01)
        process = await asyncio.create_subprocess_exec(str(agent_native_wire_client), str(host.root), str(fixture),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await asyncio.wait_for(process.wait(), 15) == 0
        records = registry.list()["providers"]
        assert len(records) == 3 and next(value for value in records if value["provider_id"] == row["provider_id"])["name"] == "Native renamed"
        assert not host.agent_preparations.tasks and not host.preparations.list()
        assert not host.agent_preparations.live_enabled and not host.preparation_runner.enabled and not host.resolution_runner.enabled
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close(); socket.unlink(missing_ok=True)
        await host.close()
