import AppKit

/// Preserve exact represented identities even when human titles collide.
func agentPickerTitles(_ bases: [String], identities: [String], reserved: Set<String> = []) -> [String]? {
    guard bases.count == identities.count, bases.count <= 128, Set(identities).count == identities.count else { return nil }
    var disambiguate: Set<Int> = []
    for _ in 0...bases.count {
        let titles = bases.indices.map { disambiguate.contains($0) ? bases[$0] + " · " + identities[$0] : bases[$0] }
        let groups = Dictionary(grouping: bases.indices, by: { titles[$0] })
        let collisions = Set(groups.filter { $0.value.count > 1 || reserved.contains($0.key) }.flatMap(\.value))
        if collisions.isEmpty { return titles }
        let additions = collisions.subtracting(disambiguate)
        guard !additions.isEmpty else { return nil }
        disambiguate.formUnion(additions)
    }
    return nil
}
import Foundation

private let agentProviderAdapter = "openai-responses-v1"
private let agentProviderDestination = "https://api.openai.com/v1/responses"

private func providerEnvelope(_ object: [String: Any], keys: Set<String>) -> Bool {
    var expected = keys
    if object["api_version"] != nil {
        guard preparationInteger(object["api_version"]) == 1 else { return false }
        expected.insert("api_version")
    }
    return Set(object.keys) == expected
}

private func providerName(_ value: Any?) -> String? {
    guard let text = value as? String, (1...80).contains(text.unicodeScalars.count),
          !text.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
    return text
}

