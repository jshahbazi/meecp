import AppKit
import Foundation

private func piIdentifier(_ value: Any?) -> String? {
    guard let text = value as? String,
          text.range(of: "^[a-z][a-z0-9-]{0,63}$", options: .regularExpression) != nil else { return nil }
    return text
}

private func piModel(_ value: Any?, empty: Bool = false) -> String? {
    guard let text = preparationText(value, maximum: 256, empty: empty),
          !text.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) }) else { return nil }
    return text
}

struct PiSettingsMetadata {
    let configured: Bool
    let provider: String
    let model: String
    let baseURL: String?
    let runtimeAvailable: Bool
    let providers: [String]

    init?(_ object: [String: Any]) {
        guard let configured = preparationBoolean(object["configured"]),
              let provider = piIdentifier(object["provider"]), let model = piModel(object["model"], empty: true),
              let runtime = preparationBoolean(object["runtime_available"]),
              let providers = object["available_providers"] as? [String], (1...32).contains(providers.count),
              providers.allSatisfy({ piIdentifier($0) != nil }), Set(providers).count == providers.count,
              providers.contains(provider), !configured || !model.isEmpty else { return nil }
        let baseURL: String?
        if let value = object["base_url"], !(value is NSNull) {
            guard let text = preparationText(value, maximum: 512, empty: true),
                  text.isEmpty || PiSettingsView.validBaseURL(text) else { return nil }
            baseURL = text.isEmpty ? nil : text
        } else { baseURL = nil }
        self.configured = configured; self.provider = provider; self.model = model
        self.baseURL = baseURL; self.runtimeAvailable = runtime; self.providers = providers
    }

    var ready: Bool { configured && runtimeAvailable }
}

struct PiSetupJob {
    let id: String
    let importID: String
    let revision: Int
    let sourceRevision: String
    let status: String
    let stage: String
    let errorCode: String?

    init?(_ object: [String: Any]) {
        guard let id = preparationUUID(object["id"]), let importID = preparationUUID(object["import_id"]),
              let revision = preparationInteger(object["revision"]), revision > 0,
              let sourceRevision = preparationText(object["source_revision"], maximum: 256),
              let status = object["status"] as? String, ["running", "ready", "failed", "canceled"].contains(status),
              let stage = preparationCode(object["stage"]) else { return nil }
        if let value = object["code"], !(value is NSNull), preparationCode(value) == nil { return nil }
        self.id = id; self.importID = importID; self.revision = revision
        self.sourceRevision = sourceRevision
        self.status = status; self.stage = stage; self.errorCode = preparationCode(object["code"])
    }

    var active: Bool { status == "running" }
}

