import AppKit
import Foundation
import ServiceManagement
import Darwin
import CoreFoundation
import UniformTypeIdentifiers
import PrivateTransport
import AppInstallation

private let responseLimit = 1_048_576
private let managePreviewMode = CommandLine.arguments.contains("--manage-preview") ||
    Bundle.main.object(forInfoDictionaryKey: "McpmeManagePreview") as? Bool == true
private let uiPreviewMode = managePreviewMode || CommandLine.arguments.contains("--ui-preview") ||
    Bundle.main.object(forInfoDictionaryKey: "McpmeUIPreview") as? Bool == true

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

private func fallbackConnectionName(_ name: String, id: String) -> String {
    var result = ""
    var separator = false
    for scalar in name.lowercased().unicodeScalars {
        let value = scalar.value
        if (48...57).contains(value) || (97...122).contains(value) {
            result.unicodeScalars.append(scalar); separator = false
        } else if !result.isEmpty {
            separator = true
        }
        if separator && !result.hasSuffix("-") { result.append("-") }
    }
    while result.hasSuffix("-") { result.removeLast() }
    if result.count > 64 { result = String(result.prefix(64)).trimmingCharacters(in: CharacterSet(charactersIn: "-")) }
    return result.isEmpty ? "app-" + id : result
}

private func connectionConfiguration(name: String, id: String, executable: String, stateDirectory: String) -> [String: Any] {
    ["mcpServers": [name: ["command": executable,
                              "args": ["--state-dir", stateDirectory, "connect", "--slot", id]]]]
}

private func finiteNumber(_ object: Any?) -> Double? {
    guard let value = object as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
          value.doubleValue.isFinite, value.doubleValue >= 0, value.doubleValue < Double(Int.max) else { return nil }
    return value.doubleValue
}

private func activityDate(_ object: Any?) -> Double? {
    guard let value = object as? String, value.count <= 40 else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) { return date.timeIntervalSince1970 }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)?.timeIntervalSince1970
}

private func parseActivity(_ object: Any?) -> DashboardActivity? {
    guard let object = object as? [String: Any],
          object["available"] as? Bool != false,
          let received = integer(object["received"]), let succeeded = integer(object["succeeded"]),
          let failed = integer(object["failed"]), let inFlight = integer(object["in_flight"]),
          let lastMinute = integer(object["requests_last_minute"]),
          let startedAt = activityDate(object["started_at"]),
          succeeded <= received, failed <= received - succeeded,
          inFlight == received - succeeded - failed, lastMinute <= received else { return nil }
    return DashboardActivity(received: received, succeeded: succeeded, failed: failed, inFlight: inFlight,
                             lastMinute: lastMinute, lastLatencyMS: finiteNumber(object["last_latency_ms"]),
                             lastRequestAt: activityDate(object["last_request_at"]), startedAt: startedAt)
}

private struct Project: Equatable {
    let id: String
    let name: String
    let connectionName: String
    let state: String
    let port: Int
    let policy: String
    let generation: Int
    let error: String?
    let activity: DashboardActivity?

    init?(_ object: [String: Any]) {
        guard let id = object["slot_id"] as? String, validUUID(id),
              let name = object["name"] as? String, !name.isEmpty, name.count <= 256,
              !name.unicodeScalars.contains(where: { $0.value < 32 }),
              let state = object["observed_state"] as? String,
              ["stopped", "starting", "loading", "warming_up", "ready", "stopping", "draining", "failed", "ownership_lost", "updating"].contains(state),
              let port = integer(object["public_port"]), (1024...65535).contains(port),
              let policy = object["start_policy"] as? String, ["manual", "host", "first_call"].contains(policy),
              let generation = integer(object["generation"]), generation > 0 else { return nil }
        let suppliedConnectionName = object["connection_name"] as? String
        self.id = id; self.name = name
        self.connectionName = suppliedConnectionName?.range(of: "^[a-z0-9][a-z0-9-]{0,63}$", options: .regularExpression) != nil
            ? suppliedConnectionName! : fallbackConnectionName(name, id: id)
        self.state = state; self.port = port
        self.policy = policy; self.generation = generation; self.error = safeCode(object["error_code"])
        self.activity = parseActivity(object["activity"])
    }

    var status: String {
        ["stopped": "Stopped", "starting": "Starting", "loading": "Loading", "warming_up": "Warming up",
         "ready": "Ready", "stopping": "Stopping", "draining": "Stopping", "failed": "Failed",
         "ownership_lost": "Reconciliation required", "updating": "Updating"][state] ?? "Unavailable"
    }
    var policyLabel: String { ["manual": "Manual", "host": "Automatic", "first_call": "On first call"][policy] ?? "Manual" }
    var blocked: Bool { ["ownership_lost", "updating"].contains(state) || ["installation_recovery_required", "removal_recovery_required", "upgrade_recovery_required"].contains(error ?? "") }
}

private func managementRequestTimeout(_ path: String, method: String) -> TimeInterval {
    let preparesFiles = path == "v1/imports" || path == "v1/preparations/plan-form" || path == "v1/agent-preparations/plans" || path == "v1/agent-preparations/start" ||
        ["/plan", "/attach", "/engine"].contains { path.hasSuffix($0) }
    return method == "POST" && preparesFiles ? 300 : 30
}

private struct Operation {
    let id: String
    let projectID: String?
    let action: String
    let status: String
    let error: String?
    let stage: String?
    let exposed: Bool

    init?(_ object: [String: Any]) {
        guard let id = object["operation_id"] as? String, validUUID(id),
              let action = object["action"] as? String,
              ["install", "upgrade", "start", "stop", "restart", "remove", "recover", "migrate"].contains(action),
              let status = object["status"] as? String,
              ["pending", "running", "completed", "failed", "canceled", "recovery_required"].contains(status) else { return nil }
        self.id = id; self.action = action; self.status = status
        let projectID = object["slot_id"] as? String
        if let projectID = projectID, !validUUID(projectID) { return nil }
        self.projectID = projectID; self.error = safeCode(object["error_code"])
        let details = object["details"] as? [String: Any] ?? [:]
        self.stage = safeCode(details["stage"])
        self.exposed = details["exposed"] as? Bool == true || details["exposure_committed"] as? Bool == true
    }
    var active: Bool { ["pending", "running"].contains(status) }
    var canStopExposedRecovery: Bool {
        ["install", "upgrade"].contains(action) && status == "recovery_required" && exposed && projectID != nil
    }
    var recoveryError: String? {
        guard status == "recovery_required" else { return nil }
        return ["install": "installation_recovery_required", "upgrade": "upgrade_recovery_required",
                "remove": "removal_recovery_required"][action]
    }
    var label: String {
        let state = ["pending": "Pending", "running": "Running", "completed": "Completed", "failed": "Failed",
                     "canceled": "Canceled", "recovery_required": "Recovery required"][status] ?? "Unavailable"
        return action.capitalized + ": " + state + (stage.map { " · " + $0.replacingOccurrences(of: "_", with: " ") } ?? "") + (error.map { " (\($0))" } ?? "")
    }
}

private func projectCanControl(_ project: Project, action: String, operations: [Operation], recoveryOperationID: String? = nil) -> Bool {
    guard ["start", "stop", "restart", "remove"].contains(action),
          project.state != "ownership_lost", project.error != "ownership_lost" else { return false }
    let related = operations.filter { $0.projectID == project.id }
    guard !related.contains(where: { $0.active }) else { return false }
    let recoveries = related.filter { $0.recoveryError != nil }
    if let recoveryOperationID {
        return action == "stop" && recoveries.count == 1 && recoveries.first?.id == recoveryOperationID
            && recoveries.first?.canStopExposedRecovery == true
    }
    return !project.blocked && recoveries.isEmpty
}

private struct ImportDraft {
    let id: String
    let revision: Int
    let state: String
    let name: String
    let sourcePath: String
    let sourceKind: String
    let sourceRevision: String?
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
    let requirements: [String: Any]
    let attachment: [String: Any]?

