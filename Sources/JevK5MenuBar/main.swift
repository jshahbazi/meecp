import AppKit
import Foundation
import ServiceManagement
import Darwin
import PrivateTransport

private struct BackendStatus {
    let state: String
    let ready: Bool
    let paused: Bool
    let modelID: String
    let decisions: Int
    let cacheHits: Int
    let errors: Int
    let lastLatency: Double?
    let backendPID: Int32

    init?(_ object: [String: Any]) {
        guard let state = object["state"] as? String,
              ["loading", "ready", "paused", "error"].contains(state),
              let ready = object["ready"] as? Bool,
              let paused = object["paused"] as? Bool,
              let pid = object["backend_pid"] as? Int,
              pid > 0, pid <= Int(Int32.max),
              let model = object["model"] as? [String: Any],
              let counts = object["counts"] as? [String: Any],
              let decisions = counts["decisions"] as? Int,
              let cacheHits = counts["cache_hits"] as? Int,
              let errors = counts["errors"] as? Int,
              decisions >= 0, cacheHits >= 0, errors >= 0 else { return nil }
        self.state = state
        self.ready = ready
        self.paused = paused
        self.modelID = (model["id"] as? String) ?? "JevK5-4B"
        self.decisions = decisions
        self.cacheHits = cacheHits
        self.errors = errors
        let latency = object["last_latency_ms"] as? Double
        self.lastLatency = latency.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
        self.backendPID = Int32(pid)
    }
}

private struct Connection {
    let endpoint: URL
    let token: String
}

