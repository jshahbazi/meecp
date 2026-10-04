import AppKit
import Darwin
import Foundation

let defaultRuntimePath = "/opt/homebrew/bin/llama-completion"

struct GenerationSettings {
    var profile = ModelProfile.bonsai
    var model = ModelProfile.bonsai.defaultModelPath
    var runtime = defaultRuntimePath
    var system = "Be helpful, concise, and accurate."
    var temperature = 0.2
    var context = 4096
    var maximumOutput = 512
    var seed = 42
    var thinkingEnabled = false
    var thinkingBudget = 2048
    var samplingPreset = SamplingPreset.recommended

    var effectiveThinkingEnabled: Bool {
        profile.supportsThinkingToggle ? thinkingEnabled : profile.supportsThinking
    }

    var effectiveThinkingBudget: Int {
        guard effectiveThinkingEnabled else { return 0 }
        let reserved = min(512, max(0, maximumOutput / 2))
        return max(0, min(thinkingBudget, maximumOutput - reserved))
    }

    func render(_ prompt: String, conversation: Conversation = Conversation()) -> String {
        profile.render(system: system, prompt: prompt, conversation: conversation, thinkingEnabled: thinkingEnabled)
    }
}

struct GenerationResult {
    let elapsed: Double
    let tokensPerSecond: Double?
    let failure: String?
    let stopped: Bool

    var message: String {
        if stopped && failure != "incomplete_result" { return "Stopped." }
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
        case "incomplete_result": return "The model stopped before finishing its answer. Increase Max response in Settings or try again."
        case "response_limit": return "The response limit was reached. Increase Max response in Settings or try again."
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

final class ModelRunner {
    private let completion = CompletionRunner()
    private let reasoning = ReasoningRunner()
    private let lock = NSLock()
    private var running = false
    private var native = false

    func start(prompt: String, settings: GenerationSettings, conversation: Conversation = Conversation(),
               onText: @escaping (String) -> Void, onComplete: @escaping (GenerationResult) -> Void) {
        lock.lock()
        guard !running else {
            lock.unlock()
            DispatchQueue.main.async {
                onComplete(GenerationResult(elapsed: 0, tokensPerSecond: nil, failure: "busy", stopped: false))
            }
            return
        }
        running = true
        native = settings.profile != .bonsai
        let useNative = native
        lock.unlock()
        let finished: (GenerationResult) -> Void = { result in
            self.lock.lock(); self.running = false; self.lock.unlock()
            onComplete(result)
        }
        if useNative {
            reasoning.start(prompt: prompt, settings: settings, conversation: conversation,
                            onText: onText, onComplete: finished)
        } else {
            completion.start(prompt: prompt, settings: settings, conversation: conversation,
                             onText: onText, onComplete: finished)
        }
    }

    func cancel() {
        lock.lock(); let useNative = native; lock.unlock()
        if useNative { reasoning.cancel() } else { completion.cancel() }
    }
}

private final class CompletionRunner {
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
        var completionText = ""

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
            let timeLimit = settings.profile.generationTimeout(maximumOutput: settings.maximumOutput)
            self.queue.asyncAfter(deadline: .now() + timeLimit) {
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
            job.completionText += chunk
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
        let incomplete = job.hasVisibleText && job.profile != .bonsai
            && job.profile.finalAnswer(job.completionText).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let failure = job.failure ?? (contextFull ? "context_full"
            : (incomplete ? "incomplete_result"
                : (job.status != 0 ? "process_failed" : (job.hasVisibleText ? nil : "empty_result"))))
        let result = GenerationResult(elapsed: Date().timeIntervalSince(job.started),
                                      tokensPerSecond: speed, failure: failure, stopped: job.stopped)
        job.stderr.removeAll(keepingCapacity: false)
        job.completionText.removeAll(keepingCapacity: false)
        DispatchQueue.main.async { job.onComplete(result) }
    }
}
