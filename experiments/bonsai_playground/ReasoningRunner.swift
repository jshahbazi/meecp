import Darwin
import Foundation

private enum NativeFailure: Error { case fixed(String) }

/// Owns one authenticated UNIX-socket server for one complete native generation.
final class ReasoningRunner {
    private final class Job {
        let supervisor = Process()
        let lifetime = Pipe()
        let lock = NSLock()
        let started = Date()
        var stopped = false
        var socket: Int32 = -1
        var directory: String?
        var completion = ""
        var token = ""

        func abort() {
            lock.lock()
            stopped = true
            if socket >= 0 { _ = Darwin.shutdown(socket, SHUT_RDWR) }
            lock.unlock()
            if supervisor.isRunning { supervisor.terminate() }
        }

        var isStopped: Bool { lock.lock(); defer { lock.unlock() }; return stopped }
    }

    private let queue = DispatchQueue(label: "BonsaiPlayground.reasoning")
    private let state = NSLock()
    private var active: Job?

    func start(prompt: String, settings: GenerationSettings, conversation: Conversation,
               onText: @escaping (String) -> Void, onComplete: @escaping (GenerationResult) -> Void) {
        state.lock()
        guard active == nil else {
            state.unlock()
            DispatchQueue.main.async {
                onComplete(GenerationResult(elapsed: 0, tokensPerSecond: nil, failure: "busy", stopped: false))
            }
            return
        }
        let job = Job()
        active = job
        state.unlock()
        queue.async {
            var failure: String?
            var speed: Double?
            do {
                speed = try self.generate(job: job, prompt: prompt, settings: settings,
                                          conversation: conversation, onText: onText)
            } catch NativeFailure.fixed(let code) { failure = code }
            catch { failure = "io_failed" }
            let stopped = job.isStopped
            if failure == nil || stopped {
                if job.completion.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    failure = "empty_result"
                } else if settings.profile.finalAnswer(job.completion)
                    .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    failure = "incomplete_result"
                }
            }
            self.cleanup(job)
            let result = GenerationResult(elapsed: Date().timeIntervalSince(job.started),
                                          tokensPerSecond: speed, failure: failure, stopped: stopped)
            job.completion.removeAll(keepingCapacity: false)
            job.token.removeAll(keepingCapacity: false)
            self.state.lock(); self.active = nil; self.state.unlock()
            DispatchQueue.main.async { onComplete(result) }
        }
    }

    func cancel() {
        state.lock(); let job = active; state.unlock()
        job?.abort()
    }

    private func generate(job: Job, prompt: String, settings: GenerationSettings,
                          conversation: Conversation, onText: @escaping (String) -> Void) throws -> Double? {
        let model = (settings.model as NSString).expandingTildeInPath
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: model, isDirectory: &isDirectory), !isDirectory.boolValue else {
            throw NativeFailure.fixed("model_missing")
        }
        let completion = (settings.runtime as NSString).expandingTildeInPath
        let server = URL(fileURLWithPath: completion).deletingLastPathComponent().appendingPathComponent("llama-server").path
        guard FileManager.default.isExecutableFile(atPath: server) else { throw NativeFailure.fixed("runtime_missing") }
        let directory = "/tmp/bp-" + UUID().uuidString.lowercased()
        try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
        job.directory = directory
        let socketPath = directory + "/model.sock"
        let keyPath = directory + "/key"
        var bytes = [UInt8](repeating: 0, count: 32)
        bytes.withUnsafeMutableBytes { arc4random_buf($0.baseAddress, $0.count) }
        job.token = bytes.map { String(format: "%02x", $0) }.joined()
        bytes = [UInt8](repeating: 0, count: bytes.count)
        let key = open(keyPath, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard key >= 0 else { throw NativeFailure.fixed("launch_failed") }
        let keyData = Data((job.token + "\n").utf8)
        let written = keyData.withUnsafeBytes { Darwin.write(key, $0.baseAddress, $0.count) }
        close(key)
        guard written == keyData.count else { throw NativeFailure.fixed("launch_failed") }
        let thinking = settings.effectiveThinkingEnabled
        let budget = settings.effectiveThinkingBudget
        let sampler = settings.profile.sampling(preset: settings.samplingPreset, thinking: thinking)
        let template = thinking ? "{\"enable_thinking\":true}" : "{\"enable_thinking\":false}"
        let flashAttention = settings.profile == .falconH1Tiny ? "auto" : "on"
        var serverArguments = ["-m", model, "-c", String(settings.context), "-n", String(settings.maximumOutput),
            "-ngl", "99", "-fa", flashAttention, "-t", "6", "-np", "1", "--host", socketPath,
            "--api-key-file", keyPath, "--log-disable", "--no-webui", "--no-slots", "--no-cache-prompt",
            "--no-context-shift", "--jinja"]
        if settings.profile.supportsThinking {
            serverArguments += ["--reasoning-format", "deepseek", "--reasoning", thinking ? "on" : "off",
                "--reasoning-budget", String(budget), "--chat-template-kwargs", template]
        } else {
            serverArguments += ["--reasoning", "off"]
        }
        guard let executable = Bundle.main.executableURL else { throw NativeFailure.fixed("launch_failed") }
        job.supervisor.executableURL = executable
        job.supervisor.arguments = ["--reasoning-supervisor", "--directory", directory,
                                    "--parent-pid", String(getpid()), "--server", server, "--"] + serverArguments
        job.supervisor.standardInput = job.lifetime
        job.supervisor.standardOutput = FileHandle.nullDevice
        job.supervisor.standardError = FileHandle.nullDevice
        guard !job.isStopped else { throw NativeFailure.fixed("io_failed") }
        do { try job.supervisor.run() } catch { throw NativeFailure.fixed("launch_failed") }
        let deadline = Date().addingTimeInterval(settings.profile.generationTimeout(maximumOutput: settings.maximumOutput))
        var ready = false
        while !ready {
            try check(job, deadline: deadline)
            guard job.supervisor.isRunning else { throw NativeFailure.fixed("process_failed") }
            if FileManager.default.fileExists(atPath: socketPath) {
                _ = chmod(socketPath, 0o600)
                do {
                    let health = try UnixHTTP(path: socketPath, token: job.token, job: job, deadline: deadline)
                    defer { health.close() }
                    let status = try health.request(method: "GET", path: "/health", body: Data())
                    ready = status == 200
                    if status == 401 || status == 403 { throw NativeFailure.fixed("process_failed") }
                } catch NativeFailure.fixed(let code) where code == "timeout" || code == "process_failed" { throw NativeFailure.fixed(code) }
                catch { }
            }
            if !ready { Thread.sleep(forTimeInterval: 0.05) }
        }
        _ = unlink(keyPath)
        var body: [String: Any] = [
            "messages": settings.profile.chatMessages(system: settings.system, prompt: prompt, conversation: conversation),
            "stream": true, "stream_options": ["include_usage": true], "max_tokens": settings.maximumOutput,
            "temperature": settings.temperature, "top_k": sampler.topK, "top_p": sampler.topP, "min_p": sampler.minP,
            "presence_penalty": sampler.presencePenalty, "repeat_penalty": sampler.repetitionPenalty,
            "seed": settings.seed, "cache_prompt": false
        ]
        if settings.profile.supportsThinking {
            body["reasoning_format"] = "deepseek"
            body["reasoning_budget_tokens"] = budget
            body["chat_template_kwargs"] = ["enable_thinking": thinking]
        }
        let request = try JSONSerialization.data(withJSONObject: body)
        let connection = try UnixHTTP(path: socketPath, token: job.token, job: job, deadline: deadline)
        defer { connection.close() }
        let status = try connection.request(method: "POST", path: "/v1/chat/completions", body: request)
        guard status == 200 else {
            let error = try? connection.errorObject()
            let typed = error?["error"] as? [String: Any]
            throw NativeFailure.fixed(typed?["type"] as? String == "exceed_context_size" ? "context_full" : "process_failed")
        }
        var stream = NativeStream(profile: settings.profile, thinking: thinking)
        try connection.events { frame in
            let chunks = try stream.consume(frame)
            for text in chunks {
                job.completion += text
                DispatchQueue.main.async { onText(text) }
            }
        }
        guard stream.done else { throw NativeFailure.fixed("io_failed") }
        if stream.responseLimitReached { throw NativeFailure.fixed("response_limit") }
        return stream.speed
    }

    private func check(_ job: Job, deadline: Date) throws {
        if job.isStopped { throw NativeFailure.fixed("io_failed") }
        if Date() >= deadline { throw NativeFailure.fixed("timeout") }
    }

    private func cleanup(_ job: Job) {
        try? job.lifetime.fileHandleForWriting.close()
        if job.supervisor.isRunning { job.supervisor.terminate() }
        let deadline = Date().addingTimeInterval(3)
        while job.supervisor.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.02) }
        // The supervisor itself kills its owned server after two seconds.
        if job.supervisor.isRunning { Darwin.kill(job.supervisor.processIdentifier, SIGKILL) }
        if job.supervisor.processIdentifier > 0 { job.supervisor.waitUntilExit() }
        if let directory = job.directory { try? FileManager.default.removeItem(atPath: directory) }
    }

    /// No URLSession, proxy configuration, redirects, or TCP endpoint is involved.
    private final class UnixHTTP {
        private var descriptor: Int32
        private var buffer = Data()
        private let token: String
        private let job: Job
        private let deadline: Date
        private var chunked = false
        private var remainingLength: Int?

        init(path: String, token: String, job: Job, deadline: Date) throws {
            self.token = token; self.job = job; self.deadline = deadline
            descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
            guard descriptor >= 0 else { throw NativeFailure.fixed("io_failed") }
            var noSignal: Int32 = 1
            _ = setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout.size(ofValue: noSignal)))
            var timeout = timeval(tv_sec: 0, tv_usec: 500_000)
            _ = setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
            _ = setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
            var address = sockaddr_un()
            address.sun_family = sa_family_t(AF_UNIX)
            let encoded = Array(path.utf8) + [0]
            guard encoded.count <= MemoryLayout.size(ofValue: address.sun_path) else {
                Darwin.close(descriptor); throw NativeFailure.fixed("launch_failed")
            }
            withUnsafeMutableBytes(of: &address.sun_path) { destination in destination.copyBytes(from: encoded) }
            address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
            let connected = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
            }
            guard connected == 0 else { Darwin.close(descriptor); throw NativeFailure.fixed("io_failed") }
            job.lock.lock(); job.socket = descriptor; job.lock.unlock()
        }

        func close() {
            guard descriptor >= 0 else { return }
            job.lock.lock(); if job.socket == descriptor { job.socket = -1 }; job.lock.unlock()
            Darwin.close(descriptor); descriptor = -1
            buffer.removeAll(keepingCapacity: false)
        }

        deinit { close() }

        private func check() throws {
            if job.isStopped { throw NativeFailure.fixed("io_failed") }
            if Date() >= deadline { throw NativeFailure.fixed("timeout") }
        }

        private func more() throws -> Bool {
            var bytes = [UInt8](repeating: 0, count: 8192)
            while true {
                try check()
                let count = Darwin.read(descriptor, &bytes, bytes.count)
                if count > 0 { buffer.append(contentsOf: bytes.prefix(count)); return true }
                if count == 0 { return false }
                if errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK { continue }
                throw NativeFailure.fixed("io_failed")
            }
        }

        private func line(limit: Int) throws -> Data {
            while true {
                if let end = buffer.range(of: Data([13, 10])) {
                    let result = Data(buffer[..<end.lowerBound]); buffer.removeSubrange(..<end.upperBound)
                    guard result.count <= limit else { throw NativeFailure.fixed("io_failed") }
                    return result
                }
                guard buffer.count <= limit, try more() else { throw NativeFailure.fixed("io_failed") }
            }
        }

        private func take(_ count: Int) throws -> Data {
            guard count >= 0 && count <= 1_048_576 else { throw NativeFailure.fixed("io_failed") }
            while buffer.count < count { guard try more() else { throw NativeFailure.fixed("io_failed") } }
            let result = Data(buffer.prefix(count)); buffer.removeFirst(count); return result
        }

        func request(method: String, path: String, body: Data) throws -> Int {
            let headers = "\(method) \(path) HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer \(token)\r\nContent-Type: application/json\r\nAccept: text/event-stream\r\nConnection: close\r\nContent-Length: \(body.count)\r\n\r\n"
            var packet = Data(headers.utf8); packet.append(body)
            var sent = 0
            while sent < packet.count {
                try check()
                let count = packet.withUnsafeBytes { Darwin.write(descriptor, $0.baseAddress!.advanced(by: sent), packet.count - sent) }
                if count > 0 { sent += count }
                else if count < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) { continue }
                else { throw NativeFailure.fixed("io_failed") }
            }
            let first = String(decoding: try line(limit: 4096), as: UTF8.self).split(separator: " ")
            guard first.count >= 2, first[0] == "HTTP/1.1", let status = Int(first[1]) else { throw NativeFailure.fixed("io_failed") }
            var size = 0
            while true {
                let data = try line(limit: 8192); size += data.count
                guard size <= 16384 else { throw NativeFailure.fixed("io_failed") }
                if data.isEmpty { break }
                let header = String(decoding: data, as: UTF8.self).lowercased()
                if header.hasPrefix("transfer-encoding:") { chunked = header.contains("chunked") }
                if header.hasPrefix("content-length:") { remainingLength = Int(header.dropFirst(15).trimmingCharacters(in: .whitespaces)) }
            }
            return status
        }

        func events(_ consume: (Data) throws -> Void) throws {
            var frames = SSEFrames()
            if chunked {
                while true {
                    guard let sizeLine = String(decoding: try line(limit: 128), as: UTF8.self).split(separator: ";").first else {
                        throw NativeFailure.fixed("io_failed")
                    }
                    guard let size = Int(sizeLine, radix: 16), size >= 0 else { throw NativeFailure.fixed("io_failed") }
                    if size == 0 { break }
                    for frame in try frames.append(take(size)) { try consume(frame) }
                    guard try take(2) == Data([13, 10]) else { throw NativeFailure.fixed("io_failed") }
                }
            } else {
                while true {
                    if !buffer.isEmpty {
                        let data = buffer; buffer = Data()
                        for frame in try frames.append(data) { try consume(frame) }
                    }
                    if let remaining = remainingLength, remaining <= 0 { break }
                    guard try more() else { break }
                    if remainingLength != nil { remainingLength! -= buffer.count }
                }
            }
        }

        func errorObject() throws -> [String: Any]? {
            guard let length = remainingLength, length >= 0, length <= 16384 else { return nil }
            return try JSONSerialization.jsonObject(with: take(length)) as? [String: Any]
        }
    }
}

