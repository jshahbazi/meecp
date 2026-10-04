import AppKit

private struct AgentRecipeChoice {
    let recipe: [String: Any]
    let label: String
    let compatible: Bool
    let recommended: Bool
    var identity: String { (recipe["id"] as! String) + "\u{1f}" + (recipe["version"] as! String) + "\u{1f}" + (recipe["digest"] as! String) }
    init?(_ object: [String: Any], recipes: [[String: Any]]) {
        guard Set(object.keys) == ["recipe", "label", "compatibility", "recommended"],
              let recipe = object["recipe"] as? [String: Any],
              Set(recipe.keys) == ["id", "version", "digest"], preparationText(recipe["id"], maximum: 128) != nil,
              preparationText(recipe["version"], maximum: 128) != nil, preparationDigest(recipe["digest"]) != nil,
              recipes.contains(where: { NSDictionary(dictionary: $0).isEqual(to: recipe) }),
              let label = preparationText(object["label"], maximum: 96),
              !label.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) || CharacterSet.newlines.contains($0) }),
              let compatibility = object["compatibility"] as? String, ["compatible", "unresolved"].contains(compatibility),
              let recommended = preparationBoolean(object["recommended"]), !recommended || compatibility == "compatible" else { return nil }
        self.recipe = recipe; self.label = label; self.compatible = compatibility == "compatible"; self.recommended = recommended
    }
}

struct AgentPreparationJob {
    let id: String
    let context: PreparationContext
    let state: String
    let settled: Bool
    let proposalDigest: String?
    let candidateDigest: String?
    let errorCode: String?
    static let terminal: Set<String> = ["succeeded", "canceled", "failed", "interrupted", "adopted"]
    var active: Bool { !Self.terminal.contains(state) || !settled }

    init?(_ object: [String: Any]) {
        guard preparationInteger(object["schema_version"]) == 1,
              let id = preparationUUID(object["job_id"]), let importID = preparationUUID(object["import_id"]),
              let revision = preparationInteger(object["revision"]), let source = preparationText(object["source_revision"], maximum: 256),
              let context = PreparationContext(importID: importID, revision: revision, sourceRevision: source),
              let state = preparationCode(object["state"]),
              Self.terminal.union(["queued", "snapshotting", "analyzing", "validating", "cancel_requested", "adopting"]).contains(state),
              let settled = preparationBoolean(object["local_io_settled"]),
              object["verification"] is NSNull, preparationBoolean(object["installed"]) == false else { return nil }
        let proposal = preparationDigest(object["proposal_digest"])
        guard object["proposal_digest"] == nil || object["proposal_digest"] is NSNull || proposal != nil else { return nil }
        let candidate = object["candidate"] as? [String: Any]
        let digest = candidate.flatMap { preparationDigest($0["descriptor_digest"]) }
        guard object["candidate"] == nil || object["candidate"] is NSNull || digest != nil,
              candidate == nil || state == "succeeded" && settled && proposal != nil else { return nil }
        let code = preparationCode(object["error_code"])
        guard object["error_code"] == nil || object["error_code"] is NSNull || code != nil else { return nil }
        self.id = id; self.context = context; self.state = state; self.settled = settled
        proposalDigest = proposal; candidateDigest = digest; errorCode = code
    }
}