/// This editor handles provider credentials only. Source transfer begins from
/// the separate Set Up App action after the draft is saved.
final class PiSettingsView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var onSaved: ((PiSettingsMetadata) -> Void)?
    var onClose: (() -> Void)?
    private let provider = NSPopUpButton()
    private let model = NSTextField(string: "")
    private let baseURL = NSTextField(string: "")
    private let apiKey = NSSecureTextField(string: "")
    private let status = workspaceLabel("", size: 12, secondary: true)
    private var saveButton: NSButton!
    private var closeButton: NSButton!
    private var metadata: PiSettingsMetadata?
    private var busy = false
    private var epoch: UInt64 = 0

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 12
        provider.setAccessibilityLabel("Setup model provider")
        model.placeholderString = "Model ID"; model.setAccessibilityLabel("Setup model ID")
        baseURL.placeholderString = "Default provider endpoint"; baseURL.setAccessibilityLabel("Custom provider base URL")
        apiKey.placeholderString = "Leave blank to keep the saved key"; apiKey.setAccessibilityLabel("Setup provider API key")
        apiKey.isAutomaticTextCompletionEnabled = false
        saveButton = NSButton(title: "Save Settings", target: self, action: #selector(save))
        closeButton = NSButton(title: "Close", target: self, action: #selector(close))
        let notice = workspaceLabel("Choose the model Pi uses to set up apps. The API key is stored privately on this Mac and is never displayed. Saving settings sends no source files.", size: 12, secondary: true)
        for view in [notice, workspaceLabel("Provider", size: 12, weight: .medium), provider,
                     workspaceLabel("Model", size: 12, weight: .medium), model,
                     workspaceLabel("API key", size: 12, weight: .medium), apiKey,
                     workspaceLabel("Custom endpoint (optional)", size: 12, weight: .medium), baseURL,
                     status, workspaceStack([saveButton!, closeButton!], horizontal: true)] {
            addArrangedSubview(view); workspaceFill(view, in: self)
        }
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    static func validBaseURL(_ text: String) -> Bool {
        guard let url = URL(string: text), ["http", "https"].contains(url.scheme ?? ""),
              url.host != nil, url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              !text.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) }) else { return false }
        return url.scheme == "https" || ["localhost", "127.0.0.1", "::1"].contains(url.host ?? "")
    }

    func configure(_ value: PiSettingsMetadata) {
        epoch &+= 1; busy = false; metadata = value; clearCredentialEntry()
        provider.removeAllItems()
        let labels = ["openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "openai-compatible": "OpenAI compatible"]
        for id in value.providers {
            let item = NSMenuItem(title: labels[id] ?? id, action: nil, keyEquivalent: ""); item.representedObject = id
            provider.menu?.addItem(item)
        }
        if let item = provider.itemArray.first(where: { $0.representedObject as? String == value.provider }) { provider.select(item) }
        model.stringValue = value.model.isEmpty && value.provider == "openai" ? "gpt-5.4" : value.model
        baseURL.stringValue = value.baseURL ?? ""
        status.stringValue = value.runtimeAvailable ? "" : "The bundled Pi runtime is unavailable. Settings can be saved; app setup needs a complete runtime."
        refreshAvailability()
    }

    func clearCredentialEntry() {
        if let editor = apiKey.currentEditor() { editor.string = ""; editor.undoManager?.removeAllActions() }
        apiKey.stringValue = ""
    }

    func connectionReset() {
        epoch &+= 1; busy = false; metadata = nil; clearCredentialEntry(); refreshAvailability()
    }

    private func refreshAvailability() {
        let enabled = !busy && available?() == true && metadata != nil
        for field in [model, baseURL, apiKey] { field.isEnabled = enabled }
        provider.isEnabled = enabled; saveButton.isEnabled = enabled; closeButton.isEnabled = !busy
    }

    @objc private func close() { clearCredentialEntry(); onClose?() }

    @objc private func save() {
        guard !busy, available?() == true, metadata != nil, let request,
              let providerID = provider.selectedItem?.representedObject as? String,
              let modelID = piModel(model.stringValue), !modelID.isEmpty else {
            status.stringValue = "Choose a provider and enter a model ID."; return
        }
        let endpoint = baseURL.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard endpoint.isEmpty || Self.validBaseURL(endpoint) else {
            status.stringValue = "Use an HTTPS endpoint or an HTTP endpoint on this Mac, without a password or query."; return
        }
        var body: [String: Any] = ["provider": providerID, "model": modelID, "base_url": endpoint.isEmpty ? NSNull() : endpoint]
        if !apiKey.stringValue.isEmpty { body["api_key"] = apiKey.stringValue }
        clearCredentialEntry(); busy = true; refreshAvailability(); let expected = epoch
        request("v1/pi/settings", "PUT", body) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.busy = false
            guard let object, let value = PiSettingsMetadata(object) else {
                self.status.stringValue = "Settings could not be saved (" + (preparationCode(code) ?? "pi_settings_invalid") + ")."
                self.refreshAvailability(); return
            }
            self.configure(value); self.status.stringValue = "Settings saved."
            self.onSaved?(value)
        }
    }
}

