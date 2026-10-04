import AppKit
import Darwin
import Foundation

// The document grows inside its clip view; it must not supply a preferred
// height to the surrounding window or stack view.
private final class BoundedScrollView: NSScrollView {
    override var intrinsicContentSize: NSSize {
        NSSize(width: NSView.noIntrinsicMetric, height: NSView.noIntrinsicMetric)
    }
}

private final class DraftTextView: NSTextView {
    private let edits = UndoManager()
    override var undoManager: UndoManager? { edits }
}

private final class ApplicationDelegate: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private struct ChatSession {
        let conversation: Conversation
        let transcript: NSAttributedString
        let draft: String
        let modelPath: String
        let context: String
        let maximumOutput: String
        let thinkingEnabled: Bool
        let thinkingBudget: String
        let samplingPreset: SamplingPreset
        let temperature: Double
    }

    private let runner = ModelRunner()
    private var window: NSWindow!
    private var promptView: NSTextView!
    private var responseView: NSTextView!
    private let status = NSTextField(labelWithString: "Ready. Follow-ups include this conversation.")
    private let systemEditor = SystemPromptEditor(text: "Be helpful, concise, and accurate.")
    private let modelField = NSTextField(string: ModelProfile.bonsai.defaultModelPath)
    private let modelSelector = NSPopUpButton(frame: .zero, pullsDown: false)
    private let runtimeField = NSTextField(string: defaultRuntimePath)
    private let seedField = NSTextField(string: "42")
    private let temperature = NSSlider(value: 0.2, minValue: 0, maxValue: 1, target: nil, action: nil)
    private let temperatureValue = NSTextField(labelWithString: "0.20")
    private let context = NSPopUpButton(frame: .zero, pullsDown: false)
    private let maximumOutput = NSPopUpButton(frame: .zero, pullsDown: false)
    private let thinking = NSButton(checkboxWithTitle: "Think before answering", target: nil, action: nil)
    private let thinkingBudget = NSPopUpButton(frame: .zero, pullsDown: false)
    private let thinkingHint = NSTextField(labelWithString: "")
    private let samplingPreset = NSPopUpButton(frame: .zero, pullsDown: false)
    private let samplingHint = NSTextField(labelWithString: "")
    private let send = NSButton(title: "Send", target: nil, action: nil)
    private let stop = NSButton(title: "Stop", target: nil, action: nil)
    private let clear = NSButton(title: "New Chat", target: nil, action: nil)
    private let settingsToggle = NSButton(checkboxWithTitle: "Show settings", target: nil, action: nil)
    private var settingsPanel: NSStackView!
    private var settingsControls: [NSControl] = []
    private var generating = false
    private var quitting = false
    private var timer: Timer?
    private var started = Date()
    private var conversation = Conversation()
    private var pendingPrompt = ""
    private var pendingResponse = ""
    private var selectedProfile = ModelProfile.bonsai
    private var sessions: [ModelProfile: ChatSession] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenus()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 920, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Bonsai Playground — Model Comparison"
        window.isReleasedWhenClosed = false
        window.isRestorable = false
        window.contentMinSize = NSSize(width: 760, height: 620)
        let content = window.contentView!
        let explanation = NSTextField(labelWithString: "Try compact models locally. Each model keeps its own chat in memory.")
        explanation.textColor = .secondaryLabelColor
        modelSelector.addItems(withTitles: ModelProfile.selectableProfiles.map { $0.displayName })
        modelSelector.target = self
        modelSelector.action = #selector(selectModel)
        modelSelector.toolTip = "Bonsai, MiniCPM5 Fable, Qwen3.5 2B, Qwen3 0.6B BF16, Falcon3 1B, and Falcon-H1 Tiny 90M"
        let modelSelection = row([label("Model"), modelSelector])
        let conversationLabel = label("Conversation")
        let responseScroll = textArea(editable: false)
        responseView = responseScroll.documentView as? NSTextView
        let composer = NSStackView()
        composer.orientation = .vertical
        composer.alignment = .leading
        composer.spacing = 8
        composer.detachesHiddenViews = true
        settingsToggle.target = self; settingsToggle.action = #selector(toggleSettings)
        composer.addArrangedSubview(settingsToggle)
        settingsPanel = makeSettings()
        composer.addArrangedSubview(settingsPanel)
        settingsPanel.widthAnchor.constraint(equalTo: composer.widthAnchor).isActive = true
        settingsPanel.isHidden = true
        composer.addArrangedSubview(label("Message"))
        let promptScroll = textArea(editable: true)
        promptView = promptScroll.documentView as? NSTextView
        composer.addArrangedSubview(promptScroll)
        promptScroll.heightAnchor.constraint(equalToConstant: 120).isActive = true
        promptScroll.widthAnchor.constraint(equalTo: composer.widthAnchor).isActive = true
        send.target = self; send.action = #selector(sendPrompt)
        send.bezelStyle = .rounded; send.keyEquivalent = "\r"; send.keyEquivalentModifierMask = [.command]
        stop.target = self; stop.action = #selector(stopGeneration)
        stop.bezelStyle = .rounded; stop.keyEquivalent = "\u{1b}"; stop.keyEquivalentModifierMask = []
        stop.isEnabled = false
        clear.target = self; clear.action = #selector(clearPrompt)
        clear.bezelStyle = .rounded; clear.keyEquivalent = "n"; clear.keyEquivalentModifierMask = [.command]
        let buttons = row([send, stop, clear])
        composer.addArrangedSubview(buttons)
        status.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 12)
        composer.addArrangedSubview(status)
        for view in [explanation, modelSelection, conversationLabel, responseScroll, composer] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
            view.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18).isActive = true
            view.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18).isActive = true
        }
        NSLayoutConstraint.activate([
            explanation.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            modelSelection.topAnchor.constraint(equalTo: explanation.bottomAnchor, constant: 10),
            conversationLabel.topAnchor.constraint(equalTo: modelSelection.bottomAnchor, constant: 10),
            responseScroll.topAnchor.constraint(equalTo: conversationLabel.bottomAnchor, constant: 6),
            responseScroll.bottomAnchor.constraint(equalTo: composer.topAnchor, constant: -14),
            responseScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 100),
            composer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18)
        ])
        window.center()
        window.makeFirstResponder(promptView)
        if !CommandLine.arguments.contains("--ui-self-test") {
            window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
        }
    }

    private func label(_ title: String) -> NSTextField {
        let field = NSTextField(labelWithString: title)
        field.font = .systemFont(ofSize: 13, weight: .semibold)
        return field
    }

    private func row(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 10
        return stack
    }

    private func textArea(editable: Bool) -> NSScrollView {
        let scroll = BoundedScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.borderType = .bezelBorder
        let frame = NSRect(x: 0, y: 0, width: 800, height: 120)
        let text: NSTextView = editable ? DraftTextView(frame: frame) : NSTextView(frame: frame)
        text.isEditable = editable
        text.isSelectable = true
        text.isRichText = false
        text.allowsUndo = editable
        text.isAutomaticQuoteSubstitutionEnabled = false
        text.isAutomaticDashSubstitutionEnabled = false
        text.isAutomaticTextReplacementEnabled = false
        text.isAutomaticSpellingCorrectionEnabled = false
        text.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        text.textContainerInset = NSSize(width: 10, height: 10)
        text.minSize = .zero
        text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = false
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        text.textContainer?.containerSize = NSSize(width: 800, height: CGFloat.greatestFiniteMagnitude)
        scroll.documentView = text
        return scroll
    }

    private func makeSettings() -> NSStackView {
        let panel = NSStackView()
        panel.orientation = .vertical
        panel.alignment = .leading
        panel.spacing = 8
        let systemRow = row([label("System"), systemEditor])
        panel.addArrangedSubview(systemRow)
        systemRow.widthAnchor.constraint(equalTo: panel.widthAnchor).isActive = true
        temperature.target = self; temperature.action = #selector(updateTemperature)
        temperature.widthAnchor.constraint(equalToConstant: 140).isActive = true
        temperatureValue.widthAnchor.constraint(equalToConstant: 35).isActive = true
        configureTokenControls(for: selectedProfile)
        context.target = self; context.action = #selector(contextBudgetChanged)
        maximumOutput.target = self; maximumOutput.action = #selector(responseBudgetChanged)
        context.toolTip = "Total tokens available for the conversation, thinking, and final answer."
        maximumOutput.toolTip = "Maximum generated tokens, including thinking and the final answer."
        runtimeField.toolTip = "Choose llama-completion. The other models also require the sibling llama-server executable in this directory."
        thinking.target = self; thinking.action = #selector(thinkingChanged)
        thinkingBudget.target = self; thinkingBudget.action = #selector(thinkingBudgetChanged)
        thinkingBudget.toolTip = "Maximum thinking tokens. The runtime then continues the final answer within Max response. A lower response limit also reduces this limit to leave room for the answer."
        samplingPreset.target = self; samplingPreset.action = #selector(samplingPresetChanged)
        samplingPreset.toolTip = "Model-specific sampling. Coding uses Qwen's recommended settings for precise coding and enables thinking."
        for hint in [thinkingHint, samplingHint] {
            hint.font = .systemFont(ofSize: 11)
            hint.textColor = .secondaryLabelColor
        }
        seedField.widthAnchor.constraint(equalToConstant: 65).isActive = true
        panel.addArrangedSubview(row([label("Temperature"), temperature, temperatureValue,
                                     label("Context"), context, label("Max response"), maximumOutput,
                                     label("Seed"), seedField]))
        panel.addArrangedSubview(row([thinking, label("Thinking limit"), thinkingBudget, thinkingHint]))
        panel.addArrangedSubview(row([label("Sampling"), samplingPreset, samplingHint]))
        configureReasoningControls(for: selectedProfile)
        let modelBrowse = NSButton(title: "Choose…", target: self, action: #selector(chooseModel))
        let runtimeBrowse = NSButton(title: "Choose…", target: self, action: #selector(chooseRuntime))
        let modelRow = row([label("Model file"), modelField, modelBrowse])
        let runtimeRow = row([label("Runtime"), runtimeField, runtimeBrowse])
        panel.addArrangedSubview(modelRow)
        panel.addArrangedSubview(runtimeRow)
        modelRow.widthAnchor.constraint(equalTo: panel.widthAnchor).isActive = true
        runtimeRow.widthAnchor.constraint(equalTo: panel.widthAnchor).isActive = true
        systemEditor.setContentHuggingPriority(.defaultLow, for: .horizontal)
        modelField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        runtimeField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        for field in [modelField, runtimeField] {
            field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            field.lineBreakMode = .byTruncatingMiddle
        }
        settingsControls = [modelField, runtimeField, seedField, temperature,
                            context, maximumOutput, thinking, thinkingBudget, samplingPreset, modelBrowse, runtimeBrowse]
        return panel
    }

    @objc private func updateTemperature() {
        temperatureValue.stringValue = String(format: "%.2f", temperature.doubleValue)
    }

    private var selectedSamplingPreset: SamplingPreset {
        let index = samplingPreset.indexOfSelectedItem
        return selectedProfile.samplingPresets.indices.contains(index) ? selectedProfile.samplingPresets[index] : .recommended
    }

    private func configureReasoningControls(for profile: ModelProfile, session: ChatSession? = nil) {
        thinking.state = (session?.thinkingEnabled ?? profile.defaultThinkingEnabled) ? .on : .off
        thinkingBudget.removeAllItems()
        thinkingBudget.addItems(withTitles: profile.thinkingBudgets.map(String.init))
        if !profile.supportsThinking { thinkingBudget.addItem(withTitle: "—") }
        thinkingBudget.selectItem(withTitle: session?.thinkingBudget ?? String(profile.defaultThinkingBudget))
        samplingPreset.removeAllItems()
        samplingPreset.addItems(withTitles: profile.samplingPresets.map { $0.displayName })
        samplingPreset.selectItem(at: profile.samplingPresets.firstIndex(of: session?.samplingPreset ?? .recommended) ?? 0)
        temperature.doubleValue = session?.temperature ?? profile.sampling(preset: selectedSamplingPreset, thinking: thinking.state == .on).temperature
        updateTemperature()
        refreshReasoningControls()
    }

    private func refreshReasoningControls() {
        thinking.isEnabled = !generating && selectedProfile.supportsThinkingToggle
        thinkingBudget.isEnabled = !generating && selectedProfile.supportsThinking && thinking.state == .on
        if !selectedProfile.supportsThinking { thinkingHint.stringValue = "This model answers directly." }
        else if thinking.state == .off { thinkingHint.stringValue = "Direct answers. Thinking is off." }
        else {
            let output = Int(maximumOutput.titleOfSelectedItem ?? "512") ?? 512
            let requested = Int(thinkingBudget.titleOfSelectedItem ?? "0") ?? 0
            let effective = min(requested, max(0, output - min(512, output / 2)))
            let suffix = selectedProfile.supportsThinkingToggle ? "" : " · thinking-only model"
            thinkingHint.stringValue = "Up to \(effective) thinking tokens\(suffix)"
        }
        let sample = selectedProfile.sampling(preset: selectedSamplingPreset, thinking: thinking.state == .on)
        samplingHint.stringValue = String(format: "Top k %d · Top p %.2f · Min p %.2f · Presence %.1f · Repeat %.2f", sample.topK, sample.topP, sample.minP, sample.presencePenalty, sample.repetitionPenalty)
    }

    @objc private func thinkingChanged() {
        guard !generating, selectedProfile.supportsThinkingToggle else { return }
        if thinking.state == .off { samplingPreset.selectItem(at: 0) }
        temperature.doubleValue = selectedProfile.sampling(preset: selectedSamplingPreset, thinking: thinking.state == .on).temperature
        updateTemperature()
        refreshReasoningControls()
    }

    @objc private func thinkingBudgetChanged() { refreshReasoningControls() }

    @objc private func samplingPresetChanged() {
        guard !generating else { return }
        if selectedSamplingPreset == .coding { thinking.state = .on }
        temperature.doubleValue = selectedProfile.sampling(preset: selectedSamplingPreset, thinking: thinking.state == .on).temperature
        updateTemperature()
        refreshReasoningControls()
    }

    private func configureTokenControls(for profile: ModelProfile, savedContext: String? = nil,
                                        savedOutput: String? = nil) {
        context.removeAllItems()
        context.addItems(withTitles: profile.contextSizes.map(String.init))
        context.selectItem(withTitle: savedContext ?? String(profile.defaultContext))
        maximumOutput.removeAllItems()
        maximumOutput.addItems(withTitles: profile.maximumOutputs.map(String.init))
        maximumOutput.selectItem(withTitle: savedOutput ?? String(profile.defaultMaximumOutput))
    }

    @objc private func responseBudgetChanged() {
        defer { refreshReasoningControls() }
        guard !generating, selectedProfile.isQwen,
              let output = Int(maximumOutput.titleOfSelectedItem ?? ""),
              let current = Int(context.titleOfSelectedItem ?? "") else { return }
        // Leave room for conversation when the requested response grows.
        let required = output + (selectedProfile.isQwen35 ? 8192 : 2048)
        if current < required, let larger = selectedProfile.contextSizes.first(where: { $0 >= required }) {
            context.selectItem(withTitle: String(larger))
        }
    }

    @objc private func contextBudgetChanged() {
        defer { refreshReasoningControls() }
        guard !generating, selectedProfile.isQwen,
              let current = Int(context.titleOfSelectedItem ?? ""),
              let output = Int(maximumOutput.titleOfSelectedItem ?? "") else { return }
        if output > current - 512,
           let smaller = selectedProfile.maximumOutputs.last(where: { $0 <= current - 512 }) {
            maximumOutput.selectItem(withTitle: String(smaller))
        }
    }

    @objc private func toggleSettings() {
        let visible = settingsToggle.state == .on
        settingsPanel.isHidden = !visible
        window.contentMinSize = NSSize(width: 760, height: visible ? 840 : 620)
        if visible && window.contentView!.bounds.height < 840 {
            var frame = window.frame
            let addition = 840 - window.contentView!.bounds.height
            frame.origin.y -= addition
            frame.size.height += addition
            window.setFrame(frame, display: true, animate: true)
        }
    }

    @objc private func chooseModel() { chooseFile(for: modelField) }
    @objc private func chooseRuntime() { chooseFile(for: runtimeField) }

    @objc private func selectModel() {
        guard !generating, ModelProfile.selectableProfiles.indices.contains(modelSelector.indexOfSelectedItem) else { return }
        let profile = ModelProfile.selectableProfiles[modelSelector.indexOfSelectedItem]
        guard profile != selectedProfile else { return }
        sessions[selectedProfile] = ChatSession(conversation: conversation,
            transcript: NSAttributedString(attributedString: responseView.attributedString()),
            draft: promptView.string, modelPath: modelField.stringValue,
            context: context.titleOfSelectedItem ?? String(selectedProfile.defaultContext),
            maximumOutput: maximumOutput.titleOfSelectedItem ?? "512",
            thinkingEnabled: thinking.state == .on, thinkingBudget: thinkingBudget.titleOfSelectedItem ?? "0",
            samplingPreset: selectedSamplingPreset, temperature: temperature.doubleValue)
        selectedProfile = profile
        let session = sessions[profile]
        conversation = session?.conversation ?? Conversation()
        responseView.textStorage?.setAttributedString(session?.transcript ?? NSAttributedString(string: ""))
        replaceDraft(session?.draft ?? "")
        promptView.undoManager?.removeAllActions()
        modelField.stringValue = session?.modelPath ?? profile.defaultModelPath
        configureTokenControls(for: profile, savedContext: session?.context, savedOutput: session?.maximumOutput)
        configureReasoningControls(for: profile, session: session)
        status.stringValue = "Ready. " + profile.speakerName + " chat selected."
        window.makeFirstResponder(promptView)
    }

    private func chooseFile(for field: NSTextField) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.beginSheetModal(for: window) { response in
            if response == .OK, let url = panel.url { field.stringValue = url.path }
        }
    }

    @objc private func sendPrompt() {
        guard !generating else { return }
        let prompt = promptView.string
        guard !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            status.stringValue = "Enter a prompt first."
            window.makeFirstResponder(promptView)
            return
        }
        guard let seed = Int(seedField.stringValue.trimmingCharacters(in: .whitespaces)),
              seed >= 0, seed <= Int(UInt32.max) else {
            status.stringValue = "Seed must be an integer between 0 and 4294967295."
            return
        }
        var settings = GenerationSettings()
        settings.profile = selectedProfile
        settings.model = modelField.stringValue
        settings.runtime = runtimeField.stringValue
        settings.system = systemEditor.string
        settings.temperature = temperature.doubleValue
        settings.context = Int(context.titleOfSelectedItem ?? "2048") ?? 2048
        settings.maximumOutput = Int(maximumOutput.titleOfSelectedItem ?? "512") ?? 512
        settings.thinkingEnabled = selectedProfile.supportsThinking && thinking.state == .on
        settings.thinkingBudget = Int(thinkingBudget.titleOfSelectedItem ?? "0") ?? 0
        settings.samplingPreset = selectedSamplingPreset
        settings.seed = seed
        pendingPrompt = prompt
        pendingResponse = ""
        appendTranscript("You", body: prompt)
        appendTranscript(selectedProfile.speakerName, body: "")
        replaceDraft("")
        setGenerating(true)
        started = Date()
        status.stringValue = "Starting…"
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.status.stringValue = String(format: "Generating · %.1f s", Date().timeIntervalSince(self.started))
        }
        runner.start(prompt: prompt, settings: settings, conversation: conversation, onText: { [weak self] text in
            guard let self = self else { return }
            let scroll = self.responseView.enclosingScrollView!
            let atBottom = scroll.contentView.bounds.maxY >= self.responseView.bounds.maxY - 24
            self.pendingResponse += text
            self.responseView.textStorage?.append(NSAttributedString(string: text, attributes: self.bodyAttributes))
            if atBottom {
                self.responseView.scrollRangeToVisible(NSRange(location: self.responseView.string.utf16.count, length: 0))
            }
        }, onComplete: { [weak self] result in
            self?.completeGeneration(result)
        })
    }

    private func completeGeneration(_ result: GenerationResult) {
        timer?.invalidate(); timer = nil
        setGenerating(false)
        status.stringValue = result.message
        let hasAnswer = selectedProfile == .bonsai
            || !selectedProfile.finalAnswer(pendingResponse).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if (result.failure == nil || result.stopped) && hasAnswer && !pendingResponse.isEmpty {
            conversation.append(user: pendingPrompt, assistant: pendingResponse)
        } else {
            replaceDraft(pendingPrompt)
        }
        if result.failure != nil && (!result.stopped || result.failure == "incomplete_result") {
            responseView.textStorage?.append(NSAttributedString(string: "\n\n" + result.message))
        }
        responseView.textStorage?.append(NSAttributedString(string: "\n\n", attributes: bodyAttributes))
        pendingPrompt = ""
        pendingResponse = ""
        window.makeFirstResponder(promptView)
        if quitting { NSApplication.shared.reply(toApplicationShouldTerminate: true) }
    }

    private func setGenerating(_ value: Bool) {
        generating = value
        send.isEnabled = !value
        clear.isEnabled = !value
        stop.isEnabled = value
        modelSelector.isEnabled = !value
        promptView.isEditable = !value
        systemEditor.isEditable = !value
        for control in settingsControls { control.isEnabled = !value }
        refreshReasoningControls()
    }

    @objc private func stopGeneration() {
        if generating { status.stringValue = "Stopping…"; runner.cancel() }
    }

    @objc private func clearPrompt() {
        guard !generating else { return }
        replaceDraft("")
        promptView.undoManager?.removeAllActions()
        conversation.reset()
        responseView.string = ""
        status.stringValue = "New chat. Conversation reset."
        window.makeFirstResponder(promptView)
    }

    private var bodyAttributes: [NSAttributedString.Key: Any] {
        [.font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular), .foregroundColor: NSColor.textColor]
    }

    private func appendTranscript(_ speaker: String, body: String) {
        responseView.textStorage?.append(NSAttributedString(string: speaker + "\n", attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.labelColor
        ]))
        if !body.isEmpty {
            responseView.textStorage?.append(NSAttributedString(string: body + "\n\n", attributes: bodyAttributes))
        }
        responseView.scrollRangeToVisible(NSRange(location: responseView.string.utf16.count, length: 0))
    }

    private func replaceDraft(_ text: String) {
        promptView.insertText(text, replacementRange: NSRange(location: 0, length: promptView.string.utf16.count))
    }

    private var activeUndoManager: UndoManager? {
        if let editor = window.firstResponder as? NSTextView, editor.isEditable { return editor.undoManager }
        return promptView.undoManager
    }

    @objc private func undoEdit(_ sender: Any?) { activeUndoManager?.undo() }
    @objc private func redoEdit(_ sender: Any?) { activeUndoManager?.redo() }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(undoEdit(_:)) { return !generating && activeUndoManager?.canUndo == true }
        if menuItem.action == #selector(redoEdit(_:)) { return !generating && activeUndoManager?.canRedo == true }
        return true
    }

    // Exercise the actual controls in an undisplayed window. No system input,
    // clipboard changes, model calls, or conversation text leave this process.
    func uiChecks() -> [String: Bool] {
        let frame = window.frame
        let scroll = responseView.enclosingScrollView!
        window.contentView?.layoutSubtreeIfNeeded()
        let scrollHeight = scroll.bounds.height
        var checks: [String: Bool] = [:]
        promptView.insertText("synthetic draft", replacementRange: NSRange(location: 0, length: 0))
        checks["draft_registers_undo"] = promptView.undoManager?.canUndo == true
        undoEdit(nil)
        checks["undo_restores_draft"] = promptView.string.isEmpty
        redoEdit(nil)
        checks["redo_restores_draft"] = promptView.string == "synthetic draft"
        let editMenu = NSApplication.shared.mainMenu?.item(at: 1)?.submenu
        checks["undo_shortcut"] = editMenu?.item(at: 0)?.keyEquivalent == "z"
            && editMenu?.item(at: 0)?.keyEquivalentModifierMask == [.command]
        checks["redo_shortcut"] = editMenu?.item(at: 1)?.keyEquivalent == "z"
            && editMenu?.item(at: 1)?.keyEquivalentModifierMask == [.command, .shift]
        let longText = Array(repeating: "Synthetic long line for checking scrolling.", count: 500).joined(separator: "\n")
        replaceDraft(longText)
        responseView.textStorage?.append(NSAttributedString(string: longText, attributes: bodyAttributes))
        promptView.layoutManager?.ensureLayout(for: promptView.textContainer!)
        responseView.layoutManager?.ensureLayout(for: responseView.textContainer!)
        promptView.sizeToFit()
        responseView.sizeToFit()
        window.contentView?.layoutSubtreeIfNeeded()
        checks["long_text_keeps_window_size"] = window.frame.size == frame.size
        checks["long_text_keeps_transcript_height"] = abs(scroll.bounds.height - scrollHeight) < 1
        checks["composer_height_bounded"] = abs(promptView.enclosingScrollView!.bounds.height - 120) < 1
        checks["composer_document_scrolls"] = promptView.bounds.height > promptView.enclosingScrollView!.contentView.bounds.height
        checks["transcript_document_scrolls"] = responseView.bounds.height > scroll.contentView.bounds.height
        conversation.append(user: "synthetic user", assistant: "synthetic answer")
        clearPrompt()
        checks["new_chat_resets_history"] = conversation.isEmpty && responseView.string.isEmpty && promptView.string.isEmpty
        checks["new_chat_resets_undo"] = promptView.undoManager?.canUndo == false && promptView.undoManager?.canRedo == false

        settingsPanel.isHidden = false
        func systemHeight(_ text: String) -> CGFloat {
            systemEditor.string = text
            window.contentView?.layoutSubtreeIfNeeded()
            systemEditor.refreshHeight()
            window.contentView?.layoutSubtreeIfNeeded()
            return systemEditor.bounds.height
        }
        let oneLine = systemHeight("synthetic one")
        let twoLines = systemHeight("synthetic one\nsynthetic two")
        let threeLines = systemHeight("synthetic one\nsynthetic two\nsynthetic three")
        let fourLines = systemHeight("synthetic one\nsynthetic two\nsynthetic three\nsynthetic four")
        checks["system_grows_to_three_lines"] = oneLine < twoLines && twoLines < threeLines
        checks["system_height_capped_at_three_lines"] = abs(fourLines - threeLines) < 1
        checks["system_document_scrolls"] = systemEditor.textView.bounds.height > systemEditor.contentView.bounds.height
        let wrappedHeight = systemHeight(Array(repeating: "synthetic wrapping text", count: 100).joined(separator: " "))
        checks["system_wrapping_keeps_height_bounded"] = abs(wrappedHeight - threeLines) < 1
        checks["system_empty_shrinks_to_one_line"] = abs(systemHeight("") - oneLine) < 1
        let systemUndo = systemEditor.textView.undoManager!
        systemUndo.removeAllActions()
        // These synchronous checks have no input event boundaries. Give each
        // synthetic edit its own group, as separate keyboard events would.
        systemUndo.groupsByEvent = false
        window.makeFirstResponder(systemEditor.textView)
        systemUndo.beginUndoGrouping()
        systemEditor.textView.insertText("synthetic edit", replacementRange: NSRange(location: 0, length: 0))
        systemUndo.endUndoGrouping()
        systemEditor.textView.breakUndoCoalescing()
        systemUndo.beginUndoGrouping()
        systemEditor.textView.insertNewline(nil)
        systemUndo.endUndoGrouping()
        checks["system_enter_inserts_newline"] = systemEditor.string == "synthetic edit\n"
        undoEdit(nil)
        checks["system_undo_uses_active_editor"] = systemEditor.string == "synthetic edit" && promptView.string.isEmpty
        redoEdit(nil)
        checks["system_redo_uses_active_editor"] = systemEditor.string == "synthetic edit\n" && promptView.string.isEmpty
        systemUndo.groupsByEvent = true
        setGenerating(true)
        checks["generation_disables_system_editor"] = !systemEditor.isEditable
        setGenerating(false)
        checks["generation_restores_system_editor"] = systemEditor.isEditable
        checks["system_edit_keeps_window_size"] = window.frame.size == frame.size
        conversation.append(user: "synthetic bonsai user", assistant: "synthetic bonsai answer")
        responseView.string = "synthetic bonsai transcript"
        replaceDraft("synthetic bonsai draft")
        modelSelector.selectItem(at: 1)
        selectModel()
        checks["selector_switches_model_path_and_budget"] = selectedProfile == .minicpmQ4
            && modelField.stringValue == ModelProfile.minicpmQ4.defaultModelPath
            && maximumOutput.titleOfSelectedItem == "2048"
        checks["minicpm_has_recommended_sampling_and_fixed_thinking"] = temperature.doubleValue == 1
            && thinking.state == .on && !thinking.isEnabled && thinkingBudget.isEnabled
            && thinkingBudget.titleOfSelectedItem == "1024"
        checks["models_start_with_separate_chats"] = conversation.isEmpty
            && responseView.string.isEmpty && promptView.string.isEmpty
        conversation.append(user: "synthetic minicpm user", assistant: "synthetic minicpm answer")
        responseView.string = "synthetic minicpm transcript"
        replaceDraft("synthetic minicpm draft")
        modelSelector.selectItem(at: 0)
        selectModel()
        checks["switch_restores_bonsai_chat"] = selectedProfile == .bonsai
            && conversation.turns.first?.assistant == "synthetic bonsai answer"
            && responseView.string == "synthetic bonsai transcript" && promptView.string == "synthetic bonsai draft"
        clearPrompt()
        modelSelector.selectItem(at: 1)
        selectModel()
        checks["new_chat_preserves_other_model_chat"] = conversation.turns.first?.assistant == "synthetic minicpm answer"
            && responseView.string == "synthetic minicpm transcript" && promptView.string == "synthetic minicpm draft"
        modelSelector.selectItem(at: 0)
        selectModel()
        checks["new_chat_stays_reset_after_model_switch"] = conversation.isEmpty
            && responseView.string.isEmpty && promptView.string.isEmpty
        setGenerating(true)
        checks["generation_disables_model_switch"] = !modelSelector.isEnabled
        setGenerating(false)
        checks["generation_restores_model_switch"] = modelSelector.isEnabled
        modelSelector.selectItem(at: 2)
        selectModel()
        checks["qwen_selector_uses_reasoning_model"] = selectedProfile == .qwen35
            && modelField.stringValue == ModelProfile.qwen35.defaultModelPath && conversation.isEmpty
            && context.titleOfSelectedItem == "65536" && maximumOutput.titleOfSelectedItem == "32768"
        checks["qwen_defaults_to_direct_answers_and_recommended_sampling"] = thinking.state == .off
            && thinking.isEnabled && !thinkingBudget.isEnabled && temperature.doubleValue == 1
            && samplingHint.stringValue.contains("Presence 2.0")
        thinking.state = .on
        thinkingChanged()
        checks["qwen_thinking_toggle_updates_sampler_and_budget_controls"] = thinking.state == .on
            && thinkingBudget.isEnabled && thinkingBudget.titleOfSelectedItem == "2048"
            && samplingHint.stringValue.contains("Presence 1.5")
        samplingPreset.selectItem(at: 1)
        samplingPresetChanged()
        checks["qwen_coding_preset_uses_precise_thinking_settings"] = selectedSamplingPreset == .coding
            && thinking.state == .on && temperature.doubleValue == 0.6
            && samplingHint.stringValue.contains("Presence 0.0")
        thinkingBudget.selectItem(withTitle: "1024")
        thinkingBudgetChanged()
        temperature.doubleValue = 0.75
        updateTemperature()
        setGenerating(true)
        checks["generation_locks_new_reasoning_controls"] = !thinking.isEnabled && !thinkingBudget.isEnabled && !samplingPreset.isEnabled
        setGenerating(false)
        checks["generation_restores_eligible_reasoning_controls"] = thinking.isEnabled && thinkingBudget.isEnabled && samplingPreset.isEnabled
        maximumOutput.selectItem(withTitle: "81920")
        responseBudgetChanged()
        checks["larger_qwen_response_raises_context"] = maximumOutput.titleOfSelectedItem == "81920"
            && context.titleOfSelectedItem == "131072"
        conversation.append(user: "synthetic qwen user", assistant: "synthetic qwen answer")
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen35Full)!)
        selectModel()
        checks["full_qwen_selector_uses_full_precision_model"] = selectedProfile == .qwen35Full
            && modelField.stringValue == ModelProfile.qwen35Full.defaultModelPath
            && maximumOutput.titleOfSelectedItem == "32768" && context.titleOfSelectedItem == "65536"
            && conversation.isEmpty
        checks["full_qwen_starts_with_separate_sampling_and_thinking_defaults"] = thinking.state == .off
            && selectedSamplingPreset == .recommended && temperature.doubleValue == 1
        context.selectItem(withTitle: "4096")
        contextBudgetChanged()
        checks["smaller_qwen_context_reduces_response_budget"] = maximumOutput.titleOfSelectedItem == "2048"
            && context.titleOfSelectedItem == "4096"
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen35)!)
        selectModel()
        checks["full_qwen_keeps_q4_history_separate"] = conversation.turns.first?.assistant == "synthetic qwen answer"
        checks["qwen_large_budgets_restore_per_chat"] = maximumOutput.titleOfSelectedItem == "81920"
            && context.titleOfSelectedItem == "131072"
        checks["sampling_and_thinking_restore_per_model_chat"] = thinking.state == .on
            && thinkingBudget.titleOfSelectedItem == "1024" && selectedSamplingPreset == .coding
            && temperature.doubleValue == 0.75
        maximumOutput.selectItem(withTitle: "128")
        responseBudgetChanged()
        checks["thinking_budget_leaves_room_for_short_answers"] = thinkingHint.stringValue.contains("64 thinking tokens")
        maximumOutput.selectItem(withTitle: "81920")
        responseBudgetChanged()
        thinking.state = .off
        thinkingChanged()
        checks["turning_thinking_off_restores_direct_sampling"] = selectedSamplingPreset == .recommended
            && temperature.doubleValue == 1 && !thinkingBudget.isEnabled && samplingHint.stringValue.contains("Presence 2.0")
        func simulateCompletion(_ response: String, failure: String?, stopped: Bool) {
            pendingPrompt = "synthetic retry request"
            pendingResponse = response
            responseView.string = response
            replaceDraft("")
            setGenerating(true)
            completeGeneration(GenerationResult(elapsed: 0, tokensPerSecond: nil, failure: failure, stopped: stopped))
        }
        let priorCount = conversation.turns.count
        simulateCompletion("<think>\nSynthetic reasoning</think>\nSynthetic truncated answer", failure: "response_limit", stopped: false)
        checks["truncated_answer_preserves_history_and_retry_draft"] = conversation.turns.count == priorCount
            && promptView.string == "synthetic retry request" && status.stringValue.contains("response limit")
        let unfinished = "<think>\nSynthetic unfinished reasoning"
        simulateCompletion(unfinished, failure: "incomplete_result", stopped: false)
        checks["incomplete_reasoning_preserves_chat_and_draft"] = conversation.turns.count == priorCount
            && promptView.string == "synthetic retry request" && responseView.string.hasPrefix(unfinished)
            && status.stringValue.contains("before finishing its answer")
        simulateCompletion(unfinished, failure: "incomplete_result", stopped: true)
        checks["stopped_reasoning_preserves_chat_and_draft"] = conversation.turns.count == priorCount
            && promptView.string == "synthetic retry request" && responseView.string.hasPrefix(unfinished)
            && status.stringValue.contains("before finishing its answer")
        simulateCompletion("<think>\nSynthetic reasoning</think>\n \t", failure: "incomplete_result", stopped: false)
        checks["empty_final_answer_is_excluded_from_history"] = conversation.turns.count == priorCount
            && promptView.string == "synthetic retry request"
        let partialAnswer = "<think>\nSynthetic reasoning</think>\nSynthetic partial answer"
        simulateCompletion(partialAnswer, failure: "process_failed", stopped: true)
        checks["stopped_final_answer_still_enters_history"] = conversation.turns.count == priorCount + 1
            && conversation.turns.last?.assistant == partialAnswer && promptView.string.isEmpty
            && status.stringValue == "Stopped."
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .bonsai)!)
        selectModel()
        checks["qwen_large_budgets_preserve_bonsai_defaults"] = maximumOutput.titleOfSelectedItem == "512"
            && context.titleOfSelectedItem == "4096" && !maximumOutput.itemTitles.contains("32768")
        checks["bonsai_sampling_and_thinking_controls_unchanged"] = temperature.doubleValue == 0.2
            && thinking.state == .off && !thinking.isEnabled && !thinkingBudget.isEnabled
        let bonsaiCount = conversation.turns.count
        simulateCompletion("Synthetic partial Bonsai answer", failure: "process_failed", stopped: true)
        checks["bonsai_stopped_answer_behavior_unchanged"] = conversation.turns.count == bonsaiCount + 1
            && conversation.turns.last?.assistant == "Synthetic partial Bonsai answer" && promptView.string.isEmpty
            && status.stringValue == "Stopped."
        for profile in [ModelProfile.falcon3, .falconH1Tiny] {
            modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: profile)!)
            selectModel()
            checks[profile.rawValue + "_selector_defaults"] = modelField.stringValue == profile.defaultModelPath
                && conversation.isEmpty && context.titleOfSelectedItem == "4096"
                && maximumOutput.titleOfSelectedItem == "512" && temperature.doubleValue == 0.2
            checks[profile.rawValue + "_direct_controls"] = thinking.state == .off
                && !thinking.isEnabled && !thinkingBudget.isEnabled
                && thinkingHint.stringValue == "This model answers directly."
            simulateCompletion("Synthetic direct answer", failure: nil, stopped: false)
            checks[profile.rawValue + "_plain_answer_enters_history"] = conversation.turns.count == 1
                && conversation.turns.last?.assistant == "Synthetic direct answer" && promptView.string.isEmpty
            replaceDraft("Synthetic Falcon draft")
            modelSelector.selectItem(at: 0)
            selectModel()
            modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: profile)!)
            selectModel()
            checks[profile.rawValue + "_chat_restores"] = conversation.turns.count == 1
                && promptView.string == "Synthetic Falcon draft"
        }
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen3Small)!)
        selectModel()
        checks["qwen3_small_selector_uses_full_precision_defaults"] = selectedProfile == .qwen3Small
            && modelField.stringValue == ModelProfile.qwen3Small.defaultModelPath && conversation.isEmpty
            && context.titleOfSelectedItem == "8192" && maximumOutput.titleOfSelectedItem == "2048"
            && thinking.state == .off && thinking.isEnabled && !thinkingBudget.isEnabled
            && temperature.doubleValue == 0.7 && samplingHint.stringValue.contains("Top p 0.80")
        thinking.state = .on
        thinkingChanged()
        checks["qwen3_small_thinking_uses_own_sampler"] = temperature.doubleValue == 0.6
            && thinkingBudget.isEnabled && thinkingBudget.titleOfSelectedItem == "1024"
            && samplingPreset.itemTitles == ["Recommended"] && samplingHint.stringValue.contains("Presence 0.0")
        simulateCompletion("<think>\nSynthetic thought\n</think>\n\nSynthetic small Qwen answer", failure: nil, stopped: false)
        maximumOutput.selectItem(withTitle: "16384")
        responseBudgetChanged()
        checks["qwen3_small_large_response_fits_native_context"] = context.titleOfSelectedItem == "32768"
            && !context.itemTitles.contains("65536") && !maximumOutput.itemTitles.contains("32768")
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen35Full)!)
        selectModel()
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen3Small)!)
        selectModel()
        checks["qwen3_small_restores_separate_chat_and_thinking"] = conversation.turns.count == 1
            && conversation.turns.last?.assistant.contains("Synthetic small Qwen answer") == true
            && thinking.state == .on && temperature.doubleValue == 0.6
            && maximumOutput.titleOfSelectedItem == "16384" && context.titleOfSelectedItem == "32768"
        return checks
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if generating {
            quitting = true
            timer?.invalidate()
            runner.cancel()
            return .terminateLater
        }
        return .terminateNow
    }

    private func installMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Bonsai Playground", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        let undo = editMenu.addItem(withTitle: "Undo", action: #selector(undoEdit(_:)), keyEquivalent: "z")
        undo.target = self; undo.keyEquivalentModifierMask = [.command]
        let redo = editMenu.addItem(withTitle: "Redo", action: #selector(redoEdit(_:)), keyEquivalent: "z")
        redo.target = self; redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        for (title, action, key) in [("Cut", "cut:", "x"),
                                     ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                                     ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.title = "Edit"; editItem.submenu = editMenu
        menu.addItem(editItem)
        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.title = "Window"; windowItem.submenu = windowMenu
        menu.addItem(windowItem)
        NSApplication.shared.mainMenu = menu
        NSApplication.shared.windowsMenu = windowMenu
    }
}

