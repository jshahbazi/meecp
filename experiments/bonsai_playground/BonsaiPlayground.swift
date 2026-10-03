import AppKit
import Darwin
import Foundation

private let defaultModelPath = "/Users/johnshahbazian/development/mcpme/models/bonsai-1.7b/Ternary-Bonsai-1.7B-Q2_0_g64.gguf"
private let defaultRuntimePath = "/opt/homebrew/bin/llama-completion"

private struct GenerationSettings {
    var model = defaultModelPath
    var runtime = defaultRuntimePath
    var system = "Be helpful, concise, and accurate."
    var temperature = 0.2
    var context = 2048
    var maximumOutput = 512
    var seed = 42

    func render(_ prompt: String) -> String {
        "<|im_start|>system\n" + system + "<|im_end|>\n"
            + "<|im_start|>user\n" + prompt + "<|im_end|>\n"
            + "<|im_start|>assistant\n<think>\n\n</think>\n\n"
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

        init(onText: @escaping (String) -> Void,
             onComplete: @escaping (GenerationResult) -> Void) {
            self.onText = onText
            self.onComplete = onComplete
        }
    }

    private let queue = DispatchQueue(label: "BonsaiPlayground.model")
    private var active: Job?

    func start(prompt: String, settings: GenerationSettings,
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
                "--color", "off", "--seed", String(settings.seed), "--temp", String(settings.temperature),
                "--top-k", "40", "--top-p", "0.95", "--min-p", "0.0"
            ]
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
            let rendered = Data(settings.render(prompt).utf8)
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
            visible = job.tail.replacingOccurrences(of: "\\s+\\[end of text\\]\\s*\\z",
                                                     with: "", options: .regularExpression)
            job.tail = ""
        } else if job.tail.count > 80 {
            let boundary = job.tail.index(job.tail.endIndex, offsetBy: -80)
            visible = String(job.tail[..<boundary])
            job.tail = String(job.tail[boundary...])
        } else { return }
        if !visible.isEmpty { DispatchQueue.main.async { job.onText(visible) } }
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
        let failure = job.failure ?? (job.status == 0 ? nil : "process_failed")
        let result = GenerationResult(elapsed: Date().timeIntervalSince(job.started),
                                      tokensPerSecond: speed, failure: failure, stopped: job.stopped)
        job.stderr.removeAll(keepingCapacity: false)
        DispatchQueue.main.async { job.onComplete(result) }
    }
}