    init?(_ object: [String: Any]) {
        let summary = object["summary"] as? Bool == true
        guard let id = object["draft_id"] as? String, validUUID(id),
              let revision = integer(object["revision"]), revision > 0,
              let state = safeCode(object["state"]),
              let name = object["name"] as? String, !name.isEmpty, name.count <= 256,
              !name.unicodeScalars.contains(where: { $0.value < 32 }),
              let source = object["source"] as? [String: Any],
              let kind = safeCode(source["kind"]),
              let path = source["locator"] as? String ?? ((summary || kind == "captured_artifact_collection") ? "" : nil),
              (summary || path.hasPrefix("/") || (kind == "captured_artifact_collection" && path.isEmpty)),
              !path.contains("\0"),
              let policy = object["start_policy"] as? String, ["manual", "host"].contains(policy),
              let start = object["start"] as? Bool else { return nil }
        let intent = object["intent"] as? String ?? ""
        guard intent.count <= 16384, !intent.contains("\0") else { return nil }
        self.id = id; self.revision = revision; self.state = state; self.name = name
        self.sourcePath = path; self.sourceKind = kind; self.intent = intent
        self.sourceRevision = preparationText(source["revision"], maximum: 256)
        let snapshot = source["snapshot_path"] as? String
        self.sourceSnapshotPath = snapshot?.hasPrefix("/") == true ? snapshot : nil
        self.documentRole = safeCode(object["document_role"]); self.start = start; self.policy = policy
        self.blockers = (object["blockers"] as? [[String: Any]] ?? []).compactMap { safeCode($0["code"]) }
        let attachment = object["attachment"] as? [String: Any]
        self.attachment = attachment; self.requirements = object["requirements"] as? [String: Any] ?? [:]
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
        return directory || ["md", "txt", "gguf"].contains(url.pathExtension.lowercased()) ? url : nil
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

final class McpmeApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTextFieldDelegate, NSTextViewDelegate {
    private var statusItem: NSStatusItem!
    private var process: Process?
    // Assigned only by the explicit native acceptance command. The normal app
    // continues to require its own live host Process and matching connection.
    private var nativeAcceptanceConnection: ManagerConnection?
    private var generation: UInt64 = 0
    private var quitting = false
    private var installing = false
    private var stopping = false
    private var pollInFlight = false
    private var timer: Timer?
    private var stopCompletion: (() -> Void)?
    private var hostMessage = "Starting host…"
    private var hostAvailable = false
    private let dashboard = MenuDashboard()
    private var detailRequests: Set<String> = []
    private var lastDetailRefresh = Date.distantPast
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
    private var management: ManagementDashboard?
    private var managementTabs: NSTabView?
    private var workspacePicker: NSSegmentedControl?
    private var setupHostText: NSTextField?
    private var pendingInstalledSelection: String?
    private var pendingAppRequests: Set<String> = []
    private var pathField: NSTextField?
    private var nameField: NSTextField?
    private var policyField: NSPopUpButton?
    private var startCheck: NSButton?
    private var reviewButton: NSButton?
    private var planDetailsButton: NSButton?
    private var installButton: NSButton?
    private var showInstalledButton: NSButton?
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
    private let configurationView = SetupConfigurationView()
    private let artifactCaptureView = ArtifactCaptureView()
    private var setupSettings: [String: Any] = ["schema_version": 1, "configuration": [String: Any](), "secret_references": [String: String]()]
    private var secrets: [SetupSecret] = []
    private var upgradeTarget: String?
    private var downloadRows: [[String: Any]] = []
    private var selectedDownload: [String: Any]?
    private var downloadsPolling = false
    private var downloadPicker: NSPopUpButton?
    private var downloadLabel: NSTextField?
    private var downloadStartButton: NSButton?
    private var downloadCancelButton: NSButton?
    private var downloadRetryButton: NSButton?
    private var downloadUseButton: NSButton?
    private var starterButton: NSButton?
    private var starterLabel: NSTextField?
    private var publisherDownloadToggle: NSButton?
    private var publisherDownloadCard: NSView?
    private var operationCancelButton: NSButton?
    private var operationRecoverButton: NSButton?
    private var operationStopButton: NSButton?
    private var modelEngineButton: NSButton?
    private var modelProfileButton: NSButton?
    private var recipesWindow: NSWindow?
    private let recipesView = PreparationRecipesView()
    private let preparationView = PreparationWorkspaceView()
    private let agentPreparationView = AgentPreparationView()
    private let stateDirectory: URL = {
        if let configured = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"], configured.hasPrefix("/") {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/mcpme", isDirectory: true)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if !uiPreviewMode && AppInstaller.isOnReadOnlyVolume(Bundle.main.bundleURL) {
            installFromDiskImage(); return
        }
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
        statusItem.button?.target = self; statusItem.button?.action = #selector(handleStatusButton)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        configureDashboard()
        if uiPreviewMode {
            showDashboardPreview(); return
        }
        rebuildMenu(); startHost()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
    }

    private func installFromDiskImage() {
        installing = true
        let source = Bundle.main.bundleURL
        let destination = AppInstaller.destination()
        let alert = NSAlert()
        alert.messageText = "Install mcpme?"
        alert.informativeText = "Install in \(destination.deletingLastPathComponent().path). Any running copy of mcpme will quit, stopping its services, and the installed copy will open. Your apps and settings are preserved."
        alert.addButton(withTitle: "Install and Restart"); alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { installing = false; NSApp.terminate(nil); return }
        let progress = NSAlert()
        progress.messageText = "Installing mcpme…"
        progress.informativeText = "Copying and verifying the app, then waiting for the existing copy to quit."
        progress.addButton(withTitle: "Installing…").isEnabled = false
        progress.window.standardWindowButton(.closeButton)?.isEnabled = false
        progress.window.makeKeyAndOrderFront(nil)
        DispatchQueue.global(qos: .userInitiated).async {
            var failure: AppInstallError?
            do {
                try AppInstaller.install(from: source, to: destination,
                    hooks: InstallHooks(verify: AppInstaller.verifyCandidate,
                                        stop: { try AppInstaller.stopRunningCopies() }, launch: AppInstaller.launch))
            } catch { failure = error as? AppInstallError ?? .replace }
            let result = failure
            DispatchQueue.main.async {
                progress.window.orderOut(nil)
                self.installing = false
                if let error = result {
                    let failed = NSAlert(); failed.messageText = "mcpme could not be installed"
                    failed.informativeText = "Installation stopped (\(error.rawValue)). " + (error == .recovery
                        ? "Installation needs recovery. A recovery copy may be in a hidden .mcpme-install folder in \(destination.deletingLastPathComponent().path). Read that folder's RECOVERY.md before restoring it."
                        : "Your apps and settings were preserved. If Applications requires administrator access, quit mcpme, drag the app to Applications, and open it there.")
                    failed.addButton(withTitle: "OK"); failed.runModal()
                }
                NSApp.terminate(nil)
            }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if installing { return false }
        if uiPreviewMode {
            if managePreviewMode { showManagement() }
            else if window?.isVisible != true && !dashboard.isVisible { toggleDashboard() }
            return false
        }
        if process == nil && !quitting && !stopping { startHost() }
        showManagement(); return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        agentPreparationView.clearCredentialEntry()
        if installing { return .terminateCancel }
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

    private func resetHostRequestState() {
        pollInFlight = false; importsPollInFlight = false; downloadsPolling = false
        setupRequest = false; detailRequests = []; pendingAppRequests = []
        preparationView.invalidate(); recipesView.invalidate(); agentPreparationView.invalidate()
        agentPreparationView.resetProviderConnection()
    }

    private func startHost() {
        guard !uiPreviewMode else { return }
        guard process == nil, !quitting, !stopping, let host = hostRoot else { return }
        let executable = host.appendingPathComponent("bin/mcpme")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { hostMessage = "Bundled host is unavailable"; rebuildMenu(); return }
        generation &+= 1; hostAvailable = false; resetHostRequestState()
        hostMessage = "Starting host…"; projects = []; operations = []
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
                self.process = nil; self.generation &+= 1; self.resetHostRequestState()
                self.hostAvailable = false; self.projects = []; self.operations = []
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
        stopping = true; hostAvailable = false; stopCompletion = completion; hostMessage = "Stopping apps…"; rebuildMenu(); refreshWindow()
        process.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self, weak process] in
            guard let self = self, let process = process, self.process === process, process.isRunning else { return }
            // Only signal the Process object this app launched and still owns.
            kill(process.processIdentifier, SIGKILL)
        }
    }

    private func connection() -> ManagerConnection? {
        guard !uiPreviewMode else { return nil }
        if let accepted = nativeAcceptanceConnection {
            guard let object = PrivateFile.read(stateDirectory.appendingPathComponent("manager-connection.json")),
                  let current = ManagerConnection(object), current.pid == accepted.pid,
                  current.endpoint == accepted.endpoint, current.token == accepted.token else { return nil }
            return current
        }
        guard let process = process, process.isRunning,
              let object = PrivateFile.read(stateDirectory.appendingPathComponent("manager-connection.json")),
              let connection = ManagerConnection(object), connection.pid == process.processIdentifier else { return nil }
        return connection
    }

    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil,
                         completion: @escaping ([String: Any]?, String?) -> Void) {
        let process = self.process
        guard !uiPreviewMode, !quitting, !stopping, process?.isRunning == true || nativeAcceptanceConnection != nil, let connection = connection() else {
            completion(nil, "host_unavailable"); return
        }
        var request = URLRequest(url: connection.endpoint.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = managementRequestTimeout(path, method: method)
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
                guard let self = self, self.generation == expectedGeneration, self.process === process,
                      process?.isRunning == true || self.nativeAcceptanceConnection?.pid == connection.pid else { return }
                completion(object, object == nil ? code : nil)
            }
        }
    }

    private func poll() {
        guard !uiPreviewMode else { return }
        guard !pollInFlight, !quitting, !stopping else { return }
        guard connection() != nil else {
            if hostAvailable && !uiPreviewMode {
                hostAvailable = false; hostMessage = "Waiting for host… Activity will refresh after reconnecting."
                rebuildMenu(); refreshWindow()
            }
            return
        }
        pollInFlight = true
        request("v1/status") { [weak self] object, _ in
            guard let self = self else { return }; self.pollInFlight = false
            guard let object = object, integer(object["supervisor_pid"]) == Int(self.process?.processIdentifier ?? 0),
                  let rows = object["slots"] as? [[String: Any]],
                  let operations = object["operations"] as? [[String: Any]] else {
                self.hostAvailable = false; self.hostMessage = "Waiting for host… Activity will refresh after reconnecting."; self.rebuildMenu(); self.refreshWindow(); return
            }
            let parsed = rows.compactMap(Project.init)
            guard parsed.count == rows.count, Set(parsed.map(\.id)).count == parsed.count else {
                self.hostAvailable = false; self.hostMessage = "App status unavailable"; self.rebuildMenu(); self.refreshWindow(); return
            }
            self.hostAvailable = true
            self.projects = parsed.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }; self.operations = operations.compactMap(Operation.init)
            self.hostMessage = "\(parsed.filter { $0.state == "ready" }.count) ready / \(parsed.count) installed"
            self.rebuildMenu(); self.refreshWindow(); self.pollTrackedOperations(); self.pollImports(); self.pollDownloads(); self.preparationView.poll(); self.artifactCaptureView.poll(); self.agentPreparationView.poll()
            if Date().timeIntervalSince(self.lastDetailRefresh) >= 6 {
                var selected: Set<String> = []
                if self.dashboard.isVisible, let id = self.dashboard.selectedID { selected.insert(id) }
                if self.window?.isVisible == true, self.workspacePicker?.selectedSegment == 0,
                   let id = self.management?.selectedID { selected.insert(id) }
                for id in selected { self.loadDetails(id) }
            }
        }
    }

    private func configureDashboard() {
        dashboard.onSelect = { [weak self] id in self?.showDetails(id) }
        dashboard.onControl = { [weak self] id, action in self?.control(id, action: action) }
        dashboard.onCopy = { [weak self] id in self?.copyConfiguration(id) }
        dashboard.onManage = { [weak self] in self?.showManagement() }
        dashboard.onAdd = { [weak self] in self?.addProject() }
        dashboard.onStopAll = { [weak self] in self?.stopAll() }
        dashboard.onQuit = { [weak self] in self?.quitApp() }
        dashboard.onRetry = { [weak self] in self?.retryHost() }
        dashboard.onLogin = { [weak self] in self?.toggleLogin() }
    }

    @objc private func handleStatusButton() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || (event?.type == .leftMouseUp && event?.modifierFlags.contains(.control) == true) {
            guard let button = statusItem.button else { return }
            dashboard.dismiss()
            let menu = NSMenu()
            let open = NSMenuItem(title: "Open", action: #selector(openDashboard), keyEquivalent: "")
            open.target = self; menu.addItem(open)
            menu.addItem(.separator())
            let quit = NSMenuItem(title: "Quit", action: #selector(quitApp), keyEquivalent: "")
            quit.target = self; menu.addItem(quit)
            menu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.minY), in: button)
        } else { toggleDashboard() }
    }

    @objc private func openDashboard() {
        dashboard.dismiss()
        toggleDashboard()
    }

    @objc private func toggleDashboard() {
        guard let button = statusItem.button, let window = button.window,
              let screen = window.screen ?? NSScreen.main else { return }
        let anchor = window.convertToScreen(button.convert(button.bounds, to: nil))
        dashboard.toggle(anchor: anchor, screen: screen)
        if dashboard.isVisible { poll() }
    }

    private var dashboardApps: [DashboardApp] {
        projects.map { project in
            let busy = pendingAppRequests.contains(project.id) || operations.contains {
                $0.projectID == project.id && $0.active
            }
            let recoveryError = operations.first { $0.projectID == project.id && $0.recoveryError != nil }?.recoveryError
            return DashboardApp(id: project.id, name: project.name, status: recoveryError != nil ? "Recovery required" : project.status, state: project.state,
                                policy: project.policyLabel, activity: project.activity,
                                error: project.error == "ownership_lost" ? project.error : recoveryError ?? project.error, busy: busy)
        }
    }

    private func rebuildMenu() {
        guard statusItem != nil else { return }
        let loginTitle = SMAppService.mainApp.status == .requiresApproval ? "Approve Start at Login…" :
            (SMAppService.mainApp.status == .enabled ? "Start at Login: On" : "Start at Login: Off")
        dashboard.update(apps: dashboardApps, hostMessage: hostMessage, available: hostAvailable && !stopping && !quitting, loginTitle: loginTitle)
        statusItem.button?.toolTip = "mcpme: " + hostMessage
    }

    private func control(_ id: String, action: String, recoveryOperationID: String? = nil) {
        guard !uiPreviewMode else { return }
        guard hostAvailable, !stopping, !quitting, !pendingAppRequests.contains(id),
              let project = projects.first(where: { $0.id == id }),
              projectCanControl(project, action: action, operations: operations, recoveryOperationID: recoveryOperationID) else { return }
        pendingAppRequests.insert(id); refreshWindow(); rebuildMenu()
        request("v1/slots/\(id)/\(action)", method: "POST", body: [:]) { [weak self] object, code in
            guard let self = self else { return }
            self.pendingAppRequests.remove(id); self.refreshWindow(); self.rebuildMenu()
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
                        self.completedNotices.insert(id); self.setupLabel?.stringValue = "App installed. Open it to see activity and copy its connection."
                        self.pendingInstalledSelection = projectID
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
        dashboard.dismiss()
        if window == nil { makeManagementWindow() }
        window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); refreshWindow(); poll()
        if let id = management?.selectedID { loadDetails(id) }
    }

    @objc private func addProject() { showManagement(); selectWorkspace(1); chooseSource() }

    @objc private func changeWorkspace() { selectWorkspace(workspacePicker?.selectedSegment ?? 0) }

    private func selectWorkspace(_ index: Int) {
        workspacePicker?.selectedSegment = index
        managementTabs?.selectTabViewItem(at: index)
        if index == 0, let id = management?.selectedID { loadDetails(id) }
        if index == 1 { window?.makeFirstResponder(chooseButton) }
    }

    private func makeManagementWindow() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 780),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "mcpme — Manage Apps"; window.minSize = NSSize(width: 920, height: 640)
        window.isReleasedWhenClosed = false; window.delegate = self; window.center(); self.window = window
        let root = window.contentView!
        let brand = NSImageView(image: McpmeBrand.appIcon(size: 128))
        brand.setAccessibilityLabel("mcpme")
        brand.widthAnchor.constraint(equalToConstant: 40).isActive = true
        brand.heightAnchor.constraint(equalToConstant: 40).isActive = true
        let titles = workspaceStack([
            workspaceLabel("Manage Apps", size: 23, weight: .semibold),
            workspaceLabel("Local tools, models, and connections", size: 12, secondary: true)
        ], spacing: 3)
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let picker = NSSegmentedControl(labels: ["Installed Apps", "Add App"], trackingMode: .selectOne,
                                        target: self, action: #selector(changeWorkspace))
        picker.selectedSegment = 0; picker.segmentStyle = .rounded
        picker.setAccessibilityLabel("Manage Apps workspace"); self.workspacePicker = picker
        let heading = workspaceStack([brand, titles, spacer, picker], horizontal: true, spacing: 14)
        root.addSubview(heading)
        let separator = NSBox(); separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(separator)
        let tabs = NSTabView(); tabs.tabViewType = .noTabsNoBorder
        tabs.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(tabs); self.managementTabs = tabs
        NSLayoutConstraint.activate([
            heading.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            heading.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            heading.topAnchor.constraint(equalTo: root.topAnchor, constant: 20),
            heading.heightAnchor.constraint(equalToConstant: 48),
            separator.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 20),
            separator.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            separator.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            tabs.topAnchor.constraint(equalTo: separator.bottomAnchor),
            tabs.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            tabs.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            tabs.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        let installed = ManagementDashboard()
        self.management = installed
        installed.onSelect = { [weak self] id in self?.loadDetails(id) }
        installed.onControl = { [weak self] id, action in self?.control(id, action: action) }
        installed.onCopy = { [weak self] id in self?.copyConfiguration(id) }
        installed.onPolicy = { [weak self] id, policy in self?.changeAppPolicy(id, policy: policy) }
        installed.onRemove = { [weak self] id in self?.removeApp(id) }
        installed.onUpgrade = { [weak self] id in self?.chooseUpgrade(id) }
        installed.onConfigure = { [weak self] id in self?.configureInstalled(id) }
        installed.onOperation = { [weak self] id in self?.showAppOperation(id) }
        installed.onAdd = { [weak self] in self?.selectWorkspace(1) }
        installed.onRetry = { [weak self] in if !uiPreviewMode { self?.retryHost() } }
        let installedTab = NSTabViewItem(identifier: "installed"); installedTab.view = installed; tabs.addTabViewItem(installedTab)
        let setupTab = NSTabViewItem(identifier: "setup"); setupTab.view = makeSetupWorkspace(); tabs.addTabViewItem(setupTab)
        if !uiPreviewMode { restoreSetup(); pollImports() }
        refreshWindow()
    }

    private func makeSetupWorkspace() -> NSView {
        let page = NSScrollView(); page.hasVerticalScroller = true; page.autohidesScrollers = true; page.drawsBackground = false
        let content = WorkspaceDocumentStack(); content.orientation = .vertical; content.alignment = .leading; content.spacing = 18
        content.edgeInsets = NSEdgeInsets(top: 24, left: 28, bottom: 28, right: 28)
        content.translatesAutoresizingMaskIntoConstraints = false; page.documentView = content
        content.widthAnchor.constraint(equalTo: page.contentView.widthAnchor).isActive = true

        let draftPicker = NSPopUpButton(); draftPicker.addItem(withTitle: "New draft")
        draftPicker.target = self; draftPicker.action = #selector(selectDraft)
        draftPicker.setAccessibilityLabel("Saved app draft"); self.draftField = draftPicker
        draftPicker.setContentHuggingPriority(.defaultLow, for: .horizontal)
        draftPicker.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let newDraft = button("New Draft", action: #selector(newDraft)); self.newDraftButton = newDraft
        let draftsRow = workspaceStack([workspaceLabel("Resume setup", size: 12, weight: .medium),
                                       draftPicker, newDraft], horizontal: true)
        content.addArrangedSubview(draftsRow); workspaceFill(draftsRow, in: content, inset: 28)
        let host = workspaceLabel("", size: 12, secondary: true); self.setupHostText = host
        content.addArrangedSubview(host); workspaceFill(host, in: content, inset: 28)

        let starter = button("Try JSON Utilities", action: #selector(useStarterApp)); self.starterButton = starter
        let starterLabel = workspaceLabel("Included with mcpme. No download or build setup required.", size: 12, secondary: true)
        self.starterLabel = starterLabel
        let starterCard = workspaceCard("Try an included app", subtitle: "Give your assistant a tool that validates JSON and reports its top-level type.",
                                       views: [starterLabel, starter])
        content.addArrangedSubview(starterCard); workspaceFill(starterCard, in: content, inset: 28)

        let drop = SourceDropView(frame: .zero)
        drop.wantsLayer = true; drop.layer?.borderWidth = 1; drop.layer?.cornerRadius = 8
        drop.layer?.borderColor = NSColor.separatorColor.cgColor
        drop.acceptsSource = { [weak self] in self?.intakeEnabled == true }
        drop.selectedSource = { [weak self] url in self?.beginImport(url) }
        drop.heightAnchor.constraint(equalToConstant: 66).isActive = true
        let path = NSTextField(labelWithString: "")
        path.placeholderString = "Drop a source folder, prepared package, text document, or GGUF model"
        path.isSelectable = true; path.lineBreakMode = .byTruncatingMiddle
        path.setAccessibilityLabel("Selected source")
        path.setContentHuggingPriority(.defaultLow, for: .horizontal)
        path.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        self.pathField = path
        let choose = button("Choose Files…", action: #selector(chooseSource)); self.chooseButton = choose
        let sourceRow = workspaceStack([path, choose], horizontal: true)
        drop.addSubview(sourceRow)
        NSLayoutConstraint.activate([
            sourceRow.leadingAnchor.constraint(equalTo: drop.leadingAnchor, constant: 14),
            sourceRow.trailingAnchor.constraint(equalTo: drop.trailingAnchor, constant: -14),
            sourceRow.centerYAnchor.constraint(equalTo: drop.centerYAnchor),
        ])
        let name = NSTextField(); name.placeholderString = "App name"; name.delegate = self
        name.setAccessibilityLabel("App name"); self.nameField = name
        name.widthAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true
        let policy = NSPopUpButton(); policy.addItems(withTitles: ["Start manually", "Automatic when mcpme opens"])
        policy.target = self; policy.action = #selector(setupChanged); policy.setAccessibilityLabel("New app startup"); self.policyField = policy
        let start = NSButton(checkboxWithTitle: "Start after install", target: self, action: #selector(setupChanged)); self.startCheck = start
        let settings = workspaceStack([name, policy, start], horizontal: true)
        let intentScroll = NSScrollView(); intentScroll.hasVerticalScroller = true; intentScroll.borderType = .bezelBorder
        let intent = NSTextView(); intent.delegate = self; intent.font = .systemFont(ofSize: 13)
        intent.isRichText = false; intent.isAutomaticQuoteSubstitutionEnabled = false; intent.isAutomaticDashSubstitutionEnabled = false
        intent.isVerticallyResizable = true; intent.isHorizontallyResizable = false; intent.autoresizingMask = [.width]
        intent.textContainer?.widthTracksTextView = true; intent.textContainerInset = NSSize(width: 8, height: 8)
        intent.setAccessibilityLabel("Requested tools and behavior"); intentScroll.documentView = intent; self.intentField = intent
        intentScroll.heightAnchor.constraint(equalToConstant: 76).isActive = true
        let role = NSPopUpButton(); role.addItems(withTitles: ["Choose how to use this document", "Use this information", "Use these instructions"])
        role.target = self; role.action = #selector(setupChanged); role.setAccessibilityLabel("Document role"); self.documentRoleField = role
        let sourceCard = workspaceCard("1. Choose your source", subtitle: "Choose your app's directory, a setup brief, or an already prepared app. Source apps currently use your coding agent for preparation.",
            views: [drop, settings, workspaceLabel("What should your assistant be able to do?", size: 12, weight: .medium), intentScroll, role])

        artifactCaptureView.available = { [weak self] in
            guard let self else { return false }
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting && (!self.agentPreparationView.blocksNavigation || self.artifactCaptureView.blocksNavigation)
        }
        artifactCaptureView.request = { [weak self] path, method, body, completion in self?.request(path, method: method, body: body, completion: completion) }
        artifactCaptureView.beforeCapture = { [weak self] completion in self?.saveDraftBeforeNavigation(completion: completion) }
        artifactCaptureView.choices = { [weak self] in
            guard let self else { return [:] }
            var choices: [String: Any] = ["intent": self.intentField?.string ?? "", "start": self.startCheck?.state == .on, "start_policy": self.selectedPolicy]
            if let name = self.nameField?.stringValue, !name.isEmpty { choices["name"] = name }
            return choices
        }
        artifactCaptureView.onAdopt = { [weak self] object, target in
            guard let self, !self.draftDirty, !self.setupRequest, let draft = ImportDraft(object), draft.editable,
                  draft.packagePath == nil else { return false }
            if let target {
                guard draft.id == target.importID, draft.revision == target.revision + 1 else { return false }
                if let current = self.currentDraft {
                    guard current.id == target.importID,
                          (current.revision == target.revision && (target.sourceRevision.isEmpty || current.sourceRevision == target.sourceRevision) ||
                           current.revision == draft.revision && current.sourceRevision == draft.sourceRevision) else { return false }
                }
            }
            if !self.drafts.contains(where: { $0.id == draft.id }) { self.drafts.insert(draft, at: 0) }
            self.displayDraft(draft); self.preparationView.refreshForm(); return true
        }
        artifactCaptureView.onStateChange = { [weak self] in self?.refreshWindow() }
        artifactCaptureView.onJobChanged = { [weak self] id in
            if let id { self?.savedSetup["capture_id"] = id } else { self?.savedSetup.removeValue(forKey: "capture_id") }
            self?.persistSetup()
        }
        let captureCard = workspaceCard("Choose a local model", subtitle: "Capture model weights with their license, then continue with the detected app setup. Selecting files does not run the model.", views: [artifactCaptureView])

        let download = button("Enter Package Link…", action: #selector(beginDownload)); self.downloadStartButton = download
        let downloads = NSPopUpButton(); downloads.addItem(withTitle: "No saved downloads")
        downloads.target = self; downloads.action = #selector(selectDownload); downloads.setAccessibilityLabel("Saved package downloads"); self.downloadPicker = downloads
        let downloadLabel = workspaceLabel("Enter a package link supplied by its publisher. Downloads continue when this window closes.", size: 12, secondary: true); self.downloadLabel = downloadLabel
        let cancelDownload = button("Cancel Download", action: #selector(cancelDownload)); self.downloadCancelButton = cancelDownload
        let retryDownload = button("Retry Download", action: #selector(retryDownload)); self.downloadRetryButton = retryDownload
        let useDownload = button("Use Downloaded Package", action: #selector(useDownload)); self.downloadUseButton = useDownload
        let downloadCard = workspaceCard("Download from a publisher", subtitle: "This is for an existing package link. There is no package browser yet; the publisher must supply the URL, file size, and checksum.",
            views: [workspaceStack([download, downloads], horizontal: true), downloadLabel,
                    workspaceStack([cancelDownload, retryDownload, useDownload], horizontal: true)])
        self.publisherDownloadCard = downloadCard
        let publisherToggle = NSButton(checkboxWithTitle: "I already have a package download link", target: self, action: #selector(togglePublisherDownload))
        self.publisherDownloadToggle = publisherToggle

        let save = button("Save Draft", action: #selector(saveDraft)); self.saveDraftButton = save
        let copy = button("Copy Agent Instructions", action: #selector(copyAgentInstructions)); self.copyInstructionsButton = copy
        let export = button("Export Setup Specification…", action: #selector(exportDraft)); self.exportDraftButton = export
        let attach = button("Import Prepared Package…", action: #selector(choosePackage)); self.attachButton = attach
        let cancel = button("Cancel Draft", action: #selector(cancelDraft)); self.cancelDraftButton = cancel
        let handoff = workspaceStack([save, copy, export], horizontal: true, spacing: 8)
        let handoffReturn = workspaceStack([attach, cancel], horizontal: true, spacing: 8)
        let engine = button("Choose Model Engine…", action: #selector(chooseModelEngine)); self.modelEngineButton = engine
        let profile = button("Use JevK5 Decision Tools", action: #selector(selectModelProfile)); self.modelProfileButton = profile
        let modelControls = workspaceStack([engine, profile], horizontal: true)
        let recipes = button("Manage Recipes…", action: #selector(showPreparationRecipes))
        preparationView.available = { [weak self] in
            guard let self else { return false }
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting && (!self.agentPreparationView.blocksNavigation || self.preparationView.hasActiveJob)
        }
        preparationView.request = { [weak self] path, method, body, completion in self?.request(path, method: method, body: body, completion: completion) }
        preparationView.onUpdatedDraft = { [weak self] object in
            guard let self, !self.draftDirty, !self.setupRequest, let current = self.currentDraft, let updated = ImportDraft(object),
                  updated.id == current.id, updated.revision >= current.revision, updated.sourceRevision == current.sourceRevision else { return false }
            self.displayDraft(updated); return true
        }
        preparationView.onStateChange = { [weak self] in self?.refreshWindow() }
        preparationView.onAttached = { [weak self] in self?.pollImports() }
        agentPreparationView.available = { [weak self] in
            guard let self else { return false }
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting &&
                (self.agentPreparationView.blocksNavigation || !self.preparationView.busy && !self.preparationView.hasActiveJob && !self.artifactCaptureView.blocksNavigation)
        }
        agentPreparationView.request = { [weak self] path, method, body, completion in
            guard let self else { completion(nil, "manager_request_failed"); return }
            self.refreshWindow()
            self.request(path, method: method, body: body, completion: completion)
        }
        agentPreparationView.onUpdatedDraft = { [weak self] object in
            guard let self, !self.draftDirty, !self.setupRequest, let current = self.currentDraft, let updated = ImportDraft(object),
                  updated.id == current.id, updated.revision == current.revision + 1, updated.sourceRevision == current.sourceRevision else { return false }
            self.displayDraft(updated); self.preparationView.refreshAvailability(); return true
        }
        agentPreparationView.onStateChange = { [weak self] in self?.refreshWindow() }
        agentPreparationView.onReviewPreparation = { [weak self] context in
            guard let self, !uiPreviewMode, self.connection() != nil, self.hostAvailable,
                  !self.draftDirty, !self.setupRequest, !self.stopping, !self.quitting,
                  !self.artifactCaptureView.blocksNavigation, !self.agentPreparationView.blocksNavigation,
                  !self.preparationView.busy, !self.preparationView.hasActiveJob,
                  let draft = self.currentDraft, draft.editable, draft.packagePath == nil,
                  ["local_folder", "application_directory"].contains(draft.sourceKind),
                  PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: draft.sourceRevision ?? "") == context else { return false }
            return self.preparationView.reviewAgentProposal(context)
        }
        recipesView.onRegistryChange = { [weak self] in self?.preparationView.registryChanged(); self?.agentPreparationView.registryChanged() }
        let prepareCard = workspaceCard("2. Prepare the app", subtitle: "Answer the detected questions and review the preparation plan. You can also export instructions for your coding agent.",
                                        views: [recipes, preparationView, workspaceCard("Agent assistance", subtitle: "Review the exact captured files and configured provider before transfer. Candidate selection requires ordinary preparation review.", views: [agentPreparationView]), handoff, handoffReturn, modelControls])
        configurationView.onChange = { [weak self] in self?.setupChanged() }
        configurationView.onCreateSecret = { [weak self] name in self?.createSecret(name) }
        let configureCard = workspaceCard("Configure the prepared app", subtitle: "Choose nonsecret settings and saved secret references before reviewing the exact plan.", views: [configurationView])

        let label = workspaceLabel("Choose a source to begin.", size: 13); self.setupLabel = label
        label.setAccessibilityLabel("Setup status")
        let reviewScroll = NSScrollView(); reviewScroll.hasVerticalScroller = true; reviewScroll.borderType = .bezelBorder
        let text = NSTextView(); text.isEditable = false; text.isSelectable = true; text.font = .systemFont(ofSize: 13)
        text.isVerticallyResizable = true; text.isHorizontallyResizable = false
        text.autoresizingMask = [.width]; text.textContainer?.widthTracksTextView = true
        text.textContainerInset = NSSize(width: 10, height: 10); text.setAccessibilityLabel("Installation review")
        reviewScroll.documentView = text; self.reviewText = text
        reviewScroll.heightAnchor.constraint(equalToConstant: 190).isActive = true
        let review = button("Review Package", action: #selector(reviewPackage)); self.reviewButton = review
        let install = button("Add App", action: #selector(installPackage)); self.installButton = install
        install.bezelColor = .controlAccentColor
        let details = button("Package and Verification Details…", action: #selector(showPlanDetails)); self.planDetailsButton = details
        let actions = workspaceStack([review, install, details], horizontal: true, spacing: 8)
        let showInstalled = button("Open Installed App", action: #selector(showInstalledApp))
        self.showInstalledButton = showInstalled
        let cancelOperation = button("Cancel Operation", action: #selector(cancelSetupOperation)); self.operationCancelButton = cancelOperation
        let recoverOperation = button("Recover Candidate", action: #selector(recoverSetupOperation)); self.operationRecoverButton = recoverOperation
        let stopOperation = button("Stop Candidate", action: #selector(stopSetupOperation)); self.operationStopButton = stopOperation
        stopOperation.toolTip = "Stop the exposed candidate and preserve its saved data while recovery remains required"
        let reviewCard = workspaceCard("3. Review and install", subtitle: "Check the tools, declared access, and startup choices before adding this app.",
                                       views: [label, reviewScroll, actions, workspaceStack([cancelOperation, stopOperation, recoverOperation], horizontal: true), showInstalled])
        for card in [sourceCard, captureCard, publisherToggle, downloadCard, prepareCard, configureCard, reviewCard] {
            content.addArrangedSubview(card); workspaceFill(card, in: content, inset: 28)
        }
        return page
    }

    func controlTextDidChange(_ notification: Notification) { setupChanged() }
    func textDidChange(_ notification: Notification) { setupChanged() }

    private func refreshActionButtons() {
        management?.update(apps: dashboardApps, hostMessage: hostMessage,
                           available: hostAvailable && !stopping && !quitting,
                           operations: operations.map { ManagementOperation(appID: $0.projectID, title: $0.label, status: $0.status, action: $0.action) })
    }

    private func refreshWindow() {
        let captureContext = currentDraft.flatMap { draft -> ArtifactCaptureContext? in
            guard draft.editable, draft.packagePath == nil, !draftDirty,
                  ["local_folder", "document", "captured_artifact_collection"].contains(draft.sourceKind),
                  let source = draft.sourceRevision else { return nil }
            return ArtifactCaptureContext(importID: draft.id, revision: draft.revision, sourceRevision: source)
        }
        artifactCaptureView.bind(captureContext); artifactCaptureView.refreshAvailability()
        recipesView.refreshAvailability()
        let preparationContext = currentDraft.flatMap { draft in
            draft.sourceRevision.flatMap { PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: $0) }
        }
        preparationView.bind(preparationContext, saved: !draftDirty, eligible: currentDraft?.editable == true && currentDraft?.packagePath == nil)
        preparationView.refreshAvailability()
        let agentContext = currentDraft.flatMap { draft -> PreparationContext? in
            guard draft.editable, draft.packagePath == nil, ["local_folder", "application_directory"].contains(draft.sourceKind) else { return nil }
            return preparationContext
        }
        agentPreparationView.bind(agentContext, saved: !draftDirty); agentPreparationView.refreshAvailability()
        refreshActionButtons()
        if let id = pendingInstalledSelection, projects.contains(where: { $0.id == id }), management != nil {
            pendingInstalledSelection = nil; management?.select(id: id)
        }
        setupHostText?.stringValue = uiPreviewMode ? "Preview · setup actions are disabled." :
            (hostAvailable ? "Drafts are kept when you switch views or close this window." : "Host unavailable. Setup will resume when the connection returns.")
        let operation = setupOperation.flatMap { id in operations.first { $0.id == id } }
        let active = operation.map { $0.active || $0.status == "recovery_required" } ?? (setupOperation != nil)
        let enabled = process?.isRunning == true && connection() != nil && !stopping && !quitting && !setupRequest && !active
        let editable = enabled && (currentDraft?.editable ?? true) && !preparationView.busy && !preparationView.hasActiveJob && !artifactCaptureView.blocksNavigation && !agentPreparationView.blocksNavigation
        chooseButton?.isEnabled = intakeEnabled; newDraftButton?.isEnabled = intakeEnabled
        starterButton?.isEnabled = intakeEnabled && bundledStarterPackage != nil
        starterLabel?.stringValue = bundledStarterPackage == nil ? "The included app is unavailable in this copy of mcpme." :
            "Included with mcpme. Review its tool and access before adding it."
        publisherDownloadCard?.isHidden = publisherDownloadToggle?.state != .on && downloadRows.isEmpty
        draftField?.isEnabled = intakeEnabled
        nameField?.isEnabled = editable; policyField?.isEnabled = editable; startCheck?.isEnabled = editable
        if upgradeTarget != nil { nameField?.isEnabled = false; policyField?.isEnabled = false; startCheck?.isEnabled = false }
        configurationView.setEditable(editable)
        intentField?.isEditable = editable; documentRoleField?.isEnabled = editable && currentDraft?.isDocument == true
        documentRoleField?.isHidden = currentDraft?.isDocument != true
        saveDraftButton?.isEnabled = editable && currentDraft != nil && draftDirty
        copyInstructionsButton?.isEnabled = editable && currentDraft != nil
        exportDraftButton?.isEnabled = editable && currentDraft != nil
        attachButton?.isEnabled = editable && currentDraft != nil
        cancelDraftButton?.isEnabled = enabled && currentDraft?.editable == true && !preparationView.busy && !artifactCaptureView.blocksNavigation && !agentPreparationView.blocksNavigation
        reviewButton?.isEnabled = editable && (currentDraft?.packagePath != nil || currentDraft == nil && !(pathField?.stringValue.isEmpty ?? true))
        installButton?.isEnabled = enabled && setupPlan != nil && setupOperation == nil && !draftDirty
        planDetailsButton?.isEnabled = setupPlan != nil
        installButton?.title = upgradeTarget != nil ? "Apply Reviewed Upgrade" : startCheck?.state == .on ? "Add & Start App" : "Add App"
        startCheck?.title = upgradeTarget == nil ? "Start after install" : "Keep running after upgrade"
        let operationAvailable = !uiPreviewMode && !setupRequest && hostAvailable && !stopping && !quitting
            && !pendingAppRequests.contains(operation?.projectID ?? "")
        operationCancelButton?.isHidden = operation == nil || !["install", "upgrade"].contains(operation!.action)
        operationCancelButton?.isEnabled = operationAvailable && operation?.active == true && operation?.exposed == false
        operationRecoverButton?.isHidden = operation?.status != "recovery_required"
        operationRecoverButton?.isEnabled = operationAvailable && operation?.status == "recovery_required"
        operationStopButton?.isHidden = operation?.canStopExposedRecovery != true
        operationStopButton?.isEnabled = operationAvailable && operation.map { operation in
            projects.first(where: { $0.id == operation.projectID }).map {
                projectCanControl($0, action: "stop", operations: operations, recoveryOperationID: operation.id)
            } ?? false
        } == true
        let model = currentDraft?.sourceKind == "artifact_collection"
        modelEngineButton?.isHidden = !model; modelProfileButton?.isHidden = !model
        modelEngineButton?.isEnabled = editable && model; modelProfileButton?.isEnabled = editable && model
        downloadStartButton?.isEnabled = intakeEnabled
        downloadPicker?.isEnabled = !uiPreviewMode && !setupRequest
        let downloadStatus = selectedDownload?["status"] as? String
        downloadCancelButton?.isEnabled = intakeEnabled && ["pending", "running"].contains(downloadStatus ?? "")
        downloadRetryButton?.isEnabled = intakeEnabled && ["failed", "canceled"].contains(downloadStatus ?? "")
        downloadUseButton?.isEnabled = intakeEnabled && downloadStatus == "completed" && selectedDownload?["package_path"] is String
        showInstalledButton?.isHidden = installedSetupAppID == nil
    }

    private var installedSetupAppID: String? {
        guard let id = setupOperation, let operation = operations.first(where: { $0.id == id }),
              operation.status == "completed", let appID = operation.projectID,
              projects.contains(where: { $0.id == appID }) else { return nil }
        return appID
    }

    @objc private func showInstalledApp() {
        guard let id = installedSetupAppID else { return }
        selectWorkspace(0); management?.select(id: id)
    }

    private var intakeEnabled: Bool {
        !uiPreviewMode && !setupRequest && !stopping && !quitting && !artifactCaptureView.blocksNavigation && !agentPreparationView.blocksNavigation && connection() != nil
    }

    private var bundledStarterPackage: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let package = resources.appendingPathComponent("starter-packages/json-utilities", isDirectory: true)
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: package.path, isDirectory: &directory), directory.boolValue,
              FileManager.default.fileExists(atPath: package.appendingPathComponent("mcpme.package.json").path),
              FileManager.default.fileExists(atPath: package.appendingPathComponent("mcpme.lock.json").path) else { return nil }
        return package
    }

    @objc private func useStarterApp() {
        guard intakeEnabled else { return }
        guard let package = bundledStarterPackage else {
            setupLabel?.stringValue = "The included app is unavailable in this copy of mcpme."; return
        }
        beginImport(package, name: "JSON Utilities")
    }

    @objc private func togglePublisherDownload() { refreshWindow() }

    private func pollSecrets() {
        guard !uiPreviewMode, connection() != nil else { return }
        request("v1/secrets") { [weak self] object, _ in
            guard let self, let rows = object?["secrets"] as? [[String: Any]] else { return }
            self.secrets = rows.compactMap(SetupSecret.init)
            self.configurationView.updateSecrets(self.secrets)
        }
    }

    private func createSecret(_ logicalName: String) {
        guard intakeEnabled, let window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.message = "Choose an owner-only file (mode 0600) containing this secret. Its value will not be displayed."
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url, let value = readSetupSecret(url) else {
                if response == .OK { self?.setupLabel?.stringValue = "Choose a private, owner-only UTF-8 file containing at most 4,096 bytes." }; return
            }
            let alert = NSAlert(); alert.messageText = "Save a secret reference"
            alert.informativeText = "Give this secret a display name. mcpme stores the value privately and gives this app an opaque reference."
            let name = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24)); name.stringValue = logicalName
            name.setAccessibilityLabel("Saved secret name"); alert.accessoryView = name
            alert.addButton(withTitle: "Save Secret"); alert.addButton(withTitle: "Cancel")
            alert.beginSheetModal(for: window) { [weak self] response in
                guard let self, response == .alertFirstButtonReturn, self.intakeEnabled else { return }
                let generation = self.importViewGeneration
                self.setupRequest = true; self.refreshWindow()
                self.request("v1/secrets", method: "POST", body: ["name": name.stringValue, "value": value]) { [weak self] object, code in
                    guard let self else { return }; self.setupRequest = false
                    guard let object, let secret = SetupSecret(object) else {
                        self.setupLabel?.stringValue = "Secret could not be saved (\(code ?? "invalid_secret"))."; self.refreshWindow(); return
                    }
                    self.secrets.append(secret)
                    if generation == self.importViewGeneration { self.configurationView.selectSecret(secret, for: logicalName) }
                    self.refreshWindow()
                }
            }
        }
    }

    @objc private func beginDownload() {
        guard intakeEnabled, let window else { return }
        let alert = NSAlert(); alert.messageText = "Download from a publisher"
        alert.informativeText = "Paste the package ZIP URL, exact file size, and SHA-256 checksum supplied by its publisher. These are required to verify the download. For a first app, cancel and choose Try JSON Utilities."
        let url = NSTextField(), size = NSTextField(), hash = NSTextField(), digest = NSTextField()
        url.placeholderString = "https://publisher.example/package.zip"; size.placeholderString = "Exact byte count"
        hash.placeholderString = "Archive SHA-256 (64 lowercase hexadecimal characters)"; digest.placeholderString = "Package digest (optional)"
        for (field, label) in [(url, "Public HTTPS package URL"), (size, "Exact archive byte count"), (hash, "Archive SHA-256"), (digest, "Optional package digest")] { field.setAccessibilityLabel(label) }
        let form = workspaceStack([url, size, hash, digest]); form.frame = NSRect(x: 0, y: 0, width: 500, height: 128)
        for field in [url, size, hash, digest] { workspaceFill(field, in: form) }
        alert.accessoryView = form; alert.addButton(withTitle: "Download"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn, self.intakeEnabled else { return }
            guard size.stringValue.range(of: "^[1-9][0-9]*$", options: .regularExpression) != nil, let bytes = Int(size.stringValue), bytes > 0,
                  hash.stringValue.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                  digest.stringValue.isEmpty || digest.stringValue.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
                self.downloadLabel?.stringValue = "Enter an exact positive byte count and lowercase SHA-256."; return
            }
            var spec: [String: Any] = ["url": url.stringValue, "size": bytes, "sha256": hash.stringValue]
            if !digest.stringValue.isEmpty { spec["package_digest"] = digest.stringValue }
            self.fetchDownload(spec)
        }
    }

    private func fetchDownload(_ descriptor: [String: Any]) {
        guard intakeEnabled else { return }; setupRequest = true; refreshWindow()
        request("v1/downloads", method: "POST", body: descriptor) { [weak self] object, code in
            guard let self else { return }; self.setupRequest = false
            guard let object, let id = object["download_id"] as? String, validUUID(id) else {
                self.downloadLabel?.stringValue = "Download request failed (\(code ?? "invalid_response"))."; self.refreshWindow(); return
            }
            self.selectedDownload = object; self.savedSetup["download_id"] = id
            self.renderDownload(); self.persistSetup(); self.pollDownloads(); self.refreshWindow()
        }
    }

    private func pollDownloads() {
        guard !uiPreviewMode, !downloadsPolling, connection() != nil else { return }; downloadsPolling = true
        request("v1/downloads") { [weak self] object, _ in
            guard let self else { return }; self.downloadsPolling = false
            guard let rows = object?["downloads"] as? [[String: Any]] else { return }
            self.downloadRows = rows.filter { ($0["download_id"] as? String).map(validUUID) == true }
            let selected = self.selectedDownload?["download_id"] as? String ?? self.savedSetup["download_id"] as? String
            self.downloadPicker?.removeAllItems()
            if self.downloadRows.isEmpty { self.downloadPicker?.addItem(withTitle: "No saved downloads") }
            for row in self.downloadRows {
                guard let id = row["download_id"] as? String else { continue }
                self.downloadPicker?.addItem(withTitle: String(id.prefix(8)) + " · " + (safeCode(row["status"]) ?? "unavailable"))
                self.downloadPicker?.lastItem?.representedObject = id
            }
            if let selected, let index = self.downloadPicker?.itemArray.firstIndex(where: { $0.representedObject as? String == selected }) { self.downloadPicker?.selectItem(at: index) }
            let id = selected ?? self.downloadRows.first?["download_id"] as? String
            if let id { self.inspectDownload(id) }
            else { self.refreshWindow() }
        }
    }

    private func inspectDownload(_ id: String) {
        request("v1/downloads/" + id) { [weak self] object, _ in
            guard let self, let object, object["download_id"] as? String == id,
                  let status = safeCode(object["status"]), ["pending", "running", "completed", "failed", "canceled"].contains(status),
                  integer(object["received_bytes"]) != nil, integer(object["size"]) != nil else { return }
            let selected = self.downloadPicker?.selectedItem?.representedObject as? String
            guard selected == nil || selected == id else { return }
            self.selectedDownload = object; self.savedSetup["download_id"] = id; self.renderDownload(); self.refreshWindow()
        }
    }

    private func renderDownload() {
        guard let download = selectedDownload else { return }
        let received = integer(download["received_bytes"]) ?? 0, size = integer(download["size"]) ?? 0
        let stage = safeCode(download["stage"]) ?? "unavailable"
        var text = stage.replacingOccurrences(of: "_", with: " ").capitalized + " · \(received.formatted()) / \(size.formatted()) bytes"
        if let attempt = integer(download["attempt"]) { text += " · attempt \(attempt)" }
        if let code = safeCode(download["error_code"]) { text += " (\(code))" }
        downloadLabel?.stringValue = text
    }

    @objc private func selectDownload() { if let id = downloadPicker?.selectedItem?.representedObject as? String { selectedDownload = nil; savedSetup["download_id"] = id; inspectDownload(id); persistSetup() } }
    @objc private func cancelDownload() {
        guard intakeEnabled, let id = selectedDownload?["download_id"] as? String, validUUID(id) else { return }
        request("v1/downloads/\(id)/cancel", method: "POST", body: [:]) { [weak self] object, code in
            guard let self else { return }
            if let object { self.selectedDownload = object; self.renderDownload() }
            else { self.downloadLabel?.stringValue = "Cancellation failed (\(code ?? "request_failed"))." }
            self.refreshWindow()
        }
    }
    @objc private func retryDownload() { if let descriptor = selectedDownload?["descriptor"] as? [String: Any] { fetchDownload(descriptor) } }
    @objc private func useDownload() {
        guard intakeEnabled, selectedDownload?["status"] as? String == "completed", let path = selectedDownload?["package_path"] as? String, path.hasPrefix("/") else { return }
        beginImport(URL(fileURLWithPath: path, isDirectory: true))
    }

    private func configureInstalled(_ id: String) {
        guard intakeEnabled, let project = projects.first(where: { $0.id == id }),
              projectCanControl(project, action: "start", operations: operations) else { return }
        request("v1/slots/\(id)/details") { [weak self] object, code in
            guard let self, let object, let slot = object["slot"] as? [String: Any], let package = object["package"] as? [String: Any],
                  let path = slot["package_path"] as? String, path.hasPrefix("/") else { self?.showError(code ?? "configuration_unavailable"); return }
            self.beginUpgrade(project: project, path: path, package: package, settings: object["settings"] as? [String: Any] ?? [:])
        }
    }

    private func chooseUpgrade(_ id: String) {
        guard intakeEnabled, let project = projects.first(where: { $0.id == id }),
              projectCanControl(project, action: "start", operations: operations), let window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.message = "Choose the prepared replacement package for \(project.name)"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url else { return }
            self.request("v1/slots/\(id)/details") { [weak self] object, code in
                guard let self, let object else { self?.showError(code ?? "configuration_unavailable"); return }
                self.beginUpgrade(project: project, path: url.path, package: nil, settings: object["settings"] as? [String: Any] ?? [:])
            }
        }
    }

    private func beginUpgrade(project: Project, path: String, package: [String: Any]?, settings: [String: Any]) {
        saveCurrentDraft { [weak self] saved in
            guard let self, saved else { return }; self.clearDraft(); self.upgradeTarget = project.id
            self.selectWorkspace(1); self.pathField?.stringValue = path; self.nameField?.stringValue = project.name
            self.policyField?.selectItem(at: project.policy == "host" ? 1 : 0); self.startCheck?.state = project.state == "ready" ? .on : .off
            self.setupSettings = settings; self.configurationView.configure(package: package, settings: settings)
            self.pollSecrets(); self.setupLabel?.stringValue = "Upgrade \(project.name). Review the candidate and its configuration before applying."
            if package == nil { self.readUpgradeMetadata(path) }; self.persistSetup(); self.refreshWindow()
        }
    }

    private func readUpgradeMetadata(_ path: String) {
        // Metadata only; planning performs authoritative complete package validation.
        let file = URL(fileURLWithPath: path, isDirectory: true).appendingPathComponent("mcpme.package.json")
        let generation = importViewGeneration
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let metadata = readSetupPackageMetadata(file)
            DispatchQueue.main.async {
                guard let self, generation == self.importViewGeneration else { return }
                self.configurationView.configure(package: metadata, settings: self.setupSettings); self.pollSecrets(); self.refreshWindow()
                if metadata == nil { self.setupLabel?.stringValue = "The selected package metadata could not be read safely. Choose a complete prepared package." }
            }
        }
    }

    private func showAppOperation(_ id: String) {
        let related = operations.filter { $0.projectID == id && ["install", "upgrade"].contains($0.action) }
        guard let operation = related.first(where: { $0.status == "recovery_required" }) ?? related.first else { return }
        saveDraftBeforeNavigation { [weak self] saved in
            guard let self, saved else { return }; self.clearDraft(); self.showManagement(); self.selectWorkspace(1)
            self.setupOperation = operation.id; self.trackedOperations.insert(operation.id)
            self.setupLabel?.stringValue = operation.label + (operation.exposed ? " Candidate was exposed; recovery preserves its saved data." : "")
            self.persistSetup(); self.refreshWindow(); self.pollTrackedOperations()
        }
    }

    @objc private func cancelSetupOperation() { mutateSetupOperation("cancel") }
    @objc private func stopSetupOperation() {
        guard !setupRequest, let operation = setupOperation.flatMap({ id in operations.first { $0.id == id } }),
              operation.canStopExposedRecovery, let projectID = operation.projectID else { return }
        control(projectID, action: "stop", recoveryOperationID: operation.id)
    }
    @objc private func recoverSetupOperation() {
        guard let operation = setupOperation.flatMap({ id in operations.first { $0.id == id } }), operation.status == "recovery_required", let window else { return }
        let alert = NSAlert(); alert.messageText = "Recover this candidate?"
        alert.informativeText = operation.exposed ? "The candidate was exposed to clients. Recovery verifies the retained candidate and preserves its saved data and recovery artifacts. If you chose Stop, it stays stopped until you explicitly Start it after recovery completes. Old data will not be restored automatically." : "Recovery finishes settling owned processes and restores the previous generation only when exposure has not occurred."
        alert.addButton(withTitle: "Recover Candidate"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in if response == .alertFirstButtonReturn { self?.mutateSetupOperation("recover") } }
    }
    private func mutateSetupOperation(_ action: String) {
        guard !uiPreviewMode, !setupRequest, let id = setupOperation,
              let operation = operations.first(where: { $0.id == id }),
              action == "cancel" && operation.active && !operation.exposed || action == "recover" && operation.status == "recovery_required" else { return }
        setupRequest = true; refreshWindow()
        request("v1/operations/\(id)/\(action)", method: "POST", body: [:]) { [weak self] object, code in
            guard let self else { return }; self.setupRequest = false
            if let object, let updated = Operation(object) {
                self.operations.removeAll { $0.id == id }; self.operations.insert(updated, at: 0); self.trackedOperations.insert(id)
                self.setupLabel?.stringValue = updated.label
            } else { self.setupLabel?.stringValue = "Operation request failed (\(code ?? "request_failed"))." }
            self.refreshWindow(); self.poll()
        }
    }

    @objc private func chooseModelEngine() {
        guard intakeEnabled, let draft = currentDraft, draft.editable, draft.sourceKind == "artifact_collection", let window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.message = "Choose a verified JevK5 app or engine build containing its runtime manifest, llama engine, and licenses."
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let path = panel.url?.path else { return }
            self.saveCurrentDraft { [weak self] saved in
                guard let self, saved, let current = self.currentDraft else { return }; self.setupRequest = true; self.refreshWindow()
                self.request("v1/imports/\(current.id)/engine", method: "POST", body: ["revision": current.revision, "model_engine": path]) { [weak self] object, code in
                    guard let self else { return }; self.setupRequest = false
                    if let object, let updated = ImportDraft(object) { self.displayDraft(updated) }
                    else { self.setupLabel?.stringValue = self.draftError(code ?? "engine_artifacts_required"); self.refreshWindow() }
                }
            }
        }
    }

    @objc private func selectModelProfile() {
        guard intakeEnabled, let draft = currentDraft, draft.editable, draft.sourceKind == "artifact_collection", let window else { return }
        let alert = NSAlert(); alert.messageText = "Use this model for JevK5 decisions?"
        alert.informativeText = "This prepared profile exposes jevk5_decide and jevk5_status using the pinned JevK5 scorer and compatible llama.cpp engine. Model identity and engine compatibility must pass preparation checks."
        alert.addButton(withTitle: "Use Decision Tools"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn else { return }
            self.saveCurrentDraft { [weak self] saved in
                guard let self, saved, let current = self.currentDraft else { return }
                var requirements = current.requirements; requirements["engine_profile"] = "jevk5-llamacpp-v1"
                requirements["profile"] = "model-service"; requirements["tools"] = ["jevk5_decide", "jevk5_status"]
                self.setupRequest = true; self.refreshWindow()
                self.request("v1/imports/\(current.id)/revise", method: "POST", body: ["revision": current.revision, "requirements": requirements]) { [weak self] object, code in
                    guard let self else { return }; self.setupRequest = false
                    if let object, let updated = ImportDraft(object) { self.displayDraft(updated) }
                    else { self.setupLabel?.stringValue = self.draftError(code ?? "invalid_import_requirements"); self.refreshWindow() }
                }
            }
        }
    }

    private func changeAppPolicy(_ id: String, policy: String) {
        guard !uiPreviewMode, hostAvailable, !stopping, !quitting,
              let project = projects.first(where: { $0.id == id }), !project.blocked,
              ["manual", "host"].contains(policy), project.policy != "first_call",
              policy != project.policy, !pendingAppRequests.contains(project.id),
              !operations.contains(where: { $0.projectID == id && ($0.active || $0.recoveryError != nil) }) else { return }
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

    private func removeApp(_ id: String) {
        guard !uiPreviewMode, hostAvailable, !stopping, !quitting,
              let project = projects.first(where: { $0.id == id }), !project.blocked,
              !pendingAppRequests.contains(id),
              !operations.contains(where: { $0.projectID == id && ($0.active || $0.recoveryError != nil) }),
              let window = window else { return }
        let alert = NSAlert(); alert.alertStyle = .warning
        alert.messageText = "Remove \(project.name)?"
        alert.informativeText = "This stops the app and removes it from mcpme. Its assistant connections will stop working. Source files and saved app data are kept."
        alert.addButton(withTitle: "Remove App"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.control(project.id, action: "remove") }
        }
    }

    private func copyConfiguration(_ id: String) {
        guard !uiPreviewMode else { management?.connectionCopied(id: id); dashboard.connectionCopied(id: id); return }
        guard let project = projects.first(where: { $0.id == id }) else { showError("configuration_unavailable"); return }
        let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/mcpme").path
        let object = connectionConfiguration(name: project.connectionName, id: id, executable: executable, stateDirectory: stateDirectory.path)
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else { showError("configuration_unavailable"); return }
        NSPasteboard.general.clearContents()
        guard NSPasteboard.general.setString(text, forType: .string) else { showError("configuration_unavailable"); return }
        dashboard.connectionCopied(id: id)
        management?.connectionCopied(id: id)
    }

    private func showDetails(_ id: String) {
        if !dashboard.isVisible { toggleDashboard() }
        dashboard.showDetails(id: id)
        loadDetails(id)
    }

    private func loadDetails(_ id: String) {
        if uiPreviewMode { loadPreviewDetails(id); return }
        guard !detailRequests.contains(id) else { return }
        detailRequests.insert(id); lastDetailRefresh = Date()
        request("v1/slots/\(id)/details") { [weak self] object, code in
            guard let self = self else { return }
            self.detailRequests.remove(id)
            guard self.dashboard.selectedID == id || self.management?.selectedID == id else { return }
            guard let object = object, let slot = object["slot"] as? [String: Any],
                  let project = Project(slot), project.id == id,
                  let package = object["package"] as? [String: Any] else {
                self.dashboard.detailsFailed(id: id); self.management?.detailsFailed(id: id); return
            }
            let details = self.dashboardDetails(project: project, package: package, response: object)
            self.dashboard.updateDetails(details)
            self.management?.updateDetails(details)
        }
    }

    private func dashboardDetails(project: Project, package: [String: Any], response: [String: Any]) -> DashboardDetails {
        var sections: [DashboardSection] = []
        if let model = package["model"] as? [String: Any] {
            var lines = [model["model_id"] as? String ?? "Model"]
            if let metrics = response["runtime_metrics"] as? [String: Any],
               let counts = metrics["counts"] as? [String: Any] {
                if let decisions = integer(counts["decisions"]) { lines.append("Decisions: \(decisions.formatted())") }
                if let hits = integer(counts["cache_hits"]) { lines.append("Cache hits: \(hits.formatted())") }
                if let errors = integer(counts["errors"]) { lines.append("Failed decisions: \(errors.formatted())") }
                if let queue = integer(metrics["queue_depth"]) { lines.append("Queue depth: \(queue)") }
                if let latency = finiteNumber(metrics["last_latency_ms"]) { lines.append("Last decision: \(Int(latency.rounded())) ms") }
                lines.append("Reported by the model runtime; resets when the runtime restarts.")
            } else { lines.append("Model activity is unavailable while the runtime is stopped or unreachable.") }
            if let context = integer(model["context_tokens"]) { lines.append("Context capacity: \(context.formatted()) tokens") }
            sections.append(DashboardSection(title: "Model", body: lines.joined(separator: "\n")))
        }
        if let tools = package["tools"] as? [[String: Any]] {
            sections.append(DashboardSection(title: "Tools · \(tools.count)", body: tools.map {
                ($0["name"] as? String ?? "Tool") + "\n" + ($0["description"] as? String ?? "")
            }.joined(separator: "\n\n")))
        }
        sections.append(DashboardSection(title: "Connection", body: "http://127.0.0.1:\(project.port)/mcp\nUse Copy Connection to add this app to your assistant.\nStartup: \(project.policyLabel)"))
        if let resources = package["resources"] as? [String: Any] {
            let memory = integer(resources["memory_mb"]).map { "\($0.formatted()) MB memory reservation" } ?? "Memory estimate unavailable"
            let compute = resources["gpu"] as? Bool == true ? "Uses GPU" : "CPU"
            sections.append(DashboardSection(title: "Declared resources", body: "\(memory) · \(compute)\nPackage estimates; current memory use is not measured."))
        }
        if let access = package["access"] as? [String: Any] {
            let files = (access["filesystem"] as? [String] ?? []).joined(separator: ", ")
            let network = (access["network"] as? [String] ?? []).joined(separator: ", ")
            sections.append(DashboardSection(title: "Declared access", body: [
                "Files: " + (files.isEmpty ? "None declared" : files),
                "Network: " + (network.isEmpty ? "None declared" : network),
                "Writes: " + (access["writes"] as? Bool == true ? "Yes" : "None declared"),
                "Runs with your account permissions. These declarations are not an OS sandbox."
            ].joined(separator: "\n")))
        }
        var identity: [String] = []
        for (key, label) in [("package_id", "Package"), ("version", "Version"), ("source_revision", "Source revision")] {
            if let value = package[key] as? String { identity.append(label + ": " + value) }
        }
        if let licenses = package["licenses"] as? [String] { identity.append("Licenses: " + licenses.joined(separator: ", ")) }
        identity.append("Generation: \(project.generation)")
        sections.append(DashboardSection(title: "Package", body: identity.joined(separator: "\n")))
        let profile = package["profile"] as? String == "model-service" ? "Local model" : "Local tools"
        return DashboardDetails(appID: project.id, subtitle: profile, sections: sections)
    }

    // A content-free fixture for native layout and accessibility review. This path
    // never starts a host, opens installation state, or changes login settings.
    private func showDashboardPreview() {
        let now = Date().timeIntervalSince1970
        let formatter = ISO8601DateFormatter()
        let states: [(String, String, Int, Int)] = [("JevK5 Judge", "ready", 1284, 2), ("JSON Utilities", "ready", 327, 0),
                      ("Document Search", "failed", 12, 3), ("Local Chat", "stopped", 0, 0)]
        projects = states.enumerated().compactMap { index, value in
            let previewActivity: [String: Any] = ["received": value.2, "succeeded": value.2 - value.3, "failed": value.3,
                "in_flight": 0, "requests_last_minute": min(value.2, 18),
                "last_latency_ms": value.2 > 0 ? 146 as Any : NSNull(),
                "last_request_at": value.2 > 0 ? formatter.string(from: Date(timeIntervalSince1970: now - 8)) as Any : NSNull(),
                "started_at": formatter.string(from: Date(timeIntervalSince1970: now - 3600))]
            return Project(["slot_id": String(format: "%08d-2222-4333-8444-555555555555", index + 1),
                     "name": value.0, "observed_state": value.1, "public_port": 19385 + index,
                     "start_policy": "manual", "generation": 1,
                     "error_code": value.1 == "failed" ? "readiness_timeout" as Any : NSNull(),
                     "activity": previewActivity])
        }
        hostAvailable = true; hostMessage = "Preview · 2 ready / 4 installed"
        let scenario = ProcessInfo.processInfo.environment["MCPME_PREVIEW_SCENARIO"] ??
            Bundle.main.object(forInfoDictionaryKey: "McpmePreviewScenario") as? String ?? "standard"
        if scenario == "empty" { projects = [] }
        if scenario == "many", let template = projects.first {
            projects += (5...35).compactMap { index in
                Project(["slot_id": String(format: "%08d-2222-4333-8444-555555555555", index),
                         "name": index == 5 ? "A very long app name that still needs readable controls and an accessible identity" : "Utility \(index)",
                         "observed_state": index % 3 == 0 ? "ready" : "stopped",
                         "public_port": 19385 + index, "start_policy": "host", "generation": template.generation])
            }
        }
        hostMessage = "Preview · \(projects.filter { $0.state == "ready" }.count) ready / \(projects.count) installed"
        if scenario == "unavailable" { hostAvailable = false; hostMessage = "Preview · Host unavailable. Reconnecting…" }
        if scenario == "recovery", let project = projects.first {
            operations = [Operation(["operation_id": "99999999-2222-4333-8444-555555555555",
                                     "slot_id": project.id, "action": "remove", "status": "recovery_required",
                                     "error_code": "removal_recovery_required"])!]
        }
        dashboard.onSelect = { [weak self] id in
            self?.dashboard.showDetails(id: id); self?.loadPreviewDetails(id)
        }
        dashboard.onControl = { _, _ in }; dashboard.onCopy = { _ in }
        dashboard.onManage = { [weak self] in self?.showManagement() }
        dashboard.onAdd = { [weak self] in self?.showManagement(); self?.selectWorkspace(1) }
        dashboard.onLogin = {}; dashboard.onStopAll = {}; dashboard.onRetry = {}
        rebuildMenu()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if managePreviewMode || scenario.hasPrefix("setup_") {
                self.showManagement()
                if scenario.hasPrefix("setup_") {
                    self.selectWorkspace(1)
                    let fixture: [String: Any] = ["package_id": "org.example.synthetic", "version": "1.0.0", "profile": "mcp-stdio",
                        "configuration": ["schema_version": 1, "fields": ["Label": ["type": "string", "required": true, "default": "Synthetic label"],
                            "Retries": ["type": "integer", "minimum": 0, "maximum": 8, "default": 2], "Enabled": ["type": "boolean", "default": false]]],
                        "access": ["filesystem": [], "network": [], "writes": false, "secrets": ["API_KEY"]],
                        "tools": [["name": "synthetic_tool", "description": "Exercise the native setup form."]]]
                    let secretID = "11111111-2222-4333-8444-555555555555"
                    self.configurationView.configure(package: fixture, settings: ["configuration": [:], "secret_references": ["API_KEY": secretID]])
                    self.configurationView.updateSecrets([SetupSecret(["secret_id": secretID, "name": "Synthetic secret", "status": "active"])!])
                    self.nameField?.stringValue = "Synthetic app"; self.pathField?.stringValue = "/private/preview/prepared-package"
                    if scenario == "setup_recovery" {
                        let id = "99999999-2222-4333-8444-555555555555"
                        self.operations = [Operation(["operation_id": id, "slot_id": secretID, "action": "upgrade", "status": "recovery_required",
                            "details": ["stage": "candidate_exposed", "exposed": true], "error_code": "upgrade_recovery_required"])!]
                        self.setupOperation = id; self.setupLabel?.stringValue = "Upgrade: Recovery required. Candidate writes and recovery artifacts are preserved."
                    }
                    if scenario == "setup_model" {
                        self.currentDraft = ImportDraft(["draft_id": secretID, "revision": 1, "state": "needs_information", "name": "Synthetic model",
                            "source": ["kind": "artifact_collection", "locator": "/private/preview/model.gguf"], "start_policy": "manual", "start": false])
                    }
                    self.refreshWindow()
                }
            }
            else {
                self.toggleDashboard()
            }
        }
    }

    private func loadPreviewDetails(_ id: String) {
        guard let project = projects.first(where: { $0.id == id }) else { return }
        let model = project.name == "JevK5 Judge"
        let package: [String: Any] = ["profile": model ? "model-service" : "mcp-stdio",
            "package_id": "org.example.local-app", "version": "1.0.0", "source_revision": "preview",
            "licenses": ["Apache-2.0"], "resources": ["memory_mb": model ? 6144 : 128, "gpu": model],
            "model": model ? ["model_id": "JevK5-4B-v0.3-Q8_0", "context_tokens": 8192] : NSNull(),
            "tools": [["name": model ? "jevk5_decide" : "validate_json", "description": "Evaluate supplied input with a local tool."],
                      ["name": "status", "description": "Check readiness and aggregate activity."]],
            "access": ["filesystem": [], "network": [], "writes": false]]
        let response: [String: Any] = ["runtime_metrics": ["counts": ["decisions": 1284, "cache_hits": 318, "errors": 2], "queue_depth": 0]]
        let details = dashboardDetails(project: project, package: package, response: response)
        dashboard.updateDetails(details); management?.updateDetails(details)
    }

    func windowWillClose(_ notification: Notification) {
        if notification.object as? NSWindow === window { agentPreparationView.clearCredentialEntry() }
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
        guard !uiPreviewMode else { return }
        guard !importsPollInFlight, !setupRequest, !preparationView.busy, connection() != nil else { return }
        importsPollInFlight = true
        let expectedViewGeneration = importViewGeneration
        request("v1/imports") { [weak self] object, _ in
            guard let self = self else { return }; self.importsPollInFlight = false
            guard let rows = object?["imports"] as? [[String: Any]] else { return }
            let drafts = rows.compactMap(ImportDraft.init)
            guard drafts.count == rows.count, Set(drafts.map(\.id)).count == drafts.count else { return }
            self.drafts = drafts; self.updateDraftPicker()
            let selectedID = self.currentDraft?.id ?? self.savedSetup["import_id"] as? String
            if let id = selectedID, let draft = drafts.first(where: { $0.id == id }), !self.draftDirty, !self.setupRequest, !self.preparationView.busy,
               expectedViewGeneration == self.importViewGeneration {
                let previous = self.currentDraft
                if previous == nil || previous?.revision != draft.revision || previous?.state != draft.state || previous?.packageDigest != draft.packageDigest || previous?.operationID != draft.operationID {
                    self.request("v1/imports/" + id) { [weak self] object, _ in
                        guard let self = self, !self.draftDirty, !self.setupRequest, !self.preparationView.busy,
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
        // A poll begun before a save or import can still return an older list.
        // Keep the visible draft identified until inventory catches up.
        var displayedDrafts = drafts
        if let currentDraft {
            if let index = displayedDrafts.firstIndex(where: { $0.id == currentDraft.id }) {
                if displayedDrafts[index].revision <= currentDraft.revision { displayedDrafts[index] = currentDraft }
            } else { displayedDrafts.insert(currentDraft, at: 0) }
        }
        picker.removeAllItems(); picker.addItem(withTitle: "New draft")
        for draft in displayedDrafts {
            let item = NSMenuItem(title: draft.name + " — " + draft.label, action: nil, keyEquivalent: "")
            item.representedObject = draft.id; picker.menu?.addItem(item)
        }
        if let id = selectedID, let item = picker.itemArray.first(where: { $0.representedObject as? String == id }) {
            picker.select(item)
        }
    }

    private func displayDraft(_ draft: ImportDraft) {
        restoringDraft = true; defer { restoringDraft = false }
        importViewGeneration &+= 1
        let changed = currentDraft?.id != draft.id || currentDraft?.revision != draft.revision || currentDraft?.packageDigest != draft.packageDigest
        currentDraft = draft; draftDirty = false
        upgradeTarget = nil
        pathField?.stringValue = draft.sourceKind == "captured_artifact_collection" ? "Captured artifacts" : draft.sourcePath
        nameField?.stringValue = draft.name; intentField?.string = draft.intent
        policyField?.selectItem(at: draft.policy == "host" ? 1 : 0)
        startCheck?.state = draft.start ? .on : .off
        documentRoleField?.selectItem(at: draft.documentRole == "reference" ? 1 : draft.documentRole == "instructions" ? 2 : 0)
        if changed { setupPlan = nil; reviewText?.string = "" }
        setupOperation = draft.operationID
        setupSettings = ["schema_version": 1, "configuration": draft.requirements["configuration"] as? [String: Any] ?? [:],
                         "secret_references": draft.requirements["secret_references"] as? [String: String] ?? [:]]
        configurationView.configure(package: draft.attachment, settings: setupSettings)
        pollSecrets()
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
        saveDraftBeforeNavigation { [weak self] saved in
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

    @objc private func newDraft() { saveDraftBeforeNavigation { [weak self] saved in if saved { self?.clearDraft() } } }

    private func clearDraft() {
        importViewGeneration &+= 1
        currentDraft = nil; draftDirty = false; setupPlan = nil; setupOperation = nil
        upgradeTarget = nil; setupSettings = ["schema_version": 1, "configuration": [String: Any](), "secret_references": [String: String]()]
        configurationView.configure(package: nil, settings: setupSettings)
        pathField?.stringValue = ""; nameField?.stringValue = ""; intentField?.string = ""
        policyField?.selectItem(at: 0); startCheck?.state = .off; documentRoleField?.selectItem(at: 0)
        reviewText?.string = ""; setupLabel?.stringValue = "Choose a source to begin."
        savedSetup.removeValue(forKey: "import_id"); updateDraftPicker(); persistSetup(); refreshWindow()
        window?.makeFirstResponder(chooseButton)
    }

    @objc private func chooseSource() {
        guard intakeEnabled, let window = window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [UTType(filenameExtension: "md")!, .plainText, UTType(filenameExtension: "gguf") ?? .data]
        panel.message = "Choose a source folder, prepared package, text document, or GGUF model"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }; self?.beginImport(url)
        }
    }

    private func beginImport(_ source: URL, name suppliedName: String? = nil) {
        guard intakeEnabled else { return }
        saveDraftBeforeNavigation { [weak self] saved in
            guard let self = self, saved else { return }
            let directory = (try? source.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
            let name = suppliedName ?? (directory ? source.lastPathComponent : source.deletingPathExtension().lastPathComponent)
            self.setupRequest = true; self.setupLabel?.stringValue = "Inspecting selected source…"; self.refreshWindow()
            self.request("v1/imports", method: "POST", body: ["source": source.path, "source_mode": "automatic", "name": name, "start": false, "start_policy": "manual"]) { [weak self] object, code in
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
        draft.isDocument && selectedDocumentRole != draft.documentRole ||
        configurationChanged(draft)
    }

    private func configurationChanged(_ draft: ImportDraft) -> Bool {
        guard draft.attachment != nil else { return false }
        guard let choices = try? configurationView.settings(allowIncomplete: true) else { return true }
        return !NSDictionary(dictionary: choices).isEqual(to: setupSettings)
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
        if draft.attachment != nil {
            guard let settings = try? configurationView.settings(allowIncomplete: true) else {
                setupLabel?.stringValue = "Correct the entered settings before saving this draft."; completion(false); return
            }
            var requirements = draft.requirements
            requirements["configuration"] = settings["configuration"]; requirements["secret_references"] = settings["secret_references"]
            payload["requirements"] = requirements
            if (intentField?.string ?? draft.intent) == draft.intent && selectedDocumentRole == draft.documentRole {
                payload["carry_attachment"] = true
                payload.removeValue(forKey: "intent"); payload.removeValue(forKey: "document_role")
            }
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

    private func saveDraftBeforeNavigation(completion: @escaping (Bool) -> Void) {
        guard !agentPreparationView.blocksNavigation else {
            updateDraftPicker(); completion(false); return
        }
        saveCurrentDraft { [weak self] saved in
            guard let self, !self.agentPreparationView.blocksNavigation else { completion(false); return }
            guard !saved else { completion(true); return }
            guard self.draftDirty, !self.setupRequest, let draft = self.currentDraft,
                  draft.editable, let window = self.window else { completion(false); return }
            let expectedView = self.importViewGeneration
            let alert = NSAlert(); alert.messageText = "Keep editing this draft?"
            alert.informativeText = "These edits could not be saved. You can keep editing, or continue with the last saved version of this draft. Continuing discards only the unsaved edits; the saved draft and its source are kept."
            alert.addButton(withTitle: "Keep Editing"); alert.addButton(withTitle: "Discard Edits and Continue")
            alert.beginSheetModal(for: window) { [weak self] response in
                guard let self, self.importViewGeneration == expectedView, self.currentDraft?.id == draft.id,
                      self.currentDraft?.revision == draft.revision, !self.setupRequest,
                      !self.agentPreparationView.blocksNavigation else { completion(false); return }
                guard response == .alertSecondButtonReturn else {
                    self.updateDraftPicker(); completion(false); return
                }
                self.displayDraft(draft); completion(true)
            }
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
        if currentDraft?.attachment != nil || upgradeTarget != nil {
            guard let settings = try? configurationView.settings() else {
                setupLabel?.stringValue = "Complete the required settings and select an available saved secret."; return
            }
            if currentDraft?.attachment != nil, !NSDictionary(dictionary: settings).isEqual(to: setupSettings) { draftDirty = true }
        }
        saveCurrentDraft { [weak self] saved in
            guard let self = self, saved else { return }
            var payload: [String: Any]
            if let draft = self.currentDraft {
                guard draft.editable, draft.packagePath != nil else { return }
                payload = ["import_id": draft.id, "revision": draft.revision]
            } else {
                guard let path = self.pathField?.stringValue, !path.isEmpty else { return }
                guard let settings = try? self.configurationView.settings() else { self.setupLabel?.stringValue = "Complete the app configuration before reviewing."; return }
                if let target = self.upgradeTarget { payload = ["slot_id": target, "package": path, "settings": settings] }
                else {
                    payload = ["package": path, "start_policy": self.selectedPolicy, "start": self.startCheck?.state == .on, "settings": settings]
                    if let name = self.nameField?.stringValue, !name.isEmpty { payload["name"] = name }
                }
            }
            self.setupRequest = true; self.setupLabel?.stringValue = "Verifying prepared artifacts…"; self.refreshWindow()
            self.request(self.upgradeTarget == nil ? "v1/install/plan" : "v1/upgrade/plan", method: "POST", body: payload) { [weak self] object, code in
                guard let self = self else { return }; self.setupRequest = false
                guard let object = object, let id = object["plan_id"] as? String, validUUID(id), object["package"] is [String: Any] else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "invalid_plan"); self.refreshWindow(); return
                }
                self.setupPlan = object; self.setupOperation = nil; self.reviewText?.string = self.reviewSummary(object)
                self.setupLabel?.stringValue = self.upgradeTarget == nil ? "Review the tools and declared access. Add App applies this exact plan." : "Review the replacement tools, configuration, and data policy. Apply Reviewed Upgrade applies this exact plan."
                self.persistSetup(); self.refreshWindow(); self.pollImports()
            }
        }
    }

    @objc private func installPackage() {
        guard let id = setupPlan?["plan_id"] as? String, validUUID(id) else { return }
        setupRequest = true; refreshWindow()
        request(upgradeTarget == nil ? "v1/install/apply" : "v1/upgrade/apply", method: "POST", body: ["plan_id": id]) { [weak self] object, code in
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
            if let lifecycle = package["lifecycle"] { sections.append("Request size and lifecycle limits\n" + pretty(lifecycle)) }
            if let resources = package["resources"] { sections.append("Declared resource estimates\n" + pretty(resources)) }
            if let access = package["access"] { sections.append("Declared access (same-user code; no OS sandbox)\n" + pretty(access)) }
            if let configuration = package["configuration"] { sections.append("Declared configuration fields and bounds\n" + pretty(configuration)) }
            if let tools = package["tools"] { sections.append("MCP tools and schemas\n" + pretty(tools)) }
        }
        if let digest = object["package_digest"] as? String { sections.append("Package digest: " + digest) }
        if let policy = object["start_policy"] as? String { sections.append("Start policy: " + policy) }
        if let settings = object["settings"] { sections.append("Configuration and opaque secret references\n" + pretty(settings)) }
        if let dataPolicy = object["data_policy"] as? String { sections.append("Data snapshot policy: " + dataPolicy) }
        if let rollback = object["rollback_policy"] as? String { sections.append("Rollback policy: " + rollback) }
        return sections.joined(separator: "\n\n")
    }

    private func reviewSummary(_ object: [String: Any]) -> String {
        var sections: [String] = []
        if let name = object["name"] as? String { sections.append("App: " + name) }
        if let draft = currentDraft {
            sections.append("Source: " + (draft.sourceKind == "captured_artifact_collection" ? "Captured artifacts" : draft.sourcePath))
            if !draft.intent.isEmpty { sections.append("Requested capabilities\n" + draft.intent) }
        }
        if let package = object["package"] as? [String: Any] {
            if let tools = package["tools"] as? [[String: Any]] {
                sections.append("Tools your assistant will receive\n" + tools.map {
                    ($0["name"] as? String ?? "Tool") + (($0["description"] as? String).map { " — " + $0 } ?? "")
                }.joined(separator: "\n"))
            }
            if let access = package["access"] { sections.append("Declared access and side effects\n" + pretty(access) + "\nRuns with your account permissions.") }
            let lifecycle = package["lifecycle"] as? [String: Any]
            let requestBytes = integer(lifecycle?["max_request_bytes"]) ?? 1_048_576
            sections.append("Maximum tool request: \(requestBytes.formatted()) bytes")
            if let resources = package["resources"] { sections.append("Declared resource estimates\n" + pretty(resources)) }
        }
        if let settings = object["settings"] { sections.append("Configuration and saved secret references\n" + pretty(settings)) }
        if object["kind"] as? String == "upgrade" {
            sections.append("Upgrade data policy\nThe host closes old admission, settles its processes, and clones saved app data before probing the candidate. Before exposure, a failed candidate restores the prior generation and data. After candidate exposure, recovery preserves candidate writes and recovery artifacts; it does not restore old data automatically.")
            if let generation = integer(object["expected_generation"]) { sections.append("Replaces generation: \(generation)") }
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

    @objc private func showPreparationRecipes() {
        if recipesWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 510),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = "Manage Recipes"; window.contentMinSize = NSSize(width: 680, height: 510)
            window.isReleasedWhenClosed = false; window.center()
            let container = NSView(); window.contentView = container
            recipesView.translatesAutoresizingMaskIntoConstraints = false; container.addSubview(recipesView)
            NSLayoutConstraint.activate([recipesView.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 20),
                recipesView.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -20),
                recipesView.topAnchor.constraint(equalTo: container.topAnchor, constant: 20),
                recipesView.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -20)])
            recipesView.available = { [weak self] in self?.connection() != nil && self?.hostAvailable == true }
            recipesView.request = { [weak self] path, method, body, completion in
                self?.request(path, method: method, body: body, completion: completion)
            }
            recipesWindow = window
        }
        recipesView.refreshRecipes(); recipesWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }

    private func persistSetup() {
        guard !uiPreviewMode else { return }
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
        if let target = upgradeTarget { object["upgrade_slot_id"] = target }
        if let id = selectedDownload?["download_id"] as? String ?? savedSetup["download_id"] as? String, validUUID(id) { object["download_id"] = id }
        if let id = artifactCaptureView.job?.id ?? savedSetup["capture_id"] as? String, validUUID(id) { object["capture_id"] = id }
        if let choices = try? configurationView.settings() { object["settings"] = choices }
        PrivateFile.write(object, to: setupFile)
        savedSetup = object
    }

    private func restoreSetup() {
        guard !uiPreviewMode else { return }
        guard let object = PrivateFile.read(setupFile), integer(object["schema_version"]) == 1 else { return }
        savedSetup = object
        if let id = preparationUUID(object["capture_id"]) { artifactCaptureView.restore(id) }
        if let id = object["upgrade_slot_id"] as? String, validUUID(id) { upgradeTarget = id }
        setupSettings = object["settings"] as? [String: Any] ?? setupSettings
        pathField?.stringValue = object["package"] as? String ?? ""; nameField?.stringValue = object["name"] as? String ?? ""
        policyField?.selectItem(at: object["start_policy"] as? String == "host" ? 1 : 0)
        startCheck?.state = object["start"] as? Bool == true ? .on : .off
        if let plan = object["plan"] as? [String: Any], let id = plan["plan_id"] as? String, validUUID(id) {
            setupPlan = plan; reviewText?.string = reviewSummary(plan)
            configurationView.configure(package: plan["package"] as? [String: Any], settings: setupSettings); pollSecrets()
            if upgradeTarget != nil {
                request("v1/upgrade/plans/" + id) { [weak self] object, code in
                    guard let self else { return }
                    if let object, object["plan_id"] as? String == id { self.setupPlan = object; self.reviewText?.string = self.reviewSummary(object) }
                    else { self.setupPlan = nil; self.setupLabel?.stringValue = "Review this package again (\(code ?? "plan_unavailable"))." }
                    self.refreshWindow()
                }
            }
        }
        if let id = object["operation_id"] as? String, validUUID(id) { setupOperation = id; trackedOperations.insert(id) }
        if upgradeTarget != nil && setupPlan == nil, let path = object["package"] as? String, !path.isEmpty { readUpgradeMetadata(path) }
    }

    @objc private func toggleLogin() {
        guard !uiPreviewMode else { return }
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
    let good: [String: Any] = ["slot_id": id, "name": "Synthetic", "connection_name": "synthetic", "observed_state": "ready", "public_port": 19386, "start_policy": "manual", "generation": 1]
    guard Project(good)?.id == id, Project(good)?.connectionName == "synthetic",
          fallbackConnectionName("Hot Dog / Not Hot Dog", id: id) == "hot-dog-not-hot-dog",
          fallbackConnectionName("!!!", id: id) == "app-" + id, integer(true) == nil, safeCode("private arbitrary text") == nil,
          ManagerConnection(["api_version": 1, "supervisor_pid": 42, "endpoint": "http://127.0.0.1:19384", "token": String(repeating: "x", count: 32)]) != nil,
          ManagerConnection(["api_version": 1, "supervisor_pid": 42, "endpoint": "http://example.com:19384", "token": String(repeating: "x", count: 32)]) == nil else { return false }
    var updating = good; updating["observed_state"] = "updating"
    guard Project(updating)?.status == "Updating", Project(updating)?.blocked == true,
          managementRequestTimeout("v1/imports", method: "POST") == 300,
          managementRequestTimeout("v1/imports/" + id + "/engine", method: "POST") == 300,
          managementRequestTimeout("v1/imports/" + id + "/attach", method: "POST") == 300,
          managementRequestTimeout("v1/install/plan", method: "POST") == 300,
          managementRequestTimeout("v1/upgrade/plan", method: "POST") == 300,
          managementRequestTimeout("v1/imports", method: "GET") == 30,
          managementRequestTimeout("v1/operations/" + id + "/recover", method: "POST") == 30 else { return false }
    let recoveryID = "99999999-2222-4333-8444-555555555555"
    let captured: [String: Any] = ["draft_id": id, "revision": 1, "state": "needs_information",
        "name": "Synthetic detector", "source": ["kind": "captured_artifact_collection", "revision": "sha256:" + String(repeating: "a", count: 64)],
        "start_policy": "manual", "start": false]
    guard let capturedDraft = ImportDraft(captured), capturedDraft.sourcePath.isEmpty,
          capturedDraft.sourceSnapshotPath == nil, capturedDraft.editable else { return false }
    var missingFolderLocator = captured; missingFolderLocator["source"] = ["kind": "local_folder"]
    guard ImportDraft(missingFolderLocator) == nil else { return false }
    guard let ordinary = Project(good), projectCanControl(ordinary, action: "remove", operations: []) else { return false }
    for action in ["install", "upgrade"] {
        var blocked = good; blocked["observed_state"] = "failed"; blocked["error_code"] = action == "install" ? "installation_recovery_required" : "upgrade_recovery_required"
        let fixture: [String: Any] = ["operation_id": recoveryID, "slot_id": id, "action": action,
                                      "status": "recovery_required", "details": ["exposed": true]]
        guard let project = Project(blocked), let operation = Operation(fixture), operation.canStopExposedRecovery,
              projectCanControl(project, action: "stop", operations: [operation], recoveryOperationID: recoveryID),
              !projectCanControl(project, action: "stop", operations: [operation]),
              !projectCanControl(project, action: "stop", operations: [operation], recoveryOperationID: id),
              !projectCanControl(ordinary, action: "start", operations: [operation]) else { return false }
        for control in ["start", "restart", "remove"] {
            guard !projectCanControl(project, action: control, operations: [operation], recoveryOperationID: recoveryID) else { return false }
        }
        var unexposed = fixture; unexposed["details"] = ["exposed": false]
        var active = fixture; active["status"] = "running"
        var removal = fixture; removal["action"] = "remove"
        var missingSlot = fixture; missingSlot.removeValue(forKey: "slot_id")
        for denied in [unexposed, active, removal, missingSlot] {
            guard let operation = Operation(denied), !operation.canStopExposedRecovery,
                  !projectCanControl(project, action: "stop", operations: [operation], recoveryOperationID: recoveryID) else { return false }
        }
        blocked["observed_state"] = "updating"
        guard let interrupted = Project(blocked),
              projectCanControl(interrupted, action: "stop", operations: [operation], recoveryOperationID: recoveryID),
              !projectCanControl(interrupted, action: "start", operations: [operation]) else { return false }
        blocked["observed_state"] = "ownership_lost"
        guard let lost = Project(blocked),
              !projectCanControl(lost, action: "stop", operations: [operation], recoveryOperationID: recoveryID) else { return false }
    }
    let exported = connectionConfiguration(name: "synthetic", id: id, executable: "/Applications/mcpme.app/Contents/MacOS/mcpme", stateDirectory: "/tmp/state")
    guard let servers = exported["mcpServers"] as? [String: Any],
          let registration = servers["synthetic"] as? [String: Any],
          servers[id] == nil,
          registration["args"] as? [String] == ["--state-dir", "/tmp/state", "connect", "--slot", id] else { return false }
    guard managementSelfTest() else { return false }
    let activity: [String: Any] = ["received": 8, "succeeded": 5, "failed": 1, "in_flight": 2,
                                 "requests_last_minute": 3, "last_latency_ms": 12.5,
                                 "last_request_at": "2026-10-02T12:00:01.125Z", "started_at": "2026-10-02T12:00:00Z"]
    guard let parsedActivity = parseActivity(activity), parsedActivity.received == 8,
          parsedActivity.lastLatencyMS == 12.5, parsedActivity.lastRequestAt != nil,
          parseActivity(nil) == nil, parseActivity([:]) == nil,
          finiteNumber(true) == nil, finiteNumber(Double.infinity) == nil else { return false }
    var invalidActivity = activity; invalidActivity["in_flight"] = 3
    guard parseActivity(invalidActivity) == nil else { return false }
    invalidActivity = activity; invalidActivity["received"] = true
    guard parseActivity(invalidActivity) == nil else { return false }
    invalidActivity = activity; invalidActivity["available"] = false
    guard parseActivity(invalidActivity) == nil, dashboardTotal([Int.max, 1]) == Int.max else { return false }
    for screen in [NSRect(x: 0, y: 0, width: 1512, height: 944), NSRect(x: -1024, y: 200, width: 1024, height: 700),
                   NSRect(x: 0, y: 0, width: 800, height: 600)] {
        for x in [screen.minX, screen.maxX - 24] {
            let anchor = NSRect(x: x, y: screen.maxY, width: 24, height: 24)
            let frames = dashboardFrames(anchor: anchor, screen: screen, showsDetails: true)
            guard let detail = frames.details, screen.contains(frames.menu), screen.contains(detail),
                  detail.minX > frames.menu.maxX else { return false }
        }
    }
    return true
}

private func nativeRecipeCanonicalPath(_ path: String) -> String? {
    guard path.hasPrefix("/"), path.utf8.count <= 4096, !path.contains("\0"),
          let resolved = realpath(path, nil) else { return nil }
    defer { free(resolved) }
    return String(validatingUTF8: resolved)
}

private struct NativeRecipeDirectoryIdentity: Equatable {
    let device: UInt64
    let inode: UInt64

    init?(_ url: URL) {
        guard url.isFileURL, nativeRecipeCanonicalPath(url.path) == url.path else { return nil }
        let fd = open(url.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }; defer { close(fd) }
        var opened = stat(), named = stat()
        guard fstat(fd, &opened) == 0, lstat(url.path, &named) == 0,
              (opened.st_mode & S_IFMT) == S_IFDIR, opened.st_uid == getuid(), (opened.st_mode & 0o777) == 0o700,
              opened.st_dev == named.st_dev, opened.st_ino == named.st_ino, opened.st_mode == named.st_mode,
              opened.st_uid == named.st_uid else { return nil }
        device = UInt64(opened.st_dev); inode = UInt64(opened.st_ino)
    }
}

private struct NativeRecipeFileIdentity: Equatable {
    let values: [String: Int]

    init?(_ url: URL) {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }; defer { close(fd) }
        var before = stat(), after = stat(), named = stat()
        guard fstat(fd, &before) == 0, lstat(url.path, &named) == 0,
              (before.st_mode & S_IFMT) == S_IFREG, before.st_uid == getuid(), before.st_nlink == 1,
              (before.st_mode & 0o777) == 0o600, before.st_size > 0, before.st_size <= 262144,
              fstat(fd, &after) == 0 else { return nil }
        func identity(_ info: stat) -> [String: Int] {
            ["device": Int(info.st_dev), "inode": Int(info.st_ino), "bytes": Int(info.st_size), "mode": Int(info.st_mode & 0o777),
             "modified_seconds": Int(info.st_mtimespec.tv_sec), "modified_nanoseconds": Int(info.st_mtimespec.tv_nsec),
             "changed_seconds": Int(info.st_ctimespec.tv_sec), "changed_nanoseconds": Int(info.st_ctimespec.tv_nsec)]
        }
        let value = identity(before)
        guard value == identity(after), value == identity(named), nativeRecipeCanonicalPath(url.path) == url.path else { return nil }
        values = value
    }

    func matches(_ object: [String: Any]) -> Bool {
        Set(object.keys) == Set(values.keys) && values.allSatisfy { preparationInteger(object[$0.key]) == $0.value }
    }
}

private struct NativeRecipeAcceptanceContext {
    let fixture: URL
    let state: URL
    let output: URL
    let nonce: String
    let marker: URL
    let expiry: Int
    let fixtureIdentity: NativeRecipeFileIdentity
    let markerIdentity: NativeRecipeFileIdentity
    let managerIdentity: NativeRecipeFileIdentity
    let rootIdentity: NativeRecipeDirectoryIdentity
    let stateIdentity: NativeRecipeDirectoryIdentity
    let workspaceIdentity: NativeRecipeDirectoryIdentity
    let object: [String: Any]

    static func privateDirectory(_ url: URL) -> Bool {
        NativeRecipeDirectoryIdentity(url) != nil
    }

    init?(fixture: URL) {
        guard let fixtureIdentity = NativeRecipeFileIdentity(fixture),
              Self.privateDirectory(fixture.deletingLastPathComponent()), let object = PrivateFile.read(fixture, limit: 262144),
              Set(object.keys) == ["schema_version", "state_dir", "output_path", "fixture_nonce", "recipes", "forms"],
              preparationInteger(object["schema_version"]) == 1,
              let nonce = preparationUUID(object["fixture_nonce"]),
              let statePath = preparationText(object["state_dir"], maximum: 4096), statePath.hasPrefix("/"),
              let outputPath = preparationText(object["output_path"], maximum: 4096), outputPath.hasPrefix("/") else { return nil }
        let state = URL(fileURLWithPath: statePath), output = URL(fileURLWithPath: outputPath)
        let root = fixture.deletingLastPathComponent()
        let workspace = root.deletingLastPathComponent()
        guard workspace.lastPathComponent.hasPrefix("mcpme-native-onboarding-"), let workspaceIdentity = NativeRecipeDirectoryIdentity(workspace),
              let rootIdentity = NativeRecipeDirectoryIdentity(root),
              state.path == root.appendingPathComponent("state").path, let stateIdentity = NativeRecipeDirectoryIdentity(state),
              output.deletingLastPathComponent().path == root.path, output.lastPathComponent.count <= 128,
              output.path != fixture.path, !FileManager.default.fileExists(atPath: output.path),
              let recipes = object["recipes"] as? [[String: Any]], recipes.count <= 8,
              let forms = object["forms"] as? [[String: Any]], forms.count <= 16, !recipes.isEmpty || !forms.isEmpty else { return nil }
        let marker = state.appendingPathComponent("native-recipes-" + nonce + ".json")
        guard let markerIdentity = NativeRecipeFileIdentity(marker), let proof = PrivateFile.read(marker, limit: 8192),
              Set(proof.keys) == ["schema_version", "fixture_nonce", "expires_at", "manager_file"],
              preparationInteger(proof["schema_version"]) == 1, preparationUUID(proof["fixture_nonce"]) == nonce,
              let expiry = preparationInteger(proof["expires_at"]), expiry > Int(Date().timeIntervalSince1970), expiry <= Int(Date().timeIntervalSince1970) + 300,
              let expectedManager = proof["manager_file"] as? [String: Any],
              let managerIdentity = NativeRecipeFileIdentity(state.appendingPathComponent("manager-connection.json")),
              managerIdentity.matches(expectedManager), NativeRecipeFileIdentity(fixture) == fixtureIdentity,
              NativeRecipeFileIdentity(marker) == markerIdentity else { return nil }
        self.fixture = fixture; self.state = state; self.output = output; self.nonce = nonce; self.marker = marker; self.expiry = expiry
        self.fixtureIdentity = fixtureIdentity; self.markerIdentity = markerIdentity; self.managerIdentity = managerIdentity; self.object = object
        self.rootIdentity = rootIdentity; self.stateIdentity = stateIdentity; self.workspaceIdentity = workspaceIdentity
    }

    func current() -> Bool {
        Int(Date().timeIntervalSince1970) < expiry && NativeRecipeDirectoryIdentity(state) == stateIdentity &&
            NativeRecipeDirectoryIdentity(fixture.deletingLastPathComponent()) == rootIdentity &&
            NativeRecipeDirectoryIdentity(fixture.deletingLastPathComponent().deletingLastPathComponent()) == workspaceIdentity &&
            NativeRecipeFileIdentity(fixture) == fixtureIdentity &&
            NativeRecipeFileIdentity(marker) == markerIdentity && NativeRecipeFileIdentity(state.appendingPathComponent("manager-connection.json")) == managerIdentity
    }

    func claim() -> Bool {
        guard current() else { return false }
        let path = state.appendingPathComponent("native-recipes-" + nonce + ".claimed")
        let fd = open(path.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }; defer { close(fd) }
        let bytes = Array("claimed\n".utf8)
        return bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) == $0.count } && fsync(fd) == 0 && current()
    }

    static func contractSelfTest() -> Bool {
        guard let temporary = nativeRecipeCanonicalPath(FileManager.default.temporaryDirectory.path) else { return false }
        let workspace = URL(fileURLWithPath: temporary).appendingPathComponent("mcpme-native-onboarding-" + UUID().uuidString)
        let root = workspace.appendingPathComponent("context-test"), state = root.appendingPathComponent("state")
        defer { try? FileManager.default.removeItem(at: workspace) }
        do {
            for directory in [workspace, root, state] {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            }
            func save(_ value: [String: Any], to url: URL, replace: Bool = false) throws {
                let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
                try data.write(to: url, options: replace ? .atomic : .withoutOverwriting)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
            }
            let manager = state.appendingPathComponent("manager-connection.json")
            try save(["synthetic": true], to: manager)
            guard let identity = NativeRecipeFileIdentity(manager) else { return false }
            let nonce = UUID().uuidString.lowercased(), marker = state.appendingPathComponent("native-recipes-" + nonce + ".json")
            try save(["schema_version": 1, "fixture_nonce": nonce, "expires_at": Int(Date().timeIntervalSince1970) + 120, "manager_file": identity.values], to: marker)
            let fixture = root.appendingPathComponent("case.json")
            try save(["schema_version": 1, "fixture_nonce": nonce, "state_dir": state.path,
                "output_path": root.appendingPathComponent("receipt.json").path, "recipes": [], "forms": [["synthetic": true]]], to: fixture)
            guard let context = Self(fixture: fixture), context.current(), context.claim(), !context.claim() else { return false }
            let priorRoot = workspace.appendingPathComponent("prior-context-test")
            try FileManager.default.moveItem(at: root, to: priorRoot)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            for child in try FileManager.default.contentsOfDirectory(at: priorRoot, includingPropertiesForKeys: nil) {
                try FileManager.default.moveItem(at: child, to: root.appendingPathComponent(child.lastPathComponent))
            }
            guard !context.current() else { return false }
            for child in try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) {
                try FileManager.default.moveItem(at: child, to: priorRoot.appendingPathComponent(child.lastPathComponent))
            }
            try FileManager.default.removeItem(at: root)
            try FileManager.default.moveItem(at: priorRoot, to: root)
            guard let restoredContext = Self(fixture: fixture), restoredContext.current() else { return false }
            try save(["synthetic": true], to: manager, replace: true)
            guard !restoredContext.current(), Self(fixture: fixture) == nil else { return false }
            try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: fixture.path)
            guard NativeRecipeFileIdentity(fixture) == nil else { return false }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fixture.path)
            let link = root.appendingPathComponent("linked-case.json")
            guard Darwin.link(fixture.path, link.path) == 0, NativeRecipeFileIdentity(fixture) == nil else { return false }
            try FileManager.default.removeItem(at: link)
            let symlink = root.appendingPathComponent("symlinked-case.json")
            try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: fixture)
            return NativeRecipeFileIdentity(symlink) == nil
        } catch { return false }
    }
}

extension McpmeApp {
    /// Developer-only acceptance: dialog choices are simulated by an owned
    /// fixture; production handlers and authenticated owner socket are real.
    fileprivate static func recipesNativeAcceptance(fixture: URL) -> Bool {
        guard let context = NativeRecipeAcceptanceContext(fixture: fixture),
              let rows = context.object["recipes"] as? [[String: Any]], let formRows = context.object["forms"] as? [[String: Any]] else { return false }
        struct RecipeCase {
            let identity: PreparationRecipeIdentity
            let folder: URL?
            let folderIdentity: NativeRecipeDirectoryIdentity?
            let operations: [(action: String, state: String)]
        }
        var cases: [RecipeCase] = []
        for row in rows {
            guard Set(row.keys) == ["folder", "recipe", "operations"], let reference = row["recipe"] as? [String: Any],
                  Set(reference.keys) == ["recipe_id", "version", "digest"], let identity = PreparationRecipeIdentity(reference),
                  identity.id.range(of: "^[a-z0-9]+(?:[.-][a-z0-9]+)+$", options: .regularExpression) != nil,
                  identity.version.range(of: "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?$", options: .regularExpression) != nil,
                  let operations = row["operations"] as? [[String: Any]], !operations.isEmpty, operations.count <= 16 else { return false }
            var folder: URL?
            if !(row["folder"] is NSNull) {
                guard let path = preparationText(row["folder"], maximum: 4096) else { return false }
                let selected = URL(fileURLWithPath: path)
                guard selected.path.hasPrefix(fixture.deletingLastPathComponent().path + "/"), NativeRecipeAcceptanceContext.privateDirectory(selected) else { return false }
                folder = selected
            }
            var steps: [(action: String, state: String)] = []
            for operation in operations {
                guard Set(operation.keys) == ["action", "expected_state"], let action = operation["action"] as? String,
                      ["add", "inspect", "refresh", "enable", "disable", "deprecate", "revoke", "remove"].contains(action),
                      let state = operation["expected_state"] as? String,
                      ["proposed", "enabled", "disabled", "deprecated", "revoked", "removed"].contains(state), action != "add" || folder != nil else { return false }
                steps.append((action, state))
            }
            cases.append(RecipeCase(identity: identity, folder: folder, folderIdentity: folder.flatMap(NativeRecipeDirectoryIdentity.init), operations: steps))
        }
        for row in formRows {
            guard Set(row.keys) == ["draft_id", "expected_recipe", "choose_recipe", "expected_blockers"],
                  preparationUUID(row["draft_id"]) != nil, let blockers = row["expected_blockers"] as? [String], blockers.count <= 128,
                  blockers.allSatisfy({ preparationCode($0) != nil }) else { return false }
            for key in ["expected_recipe", "choose_recipe"] {
                guard row[key] is NSNull || (row[key] as? [String: Any]).flatMap(PreparationRecipeReference.init) != nil else { return false }
            }
        }
        guard context.claim(), let connectionObject = PrivateFile.read(context.state.appendingPathComponent("manager-connection.json")),
              let connection = ManagerConnection(connectionObject) else { return false }
        let prior = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", context.state.path, 1)
        defer { if let prior { setenv("MCPME_STATE_DIR", prior, 1) } else { unsetenv("MCPME_STATE_DIR") } }
        let app = McpmeApp(); _ = app.makeSetupWorkspace()
        app.nativeAcceptanceConnection = connection; app.hostAvailable = true
        let view = app.recipesView
        var pending = 0, sent = 0, completed = 0, changes = 0, formChecks = 0
        var active: RecipeCase?, stage = "recipes_refresh", errorCode: String?
        func awaitCondition(_ condition: () -> Bool) -> Bool {
            let end = Date().addingTimeInterval(60)
            while !condition(), Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
            return condition()
        }
        let request: PreparationRequest = { path, method, body, callback in
            guard context.current(), sent < 512 else { errorCode = "native_recipe_context_stale"; callback(nil, errorCode); return }
            let staticRoute = path == "v1/recipes" && method == "GET" || path == "v1/recipes/inspect" && method == "POST" ||
                path == "v1/preparations/form" && method == "POST" || method == "GET" && formRows.contains { path == "v1/imports/" + ($0["draft_id"] as? String ?? "") }
            if !staticRoute {
                guard let active else { errorCode = "native_recipe_action_rejected"; callback(nil, errorCode); return }
                if path == "v1/recipes", method == "POST" {
                    guard let folder = active.folder, NativeRecipeDirectoryIdentity(folder) == active.folderIdentity,
                          Set(body?.keys.map { $0 } ?? []) == ["path"], body?["path"] as? String == folder.path else { errorCode = "native_recipe_action_rejected"; callback(nil, errorCode); return }
                } else {
                    guard method == "POST", ["enable", "disable", "deprecate", "revoke", "remove"].contains(path.components(separatedBy: "/").last ?? ""),
                          path == "v1/recipes/" + (path.components(separatedBy: "/").last ?? ""),
                          Set(body?.keys.map { $0 } ?? []) == ["recipe_id", "version"], body?["recipe_id"] as? String == active.identity.id,
                          body?["version"] as? String == active.identity.version else { errorCode = "native_recipe_action_rejected"; callback(nil, errorCode); return }
                }
            }
            pending += 1; sent += 1
            app.request(path, method: method, body: body) { object, code in
                pending -= 1; completed += 1
                if let code { errorCode = preparationCode(code) ?? "native_recipe_request_failed" }
                callback(object, code)
            }
        }
        view.available = { context.current() }; view.request = request
        app.preparationView.request = request
        view.onRegistryChange = { changes += 1; app.preparationView.registryChanged() }
        func report(_ passed: Bool) -> Bool {
            let joined = awaitCondition { pending == 0 && !view.acceptanceBusy && !app.preparationView.busy }
            view.invalidate(); app.preparationView.invalidate()
            var receipt: [String: Any] = ["schema_version": 1, "passed": passed && joined, "stage": stage,
                "recipe_cases": cases.count, "registry_changes": changes, "form_checks": formChecks,
                "requests_sent": sent, "requests_completed": completed, "callbacks_joined": joined,
                "simulated_dialog_folder_selection": true, "simulated_lifecycle_confirmation": true,
                "production_handlers": true, "owner_private_socket": true, "fixture_nonce_consumed": true,
                "execution_started_by_driver": false, "provider_calls": 0, "model_runs": 0]
            if let errorCode { receipt["error_code"] = errorCode }
            guard let data = try? JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys]), data.count <= 262144 else { return false }
            let fd = open(context.output.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { return false }; defer { close(fd) }
            let written = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) == $0.count }
            return written && fsync(fd) == 0 && passed && joined
        }
        view.refreshRecipes()
        guard awaitCondition({ pending == 0 && !view.acceptanceBusy }), errorCode == nil else { return report(false) }
        for item in cases {
            active = item
            for operation in item.operations {
                guard context.current(), errorCode == nil else { return report(false) }
                stage = "recipe_" + operation.action
                if operation.action == "add" {
                    guard let folder = item.folder, NativeRecipeDirectoryIdentity(folder) == item.folderIdentity, view.acceptanceChooseRecipeFolder(folder) else { return report(false) }
                } else {
                    guard view.acceptanceSelectRecipe(item.identity), awaitCondition({ pending == 0 && !view.acceptanceBusy }),
                          view.acceptanceIdentity == item.identity else { return report(false) }
                    if operation.action == "refresh" { view.refreshRecipes() }
                    else if operation.action != "inspect", !view.acceptanceConfirmedChange(operation.action, identity: item.identity) { return report(false) }
                }
                guard awaitCondition({ pending == 0 && !view.acceptanceBusy }), errorCode == nil,
                      view.acceptanceIdentity == item.identity, view.acceptanceState == operation.state else { return report(false) }
            }
        }
        active = nil; stage = "generated_forms"
        for row in formRows {
            let id = row["draft_id"] as! String
            var draft: ImportDraft?, fetched = false
            request("v1/imports/" + id, "GET", nil) { object, _ in draft = object.flatMap(ImportDraft.init); fetched = true }
            guard awaitCondition({ fetched && pending == 0 }), let draft, draft.editable, draft.packagePath == nil else { return report(false) }
            app.preparationView.invalidate(); app.displayDraft(draft); app.preparationView.refreshForm()
            guard awaitCondition({ pending == 0 && !app.preparationView.busy }), var form = app.preparationView.form else { return report(false) }
            if let choice = (row["choose_recipe"] as? [String: Any]).flatMap(PreparationRecipeReference.init) {
                guard app.preparationView.acceptanceSelectExactRecipe(choice), awaitCondition({ pending == 0 && !app.preparationView.busy }),
                      let chosen = app.preparationView.form else { return report(false) }
                form = chosen
            }
            let expected = (row["expected_recipe"] as? [String: Any]).flatMap(PreparationRecipeReference.init)
            guard form.recipe == expected, Set(form.blockers) == Set(row["expected_blockers"] as! [String]),
                  expected != nil || !app.preparationView.acceptanceReviewEnabled else { return report(false) }
            formChecks += 1
        }
        stage = "complete"
        return report(errorCode == nil && context.current())
    }

    /// Runs the native form actions against an already launched, owner-only
    /// host. Backend execution remains governed by that host's factory gates.
    fileprivate static func preparationNativeAcceptance(fixture: URL) -> Bool {
        guard fixture.isFileURL, let data = try? Data(contentsOf: fixture), data.count <= 262144,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              Set(object.keys).isSubset(of: ["state_dir", "draft_id", "output_path", "answers_by_label", "candidate_id", "recipe_id", "capture_model_path", "capture_license_path", "capture_format"]),
              let state = preparationText(object["state_dir"], maximum: 4096), state.hasPrefix("/"),
              object["draft_id"] == nil || preparationUUID(object["draft_id"]) != nil,
              let output = preparationText(object["output_path"], maximum: 4096), output.hasPrefix("/"),
              !FileManager.default.fileExists(atPath: output),
              let answers = object["answers_by_label"] as? [String: Any], answers.count <= 256,
              preparationJSON(answers, maximum: 262144) != nil,
              object["candidate_id"] == nil || preparationDigest(object["candidate_id"]) != nil,
              object["recipe_id"] == nil || preparationText(object["recipe_id"], maximum: 128) != nil else { return false }
        let captured = object["capture_model_path"] != nil || object["capture_license_path"] != nil
        let modelPath = preparationText(object["capture_model_path"], maximum: 4096)
        let licensePath = preparationText(object["capture_license_path"], maximum: 4096)
        let modelFormat = object["capture_format"] as? String ?? "onnx"
        guard captured ? modelPath?.hasPrefix("/") == true && licensePath?.hasPrefix("/") == true && ["onnx", "gguf"].contains(modelFormat) : object["draft_id"] != nil && object["capture_format"] == nil else { return false }
        var draftID = preparationUUID(object["draft_id"])
        let prior = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", state, 1)
        defer { if let prior { setenv("MCPME_STATE_DIR", prior, 1) } else { unsetenv("MCPME_STATE_DIR") } }
        let app = McpmeApp(); _ = app.makeSetupWorkspace()
        var stage = "connection", routes: [String] = [], pending = 0
        var receipt: [String: Any] = ["schema_version": 1]
        if let draftID { receipt["draft_id"] = draftID }
        func report(_ passed: Bool) -> Bool {
            receipt["passed"] = passed; receipt["stage"] = stage; receipt["routes"] = routes
            if let code = app.artifactCaptureView.errorCode ?? app.artifactCaptureView.job?.error ?? app.preparationView.job?.error ?? app.preparationView.resolutionJob?.error { receipt["error_code"] = code }
            guard let result = try? JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys]),
                  result.count <= 262144,
                  (try? result.write(to: URL(fileURLWithPath: output), options: .atomic)) != nil,
                  (try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: output)) != nil else { return false }
            return passed
        }
        func awaitCondition(_ seconds: Double = 330, _ condition: () -> Bool) -> Bool {
            let deadline = Date().addingTimeInterval(seconds)
            while !condition(), Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
            return condition()
        }
        guard let connectionObject = PrivateFile.read(app.stateDirectory.appendingPathComponent("manager-connection.json")),
              let connection = ManagerConnection(connectionObject) else { return report(false) }
        app.nativeAcceptanceConnection = connection; app.hostAvailable = true
        let view = app.preparationView
        let acceptedRequest: PreparationRequest = { path, method, body, complete in
            let route = method + " " + path
            guard routes.contains(route) || routes.count < 512 else { complete(nil, "native_acceptance_route_limit"); return }
            if !routes.contains(route) { routes.append(route) }
            pending += 1
            app.request(path, method: method, body: body) { object, code in pending -= 1; complete(object, code) }
        }
        view.request = acceptedRequest; app.artifactCaptureView.request = acceptedRequest
        func fetchDraft() -> ImportDraft? {
            guard let draftID else { return nil }
            var completed = false, received: ImportDraft?
            app.request("v1/imports/" + draftID) { object, _ in received = object.flatMap(ImportDraft.init); completed = true }
            guard awaitCondition(330, { completed }) else { return nil }; return received
        }
        if captured {
            stage = "capture_selection"
            if draftID != nil {
                guard let draft = fetchDraft(), draft.editable, draft.packagePath == nil else { return report(false) }
                app.displayDraft(draft)
            } else {
                app.nameField?.stringValue = "Native captured model"
                app.intentField?.string = "Expose the selected model through its detected tool."
                app.startCheck?.state = .on; app.policyField?.selectItem(at: 1)
            }
            guard app.artifactCaptureView.select(model: URL(fileURLWithPath: modelPath!), license: URL(fileURLWithPath: licensePath!),
                                                format: modelFormat, supplement: draftID != nil) else { return report(false) }
            stage = "capture_start"; app.artifactCaptureView.startCapture()
            stage = "capture_poll"
            let deadline = Date().addingTimeInterval(360)
            while !app.artifactCaptureView.adopted, Date() < deadline {
                app.artifactCaptureView.poll()
                guard awaitCondition(330, { pending == 0 && !app.artifactCaptureView.busy }) else { return report(false) }
                if let job = app.artifactCaptureView.job, !job.active, job.state != "succeeded" { return report(false) }
                if app.artifactCaptureView.errorCode != nil { return report(false) }
                RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            }
            guard app.artifactCaptureView.adopted, let capture = app.artifactCaptureView.job,
                  capture.state == "succeeded", capture.settled, !capture.retained,
                  let identity = capture.importID, let revision = capture.revision, let digest = capture.digest,
                  app.currentDraft?.id == identity, app.currentDraft?.revision == revision else { return report(false) }
            draftID = identity; receipt["draft_id"] = identity
            receipt["capture_job_id"] = capture.id; receipt["capture_collection_digest"] = digest
            receipt["captured_revision"] = revision; receipt["capture_target"] = capture.target == nil ? "create-import" : "attach-to-import"
        }
        stage = "initial_draft"
        guard let draft = fetchDraft(), draft.editable, draft.packagePath == nil else { return report(false) }
        app.displayDraft(draft)
        stage = "initial_form"; view.refreshForm()
        guard awaitCondition(330, { pending == 0 && !view.busy }), view.form != nil else { return report(false) }
        if object["candidate_id"] != nil || object["recipe_id"] != nil {
            guard view.acceptanceSelect(candidateID: object["candidate_id"] as? String, recipeID: object["recipe_id"] as? String),
                  awaitCondition(330, { pending == 0 && !view.busy }), view.form != nil else { return report(false) }
        }
        guard let initial = view.form, let selectedCandidate = initial.candidateID else { return report(false) }
        receipt["candidate_id"] = selectedCandidate; receipt["initial_form_digest"] = initial.digest
        if initial.resolutionRequired {
            stage = "resolution_review"; view.reviewResolution()
            guard awaitCondition(330, { pending == 0 && !view.busy }), let review = view.resolutionReview, review.canStart() else { return report(false) }
            receipt["resolution_plan_id"] = review.planID; receipt["resolution_plan_digest"] = review.planDigest
            stage = "resolution_start"; view.startResolution()
            guard awaitCondition(330, { pending == 0 && !view.busy }), view.resolutionJob != nil else { return report(false) }
            stage = "resolution_poll"
            let deadline = Date().addingTimeInterval(Double(review.policy.wallSeconds) + 60)
            while view.resolutionJob?.active == true, Date() < deadline {
                view.poll(); guard awaitCondition(330, { pending == 0 && !view.busy }) else { return report(false) }
                if view.resolutionJob?.needsRecovery == true { stage = "resolution_recovery"; return report(false) }
                RunLoop.main.run(until: Date().addingTimeInterval(0.1))
            }
            guard let resolved = view.resolutionJob, resolved.state == "succeeded", !resolved.active,
                  let digest = resolved.resultDigest, view.form?.resolutionDigest == digest,
                  view.form?.resolutionRequired == false else { return report(false) }
            receipt["resolution_job_id"] = resolved.id; receipt["resolution_digest"] = digest; receipt["resolution_progress"] = resolved.progress
        }
        stage = "generated_answers"
        guard let generated = view.form, generated.candidateID == selectedCandidate, generated.recipe != nil,
              view.acceptanceAnswers(answers) else { return report(false) }
        receipt["generated_form_digest"] = generated.digest
        receipt["recipe_id"] = generated.recipe?.id; receipt["recipe_digest"] = generated.recipe?.digest
        stage = "preparation_review"; view.reviewForm()
        guard awaitCondition(330, { pending == 0 && !view.busy }), let review = view.review,
              review.canStart(), app.currentDraft?.revision == review.context.revision else { return report(false) }
        receipt["reviewed_revision"] = review.context.revision; receipt["preparation_plan_id"] = review.planID; receipt["preparation_plan_digest"] = review.planDigest
        stage = "preparation_start"; view.startPreparation()
        guard awaitCondition(330, { pending == 0 && !view.busy }), view.job != nil else { return report(false) }
        stage = "preparation_poll"
        let deadline = Date().addingTimeInterval(660)
        while view.job?.active == true, Date() < deadline {
            view.poll(); guard awaitCondition(330, { pending == 0 && !view.busy }) else { return report(false) }
            RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        }
        guard let job = view.job, job.state == "succeeded" else { return report(false) }
        var jobFetched = false, expectedPackageDigest: String?
        app.request("v1/preparations/" + job.id) { object, _ in
            expectedPackageDigest = preparationDigest(object?["expected_package_digest"]); jobFetched = true
        }
        guard awaitCondition(330, { jobFetched }), let expectedPackageDigest, let final = fetchDraft(),
              final.sourceRevision == draft.sourceRevision, final.revision == review.context.revision,
              final.packagePath != nil, final.packageDigest == expectedPackageDigest else { return report(false) }
        receipt["preparation_job_id"] = job.id; receipt["preparation_state"] = job.state; receipt["preparation_progress"] = job.progress
        receipt["attached_revision"] = final.revision; receipt["source_revision"] = final.sourceRevision
        receipt["attached_package_digest"] = expectedPackageDigest
        stage = "attached"; return report(true)
    }

    fileprivate static func captureAdoptionSelfTest() -> Bool {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("mcpme-capture-adoption-" + UUID().uuidString)
        guard (try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])) != nil else { return false }
        defer { try? FileManager.default.removeItem(at: directory) }
        let prior = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", directory.path, 1)
        defer { if let prior { setenv("MCPME_STATE_DIR", prior, 1) } else { unsetenv("MCPME_STATE_DIR") } }
        let id = UUID().uuidString.lowercased(), other = UUID().uuidString.lowercased()
        let original = "sha256:" + String(repeating: "b", count: 64), captured = "sha256:" + String(repeating: "a", count: 64)
        func draft(_ identity: String, _ revision: Int, _ source: String) -> [String: Any] {
            ["draft_id": identity, "revision": revision, "state": "waiting_for_agent", "name": "Synthetic captured model",
             "source": ["kind": "captured_artifact_collection", "revision": source], "intent": "Synthetic model tool",
             "start": false, "start_policy": "manual"]
        }
        let app = McpmeApp(); _ = app.makeSetupWorkspace()
        let target = ArtifactCaptureContext(importID: id, revision: 3, sourceRevision: original)
        let result = draft(id, 4, captured)
        app.currentDraft = ImportDraft(draft(other, 4, captured))
        guard app.artifactCaptureView.onAdopt?(result, target) == false, app.currentDraft?.id == other else { return false }
        app.currentDraft = ImportDraft(draft(id, 5, captured))
        guard app.artifactCaptureView.onAdopt?(result, target) == false else { return false }
        app.currentDraft = ImportDraft(draft(id, 3, captured))
        guard app.artifactCaptureView.onAdopt?(result, target) == false else { return false }
        app.currentDraft = ImportDraft(draft(id, 3, original))
        guard app.artifactCaptureView.onAdopt?(result, target) == true, app.currentDraft?.id == id,
              app.currentDraft?.revision == 4, app.currentDraft?.sourceRevision == captured else { return false }
        return app.artifactCaptureView.onAdopt?(result, target) == true
    }

    /// Exercises actual host form/review responses through the parent's draft callback.
    fileprivate static func preparationBindingSelfTest(fixture: URL?) -> Bool {
        guard let fixture else { return true }
        guard let data = try? Data(contentsOf: fixture), data.count <= 1_048_576,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return false }
        guard let formObject = object["form"] as? [String: Any], let reviewObject = object["review"] as? [String: Any] else { return true }
        guard let form = OnboardingFormV1(formObject), let reviewed = PreparationReview(reviewObject, form: form) else { return false }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("mcpme-native-form-test-" + UUID().uuidString, isDirectory: true)
        guard (try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])) != nil else { return false }
        let prior = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", directory.path, 1)
        defer {
            if let prior { setenv("MCPME_STATE_DIR", prior, 1) } else { unsetenv("MCPME_STATE_DIR") }
            try? FileManager.default.removeItem(at: directory)
        }
        let app = McpmeApp(); _ = app.makeSetupWorkspace()
        var original = reviewed.draft; original["revision"] = form.revision
        guard let draft = ImportDraft(original), draft.editable, draft.sourceRevision == form.sourceRevision else { return false }
        app.currentDraft = draft; app.refreshWindow()
        app.preparationView.available = { true }
        var calls: [String] = []
        app.preparationView.request = { path, method, _, complete in
            calls.append(path)
            guard method == "POST" else { complete(nil, "invalid_test_request"); return }
            if path == "v1/preparations/form" { complete(formObject, nil) }
            else if path == "v1/preparations/plan-form" { complete(reviewObject, nil) }
            else { complete(nil, "invalid_test_request") }
        }
        app.preparationView.refreshForm(); app.preparationView.reviewForm()
        return calls == ["v1/preparations/form", "v1/preparations/plan-form"]
            && app.currentDraft?.revision == reviewed.context.revision
            && app.preparationView.review?.context == reviewed.context
            && app.preparationView.review?.formDigest == form.digest
            && !app.preparationView.busy
    }

    /// Checks the first-app route and opt-in publisher controls without contacting a host.
    fileprivate static func onboardingControlsSelfTest() -> Bool {
        let app = McpmeApp()
        _ = app.makeSetupWorkspace(); app.refreshWindow()
        guard app.starterButton?.action == #selector(useStarterApp), app.starterButton?.isEnabled == false,
              app.publisherDownloadCard?.isHidden == true, app.downloadStartButton?.isEnabled == false else { return false }
        app.publisherDownloadToggle?.state = .on; app.togglePublisherDownload()
        guard app.publisherDownloadCard?.isHidden == false else { return false }
        app.publisherDownloadToggle?.state = .off
        app.downloadRows = [["download_id": "11111111-2222-4333-8444-555555555555"]]; app.refreshWindow()
        guard app.publisherDownloadCard?.isHidden == false && app.starterButton?.isEnabled == false else { return false }
        app.setupRequest = true; app.downloadsPolling = true; app.pollInFlight = true; app.importsPollInFlight = true
        app.detailRequests = ["stale"]; app.pendingAppRequests = ["stale"]
        app.resetHostRequestState()
        guard !app.setupRequest && !app.downloadsPolling && !app.pollInFlight && !app.importsPollInFlight
            && app.detailRequests.isEmpty && app.pendingAppRequests.isEmpty else { return false }
        let id = "11111111-2222-4333-8444-555555555555"
        let old: [String: Any] = ["summary": true, "draft_id": id, "revision": 1, "state": "package_received",
                                 "name": "Old name", "source": ["kind": "prepared_package"], "start": false, "start_policy": "manual"]
        var updated = old; updated["revision"] = 2; updated["name"] = "Saved name"
        guard let current = ImportDraft(updated), let stale = ImportDraft(old) else { return false }
        app.currentDraft = current; app.drafts = []; app.updateDraftPicker()
        guard app.draftField?.selectedItem?.representedObject as? String == id,
              app.draftField?.titleOfSelectedItem == "Saved name — Prepared package received" else { return false }
        app.drafts = [stale]; app.updateDraftPicker()
        guard app.draftField?.numberOfItems == 2,
              app.draftField?.titleOfSelectedItem == "Saved name — Prepared package received" else { return false }
        var equalRevision = updated; equalRevision["state"] = "waiting_for_agent"
        guard let staleState = ImportDraft(equalRevision) else { return false }
        app.drafts = [staleState]; app.updateDraftPicker()
        guard app.draftField?.titleOfSelectedItem == "Saved name — Prepared package received" else { return false }
        var duplicate = updated; duplicate["draft_id"] = "99999999-2222-4333-8444-555555555555"
        guard let duplicateDraft = ImportDraft(duplicate) else { return false }
        app.drafts = [duplicateDraft, current]; app.updateDraftPicker()
        guard app.draftField?.numberOfItems == 3,
              app.draftField?.selectedItem?.representedObject as? String == id,
              Set(app.draftField?.itemArray.compactMap { $0.representedObject as? String } ?? []).count == 2 else { return false }
        updated["revision"] = 3; updated["name"] = "Newer name"
        guard let newer = ImportDraft(updated) else { return false }
        app.drafts = [newer]; app.updateDraftPicker()
        return app.draftField?.selectedItem?.representedObject as? String == id
            && app.draftField?.titleOfSelectedItem == "Newer name — Prepared package received"
    }

    /// Verifies recovery button availability without starting a host or issuing requests.
    fileprivate static func recoveryControlsSelfTest() -> Bool {
        let app = McpmeApp()
        _ = app.makeSetupWorkspace()
        let projectID = "11111111-2222-4333-8444-555555555555"
        let operationID = "99999999-2222-4333-8444-555555555555"
        app.projects = [Project(["slot_id": projectID, "name": "Synthetic", "observed_state": "stopped",
                                "public_port": 19386, "start_policy": "manual", "generation": 1])!]
        app.hostAvailable = true; app.setupOperation = operationID
        for action in ["install", "upgrade"] {
            let fixture: [String: Any] = ["operation_id": operationID, "slot_id": projectID, "action": action,
                                          "status": "recovery_required", "details": ["exposed": true]]
            app.operations = [Operation(fixture)!]; app.refreshWindow()
            guard app.operationStopButton?.isHidden == false, app.operationStopButton?.isEnabled == true,
                  app.operationRecoverButton?.isEnabled == true, app.operationCancelButton?.isEnabled == false,
                  let projected = app.dashboardApps.first, projected.blocked,
                  !managementCanControl(projected, available: true) else { return false }
            app.pendingAppRequests.insert(projectID); app.refreshWindow()
            guard app.operationStopButton?.isEnabled == false, app.operationRecoverButton?.isEnabled == false else { return false }
            app.pendingAppRequests.remove(projectID)
            app.hostAvailable = false; app.refreshWindow()
            guard app.operationStopButton?.isEnabled == false else { return false }
            app.hostAvailable = true
            var unexposed = fixture; unexposed["details"] = ["exposed": false]
            app.operations = [Operation(unexposed)!]; app.refreshWindow()
            guard app.operationStopButton?.isHidden == true, app.operationStopButton?.isEnabled == false,
                  app.operationRecoverButton?.isEnabled == true else { return false }
        }
        app.operations = []; app.setupOperation = nil; app.refreshWindow()
        return app.operationStopButton?.isHidden == true && app.operationStopButton?.isEnabled == false
    }
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

if CommandLine.arguments.dropFirst().first == "--dashboard-ui-self-test" {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    application.finishLaunching()
    let passed = dashboardViewSelfTest()
    print(passed ? "dashboard_ui_contracts_passed" : "dashboard_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--management-ui-self-test" {
    _ = NSApplication.shared
    let passed = managementViewSelfTest() && setupControlsSelfTest() && McpmeApp.recoveryControlsSelfTest() && McpmeApp.onboardingControlsSelfTest() && preparationFormsSelfTest() && ArtifactCaptureView.selfTest() && McpmeApp.captureAdoptionSelfTest()
    print(passed ? "management_ui_contracts_passed" : "management_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--agent-preparation-ui-self-test" {
    _ = NSApplication.shared
    let passed = runAgentPreparationContractSelfTest()
    print(passed ? "agent_preparation_ui_contracts_passed" : "agent_preparation_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--artifact-capture-ui-self-test" {
    _ = NSApplication.shared
    let passed = ArtifactCaptureView.selfTest() && McpmeApp.captureAdoptionSelfTest()
    print(passed ? "artifact_capture_ui_contracts_passed" : "artifact_capture_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--preparation-ui-self-test" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count <= 2, arguments.count == 1 || arguments[1].hasPrefix("/") else { exit(2) }
    let fixture = arguments.count == 2 ? URL(fileURLWithPath: arguments[1]) : nil
    let passed = preparationFormsSelfTest(fixture: fixture) && McpmeApp.preparationBindingSelfTest(fixture: fixture)
    print(passed ? "preparation_ui_contracts_passed" : "preparation_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--preparation-native-acceptance" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count == 2, arguments[1].hasPrefix("/") else { exit(2) }
    let passed = McpmeApp.preparationNativeAcceptance(fixture: URL(fileURLWithPath: arguments[1]))
    print(passed ? "preparation_native_acceptance_passed" : "preparation_native_acceptance_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--recipes-native-acceptance" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count == 2, arguments[1].hasPrefix("/") else { exit(2) }
    let passed = McpmeApp.recipesNativeAcceptance(fixture: URL(fileURLWithPath: arguments[1]))
    print(passed ? "recipes_native_acceptance_passed" : "recipes_native_acceptance_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--recipes-native-context-self-test" {
    let passed = NativeRecipeAcceptanceContext.contractSelfTest()
    print(passed ? "recipes_native_context_passed" : "recipes_native_context_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--native-self-test" {
    let passed = nativeSelfTest(); print(passed ? "native_contracts_passed" : "native_contracts_failed"); exit(passed ? 0 : 1)
}

var cliArguments = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
if cliArguments == ["--ui-preview"] || cliArguments == ["--manage-preview"] { cliArguments = [] }
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