/// Pi progress contains only host-owned stages and sanitized error codes.
/// Provider output, prompts, source contents and credentials never enter this view.
final class PiSetupView: NSStackView, NSWindowDelegate {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var beforeStart: ((@escaping (PreparationContext?) -> Void) -> Void)?
    var onReady: ((PiSetupJob) -> Void)?
    var onStateChange: (() -> Void)?
    var onJobChange: ((String?) -> Void)?
    private(set) var job: PiSetupJob?
    private(set) var errorCode: String?
    private var context: PreparationContext?
    private var eligible = false
    private var settings: PiSettingsMetadata?
    private var settingsLoading = false
    private var busy = false
    private var polling = false
    private var epoch: UInt64 = 0
    private var deliveredReady: String?
    private var restoredJobID: String?
    private let modelLabel = workspaceLabel("Loading setup model…", size: 12, secondary: true)
    private let status = workspaceLabel("Choose a source and describe the tools you want.", size: 13)
    private let notice = workspaceLabel("Set Up App sends the selected source to your configured model and builds it in a temporary workspace. Only the selected source is shared.", size: 12, secondary: true)
    private let progress = NSProgressIndicator()
    private var startButton: NSButton!
    private var cancelButton: NSButton!
    private var retryButton: NSButton!
    private var settingsButton: NSButton!
    private let settingsView = PiSettingsView()
    private var settingsWindow: NSWindow?

    var blocksNavigation: Bool { busy || job?.active == true || restoredJobID != nil }

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 10
        startButton = NSButton(title: "Set Up App", target: self, action: #selector(startSetup))
        startButton.bezelColor = .controlAccentColor
        cancelButton = NSButton(title: "Cancel Setup", target: self, action: #selector(cancelSetup))
        retryButton = NSButton(title: "Refresh Status", target: self, action: #selector(refreshStatus))
        settingsButton = NSButton(title: "Model Settings…", target: self, action: #selector(showSettings))
        progress.style = .spinning; progress.controlSize = .small; progress.isDisplayedWhenStopped = false
        let modelRow = workspaceStack([modelLabel, settingsButton!], horizontal: true)
        let statusRow = workspaceStack([progress, status], horizontal: true)
        for view in [modelRow, notice, workspaceStack([startButton!, cancelButton!, retryButton!], horizontal: true), statusRow] {
            addArrangedSubview(view); workspaceFill(view, in: self)
        }
        status.setAccessibilityLabel("App setup progress")
        settingsView.request = { [weak self] path, method, body, completion in self?.request?(path, method, body, completion) }
        settingsView.available = { [weak self] in self?.available?() == true && self?.blocksNavigation == false }
        settingsView.onSaved = { [weak self] value in self?.settings = value; self?.refreshAvailability(); self?.onStateChange?() }
        settingsView.onClose = { [weak self] in self?.settingsWindow?.close() }
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func bind(_ context: PreparationContext?, eligible: Bool) {
        if self.context != nil, self.context?.importID != context?.importID {
            guard !blocksNavigation else { return }
            job = nil; deliveredReady = nil; errorCode = nil
            onJobChange?(nil)
        }
        self.context = context; self.eligible = eligible
        refreshAvailability()
        deliverReady()
    }

    func restore(_ id: String) {
        guard preparationUUID(id) != nil, job == nil else { return }
        restoredJobID = id; refreshAvailability()
    }

    func connectionReset() {
        epoch &+= 1; settings = nil; settingsLoading = false; busy = false; polling = false
        settingsView.connectionReset(); refreshAvailability()
    }

    func refreshAvailability() {
        let enabled = available?() == true
        startButton.isEnabled = enabled && !blocksNavigation && eligible && settings?.ready == true
        cancelButton.isHidden = job?.active != true
        cancelButton.isEnabled = enabled && !busy && job?.active == true
        retryButton.isHidden = errorCode == nil && restoredJobID == nil
        retryButton.isEnabled = enabled && !busy && !polling && !settingsLoading
        settingsButton.isEnabled = enabled && !blocksNavigation && !settingsLoading && settings != nil
        if let settings {
            modelLabel.stringValue = settings.configured ? "Setup model: " + settings.provider + " / " + settings.model : "Choose your setup model in Model Settings."
            if !settings.runtimeAvailable { modelLabel.stringValue += " Pi runtime unavailable." }
        } else { modelLabel.stringValue = enabled ? "Loading setup model…" : "Setup model unavailable while the host is disconnected." }
        notice.isHidden = job?.active == true
        if busy || job?.active == true { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
    }

    private func fail(_ code: String) {
        errorCode = preparationCode(code) ?? "pi_setup_failed"
        status.stringValue = "Setup could not continue (" + errorCode! + ")."
        refreshAvailability(); onStateChange?()
    }

    private func loadSettings() {
        guard available?() == true, !settingsLoading, !busy, let request else { return }
        settingsLoading = true; let expected = epoch
        request("v1/pi/settings", "GET", nil) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.settingsLoading = false
            guard let object, let settings = PiSettingsMetadata(object) else { self.fail(code ?? "pi_settings_invalid"); return }
            self.settings = settings; self.errorCode = nil
            self.refreshAvailability(); self.onStateChange?()
        }
    }

    @objc private func showSettings() {
        guard !blocksNavigation, available?() == true, let settings else { return }
        if settingsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 440),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "App Setup Model"; window.isReleasedWhenClosed = false; window.delegate = self; window.center()
            let container = NSView(); window.contentView = container
            settingsView.translatesAutoresizingMaskIntoConstraints = false; container.addSubview(settingsView)
            NSLayoutConstraint.activate([
                settingsView.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 24),
                settingsView.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -24),
                settingsView.topAnchor.constraint(equalTo: container.topAnchor, constant: 24),
                settingsView.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -24),
            ])
            settingsWindow = window
        }
        settingsView.configure(settings); settingsWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    func clearCredentialEntry() { settingsView.clearCredentialEntry() }
    func windowWillClose(_ notification: Notification) { clearCredentialEntry() }