private final class ApplicationDelegate: NSObject, NSApplicationDelegate {
    private let runner = ModelRunner()
    private var window: NSWindow!
    private var promptView: NSTextView!
    private var responseView: NSTextView!
    private let status = NSTextField(labelWithString: "Ready. Each Send starts an independent prompt.")
    private let systemField = NSTextField(string: "Be helpful, concise, and accurate.")
    private let modelField = NSTextField(string: defaultModelPath)
    private let runtimeField = NSTextField(string: defaultRuntimePath)
    private let seedField = NSTextField(string: "42")
    private let temperature = NSSlider(value: 0.2, minValue: 0, maxValue: 1, target: nil, action: nil)
    private let temperatureValue = NSTextField(labelWithString: "0.20")
    private let context = NSPopUpButton(frame: .zero, pullsDown: false)
    private let maximumOutput = NSPopUpButton(frame: .zero, pullsDown: false)
    private let send = NSButton(title: "Send", target: nil, action: nil)
    private let stop = NSButton(title: "Stop", target: nil, action: nil)
    private let clear = NSButton(title: "Clear", target: nil, action: nil)
    private let settingsToggle = NSButton(checkboxWithTitle: "Show settings", target: nil, action: nil)
    private var settingsPanel: NSStackView!
    private var settingsControls: [NSControl] = []
    private var generating = false
    private var quitting = false
    private var timer: Timer?
    private var started = Date()

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenus()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 920, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Bonsai Playground"
        window.isReleasedWhenClosed = false
        window.isRestorable = false
        window.contentMinSize = NSSize(width: 760, height: 620)
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.detachesHiddenViews = true
        root.translatesAutoresizingMaskIntoConstraints = false
        let content = window.contentView!
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18),
            root.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18)
        ])
        let explanation = NSTextField(labelWithString: "Try Bonsai locally. Prompts and responses stay in memory.")
        explanation.textColor = .secondaryLabelColor
        root.addArrangedSubview(explanation)
        root.addArrangedSubview(label("Prompt"))
        let promptScroll = textArea(editable: true)
        promptView = promptScroll.documentView as? NSTextView
        root.addArrangedSubview(promptScroll)
        promptScroll.heightAnchor.constraint(equalToConstant: 150).isActive = true
        promptScroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        send.target = self; send.action = #selector(sendPrompt)
        send.bezelStyle = .rounded; send.keyEquivalent = "\r"; send.keyEquivalentModifierMask = [.command]
        stop.target = self; stop.action = #selector(stopGeneration)
        stop.bezelStyle = .rounded; stop.keyEquivalent = "\u{1b}"; stop.keyEquivalentModifierMask = []
        stop.isEnabled = false
        clear.target = self; clear.action = #selector(clearPrompt)
        clear.bezelStyle = .rounded; clear.keyEquivalent = "k"; clear.keyEquivalentModifierMask = [.command]
        let buttons = row([send, stop, clear])
        root.addArrangedSubview(buttons)
        settingsToggle.target = self; settingsToggle.action = #selector(toggleSettings)
        root.addArrangedSubview(settingsToggle)
        settingsPanel = makeSettings()
        root.addArrangedSubview(settingsPanel)
        settingsPanel.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        settingsPanel.isHidden = true
        root.addArrangedSubview(label("Response"))
        let responseScroll = textArea(editable: false)
        responseView = responseScroll.documentView as? NSTextView
        root.addArrangedSubview(responseScroll)
        responseScroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        responseScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true
        responseScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        status.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 12)
        root.addArrangedSubview(status)
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(promptView)
        NSApplication.shared.activate(ignoringOtherApps: true)
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
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 800, height: 180))
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
        let systemRow = row([label("System"), systemField])
        panel.addArrangedSubview(systemRow)
        systemRow.widthAnchor.constraint(equalTo: panel.widthAnchor).isActive = true
        temperature.target = self; temperature.action = #selector(updateTemperature)
        temperature.widthAnchor.constraint(equalToConstant: 140).isActive = true
        temperatureValue.widthAnchor.constraint(equalToConstant: 35).isActive = true
        context.addItems(withTitles: ["2048", "4096", "8192"])
        context.selectItem(withTitle: "2048")
        maximumOutput.addItems(withTitles: ["128", "512", "1024", "2048"])
        maximumOutput.selectItem(withTitle: "512")
        seedField.widthAnchor.constraint(equalToConstant: 65).isActive = true
        panel.addArrangedSubview(row([label("Temperature"), temperature, temperatureValue,
                                     label("Context"), context, label("Max response"), maximumOutput,
                                     label("Seed"), seedField]))
        let modelBrowse = NSButton(title: "Choose…", target: self, action: #selector(chooseModel))
        let runtimeBrowse = NSButton(title: "Choose…", target: self, action: #selector(chooseRuntime))
        let modelRow = row([label("Model"), modelField, modelBrowse])
        let runtimeRow = row([label("Runtime"), runtimeField, runtimeBrowse])
        panel.addArrangedSubview(modelRow)
        panel.addArrangedSubview(runtimeRow)
        modelRow.widthAnchor.constraint(equalTo: panel.widthAnchor).isActive = true
        runtimeRow.widthAnchor.constraint(equalTo: panel.widthAnchor).isActive = true
        systemField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        modelField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        runtimeField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        for field in [modelField, runtimeField] {
            field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            field.lineBreakMode = .byTruncatingMiddle
        }
        settingsControls = [systemField, modelField, runtimeField, seedField, temperature,
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
        settings.model = modelField.stringValue
        settings.runtime = runtimeField.stringValue
        settings.system = systemField.stringValue
        settings.temperature = temperature.doubleValue
        settings.context = Int(context.titleOfSelectedItem ?? "2048") ?? 2048
        settings.maximumOutput = Int(maximumOutput.titleOfSelectedItem ?? "512") ?? 512
        settings.seed = seed
        responseView.string = ""
        setGenerating(true)
        started = Date()
        status.stringValue = "Starting…"
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.status.stringValue = String(format: "Generating · %.1f s", Date().timeIntervalSince(self.started))
        }
        runner.start(prompt: prompt, settings: settings, onText: { [weak self] text in
            guard let self = self else { return }
            let scroll = self.responseView.enclosingScrollView!
            let atBottom = scroll.contentView.bounds.maxY >= self.responseView.bounds.maxY - 24
            self.responseView.textStorage?.append(NSAttributedString(string: text, attributes: [
                .font: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular),
                .foregroundColor: NSColor.textColor
            ]))
            if atBottom {
                self.responseView.scrollRangeToVisible(NSRange(location: self.responseView.string.utf16.count, length: 0))
            }
        }, onComplete: { [weak self] result in
            guard let self = self else { return }
            self.timer?.invalidate(); self.timer = nil
            self.setGenerating(false)
            self.status.stringValue = result.message
            if result.failure != nil && !result.stopped {
                self.responseView.textStorage?.append(NSAttributedString(string: "\n\n" + result.message))
            }
            if self.quitting { NSApplication.shared.reply(toApplicationShouldTerminate: true) }
        })
    }

    private func setGenerating(_ value: Bool) {
        generating = value
        send.isEnabled = !value
        clear.isEnabled = !value
        stop.isEnabled = value
        for control in settingsControls { control.isEnabled = !value }
    }

    @objc private func stopGeneration() {
        if generating { status.stringValue = "Stopping…"; runner.cancel() }
    }

    @objc private func clearPrompt() {
        guard !generating else { return }
        promptView.string = ""
        responseView.string = ""
        status.stringValue = "Ready. Each Send starts an independent prompt."
        window.makeFirstResponder(promptView)
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
        for (title, action, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"),
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
        runner.start(prompt: "Compute 17 + 25. Reply with only the integer.", settings: settings,
                     onText: { answer += $0 }, onComplete: { result in
            let passed = result.failure == nil && !result.stopped
                && answer.trimmingCharacters(in: .whitespacesAndNewlines) == "42"
            var metadata: [String: Any] = ["pass": passed, "elapsed_seconds": result.elapsed,
                                           "failure": result.failure ?? (passed ? "none" : "answer_mismatch")]
            if let speed = result.tokensPerSecond { metadata["tokens_per_second"] = speed }
            if let data = try? JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]),
               let text = String(data: data, encoding: .utf8) { print(text) }
            exit(passed ? 0 : 1)
        })
        dispatchMain()
    }
}

@main
private enum BonsaiPlayground {
    static func main() {
        signal(SIGPIPE, SIG_IGN)
        if CommandLine.arguments.contains("--smoke-test") { SmokeTest.run() }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = ApplicationDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
