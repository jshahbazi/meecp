import AppKit
import Darwin
import Foundation

private let defaultRuntimePath = "/opt/homebrew/bin/llama-completion"

private struct GenerationSettings {
    var profile = ModelProfile.bonsai
    var model = ModelProfile.bonsai.defaultModelPath
    var runtime = defaultRuntimePath
    var system = "Be helpful, concise, and accurate."
    var temperature = 0.2
    var context = 4096
    var maximumOutput = 512
    var seed = 42

    func render(_ prompt: String, conversation: Conversation = Conversation()) -> String {
        profile.render(system: system, prompt: prompt, conversation: conversation)
    }
}

private struct GenerationResult {
    let elapsed: Double
    let tokensPerSecond: Double?
    let failure: String?
    let stopped: Bool

    var message: String {
        if stopped { return "Stopped." }
        switch failure {
        case "model_missing": return "Choose an existing model file in Settings."
        case "runtime_missing": return "Choose an executable llama-completion runtime in Settings."
        case "launch_failed": return "Could not start the model. Check the model and runtime settings."
        case "io_failed": return "The model connection ended unexpectedly."
        case "process_failed": return "The model could not complete this prompt. Check the settings."
        case "timeout": return "The model took too long and was stopped."
        case "busy": return "Wait for the current response or press Stop."
        case "context_full": return "Conversation reached the context limit. Increase Context in Settings or start a New Chat."
        case "empty_result": return "The model finished without an answer. Try another prompt or change the settings."
        case .some: return "The model could not complete this prompt."
        case .none:
            if let speed = tokensPerSecond {
                return String(format: "Done in %.2f s · %.1f tokens/s", elapsed, speed)
            }
            return String(format: "Done in %.2f s", elapsed)
        }
    }
}

// Keep a partial UTF-8 sequence until the next pipe read. Replacement is used
// only for malformed bytes, rather than for valid characters split across reads.
private struct UTF8Stream {
    private var pending = Data()

    mutating func consume(_ data: Data, final: Bool = false) -> String {
        pending.append(data)
        var count = pending.count
        if !final && count > 0 {
            let bytes = Array(pending)
            var lead = count - 1
            while lead > 0 && bytes[lead] & 0xc0 == 0x80 { lead -= 1 }
            let byte = bytes[lead]
            let width: Int
            if byte >= 0xc2 && byte <= 0xdf { width = 2 }
            else if byte >= 0xe0 && byte <= 0xef { width = 3 }
            else if byte >= 0xf0 && byte <= 0xf4 { width = 4 }
            else { width = 1 }
            if count - lead < width { count = lead }
        }
        let text = String(decoding: pending.prefix(count), as: UTF8.self)
        pending.removeFirst(count)
        return text
    }
}

private final class ModelRunner {
    private final class Job {
        let process = Process()
        let input = Pipe()
        let output = Pipe()
        let errors = Pipe()
        let started = Date()
        let onText: (String) -> Void
        let onComplete: (GenerationResult) -> Void
        var decoder = UTF8Stream()
        var tail = ""
        var stderr = Data()
        var outputEnded = false
        var errorsEnded = false
        var exited = false
        var status: Int32 = 0
        var stopped = false
        var failure: String?
        var outputPrefix = ""
        var profile = ModelProfile.bonsai
        var hasVisibleText = false

        init(onText: @escaping (String) -> Void,
             onComplete: @escaping (GenerationResult) -> Void) {
            self.onText = onText
            self.onComplete = onComplete
        }
    }

    private let queue = DispatchQueue(label: "BonsaiPlayground.model")
    private var active: Job?