private func providerModels(_ value: Any?) -> [String]? {
    guard let models = value as? [String], (1...16).contains(models.count), Set(models).count == models.count,
          models.allSatisfy({ $0.range(of: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", options: .regularExpression) != nil }) else { return nil }
    return models
}

struct AgentProviderMetadata {
    let id: String
    let name: String
    let models: [String]
    let generation: String
    let credentialGeneration: String
    let credentialAvailable: Bool
    let removed: Bool
    let executionAvailable: Bool

    init?(_ object: [String: Any]) {
        let keys: Set<String> = ["provider_id", "adapter_id", "name", "models", "configuration", "generation",
            "credential_generation", "credential_status", "status", "destination", "execution_available"]
        guard Set(object.keys) == keys, let id = preparationUUID(object["provider_id"]),
              object["adapter_id"] as? String == agentProviderAdapter, let name = providerName(object["name"]),
              let models = providerModels(object["models"]), let configuration = object["configuration"] as? [String: Any], configuration.isEmpty,
              let generation = preparationUUID(object["generation"]), let credentialGeneration = preparationUUID(object["credential_generation"]),
              let credentialStatus = object["credential_status"] as? String, ["configured", "unavailable"].contains(credentialStatus),
              let status = object["status"] as? String, ["active", "removed"].contains(status),
              object["destination"] as? String == agentProviderDestination,
              let executionAvailable = preparationBoolean(object["execution_available"]),
              !executionAvailable || (credentialStatus == "configured" && status == "active") else { return nil }
        self.id = id; self.name = name; self.models = models; self.generation = generation
        self.credentialGeneration = credentialGeneration; self.credentialAvailable = credentialStatus == "configured"
        self.removed = status == "removed"
        self.executionAvailable = executionAvailable
    }
}

/// Owner-only configuration. The key exists only in the secure editor and its
/// one submitted request. This view cannot review or send captured source.
final class AgentProviderSettingsView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var onStateChange: (() -> Void)?
    var onRegistryChange: (() -> Void)?
    private(set) var busy = false
    private(set) var editing = false
    private(set) var errorCode: String?
    private var records: [AgentProviderMetadata] = []
    private var selected: AgentProviderMetadata? { records.first { $0.id == picker.selectedItem?.representedObject as? String } }
    private var editingProvider: AgentProviderMetadata?
    private var loaded = false
    private var liveExecutionEnabled = false
    private var needsRefresh = false
    private var epoch: UInt64 = 0
    private var requestEpoch: UInt64 = 0
    private let picker = NSPopUpButton()
    private let testModelPicker = NSPopUpButton()
    private let nameField = NSTextField(string: "")
    private let modelsField = NSTextField(string: "")
    private let keyField = NSSecureTextField(string: "")
    private let editor = NSStackView()
    private let status = NSTextField(wrappingLabelWithString: "Set up an OpenAI provider to choose a model for agent assistance.")
    private var refreshButton: NSButton!
    private var addButton: NSButton!
    private var editButton: NSButton!
    private var removeButton: NSButton!
    private var saveButton: NSButton!
    private var cancelButton: NSButton!
    private var testButton: NSButton!
    var blocksNavigation: Bool { busy || editing }
    var providerNames: [String: String] { Dictionary(uniqueKeysWithValues: records.filter { !$0.removed }.map { ($0.id, $0.name) }) }

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 8
        let title = NSTextField(labelWithString: "AI providers"); title.font = .boldSystemFont(ofSize: 13)
        picker.setAccessibilityLabel("Configured agent providers"); picker.target = self; picker.action = #selector(selectionChanged)
        testModelPicker.setAccessibilityLabel("Model to test"); testModelPicker.target = self; testModelPicker.action = #selector(testModelChanged)
        nameField.placeholderString = "Provider name"; nameField.setAccessibilityLabel("Provider name")
        modelsField.placeholderString = "Model IDs separated by commas"; modelsField.setAccessibilityLabel("Explicit agent model IDs")
        keyField.placeholderString = "API key"; keyField.setAccessibilityLabel("Provider API key")
        keyField.isAutomaticTextCompletionEnabled = false
        for field in [nameField, modelsField, keyField] { field.widthAnchor.constraint(greaterThanOrEqualToConstant: 320).isActive = true }
        func button(_ title: String, _ action: Selector) -> NSButton { NSButton(title: title, target: self, action: action) }
        refreshButton = button("Refresh", #selector(loadProviders)); addButton = button("Add Provider", #selector(addProvider))
        editButton = button("Edit", #selector(editProvider)); removeButton = button("Remove", #selector(removeProvider))
        saveButton = button("Save", #selector(saveProvider)); cancelButton = button("Cancel", #selector(cancelEditing))
        testButton = button("Test Connection", #selector(testConnection))
        editor.orientation = .vertical; editor.alignment = .leading; editor.spacing = 6
        let notice = workspaceLabel("Use an OpenAI API key and model IDs available to your account.\nAPI keys are saved as plain text in private files on this Mac.", size: 12, secondary: true)
        let testNotice = workspaceLabel("Test Connection sends a short sample request to OpenAI using the selected model. A small API charge may apply.", size: 12, secondary: true)
        for view in [NSTextField(labelWithString: "OpenAI Responses"),
                     workspaceStack([workspaceLabel("Profile name", size: 12), nameField], horizontal: true),
                     workspaceStack([workspaceLabel("Model IDs", size: 12), modelsField], horizontal: true),
                     workspaceStack([workspaceLabel("API key", size: 12), keyField], horizontal: true), notice,
                     workspaceStack([saveButton!, cancelButton!], horizontal: true)] { editor.addArrangedSubview(view) }
        editor.isHidden = true
        for view in [title, picker, NSStackView(views: [refreshButton!, addButton!, editButton!, removeButton!]), editor,
                     workspaceStack([workspaceLabel("Model", size: 12), testModelPicker, testButton!], horizontal: true), testNotice, status] { addArrangedSubview(view) }
        workspaceFill(editor, in: self); workspaceFill(notice, in: editor); workspaceFill(testNotice, in: self); workspaceFill(status, in: self)
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func refreshAvailability() {
        let enabled = available?() == true && !busy
        refreshButton.isEnabled = enabled && !editing
        addButton.isEnabled = enabled && loaded && !needsRefresh && !editing && records.filter { !$0.removed }.count < 16
        editButton.isEnabled = enabled && loaded && !needsRefresh && !editing && selected?.removed == false
        removeButton.isEnabled = editButton.isEnabled
        testModelPicker.isEnabled = editButton.isEnabled && selected?.credentialAvailable == true
        testButton.isEnabled = testModelPicker.isEnabled && testModelPicker.selectedItem != nil
        picker.isEnabled = enabled && !editing
        saveButton.isEnabled = enabled && editing
        cancelButton.isEnabled = !busy && editing
        for field in [nameField, modelsField, keyField] { field.isEnabled = enabled && editing }
        editor.isHidden = !editing
    }
    private func clearEditor() {
        // Also clear the live field editor, which can otherwise keep typed text.
        clearCredentialEntry(); nameField.stringValue = ""; modelsField.stringValue = ""
    }
    func clearCredentialEntry() {
        if let fieldEditor = keyField.currentEditor() { fieldEditor.string = ""; fieldEditor.undoManager?.removeAllActions() }
        keyField.stringValue = ""
    }
    func invalidate() {
        epoch &+= 1; clearEditor(); editing = false; editingProvider = nil
        loaded = false; liveExecutionEnabled = false; needsRefresh = true; records = []; picker.removeAllItems(); testModelPicker.removeAllItems()
        refreshAvailability(); onStateChange?()
    }
    /// The parent calls this only when replacing a settled/lost host connection.
    /// Its request dispatcher fences old callbacks by connection generation.
    func connectionReset() {
        requestEpoch &+= 1; busy = false; invalidate()
        status.stringValue = "Provider configuration status needs a refresh after reconnection."
    }
    private func updateModels() {
        let previous = testModelPicker.selectedItem?.title
        testModelPicker.removeAllItems(); testModelPicker.addItems(withTitles: selected?.models ?? [])
        if let previous, selected?.models.contains(previous) == true { testModelPicker.selectItem(withTitle: previous) }
    }
    @objc private func selectionChanged() { updateModels(); testModelChanged() }
    @objc private func testModelChanged() {
        errorCode = nil; status.stringValue = "Choose Test Connection to check the selected model."
        refreshAvailability()
    }
    private func updatePicker(preferred: String?) -> Bool {
        picker.removeAllItems()
        let active = records.filter { !$0.removed }
        guard let titles = agentPickerTitles(active.map { $0.name + ($0.credentialAvailable ? "" : " · Key unavailable") }, identities: active.map(\.id)) else { return false }
        for (record, title) in zip(active, titles) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: ""); item.representedObject = record.id
            picker.menu?.addItem(item)
        }
        guard picker.numberOfItems == active.count else { return false }
        if let preferred, let index = picker.itemArray.firstIndex(where: { $0.representedObject as? String == preferred }) { picker.selectItem(at: index) }
        updateModels(); return true
    }
    private func fail(_ code: String) {
        errorCode = preparationCode(code) ?? "agent_provider_request_rejected"
        let messages = ["agent_provider_authentication_failed": "OpenAI did not accept the API key. Edit the provider and try again.",
            "agent_provider_model_unavailable": "This account cannot access the selected model. Check the model ID and account permissions.",
            "agent_provider_rate_limited": "OpenAI reported a rate or usage limit. Check your account limits and try again.",
            "agent_provider_test_timeout": "The connection test timed out. Try again.",
            "agent_provider_test_busy": "Another connection test is running. Try again shortly.",
            "agent_provider_test_incomplete": "The model did not complete the short test. Check the model ID or try another model.",
            "agent_provider_test_failed": "The connection test failed. Check your connection, API key, and model ID.",
            "agent_provider_configuration_stale": "These provider settings have changed. Refresh and try again.",
            "agent_provider_configuration_invalid": "Enter a name, unique model IDs, and a valid API key."]
        status.stringValue = messages[errorCode ?? ""] ?? (errorCode ?? "agent_provider_request_rejected").replacingOccurrences(of: "_", with: " ").capitalized
        if needsRefresh { status.stringValue += " Refresh before making another change." }
        refreshAvailability(); onStateChange?()
    }
    private func beginRequest() -> (UInt64, UInt64) {
        busy = true; requestEpoch &+= 1; errorCode = nil; refreshAvailability(); onStateChange?()
        return (requestEpoch, epoch)
    }
    private func finishRequest(_ token: UInt64) -> Bool {
        guard token == requestEpoch else { return false }
        busy = false; refreshAvailability(); onStateChange?(); return true
    }
    @objc func loadProviders() {
        guard available?() == true, !blocksNavigation, let request else { return }
        needsRefresh = true
        let (token, expected) = beginRequest()
        request("v1/agent-providers", "GET", nil) { [weak self] object, code in
            guard let self, self.finishRequest(token), self.epoch == expected else { return }
            guard let object, providerEnvelope(object, keys: ["schema_version", "adapters", "providers", "live_execution_enabled"]),
                  preparationInteger(object["schema_version"]) == 1, let liveEnabled = preparationBoolean(object["live_execution_enabled"]),
                  let adapters = object["adapters"] as? [[String: Any]], adapters.count == 1, Self.validAdapter(adapters[0]),
                  let providers = object["providers"] as? [[String: Any]], providers.count <= 128 else { self.fail(code ?? "agent_provider_response_invalid"); return }
            let parsed = providers.compactMap(AgentProviderMetadata.init)
            guard parsed.count == providers.count, Set(parsed.map(\.id)).count == parsed.count,
                  parsed.filter({ !$0.removed }).count <= 16,
                  liveEnabled || !parsed.contains(where: { $0.executionAvailable }) else { self.fail("agent_provider_response_invalid"); return }
            let previous = self.selected?.id
            self.records = parsed; self.loaded = true; self.liveExecutionEnabled = liveEnabled; self.needsRefresh = false
            guard self.updatePicker(preferred: previous) else { self.fail("agent_provider_response_invalid"); return }
            self.status.stringValue = self.providerNames.isEmpty ? "No provider is configured. Choose Add Provider." :
                "Choose Test Connection to check the selected model."
            self.refreshAvailability(); self.onRegistryChange?()
        }
    }
    private static func validAdapter(_ object: [String: Any]) -> Bool {
        guard Set(object.keys) == ["adapter_id", "name", "destination", "configuration_schema", "credential_fields", "model_selection"],
              object["adapter_id"] as? String == agentProviderAdapter, object["name"] as? String == "OpenAI Responses",
              object["destination"] as? String == agentProviderDestination, object["model_selection"] as? String == "explicit_ids",
              let schema = object["configuration_schema"] as? NSDictionary,
              schema.isEqual(to: ["type": "object", "properties": [:], "additionalProperties": false]),
              let credentials = object["credential_fields"] as? [[String: Any]], credentials.count == 1,
              let field = credentials.first, Set(field.keys) == ["id", "label", "required", "max_bytes"],
              field["id"] as? String == "api_key", field["label"] as? String == "API key",
              preparationBoolean(field["required"]) == true, preparationInteger(field["max_bytes"]) == 4096 else { return false }
        return true
    }
    @objc func addProvider() {
        guard addButton.isEnabled else { return }
        clearEditor(); editingProvider = nil; editing = true; epoch &+= 1
        nameField.stringValue = "OpenAI"
        keyField.placeholderString = "API key (required)"; status.stringValue = "Add your API key and model IDs, then save."
        refreshAvailability(); onStateChange?()
    }
    @objc func editProvider() {
        guard editButton.isEnabled, let record = selected else { return }
        clearEditor(); editingProvider = record; editing = true; epoch &+= 1
        nameField.stringValue = record.name; modelsField.stringValue = record.models.joined(separator: ", ")
        keyField.placeholderString = "Leave blank to keep the saved key"
        status.stringValue = "Leave the API key blank to keep your saved key."
        refreshAvailability(); onStateChange?()
    }
    @objc func cancelEditing() {
        clearCredentialEntry()
        guard !busy else { return }
        clearEditor(); editing = false; editingProvider = nil; epoch &+= 1
        refreshAvailability(); onStateChange?()
    }
    @objc func saveProvider() {
        guard saveButton.isEnabled, request != nil else { clearCredentialEntry(); return }
        let key = keyField.stringValue
        clearCredentialEntry()
        let name = nameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let models = modelsField.stringValue.components(separatedBy: CharacterSet(charactersIn: ",\n")).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard providerName(name) != nil, providerModels(models) != nil,
              key.isEmpty && editingProvider != nil || (1...4096).contains(key.utf8.count) && key.unicodeScalars.allSatisfy({ (33...126).contains($0.value) }) else { fail("agent_provider_configuration_invalid"); return }
        var body: [String: Any] = ["schema_version": 1, "name": name, "models": models, "configuration": [:],
                                  "credentials": key.isEmpty ? [:] : ["api_key": key]]
        let existing = editingProvider
        let path: String
        if let existing { body["expected_generation"] = existing.generation; path = "v1/agent-providers/" + existing.id }
        else { body["adapter_id"] = agentProviderAdapter; path = "v1/agent-providers" }
        mutate(path: path, body: body, expected: existing, removing: false, name: name, models: models, rotating: !key.isEmpty)
    }
    @objc func removeProvider() {
        guard removeButton.isEnabled, let record = selected else { clearCredentialEntry(); return }
        clearCredentialEntry()
        mutate(path: "v1/agent-providers/" + record.id + "/remove", body: ["schema_version": 1, "expected_generation": record.generation], expected: record, removing: true, name: record.name, models: record.models, rotating: false)
    }
    @objc func testConnection() {
        guard testButton.isEnabled, let record = selected, let model = testModelPicker.selectedItem?.title,
              record.models.contains(model), let request else { return }
        let (token, viewEpoch) = beginRequest()
        status.stringValue = "Testing \(model)…"
        request("v1/agent-providers/" + record.id + "/test", "POST",
                ["schema_version": 1, "expected_generation": record.generation, "model": model]) { [weak self] object, code in
            guard let self, self.finishRequest(token), self.epoch == viewEpoch else { return }
            guard let object, providerEnvelope(object, keys: ["schema_version", "provider_id", "generation", "model", "status"]),
                  preparationInteger(object["schema_version"]) == 1, object["provider_id"] as? String == record.id,
                  object["generation"] as? String == record.generation, object["model"] as? String == model,
                  object["status"] as? String == "connected" else {
                if code == "agent_provider_configuration_stale" { self.needsRefresh = true }
                self.fail(code ?? "agent_provider_response_invalid"); return
            }
            self.status.stringValue = "Connected. \(model) accepted the test request."
            self.refreshAvailability(); self.onStateChange?()
        }
    }
    private func mutate(path: String, body: [String: Any], expected: AgentProviderMetadata?, removing: Bool, name: String, models: [String], rotating: Bool) {
        guard let request else { return }
        needsRefresh = true
        let (token, viewEpoch) = beginRequest()
        request(path, "POST", body) { [weak self] object, code in
            guard let self, self.finishRequest(token), self.epoch == viewEpoch else { return }
            self.clearEditor(); self.editing = false; self.editingProvider = nil
            guard let object, providerEnvelope(object, keys: ["schema_version", "provider"]), preparationInteger(object["schema_version"]) == 1,
                  let metadata = object["provider"] as? [String: Any], let next = AgentProviderMetadata(metadata), next.removed == removing,
                  next.name == name, next.models == models,
                  expected == nil ? !self.records.contains(where: { $0.id == next.id }) : next.id == expected?.id && next.generation != expected?.generation &&
                    (rotating ? next.credentialGeneration != expected?.credentialGeneration : next.credentialGeneration == expected?.credentialGeneration) else { self.fail(code ?? "agent_provider_response_invalid"); self.onRegistryChange?(); return }
            self.records.removeAll { $0.id == next.id }; self.records.append(next)
            guard self.updatePicker(preferred: removing ? nil : next.id) else { self.fail("agent_provider_response_invalid"); self.onRegistryChange?(); return }
            self.needsRefresh = false
            self.status.stringValue = removing ? "Provider removed." : "Provider saved. Choose Test Connection to check it."
            self.refreshAvailability(); self.onRegistryChange?()
        }
    }
}

extension AgentProviderSettingsView {
    static func connectionTestSelfTest() -> Bool {
        let id = UUID().uuidString.lowercased(), generation = UUID().uuidString.lowercased()
        let record: [String: Any] = ["provider_id": id, "adapter_id": agentProviderAdapter, "name": "Test account",
            "models": ["first-model", "second-model"], "configuration": [:], "generation": generation,
            "credential_generation": UUID().uuidString.lowercased(), "credential_status": "configured",
            "status": "active", "destination": agentProviderDestination, "execution_available": false]
        let view = AgentProviderSettingsView(); view.available = { true }
        view.records = [AgentProviderMetadata(record)!]; view.loaded = true
        guard view.updatePicker(preferred: id) else { return false }
        view.testModelPicker.selectItem(withTitle: "second-model"); view.testModelChanged()
        var requests = 0, validRequest = true
        var held: (([String: Any]?, String?) -> Void)?
        view.request = { path, method, body, completion in
            requests += 1; held = completion
            validRequest = validRequest && path == "v1/agent-providers/" + id + "/test" && method == "POST" &&
                Set(body?.keys.map { $0 } ?? []) == ["schema_version", "expected_generation", "model"] &&
                preparationInteger(body?["schema_version"]) == 1 && body?["expected_generation"] as? String == generation &&
                body?["model"] as? String == "second-model"
        }
        let connected: [String: Any] = ["api_version": 1, "schema_version": 1, "provider_id": id,
            "generation": generation, "model": "second-model", "status": "connected"]
        view.testButton.performClick(nil)
        guard view.busy, view.blocksNavigation, !view.testButton.isEnabled, !view.editButton.isEnabled,
              !view.testModelPicker.isEnabled, requests == 1, validRequest else { return false }
        view.testConnection(); guard requests == 1 else { return false }
        held?(connected, nil)
        guard !view.busy, view.testButton.isEnabled, view.status.stringValue == "Connected. second-model accepted the test request." else { return false }
        view.testButton.performClick(nil); held?(nil, "agent_provider_authentication_failed")
        guard view.errorCode == "agent_provider_authentication_failed", view.status.stringValue.contains("API key"), view.testButton.isEnabled else { return false }
        view.testButton.performClick(nil)
        var stale = connected; stale["generation"] = UUID().uuidString.lowercased()
        held?(stale, nil)
        guard view.errorCode == "agent_provider_response_invalid", !view.status.stringValue.hasPrefix("Connected") else { return false }
        view.testButton.performClick(nil); held?(nil, "agent_provider_configuration_stale")
        guard view.needsRefresh, !view.testButton.isEnabled else { return false }
        view.needsRefresh = false; view.refreshAvailability(); view.testButton.performClick(nil)
        let oldReply = held; view.connectionReset()
        oldReply?(connected, nil)
        return !view.busy && !view.loaded && view.records.isEmpty && !view.status.stringValue.hasPrefix("Connected") && validRequest
    }
    static func duplicateProviderTitlesSelfTest() -> Bool {
        let view = AgentProviderSettingsView(); view.available = { true }
        let ids = (0..<5).map { _ in UUID().uuidString.lowercased() }
        var rows: [[String: Any]] = ids.enumerated().map { index, id in
            ["provider_id": id, "adapter_id": agentProviderAdapter,
             "name": index == 3 ? "Choose a provider…" : index == 4 ? "Duplicate profile · " + ids[0] : "Duplicate profile",
             "models": ["test-model"], "configuration": [:], "generation": UUID().uuidString.lowercased(), "credential_generation": UUID().uuidString.lowercased(),
             "credential_status": "configured", "status": "active", "destination": agentProviderDestination, "execution_available": false]
        }
        let adapter: [String: Any] = ["adapter_id": agentProviderAdapter, "name": "OpenAI Responses", "destination": agentProviderDestination,
            "configuration_schema": ["type": "object", "properties": [:], "additionalProperties": false],
            "credential_fields": [["id": "api_key", "label": "API key", "required": true, "max_bytes": 4096]], "model_selection": "explicit_ids"]
        var requests = 0, edited: String?
        view.request = { path, method, body, completion in
            requests += 1
            if path == "v1/agent-providers", method == "GET", body == nil {
                completion(["schema_version": 1, "adapters": [adapter], "providers": rows, "live_execution_enabled": false], nil); return
            }
            guard method == "POST", let index = rows.firstIndex(where: { path == "v1/agent-providers/" + ($0["provider_id"] as! String) }),
                  body?["expected_generation"] as? String == rows[index]["generation"] as? String,
                  (body?["credentials"] as? [String: Any])?.isEmpty == true else { completion(nil, "invalid_test_request"); return }
            edited = rows[index]["provider_id"] as? String; rows[index]["generation"] = UUID().uuidString.lowercased()
            completion(["schema_version": 1, "provider": rows[index]], nil)
        }
        view.loadProviders()
        guard view.picker.numberOfItems == 5, Set(view.picker.itemTitles).count == 5,
              Set(view.picker.itemArray.compactMap { $0.representedObject as? String }) == Set(ids),
              view.picker.itemTitles[3] == "Choose a provider…", view.picker.itemTitles[0].contains(ids[0]) else { return false }
        view.picker.selectItem(at: 2); view.selectionChanged()
        rows.reverse(); view.loadProviders()
        guard view.selected?.id == ids[2], view.picker.numberOfItems == 5, Set(view.picker.itemTitles).count == 5 else { return false }
        view.editButton.performClick(nil); view.saveButton.performClick(nil)
        guard edited == ids[2], view.errorCode == nil, view.keyField.stringValue.isEmpty else { return false }
        view.loadProviders()
        return view.selected?.id == ids[2] && view.picker.numberOfItems == 5 && Set(view.picker.itemTitles).count == 5 && requests == 4
    }
    /// Actual AppKit actions with a synthetic owner transport, never a provider.
    static func contractSelfTest() -> Bool {
        guard connectionTestSelfTest() else { return false }
        let id = UUID().uuidString.lowercased()
        func row(_ generation: String, credential: String, removed: Bool = false) -> [String: Any] {
            ["provider_id": id, "adapter_id": agentProviderAdapter, "name": "Local test profile", "models": ["test-model"],
             "configuration": [:], "generation": generation, "credential_generation": credential,
             "credential_status": "configured", "status": removed ? "removed" : "active",
             "destination": agentProviderDestination, "execution_available": false]
        }
        let firstGeneration = UUID().uuidString.lowercased(), credential = UUID().uuidString.lowercased()
        let adapter: [String: Any] = ["adapter_id": agentProviderAdapter, "name": "OpenAI Responses", "destination": agentProviderDestination,
            "configuration_schema": ["type": "object", "properties": [:], "additionalProperties": false],
            "credential_fields": [["id": "api_key", "label": "API key", "required": true, "max_bytes": 4096]], "model_selection": "explicit_ids"]
        func list(_ providers: [[String: Any]]) -> [String: Any] { ["api_version": 1, "schema_version": 1, "adapters": [adapter], "providers": providers, "live_execution_enabled": false] }
        let view = AgentProviderSettingsView(); view.available = { true }; view.refreshAvailability()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 480), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = view
        var changes = 0, requests: [(String, String)] = [], saved = row(firstGeneration, credential: credential)
        var sawClearedSecureField = true, rotate = false
        view.onRegistryChange = { changes += 1 }
        view.request = { path, method, body, completion in
            requests.append((path, method))
            sawClearedSecureField = sawClearedSecureField && view.keyField.stringValue.isEmpty && (view.keyField.currentEditor()?.string.isEmpty ?? true)
            if method == "GET" { completion(list(requests.count == 1 ? [] : [saved]), nil); return }
            guard let body, Set(body.keys).isSubset(of: ["schema_version", "adapter_id", "expected_generation", "name", "models", "configuration", "credentials"]),
                  body["selected_paths"] == nil, body["intent"] == nil else { completion(nil, "agent_provider_request_rejected"); return }
            if path.hasSuffix("/remove") {
                guard body["expected_generation"] as? String == saved["generation"] as? String else { completion(nil, "agent_provider_configuration_stale"); return }
                saved = row(UUID().uuidString.lowercased(), credential: saved["credential_generation"] as! String, removed: true)
            } else if body["expected_generation"] != nil {
                guard body["expected_generation"] as? String == saved["generation"] as? String,
                      let credentials = body["credentials"] as? [String: String], credentials.isEmpty || rotate && credentials["api_key"] == "synthetic-only-key" else { completion(nil, "agent_provider_request_rejected"); return }
                saved = row(UUID().uuidString.lowercased(), credential: rotate ? UUID().uuidString.lowercased() : saved["credential_generation"] as! String)
            } else {
                guard body["adapter_id"] as? String == agentProviderAdapter,
                      (body["credentials"] as? [String: String])?["api_key"] == "synthetic-only-key" else { completion(nil, "agent_provider_request_rejected"); return }
            }
            completion(["schema_version": 1, "provider": saved], nil)
        }
        view.refreshButton.performClick(nil); view.addButton.performClick(nil)
        guard view.editing, view.blocksNavigation, !view.refreshButton.isEnabled else { return false }
        func visibleStorageNotice(_ parent: NSView) -> Bool {
            guard !parent.isHidden else { return false }
            if let field = parent as? NSTextField, field.stringValue.contains("API keys are saved as plain text in private files on this Mac.") { return true }
            return parent.subviews.contains(where: visibleStorageNotice)
        }
        guard !view.editor.isHidden, view.saveButton.isEnabled, visibleStorageNotice(view), requests.count == 1 else { return false }
        view.nameField.stringValue = "Local test profile"; view.modelsField.stringValue = "test-model"
        view.keyField.stringValue = "synthetic-only-key"
        guard window.makeFirstResponder(view.keyField), view.keyField.currentEditor() != nil else { return false }
        view.keyField.currentEditor()?.string = "synthetic-only-key"
        view.saveButton.performClick(nil)
        guard requests.count == 2, sawClearedSecureField, view.keyField.stringValue.isEmpty, !view.blocksNavigation,
              !view.needsRefresh, view.testButton.isEnabled, changes == 2 else { return false }
        view.refreshButton.performClick(nil); view.editButton.performClick(nil)
        guard view.editing, view.keyField.stringValue.isEmpty, view.modelsField.stringValue == "test-model" else { return false }
        view.saveButton.performClick(nil)
        guard view.records.first?.credentialGeneration == credential, view.records.first?.generation != firstGeneration else { return false }
        view.refreshButton.performClick(nil); view.editButton.performClick(nil)
        rotate = true; view.keyField.stringValue = "synthetic-only-key"; view.saveButton.performClick(nil)
        guard view.records.first?.credentialGeneration != credential, sawClearedSecureField else { return false }
        view.refreshButton.performClick(nil); view.removeButton.performClick(nil)
        guard view.records.first?.removed == true, view.keyField.stringValue.isEmpty else { return false }
        view.refreshButton.performClick(nil)
        guard view.providerNames.isEmpty, view.picker.numberOfItems == 0, requests.allSatisfy({ $0.0.hasPrefix("v1/agent-providers") }) else { return false }

        // Invalid input is rejected locally and the key is still cleared.
        view.addButton.performClick(nil); let before = requests.count
        view.nameField.stringValue = "Test"; view.modelsField.stringValue = "model,model"; view.keyField.stringValue = "synthetic-only-key"
        view.saveButton.performClick(nil)
        guard requests.count == before, view.errorCode == "agent_provider_configuration_invalid", view.keyField.stringValue.isEmpty else { return false }
        view.keyField.stringValue = "synthetic-only-key"; view.cancelButton.performClick(nil)
        guard !view.blocksNavigation, view.keyField.stringValue.isEmpty else { return false }
        view.addButton.performClick(nil); view.keyField.stringValue = "synthetic-only-key"; view.clearCredentialEntry()
        guard view.keyField.stringValue.isEmpty else { return false }
        view.keyField.stringValue = "synthetic-only-key"; view.invalidate()
        guard !view.editing, view.keyField.stringValue.isEmpty else { return false }

        // A lost create acknowledgment is never automatically replayed.
        view.request = { _, method, _, completion in completion(method == "GET" ? list([]) : nil, method == "GET" ? nil : "manager_request_failed") }
        view.loadProviders(); view.addProvider(); view.nameField.stringValue = "Test"; view.modelsField.stringValue = "model"
        view.keyField.stringValue = "synthetic-only-key"; view.saveProvider()
        guard view.errorCode == "manager_request_failed", !view.blocksNavigation, view.needsRefresh, !view.addButton.isEnabled,
              view.keyField.stringValue.isEmpty else { return false }

        // Stale list and mutation replies cannot repopulate an invalidated editor.
        var held: (([String: Any]?, String?) -> Void)?
        view.request = { _, _, _, completion in held = completion }
        view.loadProviders(); guard view.busy && view.blocksNavigation else { return false }
        view.invalidate(); held?(list([row(firstGeneration, credential: credential)]), nil)
        guard !view.busy, !view.loaded, view.records.isEmpty else { return false }
        view.request = { _, _, _, completion in completion(list([row(firstGeneration, credential: credential)]), nil) }
        view.loadProviders(); view.editProvider()
        view.request = { _, _, _, completion in held = completion }
        view.saveProvider(); guard view.busy else { return false }
        view.invalidate(); held?(["schema_version": 1, "provider": row(UUID().uuidString.lowercased(), credential: credential)], nil)
        guard !view.busy, !view.loaded, !view.editing, view.records.isEmpty, view.keyField.stringValue.isEmpty else { return false }

        // A connection replacement drops old dispatcher callbacks. It must
        // permit fresh metadata discovery while fencing those old replies.
        view.loadProviders(); let oldReply = held
        guard view.busy else { return false }
        view.connectionReset(); view.loadProviders(); let currentReply = held
        oldReply?(list([row(firstGeneration, credential: credential)]), nil)
        guard view.busy, !view.loaded, view.records.isEmpty else { return false }
        currentReply?(list([]), nil)
        guard !view.busy, view.loaded, view.records.isEmpty else { return false }

        // Host conflicts and malformed metadata stay bounded; no secret field is accepted.
        view.request = { _, _, _, completion in completion(list([row(firstGeneration, credential: credential)]), nil) }
        view.loadProviders(); view.editProvider(); view.keyField.stringValue = "synthetic-only-key"
        view.request = { _, _, _, completion in completion(nil, "agent_provider_configuration_stale") }
        view.saveProvider()
        guard view.errorCode == "agent_provider_configuration_stale", !view.editing, view.keyField.stringValue.isEmpty, view.needsRefresh else { return false }
        var invalid = row(firstGeneration, credential: credential); invalid["api_key"] = "synthetic-only-key"
        guard AgentProviderMetadata(invalid) == nil else { return false }
        invalid = row(firstGeneration, credential: credential); invalid["execution_available"] = true
        guard AgentProviderMetadata(invalid)?.executionAvailable == true else { return false }
        let liveRow = invalid
        invalid["credential_status"] = "unavailable"
        guard AgentProviderMetadata(invalid) == nil else { return false }
        invalid = liveRow; invalid["status"] = "removed"
        guard AgentProviderMetadata(invalid) == nil else { return false }
        for malformed in [1, "true"] as [Any] {
            invalid = liveRow; invalid["execution_available"] = malformed
            guard AgentProviderMetadata(invalid) == nil else { return false }
        }
        var liveList = list([liveRow]); liveList["live_execution_enabled"] = true
        view.request = { _, _, _, completion in completion(liveList, nil) }
        view.loadProviders()
        guard view.loaded, view.liveExecutionEnabled, view.records.first?.executionAvailable == true,
              !view.status.stringValue.contains("disabled"), view.keyField.stringValue.isEmpty else { return false }
        view.request = { _, _, _, completion in completion(list([liveRow]), nil) }
        view.loadProviders()
        guard view.errorCode == "agent_provider_response_invalid" else { return false }
        liveList["live_execution_enabled"] = 1
        view.request = { _, _, _, completion in completion(liveList, nil) }
        view.loadProviders()
        guard view.errorCode == "agent_provider_response_invalid" else { return false }
        view.connectionReset()
        guard !view.liveExecutionEnabled, view.records.isEmpty else { return false }
        invalid = row(firstGeneration, credential: credential); invalid["destination"] = "https://example.invalid"
        guard AgentProviderMetadata(invalid) == nil else { return false }
        var wrongVersion = list([]); wrongVersion["api_version"] = 2
        guard !providerEnvelope(wrongVersion, keys: ["schema_version", "adapters", "providers", "live_execution_enabled"]) else { return false }
        view.request = { _, _, _, completion in completion(list([invalid]), nil) }
        view.loadProviders()
        return view.errorCode == "agent_provider_response_invalid" && !view.busy && view.keyField.stringValue.isEmpty
    }
}
