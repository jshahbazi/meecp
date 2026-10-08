import AppKit

enum ServiceConnectionContract {
    static func reference(_ value: Any?, id: String) -> [String: Any]? {
        guard let value = value as? [String: Any], Set(value.keys) == ["id", "version", "digest"],
              value["id"] as? String == id, value["version"] as? String == "1.0.0",
              preparationDigest(value["digest"]) != nil else { return nil }
        return value
    }

    static func endpoint(_ value: Any?) -> Int? {
        guard let value = value as? [String: Any], Set(value.keys) == ["scheme", "host", "port", "base_path"],
              value["scheme"] as? String == "http", value["host"] as? String == "127.0.0.1",
              value["base_path"] as? String == "/v1", let port = preparationInteger(value["port"]),
              (1...65535).contains(port) else { return nil }
        return port
    }

    static func probeSummary(_ value: Any?, draft: [String: Any]) -> [String]? {
        let keys: Set<String> = ["schema_version", "source_revision", "descriptor_digest", "protocol", "transport_auth", "task", "endpoint",
            "method", "path", "auth_role", "secret_reference", "fixture_digest", "max_calls", "request_bytes", "response_bytes", "timeout_seconds"]
        guard let grant = value as? [String: Any], Set(grant.keys) == keys, preparationInteger(grant["schema_version"]) == 1,
              let source = draft["source"] as? [String: Any], source["kind"] as? String == "service_connection",
              let connection = source["connection"] as? [String: Any],
              grant["source_revision"] as? String == source["revision"] as? String,
              preparationDigest(grant["descriptor_digest"]) != nil, preparationDigest(grant["fixture_digest"]) != nil,
              let protocolRef = reference(grant["protocol"], id: "openai-chat-completions-v1"),
              let transport = reference(grant["transport_auth"], id: "loopback-http-bearer-v1"),
              let task = reference(grant["task"], id: "service-text-generate-v1"),
              NSDictionary(dictionary: protocolRef).isEqual(to: connection["protocol"] as? [String: Any] ?? [:]),
              NSDictionary(dictionary: transport).isEqual(to: connection["transport_auth"] as? [String: Any] ?? [:]),
              NSDictionary(dictionary: task).isEqual(to: connection["task"] as? [String: Any] ?? [:]),
              let port = endpoint(grant["endpoint"]), port == endpoint(connection["endpoint"]),
              grant["method"] as? String == "POST", grant["path"] as? String == "/v1/chat/completions",
              grant["auth_role"] as? String == "api_key", let secret = preparationUUID(grant["secret_reference"]),
              let refs = connection["secret_references"] as? [String: String], refs == ["api_key": secret],
              preparationInteger(grant["max_calls"]) == 1, preparationInteger(grant["request_bytes"]) == 32768,
              let limits = connection["limits"] as? [String: Any],
              let model = preparationText(connection["model"], maximum: 128),
              model.range(of: "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", options: .regularExpression) != nil,
              let input = preparationInteger(limits["max_input_bytes"]), (1...16384).contains(input),
              let output = preparationInteger(limits["max_output_tokens"]), (1...512).contains(output),
              let response = preparationInteger(grant["response_bytes"]), (256...1048576).contains(response),
              response == preparationInteger(limits["max_response_bytes"]),
              let timeout = preparationInteger(grant["timeout_seconds"]), (1...60).contains(timeout),
              timeout == preparationInteger(limits["timeout_seconds"]) else { return nil }
        return ["Service compatibility probe: one fixed synthetic request", "Destination: http://127.0.0.1:\(port)/v1/chat/completions",
            "Selected model ID: " + model, "Input limit: \(input) bytes; generation limit: \(output) tokens",
            "Saved secret role: api_key; reference: " + secret, "Request limit: 32,768 bytes; response limit: \(response) bytes; deadline: \(timeout) seconds",
            "Checks protocol compatibility. No model-byte identity or answer-quality claim.",
            "Cancellation stops the owned adapter and connection. The external service's work is not owned by meecp."]
    }
}

