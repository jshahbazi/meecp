"""Actual AppKit action to peer-checked owner UDS; no service request or job."""
import asyncio
import json
import os
from pathlib import Path
import subprocess

from meecp.supervisor import Supervisor, management_app, HostServer, bind_listener
from meecp_local_transport import bind_private_listener
import uvicorn


SWIFT = r'''
import AppKit
let _ = NSApplication.shared
func require(_ condition: Bool) { if !condition { exit(2) } }
func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap { descendants($0) } }
let fixture = try! JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
let displayed = fixture["draft"] as! [String: Any]
let grant = fixture["grant"] as! [String: Any]
require(ServiceConnectionContract.probeSummary(grant, draft: displayed) != nil)
for field in ["secret_reference", "source_revision", "endpoint", "max_calls", "response_bytes", "timeout_seconds"] {
    var changed = grant
    if field == "endpoint" { changed[field] = ["scheme":"http", "host":"127.0.0.1", "port":12346,"base_path":"/v1"] }
    else if field == "max_calls" { changed[field] = 2 }
    else if field == "response_bytes" || field == "timeout_seconds" { changed[field] = 0 }
    else { changed[field] = "invalid" }
    require(ServiceConnectionContract.probeSummary(changed, draft: displayed) == nil)
}
let view = ServiceConnectionView()
let fields = descendants(view).compactMap { $0 as? NSTextField }
fields.first { $0.accessibilityLabel() == "Service app name" }!.stringValue = "Synthetic local service"
fields.first { $0.accessibilityLabel() == "Service model ID" }!.stringValue = "synthetic-model"
fields.first { $0.accessibilityLabel() == "Service localhost port" }!.stringValue = "12345"
let secret = fixture["secret"] as! String
view.updateSecrets([(id: secret, name: "Duplicate"), (id: "00000000-0000-0000-0000-000000000001", name: "Duplicate")])
let picker = descendants(view).compactMap { $0 as? NSPopUpButton }.first!
require(picker.numberOfItems == 3); picker.selectItem(at: 1)
let save = descendants(view).compactMap { $0 as? NSButton }.first { $0.title == "Save Service Connection" }!
var online = true
view.available = { online }
var saved: ((Bool) -> Void)?
view.beforeCreate = { saved = $0 }
var requests = 0, finished = false
view.request = { path, method, body, completion in
    requests += 1
    require(["v1/service-connectors", "v1/imports/service"].contains(path))
    var request = URLRequest(url: URL(string: fixture["endpoint"] as! String)!.appendingPathComponent(path))
    request.httpMethod = method; request.timeoutInterval = 5
    request.setValue("Bearer " + (fixture["owner"] as! String), forHTTPHeaderField: "Authorization")
    if let body { request.httpBody = try! JSONSerialization.data(withJSONObject: body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
    PrivateHTTP.request(request, stateDirectory: URL(fileURLWithPath: fixture["state"] as! String), service: "manager") { data, response, _ in
        let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        DispatchQueue.main.async {
            require(response?.statusCode == 200 && object?["api_version"] as? Int == 1)
            completion(object, nil)
        }
    }
}
view.onCreated = { draft in
    let source = draft["source"] as! [String: Any]
    require(source["kind"] as? String == "service_connection" && source["locator"] == nil)
    let connection = source["connection"] as! [String: Any]
    require((connection["secret_references"] as? [String:String]) == ["api_key": secret])
    require(draft["revision"] as? Int == 1)
    finished = true
}
view.refreshAvailability()
save.performClick(nil); require(saved != nil && requests == 0 && !save.isEnabled)
save.performClick(nil); require(requests == 0)
view.connectionReset(); saved?(true); require(requests == 0)
view.updateSecrets([(id: secret, name: "Synthetic")]); picker.selectItem(at: 1)
online = false; view.refreshAvailability(); view.perform(NSSelectorFromString("create")); require(requests == 0)
online = true; view.refreshAvailability()
fields.first { $0.accessibilityLabel() == "Service localhost port" }!.stringValue = "65536"
view.perform(NSSelectorFromString("create")); require(requests == 0)
fields.first { $0.accessibilityLabel() == "Service localhost port" }!.stringValue = "12345"
save.performClick(nil); saved?(true)
let deadline = Date().addingTimeInterval(10)
while !finished && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.01)) }
require(finished && requests == 2)
exit(0)
'''


async def test_actual_service_owner_native_creation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "main.swift"; source.write_text(SWIFT)
    executable = tmp_path / "native"
    inputs = [root / "Sources/MeecpMenuBar" / name for name in ("Brand.swift", "WorkspaceStyle.swift", "PreparationForms.swift", "ServiceConnectionView.swift", "PreparationResolution.swift")]
    inputs += list((root / "Sources/PrivateTransport").glob("*.swift"))
    compiled = await asyncio.to_thread(subprocess.run, ["/usr/bin/swiftc", *map(str, inputs), str(source), "-o", str(executable)],
        capture_output=True, timeout=90)
    assert compiled.returncode == 0, "service_native_compile_failed"
    host = Supervisor(tmp_path.resolve() / "state", tmp_path / "unused")
    listener = bind_listener(); private = bind_private_listener(host.root, "manager")
    port = listener.getsockname()[1]; owner = "synthetic-native-owner"
    server = HostServer(uvicorn.Config(management_app(host, owner, port), access_log=False, log_config=None, log_level="critical", timeout_graceful_shutdown=2))
    server_task = asyncio.create_task(server.serve(sockets=[listener, private]))
    process = None
    try:
        async with asyncio.timeout(5):
            while not server.started: await asyncio.sleep(0.01)
        secret = host.secrets.create("Synthetic", "synthetic-native-credential")
        from meecp.preparation_service import references, descriptor, ServicePreparationFamily, validate_components
        from meecp.preparation_service_source import source as service_source
        from test_service_source_contracts import connection
        selected = connection(); selected["endpoint"]["port"] = 12345
        selected.update({key: ref.model_dump() for key, ref in zip(("protocol","transport_auth","task"), references())})
        selected["secret_references"]["api_key"] = secret["secret_id"]
        import uuid
        displayed = {"draft_id":str(uuid.uuid4()),"source":service_source(selected, host.secrets, validate_components=validate_components)}
        value = descriptor(displayed, {"manifest":{"id":"test.service","version":"1.0.0"},"digest":"a"*64}, name="Synthetic", memory_mb=256)
        grant = ServicePreparationFamily(host.secrets).execution_policy(value, displayed).service_probe.model_dump()
        fixture = tmp_path / "fixture.json"
        fd = os.open(fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"state":str(host.root),"endpoint":f"http://127.0.0.1:{port}","owner":owner,"secret":secret["secret_id"],
                "draft":displayed,"grant":grant}, output)
        process = await asyncio.create_subprocess_exec(str(executable), str(fixture), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH":"/usr/bin:/bin","HOME":str(tmp_path),"TMPDIR":str(tmp_path)})
        async with asyncio.timeout(15): assert await process.wait() == 0, "service_native_owner_action_failed"
        assert len(host.imports.list()) == 1
        assert not host.preparations.list() and not host.registry.list_operations()
        assert not list(host.imports.staging.iterdir())
    finally:
        if process is not None and process.returncode is None:
            process.kill(); await process.wait()
        server.should_exit = True
        await server_task
        await host.close(); listener.close(); private.close()
