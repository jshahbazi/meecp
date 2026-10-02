import AppKit
import Foundation
import ServiceManagement
import Darwin
import CoreFoundation
import UniformTypeIdentifiers
import PrivateTransport

private let responseLimit = 1_048_576

private func validUUID(_ value: String) -> Bool {
    UUID(uuidString: value)?.uuidString.lowercased() == value
}

private func integer(_ object: Any?) -> Int? {
    guard let number = object as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
          number.doubleValue >= 0, number.doubleValue < Double(Int.max) else { return nil }
    return number.intValue
}

private func safeCode(_ object: Any?) -> String? {
    guard let value = object as? String, value.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression) != nil else { return nil }
    return value
}

private struct Project: Equatable {
    let id: String
    let name: String
    let state: String
    let port: Int
    let policy: String
    let generation: Int
    let error: String?

    init?(_ object: [String: Any]) {
        guard let id = object["slot_id"] as? String, validUUID(id),
              let name = object["name"] as? String, !name.isEmpty, name.count <= 256,
              !name.unicodeScalars.contains(where: { $0.value < 32 }),
              let state = object["observed_state"] as? String,
              ["stopped", "starting", "loading", "warming_up", "ready", "stopping", "draining", "failed", "ownership_lost"].contains(state),
              let port = integer(object["public_port"]), (1024...65535).contains(port),
              let policy = object["start_policy"] as? String, ["manual", "host", "first_call"].contains(policy),
              let generation = integer(object["generation"]), generation > 0 else { return nil }
        self.id = id; self.name = name; self.state = state; self.port = port
        self.policy = policy; self.generation = generation; self.error = safeCode(object["error_code"])
    }

    var status: String {
        ["stopped": "Stopped", "starting": "Starting", "loading": "Loading", "warming_up": "Warming up",
         "ready": "Ready", "stopping": "Stopping", "draining": "Stopping", "failed": "Failed",
         "ownership_lost": "Reconciliation required"][state] ?? "Unavailable"
    }
    var policyLabel: String { ["manual": "Manual", "host": "Automatic", "first_call": "On first call"][policy] ?? "Manual" }
    var blocked: Bool { state == "ownership_lost" || error == "installation_recovery_required" }
}

private func projectForSelection(row: Int, rendered: [Project]?, current: [Project]) -> Project? {
    guard let rendered = rendered, rendered.indices.contains(row) else { return nil }
    let id = rendered[row].id
    return current.first { $0.id == id }
}

private struct Operation {
    let id: String
    let projectID: String?
    let action: String
    let status: String
    let error: String?

    init?(_ object: [String: Any]) {
        guard let id = object["operation_id"] as? String, validUUID(id),
              let action = object["action"] as? String,
              ["install", "start", "stop", "restart", "remove", "recover", "migrate"].contains(action),
              let status = object["status"] as? String,
              ["pending", "running", "completed", "failed", "canceled", "recovery_required"].contains(status) else { return nil }
        self.id = id; self.action = action; self.status = status
        let projectID = object["slot_id"] as? String
        if let projectID = projectID, !validUUID(projectID) { return nil }
        self.projectID = projectID; self.error = safeCode(object["error_code"])
    }
    var active: Bool { ["pending", "running"].contains(status) }
    var label: String {
        let state = ["pending": "Pending", "running": "Running", "completed": "Completed", "failed": "Failed",
                     "canceled": "Canceled", "recovery_required": "Recovery required"][status] ?? "Unavailable"
        return action.capitalized + ": " + state + (error.map { " (\($0))" } ?? "")
    }
}

private struct ImportDraft {
    let id: String
    let revision: Int
    let state: String
    let name: String
    let sourcePath: String
    let sourceKind: String
    let sourceSnapshotPath: String?
    let intent: String
    let documentRole: String?
    let start: Bool
    let policy: String
    let blockers: [String]
    let packagePath: String?
    let packageDigest: String?
    let operationID: String?
    let resultSlotID: String?

    init?(_ object: [String: Any]) {
        let summary = object["summary"] as? Bool == true
        guard let id = object["draft_id"] as? String, validUUID(id),
              let revision = integer(object["revision"]), revision > 0,
              let state = safeCode(object["state"]),
              let name = object["name"] as? String, !name.isEmpty, name.count <= 256,
              !name.unicodeScalars.contains(where: { $0.value < 32 }),
              let source = object["source"] as? [String: Any],
              let path = source["locator"] as? String ?? (summary ? "" : nil),
              (summary || path.hasPrefix("/")), !path.contains("\0"),
              let kind = safeCode(source["kind"]),
              let policy = object["start_policy"] as? String, ["manual", "host"].contains(policy),
              let start = object["start"] as? Bool else { return nil }
        let intent = object["intent"] as? String ?? ""
        guard intent.count <= 16384, !intent.contains("\0") else { return nil }
        self.id = id; self.revision = revision; self.state = state; self.name = name
        self.sourcePath = path; self.sourceKind = kind; self.intent = intent
        let snapshot = source["snapshot_path"] as? String
        self.sourceSnapshotPath = snapshot?.hasPrefix("/") == true ? snapshot : nil
        self.documentRole = safeCode(object["document_role"]); self.start = start; self.policy = policy
        self.blockers = (object["blockers"] as? [[String: Any]] ?? []).compactMap { safeCode($0["code"]) }
        let attachment = object["attachment"] as? [String: Any]
        let packagePath = attachment?["path"] as? String
        self.packagePath = packagePath?.hasPrefix("/") == true ? packagePath : nil
        self.packageDigest = attachment?["digest"] as? String
        let operationID = object["operation_id"] as? String
        self.operationID = operationID.flatMap { validUUID($0) ? $0 : nil }
        let slotID = object["result_slot_id"] as? String
        self.resultSlotID = slotID.flatMap { validUUID($0) ? $0 : nil }
    }

    var editable: Bool { !["submitted", "installed", "canceled", "expired"].contains(state) }
    var isDocument: Bool { ["document", "text_document", "markdown", "text"].contains(sourceKind) }
    var label: String {
        ["needs_information": "More information needed", "waiting_for_agent": "Waiting for prepared package",
         "package_received": "Prepared package received", "ready_to_plan": "Ready to review",
         "submitted": "Installation submitted", "installed": "Installed", "canceled": "Canceled",
         "expired": "Expired"][state] ?? "More information needed"
    }
}

private final class SourceDropView: NSView {
    var acceptsSource: (() -> Bool)?
    var selectedSource: ((URL) -> Void)?
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect); registerForDraggedTypes([.fileURL])
    }
    required init?(coder: NSCoder) { super.init(coder: coder); registerForDraggedTypes([.fileURL]) }
    private func source(_ sender: NSDraggingInfo) -> URL? {
        guard acceptsSource?() == true,
              let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
              urls.count == 1 else { return nil }
        let url = urls[0]
        let directory = (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
        return directory || ["md", "txt"].contains(url.pathExtension.lowercased()) ? url : nil
    }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { source(sender) == nil ? [] : .copy }
    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { source(sender) != nil }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let url = source(sender) else { return false }; selectedSource?(url); return true
    }
}

private struct ManagerConnection {
    let endpoint: URL
    let token: String
    let pid: Int32

    init?(_ object: [String: Any]) {
        guard integer(object["api_version"]) == 1,
              let pid = integer(object["supervisor_pid"]), pid > 1, pid <= Int(Int32.max),
              let address = object["endpoint"] as? String, let endpoint = URL(string: address),
              endpoint.scheme == "http", endpoint.host == "127.0.0.1",
              let port = endpoint.port, (1024...65535).contains(port),
              endpoint.user == nil, endpoint.password == nil, endpoint.query == nil, endpoint.fragment == nil,
              endpoint.path.isEmpty || endpoint.path == "/",
              let token = object["token"] as? String, token.count >= 32, token.count <= 512,
              token.unicodeScalars.allSatisfy({ $0.value >= 33 && $0.value <= 126 }) else { return nil }
        self.endpoint = endpoint; self.token = token; self.pid = Int32(pid)
    }
}