private enum SmokeTest {
    static func run() -> Never {
        var settings = GenerationSettings()
        settings.temperature = 0
        settings.context = 2048
        settings.maximumOutput = 64
        let arguments = CommandLine.arguments
        let greetingOnly = arguments.contains("--greeting-test")
        if let index = arguments.firstIndex(of: "--profile"), index + 1 < arguments.count,
           let profile = ModelProfile(rawValue: arguments[index + 1]) {
            settings.profile = profile
            settings.model = profile.defaultModelPath
            settings.maximumOutput = profile == .bonsai ? 64 : (greetingOnly ? 2048 : profile.defaultMaximumOutput)
            settings.context = profile == .bonsai ? 2048 : profile.defaultContext
            settings.thinkingEnabled = profile.defaultThinkingEnabled
            settings.thinkingBudget = profile.defaultThinkingBudget
            settings.temperature = profile.sampling(thinking: settings.thinkingEnabled).temperature
        }
        if arguments.contains("--thinking"), settings.profile.supportsThinking {
            settings.thinkingEnabled = true
            settings.temperature = settings.profile.sampling(thinking: true).temperature
        }
        if let index = arguments.firstIndex(of: "--thinking-budget"), index + 1 < arguments.count,
           let budget = Int(arguments[index + 1]), budget >= 0 { settings.thinkingBudget = budget }
        if let index = arguments.firstIndex(of: "--max-response"), index + 1 < arguments.count,
           let budget = Int(arguments[index + 1]), budget > 0 { settings.maximumOutput = budget }
        for (flag, apply) in [
            ("--model", { (value: String) in settings.model = value }),
            ("--runtime", { (value: String) in settings.runtime = value })
        ] {
            if let index = arguments.firstIndex(of: flag), index + 1 < arguments.count {
                apply(arguments[index + 1])
            }
        }
        let runner = ModelRunner()
        var answer = ""
        var conversation = Conversation()
        var turn = 0
        func runTurn() {
            let prompt = greetingOnly ? "hi" : turn == 0 ? "Compute 17 + 25. Reply with only the integer."
                : "Add one to your last answer. Reply with only the integer."
            answer = ""
            runner.start(prompt: prompt, settings: settings, conversation: conversation,
                         onText: { answer += $0 }, onComplete: { result in
                let finalAnswer = settings.profile.finalAnswer(answer)
                let trimmed = finalAnswer.trimmingCharacters(in: .whitespacesAndNewlines)
                let expected = greetingOnly
                    ? trimmed.count < 500 && trimmed.range(of: "\\b(?:hello|hi|hey)\\b", options: [.regularExpression, .caseInsensitive]) != nil
                    : trimmed == (turn == 0 ? "42" : "43")
                let passed = result.failure == nil && !result.stopped && expected
                if passed && turn == 0 && !greetingOnly {
                    conversation.append(user: prompt, assistant: answer)
                    turn = 1
                    runTurn()
                    return
                }
                var metadata: [String: Any] = ["pass": passed, "turns_checked": turn + 1,
                    "test": greetingOnly ? "greeting" : "multi_turn_arithmetic",
                    "elapsed_seconds": result.elapsed,
                    "failure": result.failure ?? (passed ? "none" : "answer_mismatch")]
                if settings.profile != .bonsai {
                    metadata["thinking_enabled"] = settings.thinkingEnabled
                    metadata["thinking_closed"] = !settings.thinkingEnabled || answer.contains("</think>")
                    metadata["response_characters"] = answer.count
                    if !greetingOnly {
                        metadata["expected_integer_present"] = answer.contains(turn == 0 ? "42" : "43")
                        metadata["final_is_integer"] = Int(finalAnswer.trimmingCharacters(in: .whitespacesAndNewlines)) != nil
                    }
                }
                if let speed = result.tokensPerSecond { metadata["tokens_per_second"] = speed }
                if let data = try? JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]),
                   let text = String(data: data, encoding: .utf8) { print(text) }
                exit(passed ? 0 : 1)
            })
        }
        runTurn()
        dispatchMain()
    }
}