extension AgentPreparationView {
    static func contractSelfTest() -> Bool {
        let importID = "10000000-0000-4000-8000-000000000001", planID = "10000000-0000-4000-8000-000000000002"
        let jobID = "10000000-0000-4000-8000-000000000003", digest = String(repeating: "a", count: 64)
        let source = "sha256:" + digest
        guard let context = PreparationContext(importID: importID, revision: 1, sourceRevision: source) else { return false }
        let recipe: [String: Any] = ["id": "test.agent", "version": "1.0.0", "digest": digest]
        func row(_ state: String, settled: Bool = true, candidate: Bool = false) -> [String: Any] {
            ["schema_version": 1, "job_id": jobID, "plan_id": planID, "import_id": importID, "revision": 1,
             "source_revision": source, "state": state, "local_io_settled": settled,
             "proposal_digest": candidate || state == "adopted" ? digest as Any : NSNull(),
             "candidate": candidate ? ["descriptor_digest": digest, "recipe": recipe] as Any : NSNull(),
             "verification": NSNull(), "installed": false, "error_code": NSNull()]
        }
        let view = AgentPreparationView(); view.available = { true }; view.bind(context, saved: true)
        var starts: [[String: Any]] = [], adoptions = 0, requests: [String] = []
        view.onUpdatedDraft = { draft in
            guard PreparationContext(draft)?.revision == 2 else { return false }; adoptions += 1; return true
        }
        view.request = { path, method, body, completion in
            requests.append(method + " " + path)
            if path.hasSuffix("/plans"), body?.count == 2 {
                completion(["schema_version": 1, "import_id": importID, "revision": 1, "source_revision": source,
                    "providers": [["provider_id": "configured", "name": "Configured test", "generation": NSNull(), "models": ["configured-model"]]],
                    "files": [["path": "worker.py", "sha256": digest, "size": 32]],
                    "metadata_files": [["path": "uv.lock", "sha256": digest, "size": 32768],
                        ["path": "worker.py", "sha256": digest, "size": 32]], "recipes": [recipe], "latest_job": NSNull()], nil)
            } else if path.hasSuffix("/plans"), let body {
                completion(["schema_version": 1, "import_id": importID, "revision": 1, "source_revision": source,
                    "plan_id": planID, "plan_digest": digest, "expires_at": Date().timeIntervalSince1970 + 300,
                    "provider_id": "configured", "model": "configured-model", "recipe": recipe,
                    "selected_files": [["path": "worker.py", "sha256": digest, "bytes": 32]], "limits": body["limits"]!,
                    "selected_metadata": [["path": "uv.lock", "sha256": digest, "bytes": 32768]],
                    "guide_digest": digest, "guide": ["schema_version": 1, "id": "test.guide", "component": recipe,
                        "roles": ["interface", "dependencies", "fixtures", "adapter"].map { ["role": $0, "instructions": "Public contract", "depends_on": []] as [String: Any] }],
                    "destination": "https://api.openai.com/v1/responses", "simulated": true,
                    "monetary_ceiling_enforced": false, "execution_available": true], nil)
            } else if path.hasSuffix("/start"), body?["action"] as? String == "analyze", let body {
                starts.append(body)
                completion(starts.count == 1 ? nil : row("analyzing", settled: false), starts.count == 1 ? "manager_request_failed" : nil)
            } else if path.hasSuffix("/start"), body?["action"] as? String == "adopt-candidate" {
                completion(["schema_version": 1, "job": row("adopted"),
                    "draft": ["draft_id": importID, "revision": 2, "source": ["revision": source]],
                    "descriptor_digest": digest, "requires_preparation_review": true], nil)
            } else if path.hasSuffix("/cancel") { completion(row("canceled"), nil) }
            else { completion(row("succeeded", candidate: true), nil) }
        }
        view.loadButton.performClick(nil)
        guard view.fileControls.count == 1, view.metadataControls.count == 2, view.metadataControls[0].state == .off, !view.sendButton.isEnabled else { return false }
        guard view.scopeRequest() == nil else { return false }
        guard view.recipePicker.selectedItem?.representedObject == nil else { return false }
        view.recipePicker.selectItem(at: 1)
        view.fileControls[0].performClick(nil)
        view.metadataControls[1].performClick(nil)
        guard view.fileControls[0].state == .off, view.metadataControls[1].state == .on else { return false }
        view.fileControls[0].performClick(nil)
        guard view.metadataControls[1].state == .off, view.fileControls[0].state == .on else { return false }
        view.metadataControls[0].performClick(nil)
        guard view.scopeRequest() == nil, !view.reviewButton.isEnabled else { return false }
        view.modelPicker.selectItem(withTitle: "configured-model"); view.selectionChanged(view.modelPicker)
        view.reviewButton.performClick(nil)
        guard view.sendButton.isEnabled, view.reviewLabel.stringValue.contains("worker.py"),
              view.reviewLabel.stringValue.contains("Identity-only transfer"), view.reviewLabel.stringValue.contains("uv.lock"),
              view.reviewLabel.stringValue.contains("test.guide"),
              view.reviewLabel.stringValue.contains("dollar ceiling"), view.reviewLabel.stringValue.contains("Simulated") else { return false }
        view.sendButton.performClick(nil)
        guard view.blocksNavigation, view.pendingStart != nil, starts.count == 1 else { return false }
        view.retryButton.performClick(nil)
        guard starts.count == 2, starts[0]["idempotency_key"] as? String == starts[1]["idempotency_key"] as? String,
              view.pendingStart == nil, view.job?.state == "analyzing", view.cancelButton.isEnabled else { return false }
        view.poll(force: true)
        guard view.adoptButton.isEnabled, !view.blocksNavigation else { return false }
        view.adoptButton.performClick(nil)
        guard adoptions == 1, view.job?.state == "adopted", requests.filter({ $0 == "POST v1/agent-preparations/start" }).count == 3 else { return false }
        var wrong = row("succeeded", candidate: true); wrong["installed"] = true
        guard AgentPreparationJob(wrong) == nil else { return false }
        wrong = row("succeeded", candidate: true); wrong["verification"] = ["succeeded": true]
        guard AgentPreparationJob(wrong) == nil else { return false }
        wrong = row("analyzing", settled: false, candidate: true)
        guard AgentPreparationJob(wrong) == nil else { return false }
        // Unknown start acknowledgment remains owned even if the parent draft
        // changes before the acknowledgment; it can still be canceled exactly.
        let race = AgentPreparationView(); race.available = { true }; race.bind(context, saved: true)
        race.plan = view.plan
        race.pendingStart = starts[0]; race.pendingContext = context
        race.bind(PreparationContext(importID: importID, revision: 2, sourceRevision: source), saved: true)
        race.request = { path, _, _, complete in complete(row(path.hasSuffix("/cancel") ? "canceled" : "analyzing", settled: path.hasSuffix("/cancel")), nil) }
        race.retryButton.performClick(nil)
        guard race.job?.context == context, race.blocksNavigation, !race.adoptButton.isEnabled else { return false }
        race.cancelButton.performClick(nil)
        guard race.job?.state == "canceled" && race.job?.settled == true && !race.blocksNavigation else { return false }
        // A GET sent before cancellation cannot overwrite its later settled
        // acknowledgment or admit the stale candidate into this view.
        let delayed = AgentPreparationView(); delayed.available = { true }; delayed.bind(context, saved: true)
        delayed.job = AgentPreparationJob(row("analyzing", settled: false)); delayed.refreshAvailability()
        var held: (([String: Any]?, String?) -> Void)?
        delayed.request = { path, method, _, complete in
            if method == "GET" { held = complete }
            else if path.hasSuffix("/cancel") { complete(row("canceled"), nil) }
        }
        delayed.poll(force: true); delayed.cancelButton.performClick(nil)
        guard delayed.job?.state == "canceled", delayed.job?.settled == true, held != nil else { return false }
        held?(row("succeeded", candidate: true), nil)
        guard delayed.job?.state == "canceled", !delayed.adoptButton.isEnabled else { return false }
        // The exact current job identity also fences a late reply from a prior
        // terminal job even if a replacement is independently restored.
        delayed.job = AgentPreparationJob(row("succeeded", candidate: true)); delayed.refreshAvailability()
        delayed.poll(force: true)
        var replacement = row("analyzing", settled: false)
        replacement["job_id"] = "10000000-0000-4000-8000-000000000004"
        delayed.job = AgentPreparationJob(replacement); delayed.refreshAvailability()
        held?(row("succeeded", candidate: true), nil)
        guard delayed.job?.id == replacement["job_id"] as? String && !delayed.adoptButton.isEnabled else { return false }
        // A configured profile changing after options load requires a new review.
        let generation = UUID().uuidString.lowercased(), nextGeneration = UUID().uuidString.lowercased()
        var reviewed: [String: Any]?
        view.request = { path, method, body, complete in
            guard path.hasSuffix("/plans"), method == "POST", let body else { complete(nil, "invalid_test_request"); return }
            reviewed = ["schema_version": 1, "import_id": importID, "revision": 1, "source_revision": source,
                "plan_id": planID, "plan_digest": digest, "expires_at": Date().timeIntervalSince1970 + 300,
                "provider_id": "configured", "provider_generation": nextGeneration, "model": "configured-model", "recipe": recipe,
                "selected_files": [["path": "worker.py", "sha256": digest, "bytes": 32]], "selected_metadata": [["path": "uv.lock", "sha256": digest, "bytes": 32768]],
                "limits": body["limits"]!, "destination": "https://api.openai.com/v1/responses", "simulated": true,
                "monetary_ceiling_enforced": false, "execution_available": false]
            complete(reviewed, nil)
        }
        view.job = nil; view.options?["providers"] = [["provider_id": "configured", "generation": generation, "models": ["configured-model"]]]
        view.plan = nil; view.refreshAvailability(); view.reviewScope()
        guard reviewed != nil, view.plan == nil, view.errorCode == "agent_plan_invalid", !view.sendButton.isEnabled else { return false }
        reviewed?["provider_generation"] = generation
        guard let body = view.scopeRequest(), let reviewed else { return false }
        return view.validPlan(reviewed, body: body)
    }
}