private enum PrivateFile {
    static func read(_ path: URL, limit: Int = responseLimit) -> [String: Any]? {
        var directoryInfo = stat()
        guard lstat(path.deletingLastPathComponent().path, &directoryInfo) == 0,
              (directoryInfo.st_mode & S_IFMT) == S_IFDIR, directoryInfo.st_uid == getuid(),
              (directoryInfo.st_mode & 0o777) == 0o700 else { return nil }
        let fd = open(path.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), info.st_nlink == 1, (info.st_mode & 0o777) == 0o600,
              info.st_size > 0, info.st_size <= limit else { return nil }
        var data = Data(count: Int(info.st_size))
        var offset = 0
        let success = data.withUnsafeMutableBytes { buffer -> Bool in
            while offset < buffer.count {
                let count = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count <= 0 { return false }
                offset += count
            }
            return true
        }
        guard success else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    static func write(_ object: [String: Any], to path: URL) {
        let parent = path.deletingLastPathComponent()
        var info = stat()
        guard lstat(parent.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
              info.st_uid == getuid(), (info.st_mode & 0o777) == 0o700,
              let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              data.count <= responseLimit else { return }
        var existing = stat()
        if lstat(path.path, &existing) == 0 && ((existing.st_mode & S_IFMT) != S_IFREG || existing.st_uid != getuid()
            || existing.st_nlink != 1 || (existing.st_mode & 0o777) != 0o600) { return }
        let temporary = parent.appendingPathComponent(".ui-" + UUID().uuidString.lowercased())
        let fd = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return }
        defer { close(fd); unlink(temporary.path) }
        let succeeded = data.withUnsafeBytes { buffer -> Bool in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count <= 0 { return false }
                offset += count
            }
            return true
        }
        if succeeded && fsync(fd) == 0 && rename(temporary.path, path.path) == 0 {
            let directory = open(parent.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            if directory >= 0 { _ = fsync(directory); close(directory) }
        }
    }

    static func export(_ object: [String: Any], to path: URL) -> Bool {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              data.count <= responseLimit else { return false }
        let directory = open(path.deletingLastPathComponent().path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard directory >= 0 else { return false }; defer { close(directory) }
        let name = path.lastPathComponent
        guard !name.isEmpty, name != ".", name != "..", !name.contains("\0") else { return false }
        var existing = stat()
        if fstatat(directory, name, &existing, AT_SYMLINK_NOFOLLOW) == 0 &&
            ((existing.st_mode & S_IFMT) != S_IFREG || existing.st_uid != getuid() || existing.st_nlink != 1) { return false }
        let temporary = ".mcpme-export-" + UUID().uuidString.lowercased()
        let fd = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }
        defer { close(fd); unlinkat(directory, temporary, 0) }
        let written = data.withUnsafeBytes { buffer -> Bool in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count <= 0 { return false }; offset += count
            }
            return true
        }
        guard written, fsync(fd) == 0, renameat(directory, temporary, directory, name) == 0 else { return false }
        _ = fsync(directory); return true
    }
}

