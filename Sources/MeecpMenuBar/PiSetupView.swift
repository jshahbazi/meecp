import AppKit
import AppInstallation
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
    let question: PiSetupQuestion?
    let systemAction: [String: Any]?

    init?(_ object: [String: Any]) {
        guard let id = preparationUUID(object["id"]), let importID = preparationUUID(object["import_id"]),
              let revision = preparationInteger(object["revision"]), revision > 0,
              let sourceRevision = preparationText(object["source_revision"], maximum: 256),
              let status = object["status"] as? String, ["running", "ready", "failed", "canceled", "interrupted"].contains(status),
              let stage = preparationCode(object["stage"]) else { return nil }
        if let value = object["code"], !(value is NSNull), preparationCode(value) == nil { return nil }
        let question: PiSetupQuestion?
        if let value = object["question"], !(value is NSNull) {
            guard status == "running", let row = value as? [String: Any], let pending = PiSetupQuestion(row),
                  pending.jobID == id, pending.importID == importID, pending.revision == revision else { return nil }
            question = pending
        } else { question = nil }
        self.id = id; self.importID = importID; self.revision = revision
        self.sourceRevision = sourceRevision
        self.status = status; self.stage = stage; self.errorCode = preparationCode(object["code"])
        self.question = question
        self.systemAction = object["system_action"] as? [String: Any]
    }

    var active: Bool { status == "running" }
}

struct PiSetupQuestion {
    let id: String
    let jobID: String
    let importID: String
    let revision: Int
    let kind: String
    let text: String
    let options: [String]
    let credentialName: String?

    init?(_ object: [String: Any]) {
        guard let id = preparationUUID(object["id"]), let jobID = preparationUUID(object["job_id"]),
              let importID = preparationUUID(object["import_id"]), let revision = preparationInteger(object["revision"]), revision > 0,
              let kind = object["kind"] as? String, ["text", "secret"].contains(kind),
              let text = preparationText(object["question"], maximum: 8192), text.utf8.count <= 8192,
              let options = object["options"] as? [String], options.count <= 16,
              options.allSatisfy({ preparationText($0, maximum: 256) != nil && $0.utf8.count <= 256 }),
              Set(options).count == options.count else { return nil }
        let name: String?
        if let value = object["credential_name"], !(value is NSNull) {
            guard let field = value as? String, field.range(of: "^[A-Za-z][A-Za-z0-9_]{0,63}$", options: .regularExpression) != nil else { return nil }
            name = field
        } else { name = nil }
        guard kind == "secret" ? name != nil && options.isEmpty : name == nil else { return nil }
        self.id = id; self.jobID = jobID; self.importID = importID; self.revision = revision
        self.kind = kind; self.text = text; self.options = options; self.credentialName = name
    }
}

/// Provider credentials stay in this secure inline card and never enter messages.
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
    private let card = NSBox()
    private let heading = workspaceLabel("Set up your agent", size: 17, weight: .semibold)
    private let explanation = workspaceLabel("Choose a model so MeeCP can prepare your app.", size: 12, secondary: true)
    private let keyHelp = workspaceLabel("Stored privately on this Mac.", size: 11, secondary: true)
    private let modelFields = NSStackView()
    private let advancedFields = NSStackView()
    private var stackedFieldWidths: [NSLayoutConstraint] = []
    private var advancedButton: NSButton!
    private var saveButton: NSButton!
    private var closeButton: NSButton!
    private var metadata: PiSettingsMetadata?
    private var busy = false
    private var epoch: UInt64 = 0

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 0
        setContentHuggingPriority(.required, for: .vertical)
        card.boxType = .custom; card.titlePosition = .noTitle
        card.borderColor = .separatorColor; card.fillColor = .controlBackgroundColor
        card.borderWidth = 1; card.cornerRadius = 12; card.contentViewMargins = .zero
        provider.setAccessibilityLabel("Setup model provider")
        model.placeholderString = "Enter a model ID"; model.setAccessibilityLabel("Setup model ID")
        baseURL.placeholderString = "https://api.example.com/v1"; baseURL.setAccessibilityLabel("Custom provider base URL")
        apiKey.placeholderString = "Enter your API key"; apiKey.setAccessibilityLabel("Setup provider API key")
        apiKey.isAutomaticTextCompletionEnabled = false
        saveButton = NSButton(title: "Continue", target: self, action: #selector(save))
        saveButton.bezelColor = .controlAccentColor
        closeButton = NSButton(title: "Cancel", target: self, action: #selector(close))
        advancedButton = NSButton(title: "Advanced", target: self, action: #selector(toggleAdvanced))
        advancedButton.isBordered = false; advancedButton.controlSize = .small
        advancedButton.font = .systemFont(ofSize: 12)
        advancedButton.imagePosition = .imageLeft
        advancedButton.contentTintColor = .secondaryLabelColor
        advancedButton.setAccessibilityLabel("Advanced agent settings")

        func group(_ title: String, _ views: [NSView]) -> NSStackView {
            let stack = workspaceStack([workspaceLabel(title, size: 12, weight: .medium)] + views, spacing: 6)
            for child in stack.arrangedSubviews { workspaceFill(child, in: stack) }
            return stack
        }
        let providerGroup = group("Provider", [provider])
        let modelGroup = group("Model ID", [model])
        modelFields.orientation = .horizontal; modelFields.alignment = .top; modelFields.spacing = 12
        modelFields.distribution = .fillEqually
        modelFields.addArrangedSubview(providerGroup); modelFields.addArrangedSubview(modelGroup)
        stackedFieldWidths = [providerGroup.widthAnchor.constraint(equalTo: modelFields.widthAnchor),
                              modelGroup.widthAnchor.constraint(equalTo: modelFields.widthAnchor)]
        let keyGroup = group("API key", [apiKey, keyHelp])
        advancedFields.orientation = .vertical; advancedFields.alignment = .leading; advancedFields.spacing = 6
        let endpointGroup = group("Custom endpoint", [baseURL,
            workspaceLabel("Leave blank to use your provider's default endpoint.", size: 11, secondary: true)])
        advancedFields.addArrangedSubview(endpointGroup); workspaceFill(endpointGroup, in: advancedFields)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let actions = workspaceStack([closeButton!, spacer, saveButton!], horizontal: true, spacing: 10)
        let privacy = workspaceLabel("Your request and selected source are shared with this provider when app setup begins.", size: 11, secondary: true)
        let content = workspaceStack([heading, explanation, modelFields, keyGroup, advancedButton!, advancedFields,
                                      status, actions, privacy], spacing: 14)
        content.setCustomSpacing(5, after: heading)
        content.setCustomSpacing(8, after: advancedButton)
        for child in [heading, explanation, modelFields, keyGroup, advancedFields, status, actions, privacy] {
            workspaceFill(child, in: content)
        }
        card.translatesAutoresizingMaskIntoConstraints = false
        addArrangedSubview(card)
        let fillWidth = card.widthAnchor.constraint(equalTo: widthAnchor)
        fillWidth.priority = .defaultHigh
        NSLayoutConstraint.activate([
            card.leadingAnchor.constraint(equalTo: leadingAnchor),
            card.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor),
            card.widthAnchor.constraint(lessThanOrEqualToConstant: 600), fillWidth,
        ])
        let container = card.contentView!
        container.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 18),
            content.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -18),
            content.topAnchor.constraint(equalTo: container.topAnchor, constant: 18),
            content.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -18),
        ])
        setAdvanced(false); setStatus("")
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layout() {
        let orientation: NSUserInterfaceLayoutOrientation = bounds.width > 0 && bounds.width < 440 ? .vertical : .horizontal
        if modelFields.orientation != orientation {
            modelFields.orientation = orientation
            modelFields.alignment = orientation == .horizontal ? .top : .leading
            modelFields.distribution = orientation == .horizontal ? .fillEqually : .fill
            if orientation == .vertical { NSLayoutConstraint.activate(stackedFieldWidths) }
            else { NSLayoutConstraint.deactivate(stackedFieldWidths) }
        }
        super.layout()
    }

    private func setAdvanced(_ expanded: Bool) {
        advancedFields.isHidden = !expanded
        advancedButton.image = NSImage(systemSymbolName: expanded ? "chevron.down" : "chevron.right", accessibilityDescription: nil)
        advancedButton.setAccessibilityValue(expanded ? 1 : 0)
    }

    @objc private func toggleAdvanced() { setAdvanced(advancedFields.isHidden) }

    private func setStatus(_ text: String) {
        status.stringValue = text; status.isHidden = text.isEmpty
    }

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
        model.stringValue = value.model
        baseURL.stringValue = value.baseURL ?? ""
        heading.stringValue = value.configured ? "Agent settings" : "Set up your agent"
        explanation.stringValue = value.configured ? "MeeCP uses this model to create and update your apps." : "Choose a model so MeeCP can prepare your app."
        apiKey.placeholderString = value.configured ? "Leave blank to keep your saved key" : "Enter your API key"
        keyHelp.stringValue = value.configured ? "Stored privately on this Mac. Leave blank to keep your saved key." : "Stored privately on this Mac and used only with your provider."
        saveButton.title = value.configured ? "Save settings" : "Continue"
        setAdvanced(value.baseURL != nil)
        setStatus(value.runtimeAvailable ? "" : "The agent runtime is unavailable. You can save these settings and finish setup once it is restored.")
        refreshAvailability()
    }

    func clearCredentialEntry() {
        if let editor = apiKey.currentEditor() { editor.string = ""; editor.undoManager?.removeAllActions() }
        apiKey.stringValue = ""
    }

    func connectionReset() {
        epoch &+= 1; busy = false; metadata = nil; clearCredentialEntry(); refreshAvailability()
    }

    func dismiss() {
        epoch &+= 1; busy = false; clearCredentialEntry(); refreshAvailability()
    }

    private func refreshAvailability() {
        let enabled = !busy && available?() == true && metadata != nil
        for field in [model, baseURL, apiKey] { field.isEnabled = enabled }
        provider.isEnabled = enabled; saveButton.isEnabled = enabled; closeButton.isEnabled = !busy
        advancedButton.isEnabled = !busy
    }

    @objc private func close() { clearCredentialEntry(); onClose?() }

    @objc private func save() {
        guard !busy, available?() == true, metadata != nil, let request,
              let providerID = provider.selectedItem?.representedObject as? String,
              let modelID = piModel(model.stringValue), !modelID.isEmpty else {
            setStatus("Choose a provider and enter a model ID."); return
        }
        let endpoint = baseURL.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard endpoint.isEmpty || Self.validBaseURL(endpoint) else {
            setAdvanced(true)
            setStatus("Use an HTTPS endpoint or an HTTP endpoint on this Mac, without a password or query."); return
        }
        var body: [String: Any] = ["provider": providerID, "model": modelID, "base_url": endpoint.isEmpty ? NSNull() : endpoint]
        if !apiKey.stringValue.isEmpty { body["api_key"] = apiKey.stringValue }
        clearCredentialEntry(); busy = true; setStatus("Saving settings…"); refreshAvailability(); let expected = epoch
        request("v1/pi/settings", "PUT", body) { [weak self] object, _ in
            guard let self, expected == self.epoch else { return }
            self.busy = false
            guard let object, let value = PiSettingsMetadata(object) else {
                self.setStatus("Settings could not be saved. Try again.")
                self.refreshAvailability(); return
            }
            self.configure(value); self.setStatus("Settings saved.")
            self.onSaved?(value)
        }
    }

    /// Only used with a fake request closure by the native contract self-test.
    static func submitSelfTest(_ view: PiSettingsView, model: String, key: String) -> Bool {
        view.model.stringValue = model; view.apiKey.stringValue = key; view.save()
        return view.apiKey.stringValue.isEmpty
    }

    /// Uses synthetic metadata and a fake request; never touches saved credentials.
    static func formSelfTest() -> Bool {
        let configured: [String: Any] = ["configured": true, "provider": "openai", "model": "test-model",
            "base_url": "https://api.example.com/v1", "runtime_available": true, "available_providers": ["openai"]]
        guard let metadata = PiSettingsMetadata(configured) else { return false }
        let view = PiSettingsView(); view.available = { true }; view.configure(metadata)
        guard view.saveButton.title == "Save settings", !view.advancedFields.isHidden,
              view.apiKey.stringValue.isEmpty else { return false }
        view.toggleAdvanced()
        var preservedEndpoint = false
        view.request = { path, method, body, done in
            preservedEndpoint = path == "v1/pi/settings" && method == "PUT" &&
                body?["base_url"] as? String == "https://api.example.com/v1" && body?["api_key"] == nil
            done(configured, nil)
        }
        view.save()
        guard preservedEndpoint, view.apiKey.stringValue.isEmpty else { return false }
        var initial = configured; initial["configured"] = false; initial["model"] = ""; initial["base_url"] = NSNull()
        guard let initialMetadata = PiSettingsMetadata(initial) else { return false }
        view.configure(initialMetadata)
        guard view.saveButton.title == "Continue", view.advancedFields.isHidden, view.status.isHidden else { return false }
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 920, height: 700))
        view.translatesAutoresizingMaskIntoConstraints = false; container.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            view.topAnchor.constraint(equalTo: container.topAnchor),
        ])
        container.layoutSubtreeIfNeeded()
        guard view.card.frame.width > 590, view.card.frame.width <= 600,
              view.card.frame.height > 200, view.card.frame.height < 500,
              view.modelFields.orientation == .horizontal else { return false }
        container.setFrameSize(NSSize(width: 360, height: 700)); container.layoutSubtreeIfNeeded()
        container.layoutSubtreeIfNeeded()
        return view.card.frame.width <= 360 && view.modelFields.orientation == .vertical &&
            view.modelFields.arrangedSubviews.allSatisfy { $0.frame.width > 300 }
    }
}