final class MenuBarApp: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var service: Process?
    private var status: BackendStatus?
    private var pollTimer: Timer?
    private var pollInFlight = false
    private var controlInFlight = false
    private var stopping = false
    private var quitting = false
    private var stopCompletion: (() -> Void)?
    private var startupDate = Date()
    private var statusMessage = "Starting service…"
    private var generation: UInt64 = 0
    private let stateDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/JevK5", isDirectory: true)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "brain.head.profile", accessibilityDescription: "JevK5 local decision model")
            button.image?.isTemplate = true
        }
        rebuildMenu()
        startService()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // The stdio adapter opens the existing app when its backend is unavailable.
        // Reopening an accessory app does not relaunch it, so recover the service here.
        if service == nil && !quitting && !stopping { startService() }
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        pollTimer?.invalidate()
        pollTimer = nil
        stopService { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        // The backend also watches this app's PID in case termination skips the delegate.
        if let process = service, process.isRunning { process.terminate() }
    }

    private func menuItem(_ title: String, action: Selector? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.isEnabled = action != nil
        return item
    }

    private func rebuildMenu() {
        guard statusItem != nil else { return }
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.addItem(menuItem("JevK5 — \(statusMessage)"))
        if let status = status {
            menu.addItem(menuItem(status.modelID))
            menu.addItem(menuItem("Decisions: \(status.decisions)"))
            menu.addItem(menuItem("Cache hits: \(status.cacheHits)"))
            if status.errors > 0 { menu.addItem(menuItem("Failed calls: \(status.errors)")) }
            if let latency = status.lastLatency {
                menu.addItem(menuItem(String(format: "Last decision: %.0f ms", latency)))
            }
        }
        menu.addItem(.separator())
        let pause = menuItem(status?.paused == true ? "Resume Model" : "Pause Model", action: #selector(togglePaused))
        pause.isEnabled = status != nil && service?.isRunning == true && !controlInFlight && !stopping
        menu.addItem(pause)
        let restart = menuItem("Restart Service", action: #selector(restartService))
        restart.isEnabled = !stopping && !quitting
        menu.addItem(restart)
        menu.addItem(.separator())
        menu.addItem(menuItem("Copy MCP Configuration", action: #selector(copyMCPConfiguration)))
        menu.addItem(menuItem("Show Setup", action: #selector(showSetup)))
        let login = menuItem("Launch at Login", action: #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        let quit = menuItem("Quit JevK5", action: #selector(quitApp))
        quit.keyEquivalent = "q"
        menu.addItem(quit)
        statusItem.menu = menu
        statusItem.button?.toolTip = "JevK5: \(statusMessage)"
    }

    private func startService() {
        guard !quitting, service == nil, !stopping else { return }
        generation &+= 1
        status = nil
        startupDate = Date()
        statusMessage = "Starting service…"
        pollInFlight = false
        controlInFlight = false
        rebuildMenu()
        guard let resources = Bundle.main.resourceURL else {
            statusMessage = "App resources are unavailable"
            rebuildMenu()
            return
        }
        let python = resources.appendingPathComponent("python/bin/python3.12")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            statusMessage = "Bundled runtime is unavailable"
            rebuildMenu()
            return
        }
        let process = Process()
        process.executableURL = python
        process.arguments = ["-P", "-m", "jevk5_app", "serve", "--resources", resources.path,
                             "--state-dir", stateDirectory.path, "--parent-pid", String(getpid())]
        var environment = ProcessInfo.processInfo.environment
        for name in ["PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONINSPECT"] {
            environment.removeValue(forKey: name)
        }
        environment["PYTHONPATH"] = ["backend", "vendor", "python/site-packages"]
            .map { resources.appendingPathComponent($0).path }.joined(separator: ":")
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self, weak process] _ in
            DispatchQueue.main.async {
                guard let self = self, let process = process, self.service === process else { return }
                self.service = nil
                self.generation &+= 1
                self.status = nil
                self.pollInFlight = false
                self.controlInFlight = false
                let completion = self.stopCompletion
                self.stopCompletion = nil
                self.stopping = false
                if !self.quitting {
                    self.statusMessage = completion == nil ? "Service stopped unexpectedly" : "Service stopped"
                    self.rebuildMenu()
                }
                completion?()
            }
        }
        service = process
        do {
            try process.run()
            pollStatus()
        } catch {
            service = nil
            statusMessage = "Service could not start"
            rebuildMenu()
        }
    }

    private func stopService(completion: @escaping () -> Void) {
        if stopping {
            // Quit can arrive while Restart is waiting for process termination.
            // Replace the restart continuation so termination still gets a reply.
            if quitting { stopCompletion = completion }
            return
        }
        generation &+= 1
        pollInFlight = false
        controlInFlight = false
        status = nil
        guard let process = service, process.isRunning else {
            service = nil
            DispatchQueue.main.async(execute: completion)
            return
        }
        stopping = true
        stopCompletion = completion
        statusMessage = "Stopping service…"
        rebuildMenu()
        process.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self, weak process] in
            guard let self = self, let process = process,
                  self.service === process, process.isRunning else { return }
            // Process identity is held until termination; never signal a discovered PID.
            kill(process.processIdentifier, SIGKILL)
        }
    }

    private func connection() -> Connection? {
        let file = stateDirectory.appendingPathComponent("connection.json")
        // Refuse symlinks or group/world-readable credentials before opening the file.
        var info = stat()
        guard lstat(file.path, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), (info.st_mode & 0o077) == 0,
              info.st_size > 0, info.st_size <= 16_384,
              let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let endpointString = object["endpoint"] as? String,
              let endpoint = URL(string: endpointString),
              endpoint.scheme == "http", endpoint.host == "127.0.0.1", endpoint.port == 19385,
              endpoint.user == nil, endpoint.password == nil,
              endpoint.query == nil, endpoint.fragment == nil,
              endpoint.path.isEmpty || endpoint.path == "/",
              let token = object["token"] as? String,
              !token.isEmpty, token.count <= 512,
              token.unicodeScalars.allSatisfy({ $0.value >= 33 && $0.value <= 126 }) else { return nil }
        return Connection(endpoint: endpoint, token: token)
    }

    private func request(_ path: String, connection: Connection) -> URLRequest {
        var request = URLRequest(url: connection.endpoint.appendingPathComponent(path))
        request.timeoutInterval = 3
        request.setValue("Bearer \(connection.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private func pollStatus() {
        guard !quitting, !stopping, !pollInFlight,
              let process = service, process.isRunning else { return }
        guard let connection = connection() else {
            if Date().timeIntervalSince(startupDate) > 10 {
                status = nil
                statusMessage = "Service connection is unavailable"
                rebuildMenu()
            }
            return
        }
        pollInFlight = true
        let expectedGeneration = generation
        let expectedPID = process.processIdentifier
        PrivateHTTP.request(request("api/status", connection: connection), stateDirectory: stateDirectory, service: "jevk5") { [weak self] data, response, _ in
            var parsed: BackendStatus?
            if let response = response, response.statusCode == 200,
               let data = data, data.count <= 65_536,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                parsed = BackendStatus(object)
            }
            DispatchQueue.main.async {
                guard let self = self, self.generation == expectedGeneration,
                      self.service === process, process.isRunning else { return }
                self.pollInFlight = false
                guard let value = parsed, value.backendPID == expectedPID else {
                    self.status = nil
                    self.statusMessage = parsed == nil ? "Waiting for service…" : "Another service owns this connection"
                    self.rebuildMenu()
                    return
                }
                self.status = value
                switch value.state {
                case "ready": self.statusMessage = value.ready ? "Ready" : "Loading model…"
                case "paused": self.statusMessage = "Paused · model unloaded"
                case "error": self.statusMessage = "Model unavailable · restart service"
                default: self.statusMessage = "Loading model…"
                }
                self.rebuildMenu()
            }
        }
    }

    @objc private func togglePaused() {
        guard let status = status, !controlInFlight, !stopping,
              let process = service, process.isRunning, let connection = connection() else { return }
        let expectedGeneration = generation
        controlInFlight = true
        rebuildMenu()
        var request = request("api/control", connection: connection)
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["action": status.paused ? "resume" : "pause"])
        PrivateHTTP.request(request, stateDirectory: stateDirectory, service: "jevk5") { [weak self] _, response, error in
            let success = error == nil && response?.statusCode == 200
            DispatchQueue.main.async {
                guard let self = self, self.generation == expectedGeneration,
                      self.service === process, process.isRunning else { return }
                self.controlInFlight = false
                self.rebuildMenu()
                if !success { self.showError("The model control request could not be completed. Check the menu status or restart the service.") }
                self.pollStatus()
            }
        }
    }

    @objc private func restartService() {
        guard !stopping, !quitting else { return }
        stopService { [weak self] in self?.startService() }
    }

    @objc private func copyMCPConfiguration() {
        let command = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/jevk5-mcp").path
        guard FileManager.default.isExecutableFile(atPath: command),
              let data = try? JSONSerialization.data(withJSONObject: [
                "mcpServers": ["JevK5": ["command": command, "args": [String]()] ]
              ], options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else {
            showError("The bundled MCP adapter is unavailable. Rebuild or reinstall the app.")
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    @objc private func showSetup() {
        guard let resources = Bundle.main.resourceURL else { return }
        let path = resources.appendingPathComponent("README.md")
        guard FileManager.default.fileExists(atPath: path.path) else {
            showError("The bundled setup document is unavailable. Rebuild or reinstall the app.")
            return
        }
        NSWorkspace.shared.open(path)
    }

    @objc private func toggleLogin() {
        do {
            switch SMAppService.mainApp.status {
            case .enabled: try SMAppService.mainApp.unregister()
            case .requiresApproval:
                SMAppService.openSystemSettingsLoginItems()
            default: try SMAppService.mainApp.register()
            }
            if SMAppService.mainApp.status == .requiresApproval {
                SMAppService.openSystemSettingsLoginItems()
            }
        } catch {
            showError("Launch at login could not be updated. Check System Settings → General → Login Items for JevK5.")
        }
        rebuildMenu()
    }

    private func showError(_ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "JevK5"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    @objc private func quitApp() { NSApp.terminate(nil) }
}

// Fixed native login-item operations for a verified meecp recovery bundle.
// These return before starting AppKit or the legacy backend.
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--mcpme-handoff" {
    let action = CommandLine.arguments[2]
    var result: [String: Any] = ["api_version": 1]
    var exitCode: Int32 = 0
    do {
        guard Bundle.main.bundleIdentifier == "com.jshahbazi.jevk5" else { throw NSError(domain: "handoff", code: 1) }
        switch action {
        case "login-status": break
        case "login-disable":
            if SMAppService.mainApp.status == .enabled || SMAppService.mainApp.status == .requiresApproval {
                try SMAppService.mainApp.unregister()
            }
        case "login-enable":
            if SMAppService.mainApp.status != .enabled { try SMAppService.mainApp.register() }
        default: throw NSError(domain: "handoff", code: 2)
        }
        switch SMAppService.mainApp.status {
        case .enabled: result["login_status"] = "enabled"
        case .notRegistered: result["login_status"] = "not_registered"
        case .requiresApproval: result["login_status"] = "requires_approval"
        case .notFound: result["login_status"] = "not_found"
        @unknown default: result["login_status"] = "unknown"
        }
    } catch { result["error"] = ["code": "legacy_login_update_failed"]; exitCode = 2 }
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), let text = String(data: data, encoding: .utf8) {
        print(text)
    }
    exit(exitCode)
}

let application = NSApplication.shared
let delegate = MenuBarApp()
application.delegate = delegate
application.run()