    @objc func startSetup() {
        guard startButton.isEnabled, !blocksNavigation, let beforeStart else { return }
        beforeStart { [weak self] context in
            guard let self, let context, self.available?() == true, !self.blocksNavigation,
                  self.context == context, self.eligible, self.settings?.ready == true, let request = self.request else { return }
            self.epoch &+= 1; self.busy = true; self.errorCode = nil; self.deliveredReady = nil
            self.status.stringValue = "Starting app setup…"; self.refreshAvailability(); self.onStateChange?()
            let expected = self.epoch
            request("v1/pi/jobs", "POST", ["import_id": context.importID, "revision": context.revision]) { [weak self] object, code in
                guard let self, expected == self.epoch else { return }
                self.busy = false
                guard let object, let job = PiSetupJob(object), job.importID == context.importID, job.revision == context.revision,
                      job.sourceRevision == context.sourceRevision else {
                    self.fail(code ?? "pi_job_invalid"); return
                }
                self.job = job; self.onJobChange?(job.id); self.showJob()
            }
        }
    }

    private func showJob() {
        guard let job else { return }
        errorCode = job.errorCode
        switch job.status {
        case "running":
            let stages = ["starting": "Starting Pi…", "preparing": "Preparing the workspace…", "running": "Pi is setting up your app…",
                          "building": "Building your app…", "verifying": "Verifying the prepared app…", "attaching": "Preparing installation review…"]
            status.stringValue = stages[job.stage] ?? "Setting up your app: " + job.stage.replacingOccurrences(of: "_", with: " ") + "…"
        case "ready": status.stringValue = "App prepared. Review its tools and access before adding it."
        case "canceled": status.stringValue = "App setup canceled. Your source and draft are kept."
        default: status.stringValue = "App setup failed (" + (job.errorCode ?? "pi_setup_failed") + ")."
        }
        refreshAvailability(); onStateChange?()
        deliverReady()
    }

    private func deliverReady() {
        guard let job, let context, job.status == "ready", deliveredReady != job.id,
              job.importID == context.importID, job.revision == context.revision,
              job.sourceRevision == context.sourceRevision else { return }
            deliveredReady = job.id; onReady?(job)
    }

    @objc func refreshStatus() { errorCode = nil; if settings == nil { loadSettings() }; poll(force: true) }