private struct SSEFrames {
    private var buffer = Data()
    mutating func append(_ data: Data) throws -> [Data] {
        buffer.append(data)
        var frames: [Data] = []
        while true {
            let lf = buffer.range(of: Data([10, 10]))
            let crlf = buffer.range(of: Data([13, 10, 13, 10]))
            guard let end = [lf, crlf].compactMap({ $0 }).min(by: { $0.lowerBound < $1.lowerBound }) else { break }
            let frame = Data(buffer[..<end.lowerBound]); buffer.removeSubrange(..<end.upperBound)
            guard frame.count <= 262144 else { throw NativeFailure.fixed("io_failed") }
            frames.append(frame)
        }
        guard buffer.count <= 262144 else { throw NativeFailure.fixed("io_failed") }
        return frames
    }
}

private struct NativeStream {
    let thinking: Bool
    let needsBlock: Bool
    var done = false
    var speed: Double?
    var responseLimitReached = false
    private var opened = false
    private var closed = false

    init(thinking: Bool, needsBlock: Bool) {
        self.thinking = thinking
        self.needsBlock = needsBlock
    }

    init(profile: ModelProfile, thinking: Bool) {
        self.init(thinking: profile.supportsThinking && thinking,
                  needsBlock: profile.supportsThinking && !profile.isQwen)
    }

