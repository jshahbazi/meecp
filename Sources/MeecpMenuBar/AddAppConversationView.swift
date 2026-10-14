import AppKit

/// The Add workspace owns one composer and one draft. This view only presents
/// that lifecycle; all source choices, saving and installation remain host actions.
final class AddAppConversationView: NSView {
    var onSend: ((String) -> Void)?
    var onChooseSource: (() -> Void)?
    var onChoosePackage: (() -> Void)?
    var onSourceLink: (() -> Void)?
    var onOtherWays: (() -> Void)?
    var onEditRequest: (() -> Void)?
    let contextContainer = NSStackView()
    let alternateContainer = NSStackView()
    let reviewContainer = NSStackView()
    private let setup: PiSetupView
    private let resources: PiResourcesView
    private let composer: NSScrollView
    private let sourceOptions: NSView
    private let titleLabel = workspaceLabel("Add an app", size: 23, weight: .semibold)
    private let subtitleLabel = workspaceLabel("An agent to turn your idea into a working tool", size: 12, secondary: true)
    private let statusLabel = workspaceLabel("New app", size: 11, weight: .medium, secondary: true)
    private let thread = WorkspaceDocumentStack()
    private let threadScroll = NSScrollView()
    private let welcome = workspaceStack(spacing: 10)
    private let requestStack = NSStackView()
    private let detailsStack = NSStackView()
    private let composerHint = workspaceLabel("Links, files, or a description", size: 11, secondary: true)
    private let footerHint = workspaceLabel("", size: 11, secondary: true)
    private let sourceSummary = workspaceLabel("", size: 11, secondary: true)
    private var sendButton: NSButton!
    private var attachButton: NSPopUpButton!
    private var detailsButton: NSButton!
    private var settingsButton: NSButton!
    private var editButton: NSButton!
    private var agentVisible = true
    private var writable = true
    private var composerSendAllowed = true
    private var hasIntent = false
    private var detailsVisible = false
    private var presentedIntent = ""
    private var presentedContext: PreparationContext?
    private var installationStatus: String?
    private var prepared = false
    private var canEditRequest = false