    func start(prompt: String, settings: GenerationSettings, conversation: Conversation = Conversation(),
               onText: @escaping (String) -> Void,
               onComplete: @escaping (GenerationResult) -> Void) {
        queue.async {
            guard self.active == nil else {
                DispatchQueue.main.async {
                    onComplete(GenerationResult(elapsed: 0, tokensPerSecond: nil,
                                                failure: "busy", stopped: false))
                }
                return
            }
            let job = Job(onText: onText, onComplete: onComplete)
            job.outputPrefix = settings.profile.assistantOutputPrefix
            job.profile = settings.profile
            let model = (settings.model as NSString).expandingTildeInPath
            let runtime = (settings.runtime as NSString).expandingTildeInPath
            var directory: ObjCBool = false
            if !FileManager.default.fileExists(atPath: model, isDirectory: &directory)
                || directory.boolValue {
                self.completeUnstarted(job, failure: "model_missing")
                return
            }
            if !FileManager.default.isExecutableFile(atPath: runtime) {
                self.completeUnstarted(job, failure: "runtime_missing")
                return
            }
            self.active = job
            job.process.executableURL = URL(fileURLWithPath: runtime)
            job.process.arguments = [
                "-m", model, "-f", "/dev/stdin", "-c", String(settings.context),
                "-n", String(settings.maximumOutput), "-ngl", "99", "-fa", "on", "-t", "6",
                "--no-conversation", "--no-jinja", "--no-display-prompt", "--simple-io",
                "--no-context-shift", "--no-escape",
                "--color", "off", "--seed", String(settings.seed), "--temp", String(settings.temperature),
                "--top-k", "40", "--top-p", "0.95", "--min-p", "0.0"
            ] + settings.profile.runtimeArguments
            job.process.standardInput = job.input
            job.process.standardOutput = job.output
            job.process.standardError = job.errors
            job.process.terminationHandler = { process in
                self.queue.async {
                    job.exited = true
                    job.status = process.terminationStatus
                    self.finishIfReady(job)
                }
            }
            do {
                try job.process.run()
            } catch {
                job.process.terminationHandler = nil
                self.active = nil
                self.completeUnstarted(job, failure: "launch_failed")
                return
            }
            self.drain(job.output.fileHandleForReading, job: job, isOutput: true)
            self.drain(job.errors.fileHandleForReading, job: job, isOutput: false)
            let rendered = Data(settings.render(prompt, conversation: conversation).utf8)
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    try job.input.fileHandleForWriting.write(contentsOf: rendered)
                    try job.input.fileHandleForWriting.close()
                } catch {
                    try? job.input.fileHandleForWriting.close()
                    self.queue.async {
                        if job.failure == nil { job.failure = "io_failed" }
                        self.finishIfReady(job)
                    }
                }
            }
            self.queue.asyncAfter(deadline: .now() + 90) {
                guard self.active === job else { return }
                job.failure = "timeout"
                self.stop(job)
            }
        }
    }

    func cancel() {
        queue.async {
            guard let job = self.active else { return }
            job.stopped = true
            self.stop(job)
        }
    }

    private func stop(_ job: Job) {
        guard active === job else { return }
        if job.process.isRunning { job.process.terminate() }
        queue.asyncAfter(deadline: .now() + 2) {
            guard self.active === job, job.process.isRunning else { return }
            // This PID belongs to the still-running Process owned by this job.
            Darwin.kill(job.process.processIdentifier, SIGKILL)
        }
    }

    private func completeUnstarted(_ job: Job, failure: String) {
        let result = GenerationResult(elapsed: Date().timeIntervalSince(job.started),
                                      tokensPerSecond: nil, failure: failure, stopped: false)
        DispatchQueue.main.async { job.onComplete(result) }
    }

    private func drain(_ handle: FileHandle, job: Job, isOutput: Bool) {
        DispatchQueue.global(qos: .userInitiated).async {
            var failed = false
            do {
                while let data = try handle.read(upToCount: 4096), !data.isEmpty {
                    self.queue.async {
                        if isOutput {
                            self.emit(job.decoder.consume(data), job: job, final: false)
                        } else {
                            job.stderr.append(data)
                            if job.stderr.count > 131_072 {
                                job.stderr.removeFirst(job.stderr.count - 131_072)
                            }
                        }
                    }
                }
            } catch { failed = true }
            try? handle.close()
            let readFailed = failed
            self.queue.async {
                if readFailed && job.failure == nil { job.failure = "io_failed" }
                if isOutput {
                    self.emit(job.decoder.consume(Data(), final: true), job: job, final: true)
                    job.outputEnded = true
                } else {
                    job.errorsEnded = true
                }
                self.finishIfReady(job)
            }
        }
    }

    private func emit(_ text: String, job: Job, final: Bool) {
        job.tail += text
        let visible: String
        if final {
            visible = job.profile.cleanCompletionTail(job.tail.replacingOccurrences(
                of: "\\s+\\[end of text\\]\\s*\\z", with: "", options: .regularExpression))
            job.tail = ""
        } else if job.tail.count > 80 {
            let boundary = job.tail.index(job.tail.endIndex, offsetBy: -80)
            visible = String(job.tail[..<boundary])
            job.tail = String(job.tail[boundary...])
        } else { return }
        if !visible.isEmpty {
            if final && !job.hasVisibleText && visible.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return
            }
            if !visible.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                job.hasVisibleText = true
            }
            let chunk = job.outputPrefix + visible
            job.outputPrefix = ""
            DispatchQueue.main.async { job.onText(chunk) }
        }
    }

    private func finishIfReady(_ job: Job) {
        guard active === job, job.exited, job.outputEnded, job.errorsEnded else { return }
        active = nil
        job.process.terminationHandler = nil
        let diagnostics = String(decoding: job.stderr, as: UTF8.self)
        func number(_ pattern: String) -> Double? {
            guard let expression = try? NSRegularExpression(pattern: pattern),
                  let match = expression.firstMatch(in: diagnostics,
                    range: NSRange(diagnostics.startIndex..., in: diagnostics)),
                  let range = Range(match.range(at: 1), in: diagnostics) else { return nil }
            return Double(diagnostics[range])
        }
        let milliseconds = number("(?<!prompt )eval time\\s*=\\s*([\\d.]+)\\s*ms")
        let steps = number("(?<!prompt )eval time\\s*=\\s*[\\d.]+\\s*ms\\s*/\\s*(\\d+)\\s*runs")
        let speed: Double?
        if let ms = milliseconds, let count = steps, ms > 0 { speed = count * 1000 / ms }
        else { speed = nil }
        let contextFull = diagnostics.range(of: "prompt is too long", options: .caseInsensitive) != nil
            || diagnostics.range(of: "context full", options: .caseInsensitive) != nil
        let failure = job.failure ?? (contextFull ? "context_full"
            : (job.status != 0 ? "process_failed" : (job.hasVisibleText ? nil : "empty_result")))
        let result = GenerationResult(elapsed: Date().timeIntervalSince(job.started),
                                      tokensPerSecond: speed, failure: failure, stopped: job.stopped)
        job.stderr.removeAll(keepingCapacity: false)
        DispatchQueue.main.async { job.onComplete(result) }
    }
}

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
        let maximumOutput: String
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
        modelSelector.toolTip = "Bonsai, MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic, and Qwen3.5-2B in Q4_K_M or full-precision BF16"
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
        context.addItems(withTitles: ["2048", "4096", "8192"])
        context.selectItem(withTitle: "4096")
        maximumOutput.addItems(withTitles: ["128", "512", "1024", "2048"])
        maximumOutput.selectItem(withTitle: "512")
        seedField.widthAnchor.constraint(equalToConstant: 65).isActive = true
        panel.addArrangedSubview(row([label("Temperature"), temperature, temperatureValue,
                                     label("Context"), context, label("Max response"), maximumOutput,
                                     label("Seed"), seedField]))
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
                            context, maximumOutput, modelBrowse, runtimeBrowse]
        return panel
    }

    @objc private func updateTemperature() {
        temperatureValue.stringValue = String(format: "%.2f", temperature.doubleValue)
    }

    @objc private func toggleSettings() {
        let visible = settingsToggle.state == .on
        settingsPanel.isHidden = !visible
        window.contentMinSize = NSSize(width: 760, height: visible ? 760 : 620)
        if visible && window.contentView!.bounds.height < 760 {
            var frame = window.frame
            let addition = 760 - window.contentView!.bounds.height
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
            maximumOutput: maximumOutput.titleOfSelectedItem ?? "512")
        selectedProfile = profile
        let session = sessions[profile]
        conversation = session?.conversation ?? Conversation()
        responseView.textStorage?.setAttributedString(session?.transcript ?? NSAttributedString(string: ""))
        replaceDraft(session?.draft ?? "")
        promptView.undoManager?.removeAllActions()
        modelField.stringValue = session?.modelPath ?? profile.defaultModelPath
        maximumOutput.selectItem(withTitle: session?.maximumOutput ?? String(profile.defaultMaximumOutput))
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
            guard let self = self else { return }
            self.timer?.invalidate(); self.timer = nil
            self.setGenerating(false)
            self.status.stringValue = result.message
            if (result.failure == nil || result.stopped) && !self.pendingResponse.isEmpty {
                self.conversation.append(user: self.pendingPrompt, assistant: self.pendingResponse)
            } else {
                self.replaceDraft(self.pendingPrompt)
            }
            if result.failure != nil && !result.stopped {
                self.responseView.textStorage?.append(NSAttributedString(string: "\n\n" + result.message))
            }
            self.responseView.textStorage?.append(NSAttributedString(string: "\n\n", attributes: self.bodyAttributes))
            self.pendingPrompt = ""
            self.pendingResponse = ""
            self.window.makeFirstResponder(self.promptView)
            if self.quitting { NSApplication.shared.reply(toApplicationShouldTerminate: true) }
        })
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
        conversation.append(user: "synthetic qwen user", assistant: "synthetic qwen answer")
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen35Full)!)
        selectModel()
        checks["full_qwen_selector_uses_full_precision_model"] = selectedProfile == .qwen35Full
            && modelField.stringValue == ModelProfile.qwen35Full.defaultModelPath
            && maximumOutput.titleOfSelectedItem == "2048" && conversation.isEmpty
        modelSelector.selectItem(at: ModelProfile.selectableProfiles.firstIndex(of: .qwen35)!)
        selectModel()
        checks["full_qwen_keeps_q4_history_separate"] = conversation.turns.first?.assistant == "synthetic qwen answer"
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
            settings.maximumOutput = profile == .bonsai ? 64 : profile.defaultMaximumOutput
            settings.context = profile == .bonsai ? 2048 : 4096
        }
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
                    metadata["thinking_closed"] = answer.contains("</think>")
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