/// Progress is host metadata; questions and answers remain in the live view.
/// Credential values are submitted separately and never reappear in job metadata.
final class PiSetupView: NSStackView, NSWindowDelegate {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var beforeStart: ((@escaping (PreparationContext?) -> Void) -> Void)?
    var onReady: ((PiSetupJob) -> Void)?
    var onStateChange: (() -> Void)?
    var onJobChange: ((String?) -> Void)?
    var onConversationChange: (() -> Void)?
    var onSuggestedAnswer: ((String) -> Void)?
    let technicalDetailsView = NSStackView()
    var applicationLaunchExclusions: [URL] = [FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/mcpme", isDirectory: true)]
    private var verifyXcodeApplication: (String, [URL], @escaping (URL?) -> Void) -> Void = { path, roots, completion in
        DispatchQueue.global(qos: .userInitiated).async {
            let app = XcodeApplication.verifiedURL(for: path, excluding: roots)
            DispatchQueue.main.async { completion(app) }
        }
    }
    private var openSystemURL: (URL) -> Bool = { NSWorkspace.shared.open($0) }
    private var systemVerification: UUID?
    private(set) var job: PiSetupJob?
    private(set) var errorCode: String?
    private var context: PreparationContext?
    private var eligible = false
    private var settings: PiSettingsMetadata?
    private var settingsLoading = false
    private var busy = false
    private var polling = false
    private var epoch: UInt64 = 0 { didSet { systemVerification = nil } }
    private var deliveredReady: String?
    private var restoredJobID: String?
    private let modelLabel = workspaceLabel("Loading setup model…", size: 12, secondary: true)
    private let status = workspaceLabel("Describe the tools you want, then choose Create app.", size: 13, weight: .medium)
    private let errorDetail = workspaceLabel("", size: 11, secondary: true)
    private let notice = workspaceLabel("Set Up App sends your description, selected source, and any answers to your configured model, then builds the app in a temporary workspace.", size: 12, secondary: true)
    private let progress = NSProgressIndicator()
    private let questionView = NSStackView()
    private let questionText = workspaceLabel("", size: 13, weight: .medium)
    private let answerField = NSComboBox()
    private let credentialField = NSSecureTextField(string: "")
    private let questionNotice = workspaceLabel("", size: 12, secondary: true)
    private var answerButton: NSButton!
    private var shownQuestionID: String?
    private var startButton: NSButton!
    private var cancelButton: NSButton!
    private var retryButton: NSButton!
    private var settingsButton: NSButton!
    private var systemButton: NSButton!
    private let settingsView = PiSettingsView()
    private var inlineSettingsVisible = false
    private var pendingConfiguredStart: PreparationContext?
    private var conversationMode = false
    private let eventStack = NSStackView()
    private let conversationNotice = workspaceLabel("", size: 11, secondary: true)
    private var conversationJobID: String?
    private var conversationCursor = 0
    private var conversationPolling = false
    private var conversationAvailable = false
    private var conversationCanMessage = false
    private var conversationEvents: [PiConversationEvent] = []
    private var renderedEvents: [Int: (event: PiConversationEvent, view: NSView)] = [:]
    private var unacknowledgedMessage: (id: String, text: String, jobID: String, context: PreparationContext)?
    private let optionsStack = NSStackView()
    private var optionButtons: [NSButton] = []
    private var modelRow: NSView!
    private var actionRow: NSView!
    private var taskCard: NSView!

    var blocksNavigation: Bool { busy || job?.active == true || restoredJobID != nil }
    var authorizationPresentationBlocked: Bool {
        inlineSettingsVisible || (job?.question?.kind == "secret" && !questionView.isHidden)
    }

    var needsConfiguration: Bool { settings?.configured != true }
    var currentQuestionKind: String? { currentQuestion?.kind }
    var canSendMessage: Bool { !busy && available?() == true && job?.active == true && currentQuestion == nil && conversationCanMessage }
    var composerActionTitle: String {
        if inlineSettingsVisible { return "Complete agent settings" }
        if let question = currentQuestion { return question.kind == "secret" ? "Enter credential above" : "Continue" }
        if job?.active == true { return "Send" }
        return job?.status == "interrupted" ? "Resume setup" : ["failed", "canceled"].contains(job?.status ?? "") ? "Try again" : "Create app"
    }
    var composerPlaceholder: String {
        if inlineSettingsVisible { return "Complete the agent settings above…" }
        if let question = currentQuestion { return question.kind == "secret" ? "Use the secure credential field above…" : "Answer here, or choose an option above…" }
        return job?.active == true ? "Add a requirement or ask about this setup…" : "Describe an app, paste a link, or add files…"
    }
    var composerEnabled: Bool {
        guard available?() == true, !busy, !inlineSettingsVisible else { return false }
        if let question = currentQuestion { return question.kind == "text" }
        if job?.active == true { return canSendMessage }
        return startButton.isEnabled
    }

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 10
        startButton = NSButton(title: "Set Up App", target: self, action: #selector(startSetup))
        startButton.bezelColor = .controlAccentColor
        cancelButton = NSButton(title: "Cancel Setup", target: self, action: #selector(cancelSetup))
        retryButton = NSButton(title: "Refresh Status", target: self, action: #selector(refreshStatus))
        settingsButton = NSButton(title: "Model Settings…", target: self, action: #selector(showSettings))
        systemButton = NSButton(title: "Finish Metal Setup…", target: self, action: #selector(openSystemSetup))
        systemButton.isHidden = true
        answerButton = NSButton(title: "Continue", target: self, action: #selector(answerQuestion))
        answerField.isAutomaticTextCompletionEnabled = false; answerField.completes = false
        answerField.hasVerticalScroller = true; answerField.numberOfVisibleItems = 6
        answerField.setAccessibilityLabel("Answer the MeeCP agent's setup question")
        credentialField.isAutomaticTextCompletionEnabled = false
        credentialField.placeholderString = "Enter credential"; credentialField.setAccessibilityLabel("Requested app credential")
        questionText.setAccessibilityLabel("MeeCP agent setup question")
        questionText.isSelectable = true
        questionView.orientation = .vertical; questionView.alignment = .leading; questionView.spacing = 8
        optionsStack.orientation = .vertical; optionsStack.alignment = .leading; optionsStack.spacing = 6
        for view in [questionText, optionsStack, answerField, credentialField, questionNotice, answerButton!] {
            questionView.addArrangedSubview(view); workspaceFill(view, in: questionView)
        }
        questionView.isHidden = true
        progress.style = .spinning; progress.controlSize = .small; progress.isDisplayedWhenStopped = false
        modelRow = workspaceStack([modelLabel, settingsButton!], horizontal: true)
        let statusRow = workspaceStack([progress, status], horizontal: true)
        actionRow = workspaceStack([startButton!, cancelButton!, retryButton!, systemButton!], horizontal: true)
        eventStack.orientation = .vertical; eventStack.alignment = .leading; eventStack.spacing = 10
        eventStack.setAccessibilityLabel("Live setup conversation")
        settingsView.isHidden = true; conversationNotice.isHidden = true
        taskCard = piConversationTask([statusRow, errorDetail, questionView, actionRow!])
        for view in [eventStack, conversationNotice, modelRow!, notice, taskCard!, settingsView] {
            addArrangedSubview(view); workspaceFill(view, in: self)
        }
        status.setAccessibilityLabel("App setup progress")
        settingsView.request = { [weak self] path, method, body, completion in self?.request?(path, method, body, completion) }
        settingsView.available = { [weak self] in self?.available?() == true && self?.blocksNavigation == false }
        settingsView.onSaved = { [weak self] value in
            guard let self else { return }
            self.settings = value; self.inlineSettingsVisible = false; self.settingsView.isHidden = true
            let authorized = self.pendingConfiguredStart; self.pendingConfiguredStart = nil
            self.refreshAvailability(); self.onStateChange?(); self.onConversationChange?()
            if let authorized, self.context == authorized, self.eligible, !self.blocksNavigation {
                if value.ready { self.beginSetup(authorized) }
                else { self.fail("pi_runtime_unavailable") }
            }
        }
        settingsView.onClose = { [weak self] in
            guard let self else { return }
            self.pendingConfiguredStart = nil; self.inlineSettingsVisible = false; self.settingsView.isHidden = true
            self.clearCredentialEntry(); self.refreshAvailability(); self.onStateChange?(); self.onConversationChange?()
        }
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func useConversationPresentation() {
        if !conversationMode {
            technicalDetailsView.orientation = .vertical; technicalDetailsView.alignment = .leading
            if let stack = errorDetail.superview as? NSStackView { stack.removeArrangedSubview(errorDetail) }
            errorDetail.removeFromSuperview(); technicalDetailsView.addArrangedSubview(errorDetail)
            workspaceFill(errorDetail, in: technicalDetailsView)
        }
        conversationMode = true; refreshAvailability()
    }

    func bind(_ context: PreparationContext?, eligible: Bool) {
        if self.context != nil, self.context?.importID != context?.importID {
            guard !blocksNavigation else { return }
            job = nil; deliveredReady = nil; errorCode = nil
            errorDetail.stringValue = ""
            status.stringValue = "Describe the tools you want, then choose Create app."
            clearQuestion()
            clearConversation()
            onJobChange?(nil)
        }
        if self.context != context {
            // A save can advance this import while its previous status request
            // is still outstanding. Invalidate that reply and release its latch
            // so the new exact binding can be reconciled immediately.
            epoch &+= 1; polling = false; settingsLoading = false; conversationPolling = false
            pendingConfiguredStart = nil; inlineSettingsVisible = false; settingsView.isHidden = true
            settingsView.dismiss(); clearConversation()
        }
        self.context = context; self.eligible = eligible
        refreshAvailability()
        deliverReady()
    }

    func restore(_ id: String) {
        guard preparationUUID(id) != nil, job == nil else { return }
        restoredJobID = id; refreshAvailability()
    }

    func adopt(_ object: [String: Any]?, context: PreparationContext) {
        if let object {
            guard let next = PiSetupJob(object), next.importID == context.importID,
                  next.revision == context.revision, next.sourceRevision == context.sourceRevision else { return }
            epoch &+= 1; busy = false; polling = false; conversationPolling = false; restoredJobID = nil
            self.context = context; job = next; onJobChange?(next.id); showJob()
        } else {
            epoch &+= 1; busy = false; polling = false; conversationPolling = false; restoredJobID = nil
            self.context = context; job = nil; deliveredReady = nil; clearQuestion(); clearConversation(); onJobChange?(nil); refreshAvailability()
        }
    }

    func connectionReset() {
        epoch &+= 1; settings = nil; settingsLoading = false; busy = false; polling = false; conversationPolling = false
        pendingConfiguredStart = nil; inlineSettingsVisible = false; settingsView.isHidden = true; clearConversation()
        errorCode = nil; errorDetail.stringValue = ""
        clearQuestion()
        settingsView.connectionReset(); refreshAvailability()
    }

    func refreshAvailability() {
        let enabled = available?() == true
        let active = job?.active == true || restoredJobID != nil || busy
        let configured = settings?.configured == true
        let retryState = ["failed", "canceled", "interrupted"].contains(job?.status ?? "")
        startButton.title = configured && job?.status == "interrupted" ? "Resume Setup" : configured && retryState ? "Try Again" : configured ? "Set Up App" : "Configure Model"
        startButton.action = #selector(startSetup)
        startButton.isEnabled = enabled && !active && (configured ? eligible && settings?.ready == true : settings != nil)
        cancelButton.isHidden = job?.active != true
        cancelButton.isEnabled = enabled && !busy && job?.active == true
        retryButton.isEnabled = enabled && !busy && !polling && !settingsLoading
        settingsButton.isEnabled = enabled && !active && !settingsLoading && settings != nil
        settingsButton.isHidden = settings?.configured != true
        modelRow.isHidden = active || conversationMode
        notice.isHidden = active || conversationMode
        startButton.isHidden = active || conversationMode
        retryButton.isHidden = active || (errorCode == nil && restoredJobID == nil)
        systemButton.isHidden = job?.active != true || job?.stage != "waiting_for_system" || job?.systemAction == nil
        systemButton.isEnabled = enabled && !busy && systemVerification == nil && !systemButton.isHidden
        actionRow.isHidden = startButton.isHidden && cancelButton.isHidden && retryButton.isHidden && systemButton.isHidden
        errorDetail.isHidden = errorDetail.stringValue.isEmpty
        taskCard.isHidden = conversationMode && job == nil && !busy && errorCode == nil
        if let settings {
            modelLabel.stringValue = settings.configured ? "Setup model: " + settings.provider + " / " + settings.model : "Choose a model to set up your apps."
            if !settings.runtimeAvailable { modelLabel.stringValue += " Pi runtime unavailable." }
        } else { modelLabel.stringValue = enabled ? "Loading setup model…" : "Setup model unavailable while the host is disconnected." }
        updateQuestion()
        if busy || job?.active == true && job?.question == nil { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
        onConversationChange?()
    }

    private var currentQuestion: PiSetupQuestion? {
        guard available?() == true, let job, job.active, let question = job.question, let context,
              job.importID == context.importID, job.revision == context.revision, job.sourceRevision == context.sourceRevision,
              question.jobID == job.id, question.importID == context.importID, question.revision == context.revision else { return nil }
        return question
    }

    private func clearAnswerEntry() {
        for field in [answerField as NSTextField, credentialField] {
            if let editor = field.currentEditor() { editor.string = ""; editor.undoManager?.removeAllActions() }
            field.stringValue = ""
        }
    }

    private func clearQuestion() {
        clearAnswerEntry(); shownQuestionID = nil; questionText.stringValue = ""; questionNotice.stringValue = ""
        answerField.removeAllItems(); questionView.isHidden = true
    }

    private func updateQuestion() {
        guard let question = currentQuestion else { clearQuestion(); return }
        if shownQuestionID != question.id {
            clearAnswerEntry(); shownQuestionID = question.id
            questionText.stringValue = question.text
            answerField.removeAllItems(); answerField.addItems(withObjectValues: question.options)
            for button in optionButtons { optionsStack.removeArrangedSubview(button); button.removeFromSuperview() }
            optionButtons = question.options.map { option in
                let button = NSButton(radioButtonWithTitle: option, target: self, action: #selector(selectQuestionOption(_:)))
                button.setAccessibilityLabel("Answer option: " + option)
                button.lineBreakMode = .byWordWrapping; optionsStack.addArrangedSubview(button)
                workspaceFill(button, in: optionsStack); return button
            }
            answerField.placeholderString = question.options.isEmpty ? "Your answer" : "Type an answer or choose a suggestion"
            questionNotice.stringValue = question.kind == "secret"
                ? "Stored privately for your app. The agent receives a credential reference."
                : "Your answer is sent to the setup model for this run."
        }
        questionView.isHidden = false; answerField.isHidden = question.kind == "secret" || conversationMode; credentialField.isHidden = question.kind != "secret"
        optionsStack.isHidden = question.kind == "secret" || !conversationMode || question.options.isEmpty
        answerButton.isHidden = conversationMode && question.kind == "text"
        answerField.isEnabled = !busy; credentialField.isEnabled = !busy; answerButton.isEnabled = !busy
        for button in optionButtons { button.isEnabled = !busy }
    }

    @objc private func selectQuestionOption(_ sender: NSButton) {
        guard !busy, currentQuestion?.kind == "text", optionButtons.contains(where: { $0 === sender }) else { return }
        for button in optionButtons { button.state = button === sender ? .on : .off }
        answerField.stringValue = sender.title
        if conversationMode { onSuggestedAnswer?(sender.title) }
    }

    private func fail(_ code: String) {
        errorCode = preparationCode(code) ?? "pi_setup_failed"
        status.stringValue = presentationMessage(for: errorCode!)
        errorDetail.stringValue = "Details: " + errorCode!
        refreshAvailability(); onStateChange?()
    }

    private static func userMessage(for code: String) -> String {
        switch code {
        case "pi_settings_invalid", "pi_settings_required": return "Model settings are incomplete. Open Model Settings and choose a provider and model."
        case "pi_runtime_unavailable", "pi_runtime_changed": return "The bundled setup runtime is unavailable. Rebuild or reinstall meecp, then try again."
        case "pi_job_not_found": return "This setup run is no longer available. Start it again."
        case "pi_source_changed": return "The selected source changed during setup. Choose the source again and retry."
        case "pi_interrupted": return "Setup was interrupted. Resume using your saved files and resources."
        case "pi_deadline_exceeded": return "Setup took too long to finish. Check the source and try again."
        case "pi_credentials_unavailable": return "A requested app credential is unavailable. Try setup again and provide the credential when asked."
        case "pi_provider_auth_failed": return "The provider rejected the API key. Check Model Settings and try again."
        case "pi_provider_rate_limited": return "The provider is rate limiting requests. Wait briefly, then try again."
        case "pi_provider_timeout": return "The provider did not respond in time. Check the endpoint and try again."
        case "pi_provider_context_limit": return "The source is too large for this model. Choose a smaller source or a model with a larger context."
        case "pi_model_unsupported": return "The selected model does not support this setup. Choose another model."
        case "pi_provider_failed": return "The provider could not complete setup. Check Model Settings and try again."
        case "pi_job_unavailable": return "Setup is unavailable right now. Refresh status and try again."
        case "pi_question_changed": return "The setup question changed. Refresh the setup and answer the new question."
        case "pi_resource_blocked": return "The Mac cannot run another setup right now. Close other setup work and try again."
        case "pi_canceled", "canceled": return "Setup was canceled. Your draft is still available to try again."
        case "pi_xcode_untrusted": return "MeeCP could not verify this application as Apple Xcode. Install Xcode from Apple or refresh Metal setup."
        case "pi_system_action_failed": return "The Apple setup application could not open. Refresh Metal setup and try again."
        default: return "Setup could not finish. Check the draft and try again."
        }
    }

    private func presentationMessage(for code: String) -> String {
        let text = Self.userMessage(for: code)
        return conversationMode ? text.replacingOccurrences(of: "Model Settings", with: "Agent settings")
            .replacingOccurrences(of: "Model settings", with: "Agent settings")
            .replacingOccurrences(of: "Pi runtime", with: "setup runtime") : text
    }

    private func loadSettings() {
        guard available?() == true, !settingsLoading, !busy, let request else { return }
        settingsLoading = true; let expected = epoch
        request("v1/pi/settings", "GET", nil) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.settingsLoading = false
            guard let object, let settings = PiSettingsMetadata(object) else { self.fail(code ?? "pi_settings_invalid"); return }
            self.settings = settings; self.errorCode = nil; self.errorDetail.stringValue = ""
            self.refreshAvailability(); self.onStateChange?()
        }
    }

    func showInlineSettings() { showSettings() }

    func dismissInlineSettings() {
        pendingConfiguredStart = nil; inlineSettingsVisible = false; settingsView.isHidden = true
        settingsView.dismiss(); refreshAvailability()
    }

    @objc private func showSettings() {
        guard !blocksNavigation, available?() == true, let settings else { return }
        inlineSettingsVisible = true; settingsView.configure(settings); settingsView.isHidden = false
        refreshAvailability(); onStateChange?(); onConversationChange?()
    }

    func clearCredentialEntry() { settingsView.clearCredentialEntry(); clearAnswerEntry() }
    func windowWillClose(_ notification: Notification) { clearCredentialEntry() }

    @objc func startSetup() {
        guard startButton.isEnabled, !blocksNavigation, let beforeStart else { return }
        beforeStart { [weak self] context in
            guard let self, let context, self.available?() == true, !self.blocksNavigation,
                  self.context == context, self.eligible else { return }
            if self.settings?.configured != true {
                self.pendingConfiguredStart = context; self.showSettings(); return
            }
            self.beginSetup(context)
        }
    }

    private func beginSetup(_ context: PreparationContext) {
            guard self.context == context, self.available?() == true, !self.blocksNavigation,
                  self.eligible, self.settings?.ready == true, let request = self.request else { return }
            self.epoch &+= 1; self.busy = true; self.polling = false
            self.conversationPolling = false
            self.errorCode = nil; self.errorDetail.stringValue = ""; self.deliveredReady = nil
            self.clearQuestion()
            self.status.stringValue = "Starting app setup…"; self.refreshAvailability(); self.onStateChange?()
            let expected = self.epoch
            let resumable = self.job.map { ["failed", "canceled", "interrupted"].contains($0.status) && $0.importID == context.importID && $0.revision == context.revision && $0.sourceRevision == context.sourceRevision && $0.errorCode != "pi_ownership_lost" } == true
            let endpoint = resumable ? "v1/pi/jobs/" + self.job!.id + "/resume" : "v1/pi/jobs"
            request(endpoint, "POST", ["import_id": context.importID, "revision": context.revision]) { [weak self] object, code in
                guard let self, expected == self.epoch else { return }
                self.busy = false
                guard let object, let job = PiSetupJob(object), job.importID == context.importID, job.revision == context.revision,
                      job.sourceRevision == context.sourceRevision else {
                    self.fail(code ?? "pi_job_invalid"); return
                }
                self.job = job; self.onJobChange?(job.id); self.showJob()
            }
    }

    private func showJob() {
        guard let job else { return }
        if conversationJobID != job.id { clearConversation(); conversationJobID = job.id }
        errorCode = job.errorCode
        errorDetail.stringValue = errorCode.map { "Details: " + $0 } ?? ""
        switch job.status {
        case "running":
            let stages = ["starting": "Starting Pi…", "preparing": "Preparing the workspace…", "running": "Pi is setting up your app…",
                          "thinking": "Pi is working on your app…", "waiting_for_user": "Pi needs an answer to continue.",
                          "preflight": "Checking your Mac and prerequisites…", "preparing_resources": "Preparing retained resources…",
                          "preparing_metal": "Preparing Metal tools automatically…", "native_testing": "Testing the native runtime…",
                          "waiting_for_system": "Finish Apple's tool setup. MeeCP will continue automatically.",
                          "waiting_for_resources": "Waiting for available GPU and memory. Setup will continue automatically.",
                          "building": "Building your app…", "verifying": "Verifying the prepared app…", "attaching": "Preparing installation review…"]
            status.stringValue = stages[job.stage] ?? "Setting up your app: " + job.stage.replacingOccurrences(of: "_", with: " ") + "…"
            if conversationMode { status.stringValue = status.stringValue.replacingOccurrences(of: "Pi", with: "MeeCP agent") }
        case "ready": status.stringValue = "App prepared. Review its tools and access before adding it."
        case "canceled": status.stringValue = "App setup canceled. Your source and draft are kept."
        case "interrupted": status.stringValue = "Setup interrupted by a restart. Your files and resources are saved. Resume Setup to continue."
        default: status.stringValue = presentationMessage(for: job.errorCode ?? "pi_setup_failed")
        }
        refreshAvailability(); onStateChange?()
        deliverReady()
        pollConversation()
    }

    private func deliverReady() {
        guard let job, let context, job.status == "ready", deliveredReady != job.id,
              job.importID == context.importID, job.revision == context.revision,
              job.sourceRevision == context.sourceRevision else { return }
            deliveredReady = job.id; onReady?(job)
    }

    @objc func refreshStatus() { errorCode = nil; errorDetail.stringValue = ""; if settings == nil { loadSettings() }; poll(force: true) }

    func poll(force: Bool = false) {
        guard available?() == true else { return }
        if settings == nil, !settingsLoading, errorCode == nil { loadSettings() }
        guard !busy, !polling, let request else { return }
        if let context {
            polling = true; let expected = epoch
            request("v1/pi/current", "POST", ["import_id": context.importID, "revision": context.revision, "source_revision": context.sourceRevision]) { [weak self] object, code in
                guard let self, expected == self.epoch, self.context == context else { return }
                self.polling = false
                guard let object else { self.fail(code ?? "pi_job_invalid"); return }
                if let row = object["job"] as? [String: Any], let next = PiSetupJob(row),
                   next.importID == context.importID, next.revision == context.revision, next.sourceRevision == context.sourceRevision {
                    self.restoredJobID = nil; self.job = next; self.onJobChange?(next.id); self.showJob()
                } else if object["job"] is NSNull {
                    self.restoredJobID = nil; self.job = nil; self.clearConversation(); self.onJobChange?(nil); self.refreshAvailability()
                } else { self.fail("pi_job_invalid") }
            }
            return
        }
        guard let id = restoredJobID ?? job?.id, force || restoredJobID != nil || job?.active == true else { return }
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

    @objc private func openSystemSetup() {
        guard available?() == true, !busy, systemVerification == nil, let job, job.active,
              job.stage == "waiting_for_system", let action = job.systemAction,
              let kind = action["kind"] as? String else { return }
        if kind == "install_xcode", action["url"] as? String == "https://developer.apple.com/xcode/",
           let url = URL(string: "https://developer.apple.com/xcode/") {
            guard openSystemURL(url) else { fail("pi_system_action_failed"); return }
            continueSystem(job)
        } else if kind == "open_xcode", let path = action["application_path"] as? String {
            let verification = UUID(), expected = epoch
            systemVerification = verification; refreshAvailability()
            verifyXcodeApplication(path, applicationLaunchExclusions) { [weak self] app in
                guard let self, self.systemVerification == verification else { return }
                self.systemVerification = nil; self.refreshAvailability()
                guard expected == self.epoch, self.available?() == true,
                      let current = self.job, current.active, current.stage == "waiting_for_system",
                      current.id == job.id, current.importID == job.importID,
                      current.revision == job.revision, current.sourceRevision == job.sourceRevision,
                      (current.systemAction as NSDictionary?)?.isEqual(to: action) == true else { return }
                guard let app else { self.fail("pi_xcode_untrusted"); return }
                guard self.openSystemURL(app) else { self.fail("pi_system_action_failed"); return }
                self.continueSystem(current)
            }
        }
    }

    private func continueSystem(_ job: PiSetupJob) {
        request?("v1/pi/jobs/" + job.id + "/continue-system", "POST", [:]) { [weak self] _, _ in self?.poll(force: true) }
    }

    @objc func cancelSetup() {
        guard available?() == true, !busy, let job, job.active, let request else { return }
        epoch &+= 1; busy = true; polling = false; conversationPolling = false; clearAnswerEntry(); refreshAvailability(); onStateChange?(); let expected = epoch
        request("v1/pi/jobs/" + job.id + "/cancel", "POST", [:]) { [weak self] object, code in
            guard let self, expected == self.epoch else { return }
            self.busy = false
            guard let object, let next = PiSetupJob(object), next.id == job.id,
                  next.importID == job.importID, next.revision == job.revision,
                  next.sourceRevision == job.sourceRevision else { self.fail(code ?? "pi_job_invalid"); return }
            self.job = next; self.showJob()
        }
    }

    @objc func answerQuestion() {
        guard let question = currentQuestion else { return }
        let value = question.kind == "secret" ? credentialField.stringValue : answerField.stringValue
        submitAnswer(value)
    }

    private func submitAnswer(_ value: String, completion: ((Bool) -> Void)? = nil) {
        guard !busy, let job, let question = currentQuestion, let request else { completion?(false); return }
        guard !value.isEmpty, value.utf8.count <= (question.kind == "secret" ? 4096 : 8192),
              question.kind == "secret" || !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              preparationText(value, maximum: 8192) != nil else {
            status.stringValue = "Enter an answer to continue."; completion?(false); return
        }
        epoch &+= 1; busy = true; polling = false; conversationPolling = false; errorCode = nil; clearAnswerEntry()
        refreshAvailability(); onStateChange?(); let expected = epoch
        request("v1/pi/jobs/" + job.id + "/answer", "POST", ["question_id": question.id, "import_id": question.importID,
                "revision": question.revision, "value": value]) { [weak self] object, code in
            guard let self, expected == self.epoch else { completion?(false); return }
            self.busy = false
            guard let object, let next = PiSetupJob(object), next.id == job.id, next.importID == job.importID,
                  next.revision == job.revision, next.sourceRevision == job.sourceRevision else {
                self.fail(code ?? "pi_question_changed"); completion?(false); return
            }
            self.job = next; self.showJob(); completion?(true)
        }
    }

    /// Text questions stay on their exact /answer contract. Credentials are
    /// accepted only by the secure card, never by this freeform composer.
    func submitText(_ text: String, completion: @escaping (Bool) -> Void) {
        guard preparationText(text, maximum: 8192) != nil, text.utf8.count <= 8192,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { completion(false); return }
        if let question = currentQuestion {
            guard question.kind == "text" else { completion(false); return }
            submitAnswer(text, completion: completion); return
        }
        guard canSendMessage, let job, let context, let request,
              job.importID == context.importID, job.revision == context.revision,
              job.sourceRevision == context.sourceRevision else { completion(false); return }
        let messageID: String
        if let pending = unacknowledgedMessage, pending.text == text, pending.jobID == job.id, pending.context == context {
            messageID = pending.id
        } else {
            messageID = UUID().uuidString.lowercased()
            unacknowledgedMessage = (messageID, text, job.id, context)
        }
        epoch &+= 1; polling = false; conversationPolling = false; busy = true
        let expected = epoch; refreshAvailability()
        request("v1/pi/jobs/" + job.id + "/messages", "POST", ["import_id": context.importID, "revision": context.revision,
            "source_revision": context.sourceRevision, "message_id": messageID, "text": text]) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.job?.id == job.id else { completion(false); return }
            self.busy = false
            guard preparationBoolean(object?["accepted"]) == true, preparationBoolean(object?["duplicate"]) != nil,
                  preparationUUID(object?["message_id"]) == messageID else {
                self.conversationNotice.stringValue = "Your message could not be accepted. Check the current setup and try again."
                self.conversationNotice.isHidden = false; self.refreshAvailability(); self.poll(force: true); completion(false); return
            }
            self.unacknowledgedMessage = nil; self.refreshAvailability(); self.poll(force: true); completion(true)
        }
    }

    private func clearConversation() {
        conversationJobID = nil; conversationCursor = 0; conversationPolling = false
        conversationAvailable = false; conversationCanMessage = false; conversationEvents = []; unacknowledgedMessage = nil
        for view in eventStack.arrangedSubviews { eventStack.removeArrangedSubview(view); view.removeFromSuperview() }
        renderedEvents.removeAll()
        conversationNotice.stringValue = ""; conversationNotice.isHidden = true
    }

    private func pollConversation() {
        guard conversationMode, !conversationPolling, !busy, available?() == true, let context, let job, let request,
              job.importID == context.importID, job.revision == context.revision,
              job.sourceRevision == context.sourceRevision else { return }
        conversationPolling = true; let expected = epoch
        request("v1/pi/jobs/" + job.id + "/conversation", "POST", ["import_id": context.importID, "revision": context.revision,
            "source_revision": context.sourceRevision, "after": conversationCursor]) { [weak self] object, _ in
            guard let self, self.epoch == expected, self.context == context, self.job?.id == job.id else { return }
            self.conversationPolling = false
            guard let object, let batch = PiConversationBatch(object), batch.jobID == job.id else {
                self.conversationCanMessage = false; self.onConversationChange?(); return
            }
            if batch.reset || !batch.available { self.conversationEvents = [] }
            let previous = self.conversationEvents.last?.sequence ?? 0
            guard batch.reset || batch.events.allSatisfy({ $0.sequence > previous }) else { return }
            self.conversationEvents.append(contentsOf: batch.events)
            self.conversationEvents.removeAll { $0.sequence < batch.firstSequence }
            while self.conversationEvents.count > 128 || self.conversationEvents.reduce(0, { $0 + $1.text.utf8.count }) > 262144 {
                self.conversationEvents.removeFirst()
            }
            self.conversationCursor = batch.cursor; self.conversationAvailable = batch.available
            self.conversationCanMessage = batch.canMessage
            self.renderConversation(); self.onConversationChange?()
        }
    }

    private func renderConversation() {
        let retained = Set(conversationEvents.map(\.sequence))
        for sequence in Array(renderedEvents.keys) where !retained.contains(sequence) {
            if let old = renderedEvents.removeValue(forKey: sequence) {
                eventStack.removeArrangedSubview(old.view); old.view.removeFromSuperview()
            }
        }
        for (index, event) in conversationEvents.enumerated() {
            if let old = renderedEvents[event.sequence], old.event == event { continue }
            if let old = renderedEvents[event.sequence] {
                eventStack.removeArrangedSubview(old.view); old.view.removeFromSuperview()
            }
            let bubble = piConversationBubble(role: event.role, text: event.text)
            eventStack.insertArrangedSubview(bubble, at: index); workspaceFill(bubble, in: eventStack)
            renderedEvents[event.sequence] = (event, bubble)
        }
        conversationNotice.stringValue = conversationAvailable ? "Messages stay in memory for this setup. Credentials use the secure fields below."
            : "The earlier conversation is unavailable. Your draft and saved resources remain available."
        conversationNotice.isHidden = !conversationMode
    }

    static func conversationSelfTest() -> Bool {
        guard PiSettingsView.formSelfTest() else { return false }
        let importID = "11111111-2222-4333-8444-555555555555", jobID = "99999999-2222-4333-8444-555555555555"
        let source = "sha256:" + String(repeating: "a", count: 64)
        guard let context = PreparationContext(importID: importID, revision: 1, sourceRevision: source),
              let changed = PreparationContext(importID: importID, revision: 2, sourceRevision: source) else { return false }
        let configured: [String: Any] = ["configured": true, "provider": "openai", "model": "test-model", "base_url": NSNull(),
            "runtime_available": true, "available_providers": ["openai", "anthropic"]]
        var row: [String: Any] = ["id": jobID, "import_id": importID, "revision": 1, "source_revision": source,
            "status": "running", "stage": "thinking", "code": NSNull()]
        func batch(_ events: [[String: Any]], cursor: Int, first: Int = 1, reset: Bool = false, available: Bool = true) -> [String: Any] {
            ["api_version": 1, "job_id": jobID, "events": events, "cursor": cursor, "first_sequence": first,
             "reset": reset, "available": available, "can_message": available]
        }
        let assistant: [String: Any] = ["sequence": 1, "role": "assistant", "text": "I will inspect the supplied source.", "message_id": NSNull()]
        var invalid = assistant; invalid["role"] = "tool"
        guard PiConversationEvent(invalid) == nil,
              PiConversationBatch(batch([assistant, assistant], cursor: 1)) == nil,
              PiConversationBatch(batch([assistant], cursor: 0)) == nil else { return false }
        let view = PiSetupView(); view.available = { true }; view.settings = PiSettingsMetadata(configured)
        view.useConversationPresentation(); view.bind(context, eligible: true)
        var pendingConversation: (([String: Any]?, String?) -> Void)?
        var pendingMessage: (([String: Any]?, String?) -> Void)?
        var messageBodies: [[String: Any]] = []
        var validQuery = true, accepted = false
        view.request = { path, method, body, completion in
            if path.hasSuffix("/conversation") {
                validQuery = validQuery && method == "POST" && Set(body?.keys.map { $0 } ?? []) == ["import_id", "revision", "source_revision", "after"] &&
                    body?["import_id"] as? String == importID && preparationInteger(body?["revision"]) == view.context?.revision && body?["source_revision"] as? String == source && preparationInteger(body?["after"]) != nil
                pendingConversation = completion
            } else if path.hasSuffix("/messages"), let body {
                messageBodies.append(body); pendingMessage = completion
            } else if path == "v1/pi/current" { completion(["job": row], nil) }
        }
        view.adopt(row, context: context)
        pendingConversation?(batch([assistant], cursor: 1), nil)
        guard validQuery, view.canSendMessage, view.eventStack.arrangedSubviews.count == 1,
              let assistantBubble = view.eventStack.arrangedSubviews.first else { return false }
        view.pollConversation(); pendingConversation?(batch([], cursor: 1), nil)
        guard view.eventStack.arrangedSubviews.first === assistantBubble else { return false }
        var whitespaceAccepted = true
        view.submitText(" \n\t ") { whitespaceAccepted = $0 }
        guard !whitespaceAccepted, messageBodies.isEmpty else { return false }
        view.submitText("Keep the requested engine.") { accepted = $0 }
        guard messageBodies.count == 1, let id = preparationUUID(messageBodies[0]["message_id"]),
              messageBodies[0]["source_revision"] as? String == source, messageBodies[0]["import_id"] as? String == importID,
              preparationInteger(messageBodies[0]["revision"]) == 1, !view.canSendMessage else { return false }
        pendingMessage?(nil, "manager_request_failed")
        pendingConversation?(batch([], cursor: 1), nil)
        view.submitText("Keep the requested engine.") { accepted = $0 }
        guard messageBodies.count == 2, messageBodies[1]["message_id"] as? String == id else { return false }
        pendingMessage?(["accepted": true, "duplicate": true, "message_id": id], nil)
        guard accepted, view.conversationEvents.count == 1 else { return false }
        let user: [String: Any] = ["sequence": 2, "role": "user", "text": "Keep the requested engine.", "message_id": id]
        pendingConversation?(batch([user], cursor: 2), nil)
        guard view.conversationEvents.count == 2, view.eventStack.arrangedSubviews.count == 2,
              view.eventStack.arrangedSubviews.first === assistantBubble,
              let userBubble = view.eventStack.arrangedSubviews.last else { return false }
        view.pollConversation(); pendingConversation?(batch([], cursor: 2, first: 2), nil)
        guard view.eventStack.arrangedSubviews.count == 1, view.eventStack.arrangedSubviews.first === userBubble,
              assistantBubble.superview == nil, view.renderedEvents.count == 1 else { return false }
        view.pollConversation(); let stale = pendingConversation
        view.bind(changed, eligible: true)
        stale?(batch([user], cursor: 2), nil)
        guard view.conversationEvents.isEmpty, !view.canSendMessage else { return false }
        row["revision"] = 2; view.adopt(row, context: changed)
        pendingConversation?(batch([], cursor: 0, first: 0, reset: true, available: false), nil)
        guard !view.canSendMessage, view.conversationEvents.isEmpty,
              view.conversationNotice.stringValue.contains("unavailable") else { return false }

        let choosing = PiSetupView(); choosing.available = { true }; choosing.settings = PiSettingsMetadata(configured)
        choosing.useConversationPresentation(); choosing.bind(changed, eligible: true)
        let questionID = "77777777-2222-4333-8444-555555555555"
        var questionRow = row; questionRow["stage"] = "waiting_for_user"
        questionRow["question"] = ["id": questionID, "job_id": jobID, "import_id": importID, "revision": 2,
            "kind": "text", "question": "Which calendar?", "options": ["Work", "Personal"], "credential_name": NSNull()]
        var suggested: String?, chosen = false
        choosing.onSuggestedAnswer = { suggested = $0 }
        choosing.request = { path, method, body, done in
            if path.hasSuffix("/answer") {
                chosen = method == "POST" && body?["question_id"] as? String == questionID &&
                    body?["value"] as? String == "Personal" && preparationInteger(body?["revision"]) == 2
                done(row, nil)
            }
        }
        choosing.adopt(questionRow, context: changed)
        choosing.optionButtons.last?.performClick(nil)
        guard suggested == "Personal", choosing.answerButton.isHidden, choosing.answerField.isHidden,
              choosing.composerActionTitle == "Continue", choosing.composerEnabled else { return false }
        choosing.submitText("Personal") { chosen = chosen && $0 }
        guard chosen, choosing.currentQuestion == nil else { return false }

        let config = PiSetupView(); config.available = { true }; config.useConversationPresentation()
        var unconfigured = configured; unconfigured["configured"] = false; unconfigured["model"] = ""
        config.settings = PiSettingsMetadata(unconfigured); config.bind(context, eligible: true)
        config.beforeStart = { done in done(context) }
        var started = 0, privateKeySubmitted = false
        config.request = { path, method, body, done in
            if path == "v1/pi/settings", method == "PUT" {
                privateKeySubmitted = body?["api_key"] as? String == "Synthetic provider key"
                done(configured, nil)
            } else if path == "v1/pi/jobs", method == "POST" {
                started += 1; var fresh = row; fresh["revision"] = 1; done(fresh, nil)
            }
        }
        config.startSetup()
        guard config.inlineSettingsVisible, config.authorizationPresentationBlocked, started == 0,
              config.pendingConfiguredStart == context else { return false }
        guard PiSettingsView.submitSelfTest(config.settingsView, model: "test-model", key: "Synthetic provider key"),
              privateKeySubmitted, started == 1, config.job?.active == true, !config.inlineSettingsVisible,
              config.conversationEvents.isEmpty else { return false }
        let abandoned = PiSetupView(); abandoned.available = { true }; abandoned.settings = PiSettingsMetadata(unconfigured)
        abandoned.bind(context, eligible: true); abandoned.beforeStart = { done in done(context) }; abandoned.startSetup()
        abandoned.bind(changed, eligible: true)
        return abandoned.pendingConfiguredStart == nil && !abandoned.inlineSettingsVisible
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
              !PiSettingsView.validBaseURL("https://user:private@example.com/v1"),
              PiSetupView.userMessage(for: "pi_job_not_found").contains("Start it again"),
              !PiSetupView.userMessage(for: "raw provider response").contains("raw provider response") else { return false }
        var row: [String: Any] = ["id": jobID, "import_id": importID, "revision": 2, "source_revision": source,
            "status": "running", "stage": "building", "code": NSNull()]
        var invalid = row; invalid["code"] = "raw provider response"
        guard PiSetupJob(invalid) == nil else { return false }
        let view = PiSetupView(); view.available = { true }
        let unconfigured: [String: Any] = ["configured": false, "provider": "openai", "model": "", "base_url": NSNull(),
            "runtime_available": true, "available_providers": ["openai", "anthropic", "google", "openai-compatible"]]
        guard let unconfiguredSettings = PiSettingsMetadata(unconfigured) else { return false }
        view.settings = unconfiguredSettings; view.bind(revised, eligible: true); view.refreshAvailability()
        guard view.startButton.title == "Configure Model", view.startButton.isEnabled,
              view.errorDetail.stringValue.isEmpty else { return false }
        view.settings = PiSettingsMetadata(settings); view.bind(revised, eligible: true); view.refreshAvailability()
        guard view.startButton.title == "Set Up App", view.startButton.isEnabled else { return false }
        view.settings = PiSettingsMetadata(missingRuntime); view.refreshAvailability()
        guard view.startButton.title == "Set Up App", !view.startButton.isEnabled else { return false }
        view.settings = PiSettingsMetadata(settings)
        view.job = PiSetupJob(row); view.showJob()
        guard view.startButton.isHidden, view.modelRow.isHidden, !view.cancelButton.isHidden else { return false }
        row["status"] = "failed"; row["code"] = "pi_provider_failed"; view.job = PiSetupJob(row); view.showJob()
        guard view.startButton.title == "Try Again", view.startButton.isEnabled,
              view.status.stringValue.contains("provider") else { return false }
        view.fail("pi_settings_invalid")
        guard view.retryButton.isHidden == false, view.errorDetail.stringValue == "Details: pi_settings_invalid",
              !view.status.stringValue.contains("pi_settings_invalid") else { return false }
        row["status"] = "running"; row["stage"] = "building"; row["code"] = NSNull(); row.removeValue(forKey: "question")
        var postedRevision: Int?
        var postedEndpoint: String?
        var currentQueries = 0
        var validCurrentRequests = true
        let nextJobID = "66666666-2222-4333-8444-555555555555"
        var stalePoll: (([String: Any]?, String?) -> Void)?
        var readyCount = 0
        view.onReady = { _ in readyCount += 1 }
        view.job = nil; view.errorCode = nil; view.errorDetail.stringValue = ""; view.deliveredReady = nil
        view.request = { path, method, body, completion in
            if path == "v1/pi/settings" { completion(settings, nil) }
            else if path == "v1/pi/jobs", method == "POST" {
                postedRevision = preparationInteger(body?["revision"]); postedEndpoint = path; completion(row, nil)
            } else if path == "v1/pi/jobs/" + jobID + "/resume", method == "POST" {
                postedRevision = preparationInteger(body?["revision"]); postedEndpoint = path
                var next = row; next["id"] = nextJobID; completion(next, nil)
            } else if path.hasSuffix("/cancel") {
                var canceled = row; canceled["status"] = "canceled"; canceled["stage"] = "canceled"; completion(canceled, nil)
            } else if path == "v1/pi/current" {
                currentQueries += 1
                validCurrentRequests = validCurrentRequests && method == "POST" && body?["import_id"] as? String == importID &&
                    preparationInteger(body?["revision"]) == 1 && body?["source_revision"] as? String == source
                stalePoll = completion
            }
        }
        view.bind(original, eligible: true); view.poll()
        view.beforeStart = { completion in view.bind(revised, eligible: true); completion(revised) }
        view.startSetup()
        guard postedRevision == 2, postedEndpoint == "v1/pi/jobs", validCurrentRequests, currentQueries == 1,
              view.blocksNavigation, view.cancelButton.isEnabled else { return false }
        view.poll(); view.cancelSetup()
        row["status"] = "ready"; row["stage"] = "ready"
        stalePoll?(["job": row], nil)
        guard view.job?.status == "canceled", !view.blocksNavigation, readyCount == 0,
              view.startButton.title == "Try Again" else { return false }
        view.startSetup()
        guard view.job?.status == "ready", view.job?.id == nextJobID, readyCount == 1,
              postedEndpoint == "v1/pi/jobs/" + jobID + "/resume", postedRevision == 2 else { return false }
        view.bind(revised, eligible: false); view.bind(revised, eligible: false)
        guard readyCount == 1 else { return false }

        // Saving a revision and starting setup can both supersede an in-flight
        // current-job request. Its late reply must not pin the polling latch or
        // overwrite the new context and attempt.
        let rebinding = PiSetupView(); rebinding.available = { true }
        rebinding.settings = PiSettingsMetadata(settings); rebinding.bind(original, eligible: true)
        var pendingCurrent: [(([String: Any]?, String?) -> Void)] = []
        var rebindingReady = 0
        var freshRow = row; freshRow["id"] = nextJobID; freshRow["revision"] = 2
        freshRow["status"] = "running"; freshRow["stage"] = "preflight"
        rebinding.onReady = { _ in rebindingReady += 1 }
        rebinding.request = { path, method, body, completion in
            if path == "v1/pi/current", method == "POST" { pendingCurrent.append(completion) }
            else if path == "v1/pi/jobs", method == "POST", preparationInteger(body?["revision"]) == 2 {
                completion(freshRow, nil)
            }
        }
        rebinding.poll(); rebinding.bind(revised, eligible: true); rebinding.poll()
        guard pendingCurrent.count == 2 else { return false }
        var oldReady = row; oldReady["revision"] = 1
        pendingCurrent[0](["job": oldReady], nil)
        guard rebinding.job == nil, rebindingReady == 0 else { return false }
        pendingCurrent[1](["job": NSNull()], nil)
        rebinding.poll()
        guard pendingCurrent.count == 3 else { return false }
        rebinding.beforeStart = { completion in completion(revised) }
        rebinding.startSetup(); rebinding.poll()
        guard pendingCurrent.count == 4, rebinding.job?.id == nextJobID else { return false }
        pendingCurrent[2](["job": NSNull()], nil)
        guard rebinding.job?.id == nextJobID else { return false }
        freshRow["status"] = "ready"; freshRow["stage"] = "ready"
        pendingCurrent[3](["job": freshRow], nil)
        guard rebindingReady == 1, rebinding.job?.status == "ready", !rebinding.blocksNavigation else { return false }

        let resumed = PiSetupView(); resumed.available = { true }
        var restoredGET = false
        resumed.request = { path, method, body, completion in
            if path == "v1/pi/settings" { completion(settings, nil) }
            else if path == "v1/pi/jobs/" + jobID && method == "GET" && body == nil {
                restoredGET = true; completion(row, nil)
            } else if path == "v1/pi/current" && method == "POST" { completion(["job": row], nil) }
        }
        resumed.onReady = { _ in readyCount += 1 }; resumed.restore(jobID); resumed.poll()
        guard readyCount == 1, restoredGET else { return false }
        resumed.bind(revised, eligible: false)
        guard readyCount == 2 && !resumed.blocksNavigation else { return false }

        // A persisted canceled attempt cannot pin polling to its old ID. The
        // current-job route supplies the unique active attempt for this binding.
        let authoritative = PiSetupView(); authoritative.available = { true }
        authoritative.settings = PiSettingsMetadata(settings)
        var oldCanceled = row; oldCanceled["status"] = "canceled"; oldCanceled["stage"] = "canceled"
        var active = row; active["id"] = nextJobID; active["status"] = "running"; active["stage"] = "preparing_resources"
        authoritative.restore(jobID); authoritative.job = PiSetupJob(oldCanceled); authoritative.bind(revised, eligible: true)
        var currentRow: [String: Any]? = active
        var authoritativeRequests = 0, validAuthorityRequest = true
        authoritative.request = { path, method, body, completion in
            authoritativeRequests += 1
            validAuthorityRequest = validAuthorityRequest && path == "v1/pi/current" && method == "POST" &&
                body?["import_id"] as? String == importID && preparationInteger(body?["revision"]) == authoritative.context?.revision &&
                body?["source_revision"] as? String == authoritative.context?.sourceRevision
            completion(["job": currentRow.map { $0 as Any } ?? NSNull()], nil)
        }
        authoritative.poll()
        guard validAuthorityRequest, authoritativeRequests == 1, authoritative.job?.id == nextJobID,
              authoritative.job?.active == true, authoritative.restoredJobID == nil, authoritative.blocksNavigation else { return false }
        var wrongCurrent = active; wrongCurrent["revision"] = 1; currentRow = wrongCurrent; authoritative.poll()
        guard authoritative.job?.id == nextJobID, authoritative.errorCode == "pi_job_invalid" else { return false }
        wrongCurrent = active; wrongCurrent["source_revision"] = "sha256:" + String(repeating: "b", count: 64)
        currentRow = wrongCurrent; authoritative.poll()
        guard authoritative.job?.id == nextJobID, authoritative.errorCode == "pi_job_invalid" else { return false }
        currentRow = active; authoritative.poll()
        guard authoritative.errorCode == nil, authoritative.job?.id == nextJobID else { return false }
        currentRow = nil; authoritative.poll()
        guard authoritative.job == nil, !authoritative.blocksNavigation else { return false }

        // Resource changes grant a new exact context. Only its matching host
        // record may be adopted; an earlier source/revision cannot replace it.
        guard let advanced = PreparationContext(importID: importID, revision: 3, sourceRevision: source) else { return false }
        let advancedJobID = "55555555-2222-4333-8444-555555555555"
        var advancedRow = active; advancedRow["id"] = advancedJobID; advancedRow["revision"] = 3
        authoritative.adopt(advancedRow, context: advanced)
        guard authoritative.context == advanced, authoritative.job?.id == advancedJobID,
              authoritative.job?.revision == 3, authoritative.blocksNavigation else { return false }
        authoritative.adopt(active, context: advanced)
        guard authoritative.job?.id == advancedJobID, authoritative.context == advanced else { return false }
        advancedRow["stage"] = "waiting_for_system"
        advancedRow["system_action"] = ["kind": "install_xcode", "url": "https://developer.apple.com/xcode/"]
        authoritative.adopt(advancedRow, context: advanced)
        guard !authoritative.systemButton.isHidden, authoritative.systemButton.isEnabled,
              authoritative.status.stringValue.contains("Apple"), authoritative.job?.systemAction != nil else { return false }
        // Exercise the real launch controller with injected effects. No app or
        // browser opens, and no continuation is sent before local verification.
        let system = PiSetupView(); system.available = { true }
        var pendingVerification: ((URL?) -> Void)?
        var opened: [URL] = [], continuations = 0
        system.verifyXcodeApplication = { _, _, completion in pendingVerification = completion }
        system.openSystemURL = { opened.append($0); return true }
        system.request = { path, _, _, _ in if path.hasSuffix("/continue-system") { continuations += 1 } }
        var systemRow = advancedRow
        systemRow["system_action"] = ["kind": "open_xcode", "application_path": "/workspace/Fake.app"]
        system.adopt(systemRow, context: advanced); system.openSystemSetup()
        guard opened.isEmpty, continuations == 0, !system.systemButton.isEnabled,
              system.cancelButton.isEnabled else { return false }
        pendingVerification?(nil)
        guard opened.isEmpty, continuations == 0, system.errorCode == "pi_xcode_untrusted" else { return false }
        let verified = URL(fileURLWithPath: "/Applications/Xcode Beta.app")
        systemRow["system_action"] = ["kind": "open_xcode", "application_path": "/alias/Xcode.app"]
        system.adopt(systemRow, context: advanced); system.openSystemSetup(); pendingVerification?(verified)
        guard opened == [verified], continuations == 1 else { return false }
        system.adopt(systemRow, context: advanced); system.openSystemSetup()
        var canceled = systemRow; canceled["status"] = "canceled"
        system.adopt(canceled, context: advanced); pendingVerification?(verified)
        guard opened.count == 1, continuations == 1 else { return false }
        system.adopt(systemRow, context: advanced); system.openSystemSetup()
        var changed = systemRow; changed["stage"] = "building"
        system.job = PiSetupJob(changed); pendingVerification?(verified)
        guard opened.count == 1, continuations == 1 else { return false }
        system.adopt(systemRow, context: advanced); system.openSystemSetup()
        changed = systemRow
        changed["system_action"] = ["kind": "open_xcode", "application_path": "/changed/Xcode.app"]
        system.job = PiSetupJob(changed); pendingVerification?(verified)
        guard opened.count == 1, continuations == 1 else { return false }
        system.adopt(systemRow, context: advanced); system.openSystemSetup()
        system.openSystemURL = { _ in false }; pendingVerification?(verified)
        guard opened.count == 1, continuations == 1, system.errorCode == "pi_system_action_failed" else { return false }
        system.openSystemURL = { opened.append($0); return true }
        system.adopt(systemRow, context: advanced); system.openSystemSetup(); system.connectionReset()
        pendingVerification?(verified)
        guard opened.count == 1, continuations == 1 else { return false }
        systemRow["system_action"] = ["kind": "install_xcode", "url": "https://developer.apple.com/xcode/"]
        system.adopt(systemRow, context: advanced); system.openSystemSetup()
        guard opened.last?.absoluteString == "https://developer.apple.com/xcode/", continuations == 2 else { return false }
        advancedRow.removeValue(forKey: "system_action")
        advancedRow["status"] = "interrupted"; advancedRow["stage"] = "interrupted"; advancedRow["code"] = "pi_interrupted"
        authoritative.adopt(advancedRow, context: advanced)
        guard authoritative.startButton.title == "Resume Setup", authoritative.startButton.isEnabled,
              authoritative.systemButton.isHidden, authoritative.status.stringValue.contains("saved") else { return false }
        var resumeEndpoint: String?, resumeRevision: Int?
        authoritative.beforeStart = { completion in completion(advanced) }
        authoritative.request = { path, method, body, completion in
            guard method == "POST", body?["import_id"] as? String == importID else { return }
            resumeEndpoint = path; resumeRevision = preparationInteger(body?["revision"])
            var fresh = advancedRow; fresh["id"] = "44444444-2222-4333-8444-555555555555"
            fresh["status"] = "running"; fresh["stage"] = "preflight"; fresh["code"] = NSNull(); completion(fresh, nil)
        }
        authoritative.startButton.performClick(nil)
        guard resumeEndpoint == "v1/pi/jobs/" + advancedJobID + "/resume", resumeRevision == 3,
              authoritative.job?.id == "44444444-2222-4333-8444-555555555555", authoritative.job?.active == true else { return false }
        for terminal in ["failed", "canceled"] {
            var retryRow = advancedRow; retryRow["status"] = terminal; retryRow["stage"] = terminal
            retryRow["code"] = terminal == "failed" ? "pi_provider_failed" : NSNull()
            authoritative.adopt(retryRow, context: advanced); resumeEndpoint = nil
            authoritative.startSetup()
            guard resumeEndpoint == "v1/pi/jobs/" + advancedJobID + "/resume", resumeRevision == 3 else { return false }
        }

        let questionID = "77777777-2222-4333-8444-555555555555"
        var question: [String: Any] = ["id": questionID, "job_id": jobID, "import_id": importID, "revision": 2,
            "kind": "text", "question": "Which calendar should your app use?", "options": ["Work", "Personal"], "credential_name": NSNull()]
        row["status"] = "running"; row["stage"] = "waiting_for_user"; row["question"] = question
        invalid = row; var wrongQuestion = question; wrongQuestion["revision"] = 1; invalid["question"] = wrongQuestion
        guard PiSetupJob(invalid) == nil else { return false }
        invalid = row; invalid["status"] = "ready"
        guard PiSetupJob(invalid) == nil else { return false }

        let answering = PiSetupView(); answering.available = { true }
        var answerCount = 0
        var questionPoll: (([String: Any]?, String?) -> Void)?
        var answerReply: (([String: Any]?, String?) -> Void)?
        answering.request = { path, method, body, completion in
            if path == "v1/pi/settings" { completion(settings, nil) }
            else if path == "v1/pi/jobs", method == "POST" { completion(row, nil) }
            else if path.hasSuffix("/answer") {
                guard body?["question_id"] as? String == question["id"] as? String,
                      body?["import_id"] as? String == importID, preparationInteger(body?["revision"]) == 2,
                      body?["value"] as? String == (question["kind"] as? String == "secret" ? "Synthetic credential" : "Personal") else { return }
                answerCount += 1; answerReply = completion
            } else if path.hasSuffix("/cancel") {
                var canceled = row; canceled["status"] = "canceled"; canceled["stage"] = "canceled"; canceled.removeValue(forKey: "question")
                completion(canceled, nil)
            } else if path == "v1/pi/current", method == "POST" {
                guard body?["import_id"] as? String == importID, body?["source_revision"] as? String == source,
                      preparationInteger(body?["revision"]) == answering.context?.revision else { return }
                questionPoll = completion
            }
        }
        answering.bind(revised, eligible: true); answering.poll(); answering.beforeStart = { completion in completion(revised) }
        answering.startSetup()
        guard answering.currentQuestion?.id == questionID, !answering.questionView.isHidden,
              answering.credentialField.isHidden, answering.answerField.numberOfItems == 2, answering.blocksNavigation,
              !answering.authorizationPresentationBlocked else { return false }
        answering.answerField.stringValue = "Personal"
        // An independent authorization lifecycle cannot reset an inline Pi answer.
        let consent = ExternalAuthorizationCoordinator(); consent.invalidate()
        guard answering.answerField.stringValue == "Personal", answering.currentQuestion?.id == questionID else { return false }
        answering.bind(original, eligible: true); answering.answerField.stringValue = "Personal"; answering.answerQuestion()
        guard answerCount == 0, answering.questionView.isHidden else { return false }
        answering.bind(revised, eligible: true); answering.answerField.stringValue = "Personal"; answering.poll()
        let staleQuestionRow = row
        answering.answerQuestion()
        guard answerCount == 1, answering.answerField.stringValue.isEmpty else { return false }
        row.removeValue(forKey: "question"); row["stage"] = "thinking"
        answerReply?(row, nil); questionPoll?(["job": staleQuestionRow], nil)
        guard answering.currentQuestion == nil, answering.job?.stage == "thinking", answering.questionView.isHidden else { return false }

        question["id"] = "88888888-2222-4333-8444-555555555555"; question["kind"] = "secret"
        question["question"] = "Enter the app credential."; question["options"] = []; question["credential_name"] = "API_KEY"
        row["question"] = question; row["stage"] = "waiting_for_user"
        answering.poll(); questionPoll?(["job": row], nil)
        guard !answering.credentialField.isHidden, answering.answerField.isHidden, answering.authorizationPresentationBlocked else { return false }
        answering.credentialField.stringValue = "Synthetic credential"; answering.answerQuestion()
        guard answerCount == 2, answering.credentialField.stringValue.isEmpty else { return false }
        row.removeValue(forKey: "question"); row["stage"] = "thinking"; answerReply?(row, nil)
        guard answering.currentQuestion == nil else { return false }
        row["question"] = question; row["stage"] = "waiting_for_user"
        answering.poll(); questionPoll?(["job": row], nil); answering.credentialField.stringValue = "Synthetic credential"
        answering.cancelSetup(); questionPoll?(["job": row], nil)
        return answering.job?.status == "canceled" && answering.credentialField.stringValue.isEmpty && answering.questionView.isHidden && !answering.blocksNavigation
    }
}