    func poll(force: Bool = false) {
        guard available?() == true else { return }
        if settings == nil, !settingsLoading, errorCode == nil { loadSettings() }
        guard !busy, !polling, let id = restoredJobID ?? job?.id, force || restoredJobID != nil || job?.active == true,
              let request else { return }
        polling = true; let expected = epoch; let previous = job
        request("v1/pi/jobs/" + id, "GET", nil) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.polling = false
            guard let object, let next = PiSetupJob(object), next.id == id,
                  previous == nil || next.importID == previous?.importID && next.revision == previous?.revision && next.sourceRevision == previous?.sourceRevision else {
                if ["pi_job_not_found", "pi_job_unavailable"].contains(code ?? "") {
                    self.restoredJobID = nil; self.job = nil; self.onJobChange?(nil)
                }
                self.fail(code ?? "pi_job_invalid"); return
            }
            self.restoredJobID = nil; self.job = next; self.showJob()
        }
    }

    @objc func cancelSetup() {
        guard available?() == true, !busy, let job, job.active, let request else { return }
        epoch &+= 1; busy = true; polling = false; refreshAvailability(); onStateChange?(); let expected = epoch
        request("v1/pi/jobs/" + job.id + "/cancel", "POST", [:]) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.busy = false
            guard let object, let next = PiSetupJob(object), next.id == job.id,
                  next.importID == job.importID, next.revision == job.revision,
                  next.sourceRevision == job.sourceRevision else { self.fail(code ?? "pi_job_invalid"); return }
            self.job = next; self.showJob()
        }
    }

    static func contractSelfTest() -> Bool {
        let importID = "11111111-2222-4333-8444-555555555555"
        let jobID = "99999999-2222-4333-8444-555555555555"
        let source = "sha256:" + String(repeating: "a", count: 64)
        let original = PreparationContext(importID: importID, revision: 1, sourceRevision: source)
        let revised = PreparationContext(importID: importID, revision: 2, sourceRevision: source)
        let settings: [String: Any] = ["configured": true, "provider": "openai", "model": "gpt-5.6",
            "base_url": NSNull(), "runtime_available": true, "available_providers": ["openai", "anthropic", "google", "openai-compatible"]]
        var missingRuntime = settings; missingRuntime["runtime_available"] = false
        guard PiSettingsMetadata(missingRuntime)?.ready == false,
              PiSettingsView.validBaseURL("http://127.0.0.1:8080/v1"),
              !PiSettingsView.validBaseURL("http://remote.example/v1"),
              !PiSettingsView.validBaseURL("https://user:private@example.com/v1") else { return false }
        var row: [String: Any] = ["id": jobID, "import_id": importID, "revision": 2, "source_revision": source,
            "status": "running", "stage": "building", "code": NSNull()]
        var invalid = row; invalid["code"] = "raw provider response"
        guard PiSetupJob(invalid) == nil else { return false }
        let view = PiSetupView(); view.available = { true }
        var postedRevision: Int?
        var stalePoll: (([String: Any]?, String?) -> Void)?
        var readyCount = 0
        view.onReady = { _ in readyCount += 1 }
        view.request = { path, method, body, completion in
            if path == "v1/pi/settings" { completion(settings, nil) }
            else if path == "v1/pi/jobs", method == "POST" {
                postedRevision = preparationInteger(body?["revision"]); completion(row, nil)
            } else if path.hasSuffix("/cancel") {
                var canceled = row; canceled["status"] = "canceled"; canceled["stage"] = "canceled"; completion(canceled, nil)
            } else { stalePoll = completion }
        }
        view.bind(original, eligible: true); view.poll()
        view.beforeStart = { completion in view.bind(revised, eligible: true); completion(revised) }
        view.startSetup()
        guard postedRevision == 2, view.blocksNavigation, view.cancelButton.isEnabled else { return false }
        view.poll(); view.cancelSetup()
        row["status"] = "ready"; row["stage"] = "ready"
        stalePoll?(row, nil)
        guard view.job?.status == "canceled", !view.blocksNavigation, readyCount == 0 else { return false }
        view.startSetup()
        guard view.job?.status == "ready", readyCount == 1 else { return false }
        view.bind(revised, eligible: false); view.bind(revised, eligible: false)
        guard readyCount == 1 else { return false }
        let resumed = PiSetupView(); resumed.available = { true }; resumed.request = { path, _, _, completion in completion(path == "v1/pi/settings" ? settings : row, nil) }
        resumed.onReady = { _ in readyCount += 1 }; resumed.restore(jobID); resumed.poll()
        guard readyCount == 1 else { return false }
        resumed.bind(revised, eligible: false)
        return readyCount == 2 && !resumed.blocksNavigation
    }
}