    mutating func consume(_ frame: Data) throws -> [String] {
        let lines = String(decoding: frame, as: UTF8.self).components(separatedBy: "\n")
        let data = lines.filter { $0.hasPrefix("data:") }.map { String($0.dropFirst(5)).trimmingCharacters(in: .whitespaces) }.joined(separator: "\n")
        if data.isEmpty { return [] }
        if data == "[DONE]" { done = true; return [] }
        guard let bytes = data.data(using: .utf8), let object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
            throw NativeFailure.fixed("io_failed")
        }
        if object["error"] != nil { throw NativeFailure.fixed("process_failed") }
        if let timings = object["timings"] as? [String: Any], let value = timings["predicted_per_second"] as? Double { speed = value }
        var result: [String] = []
        for choice in object["choices"] as? [[String: Any]] ?? [] {
            if choice["finish_reason"] as? String == "length" { responseLimitReached = true }
            guard let delta = choice["delta"] as? [String: Any] else { continue }
            if let reasoning = delta["reasoning_content"] as? String, !reasoning.isEmpty {
                if !opened { result.append("<think>\n"); opened = true }
                result.append(reasoning)
            }
            if let content = delta["content"] as? String, !content.isEmpty {
                if opened && !closed { result.append("\n</think>\n\n"); closed = true }
                else if !opened && (thinking || needsBlock) { result.append("<think>\n\n</think>\n\n"); opened = true; closed = true }
                result.append(content)
            }
        }
        return result
    }
}