func runAgentPreparationContractSelfTest() -> Bool {
    AgentProviderSettingsView.contractSelfTest() && AgentPreparationView.providerOwnershipSelfTest() &&
        AgentPreparationView.recipeChoiceSelfTest() && AgentPreparationView.contractSelfTest()
}

extension AgentPreparationView {
    static func recipeChoiceSelfTest() -> Bool {
        let view = AgentPreparationView(); view.available = { true }
        guard let context = PreparationContext(importID: UUID().uuidString.lowercased(), revision: 1, sourceRevision: "sha256:" + String(repeating: "a", count: 64)) else { return false }
        view.bind(context, saved: true)
        let unrelated: [String: Any] = ["id": "mcpme.resolved-python", "version": "1.0.1", "digest": String(repeating: "b", count: 64)]
        let locked: [String: Any] = ["id": "mcpme.agent-python-adapter", "version": "1.0.0", "digest": String(repeating: "c", count: 64)]
        var unlocked: [String: Any] = ["id": "mcpme.agent-unlocked-python-adapter", "version": "1.0.0", "digest": String(repeating: "d", count: 64)]
        func choice(_ recipe: [String: Any], label: String, compatible: Bool, recommended: Bool = false) -> [String: Any] {
            ["recipe": recipe, "label": label, "compatibility": compatible ? "compatible" : "unresolved", "recommended": recommended]
        }
        var recipes = [unrelated, locked, unlocked]
        var choices = [choice(unrelated, label: "Detected Python tools", compatible: false),
                       choice(locked, label: "Python app with an existing dependency lock", compatible: false),
                       choice(unlocked, label: "Python app needing dependency resolution", compatible: true, recommended: true)]
        var truncated = false, calls = 0
        view.request = { path, method, body, completion in
            calls += 1
            guard path == "v1/agent-preparations/plans", method == "POST", body?.count == 2, let current = view.context else { completion(nil, "invalid_test_request"); return }
            completion(["api_version": 1, "schema_version": 1, "import_id": current.importID, "revision": current.revision, "source_revision": current.sourceRevision,
                "providers": [["provider_id": "configured", "models": ["test-model"]]],
                "files": [["path": "worker.py", "size": 40, "sha256": String(repeating: "e", count: 64)]],
                "metadata_files": [["path": "LICENSE", "size": 200, "sha256": String(repeating: "f", count: 64)]],
                "recipes": recipes, "recipe_choices": choices, "recipes_truncated": truncated, "latest_job": NSNull()], nil)
        }
        view.loadButton.performClick(nil)
        guard (view.recipePicker.selectedItem?.representedObject as? [String: Any])?["id"] as? String == unlocked["id"] as? String,
              view.recipePicker.titleOfSelectedItem?.contains("Python app needing dependency resolution") == true,
              view.modelPicker.selectedItem?.representedObject == nil, calls == 1 else { return false }
        // Explicit choice takes precedence over the recommended default.
        view.recipePicker.selectItem(at: 1); view.selectionChanged(view.recipePicker)
        view.loadOptions()
        guard (view.recipePicker.selectedItem?.representedObject as? [String: Any])?["id"] as? String == unrelated["id"] as? String else { return false }
        view.modelPicker.selectItem(withTitle: "test-model"); view.selectionChanged(view.modelPicker)
        view.fileControls[0].performClick(nil)
        guard view.scopeRequest() == nil, !view.reviewButton.isEnabled else { return false }
        // Disappearing explicit references are never silently replaced.
        recipes = [locked, unlocked]; choices.removeFirst(); view.loadOptions()
        guard view.recipePicker.selectedItem?.representedObject == nil, view.status.stringValue.contains("no longer available") else { return false }
        view.recipePicker.selectItem(at: 2); view.selectionChanged(view.recipePicker)
        unlocked["version"] = "1.0.1"; unlocked["digest"] = String(repeating: "1", count: 64)
        recipes = [locked, unlocked]; choices[1] = choice(unlocked, label: "Python app needing dependency resolution", compatible: true, recommended: true)
        view.loadOptions()
        guard view.recipePicker.selectedItem?.representedObject == nil, view.status.stringValue.contains("no longer available") else { return false }
        // Ambiguous captured metadata leaves the choice visibly unresolved.
        view.bind(PreparationContext(importID: context.importID, revision: 2, sourceRevision: context.sourceRevision), saved: true)
        choices = [choice(locked, label: "Python app with an existing dependency lock", compatible: false),
                   choice(unlocked, label: "Python app needing dependency resolution", compatible: false)]
        view.loadOptions()
        guard view.recipePicker.selectedItem?.representedObject == nil, view.status.stringValue.contains("No compatible preparation path") else { return false }
        // An incomplete inventory suppresses recommendations and asks for narrowing.
        truncated = true; choices[1] = choice(unlocked, label: "Python app needing dependency resolution", compatible: true, recommended: true)
        view.loadOptions()
        guard view.recipePicker.selectedItem?.representedObject == nil, view.status.stringValue.contains("Manage enabled recipes") else { return false }
        // A recommendation for an undisplayed reference cannot gain selection.
        truncated = false; choices[0] = choice(unrelated, label: "Unknown reference", compatible: true, recommended: true)
        view.loadOptions()
        guard view.errorCode == "agent_options_invalid", view.options == nil, !view.reviewButton.isEnabled else { return false }
        choices = [choice(locked, label: "Locked", compatible: true, recommended: true), choice(unlocked, label: "Unlocked", compatible: true, recommended: true)]
        view.loadOptions()
        return view.errorCode == "agent_options_invalid" && view.options == nil && !view.sendButton.isEnabled && calls == 8
    }
    static func providerOwnershipSelfTest() -> Bool {
        let view = AgentPreparationView(); view.available = { true }; view.refreshAvailability()
        let adapter: [String: Any] = ["adapter_id": "openai-responses-v1", "name": "OpenAI Responses", "destination": "https://api.openai.com/v1/responses",
            "configuration_schema": ["type": "object", "properties": [:], "additionalProperties": false],
            "credential_fields": [["id": "api_key", "label": "API key", "required": true, "max_bytes": 4096]], "model_selection": "explicit_ids"]
        let list: [String: Any] = ["schema_version": 1, "adapters": [adapter], "providers": [], "live_execution_enabled": false]
        var paths: [String] = []
        view.request = { path, method, body, completion in
            paths.append(path)
            guard path == "v1/agent-providers", method == "GET", body == nil else { completion(nil, "invalid_test_request"); return }
            completion(list, nil)
        }
        // Setup does not require an authored or saved import draft.
        view.providerSettings.loadProviders(); view.providerSettings.addProvider()
        guard view.blocksNavigation, !view.loadButton.isEnabled, !view.reviewButton.isEnabled, !view.sendButton.isEnabled else { return false }
        view.providerSettings.cancelEditing()
        guard !view.blocksNavigation else { return false }
        var held: (([String: Any]?, String?) -> Void)?
        view.request = { path, _, _, completion in paths.append(path); held = completion }
        view.providerSettings.loadProviders()
        guard view.blocksNavigation else { return false }
        view.bind(PreparationContext(importID: UUID().uuidString.lowercased(), revision: 1, sourceRevision: "sha256:" + String(repeating: "b", count: 64)), saved: true)
        guard view.blocksNavigation, !view.loadButton.isEnabled else { return false }
        held?(list, nil)
        guard !view.blocksNavigation, view.loadButton.isEnabled, paths.allSatisfy({ $0 == "v1/agent-providers" }) else { return false }
        // Editing provider settings invalidates an existing transfer review.
        view.plan = ["plan_id": "synthetic-plan"]
        view.providerSettings.addProvider()
        guard view.plan == nil, view.blocksNavigation else { return false }
        view.clearCredentialEntry(); view.providerSettings.cancelEditing()
        return !view.blocksNavigation && !view.sendButton.isEnabled
    }
}