final class ServiceConnectionView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var beforeCreate: ((@escaping (Bool) -> Void) -> Void)?
    var onCreated: (([String: Any]) -> Void)?
    var onCreateSecret: (() -> Void)?
    private let name = NSTextField(), model = NSTextField(), port = NSTextField()
    private let secrets = NSPopUpButton()
    private let status = NSTextField(wrappingLabelWithString: "Connect a local OpenAI-compatible service without a source folder. Saving makes no service request.")
    private var busy = false
    private var generation: UInt64 = 0
    private var connectionGeneration: UInt64 = 0
    private var save: NSButton!

    override init(frame: NSRect) {
        super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 8
        name.placeholderString = "App name"; name.setAccessibilityLabel("Service app name")
        model.placeholderString = "Model ID"; model.setAccessibilityLabel("Service model ID")
        port.placeholderString = "Local port"; port.setAccessibilityLabel("Service localhost port")
        secrets.addItem(withTitle: "Choose saved secret…"); secrets.setAccessibilityLabel("Service bearer secret")
        let addSecret = NSButton(title: "Save a Secret…", target: self, action: #selector(createSecret))
        save = NSButton(title: "Save Service Connection", target: self, action: #selector(create))
        for view in [status, name, model, port, secrets, workspaceStack([addSecret, save], horizontal: true)] as [NSView] {
            addArrangedSubview(view); workspaceFill(view, in: self)
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func updateSecrets(_ values: [(id: String, name: String)]) {
        let selected = secrets.selectedItem?.representedObject as? String
        secrets.removeAllItems(); secrets.addItem(withTitle: "Choose saved secret…")
        for value in values {
            let item = NSMenuItem(title: value.name + " (" + value.id + ")", action: nil, keyEquivalent: "")
            item.representedObject = value.id; secrets.menu?.addItem(item)
            if value.id == selected { secrets.select(item) }
        }
    }
    func connectionReset() { connectionGeneration &+= 1; generation &+= 1; busy = false; updateSecrets([]); refreshAvailability() }
    func invalidate() { generation &+= 1; busy = false; refreshAvailability() }
    func refreshAvailability() { save.isEnabled = !busy && available?() == true }
    @objc private func createSecret() { guard !busy, available?() == true else { return }; onCreateSecret?() }
    @objc private func create() {
        guard !busy, available?() == true, let request, let portValue = Int(port.stringValue), (1...65535).contains(portValue),
              let appName = preparationText(name.stringValue, maximum: 256),
              model.stringValue.range(of: "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", options: .regularExpression) != nil,
              let secret = preparationUUID(secrets.selectedItem?.representedObject) else {
            status.stringValue = "Enter an app name, numeric local port, model ID, and saved secret."; return
        }
        let modelID = model.stringValue
        let connectionGeneration = self.connectionGeneration
        busy = true; refreshAvailability()
        beforeCreate? { [weak self] saved in
            guard let self, connectionGeneration == self.connectionGeneration else { return }
            guard saved, self.available?() == true else { self.busy = false; self.refreshAvailability(); return }
            let current = self.generation
            self.busy = true; self.refreshAvailability()
            request("v1/service-connectors", "GET", nil) { [weak self] object, _ in
                guard let self, current == self.generation else { return }
                guard self.available?() == true, let rows = object?["connectors"] as? [[String: Any]], rows.count == 1,
                      let proto = ServiceConnectionContract.reference(rows[0]["protocol"], id: "openai-chat-completions-v1"),
                      let auth = ServiceConnectionContract.reference(rows[0]["transport_auth"], id: "loopback-http-bearer-v1"),
                      let task = ServiceConnectionContract.reference(rows[0]["task"], id: "service-text-generate-v1") else {
                    self.busy = false; self.status.stringValue = "Local service connector is unavailable."; self.refreshAvailability(); return
                }
                let connection: [String: Any] = ["schema_version": 1, "protocol": proto, "transport_auth": auth, "task": task,
                    "endpoint": ["scheme": "http", "host": "127.0.0.1", "port": portValue, "base_path": "/v1"],
                    "model": modelID, "secret_references": ["api_key": secret],
                    "limits": ["timeout_seconds": 30, "max_input_bytes": 16384, "max_output_tokens": 512, "max_response_bytes": 1048576]]
                request("v1/imports/service", "POST", ["schema_version": 1, "name": appName, "intent": "Generate text with the selected local service",
                    "start": false, "start_policy": "manual", "connection": connection]) { [weak self] draft, _ in
                    guard let self, current == self.generation else { return }
                    self.busy = false; self.refreshAvailability()
                    guard let draft, let source = draft["source"] as? [String: Any], source["kind"] as? String == "service_connection",
                          NSDictionary(dictionary: source["connection"] as? [String: Any] ?? [:]).isEqual(to: connection) else {
                        self.status.stringValue = "Service connection was not confirmed. Refresh saved drafts before trying again."; return
                    }
                    self.status.stringValue = "Connection saved. Review its preparation scope below."; self.onCreated?(draft)
                }
            }
        }
    }
}