    init(composer: NSScrollView, setup: PiSetupView, resources: PiResourcesView, sourceOptions: NSView) {
        self.composer = composer; self.setup = setup; self.resources = resources; self.sourceOptions = sourceOptions
        super.init(frame: .zero)
        // NSTabView owns this root's frame; only its contents use constraints.
        autoresizingMask = [.width, .height]
        setup.useConversationPresentation()
        setup.onConversationChange = { [weak self] in
            guard let self else { return }
            let following = self.threadScroll.contentView.bounds.maxY >= self.thread.bounds.height - 48
            self.refresh()
            if following {
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.layoutSubtreeIfNeeded()
                    let y = max(0, self.thread.bounds.height - self.threadScroll.contentView.bounds.height)
                    self.threadScroll.contentView.scroll(to: NSPoint(x: 0, y: y))
                    self.threadScroll.reflectScrolledClipView(self.threadScroll.contentView)
                }
            }
        }
        setup.onSuggestedAnswer = { [weak self] answer in
            guard let self, let text = self.composer.documentView as? NSTextView else { return }
            text.string = answer; text.didChangeText()
            self.window?.makeFirstResponder(text); self.refresh()
        }
        titleLabel.setAccessibilityLabel("App draft title")
        statusLabel.setAccessibilityLabel("App preparation state")
        detailsButton = NSButton(title: "Details", target: self, action: #selector(toggleDetails))
        detailsButton.bezelStyle = .rounded
        let heading = workspaceStack([workspaceStack([titleLabel, subtitleLabel], spacing: 4), statusLabel, detailsButton!], horizontal: true)
        heading.distribution = .fill

        let welcomeTitle = workspaceLabel("What should your app do?", size: 24, weight: .semibold)
        let welcomeText = workspaceLabel("Describe the tool you want, or share a repository, model, or existing app. MeeCP will prepare it and ask here when it needs a choice.", size: 14, secondary: true)
        for view in [welcomeTitle, welcomeText] { welcome.addArrangedSubview(view); workspaceFill(view, in: welcome) }
        requestStack.orientation = .vertical; requestStack.alignment = .leading; requestStack.spacing = 8
        for stack in [contextContainer, alternateContainer] {
            stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
        }
        alternateContainer.isHidden = true
        reviewContainer.orientation = .vertical; reviewContainer.alignment = .leading; reviewContainer.spacing = 12
        reviewContainer.setAccessibilityLabel("App installation review")
        editButton = NSButton(title: "Edit request", target: self, action: #selector(editRequest))
        editButton.bezelStyle = .inline; editButton.isHidden = true
        detailsStack.orientation = .vertical; detailsStack.alignment = .leading; detailsStack.spacing = 12
        for view in [sourceSummary, sourceOptions, resources, setup.technicalDetailsView] {
            detailsStack.addArrangedSubview(view); workspaceFill(view, in: detailsStack)
        }
        detailsStack.isHidden = true
        thread.orientation = .vertical; thread.alignment = .leading; thread.spacing = 18
        thread.edgeInsets = NSEdgeInsets(top: 24, left: 8, bottom: 24, right: 8)
        for view in [contextContainer, welcome, requestStack, editButton!, setup, alternateContainer, reviewContainer, detailsStack] {
            thread.addArrangedSubview(view); workspaceFill(view, in: thread, inset: 8)
        }
        thread.translatesAutoresizingMaskIntoConstraints = false
        threadScroll.hasVerticalScroller = true; threadScroll.drawsBackground = false
        threadScroll.documentView = thread
        thread.widthAnchor.constraint(equalTo: threadScroll.contentView.widthAnchor).isActive = true
        threadScroll.setAccessibilityLabel("App setup conversation")

        composer.borderType = .noBorder; composer.drawsBackground = false
        if let text = composer.documentView as? NSTextView {
            text.font = .systemFont(ofSize: 14); text.drawsBackground = false
            text.textContainerInset = NSSize(width: 8, height: 8)
            text.setAccessibilityLabel("Message the app setup agent")
        }
        attachButton = NSPopUpButton(); attachButton.pullsDown = true
        attachButton.addItems(withTitles: ["Add…", "Repository or source…", "Paste a source link", "Model or runtime files…", "Existing app package…", "Other setup methods"])
        attachButton.menu?.autoenablesItems = false
        attachButton.target = self; attachButton.action = #selector(chooseAttachment)
        attachButton.setAccessibilityLabel("Add files or a source to this app")
        sendButton = NSButton(title: "Create app", target: self, action: #selector(send))
        sendButton.bezelStyle = .rounded; sendButton.bezelColor = .controlAccentColor
        sendButton.keyEquivalent = "\r"; sendButton.keyEquivalentModifierMask = [.command]
        let composerControls = workspaceStack([attachButton!, composerHint, sendButton!], horizontal: true, spacing: 10)
        let composerCard = NSBox(); composerCard.boxType = .custom; composerCard.titlePosition = .noTitle
        composerCard.fillColor = .controlBackgroundColor; composerCard.borderColor = .separatorColor
        composerCard.borderWidth = 1; composerCard.cornerRadius = 13; composerCard.contentViewMargins = .zero
        let composerContent = workspaceStack([composer, composerControls], spacing: 6)
        composerCard.contentView!.addSubview(composerContent)
        NSLayoutConstraint.activate([
            composerContent.leadingAnchor.constraint(equalTo: composerCard.contentView!.leadingAnchor, constant: 12),
            composerContent.trailingAnchor.constraint(equalTo: composerCard.contentView!.trailingAnchor, constant: -12),
            composerContent.topAnchor.constraint(equalTo: composerCard.contentView!.topAnchor, constant: 8),
            composerContent.bottomAnchor.constraint(equalTo: composerCard.contentView!.bottomAnchor, constant: -10),
        ])
        workspaceFill(composer, in: composerContent); workspaceFill(composerControls, in: composerContent)
        settingsButton = NSButton(title: "Agent settings", target: self, action: #selector(openSettings))
        settingsButton.bezelStyle = .inline
        let footer = workspaceStack([footerHint, settingsButton!], horizontal: true)
        let content = workspaceStack([heading, threadScroll, composerCard, footer], spacing: 12)
        addSubview(content)
        for view in [heading, threadScroll, composerCard, footer] { workspaceFill(view, in: content) }
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 20),
            content.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -20),
            content.topAnchor.constraint(equalTo: topAnchor, constant: 20),
            content.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -18),
            threadScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180),
        ])
        threadScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        threadScroll.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        refresh()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func update(title: String, intent: String, source: String?, context: PreparationContext?, installationStatus: String? = nil,
                canEditRequest: Bool = false, prepared: Bool = false, readOnly: Bool = false) {
        titleLabel.stringValue = title.isEmpty ? "Add an app" : title
        hasIntent = context != nil && !intent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        subtitleLabel.stringValue = hasIntent ? "Your app requirements" : "An agent to turn your idea into a working tool"
        self.installationStatus = installationStatus; writable = !readOnly
        sourceSummary.stringValue = source.map { "Selected source: " + $0 } ?? "No additional source selected."
        if presentedIntent != intent || presentedContext?.importID != context?.importID {
            presentedIntent = intent; presentedContext = context
            for view in requestStack.arrangedSubviews { requestStack.removeArrangedSubview(view); view.removeFromSuperview() }
            if hasIntent {
                let bubble = piConversationBubble(role: "user", text: intent)
                requestStack.addArrangedSubview(bubble); workspaceFill(bubble, in: requestStack)
            }
        }
        self.prepared = prepared; self.canEditRequest = canEditRequest
        editButton.isHidden = !hasIntent || !canEditRequest || readOnly || setup.blocksNavigation
        reviewContainer.isHidden = !prepared && reviewContainer.arrangedSubviews.allSatisfy(\.isHidden)
        refresh()
    }

    /// Manual preparation and external-agent handoff reuse the shared composer
    /// and installation review without running a second Pi lifecycle.
    func setAgentVisible(_ visible: Bool) {
        if !visible && agentVisible { setup.dismissInlineSettings() }
        agentVisible = visible; setup.isHidden = !visible; settingsButton.isHidden = !visible; refresh()
    }

    func setMethod(_ method: Int) { setAgentVisible(method == 0); alternateContainer.isHidden = method == 0 }
    func setPrepared(_ value: Bool) { prepared = value; refresh() }
    func setCanEditRequest(_ value: Bool) { canEditRequest = value; refresh() }

    func setComposerEnabled(_ enabled: Bool) { composerSendAllowed = enabled; refresh() }

    func revealSourceOptions() {
        detailsVisible = true; detailsStack.isHidden = false; refresh()
    }

    func refresh() {
        welcome.isHidden = hasIntent || setup.job != nil
        requestStack.isHidden = !hasIntent
        editButton.isHidden = !hasIntent || !canEditRequest || !writable || setup.blocksNavigation || prepared
        if let installationStatus { statusLabel.stringValue = installationStatus }
        else if let job = setup.job {
            statusLabel.stringValue = job.status == "running" ? job.question != nil ? "Needs your input" : job.stage == "waiting_for_system" ? "Waiting for Apple" : "Working"
                : ["ready": "Ready to install", "interrupted": "Interrupted", "canceled": "Stopped", "failed": "Needs attention"][job.status] ?? "Draft"
        } else { statusLabel.stringValue = hasIntent ? "Draft" : "New app" }
        sendButton.title = agentVisible ? setup.composerActionTitle : "Save request"
        let text = composer.documentView as? NSTextView
        let hasMessage = text?.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
        let needsMessage = agentVisible && setup.job?.active == true
        sendButton.isEnabled = writable && composerSendAllowed && (agentVisible ? setup.composerEnabled : true) && (!needsMessage || hasMessage)
        if let text = composer.documentView as? NSTextView {
            // The root owns text storage and editability, including draft vs
            // follow-up mode; presentation never clears or revises that text.
            text.setAccessibilityHelp(agentVisible ? setup.composerPlaceholder : "Describe the tools and behavior for this app")
            if let brief = text as? AppBriefTextView {
                brief.placeholderText = agentVisible ? setup.composerPlaceholder : "Describe the tools and behavior for this app…"
            }
        }
        composerHint.stringValue = agentVisible && setup.job?.active == true ? "Message this setup" : "Links, files, or a description"
        footerHint.stringValue = setup.job?.active == true ? "You can leave this window. Setup stays available." : "Your draft and selected resources stay on this Mac."
        settingsButton.isEnabled = agentVisible && !setup.blocksNavigation
        let sourceAllowed = writable && !resources.blocksNavigation && !setup.blocksNavigation
        for index in [1, 2, 4, 5] { attachButton.item(at: index)?.isEnabled = sourceAllowed }
        attachButton.item(at: 3)?.isEnabled = resources.canChooseResources
        attachButton.isEnabled = sourceAllowed || resources.canChooseResources
        detailsButton.title = detailsVisible ? "Hide details" : "Details"
    }

    @objc private func send() {
        guard sendButton.isEnabled, let text = composer.documentView as? NSTextView else { return }
        onSend?(text.string)
    }
    @objc private func editRequest() { onEditRequest?() }
    @objc private func toggleDetails() {
        detailsVisible.toggle(); detailsStack.isHidden = !detailsVisible; refresh()
    }
    @objc private func openSettings() { setup.showInlineSettings() }
    @objc private func chooseAttachment() {
        guard attachButton.selectedItem?.isEnabled == true else { return }
        switch attachButton.indexOfSelectedItem {
        case 1: revealSourceOptions(); onChooseSource?()
        case 2: detailsVisible = true; detailsStack.isHidden = false; onSourceLink?()
        case 3: detailsVisible = true; detailsStack.isHidden = false; resources.chooseExistingResources()
        case 4: onChoosePackage?()
        case 5: onOtherWays?()
        default: break
        }
        refresh()
    }

    static func selfTest() -> Bool {
        let composer = NSScrollView(); let text = NSTextView(); composer.documentView = text
        let setup = PiSetupView(); let resources = PiResourcesView()
        let view = AddAppConversationView(composer: composer, setup: setup, resources: resources, sourceOptions: NSView())
        let tabs = NSTabView(frame: NSRect(x: 0, y: 0, width: 960, height: 640)); tabs.tabViewType = .noTabsNoBorder
        let item = NSTabViewItem(identifier: "add"); item.view = view; tabs.addTabViewItem(item)
        tabs.layoutSubtreeIfNeeded()
        guard view.frame.width > 900, view.frame.height > 600 else { return false }
        tabs.setFrameSize(NSSize(width: 1120, height: 676)); tabs.layoutSubtreeIfNeeded()
        guard view.frame.width > 1080, view.frame.height > 650 else { return false }
        let context = PreparationContext(importID: "11111111-2222-4333-8444-555555555555", revision: 1, sourceRevision: "sha256:" + String(repeating: "a", count: 64))
        view.update(title: "Repository tool", intent: "Find relevant source lines", source: "Selected repository", context: context, canEditRequest: true)
        guard view.titleLabel.stringValue == "Repository tool", !view.requestStack.isHidden, view.welcome.isHidden,
              !view.editButton.isHidden, view.presentedIntent == "Find relevant source lines" else { return false }
        text.string = "Keep responses concise"
        view.refresh()
        guard text.string == "Keep responses concise", view.presentedIntent == "Find relevant source lines" else { return false }
        setup.onSuggestedAnswer?("Selected answer")
        guard text.string == "Selected answer", view.presentedIntent == "Find relevant source lines" else { return false }
        text.string = "Keep responses concise"
        view.revealSourceOptions()
        guard !view.detailsStack.isHidden, view.detailsButton.title == "Hide details" else { return false }
        view.toggleDetails(); guard view.detailsStack.isHidden else { return false }
        view.revealSourceOptions(); guard !view.detailsStack.isHidden else { return false }
        view.setComposerEnabled(false)
        guard !view.sendButton.isEnabled, view.attachButton.isEnabled, view.attachButton.item(at: 5)?.isEnabled == true,
              !view.editButton.isHidden else { return false }
        view.setAgentVisible(false); view.setComposerEnabled(true)
        var sent: String?; view.onSend = { sent = $0 }; view.sendButton.performClick(nil)
        guard sent == "Keep responses concise", setup.isHidden, view.settingsButton.isHidden else { return false }
        view.update(title: "", intent: "", source: nil, context: nil, readOnly: true)
        return !view.sendButton.isEnabled && view.requestStack.isHidden
    }
}