/// Explicit selected-file transfer followed by ordinary host preparation review.
/// Provider settings confer no code or installation authority.
final class AgentPreparationView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var onUpdatedDraft: (([String: Any]) -> Bool)?
    var onStateChange: (() -> Void)?
    private var context: PreparationContext?
    private var saved = false
    private var epoch: UInt64 = 0
    private var jobEpoch: UInt64 = 0
    private var polling = false
    private(set) var busy = false
    private(set) var job: AgentPreparationJob?
    private(set) var errorCode: String?
    private var options: [String: Any]?
    private var plan: [String: Any]?
    private var preferredRecipe: [String: Any]?
    private var pendingStart: [String: Any]?
    private var pendingContext: PreparationContext?
    private let providerPicker = NSPopUpButton()
    private let providerSettings = AgentProviderSettingsView()
    private let modelPicker = NSPopUpButton()
    private let recipePicker = NSPopUpButton()
    private var fileControls: [NSButton] = []
    private var metadataControls: [NSButton] = []
    private let files = NSStackView()
    private let reviewLabel = NSTextField(wrappingLabelWithString: "Load configured providers and choose the captured files to review for transfer.")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let transfer = NSTextField(string: "524288")
    private let output = NSTextField(string: "4096")
    private let deadline = NSTextField(string: "180")
    private var loadButton: NSButton!
    private var reviewButton: NSButton!
    private var sendButton: NSButton!
    private var cancelButton: NSButton!
    private var retryButton: NSButton!
    private var adoptButton: NSButton!
    private var analysisBlocksNavigation: Bool { busy || pendingStart != nil || job?.active == true }
    var blocksNavigation: Bool { analysisBlocksNavigation || providerSettings.blocksNavigation }

    override init(frame: NSRect) {
        super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 8
        providerPicker.setAccessibilityLabel("Configured agent provider"); modelPicker.setAccessibilityLabel("Configured provider model")
        recipePicker.setAccessibilityLabel("Enabled preparation recipe")
        for picker in [providerPicker, modelPicker, recipePicker] { picker.target = self; picker.action = #selector(selectionChanged) }
        for (field, label) in [(transfer, "Maximum transfer bytes"), (output, "Maximum output tokens per call"), (deadline, "Analysis deadline in seconds")] {
            field.setAccessibilityLabel(label); field.target = self; field.action = #selector(selectionChanged)
        }
        files.orientation = .vertical; files.alignment = .leading; files.spacing = 4
        loadButton = NSButton(title: "Load Agent Options", target: self, action: #selector(loadOptions))
        reviewButton = NSButton(title: "Review Selected Transfer", target: self, action: #selector(reviewScope))
        sendButton = NSButton(title: "Send Reviewed Files", target: self, action: #selector(startAnalysis))
        cancelButton = NSButton(title: "Cancel Analysis", target: self, action: #selector(cancelAnalysis))
        retryButton = NSButton(title: "Retry Analysis Status", target: self, action: #selector(retryStatus))
        adoptButton = NSButton(title: "Use Candidate in Preparation", target: self, action: #selector(adoptCandidate))
        for button in [loadButton!, reviewButton!, sendButton!, cancelButton!, retryButton!, adoptButton!] {
            button.bezelStyle = .rounded; button.setAccessibilityLabel(button.title)
        }
        providerSettings.available = { [weak self] in
            guard let self else { return false }
            return self.available?() == true && !self.analysisBlocksNavigation
        }
        providerSettings.request = { [weak self] path, method, body, completion in
            guard let request = self?.request else { completion(nil, "manager_request_failed"); return }
            request(path, method, body, completion)
        }
        providerSettings.onStateChange = { [weak self] in
            guard let self else { return }
            if self.providerSettings.blocksNavigation { self.plan = nil }
            self.refreshAvailability(); self.onStateChange?()
        }
        providerSettings.onRegistryChange = { [weak self] in self?.registryChanged() }
        addArrangedSubview(providerSettings)
        workspaceFill(providerSettings, in: self)
        addArrangedSubview(workspaceStack([providerPicker, modelPicker, recipePicker], horizontal: true))
        addArrangedSubview(files)
        addArrangedSubview(workspaceStack([workspaceLabel("Transfer bytes", size: 12), transfer,
            workspaceLabel("Output tokens/call", size: 12), output, workspaceLabel("Seconds", size: 12), deadline], horizontal: true))
        addArrangedSubview(reviewLabel); workspaceFill(reviewLabel, in: self)
        addArrangedSubview(workspaceStack([loadButton, reviewButton, sendButton], horizontal: true))
        addArrangedSubview(status); workspaceFill(status, in: self)
        addArrangedSubview(workspaceStack([cancelButton, retryButton, adoptButton], horizontal: true))
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func bind(_ next: PreparationContext?, saved: Bool) {
        if next != context || saved != self.saved {
            providerSettings.clearCredentialEntry()
            invalidateJobResponses()
            epoch &+= 1; context = next; self.saved = saved; options = nil; plan = nil; preferredRecipe = nil
            // Keep a known pending request/job across draft changes. The owner
            // remains visible and cancellable until the host reports settlement.
            if !blocksNavigation { job = nil; errorCode = nil }
        }
        refreshAvailability()
    }
    private func invalidateJobResponses() { jobEpoch &+= 1; polling = false }
    func invalidate() { invalidateJobResponses(); epoch &+= 1; options = nil; plan = nil; providerSettings.invalidate(); refreshAvailability() }
    func clearCredentialEntry() { providerSettings.clearCredentialEntry() }
    func resetProviderConnection() { providerSettings.connectionReset() }
    func registryChanged() {
        if !blocksNavigation { invalidateJobResponses(); epoch &+= 1; options = nil; plan = nil; refreshAvailability() }
    }
    func refreshAvailability() {
        let online = available?() == true
        let editable = online && context != nil && saved && !blocksNavigation
        loadButton?.isEnabled = editable
        reviewButton?.isEnabled = editable && scopeRequest() != nil
        sendButton?.isEnabled = online && !blocksNavigation && currentPlan()
        cancelButton?.isEnabled = online && !busy && job?.active == true && job?.state != "cancel_requested"
        retryButton?.isEnabled = online && !busy && (pendingStart != nil || job != nil)
        adoptButton?.isEnabled = editable && job?.state == "succeeded" && job?.settled == true && job?.candidateDigest != nil && job?.context == context
        for control in [providerPicker, modelPicker, recipePicker, transfer, output, deadline] as [NSControl] { control.isEnabled = editable }
        fileControls.forEach { $0.isEnabled = editable }
        metadataControls.forEach { $0.isEnabled = editable }
        providerSettings.refreshAvailability()
    }
    private func fail(_ code: String) {
        errorCode = preparationCode(code) ?? "agent_request_failed"
        status.stringValue = errorCode!.replacingOccurrences(of: "_", with: " ").capitalized
        refreshAvailability(); onStateChange?()
    }
    private func finishRequest() { busy = false; refreshAvailability(); onStateChange?() }
    @objc private func selectionChanged(_ sender: NSControl) {
        guard !blocksNavigation else { return }
        if sender === recipePicker { preferredRecipe = recipePicker.selectedItem?.representedObject as? [String: Any] }
        if let button = sender as? NSButton, button.state == .on {
            if fileControls.contains(where: { $0 === button }) {
                metadataControls.filter { $0.identifier?.rawValue == button.title }.forEach { $0.state = .off }
            } else if metadataControls.contains(where: { $0 === button }) {
                fileControls.filter { $0.title == button.identifier?.rawValue }.forEach { $0.state = .off }
            }
        }
        if senderProviderChanged() { reloadModels() }
        plan = nil; reviewLabel.stringValue = "Review the exact files and provider before sending. Canceling cannot guarantee remote calls stop or avoid charges."
        if let selected = recipePicker.selectedItem?.representedObject as? [String: Any], let choice = recipeChoice(selected), !choice.compatible {
            status.stringValue = "This preparation path is unresolved for the captured files. No source has been sent."
        }
        refreshAvailability()
    }
    private var modelProvider: String?
    private func senderProviderChanged() -> Bool { modelProvider != providerPicker.selectedItem?.representedObject as? String }
    private var providers: [[String: Any]] { options?["providers"] as? [[String: Any]] ?? [] }
    private func recipeChoice(_ recipe: [String: Any]) -> AgentRecipeChoice? {
        guard let options, let choices = options["recipe_choices"] as? [[String: Any]], let recipes = options["recipes"] as? [[String: Any]],
              let object = choices.first(where: { ($0["recipe"] as? NSDictionary)?.isEqual(to: recipe) == true }) else { return nil }
        return AgentRecipeChoice(object, recipes: recipes)
    }
    private func reloadModels() {
        modelPicker.removeAllItems(); modelProvider = providerPicker.selectedItem?.representedObject as? String
        modelPicker.addItem(withTitle: "Choose a model…")
        if let provider = providers.first(where: { $0["provider_id"] as? String == modelProvider }) {
            for model in provider["models"] as? [String] ?? [] {
                modelPicker.addItem(withTitle: model); modelPicker.lastItem?.representedObject = model
            }
        }
    }
    @objc func loadOptions() {
        guard available?() == true, !blocksNavigation, saved, let context, let request else { return }
        invalidateJobResponses()
        plan = nil; options = nil
        busy = true; refreshAvailability(); let expected = epoch
        request("v1/agent-preparations/plans", "POST", ["import_id": context.importID, "revision": context.revision]) { [weak self] object, code in
            guard let self else { return }; self.finishRequest()
            guard self.epoch == expected else { return }
            guard let object, preparationInteger(object["schema_version"]) == 1,
                  object["import_id"] as? String == context.importID, preparationInteger(object["revision"]) == context.revision,
                  object["source_revision"] as? String == context.sourceRevision,
                  let providers = object["providers"] as? [[String: Any]], providers.count <= 16,
                  let records = object["files"] as? [[String: Any]], records.count <= 256,
                  let recipes = object["recipes"] as? [[String: Any]], recipes.count <= 128 else { self.fail(code ?? "agent_options_invalid"); return }
            guard providers.allSatisfy({ preparationText($0["provider_id"], maximum: 128) != nil && ($0["generation"] == nil || $0["generation"] is NSNull || preparationUUID($0["generation"]) != nil) && ($0["name"] == nil || preparationText($0["name"], maximum: 80) != nil) && ($0["models"] as? [String]).map { $0.count <= 16 && $0.allSatisfy { preparationText($0, maximum: 128) != nil } } == true }),
                  records.allSatisfy({ preparationText($0["path"], maximum: 256) != nil && preparationDigest($0["sha256"]) != nil && (1...16384).contains(preparationInteger($0["size"]) ?? 0) }),
                  recipes.allSatisfy({ preparationText($0["id"], maximum: 128) != nil && preparationText($0["version"], maximum: 128) != nil && preparationDigest($0["digest"]) != nil }) else { self.fail("agent_options_invalid"); return }
            var choices: [AgentRecipeChoice] = []
            if object["recipe_choices"] != nil {
                guard let raw = object["recipe_choices"] as? [[String: Any]], raw.count == recipes.count else { self.fail("agent_options_invalid"); return }
                choices = raw.compactMap { AgentRecipeChoice($0, recipes: recipes) }
                guard choices.count == raw.count, Set(choices.map(\.identity)).count == choices.count,
                      choices.filter(\.recommended).count <= 1 else { self.fail("agent_options_invalid"); return }
            }
            guard object["recipes_truncated"] == nil || preparationBoolean(object["recipes_truncated"]) != nil else { self.fail("agent_options_invalid"); return }
            self.options = object; self.plan = nil; self.providerPicker.removeAllItems(); self.recipePicker.removeAllItems()
            self.recipePicker.addItem(withTitle: "Choose a preparation path…")
            for value in providers {
                let id = value["provider_id"] as! String
                self.providerPicker.addItem(withTitle: self.providerSettings.providerNames[id] ?? preparationText(value["name"], maximum: 80) ?? id)
                self.providerPicker.lastItem?.representedObject = id
            }
            for value in recipes {
                let id = value["id"] as! String
                let label = choices.first(where: { NSDictionary(dictionary: $0.recipe).isEqual(to: value) })?.label ??
                    ["mcpme.agent-python-adapter": "Python app with an existing dependency lock",
                     "mcpme.agent-unlocked-python-adapter": "Python app needing dependency resolution"][id] ?? id
                self.recipePicker.addItem(withTitle: label + " · " + (value["version"] as! String)); self.recipePicker.lastItem?.representedObject = value
            }
            var previousChoiceUnavailable = false
            if let preferred = self.preferredRecipe {
                if let index = recipes.firstIndex(where: { NSDictionary(dictionary: $0).isEqual(to: preferred) }) { self.recipePicker.selectItem(at: index + 1) }
                else { self.preferredRecipe = nil; previousChoiceUnavailable = true }
            } else if preparationBoolean(object["recipes_truncated"]) != true, let choice = choices.first(where: \.recommended), let index = recipes.firstIndex(where: { NSDictionary(dictionary: $0).isEqual(to: choice.recipe) }) {
                self.recipePicker.selectItem(at: index + 1)
            }
            self.reloadModels()
            self.fileControls.forEach { self.files.removeArrangedSubview($0); $0.removeFromSuperview() }; self.fileControls = []
            self.metadataControls.forEach { self.files.removeArrangedSubview($0); $0.removeFromSuperview() }; self.metadataControls = []
            for value in records {
                let button = NSButton(checkboxWithTitle: value["path"] as! String, target: self, action: #selector(self.selectionChanged))
                button.setAccessibilityLabel("Transfer " + button.title); button.state = .off
                self.fileControls.append(button); self.files.addArrangedSubview(button)
            }
            let metadata = object["metadata_files"] as? [[String: Any]] ?? []
            guard metadata.count <= 256, metadata.allSatisfy({ preparationText($0["path"], maximum: 256) != nil && preparationDigest($0["sha256"]) != nil && (1...16777216).contains(preparationInteger($0["size"]) ?? 0) }) else { self.fail("agent_options_invalid"); return }
            for value in metadata {
                let path = value["path"] as! String
                let recommended = ["LICENSE", "LICENSE.txt", "LICENSE.md", "pyproject.toml", "uv.lock"].contains((path as NSString).lastPathComponent)
                let button = NSButton(checkboxWithTitle: "Identity only: " + path + (recommended ? " (recommended)" : ""), target: self, action: #selector(self.selectionChanged))
                button.identifier = NSUserInterfaceItemIdentifier(path)
                button.setAccessibilityLabel(button.title + "; sends path, byte count and SHA-256, no contents")
                button.state = .off
                self.metadataControls.append(button); self.files.addArrangedSubview(button)
            }
            if let latest = object["latest_job"] as? [String: Any], let job = AgentPreparationJob(latest), job.context.importID == context.importID { self.job = job }
            self.status.stringValue = providers.isEmpty ? "No agent provider is configured. Refresh Providers, then choose Add Provider above." : "Choose a model and files to transfer. Loading options sends no files."
            if previousChoiceUnavailable { self.status.stringValue += " Your previously selected preparation path is no longer available. Choose a current path explicitly." }
            if !choices.isEmpty, !choices.contains(where: \.recommended), self.preferredRecipe == nil {
                self.status.stringValue += " No compatible preparation path was recommended for these captured files."
            }
            if preparationBoolean(object["recipes_truncated"]) == true {
                self.status.stringValue += " The enabled recipe list is incomplete. Manage enabled recipes to narrow the choices, then reload options."
            }
            self.refreshAvailability()
        }
    }
    private func scopeRequest() -> [String: Any]? {
        guard options != nil, let context, saved, let provider = providerPicker.selectedItem?.representedObject as? String,
              let model = modelPicker.selectedItem?.representedObject as? String, let recipe = recipePicker.selectedItem?.representedObject as? [String: Any],
              let bytes = Int(transfer.stringValue), (1...2097152).contains(bytes),
              let tokens = Int(output.stringValue), (64...16384).contains(tokens),
              let seconds = Int(deadline.stringValue), (1...600).contains(seconds) else { return nil }
        if options?["recipe_choices"] != nil, recipeChoice(recipe)?.compatible != true { return nil }
        let paths = fileControls.filter { $0.state == .on }.map(\.title).sorted()
        let metadataPaths = metadataControls.filter { $0.state == .on }.compactMap { $0.identifier?.rawValue }.sorted()
        guard (1...32).contains(paths.count) else { return nil }
        guard metadataPaths.count <= 32, Set(paths).isDisjoint(with: metadataPaths) else { return nil }
        return ["import_id": context.importID, "revision": context.revision, "recipe": recipe,
            "provider_id": provider, "model": model, "selected_paths": paths,
            "selected_metadata_paths": metadataPaths,
            "limits": ["max_calls": 4, "max_parallel": 3, "max_transfer_bytes": bytes, "max_output_tokens": tokens,
                       "wall_seconds": seconds, "max_artifact_bytes": 131072]]
    }
    private func currentPlan() -> Bool {
        guard let plan, let context, plan["import_id"] as? String == context.importID,
              preparationInteger(plan["revision"]) == context.revision, plan["source_revision"] as? String == context.sourceRevision,
              preparationBoolean(plan["execution_available"]) == true, let expires = preparationNumber(plan["expires_at"]),
              expires > Date().timeIntervalSince1970 else { return false }
        return true
    }
    @objc func reviewScope() {
        guard !blocksNavigation, let body = scopeRequest(), let request else { return }
        busy = true; refreshAvailability(); let expected = epoch
        request("v1/agent-preparations/plans", "POST", body) { [weak self] object, code in
            guard let self else { return }; self.finishRequest()
            guard expected == self.epoch else { return }
            guard let object, self.validPlan(object, body: body), let records = object["selected_files"] as? [[String: Any]],
                  let limits = object["limits"] as? [String: Any], let destination = preparationText(object["destination"], maximum: 256),
                  let simulated = preparationBoolean(object["simulated"]) else { self.fail(code ?? "agent_plan_invalid"); return }
            self.plan = object
            var lines = ["External destination: " + destination,
                simulated ? "Simulated provider: no live external transfer is proven." : "Configured external provider",
                "Provider: " + (self.providers.first(where: { $0["provider_id"] as? String == object["provider_id"] as? String })?["name"] as? String ?? object["provider_id"] as! String) + " / " + (object["model"] as! String),
                "Recipe: " + ((body["recipe"] as! [String: Any])["id"] as! String) + " " + ((body["recipe"] as! [String: Any])["version"] as! String),
                "Four calls, at most three parallel. Transfer bytes: \(limits["max_transfer_bytes"]!).",
                "Output tokens per call: \(limits["max_output_tokens"]!). Deadline seconds: \(limits["wall_seconds"]!).",
                "A dollar ceiling is not enforced. Remote usage after cancellation is unknown.",
                "Selected files for transfer:"]
            lines += records.map { ($0["path"] as! String) + " · \($0["bytes"]!) bytes · " + ($0["sha256"] as! String) }
            let metadata = object["selected_metadata"] as? [[String: Any]] ?? []
            lines += ["Identity-only transfer (contents excluded):"]
            lines += metadata.map { ($0["path"] as! String) + " · \($0["bytes"]!) original bytes · " + ($0["sha256"] as! String) }
            if let guide = object["guide"] as? [String: Any], let id = guide["id"] as? String {
                lines += ["Trusted proposal contract: " + id + " · " + (object["guide_digest"] as! String),
                    "The provider also receives the selected recipe identity, public output contract and task dependencies."]
            }
            self.reviewLabel.stringValue = lines.joined(separator: "\n")
            self.status.stringValue = preparationBoolean(object["execution_available"]) == true ? "Review this transfer scope, then choose Send Reviewed Files." : "Agent execution is disabled. This review sends nothing."
            self.refreshAvailability()
        }
    }
    private func validPlan(_ object: [String: Any], body: [String: Any]) -> Bool {
        if let provider = providers.first(where: { $0["provider_id"] as? String == body["provider_id"] as? String }), let generation = provider["generation"] as? String {
            guard preparationUUID(object["provider_generation"]) == generation else { return false }
        }
        guard preparationInteger(object["schema_version"]) == 1, preparationUUID(object["plan_id"]) != nil,
              preparationDigest(object["plan_digest"]) != nil, preparationNumber(object["expires_at"]) != nil,
              object["import_id"] as? String == body["import_id"] as? String,
              preparationInteger(object["revision"]) == preparationInteger(body["revision"]),
              object["source_revision"] as? String == context?.sourceRevision,
              object["provider_id"] as? String == body["provider_id"] as? String, object["model"] as? String == body["model"] as? String,
              let recipe = object["recipe"] as? NSDictionary, recipe.isEqual(to: body["recipe"] as? [AnyHashable: Any] ?? [:]),
              let limits = object["limits"] as? NSDictionary, limits.isEqual(to: body["limits"] as? [AnyHashable: Any] ?? [:]),
              object["destination"] as? String == "https://api.openai.com/v1/responses",
              preparationBoolean(object["monetary_ceiling_enforced"]) == false,
              let records = object["selected_files"] as? [[String: Any]], records.count <= 32,
              records.compactMap({ $0["path"] as? String }) == body["selected_paths"] as? [String],
              records.allSatisfy({ preparationText($0["path"], maximum: 256) != nil && preparationDigest($0["sha256"]) != nil && (1...16384).contains(preparationInteger($0["bytes"]) ?? 0) }) else { return false }
        let metadata = object["selected_metadata"] as? [[String: Any]] ?? []
        guard metadata.count <= 32,
              metadata.compactMap({ $0["path"] as? String }) == (body["selected_metadata_paths"] as? [String] ?? []),
              metadata.allSatisfy({ preparationText($0["path"], maximum: 256) != nil && preparationDigest($0["sha256"]) != nil && (1...16777216).contains(preparationInteger($0["bytes"]) ?? 0) }) else { return false }
        if let guide = object["guide"] as? [String: Any] {
            guard preparationInteger(guide["schema_version"]) == 1, preparationText(guide["id"], maximum: 128) != nil,
                  preparationDigest(object["guide_digest"]) != nil,
                  let component = guide["component"] as? [String: Any], preparationDigest(component["digest"]) != nil,
                  let roles = guide["roles"] as? [[String: Any]], roles.count == 4,
                  roles.compactMap({ $0["role"] as? String }) == ["interface", "dependencies", "fixtures", "adapter"],
                  roles.allSatisfy({ preparationText($0["instructions"], maximum: 16384) != nil && ($0["depends_on"] as? [String]) != nil }) else { return false }
        } else if object["guide"] != nil && !(object["guide"] is NSNull) { return false }
        return true
    }
    @objc func startAnalysis() {
        guard available?() == true, !blocksNavigation, currentPlan(), let plan else { return }
        pendingStart = ["action": "analyze", "plan_id": plan["plan_id"]!, "plan_digest": plan["plan_digest"]!, "idempotency_key": UUID().uuidString.lowercased()]
        pendingContext = context
        sendPending()
    }
    private func sendPending() {
        guard let body = pendingStart, let request, !busy else { return }
        invalidateJobResponses()
        busy = true; refreshAvailability()
        request("v1/agent-preparations/start", "POST", body) { [weak self] object, code in
            guard let self else { return }; self.finishRequest()
            guard let object, let job = AgentPreparationJob(object), object["plan_id"] as? String == body["plan_id"] as? String,
                  job.context == self.pendingContext else { self.fail(code ?? "agent_job_invalid"); return }
            self.pendingStart = nil; self.pendingContext = nil; self.job = job; self.showJob()
        }
    }
    private func showJob() {
        status.stringValue = job?.errorCode?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Analysis: " + (job?.state ?? "unknown")
        if job?.state == "succeeded", job?.candidateDigest == nil { status.stringValue = "The proposal needs further review. No executable candidate was admitted." }
        refreshAvailability(); onStateChange?()
    }
    @objc func retryStatus() { if pendingStart != nil { sendPending() } else { poll(force: true) } }
    func poll(force: Bool = false) {
        guard available?() == true, !busy, !polling, let job, force || job.active, let request else { return }
        polling = true; let expected = jobEpoch
        request("v1/agent-preparations/jobs/" + job.id, "GET", nil) { [weak self] object, code in
            guard let self, expected == self.jobEpoch, self.job?.id == job.id, self.job?.context == job.context else { return }
            self.polling = false
            guard let object, let next = AgentPreparationJob(object), next.id == job.id, next.context == job.context else { self.fail(code ?? "agent_job_invalid"); return }
            self.job = next; self.showJob()
        }
    }
    @objc func cancelAnalysis() {
        guard !busy, let job, job.active, let request else { return }
        invalidateJobResponses()
        busy = true; refreshAvailability()
        request("v1/agent-preparations/jobs/" + job.id + "/cancel", "POST", [:]) { [weak self] object, code in
            guard let self else { return }; self.finishRequest()
            guard let object, let next = AgentPreparationJob(object), next.id == job.id, next.context == job.context else { self.fail(code ?? "agent_job_invalid"); return }
            self.job = next; self.showJob()
        }
    }
    @objc func adoptCandidate() {
        guard !blocksNavigation, let job, job.context == context, job.state == "succeeded", job.settled,
              let digest = job.proposalDigest, job.candidateDigest != nil, let request else { return }
        invalidateJobResponses()
        busy = true; refreshAvailability()
        request("v1/agent-preparations/start", "POST", ["action": "adopt-candidate", "job_id": job.id, "proposal_digest": digest]) { [weak self] object, code in
            guard let self else { return }; self.finishRequest()
            guard let object, let row = object["job"] as? [String: Any], let next = AgentPreparationJob(row),
                  next.id == job.id, next.context == job.context, next.state == "adopted", next.settled,
                  let draft = object["draft"] as? [String: Any], let updated = PreparationContext(draft),
                  updated.importID == job.context.importID, updated.sourceRevision == job.context.sourceRevision,
                  updated.revision == job.context.revision + 1, preparationBoolean(object["requires_preparation_review"]) == true,
                  preparationDigest(object["descriptor_digest"]) == job.candidateDigest else { self.fail(code ?? "agent_candidate_invalid"); return }
            self.job = next
            guard self.onUpdatedDraft?(draft) == true else { self.fail("stale_import_revision"); return }
            self.status.stringValue = "Candidate selected. Review the normal preparation questions and plan before building."; self.refreshAvailability()
        }
    }
}