final class McpmeApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate, NSTextViewDelegate {
    private var statusItem: NSStatusItem!
    private var process: Process?
    private var generation: UInt64 = 0
    private var quitting = false
    private var stopping = false
    private var pollInFlight = false
    private var timer: Timer?
    private var stopCompletion: (() -> Void)?
    private var hostMessage = "Starting host…"
    private var projects: [Project] = []
    private var operations: [Operation] = []
    private var trackedOperations: Set<String> = []
    private var completedNotices: Set<String> = []
    private var setupPlan: [String: Any]?
    private var setupOperation: String?
    private var setupRequest = false
    private var savedSetup: [String: Any] = [:]
    private var drafts: [ImportDraft] = []
    private var currentDraft: ImportDraft?
    private var draftDirty = false
    private var importsPollInFlight = false
    private var restoringDraft = false
    private var importViewGeneration: UInt64 = 0
    private var window: NSWindow?
    private var table: NSTableView?
    private var renderedProjects: [Project]?
    private var operationText: NSTextField?
    private var hostText: NSTextField?
    private var stopAllButton: NSButton?
    private var selectionActions: [NSButton] = []
    private var removeButton: NSButton?
    private var appPolicyField: NSPopUpButton?
    private var appPolicyHint: NSTextField?
    private var pendingAppRequests: Set<String> = []
    private var pathField: NSTextField?
    private var nameField: NSTextField?
    private var policyField: NSPopUpButton?
    private var startCheck: NSButton?
    private var reviewButton: NSButton?
    private var planDetailsButton: NSButton?
    private var installButton: NSButton?
    private var reviewText: NSTextView?
    private var setupLabel: NSTextField?
    private var chooseButton: NSButton?
    private var draftField: NSPopUpButton?
    private var intentField: NSTextView?
    private var documentRoleField: NSPopUpButton?
    private var newDraftButton: NSButton?
    private var saveDraftButton: NSButton?
    private var copyInstructionsButton: NSButton?
    private var exportDraftButton: NSButton?
    private var attachButton: NSButton?
    private var cancelDraftButton: NSButton?
    private var detailsWindow: NSWindow?
    private let stateDirectory: URL = {
        if let configured = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"], configured.hasPrefix("/") {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/mcpme", isDirectory: true)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let mainMenu = NSMenu()
        let applicationItem = NSMenuItem(title: "mcpme", action: nil, keyEquivalent: "")
        let applicationMenu = NSMenu(title: "mcpme")
        let manage = NSMenuItem(title: "Manage Apps…", action: #selector(showManagement), keyEquivalent: "m")
        manage.target = self; applicationMenu.addItem(manage); applicationMenu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit mcpme", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self; applicationMenu.addItem(quit); applicationItem.submenu = applicationMenu
        mainMenu.addItem(applicationItem); NSApp.mainMenu = mainMenu
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        let editMenu = NSMenu(title: "Edit")
        for (title, selector, key) in [("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"),
                                       ("Paste", #selector(NSText.paste(_:)), "v"), ("Select All", #selector(NSText.selectAll(_:)), "a")] {
            editMenu.addItem(NSMenuItem(title: title, action: selector, keyEquivalent: key))
        }
        editItem.submenu = editMenu; mainMenu.addItem(editItem)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = McpmeBrand.menuBarImage()
        if statusItem.button?.image == nil { statusItem.button?.title = "m" }
        rebuildMenu(); startHost()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if process == nil && !quitting && !stopping { startHost() }
        showManagement(); return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateLater }
        quitting = true; timer?.invalidate(); timer = nil
        stopHost { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let process = process, process.isRunning { process.terminate() }
    }

    private var hostRoot: URL? { Bundle.main.resourceURL?.appendingPathComponent("host", isDirectory: true) }
    private var setupFile: URL { stateDirectory.appendingPathComponent("ui-project-setup.json") }

    private func startHost() {
        guard process == nil, !quitting, !stopping, let host = hostRoot else { return }
        let executable = host.appendingPathComponent("bin/mcpme")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { hostMessage = "Bundled host is unavailable"; rebuildMenu(); return }
        generation &+= 1; hostMessage = "Starting host…"; projects = []; operations = []; pendingAppRequests = []; pollInFlight = false; importsPollInFlight = false
        let service = Process()
        service.executableURL = executable
        service.arguments = ["--state-dir", stateDirectory.path, "serve", "--reaper", host.appendingPathComponent("bin/mcpme-reap").path,
                             "--parent-pid", String(getpid())]
        service.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TMPDIR": NSTemporaryDirectory()]
        service.currentDirectoryURL = host
        service.standardInput = FileHandle.nullDevice; service.standardOutput = FileHandle.nullDevice; service.standardError = FileHandle.nullDevice
        service.terminationHandler = { [weak self, weak service] _ in
            DispatchQueue.main.async {
                guard let self = self, let service = service, self.process === service else { return }
                self.process = nil; self.generation &+= 1; self.pollInFlight = false; self.importsPollInFlight = false
                self.projects = []; self.operations = []; self.pendingAppRequests = []
                let completion = self.stopCompletion; self.stopCompletion = nil; self.stopping = false
                self.hostMessage = "Host stopped. Another host may own this installation."
                self.rebuildMenu(); self.refreshWindow(); completion?()
            }
        }
        process = service
        do { try service.run(); restoreSetup(); poll() }
        catch { process = nil; hostMessage = "Host could not start"; rebuildMenu(); refreshWindow() }
    }

    private func stopHost(completion: @escaping () -> Void) {
        generation &+= 1; pollInFlight = false
        guard let process = process, process.isRunning else { DispatchQueue.main.async(execute: completion); return }
        stopping = true; stopCompletion = completion; hostMessage = "Stopping apps…"; rebuildMenu(); refreshWindow()
        process.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self, weak process] in
            guard let self = self, let process = process, self.process === process, process.isRunning else { return }
            // Only signal the Process object this app launched and still owns.
            kill(process.processIdentifier, SIGKILL)
        }
    }

    private func connection() -> ManagerConnection? {
        guard let process = process, process.isRunning,
              let object = PrivateFile.read(stateDirectory.appendingPathComponent("manager-connection.json")),
              let connection = ManagerConnection(object), connection.pid == process.processIdentifier else { return nil }
        return connection
    }

    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil,
                         completion: @escaping ([String: Any]?, String?) -> Void) {
        guard !quitting, !stopping, let process = process, process.isRunning, let connection = connection() else {
            completion(nil, "host_unavailable"); return
        }
        var request = URLRequest(url: connection.endpoint.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 30
        if path.hasSuffix("/plan") || path.hasSuffix("/attach") || path == "v1/imports" && method == "POST" { request.timeoutInterval = 300 }
        request.setValue("Bearer " + connection.token, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body = body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        let expectedGeneration = generation
        PrivateHTTP.request(request, stateDirectory: stateDirectory, service: "manager") { [weak self] data, response, _ in
            var object: [String: Any]?
            var code = "request_failed"
            if let data = data, data.count <= responseLimit, let parsed = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
               integer(parsed["api_version"]) == 1 {
                if let error = parsed["error"] as? [String: Any] { code = safeCode(error["code"]) ?? "request_failed" }
                else if let response = response, (200...299).contains(response.statusCode) { object = parsed }
            }
            DispatchQueue.main.async {
                guard let self = self, self.generation == expectedGeneration, self.process === process, process.isRunning else { return }
                completion(object, object == nil ? code : nil)
            }
        }
    }

    private func poll() {
        guard !pollInFlight, !quitting, !stopping, connection() != nil else { return }
        pollInFlight = true
        request("v1/status") { [weak self] object, _ in
            guard let self = self else { return }; self.pollInFlight = false
            guard let object = object, integer(object["supervisor_pid"]) == Int(self.process?.processIdentifier ?? 0),
                  let rows = object["slots"] as? [[String: Any]],
                  let operations = object["operations"] as? [[String: Any]] else {
                self.hostMessage = "Waiting for host…"; self.rebuildMenu(); self.refreshWindow(); return
            }
            let parsed = rows.compactMap(Project.init)
            guard parsed.count == rows.count, Set(parsed.map(\.id)).count == parsed.count else { return }
            self.projects = parsed; self.operations = operations.compactMap(Operation.init)
            self.hostMessage = "\(parsed.filter { $0.state == "ready" }.count) ready / \(parsed.count) installed"
            self.rebuildMenu(); self.refreshWindow(); self.pollTrackedOperations(); self.pollImports()
        }
    }

    private func item(_ title: String, action: Selector? = nil, project: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self; item.representedObject = project; item.isEnabled = action != nil
        return item
    }

    private func rebuildMenu() {
        guard statusItem != nil else { return }
        let menu = NSMenu(); menu.autoenablesItems = false
        menu.addItem(item("mcpme: " + hostMessage))
        for project in projects {
            let row = item("\(project.name)    \(project.status)    :\(project.port)")
            let submenu = NSMenu(); submenu.autoenablesItems = false
            let busy = project.blocked || pendingAppRequests.contains(project.id) || operations.contains {
                $0.projectID == project.id && ($0.active || $0.action == "remove" && $0.status == "recovery_required")
            }
            for (title, action) in [("Start", #selector(startProject(_:))), ("Stop", #selector(stopProject(_:))), ("Restart", #selector(restartProject(_:)))] {
                let control = item(title, action: action, project: project.id)
                control.isEnabled = !busy && !stopping && !quitting; submenu.addItem(control)
            }
            submenu.addItem(.separator())
            submenu.addItem(item("Copy Connection Configuration", action: #selector(copyProject(_:)), project: project.id))
            submenu.addItem(item("Details…", action: #selector(detailsProject(_:)), project: project.id))
            row.submenu = submenu; menu.addItem(row)
        }
        menu.addItem(.separator())
        menu.addItem(item("Add App…", action: #selector(addProject)))
        menu.addItem(item("Manage Apps…", action: #selector(showManagement)))
        let stop = item("Stop All", action: #selector(stopAll)); stop.isEnabled = !projects.isEmpty && !stopping; menu.addItem(stop)
        let login = item("Start at Login", action: #selector(toggleLogin)); login.state = SMAppService.mainApp.status == .enabled ? .on : .off; menu.addItem(login)
        if process == nil { menu.addItem(item("Start Host", action: #selector(retryHost))) }
        menu.addItem(.separator()); let quit = item("Quit mcpme", action: #selector(quitApp)); quit.keyEquivalent = "q"; menu.addItem(quit)
        statusItem.menu = menu; statusItem.button?.toolTip = "mcpme: " + hostMessage
    }

    private func control(_ id: String, action: String) {
        guard !pendingAppRequests.contains(id) else { return }
        pendingAppRequests.insert(id); refreshActionButtons()
        request("v1/slots/\(id)/\(action)", method: "POST", body: [:]) { [weak self] object, code in
            guard let self = self else { return }
            self.pendingAppRequests.remove(id); self.refreshActionButtons()
            guard let object = object, let operation = Operation(object) else { self.showError(code ?? "invalid_response"); return }
            self.trackedOperations.insert(operation.id); self.operations.insert(operation, at: 0)
            self.rebuildMenu(); self.refreshWindow(); self.poll()
        }
    }

    @objc private func startProject(_ sender: NSMenuItem) { if let id = sender.representedObject as? String { control(id, action: "start") } }
    @objc private func stopProject(_ sender: NSMenuItem) { if let id = sender.representedObject as? String { control(id, action: "stop") } }
    @objc private func restartProject(_ sender: NSMenuItem) { if let id = sender.representedObject as? String { control(id, action: "restart") } }
    @objc private func stopAll() { for project in projects where !project.blocked { control(project.id, action: "stop") } }
    @objc private func retryHost() { startHost() }
    @objc private func quitApp() { NSApp.terminate(nil) }

    private func pollTrackedOperations() {
        if let id = setupOperation, operations.first(where: { $0.id == id })?.active ?? true { trackedOperations.insert(id) }
        for id in trackedOperations {
            request("v1/operations/" + id) { [weak self] object, _ in
                guard let self = self, let object = object, let operation = Operation(object) else { return }
                self.operations.removeAll { $0.id == id }; self.operations.insert(operation, at: 0)
                if !operation.active {
                    self.trackedOperations.remove(id)
                    if id == self.setupOperation { self.persistSetup(); self.setupLabel?.stringValue = operation.label }
                    if operation.status == "completed", operation.action == "install", let projectID = operation.projectID,
                       !self.completedNotices.contains(id) {
                        self.completedNotices.insert(id); self.setupLabel?.stringValue = "Installed. Select the app to copy its connection configuration."
                        self.table?.selectRowIndexes(IndexSet(integer: self.projects.firstIndex { $0.id == projectID } ?? 0), byExtendingSelection: false)
                    }
                    if operation.action == "remove", !self.completedNotices.contains(id) {
                        self.completedNotices.insert(id)
                        if operation.status == "completed" {
                            self.projects.removeAll { $0.id == operation.projectID }
                            self.rebuildMenu()
                        } else { self.showError(operation.error ?? "removal_failed") }
                    }
                } else if id == self.setupOperation { self.setupLabel?.stringValue = operation.label }
                self.refreshWindow()
            }
        }
    }

    private func button(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action); button.bezelStyle = .rounded; return button
    }

    @objc private func showManagement() {
        if window == nil { makeManagementWindow() }
        window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); refreshWindow(); poll()
    }

    @objc private func addProject() { showManagement(); chooseSource() }

    private func makeManagementWindow() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1020, height: 850),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "mcpme — Manage Apps"; window.minSize = NSSize(width: 940, height: 780)
        window.isReleasedWhenClosed = false; window.delegate = self; window.center(); self.window = window
        let pageScroll = NSScrollView(); pageScroll.hasVerticalScroller = true; pageScroll.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(pageScroll)
        NSLayoutConstraint.activate([pageScroll.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
                                     pageScroll.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
                                     pageScroll.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
                                     pageScroll.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor)])
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .leading; content.spacing = 8
        content.edgeInsets = NSEdgeInsets(top: 18, left: 18, bottom: 18, right: 18)
        content.translatesAutoresizingMaskIntoConstraints = false
        pageScroll.documentView = content
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: pageScroll.contentView.leadingAnchor),
                                     content.topAnchor.constraint(equalTo: pageScroll.contentView.topAnchor),
                                     content.widthAnchor.constraint(equalTo: pageScroll.contentView.widthAnchor),
                                     content.heightAnchor.constraint(greaterThanOrEqualTo: pageScroll.contentView.heightAnchor)])
        let headingRow = NSStackView(); headingRow.orientation = .horizontal; headingRow.spacing = 15
        let brandIcon = NSImageView(image: McpmeBrand.appIcon(size: 128))
        brandIcon.setAccessibilityLabel("mcpme")
        brandIcon.widthAnchor.constraint(equalToConstant: 40).isActive = true
        brandIcon.heightAnchor.constraint(equalToConstant: 40).isActive = true
        headingRow.addArrangedSubview(brandIcon)
        let titles = NSStackView(); titles.orientation = .vertical; titles.alignment = .leading; titles.spacing = 2
        let heading = NSTextField(labelWithString: "mcpme"); heading.font = .systemFont(ofSize: 20, weight: .semibold)
        let subtitle = NSTextField(labelWithString: "Your local apps"); subtitle.font = .systemFont(ofSize: 12)
        subtitle.textColor = .secondaryLabelColor
        titles.addArrangedSubview(heading); titles.addArrangedSubview(subtitle); headingRow.addArrangedSubview(titles)
        let hostText = NSTextField(labelWithString: hostMessage); hostText.textColor = .secondaryLabelColor
        self.hostText = hostText; headingRow.addArrangedSubview(hostText)
        content.addArrangedSubview(headingRow)
        let table = NSTableView(); table.delegate = self; table.dataSource = self; table.rowHeight = 28
        for (name, title, width) in [("name", "App", 330.0), ("status", "Status", 200.0), ("port", "Port", 90.0), ("policy", "Start", 170.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(name)); column.title = title; column.width = width; table.addTableColumn(column)
        }
        let scroll = NSScrollView(); scroll.documentView = table; scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        content.addArrangedSubview(scroll); scroll.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -36).isActive = true
        scroll.heightAnchor.constraint(equalToConstant: 110).isActive = true; self.table = table
        let controls = NSStackView(); controls.orientation = .horizontal; controls.spacing = 8
        for (title, action) in [("Start", #selector(startSelected)), ("Stop", #selector(stopSelected)), ("Restart", #selector(restartSelected)),
                                ("Details", #selector(detailsSelected)), ("Copy Connection", #selector(copySelected))] {
            let actionButton = button(title, action: action); selectionActions.append(actionButton); controls.addArrangedSubview(actionButton)
        }
        let stopAll = button("Stop All", action: #selector(stopAll)); self.stopAllButton = stopAll; controls.addArrangedSubview(stopAll)
        controls.addArrangedSubview(button("Quit mcpme", action: #selector(quitApp))); content.addArrangedSubview(controls)
        let appSettings = NSStackView(); appSettings.orientation = .horizontal; appSettings.spacing = 10
        appSettings.addArrangedSubview(NSTextField(labelWithString: "Startup:"))
        let appPolicy = NSPopUpButton(); appPolicy.addItems(withTitles: ["Manual", "Automatic — when mcpme opens"])
        appPolicy.target = self; appPolicy.action = #selector(changeAppPolicy)
        appPolicy.setAccessibilityLabel("Selected app startup")
        appSettings.addArrangedSubview(appPolicy); self.appPolicyField = appPolicy
        let remove = button("Remove App…", action: #selector(removeSelected)); self.removeButton = remove
        appSettings.addArrangedSubview(remove); content.addArrangedSubview(appSettings)
        let hint = NSTextField(wrappingLabelWithString: "Select an app to change its startup."); hint.font = .systemFont(ofSize: 12)
        hint.textColor = .secondaryLabelColor; content.addArrangedSubview(hint); self.appPolicyHint = hint
        let operationText = NSTextField(wrappingLabelWithString: ""); operationText.font = .systemFont(ofSize: 12)
        content.addArrangedSubview(operationText); self.operationText = operationText
        let divider = NSBox(); divider.boxType = .separator; content.addArrangedSubview(divider)
        divider.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -36).isActive = true
        let addHeading = NSTextField(labelWithString: "Add App"); addHeading.font = .boldSystemFont(ofSize: 18); content.addArrangedSubview(addHeading)
        let draftsRow = NSStackView(); draftsRow.orientation = .horizontal; draftsRow.spacing = 8
        let draftPicker = NSPopUpButton(); draftPicker.addItem(withTitle: "New draft"); draftPicker.target = self; draftPicker.action = #selector(selectDraft)
        draftPicker.setAccessibilityLabel("Saved app draft"); self.draftField = draftPicker; draftsRow.addArrangedSubview(draftPicker)
        draftPicker.widthAnchor.constraint(greaterThanOrEqualToConstant: 460).isActive = true
        let newDraft = button("New Draft", action: #selector(newDraft)); self.newDraftButton = newDraft; draftsRow.addArrangedSubview(newDraft)
        content.addArrangedSubview(draftsRow)
        let drop = SourceDropView(frame: .zero); drop.wantsLayer = true; drop.layer?.borderWidth = 1
        drop.layer?.borderColor = NSColor.separatorColor.cgColor; drop.layer?.cornerRadius = 6
        drop.acceptsSource = { [weak self] in self?.intakeEnabled == true }
        drop.selectedSource = { [weak self] url in self?.beginImport(url) }
        content.addArrangedSubview(drop); drop.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -36).isActive = true
        drop.heightAnchor.constraint(equalToConstant: 46).isActive = true
        let sourceRow = NSStackView(); sourceRow.orientation = .horizontal; sourceRow.spacing = 8
        sourceRow.translatesAutoresizingMaskIntoConstraints = false; drop.addSubview(sourceRow)
        NSLayoutConstraint.activate([sourceRow.leadingAnchor.constraint(equalTo: drop.leadingAnchor, constant: 10),
                                     sourceRow.trailingAnchor.constraint(equalTo: drop.trailingAnchor, constant: -10),
                                     sourceRow.centerYAnchor.constraint(equalTo: drop.centerYAnchor)])
        let path = NSTextField(); path.placeholderString = "Drop a source folder, prepared package, or Markdown/text document"; path.isEditable = false; path.isSelectable = true
        path.setAccessibilityLabel("Selected source")
        self.pathField = path; sourceRow.addArrangedSubview(path)
        let choose = button("Choose Files…", action: #selector(chooseSource)); sourceRow.addArrangedSubview(choose); self.chooseButton = choose
        path.widthAnchor.constraint(greaterThanOrEqualToConstant: 700).isActive = true
        let settings = NSStackView(); settings.orientation = .horizontal; settings.spacing = 10
        let name = NSTextField(); name.placeholderString = "App name"; name.delegate = self; self.nameField = name
        name.setAccessibilityLabel("App name")
        name.widthAnchor.constraint(equalToConstant: 270).isActive = true; settings.addArrangedSubview(name)
        let policy = NSPopUpButton(); policy.addItems(withTitles: ["Start manually", "Automatic — when mcpme opens"]); policy.target = self; policy.action = #selector(setupChanged)
        self.policyField = policy; settings.addArrangedSubview(policy)
        let start = NSButton(checkboxWithTitle: "Start after install", target: self, action: #selector(setupChanged)); self.startCheck = start; settings.addArrangedSubview(start)
        content.addArrangedSubview(settings)
        content.addArrangedSubview(NSTextField(labelWithString: "What should your assistant be able to do?"))
        let intentScroll = NSScrollView(); intentScroll.hasVerticalScroller = true; intentScroll.borderType = .bezelBorder
        let intent = NSTextView(); intent.delegate = self; intent.font = .systemFont(ofSize: 13)
        intent.isRichText = false; intent.isAutomaticQuoteSubstitutionEnabled = false; intent.isAutomaticDashSubstitutionEnabled = false
        intent.isVerticallyResizable = true; intent.isHorizontallyResizable = false; intent.autoresizingMask = [.width]
        intent.textContainer?.widthTracksTextView = true; intent.setAccessibilityLabel("Requested tools and behavior")
        intentScroll.documentView = intent; self.intentField = intent; content.addArrangedSubview(intentScroll)
        intentScroll.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -36).isActive = true
        intentScroll.heightAnchor.constraint(equalToConstant: 48).isActive = true
        let role = NSPopUpButton(); role.addItems(withTitles: ["Choose how to use this document", "Use this information", "Use these instructions"])
        role.target = self; role.action = #selector(setupChanged); role.setAccessibilityLabel("Document role")
        self.documentRoleField = role; content.addArrangedSubview(role)
        let handoff = NSStackView(); handoff.orientation = .horizontal; handoff.spacing = 8
        let save = button("Save Draft", action: #selector(saveDraft)); self.saveDraftButton = save; handoff.addArrangedSubview(save)
        let copy = button("Copy Agent Instructions", action: #selector(copyAgentInstructions)); self.copyInstructionsButton = copy; handoff.addArrangedSubview(copy)
        let export = button("Export Setup Specification…", action: #selector(exportDraft)); self.exportDraftButton = export; handoff.addArrangedSubview(export)
        let attach = button("Import Prepared Package…", action: #selector(choosePackage)); self.attachButton = attach; handoff.addArrangedSubview(attach)
        let cancel = button("Cancel Draft", action: #selector(cancelDraft)); self.cancelDraftButton = cancel; handoff.addArrangedSubview(cancel)
        content.addArrangedSubview(handoff)
        let actions = NSStackView(); actions.orientation = .horizontal; actions.spacing = 8
        let review = button("Review Package", action: #selector(reviewPackage)); self.reviewButton = review; actions.addArrangedSubview(review)
        let install = button("Add App", action: #selector(installPackage)); self.installButton = install; actions.addArrangedSubview(install)
        let details = button("Package and Verification Details…", action: #selector(showPlanDetails)); self.planDetailsButton = details; actions.addArrangedSubview(details)
        content.addArrangedSubview(actions)
        let label = NSTextField(wrappingLabelWithString: ""); self.setupLabel = label; content.addArrangedSubview(label)
        let reviewScroll = NSScrollView(); reviewScroll.hasVerticalScroller = true; reviewScroll.borderType = .bezelBorder
        let text = NSTextView(); text.isEditable = false; text.isSelectable = true; text.font = .systemFont(ofSize: 13)
        text.isVerticallyResizable = true; text.isHorizontallyResizable = false
        text.autoresizingMask = [.width]; text.textContainer?.widthTracksTextView = true
        reviewScroll.documentView = text; self.reviewText = text; content.addArrangedSubview(reviewScroll)
        reviewScroll.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -36).isActive = true
        reviewScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        window.initialFirstResponder = choose
        restoreSetup(); refreshWindow(); pollImports()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { projects.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard projects.indices.contains(row), let id = tableColumn?.identifier.rawValue else { return nil }
        let project = projects[row]
        let value = ["name": project.name, "status": project.status, "port": String(project.port), "policy": project.policyLabel][id] ?? ""
        let label = NSTextField(labelWithString: value); label.lineBreakMode = .byTruncatingTail; return label
    }
    func tableViewSelectionDidChange(_ notification: Notification) { refreshActionButtons() }
    func controlTextDidChange(_ notification: Notification) { setupChanged() }
    func textDidChange(_ notification: Notification) { setupChanged() }
    private var selectedProject: Project? {
        projectForSelection(row: table?.selectedRow ?? -1, rendered: renderedProjects, current: projects)
    }

    private func refreshActionButtons() {
        let selected = selectedProject
        let available = selected != nil && connection() != nil && !stopping && !quitting
        let busy = selected.map { project in
            project.blocked || pendingAppRequests.contains(project.id) || operations.contains { $0.projectID == project.id && $0.active }
        } ?? true
        let removalNeedsRecovery = selected.map { project in
            operations.contains { $0.projectID == project.id && $0.action == "remove" && $0.status == "recovery_required" }
        } ?? false
        for (index, button) in selectionActions.enumerated() {
            button.isEnabled = available && (index >= 3 || !busy && !removalNeedsRecovery)
        }
        removeButton?.isEnabled = available && !busy
        appPolicyField?.isEnabled = available && !busy && !removalNeedsRecovery && selected?.policy != "first_call"
        appPolicyField?.selectItem(at: selected?.policy == "host" ? 1 : 0)
        appPolicyHint?.stringValue = selected == nil ? "Select an app to change its startup." : "Applies the next time mcpme opens. Stop stays in effect until Start."
    }

    private func refreshWindow() {
        hostText?.stringValue = hostMessage
        stopAllButton?.isEnabled = !projects.isEmpty && process?.isRunning == true && !stopping && !quitting
        let selection = selectedProject?.id
        if let table = table, renderedProjects != projects {
            renderedProjects = projects
            table.reloadData()
            if let selection = selection, let index = projects.firstIndex(where: { $0.id == selection }) {
                table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
            } else { table.deselectAll(nil) }
        }
        refreshActionButtons()
        operationText?.stringValue = operations.prefix(3).map(\.label).joined(separator: "   •   ")
        let active = setupOperation.flatMap { id in operations.first { $0.id == id } }?.active ?? (setupOperation != nil)
        let enabled = process?.isRunning == true && connection() != nil && !stopping && !quitting && !setupRequest && !active
        let editable = enabled && (currentDraft?.editable ?? true)
        chooseButton?.isEnabled = intakeEnabled; newDraftButton?.isEnabled = !setupRequest && connection() != nil
        draftField?.isEnabled = !setupRequest && connection() != nil
        nameField?.isEnabled = editable; policyField?.isEnabled = editable; startCheck?.isEnabled = editable
        intentField?.isEditable = editable; documentRoleField?.isEnabled = editable && currentDraft?.isDocument == true
        documentRoleField?.isHidden = currentDraft?.isDocument != true
        saveDraftButton?.isEnabled = editable && currentDraft != nil && draftDirty
        copyInstructionsButton?.isEnabled = editable && currentDraft != nil
        exportDraftButton?.isEnabled = editable && currentDraft != nil
        attachButton?.isEnabled = editable && currentDraft != nil
        cancelDraftButton?.isEnabled = editable && currentDraft != nil
        reviewButton?.isEnabled = editable && (currentDraft?.packagePath != nil || currentDraft == nil && !(pathField?.stringValue.isEmpty ?? true))
        installButton?.isEnabled = enabled && setupPlan != nil && setupOperation == nil && !draftDirty
        planDetailsButton?.isEnabled = setupPlan != nil
        installButton?.title = startCheck?.state == .on ? "Add & Start App" : "Add App"
    }

    private var intakeEnabled: Bool {
        !setupRequest && !stopping && !quitting && connection() != nil
    }

    @objc private func startSelected() { if let project = selectedProject { control(project.id, action: "start") } }
    @objc private func stopSelected() { if let project = selectedProject { control(project.id, action: "stop") } }
    @objc private func restartSelected() { if let project = selectedProject { control(project.id, action: "restart") } }
    @objc private func changeAppPolicy() {
        guard let project = selectedProject, let field = appPolicyField else { return }
        let policy = field.indexOfSelectedItem == 1 ? "host" : "manual"
        guard policy != project.policy, !pendingAppRequests.contains(project.id) else { return }
        pendingAppRequests.insert(project.id); refreshActionButtons()
        request("v1/slots/\(project.id)", method: "PATCH", body: ["start_policy": policy]) { [weak self] object, code in
            guard let self = self else { return }
            self.pendingAppRequests.remove(project.id)
            guard let row = object?["slot"] as? [String: Any], let updated = Project(row), updated.id == project.id else {
                self.refreshActionButtons(); self.showError(code ?? "invalid_response"); return
            }
            if let index = self.projects.firstIndex(where: { $0.id == updated.id }) { self.projects[index] = updated }
            self.rebuildMenu(); self.refreshWindow(); self.poll()
        }
    }
    @objc private func removeSelected() {
        guard let project = selectedProject, !project.blocked, !pendingAppRequests.contains(project.id), let window = window else { return }
        let alert = NSAlert(); alert.alertStyle = .warning
        alert.messageText = "Remove \(project.name)?"
        alert.informativeText = "This stops the app and removes it from mcpme. Its assistant connections will stop working. Source files and saved app data are kept."
        alert.addButton(withTitle: "Remove App"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.control(project.id, action: "remove") }
        }
    }
    @objc private func detailsSelected() { if let project = selectedProject { showDetails(project.id) } }
    @objc private func copySelected() { if let project = selectedProject { copyConfiguration(project.id) } }
    @objc private func detailsProject(_ sender: NSMenuItem) { if let id = sender.representedObject as? String { showDetails(id) } }
    @objc private func copyProject(_ sender: NSMenuItem) { if let id = sender.representedObject as? String { copyConfiguration(id) } }

    private func copyConfiguration(_ id: String) {
        let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/mcpme").path
        let object: [String: Any] = ["mcpServers": [id: ["command": executable, "args": ["--state-dir", stateDirectory.path, "connect", "--slot", id]]]]
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else { showError("configuration_unavailable"); return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
        setupLabel?.stringValue = "Connection configuration copied. Add it to the MCP client you choose."
    }

    private func showDetails(_ id: String) {
        request("v1/slots/\(id)/details") { [weak self] object, code in
            guard let self = self, let object = object else { self?.showError(code ?? "details_unavailable"); return }
            let detail = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 750, height: 620),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            detail.title = "App Details"; detail.isReleasedWhenClosed = false; detail.center()
            let scroll = NSScrollView(frame: detail.contentView!.bounds); scroll.autoresizingMask = [.width, .height]; scroll.hasVerticalScroller = true
            let text = NSTextView(); text.isEditable = false; text.isSelectable = true
            text.font = .monospacedSystemFont(ofSize: 12, weight: .regular); text.autoresizingMask = [.width]
            text.isVerticallyResizable = true; text.textContainer?.widthTracksTextView = true
            text.string = self.reviewDescription(object)
            scroll.documentView = text; detail.contentView!.addSubview(scroll)
            self.detailsWindow?.close(); self.detailsWindow = detail; detail.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        }
    }

    func windowWillClose(_ notification: Notification) {
        if notification.object as? NSWindow === window, draftDirty, currentDraft?.editable == true, !setupRequest {
            saveCurrentDraft { _ in }
        }
    }

    private var selectedPolicy: String {
        policyField?.indexOfSelectedItem == 1 ? "host" : "manual"
    }

    private var selectedDocumentRole: String? {
        switch documentRoleField?.indexOfSelectedItem {
        case 1: return "reference"
        case 2: return "instructions"
        default: return nil
        }
    }

    private func pollImports() {
        guard !importsPollInFlight, !setupRequest, connection() != nil else { return }
        importsPollInFlight = true
        let expectedViewGeneration = importViewGeneration
        request("v1/imports") { [weak self] object, _ in
            guard let self = self else { return }; self.importsPollInFlight = false
            guard let rows = object?["imports"] as? [[String: Any]] else { return }
            let drafts = rows.compactMap(ImportDraft.init)
            guard drafts.count == rows.count, Set(drafts.map(\.id)).count == drafts.count else { return }
            self.drafts = drafts; self.updateDraftPicker()
            let selectedID = self.currentDraft?.id ?? self.savedSetup["import_id"] as? String
            if let id = selectedID, let draft = drafts.first(where: { $0.id == id }), !self.draftDirty, !self.setupRequest,
               expectedViewGeneration == self.importViewGeneration {
                let previous = self.currentDraft
                if previous == nil || previous?.revision != draft.revision || previous?.state != draft.state || previous?.packageDigest != draft.packageDigest || previous?.operationID != draft.operationID {
                    self.request("v1/imports/" + id) { [weak self] object, _ in
                        guard let self = self, !self.draftDirty, !self.setupRequest,
                              expectedViewGeneration == self.importViewGeneration,
                              self.currentDraft?.id == id || (self.currentDraft == nil && self.savedSetup["import_id"] as? String == id),
                              let object = object, let complete = ImportDraft(object), complete.revision >= draft.revision else { return }
                        self.displayDraft(complete)
                    }
                }
            }
            self.refreshWindow()
        }
    }

    private func updateDraftPicker() {
        guard let picker = draftField else { return }
        let selectedID = currentDraft?.id ?? savedSetup["import_id"] as? String
        picker.removeAllItems(); picker.addItem(withTitle: "New draft")
        for draft in drafts {
            picker.addItem(withTitle: draft.name + " — " + draft.label)
            picker.lastItem?.representedObject = draft.id
        }
        if let id = selectedID, let index = drafts.firstIndex(where: { $0.id == id }) { picker.selectItem(at: index + 1) }
    }

    private func displayDraft(_ draft: ImportDraft) {
        restoringDraft = true; defer { restoringDraft = false }
        importViewGeneration &+= 1
        let changed = currentDraft?.id != draft.id || currentDraft?.revision != draft.revision || currentDraft?.packageDigest != draft.packageDigest
        currentDraft = draft; draftDirty = false
        pathField?.stringValue = draft.sourcePath; nameField?.stringValue = draft.name; intentField?.string = draft.intent
        policyField?.selectItem(at: draft.policy == "host" ? 1 : 0)
        startCheck?.state = draft.start ? .on : .off
        documentRoleField?.selectItem(at: draft.documentRole == "reference" ? 1 : draft.documentRole == "instructions" ? 2 : 0)
        if changed { setupPlan = nil; reviewText?.string = "" }
        setupOperation = draft.operationID
        if let operationID = draft.operationID { trackedOperations.insert(operationID) }
        setupLabel?.stringValue = draftStatus(draft)
        updateDraftPicker(); persistSetup(); refreshWindow()
    }

    private func draftStatus(_ draft: ImportDraft) -> String {
        var message = draft.label + "."
        if draft.isDocument && draft.documentRole == "reference" && draft.packagePath == nil {
            message += " Reference document tools need a prepared document service."
        }
        if !draft.blockers.isEmpty {
            message += " " + draft.blockers.map { code in
                ["intent_required": "Describe the tools you want.", "document_role_required": "Choose how to use this document.",
                 "needs_adapter": "A compatible adapter is required.", "package_required": "Return a prepared package to continue.",
                 "prepared_package_required": "Return a prepared package to continue.",
                 "document_template_required": "Reference document tools need a prepared document service.",
                 "unsupported_process_topology": "The package needs a supported process layout.",
                 "unsupported_profile": "The selected runtime profile is unavailable.",
                 "document_service_unavailable": "Reference document tools need a prepared document service."][code] ?? "Resolve " + code.replacingOccurrences(of: "_", with: " ") + "."
            }.joined(separator: " ")
        }
        return message
    }

    private func draftError(_ code: String) -> String {
        ["stale_revision": "This draft changed. Resume it from the draft list, then try again.",
         "stale_import_revision": "This draft changed. Resume it from the draft list, then try again.",
         "draft_revision_conflict": "This draft changed. Resume it from the draft list, then try again.",
         "operation_in_progress": "Installation is running. Follow its operation status.",
         "draft_finalized": "This draft was already submitted. Create a new draft for another installation.",
         "unsupported_source": "Choose a source folder, prepared package, or Markdown/text document.",
         "source_limit_exceeded": "The selected source exceeds inspection limits. Choose a smaller source folder.",
         "source_size_exceeded": "The selected source exceeds inspection limits. Choose a smaller source folder.",
         "source_identity_mismatch": "The source changed during inspection. Choose it again to create a fresh draft.",
         "import_revision_required": "Resume this draft from the draft list to refresh its revision.",
         "intent_required": "Describe what your assistant should be able to do.",
         "document_role_required": "Choose how to use this document."][code] ?? "The request could not complete (\(safeCode(code) ?? "request_failed"))."
    }

    @objc private func selectDraft() {
        let id = draftField?.selectedItem?.representedObject as? String
        saveCurrentDraft { [weak self] saved in
            guard let self = self, saved else { return }
            guard let id = id else { self.clearDraft(); return }
            self.setupRequest = true; self.refreshWindow()
            self.request("v1/imports/" + id) { [weak self] object, code in
                guard let self = self else { return }; self.setupRequest = false
                guard let object = object, let draft = ImportDraft(object) else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); return
                }
                self.displayDraft(draft)
            }
        }
    }

    @objc private func newDraft() { saveCurrentDraft { [weak self] saved in if saved { self?.clearDraft() } } }

    private func clearDraft() {
        importViewGeneration &+= 1
        currentDraft = nil; draftDirty = false; setupPlan = nil; setupOperation = nil
        pathField?.stringValue = ""; nameField?.stringValue = ""; intentField?.string = ""
        policyField?.selectItem(at: 0); startCheck?.state = .off; documentRoleField?.selectItem(at: 0)
        reviewText?.string = ""; setupLabel?.stringValue = "Choose a source to begin."
        savedSetup.removeValue(forKey: "import_id"); updateDraftPicker(); persistSetup(); refreshWindow()
        window?.makeFirstResponder(chooseButton)
    }

    @objc private func chooseSource() {
        guard intakeEnabled, let window = window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [UTType(filenameExtension: "md")!, .plainText]
        panel.message = "Choose a source folder, prepared package, or Markdown/text document"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }; self?.beginImport(url)
        }
    }

    private func beginImport(_ source: URL) {
        guard intakeEnabled else { return }
        saveCurrentDraft { [weak self] saved in
            guard let self = self, saved else { return }
            let directory = (try? source.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
            let name = directory ? source.lastPathComponent : source.deletingPathExtension().lastPathComponent
            self.setupRequest = true; self.setupLabel?.stringValue = "Inspecting selected source…"; self.refreshWindow()
            self.request("v1/imports", method: "POST", body: ["source": source.path, "name": name, "start": false, "start_policy": "manual"]) { [weak self] object, code in
                guard let self = self else { return }; self.setupRequest = false
                guard let object = object, let draft = ImportDraft(object) else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); return
                }
                self.drafts.insert(draft, at: 0); self.displayDraft(draft)
                if draft.packagePath == nil { self.window?.makeFirstResponder(draft.isDocument ? self.documentRoleField : self.intentField) }
            }
        }
    }

    @objc private func choosePackage() {
        guard currentDraft?.editable == true, !setupRequest, let window = window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.message = "Choose the prepared package for this draft"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            self?.saveCurrentDraft { [weak self] saved in
                guard let self = self, saved, let draft = self.currentDraft else { return }
                self.setupRequest = true; self.setupLabel?.stringValue = "Verifying prepared package…"; self.refreshWindow()
                self.request("v1/imports/\(draft.id)/attach", method: "POST", body: ["revision": draft.revision, "package": url.path]) { [weak self] object, code in
                    guard let self = self else { return }; self.setupRequest = false
                    guard let object = object, let draft = ImportDraft(object) else {
                        self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); return
                    }
                    self.displayDraft(draft); self.pollImports()
                }
            }
        }
    }

    @objc private func setupChanged() {
        guard !restoringDraft, currentDraft?.editable ?? true else { return }
        importViewGeneration &+= 1
        setupPlan = nil; setupOperation = nil; reviewText?.string = ""
        if let draft = currentDraft {
            draftDirty = hasDraftChanges(draft)
            setupLabel?.stringValue = draftDirty ? "Unsaved draft changes." : draftStatus(draft)
        }
        else { setupLabel?.stringValue = "Choose a source to begin." }
        persistSetup(); refreshWindow()
    }

    @objc private func saveDraft() { saveCurrentDraft { _ in } }

    private func hasDraftChanges(_ draft: ImportDraft) -> Bool {
        (nameField?.stringValue ?? draft.name) != draft.name || (intentField?.string ?? draft.intent) != draft.intent ||
        selectedPolicy != draft.policy || (startCheck?.state == .on) != draft.start ||
        draft.isDocument && selectedDocumentRole != draft.documentRole
    }

    private func saveCurrentDraft(completion: @escaping (Bool) -> Void) {
        guard draftDirty else { completion(true); return }
        guard !setupRequest, let draft = currentDraft, draft.editable else { completion(false); return }
        guard hasDraftChanges(draft) else { draftDirty = false; completion(true); return }
        var payload: [String: Any] = ["revision": draft.revision, "name": nameField?.stringValue ?? draft.name,
                                      "intent": intentField?.string ?? draft.intent, "start": startCheck?.state == .on, "start_policy": selectedPolicy]
        if draft.isDocument {
            if let role = selectedDocumentRole { payload["document_role"] = role }
            else { payload["document_role"] = NSNull() }
        }
        setupRequest = true; setupLabel?.stringValue = "Saving draft…"; refreshWindow()
        request("v1/imports/\(draft.id)/revise", method: "POST", body: payload) { [weak self] object, code in
            guard let self = self else { return }; self.setupRequest = false
            guard let object = object, let updated = ImportDraft(object) else {
                self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); completion(false); return
            }
            self.displayDraft(updated)
            if draft.sourceKind == "prepared_package", draft.packagePath != nil, updated.packagePath == nil,
               let package = draft.sourceSnapshotPath ?? draft.packagePath {
                self.setupRequest = true; self.setupLabel?.stringValue = "Verifying prepared package for the saved draft…"; self.refreshWindow()
                self.request("v1/imports/\(updated.id)/attach", method: "POST", body: ["revision": updated.revision, "package": package]) { [weak self] object, code in
                    guard let self = self else { return }; self.setupRequest = false
                    guard let object = object, let attached = ImportDraft(object) else {
                        self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); completion(false); return
                    }
                    self.displayDraft(attached); completion(true)
                }
            } else { completion(true) }
        }
    }

    private func exportedDraft(completion: @escaping ([String: Any]) -> Void) {
        saveCurrentDraft { [weak self] saved in
            guard let self = self, saved, let draft = self.currentDraft, draft.editable else { return }
            self.setupRequest = true; self.refreshWindow()
            self.request("v1/imports/\(draft.id)/export", method: "POST", body: ["revision": draft.revision]) { [weak self] object, code in
                guard let self = self else { return }; self.setupRequest = false
                guard let object = object else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "export_unavailable"); self.refreshWindow(); return
                }
                completion(object); self.refreshWindow()
            }
        }
    }

    @objc private func copyAgentInstructions() {
        exportedDraft { [weak self] object in
            guard let text = object["agent_instructions"] as? String, !text.isEmpty else {
                self?.setupLabel?.stringValue = "Agent instructions are unavailable."; return
            }
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
            self?.setupLabel?.stringValue = self?.currentDraft?.packagePath == nil ? "Instructions copied. Waiting for prepared package." : "Instructions copied."
        }
    }

    @objc private func exportDraft() {
        exportedDraft { [weak self] object in
            guard let self = self, let window = self.window else { return }
            let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "mcpme-setup.json"
            panel.beginSheetModal(for: window) { [weak self] response in
                guard response == .OK, let url = panel.url else { return }
                if PrivateFile.export(object, to: url) {
                    self?.setupLabel?.stringValue = self?.currentDraft?.packagePath == nil ? "Setup specification exported. Waiting for prepared package." : "Setup specification exported."
                } else { self?.setupLabel?.stringValue = "The setup specification could not be saved." }
            }
        }
    }

    @objc private func cancelDraft() {
        guard let draft = currentDraft, draft.editable, !setupRequest else { return }
        setupRequest = true; refreshWindow()
        request("v1/imports/\(draft.id)/cancel", method: "POST", body: ["revision": draft.revision]) { [weak self] object, code in
            guard let self = self else { return }; self.setupRequest = false
            guard let object = object, let canceled = ImportDraft(object) else {
                self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); return
            }
            self.displayDraft(canceled); self.setupLabel?.stringValue = "Draft canceled. External agent work continues independently."
        }
    }

    @objc private func reviewPackage() {
        saveCurrentDraft { [weak self] saved in
            guard let self = self, saved else { return }
            var payload: [String: Any]
            if let draft = self.currentDraft {
                guard draft.editable, draft.packagePath != nil else { return }
                payload = ["import_id": draft.id, "revision": draft.revision]
            } else {
                guard let path = self.pathField?.stringValue, !path.isEmpty else { return }
                payload = ["package": path, "start_policy": self.selectedPolicy, "start": self.startCheck?.state == .on]
                if let name = self.nameField?.stringValue, !name.isEmpty { payload["name"] = name }
            }
            self.setupRequest = true; self.setupLabel?.stringValue = "Verifying prepared artifacts…"; self.refreshWindow()
            self.request("v1/install/plan", method: "POST", body: payload) { [weak self] object, code in
                guard let self = self else { return }; self.setupRequest = false
                guard let object = object, let id = object["plan_id"] as? String, validUUID(id), object["package"] is [String: Any] else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "invalid_plan"); self.refreshWindow(); return
                }
                self.setupPlan = object; self.setupOperation = nil; self.reviewText?.string = self.reviewSummary(object)
                self.setupLabel?.stringValue = "Review the tools and declared access. Add App applies this exact plan."
                self.persistSetup(); self.refreshWindow(); self.pollImports()
            }
        }
    }

    @objc private func installPackage() {
        guard let id = setupPlan?["plan_id"] as? String, validUUID(id) else { return }
        setupRequest = true; refreshWindow()
        request("v1/install/apply", method: "POST", body: ["plan_id": id]) { [weak self] object, code in
            guard let self = self else { return }; self.setupRequest = false
            guard let object = object, let operation = Operation(object) else {
                self.setupLabel?.stringValue = "Install request failed: " + (code ?? "invalid_response"); self.refreshWindow(); return
            }
            self.setupOperation = operation.id; self.trackedOperations.insert(operation.id); self.operations.insert(operation, at: 0)
            self.setupLabel?.stringValue = operation.label; self.persistSetup(); self.refreshWindow(); self.poll()
        }
    }

    private func reviewDescription(_ object: [String: Any]) -> String {
        var sections: [String] = []
        if let name = object["name"] as? String { sections.append("App: " + name) }
        if let slot = object["slot"] as? [String: Any] { sections.append("App state\n" + pretty(slot)) }
        if let port = integer(object["public_port"]) { sections.append("Public endpoint: http://127.0.0.1:\(port)/mcp") }
        if let bytes = integer(object["installed_bytes"]) { sections.append("Installed artifacts: \(bytes) bytes") }
        if let package = object["package"] as? [String: Any] {
            let identity = ["package_id", "version", "source_revision", "profile", "platform", "licenses", "model", "engine"].reduce(into: [String: Any]()) {
                if let value = package[$1] { $0[$1] = value }
            }
            sections.append("Identity and licenses\n" + pretty(identity))
            if let entry = package["entrypoint"] { sections.append("Executable entrypoint\n" + pretty(entry)) }
            if let processes = package["processes"] { sections.append("Managed processes\n" + pretty(processes)) }
            if let resources = package["resources"] { sections.append("Declared resource estimates\n" + pretty(resources)) }
            if let access = package["access"] { sections.append("Declared access (same-user code; no OS sandbox)\n" + pretty(access)) }
            if let tools = package["tools"] { sections.append("MCP tools and schemas\n" + pretty(tools)) }
        }
        if let digest = object["package_digest"] as? String { sections.append("Package digest: " + digest) }
        if let policy = object["start_policy"] as? String { sections.append("Start policy: " + policy) }
        return sections.joined(separator: "\n\n")
    }

    private func reviewSummary(_ object: [String: Any]) -> String {
        var sections: [String] = []
        if let name = object["name"] as? String { sections.append("App: " + name) }
        if let draft = currentDraft {
            sections.append("Source: " + draft.sourcePath)
            if !draft.intent.isEmpty { sections.append("Requested capabilities\n" + draft.intent) }
        }
        if let package = object["package"] as? [String: Any] {
            if let tools = package["tools"] as? [[String: Any]] {
                sections.append("Tools your assistant will receive\n" + tools.map {
                    ($0["name"] as? String ?? "Tool") + (($0["description"] as? String).map { " — " + $0 } ?? "")
                }.joined(separator: "\n"))
            }
            if let access = package["access"] { sections.append("Declared access and side effects\n" + pretty(access) + "\nRuns with your account permissions.") }
            if let resources = package["resources"] { sections.append("Declared resource estimates\n" + pretty(resources)) }
        }
        sections.append("Start after installation: " + ((object["start"] as? Bool ?? currentDraft?.start ?? false) ? "Yes" : "No"))
        let policy = object["start_policy"] as? String ?? currentDraft?.policy ?? "manual"
        sections.append("Later sessions: " + (policy == "host" ? "Automatic — when mcpme opens" : "Start manually"))
        return sections.joined(separator: "\n\n")
    }

    @objc private func showPlanDetails() {
        guard let plan = setupPlan else { return }
        let detail = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 750, height: 620),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        detail.title = "Package and Verification Details"; detail.isReleasedWhenClosed = false; detail.center()
        let scroll = NSScrollView(frame: detail.contentView!.bounds); scroll.autoresizingMask = [.width, .height]; scroll.hasVerticalScroller = true
        let text = NSTextView(); text.isEditable = false; text.isSelectable = true
        text.font = .monospacedSystemFont(ofSize: 12, weight: .regular); text.autoresizingMask = [.width]
        text.isVerticallyResizable = true; text.textContainer?.widthTracksTextView = true; text.string = reviewDescription(plan)
        scroll.documentView = text; detail.contentView!.addSubview(scroll)
        detailsWindow?.close(); detailsWindow = detail; detail.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    private func pretty(_ object: Any) -> String {
        guard JSONSerialization.isValidJSONObject(object), let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else { return "Unavailable" }; return text
    }

    private func persistSetup() {
        var object: [String: Any] = ["schema_version": 1,
                                    "package": pathField?.stringValue ?? savedSetup["package"] as? String ?? "",
                                    "name": nameField?.stringValue ?? savedSetup["name"] as? String ?? "",
                                    "start_policy": policyField.map { $0.indexOfSelectedItem == 1 ? "host" : "manual" } ?? savedSetup["start_policy"] as? String ?? "manual",
                                    "start": startCheck.map { $0.state == .on } ?? savedSetup["start"] as? Bool ?? false]
        if let draft = currentDraft { object["import_id"] = draft.id; object["import_revision"] = draft.revision }
        else if let id = savedSetup["import_id"] as? String, validUUID(id) {
            object["import_id"] = id
            if let revision = integer(savedSetup["import_revision"]) { object["import_revision"] = revision }
        }
        if let plan = setupPlan { object["plan"] = plan }
        if let operation = setupOperation { object["operation_id"] = operation }
        PrivateFile.write(object, to: setupFile)
        savedSetup = object
    }

    private func restoreSetup() {
        guard let object = PrivateFile.read(setupFile), integer(object["schema_version"]) == 1 else { return }
        savedSetup = object
        pathField?.stringValue = object["package"] as? String ?? ""; nameField?.stringValue = object["name"] as? String ?? ""
        policyField?.selectItem(at: object["start_policy"] as? String == "host" ? 1 : 0)
        startCheck?.state = object["start"] as? Bool == true ? .on : .off
        if let plan = object["plan"] as? [String: Any], let id = plan["plan_id"] as? String, validUUID(id) {
            setupPlan = plan; reviewText?.string = reviewSummary(plan)
        }
        if let id = object["operation_id"] as? String, validUUID(id) { setupOperation = id; trackedOperations.insert(id) }
    }

    @objc private func toggleLogin() {
        do {
            switch SMAppService.mainApp.status {
            case .enabled: try SMAppService.mainApp.unregister()
            case .requiresApproval: SMAppService.openSystemSettingsLoginItems()
            default: try SMAppService.mainApp.register()
            }
            if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
        } catch { showError("login_item_update_failed") }
        rebuildMenu()
    }

    private func showError(_ code: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert(); alert.messageText = "mcpme"; alert.informativeText = "The request could not complete (\(safeCode(code) ?? "request_failed")). Check app status."
        alert.addButton(withTitle: "OK"); alert.runModal()
    }
}

private func nativeSelfTest() -> Bool {
    let id = "11111111-2222-4333-8444-555555555555"
    let good: [String: Any] = ["slot_id": id, "name": "Synthetic", "observed_state": "ready", "public_port": 19386, "start_policy": "manual", "generation": 1]
    guard Project(good)?.id == id, integer(true) == nil, safeCode("private arbitrary text") == nil,
          ManagerConnection(["api_version": 1, "supervisor_pid": 42, "endpoint": "http://127.0.0.1:19384", "token": String(repeating: "x", count: 32)]) != nil,
          ManagerConnection(["api_version": 1, "supervisor_pid": 42, "endpoint": "http://example.com:19384", "token": String(repeating: "x", count: 32)]) == nil else { return false }
    var secondRow = good; secondRow["slot_id"] = "22222222-2222-4333-8444-555555555555"
    var thirdRow = good; thirdRow["slot_id"] = "33333333-2222-4333-8444-555555555555"
    guard let first = Project(good), let second = Project(secondRow), let third = Project(thirdRow) else { return false }
    let displayed = [first, second, third]
    // The selected identity survives removal of a preceding row; deleting the
    // selected identity clears selection rather than targeting its successor.
    guard projectForSelection(row: 1, rendered: displayed, current: [second, third]) == second,
          projectForSelection(row: 1, rendered: displayed, current: [first, third]) == nil,
          projectForSelection(row: -1, rendered: displayed, current: displayed) == nil else { return false }
    return true
}

private func legacyHandoff(_ arguments: [String]) -> Int32? {
    guard let command = arguments.first, ["--legacy-status", "--legacy-stop"].contains(command) else { return nil }
    let bundleID = "com.jshahbazi.jevk5"
    let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).filter { !$0.isTerminated }
    var result: [String: Any] = ["api_version": 1, "bundle_id": bundleID]
    var code: Int32 = 0
    if command == "--legacy-status", arguments.count == 1 {
        result["running"] = running.map { ["pid": Int($0.processIdentifier), "bundle_path": $0.bundleURL?.path ?? ""] as [String: Any] }
        result["login_status"] = "unknown"
    } else if command == "--legacy-stop", arguments.count == 5, arguments[1] == "--pid", arguments[3] == "--bundle-path",
              let pid = Int32(arguments[2]), pid > 1 {
        let expectedPath = arguments[4]
        if let target = running.first(where: { $0.processIdentifier == pid && $0.bundleURL?.path == expectedPath }),
           target.bundleIdentifier == bundleID, target.terminate() {
            let deadline = Date().addingTimeInterval(20)
            while !target.isTerminated && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
            if target.isTerminated { result["stopped"] = true }
            else { result["error"] = ["code": "legacy_stop_timeout"]; code = 2 }
        } else { result["error"] = ["code": "legacy_owner_mismatch"]; code = 2 }
    } else { result["error"] = ["code": "invalid_legacy_handoff"]; code = 2 }
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), let text = String(data: data, encoding: .utf8) { print(text) }
    return code
}

private func hostLogin(_ arguments: [String]) -> Int32? {
    guard arguments.count == 1, let action = arguments.first,
          ["--host-login-status", "--host-login-enable", "--host-login-disable"].contains(action) else { return nil }
    var result: [String: Any] = ["api_version": 1]
    var code: Int32 = 0
    do {
        guard Bundle.main.bundleIdentifier == "com.jshahbazi.mcpme" else { throw NSError(domain: "handoff", code: 1) }
        if action == "--host-login-enable" && SMAppService.mainApp.status != .enabled { try SMAppService.mainApp.register() }
        if action == "--host-login-disable" && (SMAppService.mainApp.status == .enabled || SMAppService.mainApp.status == .requiresApproval) {
            try SMAppService.mainApp.unregister()
        }
        switch SMAppService.mainApp.status {
        case .enabled: result["login_status"] = "enabled"
        case .notRegistered: result["login_status"] = "not_registered"
        case .requiresApproval: result["login_status"] = "requires_approval"
        case .notFound: result["login_status"] = "not_found"
        @unknown default: result["login_status"] = "unknown"
        }
    } catch { result["error"] = ["code": "host_login_update_failed"]; code = 2 }
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), let text = String(data: data, encoding: .utf8) { print(text) }
    return code
}

if CommandLine.arguments.dropFirst().first == "--native-self-test" {
    let passed = nativeSelfTest(); print(passed ? "native_contracts_passed" : "native_contracts_failed"); exit(passed ? 0 : 1)
}

var cliArguments = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
if let code = legacyHandoff(cliArguments) { exit(code) }
if let code = hostLogin(cliArguments) { exit(code) }
if cliArguments.first == "--ui-state-dir" {
    guard cliArguments.count == 2, cliArguments[1].hasPrefix("/"), !cliArguments[1].contains("\0") else { exit(2) }
    setenv("MCPME_STATE_DIR", cliArguments[1], 1); cliArguments = []
}
if !cliArguments.isEmpty {
    guard let executable = Bundle.main.resourceURL?.appendingPathComponent("host/bin/mcpme").path else { exit(2) }
    let values = [executable] + cliArguments
    let pointers = values.map { strdup($0) } + [nil]
    pointers.withUnsafeBufferPointer { pointer in _ = execv(executable, pointer.baseAddress!) }
    for pointer in pointers { if let pointer = pointer { free(pointer) } }
    exit(2)
}

let application = NSApplication.shared
let delegate = McpmeApp()
application.delegate = delegate
application.run()
