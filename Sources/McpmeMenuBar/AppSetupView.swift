import AppKit

/// The normal Add App workflow. The host owns planning, execution and repair;
/// this view presents one bounded action and follows its durable job.
final class AppSetupView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var beforeStart: ((@escaping (Bool) -> Void) -> Void)?
    var onReady: (([String: Any]) -> Bool)?
    var onStateChange: (() -> Void)?
    var onConfigureProvider: (() -> Void)?
    private var context: PreparationContext?
    private var epoch: UInt64 = 0
    private var requestEpoch: UInt64 = 0
    private var loaded = false
    private var polling = false
    private var saving = false
    private var providers: [[String: Any]] = []
    private var plan: [String: Any]?
    private var pendingStart: [String: Any]?
    private var pendingContext: PreparationContext?
    private var deliveredJob: String?
    private(set) var busy = false
    private(set) var job: [String: Any]?
    private(set) var errorCode: String?
    private let providerPicker = NSPopUpButton()
    private let modelPicker = NSPopUpButton()
    private let disclosure = workspaceLabel("Checking your folder…", size: 12, secondary: true)
    private let status = workspaceLabel("", size: 13)
    private let progress = NSProgressIndicator()
    private var prepareButton: NSButton!
    private var cancelButton: NSButton!
    private var retryButton: NSButton!
    private var configureButton: NSButton!
    private var reloadButton: NSButton!
    private var providerRow: NSStackView!
    private var progressRow: NSStackView!
    private var intentPresent = false
    private var folderMessage = ""
    private var folderDisclosure = ""
    private var hasCompleteFolder = false
    private var selectedFileCount = 0
    private var selectedMetadataCount = 0
    private var selectedBytes = 0
    private static let terminal: Set<String> = ["ready", "failed", "canceled", "interrupted"]
    private var jobActive: Bool {
        guard let job else { return false }
        return !Self.terminal.contains(job["state"] as? String ?? "") || preparationBoolean(job["local_io_settled"]) != true
    }
    var blocksNavigation: Bool { busy || saving || pendingStart != nil || jobActive }
    var hasActiveJob: Bool { jobActive || pendingStart != nil || saving }
    var selectedProvider: String? { providerPicker.selectedItem?.representedObject as? String }
    var selectedModel: String? { modelPicker.selectedItem?.representedObject as? String }

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 12
        providerPicker.setAccessibilityLabel("Setup provider"); modelPicker.setAccessibilityLabel("Setup model")
        for picker in [providerPicker, modelPicker] { picker.target = self; picker.action = #selector(selectionChanged) }
        configureButton = makeButton("LLM Settings…", #selector(configureProvider))
        providerRow = workspaceStack([workspaceLabel("Use", size: 12), providerPicker, modelPicker, configureButton], horizontal: true, spacing: 8)
        prepareButton = makeButton("Prepare App", #selector(prepare)); prepareButton.bezelColor = .controlAccentColor
        cancelButton = makeButton("Cancel", #selector(cancel))
        retryButton = makeButton("Check Status", #selector(retry))
        reloadButton = makeButton("Reload", #selector(reload))
        progress.style = .spinning; progress.controlSize = .small
        progressRow = workspaceStack([progress, status], horizontal: true, spacing: 8)
        for view in [providerRow!, disclosure, progressRow!, workspaceStack([prepareButton, cancelButton, retryButton, reloadButton], horizontal: true, spacing: 8)] {
            addArrangedSubview(view); workspaceFill(view, in: self)
        }
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    private func makeButton(_ title: String, _ action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded; button.setAccessibilityLabel(title); return button
    }
    func bind(_ next: PreparationContext?, intent: String) {
        intentPresent = !intent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if next != context {
            epoch &+= 1; requestEpoch &+= 1; polling = false; context = next; loaded = false; plan = nil
            // A known owner stays cancellable even if another local view updates the draft.
            if !hasActiveJob { busy = false; job = nil; errorCode = nil; deliveredJob = nil }
        }
        refreshAvailability()
    }
    func invalidate() {
        epoch &+= 1; requestEpoch &+= 1; polling = false; busy = false; loaded = false; plan = nil
        refreshAvailability()
    }
    func registryChanged() { if !blocksNavigation { invalidate() } }
    func loadIfNeeded() { if !loaded { reload() } }
    func refreshAvailability() {
        let online = available?() == true
        let editable = online && context != nil && !blocksNavigation
        providerPicker.isEnabled = editable; modelPicker.isEnabled = editable
        configureButton.isEnabled = online && !blocksNavigation
        prepareButton.isEnabled = editable && loaded && hasCompleteFolder && selectedProvider != nil && selectedModel != nil && intentPresent && (planReady || ["intent_required", "agent_intent_required"].contains(errorCode ?? ""))
        prepareButton.isHidden = hasActiveJob
        prepareButton.title = job == nil ? "Prepare App" : "Try Again"
        cancelButton.isHidden = !jobActive
        cancelButton.isEnabled = online && !busy && jobActive
        retryButton.isHidden = pendingStart == nil && !(errorCode != nil && job != nil)
        retryButton.isEnabled = online && !busy && !saving && (pendingStart != nil || job != nil)
        reloadButton.isHidden = blocksNavigation || errorCode == nil && loaded
        reloadButton.isEnabled = editable
        progress.isHidden = !blocksNavigation
        if blocksNavigation { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
        if !blocksNavigation && job == nil && errorCode == nil {
            status.stringValue = !loaded ? "Checking your folder…" : providers.isEmpty ? "Add and test an LLM provider in Settings to continue." :
                selectedProvider == nil ? "Choose your saved provider." : selectedModel == nil ? "Choose a model." :
                !hasCompleteFolder ? folderMessage : !intentPresent ? "Describe what you want the app to do above." :
                planReady ? "Your assistant will inspect the project, create the setup, and test it. You’ll review the app before adding it." : "Checking setup requirements…"
        }
    }
    private var planReady: Bool {
        guard let plan, let context, preparationBoolean(plan["execution_available"]) == true,
              plan["import_id"] as? String == context.importID, preparationInteger(plan["revision"]) == context.revision,
              plan["source_revision"] as? String == context.sourceRevision,
              plan["provider_id"] as? String == selectedProvider, plan["model"] as? String == selectedModel,
              let expires = preparationNumber(plan["expires_at"]), expires > Date().timeIntervalSince1970 else { return false }
        return true
    }
    @objc private func configureProvider() { onConfigureProvider?() }
    @objc func reload() {
        guard available?() == true, !blocksNavigation, let context, let request else { return }
        let expected = epoch, previousProvider = selectedProvider, previousModel = selectedModel
        loaded = true; busy = true; plan = nil; errorCode = nil; status.stringValue = "Checking your folder…"; refreshAvailability()
        request("v1/agent-preparations/plans", "POST", ["import_id": context.importID, "revision": context.revision]) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.busy = false
            guard let object, object["import_id"] as? String == context.importID, preparationInteger(object["revision"]) == context.revision,
                  object["source_revision"] as? String == context.sourceRevision,
                  let providers = object["providers"] as? [[String: Any]], providers.count <= 16,
                  providers.allSatisfy({ preparationText($0["provider_id"], maximum: 128) != nil &&
                      preparationText($0["destination"], maximum: 256) != nil &&
                      ($0["generation"] == nil || $0["generation"] is NSNull || preparationUUID($0["generation"]) != nil) &&
                      ($0["models"] as? [String]).map { !$0.isEmpty && $0.count <= 16 && $0.allSatisfy { preparationText($0, maximum: 128) != nil } } == true }),
                  Set(providers.compactMap { $0["provider_id"] as? String }).count == providers.count,
                  let files = object["files"] as? [[String: Any]], files.count <= 256,
                  let metadata = object["metadata_files"] as? [[String: Any]], metadata.count <= 256,
                  files.allSatisfy({ preparationText($0["path"], maximum: 256) != nil && (1...16384).contains(preparationInteger($0["size"]) ?? 0) }),
                  metadata.allSatisfy({ preparationText($0["path"], maximum: 256) != nil && (1...16777216).contains(preparationInteger($0["size"]) ?? 0) }) else { self.fail(code ?? "app_setup_options_invalid"); return }
            self.providers = providers
            self.providerPicker.removeAllItems(); self.providerPicker.addItem(withTitle: "Choose provider…")
            let ids = providers.compactMap { $0["provider_id"] as? String }
            let names = providers.map { preparationText($0["name"], maximum: 80) ?? ($0["provider_id"] as! String) }
            guard let titles = agentPickerTitles(names, identities: ids, reserved: ["Choose provider…"]) else { self.fail("app_setup_options_invalid"); return }
            for (provider, title) in zip(providers, titles) {
                let item = NSMenuItem(title: title, action: nil, keyEquivalent: ""); item.representedObject = provider["provider_id"]
                self.providerPicker.menu?.addItem(item)
            }
            if let previousProvider, let item = self.providerPicker.itemArray.first(where: { $0.representedObject as? String == previousProvider }) { self.providerPicker.select(item) }
            else if previousProvider == nil && providers.count == 1 { self.providerPicker.selectItem(at: 1) }
            self.reloadModels(preferred: previousModel, chooseSingle: previousModel == nil)
            let textPaths = Set(files.compactMap { $0["path"] as? String })
            let otherFiles = metadata.filter { !textPaths.contains($0["path"] as? String ?? "") }
            let bytes = files.reduce(0) { $0 + (preparationInteger($1["size"]) ?? 0) }
            self.selectedFileCount = files.count; self.selectedMetadataCount = otherFiles.count; self.selectedBytes = bytes
            self.hasCompleteFolder = !files.isEmpty && files.count <= 32 && bytes <= 131072 && otherFiles.count <= 32 &&
                preparationBoolean(object["files_truncated"]) != true && preparationBoolean(object["metadata_files_truncated"]) != true
            self.folderMessage = "This folder exceeds the current setup limit (32 text files, 128 KB, and details for 32 other files). Choose the project folder without dependencies or generated files."
            if files.isEmpty { self.folderMessage = "No supported source files were found in this folder. Choose the project’s source folder." }
            self.folderDisclosure = "The selected folder is included: \(files.count) text files (\(ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)))" +
                (otherFiles.isEmpty ? "." : ", plus details for \(otherFiles.count) other files.") + " Binary contents stay on this Mac."
            self.disclosure.stringValue = self.folderDisclosure
            if let latest = object["latest_setup_job"] as? [String: Any], self.validJob(latest), latest["import_id"] as? String == context.importID,
               latest["source_revision"] as? String == context.sourceRevision { self.job = latest; self.showJob() }
            guard expected == self.epoch else { return }
            if self.jobActive { self.refreshAvailability(); self.onStateChange?(); return }
            self.loadPlan(start: false)
        }
    }
    private func reloadModels(preferred: String?, chooseSingle: Bool) {
        modelPicker.removeAllItems(); modelPicker.addItem(withTitle: "Choose model…")
        let models = providers.first { $0["provider_id"] as? String == selectedProvider }?["models"] as? [String] ?? []
        for model in models { let item = NSMenuItem(title: model, action: nil, keyEquivalent: ""); item.representedObject = model; modelPicker.menu?.addItem(item) }
        if let preferred, let item = modelPicker.itemArray.first(where: { $0.representedObject as? String == preferred }) { modelPicker.select(item) }
        else if chooseSingle && models.count == 1 { modelPicker.selectItem(at: 1) }
    }
    @objc private func selectionChanged(_ sender: NSControl) {
        guard !blocksNavigation else { return }
        if sender === providerPicker { reloadModels(preferred: nil, chooseSingle: true) }
        plan = nil; errorCode = nil; job = nil; loadPlan(start: false)
    }
    private func loadPlan(start: Bool) {
        guard available?() == true, let context, let provider = selectedProvider, let model = selectedModel,
              let providerRecord = providers.first(where: { $0["provider_id"] as? String == provider }), let request, hasCompleteFolder else {
            saving = false; refreshAvailability(); onStateChange?(); return
        }
        let expected = epoch
        loaded = true; busy = true; errorCode = nil; status.stringValue = "Checking setup requirements…"; refreshAvailability()
        request("v1/app-setups/plans", "POST", ["import_id": context.importID, "revision": context.revision, "provider_id": provider, "model": model]) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.busy = false; self.saving = false
            if self.showRequestError(object) { return }
            guard let object, preparationUUID(object["plan_id"]) != nil, preparationDigest(object["plan_digest"]) != nil,
                  object["import_id"] as? String == context.importID, preparationInteger(object["revision"]) == context.revision,
                  object["source_revision"] as? String == context.sourceRevision,
                  object["provider_id"] as? String == provider, object["model"] as? String == model,
                  object["provider_generation"] as? String == providerRecord["generation"] as? String,
                  object["destination"] as? String == providerRecord["destination"] as? String,
                  preparationInteger(object["selected_file_count"]) == self.selectedFileCount,
                  preparationInteger(object["selected_metadata_count"]) == self.selectedMetadataCount,
                  preparationInteger(object["selected_bytes"]) == self.selectedBytes,
                  preparationNumber(object["expires_at"]) != nil, preparationBoolean(object["execution_available"]) != nil,
                  let limits = object["limits"] as? [String: Any], let calls = preparationInteger(limits["max_provider_calls"]), (1...8).contains(calls),
                  let repairs = preparationInteger(limits["max_repair_attempts"]), (0...2).contains(repairs),
                  let builds = preparationInteger(limits["max_preparation_attempts"]), (1...2).contains(builds),
                  let tokens = preparationInteger(limits["max_output_tokens"]), (64...8192).contains(tokens),
                  let bytes = preparationInteger(limits["max_transfer_bytes"]), (1...2097152).contains(bytes),
                  let seconds = preparationInteger(limits["wall_seconds"]), (1...1200).contains(seconds),
                  preparationText(object["destination"], maximum: 256) != nil else { self.fail(code ?? "app_setup_plan_invalid"); return }
            self.plan = object
            let providerName = self.providerPicker.titleOfSelectedItem ?? provider
            self.disclosure.stringValue = self.folderDisclosure + "\nPrepare App shares these source files with \(providerName) (\(model)); up to \(calls) paid requests, including repairs."
            if !self.planReady {
                let blocker = (object["blockers"] as? [String])?.first ?? "app_setup_unavailable"
                self.fail(blocker); return
            }
            if self.job != nil { self.showJob() } else { self.refreshAvailability(); self.onStateChange?() }
            if start { self.startReviewedPlan() }
        }
    }
    @objc func prepare() {
        guard prepareButton.isEnabled, let beforeStart else { return }
        saving = true; status.stringValue = "Saving your instructions…"; refreshAvailability(); onStateChange?()
        beforeStart { [weak self] saved in
            guard let self else { return }
            guard saved else { self.saving = false; self.fail("app_setup_save_failed"); return }
            self.loadPlan(start: true)
        }
    }
    private func startReviewedPlan() {
        guard planReady, let plan else { fail("app_setup_plan_expired"); return }
        pendingStart = ["plan_id": plan["plan_id"]!, "plan_digest": plan["plan_digest"]!, "idempotency_key": UUID().uuidString.lowercased()]
        pendingContext = context; job = nil; sendPending()
    }
    private func sendPending() {
        guard let body = pendingStart, let request, !busy else { return }
        requestEpoch &+= 1; let expected = requestEpoch
        busy = true; errorCode = nil; status.stringValue = "Starting app setup…"; refreshAvailability(); onStateChange?()
        request("v1/app-setups/start", "POST", body) { [weak self] object, code in
            guard let self, expected == self.requestEpoch else { return }
            self.busy = false
            if self.showRequestError(object, starting: true) { return }
            guard let object, self.validJob(object), let expectedContext = self.pendingContext,
                  object["import_id"] as? String == expectedContext.importID, preparationInteger(object["revision"]) == expectedContext.revision,
                  object["source_revision"] as? String == expectedContext.sourceRevision else { self.fail(code ?? "app_setup_start_unknown"); return }
            self.pendingStart = nil; self.pendingContext = nil; self.job = object; self.showJob()
        }
    }
    private func validJob(_ object: [String: Any]) -> Bool {
        guard preparationUUID(object["job_id"]) != nil, preparationUUID(object["import_id"]) != nil,
              preparationInteger(object["revision"]).map({ $0 >= 1 }) == true,
              preparationText(object["source_revision"], maximum: 256) != nil,
              let state = object["state"] as? String,
              Self.terminal.union(["queued", "analyzing", "correcting", "resolving", "preparing", "repairing", "cancel_requested"]).contains(state),
              preparationBoolean(object["local_io_settled"]) != nil, preparationBoolean(object["installed"]) == false,
              preparationText(object["phase_message"], maximum: 1024) != nil else { return false }
        if let error = object["error"] as? [String: Any] {
            guard preparationCode(error["code"]) != nil, preparationText(error["message"], maximum: 2048) != nil,
                  preparationText(error["action"], maximum: 2048, empty: true) != nil else { return false }
        } else if object["error"] != nil && !(object["error"] is NSNull) { return false }
        return true
    }
    private func showJob() {
        guard let job else { return }
        errorCode = nil
        if let error = job["error"] as? [String: Any] {
            errorCode = error["code"] as? String
            status.stringValue = (error["message"] as? String ?? "") + "\n" + (error["action"] as? String ?? "")
        } else { status.stringValue = job["phase_message"] as? String ?? "Preparing your app…" }
        refreshAvailability(); onStateChange?()
        if job["state"] as? String == "ready", !jobActive, let id = job["job_id"] as? String, deliveredJob != id,
           let draft = job["draft"] as? [String: Any], let updated = PreparationContext(draft),
           updated.importID == job["import_id"] as? String, updated.sourceRevision == job["source_revision"] as? String,
           updated.revision >= (preparationInteger(job["revision"]) ?? Int.max) {
            deliveredJob = id
            if onReady?(draft) != true { deliveredJob = nil; fail("stale_import_revision") }
        }
    }
    func poll(force: Bool = false) {
        guard available?() == true, !busy, !polling, let job, force || jobActive, let id = job["job_id"] as? String, let request else { return }
        polling = true; let expected = requestEpoch
        request("v1/app-setups/jobs/" + id, "GET", nil) { [weak self] object, code in
            guard let self, expected == self.requestEpoch, self.job?["job_id"] as? String == id else { return }
            self.polling = false
            if self.showRequestError(object) { return }
            guard let object, self.validJob(object), object["job_id"] as? String == id,
                  object["import_id"] as? String == job["import_id"] as? String,
                  object["source_revision"] as? String == job["source_revision"] as? String else { self.fail(code ?? "app_setup_status_unavailable"); return }
            self.job = object; self.showJob()
        }
    }
    @objc func retry() { if pendingStart != nil { sendPending() } else { poll(force: true) } }
    @objc func cancel() {
        guard available?() == true, !busy, jobActive, let job, let id = job["job_id"] as? String, let request else { return }
        requestEpoch &+= 1; let expected = requestEpoch; polling = false; busy = true; refreshAvailability()
        request("v1/app-setups/jobs/" + id + "/cancel", "POST", [:]) { [weak self] object, code in
            guard let self, expected == self.requestEpoch else { return }; self.busy = false
            if self.showRequestError(object) { return }
            guard let object, self.validJob(object), object["job_id"] as? String == id,
                  object["import_id"] as? String == job["import_id"] as? String,
                  object["source_revision"] as? String == job["source_revision"] as? String else { self.fail(code ?? "app_setup_status_unavailable"); return }
            self.job = object; self.showJob()
        }
    }
    private func fail(_ code: String) {
        errorCode = preparationCode(code) ?? "app_setup_request_failed"
        status.stringValue = Self.errorMessage(errorCode!)
        if pendingStart != nil { status.stringValue += "\nChoose Check Status to reconnect to the same request." }
        refreshAvailability(); onStateChange?()
    }
    private func showRequestError(_ object: [String: Any]?, starting: Bool = false) -> Bool {
        guard let error = object?["request_error"] as? [String: Any] else { return false }
        let code = preparationCode(error["code"]) ?? "app_setup_request_failed"
        if starting && ["app_setup_changed", "app_setup_busy", "app_setup_deadline", "preparation_runner_unavailable",
                        "agent_provider_changed", "agent_provider_unavailable", "agent_execution_disabled"].contains(code) {
            // These host preconditions reject dispatch before creating an owner.
            pendingStart = nil; pendingContext = nil; plan = nil
        }
        errorCode = code
        if let message = preparationText(error["message"], maximum: 2048), let action = preparationText(error["action"], maximum: 2048, empty: true) {
            status.stringValue = message + (action.isEmpty ? "" : "\n" + action)
        } else { status.stringValue = Self.errorMessage(code) }
        if pendingStart != nil { status.stringValue += "\nChoose Check Status to reconnect to the same request." }
        refreshAvailability(); onStateChange?(); return true
    }
    static func errorMessage(_ code: String) -> String {
        ["app_setup_save_failed": "Your instructions could not be saved. Check the app name and try again.",
         "app_setup_plan_expired": "The setup review expired. Choose Reload, then Prepare App again.",
         "intent_required": "Describe what you want the app to do above, then try again.",
         "agent_intent_required": "Describe what you want the app to do above, then choose Prepare App.",
         "provider_unavailable": "This provider is unavailable. Open LLM Settings and test its connection.",
         "agent_provider_unavailable": "This provider is unavailable. Open LLM Settings and test its connection.",
         "agent_execution_disabled": "This model is unavailable for setup in this app build. Open LLM Settings and test the selected model.",
         "agent_provider_changed": "The saved provider changed. Choose Reload to review its current connection.",
         "agent_request_rejected": "The host could not approve this setup. Reload the folder and check the provider connection in LLM Settings.",
         "stale_import_revision": "This saved setup changed. Resume it from the list above to continue.",
         "app_setup_status_unavailable": "Setup is still being tracked, but its latest status could not be loaded. Choose Check Status.",
         "preparation_runner_unavailable": "This copy of mcpme cannot build apps. Install a build with the preparation runner, then try again.",
         "package_verifier_unavailable": "This copy of mcpme cannot verify prepared apps. Install a build with package verification, then try again.",
         "resolution_runner_unavailable": "This copy of mcpme cannot resolve Python dependencies. Install the signed app build with dependency resolution, then try again.",
         "app_setup_python_required": "Automatic setup could not identify a supported Python app. Choose the Python project folder, including its source and dependency files.",
         "app_setup_folder_limit": "This folder exceeds the setup limit. Choose the project folder without dependencies or generated files.",
         "app_setup_changed": "This setup changed or expired. Choose Reload, then prepare the app again.",
         "app_setup_busy": "This app already has a preparation running. Reload to resume its progress.",
         "app_setup_unavailable": "Automatic setup is unavailable for this folder on this host. Open Advanced setup for manual options."][code] ??
            "Setup could not continue (\(code)). Choose Reload to retry; your folder and instructions are saved."
    }
}

extension AppSetupView {
    /// Exercises the production handlers with delayed synthetic host replies.
    /// No provider, package builder, or installation endpoint is contacted.
    static func contractSelfTest() -> Bool {
        let id = "10000000-0000-4000-8000-000000000001", provider = "10000000-0000-4000-8000-000000000002"
        let planID = "10000000-0000-4000-8000-000000000003", jobID = "10000000-0000-4000-8000-000000000004"
        let source = "sha256:" + String(repeating: "b", count: 64)
        guard let first = PreparationContext(importID: id, revision: 1, sourceRevision: source),
              let saved = PreparationContext(importID: id, revision: 2, sourceRevision: source) else { return false }
        let view = AppSetupView(); view.available = { true }
        typealias Call = (path: String, method: String, body: [String: Any]?, complete: ([String: Any]?, String?) -> Void)
        var calls: [Call] = [], readyCount = 0
        view.request = { calls.append(($0, $1, $2, $3)) }
        view.bind(first, intent: "Expose the synthetic function")
        let options: [String: Any] = ["schema_version": 1, "import_id": id, "revision": 1, "source_revision": source,
            "providers": [["provider_id": provider, "name": "Saved provider", "models": ["synthetic-model"], "destination": "https://synthetic.invalid/responses"]],
            "files": [["path": "app.py", "size": 120]], "metadata_files": [["path": "app.py", "size": 120], ["path": "models/example.onnx", "size": 1200]]]
        func plan(_ revision: Int) -> [String: Any] {
            ["plan_id": planID, "plan_digest": String(repeating: "c", count: 64), "import_id": id, "revision": revision,
             "source_revision": source, "provider_id": provider, "model": "synthetic-model", "destination": "https://synthetic.invalid/responses",
             "selected_file_count": 1, "selected_metadata_count": 1, "selected_bytes": 120,
             "expires_at": Date().timeIntervalSince1970 + 300, "execution_available": true,
             "limits": ["max_provider_calls": 8, "max_repair_attempts": 2, "max_preparation_attempts": 2, "max_output_tokens": 8192, "max_transfer_bytes": 2097152, "wall_seconds": 1200]]
        }
        func job(_ state: String, settled: Bool, error: [String: Any]? = nil) -> [String: Any] {
            var row: [String: Any] = ["job_id": jobID, "import_id": id, "revision": 2, "source_revision": source,
                "state": state, "phase_message": "Synthetic setup phase", "local_io_settled": settled, "installed": false]
            if let error { row["error"] = error }
            return row
        }
        view.loadIfNeeded()
        guard calls.count == 1, calls[0].path == "v1/agent-preparations/plans", view.blocksNavigation else { return false }
        calls.removeFirst().complete(options, nil)
        guard calls.count == 1, calls[0].path == "v1/app-setups/plans", calls[0].body?["provider_id"] as? String == provider else { return false }
        calls.removeFirst().complete(plan(1), nil)
        guard calls.isEmpty, view.prepareButton.isEnabled, view.selectedModel == "synthetic-model",
              view.disclosure.stringValue.contains("1 text files"), view.disclosure.stringValue.contains("1 other files"),
              view.disclosure.stringValue.contains("8 paid requests"), !view.blocksNavigation else { return false }
        view.beforeStart = { complete in view.bind(saved, intent: "Saved instructions"); complete(true) }
        view.onReady = { draft in readyCount += 1; return PreparationContext(draft)?.revision == 3 }
        view.prepare()
        guard calls.count == 1, calls[0].path == "v1/app-setups/plans", preparationInteger(calls[0].body?["revision"]) == 2 else { return false }
        calls.removeFirst().complete(plan(2), nil)
        guard calls.count == 1, calls[0].path == "v1/app-setups/start", view.blocksNavigation else { return false }
        let body = calls[0].body!
        calls.removeFirst().complete(nil, "manager_request_failed")
        guard view.blocksNavigation, view.retryButton.isEnabled, view.status.stringValue.contains("same request") else { return false }
        view.retry()
        guard calls.count == 1, NSDictionary(dictionary: calls[0].body!).isEqual(to: body) else { return false }
        calls.removeFirst().complete(job("analyzing", settled: false), nil)
        guard view.blocksNavigation, view.cancelButton.isEnabled, view.prepareButton.isHidden else { return false }
        view.poll()
        guard calls.count == 1 else { return false }
        let latePoll = calls.removeFirst()
        view.cancel()
        guard calls.count == 1, calls[0].path.hasSuffix("/cancel") else { return false }
        calls.removeFirst().complete(job("canceled", settled: false), nil)
        latePoll.complete(job("ready", settled: true), nil)
        guard view.blocksNavigation, view.job?["state"] as? String == "canceled", readyCount == 0 else { return false }
        view.poll(); guard calls.count == 1 else { return false }
        calls.removeFirst().complete(job("canceled", settled: true), nil)
        guard !view.blocksNavigation, view.prepareButton.isEnabled else { return false }
        view.prepare(); guard calls.count == 1 else { return false }
        calls.removeFirst().complete(plan(2), nil)
        guard calls.count == 1, calls[0].path == "v1/app-setups/start" else { return false }
        calls.removeFirst().complete(job("preparing", settled: false), nil)
        view.poll(); guard calls.count == 1 else { return false }
        let failure = ["code": "missing_model_file", "message": "The model file is missing from the captured folder.", "action": "Refresh Folder to capture the model, then try again."]
        calls.removeFirst().complete(job("failed", settled: true, error: failure), nil)
        guard view.status.stringValue.contains(failure["message"]!), view.status.stringValue.contains(failure["action"]!), !view.blocksNavigation else { return false }
        view.poll(force: true); guard calls.count == 1 else { return false }
        var ready = job("ready", settled: true)
        ready["draft"] = ["draft_id": id, "revision": 3, "source": ["revision": source]]
        calls.removeFirst().complete(ready, nil)
        guard readyCount == 1, !view.blocksNavigation else { return false }
        view.poll(force: true); guard calls.count == 1 else { return false }
        calls.removeFirst().complete(ready, nil)
        guard readyCount == 1 else { return false }
        // Empty saved instructions do not strand newly typed instructions.
        view.job = nil; view.bind(first, intent: ""); view.loadIfNeeded(); guard calls.count == 1 else { return false }
        calls.removeFirst().complete(options, nil); guard calls.count == 1 else { return false }
        var emptyIntentPlan = plan(1); emptyIntentPlan["execution_available"] = false; emptyIntentPlan["blockers"] = ["agent_intent_required"]
        calls.removeFirst().complete(emptyIntentPlan, nil)
        guard !view.prepareButton.isEnabled else { return false }
        view.bind(first, intent: "Newly typed instructions")
        guard view.prepareButton.isEnabled, calls.isEmpty else { return false }
        // Route rejection retains the host's specific diagnosis and next action.
        guard view.showRequestError(["request_error": failure]), view.status.stringValue.contains(failure["action"]!) else { return false }
        // A folder change invalidates a delayed local preview, without dispatch.
        view.invalidate(); view.bind(first, intent: "Instructions"); view.loadIfNeeded(); guard calls.count == 1 else { return false }
        let stale = calls.removeFirst(); view.bind(nil, intent: ""); stale.complete(options, nil)
        return calls.isEmpty && !view.blocksNavigation && !view.prepareButton.isEnabled
    }
}