/// The lifetime pipe and parent identity remain independent of inference data.
enum ReasoningSupervisor {
    static func runIfRequested() -> Bool {
        let args = CommandLine.arguments
        guard args.contains("--reasoning-supervisor") else { return false }
        func value(_ name: String) -> String? {
            guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
            return args[index + 1]
        }
        guard let server = value("--server"), let directory = value("--directory"),
              directory.hasPrefix("/tmp/bp-"), URL(fileURLWithPath: directory).deletingLastPathComponent().path == "/tmp",
              let parentText = value("--parent-pid"), let parent = Int32(parentText), let separator = args.firstIndex(of: "--") else { exit(2) }
        _ = umask(0o077)
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        signal(SIGPIPE, SIG_IGN)
        let lock = NSLock()
        var cancelled = false
        let signals = [SIGTERM, SIGINT].map { number -> DispatchSourceSignal in
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
            source.setEventHandler { lock.lock(); cancelled = true; lock.unlock() }
            source.resume(); return source
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: server)
        process.arguments = Array(args[(separator + 1)...])
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch {
            try? FileManager.default.removeItem(atPath: directory); exit(2)
        }
        while process.isRunning {
            lock.lock(); let stopping = cancelled; lock.unlock()
            if stopping || getppid() != parent { break }
            var descriptor = pollfd(fd: STDIN_FILENO, events: Int16(POLLIN | POLLHUP), revents: 0)
            if poll(&descriptor, 1, 100) > 0 {
                var byte: UInt8 = 0
                if Darwin.read(STDIN_FILENO, &byte, 1) <= 0 { break }
            }
        }
        if process.isRunning { process.terminate() }
        let deadline = Date().addingTimeInterval(2)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
        if process.isRunning { Darwin.kill(process.processIdentifier, SIGKILL) }
        process.waitUntilExit()
        for source in signals { source.cancel() }
        try? FileManager.default.removeItem(atPath: directory)
        exit(0)
    }
}