@main
private enum BonsaiPlayground {
    static func main() {
        signal(SIGPIPE, SIG_IGN)
        _ = ReasoningSupervisor.runIfRequested()
        if CommandLine.arguments.contains("--reasoning-self-test") {
            let checks = ReasoningChecks.run()
            if let data = try? JSONSerialization.data(withJSONObject: checks, options: [.sortedKeys]),
               let text = String(data: data, encoding: .utf8) { print(text) }
            exit(checks.values.allSatisfy { $0 } ? 0 : 1)
        }
        if CommandLine.arguments.contains("--conversation-self-test") {
            let checks = ConversationChecks.run().merging(ModelProfileChecks.run()) { first, _ in first }
            if let data = try? JSONSerialization.data(withJSONObject: checks, options: [.sortedKeys]),
               let text = String(data: data, encoding: .utf8) { print(text) }
            exit(checks.values.allSatisfy { $0 } ? 0 : 1)
        }
        if CommandLine.arguments.contains("--smoke-test") || CommandLine.arguments.contains("--greeting-test") { SmokeTest.run() }
        let app = NSApplication.shared
        let delegate = ApplicationDelegate()
        app.delegate = delegate
        if CommandLine.arguments.contains("--ui-self-test") {
            app.setActivationPolicy(.prohibited)
            delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
            let checks = delegate.uiChecks()
            if let data = try? JSONSerialization.data(withJSONObject: checks, options: [.sortedKeys]),
               let text = String(data: data, encoding: .utf8) { print(text) }
            exit(checks.values.allSatisfy { $0 } ? 0 : 1)
        }
        app.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { app.run() }
    }
}
