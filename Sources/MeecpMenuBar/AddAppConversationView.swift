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
    private let composerCard = NSBox()
    private let sourceOptions: NSView
    private let titleLabel = workspaceLabel("New app", size: 19, weight: .semibold)
    private let subtitleLabel = workspaceLabel("An agent to turn your idea into a working tool", size: 12, secondary: true)
    private let statusLabel = workspaceLabel("New app", size: 11, weight: .medium, secondary: true)
    private let thread = WorkspaceDocumentStack()
    private let threadScroll = NSScrollView()
    private let welcome = workspaceStack(spacing: 10)
    private let requestStack = NSStackView()
    private let detailsStack = NSStackView()
    private let bottomSpace = NSView()
    private var contentStack: NSStackView!
    private var compactThreadHeight: NSLayoutConstraint!
    private var compactLayout = true
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
        detailsButton = NSButton(title: "App details", target: self, action: #selector(toggleDetails))
        detailsButton.bezelStyle = .inline
        detailsButton.setAccessibilityLabel("Show app files and setup details")
        let headingSpacer = NSView()
        headingSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let heading = workspaceStack([workspaceStack([titleLabel, subtitleLabel], spacing: 4), headingSpacer, statusLabel, detailsButton!], horizontal: true)
        heading.distribution = .fill

        let welcomeTitle = workspaceLabel("What would you like to build?", size: 24, weight: .semibold)
        let welcomeText = workspaceLabel("Describe your app or paste a link. MeeCP will handle setup and ask you here when it needs a choice.", size: 14, secondary: true)
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
        detailsStack.orientation = .vertical; detailsStack.alignment = .leading; detailsStack.spacing = 10
        let sourceCard = workspaceCard("Source", subtitle: "Optional project link or source files.", views: [sourceSummary, sourceOptions])
        let resourceCard = workspaceCard("Saved files", subtitle: "Optional models, runtimes, or other files.", views: [resources])
        for view in [sourceCard, resourceCard, setup.technicalDetailsView] {
            detailsStack.addArrangedSubview(view); workspaceFill(view, in: detailsStack)
        }
        detailsStack.isHidden = true
        thread.orientation = .vertical; thread.alignment = .leading; thread.spacing = 14
        thread.edgeInsets = NSEdgeInsets(top: 12, left: 0, bottom: 12, right: 0)
        for view in [contextContainer, welcome, requestStack, editButton!, setup, alternateContainer, reviewContainer, detailsStack] {
            thread.addArrangedSubview(view); workspaceFill(view, in: thread)
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
        let composerSpacer = NSView()
        composerSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let composerControls = workspaceStack([attachButton!, composerHint, composerSpacer, sendButton!], horizontal: true, spacing: 10)
        composerCard.boxType = .custom; composerCard.titlePosition = .noTitle
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
        let footerSpacer = NSView()
        footerSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let footer = workspaceStack([footerHint, footerSpacer, settingsButton!], horizontal: true)
        bottomSpace.setContentHuggingPriority(.defaultLow, for: .vertical)
        bottomSpace.heightAnchor.constraint(greaterThanOrEqualToConstant: 0).isActive = true
        let content = workspaceStack([heading, threadScroll, composerCard, footer, bottomSpace], spacing: 12)
        contentStack = content
        // NSTabView manages the root frame. Frame the outer column here so
        // preferred widths in hidden forms cannot resize that root through
        // constraints. Its rows and controls continue to use Auto Layout.
        content.translatesAutoresizingMaskIntoConstraints = true
        addSubview(content)
        for view in [heading, threadScroll, composerCard, footer, bottomSpace] { workspaceFill(view, in: content) }
        compactThreadHeight = threadScroll.heightAnchor.constraint(equalToConstant: 180)
        compactThreadHeight.priority = NSLayoutConstraint.Priority(999)
        compactThreadHeight.isActive = true
        threadScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 60).isActive = true
        threadScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        threadScroll.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        refresh()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layout() {
        let columnWidth = min(900, max(0, bounds.width - 48))
        contentStack.frame = NSRect(x: (bounds.width - columnWidth) / 2, y: 18,
                                   width: columnWidth, height: max(0, bounds.height - 34))
        super.layout()
        if compactLayout {
            // A new request is a compact form. Only an active conversation
            // consumes the remaining height and pins the composer to the bottom.
            let visibleRows = contentStack.arrangedSubviews.filter { !$0.isHidden }
            let fixedHeight = visibleRows.filter { $0 !== threadScroll && $0 !== bottomSpace }
                .reduce(CGFloat(0)) { $0 + $1.frame.height }
            let spacing = CGFloat(max(0, visibleRows.count - 1)) * contentStack.spacing
            let available = max(60, bounds.height - 34 - fixedHeight - spacing)
            // The column has an explicit frame, so measuring its document
            // cannot resize the tab root. Newly disclosed forms need their
            // natural height before their arranged-view frames have settled.
            let desired = min(available, max(60, ceil(thread.fittingSize.height)))
            if abs(compactThreadHeight.constant - desired) > 1 {
                compactThreadHeight.constant = desired
                needsLayout = true
            }
        }
    }

    func update(title: String, intent: String, source: String?, context: PreparationContext?, installationStatus: String? = nil,
                canEditRequest: Bool = false, prepared: Bool = false, readOnly: Bool = false) {
        titleLabel.stringValue = title.isEmpty ? "New app" : title
        hasIntent = context != nil && !intent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        titleLabel.isHidden = !hasIntent && !prepared
        subtitleLabel.stringValue = hasIntent ? "Your app requirements" : ""
        subtitleLabel.isHidden = !hasIntent
        self.installationStatus = installationStatus; writable = !readOnly
        sourceSummary.stringValue = source.map { "Selected source: " + $0 } ?? "No source selected. You can start with a description."
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
        let compact = setup.job == nil && alternateContainer.isHidden && !prepared
        if compactLayout != compact {
            compactLayout = compact
            compactThreadHeight.isActive = compact
            bottomSpace.isHidden = !compact
        }
        needsLayout = true
        composerCard.isHidden = agentVisible && setup.authorizationPresentationBlocked
        settingsButton.isHidden = !agentVisible || setup.authorizationPresentationBlocked
        welcome.isHidden = hasIntent || setup.job != nil
        requestStack.isHidden = !hasIntent
        editButton.isHidden = !hasIntent || !canEditRequest || !writable || setup.blocksNavigation || prepared
        if let installationStatus { statusLabel.stringValue = installationStatus }
        else if let job = setup.job {
            statusLabel.stringValue = job.status == "running" ? job.question != nil ? "Needs your input" : job.stage == "waiting_for_system" ? "Waiting for Apple" : "Working"
                : ["ready": "Ready to install", "interrupted": "Interrupted", "canceled": "Stopped", "failed": "Needs attention"][job.status] ?? "Draft"
        } else { statusLabel.stringValue = setup.authorizationPresentationBlocked ? "Agent settings" : hasIntent ? "Draft" : "New app" }
        sendButton.title = agentVisible ? setup.composerActionTitle : "Save request"
        let text = composer.documentView as? NSTextView
        let hasMessage = text?.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
        let needsMessage = agentVisible && (setup.job?.active == true || !hasIntent)
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
        settingsButton.isEnabled = agentVisible && !setup.blocksNavigation && !setup.authorizationPresentationBlocked
        let sourceAllowed = writable && !resources.blocksNavigation && !setup.blocksNavigation
        for index in [1, 2, 4, 5] { attachButton.item(at: index)?.isEnabled = sourceAllowed }
        attachButton.item(at: 3)?.isEnabled = resources.canChooseResources
        attachButton.isEnabled = sourceAllowed || resources.canChooseResources
        detailsButton.title = detailsVisible ? "Hide app details" : "App details"
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
        setup.available = { true }
        setup.request = { path, _, _, done in
            if path == "v1/pi/settings" {
                done(["configured": false, "provider": "openai", "model": "", "base_url": NSNull(),
                      "runtime_available": true, "available_providers": ["openai"]], nil)
            }
        }
        setup.poll(); view.refresh()
        guard !view.sendButton.isEnabled else { print("add_app_check_failed:empty_request"); return false }
        text.string = "Initial public app requirement"; view.refresh()
        guard view.sendButton.isEnabled else { print("add_app_check_failed:initial_request"); return false }
        let tabs = NSTabView(frame: NSRect(x: 0, y: 0, width: 960, height: 640)); tabs.tabViewType = .noTabsNoBorder
        let item = NSTabViewItem(identifier: "add"); item.view = view; tabs.addTabViewItem(item)
        tabs.layoutSubtreeIfNeeded()
        guard view.frame.width > 900, view.frame.height > 600 else { print("add_app_check_failed:tab_frame"); return false }
        tabs.layoutSubtreeIfNeeded()
        guard view.compactThreadHeight.isActive, !view.bottomSpace.isHidden,
              view.threadScroll.frame.height < 240, view.threadScroll.frame.width <= 900 else {
            print("add_app_check_failed:compact_layout")
            return false
        }
        tabs.setFrameSize(NSSize(width: 1120, height: 676)); tabs.layoutSubtreeIfNeeded()
        guard view.frame.width > 1080, view.frame.height > 650 else { print("add_app_check_failed:resized_tab"); return false }
        guard view.threadScroll.frame.width <= 900 else { print("compact_width_check_failed"); return false }
        setup.showInlineSettings(); view.refresh()
        tabs.layoutSubtreeIfNeeded(); tabs.layoutSubtreeIfNeeded()
        guard view.composerCard.isHidden, view.settingsButton.isHidden,
              view.threadScroll.frame.height > 250,
              text.string == "Initial public app requirement" else { print("add_app_check_failed:settings_composer"); return false }
        setup.dismissInlineSettings(); view.refresh()
        guard !view.composerCard.isHidden, !view.settingsButton.isHidden,
              text.string == "Initial public app requirement" else { print("add_app_check_failed:settings_restore"); return false }
        let context = PreparationContext(importID: "11111111-2222-4333-8444-555555555555", revision: 1, sourceRevision: "sha256:" + String(repeating: "a", count: 64))
        view.update(title: "Repository tool", intent: "Find relevant source lines", source: "Selected repository", context: context, canEditRequest: true)
        guard view.titleLabel.stringValue == "Repository tool", !view.requestStack.isHidden, view.welcome.isHidden,
              !view.editButton.isHidden, view.presentedIntent == "Find relevant source lines" else { print("add_app_check_failed:saved_request"); return false }
        text.string = "Keep responses concise"
        view.refresh()
        guard text.string == "Keep responses concise", view.presentedIntent == "Find relevant source lines" else { print("add_app_check_failed:reply_storage"); return false }
        setup.onSuggestedAnswer?("Selected answer")
        guard text.string == "Selected answer", view.presentedIntent == "Find relevant source lines" else { print("add_app_check_failed:suggested_answer"); return false }
        text.string = "Keep responses concise"
        view.revealSourceOptions()
        guard !view.detailsStack.isHidden, view.detailsButton.title == "Hide app details" else { print("add_app_check_failed:details_open"); return false }
        view.toggleDetails(); guard view.detailsStack.isHidden else { print("add_app_check_failed:details_close"); return false }
        view.revealSourceOptions(); guard !view.detailsStack.isHidden else { print("add_app_check_failed:details_reopen"); return false }
        view.setPrepared(true); tabs.layoutSubtreeIfNeeded()
        guard !view.compactThreadHeight.isActive, view.bottomSpace.isHidden else { print("expanded_layout_check_failed"); return false }
        view.setPrepared(false)
        view.setComposerEnabled(false)
        guard !view.sendButton.isEnabled, view.attachButton.isEnabled, view.attachButton.item(at: 5)?.isEnabled == true,
              !view.editButton.isHidden else { print("add_app_check_failed:edit_availability"); return false }
        view.setAgentVisible(false); view.setComposerEnabled(true)
        var sent: String?; view.onSend = { sent = $0 }; view.sendButton.performClick(nil)
        guard sent == "Keep responses concise", setup.isHidden, view.settingsButton.isHidden else { print("add_app_check_failed:alternate_method"); return false }
        view.update(title: "", intent: "", source: nil, context: nil, readOnly: true)
        return !view.sendButton.isEnabled && view.requestStack.isHidden
    }
}