enum ReasoningChecks {
    static func run() -> [String: Bool] {
        var settings = GenerationSettings()
        settings.profile = .qwen35
        settings.thinkingEnabled = true
        settings.maximumOutput = 1024
        settings.thinkingBudget = 2048
        var checks = ["native_budget_reserves_final_answer": settings.effectiveThinkingBudget == 512]
        settings.thinkingEnabled = false
        checks["disabled_thinking_budget_is_zero"] = settings.effectiveThinkingBudget == 0
        func event(_ delta: [String: String]) -> Data {
            let object: [String: Any] = ["choices": [["delta": delta]]]
            return Data(("data: " + String(decoding: try! JSONSerialization.data(withJSONObject: object), as: UTF8.self)).utf8)
        }
        var stream = NativeStream(thinking: true, needsBlock: false)
        var text = ""
        text += (try? stream.consume(event(["reasoning_content": "Synthetic reasoning"])))?.joined() ?? ""
        text += (try? stream.consume(event(["content": "42"])))?.joined() ?? ""
        checks["native_stream_preserves_reasoning_and_answer"] = text == "<think>\nSynthetic reasoning\n</think>\n\n42"
        var plain = NativeStream(thinking: false, needsBlock: false)
        checks["native_disabled_thinking_is_plain_answer"] = (try? plain.consume(event(["content": "42"])))?.joined() == "42"
        checks["native_direct_profiles_keep_plain_streaming_answers"] = [ModelProfile.falcon3, .falconH1Tiny]
            .allSatisfy { profile in
                var direct = NativeStream(profile: profile, thinking: true)
                let first = (try? direct.consume(event(["content": "4"])))?.joined()
                let second = (try? direct.consume(event(["content": "2"])))?.joined()
                _ = try? direct.consume(Data("data: [DONE]".utf8))
                return first == "4" && second == "2" && direct.done
                    && profile.finalAnswer((first ?? "") + (second ?? "")) == "42"
            }
        var frames = SSEFrames()
        let first = try? frames.append(Data("data: synthetic\r".utf8))
        let second = try? frames.append(Data("\n\r\n".utf8))
        checks["native_sse_handles_split_crlf"] = first?.isEmpty == true && second == [Data("data: synthetic".utf8)]
        checks["native_sse_rejects_unbounded_frames"] = (try? frames.append(Data(repeating: 65, count: 262145))) == nil
        settings.profile = .minicpmQ4
        checks["native_minicpm_budget_is_always_active"] = settings.effectiveThinkingEnabled && settings.effectiveThinkingBudget == 512
        let limitFrame = Data("data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"length\"}]}".utf8)
        _ = try? stream.consume(limitFrame)
        checks["native_response_limit_is_not_success"] = stream.responseLimitReached
        return checks
    }
}
