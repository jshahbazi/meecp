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
    Bundle.main.object(forInfoDictionaryKey: "MeecpManagePreview") as? Bool == true
private let uiPreviewMode = managePreviewMode || CommandLine.arguments.contains("--ui-preview") ||
    Bundle.main.object(forInfoDictionaryKey: "MeecpUIPreview") as? Bool == true

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
    let authorizationPath = path.split(separator: "/")
    if authorizationPath.prefix(3) == ["v1", "authorization", "requests"] &&
        (method == "GET" && authorizationPath.count == 4 ||
         method == "POST" && authorizationPath.count == 5 && authorizationPath.last == "approve") { return 300 }
    if method == "POST" && path == "v1/pi/resources" { return 1800 }
    let preparesFiles = path == "v1/imports" || path == "v1/preparations/plan-form" || path == "v1/agent-preparations/plans" || path == "v1/agent-preparations/start" ||
        path == "v1/upgrade/apply" ||
        ["/plan", "/attach", "/engine", "/retry-install", "/resume"].contains { path.hasSuffix($0) }
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
              let path = source["locator"] as? String ?? ((summary || ["captured_artifact_collection", "service_connection"].contains(kind)) ? "" : nil),
              (summary || path.hasPrefix("/") || (["captured_artifact_collection", "service_connection"].contains(kind) && path.isEmpty)),
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

final class MeecpApp: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTextFieldDelegate, NSTextViewDelegate {
    private var statusItem: NSStatusItem!
    private var statusMenuTracking = false
    private var process: Process?
    // Assigned only by the explicit native acceptance command. The normal app
    // continues to require its own live host Process and matching connection.
    private var nativeAcceptanceConnection: ManagerConnection?
    // Only the scoped development driver assigns these fences. They also guard
    // the ordinary source-selection handler used by that driver.
    private var nativeAcceptanceScope: (() -> Bool)?
    private var nativeAcceptanceRoute: ((String, String) -> Bool)?
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
    private var savedSetupsRow: NSView?
    private var sourceOptionsStack: NSStackView?
    private var sourceOptionsExpanded = false
    private var briefSummary: NSTextField?
    private var pendingInstalledSelection: String?
    private var pendingAppRequests: Set<String> = []
    private var pathField: NSTextField?
    private var nameField: NSTextField?
    private var policyField: NSPopUpButton?
    private var startCheck: NSButton?
    private var reviewButton: NSButton?
    private var planDetailsButton: NSButton?
    private var installButton: NSButton?
    private var retryInstallationButton: NSButton?
    private let piResourcesView = PiResourcesView()
    private var capturingResourcesDraft = false
    private var showInstalledButton: NSButton?
    private var reviewText: NSTextView?
    private var setupLabel: NSTextField?
    private var chooseButton: NSButton?
    private var draftField: NSPopUpButton?
    private var intentField: NSTextView?
    private var documentRoleField: NSPopUpButton?
    private var newDraftButton: NSButton?
    private var detailsWindow: NSWindow?
    private let configurationView = SetupConfigurationView()
    private var setupSettings: [String: Any] = ["schema_version": 1, "configuration": [String: Any](), "secret_references": [String: String]()]
    private var secrets: [SetupSecret] = []
    private var upgradeTarget: String?
    private var appBriefCard: NSView?
    private var conversationView: AddAppConversationView?
    private var conversationComposerContext: PreparationContext?
    private var editingConversationRequest = false
    private var managementHeading: NSTextField?
    private var managementCaption: NSTextField?
    private var piSetupCard: NSView?
    private var preparedConfigurationCard: NSView?
    private var installationReviewCard: NSView?
    private var operationCancelButton: NSButton?
    private var operationRecoverButton: NSButton?
    private var operationStopButton: NSButton?
    private let piSetupView = PiSetupView()
    private let authorizationCoordinator = ExternalAuthorizationCoordinator()
    private var piSourceURL: NSTextField?
    private let stateDirectory: URL = {
        if let configured = ProcessInfo.processInfo.environment["MEECP_STATE_DIR"] ?? ProcessInfo.processInfo.environment["MCPME_STATE_DIR"], configured.hasPrefix("/") {
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
        let applicationItem = NSMenuItem(title: "meecp", action: nil, keyEquivalent: "")
        let applicationMenu = NSMenu(title: "meecp")
        let manage = NSMenuItem(title: "Manage Apps…", action: #selector(showManagement), keyEquivalent: "m")
        manage.target = self; applicationMenu.addItem(manage); applicationMenu.addItem(.separator())
        let connections = NSMenuItem(title: "Approved Connections…", action: #selector(showApprovedConnections), keyEquivalent: "")
        connections.target = self; applicationMenu.addItem(connections)
        let copyManagement = NSMenuItem(title: "Copy Management Connection", action: #selector(copyManagementConnection), keyEquivalent: "")
        copyManagement.target = self; applicationMenu.addItem(copyManagement); applicationMenu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit meecp", action: #selector(quitApp), keyEquivalent: "q")
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
        statusItem.button?.image = MeecpBrand.menuBarImage()
        if statusItem.button?.image == nil { statusItem.button?.title = "m" }
        statusItem.button?.target = self; statusItem.button?.action = #selector(handleStatusButton)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        configureDashboard(); configureAuthorization()
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
        alert.messageText = "Install meecp?"
        alert.informativeText = "Install in \(destination.deletingLastPathComponent().path). Any running copy of meecp will quit, stopping its services, and the installed copy will open. Your apps and settings are preserved."
        alert.addButton(withTitle: "Install and Restart"); alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { installing = false; NSApp.terminate(nil); return }
        let progress = NSAlert()
        progress.messageText = "Installing meecp…"
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
                    let failed = NSAlert(); failed.messageText = "meecp could not be installed"
                    failed.informativeText = "Installation stopped (\(error.rawValue)). " + (error == .recovery
                        ? "Installation needs recovery. A recovery copy may be in a hidden .meecp-install folder in \(destination.deletingLastPathComponent().path). Read that folder's RECOVERY.md before restoring it."
                        : "Your apps and settings were preserved. If Applications requires administrator access, quit meecp, drag the app to Applications, and open it there.")
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
        if authorizationCoordinator.state == .presenting || authorizationCoordinator.state == .submitting || authorizationPresentationBlocked { return false }
        // Reopening must not replace an active draft or interrupt a protected interaction.
        if window?.isVisible != true { showManagement() }
        authorizationCoordinator.poll(); return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        piSetupView.clearCredentialEntry()
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
        authorizationCoordinator.invalidate()
        pollInFlight = false; importsPollInFlight = false
        setupRequest = false; detailRequests = []; pendingAppRequests = []
        piSetupView.connectionReset()
        piResourcesView.connectionReset()
    }

    private func startHost() {
        guard !uiPreviewMode else { return }
        guard process == nil, !quitting, !stopping, let host = hostRoot else { return }
        let executable = host.appendingPathComponent("bin/meecp")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else { hostMessage = "Bundled host is unavailable"; rebuildMenu(); return }
        generation &+= 1; hostAvailable = false; resetHostRequestState()
        hostMessage = "Starting host…"; projects = []; operations = []
        let service = Process()
        service.executableURL = executable
        service.arguments = ["--state-dir", stateDirectory.path, "serve", "--reaper", host.appendingPathComponent("bin/meecp-reap").path,
                             "--parent-pid", String(getpid())]
        service.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TMPDIR": NSTemporaryDirectory()]
        service.currentDirectoryURL = host
        service.standardInput = FileHandle.nullDevice; service.standardOutput = FileHandle.nullDevice; service.standardError = FileHandle.nullDevice
        service.terminationHandler = { [weak self, weak service] process in
            DispatchQueue.main.async {
                guard let self = self, let service = service, self.process === service else { return }
                self.process = nil; self.generation &+= 1; self.resetHostRequestState()
                self.hostAvailable = false; self.projects = []; self.operations = []
                let completion = self.stopCompletion; self.stopCompletion = nil; self.stopping = false
                self.hostMessage = appHostExitMessage(process.terminationReason, process.terminationStatus)
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
        guard nativeAcceptanceScope?() != false, nativeAcceptanceRoute?(path, method) != false else {
            completion(nil, "native_acceptance_scope_stale"); return
        }
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
                guard self.nativeAcceptanceScope?() != false else {
                    completion(nil, "native_acceptance_scope_stale"); return
                }
                completion(object, object == nil ? code : nil)
            }
        }
    }

    private func poll() {
        guard !uiPreviewMode else { return }
        authorizationCoordinator.poll()
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
            self.rebuildMenu(); self.refreshWindow(); self.pollTrackedOperations(); self.pollImports(); self.piSetupView.poll(); self.piResourcesView.poll(); self.authorizationCoordinator.poll()
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
        dashboard.onVisibilityChange = { [weak self] _ in
            self?.updateStatusButtonSelection()
            // Status buttons clear their mouse-tracking highlight after the
            // action returns. Reapply the current selection on the next turn.
            DispatchQueue.main.async { [weak self] in self?.updateStatusButtonSelection() }
        }
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

    private var authorizationPresentationBlocked: Bool {
        installing || quitting || stopping || NSApp.modalWindow != nil ||
        NSApp.windows.contains(where: { $0.attachedSheet != nil }) ||
        piSetupView.authorizationPresentationBlocked ||
        NSApp.windows.contains(where: { window in
            guard window.isVisible, let editor = window.firstResponder as? NSTextView else { return false }
            return editor.delegate is NSSecureTextField
        })
    }

    private func configureAuthorization() {
        authorizationCoordinator.request = { [weak self] path, method, body, completion in
            self?.request(path, method: method, body: body, completion: completion)
        }
        authorizationCoordinator.available = { [weak self] in self?.hostAvailable == true && self?.quitting == false && self?.stopping == false }
        authorizationCoordinator.presentationBlocked = { [weak self] in self?.authorizationPresentationBlocked != false }
        authorizationCoordinator.onCopyConnection = { [weak self] in self?.copyManagementConnection() }
    }

    @objc private func showApprovedConnections() { authorizationCoordinator.showConnections() }

    @objc private func copyManagementConnection() {
        guard !uiPreviewMode, hostAvailable, !quitting, !stopping else { return }
        request("v1/authorization/profiles", method: "POST", body: ["label": "External management integration"]) { [weak self] object, code in
            guard let self else { return }
            guard let profile = object?["profile"] as? [String: Any], let id = profile["profile_id"] as? String, validUUID(id) else {
                self.showError(code ?? "configuration_unavailable"); return
            }
            let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/meecp").path
            let config: [String: Any] = ["mcpServers": ["meecp-management": ["command": executable,
                "args": ["--state-dir", self.stateDirectory.path, "management", "connect", "--profile", id]]]]
            guard let data = try? JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
                  let text = String(data: data, encoding: .utf8) else { return }
            let instructions = "Add meecp as a local stdio MCP server using the configuration below. Connect and request inspection and setup-planning access. Wait for me to approve the meecp popup, then list the installed apps to confirm the connection works."
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(instructions + "\n\n" + text, forType: .string)
        }
    }

    private func updateStatusButtonSelection() {
        let selected = dashboard.isVisible || statusMenuTracking
        statusItem.button?.state = selected ? .on : .off
        statusItem.button?.highlight(selected)
    }

    @objc private func handleStatusButton() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || (event?.type == .leftMouseUp && event?.modifierFlags.contains(.control) == true) {
            guard let button = statusItem.button else { return }
            statusMenuTracking = true
            dashboard.dismiss()
            button.state = .on
            button.highlight(true)
            defer {
                statusMenuTracking = false
                updateStatusButtonSelection()
                DispatchQueue.main.async { [weak self] in self?.updateStatusButtonSelection() }
            }
            let menu = NSMenu()
            let open = NSMenuItem(title: "Open", action: #selector(openDashboard), keyEquivalent: "")
            open.target = self; menu.addItem(open)
            let connections = NSMenuItem(title: "Approved Connections…", action: #selector(showApprovedConnections), keyEquivalent: "")
            connections.target = self; menu.addItem(connections)
            let copyManagement = NSMenuItem(title: "Copy Management Connection", action: #selector(copyManagementConnection), keyEquivalent: "")
            copyManagement.target = self; menu.addItem(copyManagement)
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
        let buttonFrame = window.convertToScreen(button.convert(button.bounds, to: nil))
        let anchor = dashboardHighlightAnchor(button: buttonFrame, statusItem: window.frame)
        dashboard.toggle(anchor: anchor, screen: screen, statusButtonFrame: buttonFrame)
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
        statusItem.button?.toolTip = "meecp: " + hostMessage
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
        selectWorkspace(0)
        window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); refreshWindow(); poll()
        if let id = management?.selectedID { loadDetails(id) }
    }

    @objc private func addProject() { showManagement(); selectWorkspace(1) }

    @objc private func changeWorkspace() { selectWorkspace(workspacePicker?.selectedSegment ?? 0) }

    private func selectWorkspace(_ index: Int) {
        workspacePicker?.selectedSegment = index
        managementTabs?.selectTabViewItem(at: index)
        window?.title = index == 1 ? "meecp — Add App" : "meecp — Manage Apps"
        managementHeading?.stringValue = index == 1 ? "Add App" : "Manage Apps"
        managementCaption?.stringValue = index == 1 ? "Work with an agent to prepare your app" : "Local tools, models, and connections"
        if index == 0, let id = management?.selectedID { loadDetails(id) }
        if index == 1 { window?.makeFirstResponder(intentField) }
    }

    private func makeManagementWindow() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 780),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "meecp — Manage Apps"; window.minSize = NSSize(width: 920, height: 640)
        window.isReleasedWhenClosed = false; window.delegate = self; window.center(); self.window = window
        let root = window.contentView!
        let brand = NSImageView(image: MeecpBrand.appIcon(size: 128))
        brand.setAccessibilityLabel("meecp")
        brand.widthAnchor.constraint(equalToConstant: 40).isActive = true
        brand.heightAnchor.constraint(equalToConstant: 40).isActive = true
        let headingTitle = workspaceLabel("Manage Apps", size: 23, weight: .semibold)
        let headingCaption = workspaceLabel("Local tools, models, and connections", size: 12, secondary: true)
        managementHeading = headingTitle; managementCaption = headingCaption
        let titles = workspaceStack([headingTitle, headingCaption], spacing: 3)
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
        if !uiPreviewMode {
            restoreSetup()
            // Startup polling may hydrate the draft before this lazy window
            // exists. Apply that authoritative record to the new controls;
            // an unchanged inventory will not fetch or display it again.
            if let draft = currentDraft { displayDraft(draft) }
            pollImports()
        }
        refreshWindow()
    }

    private func makeSetupWorkspace() -> NSView {
        // Retired alternative layouts are preserved in LegacyAddAppWorkflowReference.swift.

        let draftPicker = NSPopUpButton(); draftPicker.addItem(withTitle: "New draft")
        draftPicker.target = self; draftPicker.action = #selector(selectDraft)
        draftPicker.setAccessibilityLabel("Saved app draft"); self.draftField = draftPicker
        draftPicker.setContentHuggingPriority(.defaultLow, for: .horizontal)
        draftPicker.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let newDraft = button("Start New App", action: #selector(newDraft)); self.newDraftButton = newDraft
        let draftsRow = workspaceStack([workspaceLabel("Saved setups", size: 12, weight: .medium),
                                       draftPicker, newDraft], horizontal: true)
        self.savedSetupsRow = draftsRow
        let host = workspaceLabel("", size: 12, secondary: true); self.setupHostText = host

        let drop = SourceDropView(frame: .zero)
        drop.wantsLayer = true; drop.layer?.borderWidth = 1; drop.layer?.cornerRadius = 8
        drop.layer?.borderColor = NSColor.separatorColor.cgColor
        drop.acceptsSource = { [weak self] in self?.intakeEnabled == true }
        drop.selectedSource = { [weak self] url in self?.beginImport(url) }
        drop.heightAnchor.constraint(equalToConstant: 66).isActive = true
        let path = NSTextField(labelWithString: "")
        path.placeholderString = "Drop a source folder, app, or document here"
        path.isSelectable = true; path.lineBreakMode = .byTruncatingMiddle
        path.setAccessibilityLabel("Selected source")
        path.setContentHuggingPriority(.defaultLow, for: .horizontal)
        path.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        self.pathField = path
        let choose = button("Choose…", action: #selector(chooseSource)); self.chooseButton = choose
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
        let policy = NSPopUpButton(); policy.addItems(withTitles: ["Start manually", "Automatic when meecp opens"])
        policy.target = self; policy.action = #selector(setupChanged); policy.setAccessibilityLabel("New app startup"); self.policyField = policy
        let start = NSButton(checkboxWithTitle: "Start after install", target: self, action: #selector(setupChanged)); self.startCheck = start
        let nameRow = workspaceStack([workspaceLabel("App name", size: 12, weight: .medium), name], horizontal: true)
        name.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let startupRow = workspaceStack([policy, start], horizontal: true)
        let intentScroll = NSScrollView(); intentScroll.hasVerticalScroller = true; intentScroll.borderType = .bezelBorder
        let intent = AppBriefTextView(); intent.delegate = self; intent.font = .systemFont(ofSize: 14)
        intent.isRichText = false; intent.isAutomaticQuoteSubstitutionEnabled = false; intent.isAutomaticDashSubstitutionEnabled = false
        intent.isVerticallyResizable = true; intent.isHorizontallyResizable = false; intent.autoresizingMask = [.width]
        intent.textContainer?.widthTracksTextView = true; intent.textContainerInset = NSSize(width: 10, height: 10)
        intent.setAccessibilityLabel("Requested tools and behavior"); intentScroll.documentView = intent; self.intentField = intent
        intentScroll.heightAnchor.constraint(equalToConstant: 100).isActive = true
        let role = NSPopUpButton(); role.addItems(withTitles: ["Choose how to use this document", "Reference document (prepared package required)", "Use these instructions"])
        // Preserve the saved reference-role index without offering its retired
        // preparation path to new setups.
        role.menu?.autoenablesItems = false; role.item(at: 1)?.isEnabled = false
        role.target = self; role.action = #selector(setupChanged); role.setAccessibilityLabel("Document role"); self.documentRoleField = role
        let sourceURL = NSTextField(); sourceURL.placeholderString = "Project link (optional)"; sourceURL.delegate = self
        sourceURL.setAccessibilityLabel("Project link"); self.piSourceURL = sourceURL
        let sourceOptions = workspaceStack([drop, sourceURL, role], spacing: 10)
        self.sourceOptionsStack = sourceOptions
        for view in [drop, sourceURL, role] { workspaceFill(view, in: sourceOptions) }
        piSetupView.request = { [weak self] path, method, body, completion in
            self?.request(path, method: method, body: body, completion: completion)
        }
        piSetupView.available = { [weak self] in
            guard let self else { return false }
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting &&
                !self.piResourcesView.blocksNavigation
        }
        piSetupView.beforeStart = { [weak self] completion in
            guard let self else { completion(nil); return }
            if self.currentDraft == nil {
                self.createPiBrief { [weak self] saved in
                    guard let self, saved, let draft = self.currentDraft, let revision = draft.sourceRevision else { completion(nil); return }
                    completion(PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: revision))
                }
                return
            }
            self.saveCurrentDraft { [weak self] saved in
                guard let self, saved, let draft = self.currentDraft, draft.editable, draft.packagePath == nil,
                      !draft.intent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      let sourceRevision = draft.sourceRevision else { completion(nil); return }
                completion(PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: sourceRevision))
            }
        }
        piSetupView.onStateChange = { [weak self] in self?.refreshWindow() }
        piSetupView.onJobChange = { [weak self] id in
            guard let self else { return }
            if id != nil, !self.draftDirty,
               let job = self.piSetupView.job, !self.editingConversationRequest || job.active,
               let draft = self.currentDraft,
               job.importID == draft.id, job.revision == draft.revision, job.sourceRevision == draft.sourceRevision,
               let context = PreparationContext(importID: job.importID, revision: job.revision, sourceRevision: job.sourceRevision),
               self.conversationComposerContext != context {
                self.conversationComposerContext = context
                self.editingConversationRequest = false
                self.intentField?.string = ""
            } else if id == nil, self.conversationComposerContext != nil {
                self.conversationComposerContext = nil
                self.intentField?.string = self.currentDraft?.intent ?? ""
            }
            if let id { self.savedSetup["pi_job_id"] = id } else { self.savedSetup.removeValue(forKey: "pi_job_id") }
            self.persistSetup()
        }
        piSetupView.onReady = { [weak self] job in
            guard let self, !self.draftDirty, !self.setupRequest, let current = self.currentDraft,
                  current.id == job.importID, current.revision == job.revision, current.sourceRevision == job.sourceRevision else { return }
            self.setupRequest = true; self.setupLabel?.stringValue = "Loading the prepared app…"; self.refreshWindow()
            let expectedView = self.importViewGeneration
            self.request("v1/imports/" + job.importID) { [weak self] object, code in
                guard let self else { return }; self.setupRequest = false
                guard expectedView == self.importViewGeneration, self.currentDraft?.id == job.importID,
                      let object, let draft = ImportDraft(object), draft.id == job.importID, draft.revision == job.revision,
                      draft.sourceRevision == job.sourceRevision, draft.packagePath != nil else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "pi_prepared_app_unavailable"); self.refreshWindow(); return
                }
                self.displayDraft(draft); self.reviewPackage()
            }
        }
        piSetupCard = piSetupView
        piResourcesView.request = { [weak self] path, method, body, completion in self?.request(path, method: method, body: body, completion: completion) }
        piResourcesView.available = { [weak self] in
            guard let self else { return false }
            return self.hostAvailable && self.connection() != nil && !self.setupRequest && !self.stopping && !self.quitting &&
                (self.currentDraft != nil || !self.requestedAppIntent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) &&
                (self.currentDraft?.editable ?? true)
        }
        piResourcesView.beforeAttach = { [weak self] completion in
            guard let self else { completion(nil); return }
            if self.currentDraft == nil {
                self.capturingResourcesDraft = true
                self.createPiBrief { [weak self] saved in
                    guard let self else { completion(nil); return }
                    self.capturingResourcesDraft = false
                    guard saved, let draft = self.currentDraft, let source = draft.sourceRevision else { completion(nil); return }
                    completion(PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: source))
                }
            } else {
                self.saveCurrentDraft { [weak self] saved in
                    guard let self, saved, let draft = self.currentDraft, draft.editable, let source = draft.sourceRevision else { completion(nil); return }
                    completion(PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: source))
                }
            }
        }
        piResourcesView.onCaptured = { [weak self] object in
            guard let self, let draft = ImportDraft(object), let source = draft.sourceRevision else { return }
            guard let context = PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: source) else { return }
            self.piSetupView.adopt(nil, context: context)
            self.displayDraft(draft)
            if let job = object["job"] as? [String: Any] { self.piSetupView.adopt(job, context: context) }
            self.setupLabel?.stringValue = "Resources saved. MeeCP will reuse them during setup and retries."
        }
        piResourcesView.onStateChange = { [weak self] in self?.refreshWindow() }
        let conversation = AddAppConversationView(composer: intentScroll, setup: piSetupView,
            resources: piResourcesView, sourceOptions: sourceOptions)
        conversationView = conversation; appBriefCard = conversation
        conversation.onSend = { [weak self] text in self?.sendConversation(text) }
        conversation.onChooseSource = { [weak self] in self?.chooseSource() }
        conversation.onChoosePackage = { [weak self] in self?.choosePackage() }
        conversation.onSourceLink = { [weak self] in
            guard let self, self.intakeEnabled else { return }
            self.sourceOptionsExpanded = true; self.refreshWindow(); self.window?.makeFirstResponder(self.piSourceURL)
        }
        conversation.onEditRequest = { [weak self] in self?.editConversationRequest() }

        configurationView.onChange = { [weak self] in self?.setupChanged() }
        configurationView.onCreateSecret = { [weak self] name in self?.createSecret(name) }
        let configureCard = workspaceCard("App settings", subtitle: "Choose the settings and credentials your app needs.", views: [configurationView])
        preparedConfigurationCard = configureCard

        let label = workspaceLabel("", size: 13); self.setupLabel = label
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
        let retryInstallation = button("Retry Installation", action: #selector(retryInstallation)); self.retryInstallationButton = retryInstallation
        install.bezelColor = .controlAccentColor
        let details = button("Package and Verification Details…", action: #selector(showPlanDetails)); self.planDetailsButton = details
        let actions = workspaceStack([review, install, retryInstallation, details], horizontal: true, spacing: 8)
        let showInstalled = button("Open Installed App", action: #selector(showInstalledApp))
        self.showInstalledButton = showInstalled
        let cancelOperation = button("Cancel Operation", action: #selector(cancelSetupOperation)); self.operationCancelButton = cancelOperation
        let recoverOperation = button("Recover Candidate", action: #selector(recoverSetupOperation)); self.operationRecoverButton = recoverOperation
        let stopOperation = button("Stop Candidate", action: #selector(stopSetupOperation)); self.operationStopButton = stopOperation
        stopOperation.toolTip = "Stop the exposed candidate and preserve its saved data while recovery remains required"
        let briefSummary = workspaceLabel("", size: 12, secondary: true)
        briefSummary.isSelectable = true; briefSummary.maximumNumberOfLines = 3; briefSummary.lineBreakMode = .byTruncatingTail
        briefSummary.setAccessibilityLabel("Requested app behavior"); self.briefSummary = briefSummary
        let reviewCard = workspaceCard("Ready to add", subtitle: "Review your app's tools and access, then choose when it should run.",
                                       views: [briefSummary, nameRow, startupRow, reviewScroll, actions, workspaceStack([cancelOperation, stopOperation, recoverOperation], horizontal: true), showInstalled])
        installationReviewCard = reviewCard
        for card in [label, configureCard, reviewCard] {
            conversation.reviewContainer.addArrangedSubview(card)
            workspaceFill(card, in: conversation.reviewContainer)
        }
        for view in [draftsRow, host] {
            conversation.contextContainer.addArrangedSubview(view)
            workspaceFill(view, in: conversation.contextContainer)
        }
        return conversation
    }

    func controlTextDidChange(_ notification: Notification) { setupChanged() }
    func textDidChange(_ notification: Notification) {
        if usesConversationReplyComposer, notification.object as? NSTextView === intentField {
            conversationView?.refresh()
            return
        }
        setupChanged()
    }

    /// The draft owns the request. Conversation text must never revise it or
    /// enter the native setup preferences as a replacement brief.
    private var usesConversationReplyComposer: Bool {
        guard let draft = currentDraft,
              let source = draft.sourceRevision,
              let context = PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: source) else { return false }
        return conversationComposerContext == context
    }

    private var requestedAppIntent: String {
        usesConversationReplyComposer ? currentDraft?.intent ?? "" : intentField?.string ?? currentDraft?.intent ?? ""
    }

    private func sendConversation(_ text: String) {
        if piSetupView.job?.active == true {
            let expected = conversationComposerContext
            let jobID = piSetupView.job?.id
            piSetupView.submitText(text) { [weak self] accepted in
                guard let self, accepted, self.conversationComposerContext == expected,
                      self.piSetupView.job?.id == jobID, self.intentField?.string == text else { return }
                self.intentField?.string = ""; self.conversationView?.refresh()
            }
        } else {
            piSetupView.startSetup()
        }
    }

    private func editConversationRequest() {
        guard intakeEnabled, let draft = currentDraft, draft.editable, draft.packagePath == nil else { return }
        conversationComposerContext = nil; intentField?.string = draft.intent
        editingConversationRequest = true
        refreshWindow(); window?.makeFirstResponder(intentField)
    }

    private func refreshActionButtons() {
        management?.update(apps: dashboardApps, hostMessage: hostMessage,
                           available: hostAvailable && !stopping && !quitting,
                           operations: operations.map { ManagementOperation(appID: $0.projectID, title: $0.label, status: $0.status, action: $0.action) })
    }

    private func refreshWindow() {
        let preparationContext = currentDraft.flatMap { draft in
            draft.sourceRevision.flatMap { PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: $0) }
        }
        let piEligible = currentDraft.map { draft in
            draft.editable && draft.packagePath == nil &&
            (["local_folder", "application_directory"].contains(draft.sourceKind) || draft.isDocument && selectedDocumentRole == "instructions") &&
            !requestedAppIntent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        } ?? !requestedAppIntent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        piSetupView.bind(preparationContext, eligible: piEligible); piSetupView.refreshAvailability()
        piResourcesView.bind(preparationContext); piResourcesView.refreshAvailability()
        refreshActionButtons()
        if let id = pendingInstalledSelection, projects.contains(where: { $0.id == id }), management != nil {
            pendingInstalledSelection = nil; management?.select(id: id)
        }
        setupHostText?.stringValue = uiPreviewMode ? "Preview · setup actions are disabled." :
            (hostAvailable ? "" : "Waiting for meecp to connect. Your saved setup is kept.")
        setupHostText?.isHidden = !uiPreviewMode && hostAvailable
        setupLabel?.isHidden = setupLabel?.stringValue.isEmpty != false
        if let draft = currentDraft, draft.packagePath == nil, canPrepareWithAgent(draft),
           setupLabel?.stringValue == draftStatus(draft) { setupLabel?.isHidden = true }
        let brief = requestedAppIntent.trimmingCharacters(in: .whitespacesAndNewlines)
        briefSummary?.stringValue = brief.isEmpty ? "" : "Requested: " + brief
        briefSummary?.isHidden = brief.isEmpty
        savedSetupsRow?.isHidden = drafts.isEmpty && currentDraft == nil
        let sourcePath = pathField?.stringValue ?? ""
        let showsSource = sourceOptionsExpanded
        sourceOptionsStack?.isHidden = !showsSource
        let operation = setupOperation.flatMap { id in operations.first { $0.id == id } }
        let active = operation.map { $0.active || $0.status == "recovery_required" } ?? (setupOperation != nil)
        let enabled = process?.isRunning == true && connection() != nil && !stopping && !quitting && !setupRequest && !active
        let editable = enabled && (currentDraft?.editable ?? true) && !piSetupView.blocksNavigation && !piResourcesView.blocksNavigation
        chooseButton?.isEnabled = intakeEnabled; newDraftButton?.isEnabled = intakeEnabled
        piSourceURL?.isEditable = intakeEnabled && currentDraft == nil
        appBriefCard?.isHidden = false
        piSetupCard?.isHidden = upgradeTarget != nil
        preparedConfigurationCard?.isHidden = currentDraft?.attachment == nil && setupPlan == nil
        installationReviewCard?.isHidden = currentDraft?.packagePath == nil && setupPlan == nil && setupOperation == nil
        draftField?.isEnabled = intakeEnabled
        nameField?.isEnabled = editable; policyField?.isEnabled = editable; startCheck?.isEnabled = editable
        if upgradeTarget != nil { nameField?.isEnabled = false; policyField?.isEnabled = false; startCheck?.isEnabled = false }
        configurationView.setEditable(editable)
        if usesConversationReplyComposer {
            intentField?.isEditable = !uiPreviewMode && hostAvailable && !setupRequest && !stopping && !quitting &&
                piSetupView.job?.active == true && piSetupView.currentQuestionKind != "secret" && piSetupView.composerEnabled
        } else { intentField?.isEditable = editable && !piSetupView.authorizationPresentationBlocked }
        documentRoleField?.isEnabled = editable && currentDraft?.isDocument == true
        documentRoleField?.isHidden = currentDraft?.isDocument != true || isDescriptionDraft(currentDraft)
        reviewButton?.isEnabled = editable && (currentDraft?.packagePath != nil || currentDraft == nil && !(pathField?.stringValue.isEmpty ?? true))
        reviewButton?.title = setupPlan == nil ? "Review App" : "Update Review"
        reviewButton?.isHidden = setupPlan != nil && !draftDirty
        installButton?.isEnabled = enabled && setupPlan != nil && setupOperation == nil && !draftDirty
        let installationRetry = operation?.action == "install" && ["failed", "canceled"].contains(operation?.status ?? "") && currentDraft?.attachment != nil
        retryInstallationButton?.isHidden = !installationRetry
        retryInstallationButton?.isEnabled = enabled && !setupRequest && installationRetry
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
        showInstalledButton?.isHidden = installedSetupAppID == nil
        let prepared = currentDraft?.packagePath != nil || setupPlan != nil || setupOperation != nil || upgradeTarget != nil
        conversationView?.update(title: currentDraft?.name ?? "Add an app", intent: requestedAppIntent,
            source: sourcePath.isEmpty ? nil : sourcePath, context: preparationContext,
            installationStatus: operation?.label ?? (prepared && setupLabel?.isHidden == false ? setupLabel?.stringValue : nil),
            canEditRequest: intakeEnabled && currentDraft?.editable == true && !prepared,
            prepared: prepared, readOnly: !enabled || prepared)
        conversationView?.setComposerEnabled(!prepared && !requiresNewAgentDraft && piSetupView.composerEnabled)
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
        !uiPreviewMode && !setupRequest && !stopping && !quitting && !piSetupView.blocksNavigation && !piResourcesView.blocksNavigation && connection() != nil
    }

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
            alert.informativeText = "Give this secret a display name. meecp stores the value privately and gives this app an opaque reference."
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
        let root = URL(fileURLWithPath: path, isDirectory: true)
        let primary = root.appendingPathComponent("meecp.package.json")
        let file = FileManager.default.fileExists(atPath: primary.path) ? primary : root.appendingPathComponent("mcpme.package.json")
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
        alert.informativeText = "This stops the app and removes it from meecp. Its assistant connections will stop working. Source files and saved app data are kept."
        alert.addButton(withTitle: "Remove App"); alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.control(project.id, action: "remove") }
        }
    }

    private func copyConfiguration(_ id: String) {
        guard !uiPreviewMode else { management?.connectionCopied(id: id); dashboard.connectionCopied(id: id); return }
        guard let project = projects.first(where: { $0.id == id }) else { showError("configuration_unavailable"); return }
        let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/meecp").path
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
        let scenario = ProcessInfo.processInfo.environment["MEECP_PREVIEW_SCENARIO"] ??
            Bundle.main.object(forInfoDictionaryKey: "MeecpPreviewScenario") as? String ?? "standard"
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
        piSetupView.clearCredentialEntry()
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

    private func isDescriptionDraft(_ draft: ImportDraft?) -> Bool {
        guard let draft, draft.isDocument else { return false }
        return draft.sourcePath.hasPrefix(stateDirectory.appendingPathComponent("pi-setup/briefs").path + "/")
    }

    private func pollImports() {
        guard !uiPreviewMode else { return }
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
        let changedApp = currentDraft?.id != draft.id
        let sameComposer = conversationComposerContext?.importID == draft.id &&
            conversationComposerContext?.revision == draft.revision && conversationComposerContext?.sourceRevision == draft.sourceRevision
        if !sameComposer { conversationComposerContext = nil }
        if changedApp { editingConversationRequest = false }
        if !isDescriptionDraft(draft) || currentDraft != nil && changedApp { piSourceURL?.stringValue = "" }
        currentDraft = draft; draftDirty = false
        upgradeTarget = nil
        pathField?.stringValue = isDescriptionDraft(draft) ? "" : draft.sourceKind == "service_connection" ? "Local service connection" : draft.sourceKind == "captured_artifact_collection" ? "Captured artifacts" : draft.sourcePath
        if changedApp && !(pathField?.stringValue.isEmpty ?? true) { sourceOptionsExpanded = true }
        nameField?.stringValue = draft.name
        if !sameComposer { intentField?.string = draft.intent }
        policyField?.selectItem(at: draft.policy == "host" ? 1 : 0)
        startCheck?.state = draft.start ? .on : .off
        documentRoleField?.selectItem(at: draft.documentRole == "reference" ? 1 : draft.documentRole == "instructions" ? 2 : 0)
        if draft.isDocument && !isDescriptionDraft(draft) && draft.documentRole == nil && draft.packagePath == nil {
            sourceOptionsExpanded = true
            conversationView?.revealSourceOptions()
        }
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

    private func canPrepareWithAgent(_ draft: ImportDraft) -> Bool {
        ["local_folder", "application_directory"].contains(draft.sourceKind) ||
            draft.isDocument && draft.documentRole == "instructions"
    }

    private var requiresNewAgentDraft: Bool {
        guard let draft = currentDraft, draft.editable, draft.packagePath == nil else { return false }
        return !["local_folder", "application_directory", "document"].contains(draft.sourceKind) ||
            draft.isDocument && selectedDocumentRole == "reference"
    }

    private func draftStatus(_ draft: ImportDraft) -> String {
        if draft.editable, draft.packagePath == nil,
           !["local_folder", "application_directory", "document"].contains(draft.sourceKind) ||
            draft.isDocument && draft.documentRole == "reference" {
            return "This saved source needs a retired setup workflow. Choose Start New App with a description, or import a prepared package from Add…."
        }
        if draft.editable, draft.packagePath == nil,
           ["local_folder", "application_directory"].contains(draft.sourceKind) || draft.isDocument && draft.documentRole == "instructions" {
            return draft.intent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "Describe the tools you want, then choose Create app."
                : "Choose Create app to prepare this source with the MeeCP agent."
        }
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
        conversationComposerContext = nil; editingConversationRequest = false
        upgradeTarget = nil; setupSettings = ["schema_version": 1, "configuration": [String: Any](), "secret_references": [String: String]()]
        configurationView.configure(package: nil, settings: setupSettings)
        pathField?.stringValue = ""; nameField?.stringValue = ""; intentField?.string = ""; piSourceURL?.stringValue = ""
        policyField?.selectItem(at: 0); startCheck?.state = .off; documentRoleField?.selectItem(at: 0)
        sourceOptionsExpanded = false
        reviewText?.string = ""; setupLabel?.stringValue = ""
        savedSetup.removeValue(forKey: "import_id"); updateDraftPicker(); persistSetup(); refreshWindow()
        window?.makeFirstResponder(intentField)
    }

    @objc private func chooseSource() {
        guard intakeEnabled, let window = window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [UTType(filenameExtension: "md")!, .plainText, .applicationBundle]
        panel.message = "Choose a source folder, app, prepared package, or text document. Add model files using Model or runtime files…"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let url = panel.url else { return }; self?.beginImport(url)
        }
    }

    private func createPiBrief(completion: @escaping (Bool) -> Void) {
        guard intakeEnabled || capturingResourcesDraft && hostAvailable && connection() != nil && !setupRequest && !stopping && !quitting,
              currentDraft == nil else { completion(false); return }
        let intent = requestedAppIntent.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !intent.isEmpty else { completion(false); return }
        let name = (nameField?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let sourceURL = (piSourceURL?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        setupRequest = true; setupLabel?.stringValue = "Saving app description…"; refreshWindow()
        request("v1/pi/imports", method: "POST", body: ["name": name.isEmpty ? "New App" : name,
                "intent": intent, "source_url": sourceURL, "start": startCheck?.state == .on, "start_policy": selectedPolicy]) { [weak self] object, code in
            guard let self else { completion(false); return }
            self.setupRequest = false
            guard let object, let draft = ImportDraft(object) else {
                self.setupLabel?.stringValue = self.draftError(code ?? "invalid_draft"); self.refreshWindow(); completion(false); return
            }
            self.drafts.insert(draft, at: 0); self.displayDraft(draft); completion(true)
        }
    }

    private func modelFileNeedsResources(_ source: URL) -> Bool {
        guard source.isFileURL, ["gguf", "onnx", "safetensors"].contains(source.pathExtension.lowercased()) else { return false }
        return (try? source.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory != true
    }

    private func beginImport(_ source: URL, name suppliedName: String? = nil) {
        guard intakeEnabled else { return }
        if modelFileNeedsResources(source) {
            setupLabel?.stringValue = "Describe your app, then use Add… and Model or runtime files… to attach this model."
            refreshWindow(); return
        }
        // Attaching source after writing a brief must keep the user's request.
        let initialIntent = requestedAppIntent
        saveDraftBeforeNavigation { [weak self] saved in
            guard let self = self, saved else { return }
            let directory = (try? source.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true
            let name = suppliedName ?? (directory ? source.lastPathComponent : source.deletingPathExtension().lastPathComponent)
            self.setupRequest = true; self.setupLabel?.stringValue = "Inspecting selected source…"; self.refreshWindow()
            self.request("v1/imports", method: "POST", body: ["source": source.path, "source_mode": "automatic", "name": name,
                    "intent": initialIntent, "start": false, "start_policy": "manual"]) { [weak self] object, code in
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
        guard intakeEnabled, currentDraft?.editable ?? true, let window = window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.message = "Choose a prepared meecp package"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url else { return }
            if self.currentDraft == nil { self.beginImport(url); return }
            self.saveCurrentDraft { [weak self] saved in
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
        else { setupLabel?.stringValue = "" }
        persistSetup(); refreshWindow()
    }

    /* Retired Manual setup toolbar action. Shared draft saving remains active
       through saveCurrentDraft and the agent's beforeStart callback.
    @objc private func saveDraft() {
        if currentDraft == nil { createPiBrief { _ in } }
        else { saveCurrentDraft { _ in } }
    }
    */

    private func hasDraftChanges(_ draft: ImportDraft) -> Bool {
        (nameField?.stringValue ?? draft.name) != draft.name || requestedAppIntent != draft.intent ||
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
                                      "intent": requestedAppIntent, "start": startCheck?.state == .on, "start_policy": selectedPolicy]
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
            if requestedAppIntent == draft.intent && selectedDocumentRole == draft.documentRole {
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
        guard !piSetupView.blocksNavigation else {
            updateDraftPicker(); completion(false); return
        }
        saveCurrentDraft { [weak self] saved in
            guard let self, !self.piSetupView.blocksNavigation else { completion(false); return }
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
                      !self.piSetupView.blocksNavigation else { completion(false); return }
                guard response == .alertSecondButtonReturn else {
                    self.updateDraftPicker(); completion(false); return
                }
                self.displayDraft(draft); completion(true)
            }
        }
    }

    @objc private func reviewPackage() {
        preparePackageReview(installWhenReady: false)
    }

    private func preparePackageReview(installWhenReady: Bool) {
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
            let expectedDraft = self.currentDraft
            self.request(self.upgradeTarget == nil ? "v1/install/plan" : "v1/upgrade/plan", method: "POST", body: payload) { [weak self] object, code in
                guard let self = self else { return }; self.setupRequest = false
                guard let object = object, let id = object["plan_id"] as? String, validUUID(id), object["package"] is [String: Any] else {
                    self.setupLabel?.stringValue = self.draftError(code ?? "invalid_plan"); self.refreshWindow(); return
                }
                self.setupPlan = object; self.setupOperation = nil; self.reviewText?.string = self.reviewSummary(object)
                self.setupLabel?.stringValue = self.upgradeTarget == nil ? "Review the tools and declared access. Add App applies this exact plan." : "Review the replacement tools, configuration, and data policy. Apply Reviewed Upgrade applies this exact plan."
                self.persistSetup(); self.refreshWindow(); self.pollImports()
                if installWhenReady, self.upgradeTarget == nil, let expectedDraft,
                   self.currentDraft?.id == expectedDraft.id, self.currentDraft?.revision == expectedDraft.revision,
                   object["package_digest"] as? String == expectedDraft.packageDigest {
                    self.installPackage()
                }
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

    @objc private func retryInstallation() {
        guard !setupRequest, let draft = currentDraft, let operation = setupOperation.flatMap({ id in operations.first { $0.id == id } }),
              operation.action == "install", ["failed", "canceled"].contains(operation.status), draft.attachment != nil else { return }
        setupRequest = true; setupLabel?.stringValue = "Reusing your verified app for another installation…"; refreshWindow()
        let expected = importViewGeneration
        request("v1/imports/" + draft.id + "/retry-install", method: "POST", body: ["revision": draft.revision]) { [weak self] object, code in
            guard let self else { return }; self.setupRequest = false
            guard expected == self.importViewGeneration, self.currentDraft?.id == draft.id,
                  let object, let retry = ImportDraft(object), retry.packageDigest == draft.packageDigest,
                  let source = retry.sourceRevision,
                  let context = PreparationContext(importID: retry.id, revision: retry.revision, sourceRevision: source) else {
                self.setupLabel?.stringValue = self.draftError(code ?? "installation_retry_unavailable"); self.refreshWindow(); return
            }
            self.piSetupView.adopt(nil, context: context)
            self.displayDraft(retry); self.preparePackageReview(installWhenReady: true)
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
        sections.append("Later sessions: " + (policy == "host" ? "Automatic — when meecp opens" : "Start manually"))
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
        if let id = savedSetup["download_id"] as? String, validUUID(id) { object["download_id"] = id }
        if let id = savedSetup["capture_id"] as? String, validUUID(id) { object["capture_id"] = id }
        if let id = savedSetup["pi_job_id"] as? String, validUUID(id) { object["pi_job_id"] = id }
        if let choices = try? configurationView.settings() { object["settings"] = choices }
        PrivateFile.write(object, to: setupFile)
        savedSetup = object
    }

    private func restoreSetup() {
        guard !uiPreviewMode else { return }
        guard let object = PrivateFile.read(setupFile), integer(object["schema_version"]) == 1 else { return }
        savedSetup = object
        if let id = preparationUUID(object["pi_job_id"]) { piSetupView.restore(id) }
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
        let alert = NSAlert(); alert.messageText = "meecp"; alert.informativeText = "The request could not complete (\(safeCode(code) ?? "request_failed")). Check app status."
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
          managementRequestTimeout("v1/upgrade/apply", method: "POST") == 300,
          managementRequestTimeout("v1/authorization/requests/" + id, method: "GET") == 300,
          managementRequestTimeout("v1/authorization/requests/" + id + "/approve", method: "POST") == 300,
          managementRequestTimeout("v1/authorization/requests", method: "GET") == 30,
          managementRequestTimeout("v1/authorization/requests/" + id + "/deny", method: "POST") == 30,
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
    let exported = connectionConfiguration(name: "synthetic", id: id, executable: "/Applications/meecp.app/Contents/MacOS/meecp", stateDirectory: "/tmp/state")
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
                  detail.minX > frames.menu.maxX || detail.maxX < frames.menu.minX,
                  frames.menu == dashboardFrames(anchor: anchor, screen: screen, showsDetails: false).menu else { return false }
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

private struct NativeDirectoryAcceptanceContext {
    let fixture: URL
    let state: URL
    let output: URL
    let source: URL
    let nonce: String
    let marker: URL
    let expiry: Int
    let fixtureIdentity: NativeRecipeFileIdentity
    let markerIdentity: NativeRecipeFileIdentity
    let managerIdentity: NativeRecipeFileIdentity
    let rootIdentity: NativeRecipeDirectoryIdentity
    let stateIdentity: NativeRecipeDirectoryIdentity
    let workspaceIdentity: NativeRecipeDirectoryIdentity
    let sourceIdentity: NativeRecipeDirectoryIdentity
    let object: [String: Any]
    let publicPython: Bool

    init?(fixture: URL, publicPython: Bool = false) {
        guard let fixtureIdentity = NativeRecipeFileIdentity(fixture),
              let object = PrivateFile.read(fixture, limit: 262144),
              Set(object.keys) == ["schema_version", "state_dir", "output_path", "source_directory", "fixture_nonce", "answers_by_label"],
              preparationInteger(object["schema_version"]) == 1,
              let nonce = preparationUUID(object["fixture_nonce"]),
              let statePath = preparationText(object["state_dir"], maximum: 4096),
              let outputPath = preparationText(object["output_path"], maximum: 4096),
              let sourcePath = preparationText(object["source_directory"], maximum: 4096),
              let answers = object["answers_by_label"] as? [String: Any], answers.count <= 256,
              preparationJSON(answers, maximum: 262144) != nil else { return nil }
        let state = URL(fileURLWithPath: statePath), output = URL(fileURLWithPath: outputPath), source = URL(fileURLWithPath: sourcePath)
        let root = fixture.deletingLastPathComponent(), workspace = root.deletingLastPathComponent()
        var outputInfo = stat()
        guard workspace.lastPathComponent.hasPrefix("meecp-native-onboarding-"),
              let workspaceIdentity = NativeRecipeDirectoryIdentity(workspace), let rootIdentity = NativeRecipeDirectoryIdentity(root),
              state.path == root.appendingPathComponent("state").path, let stateIdentity = NativeRecipeDirectoryIdentity(state),
              source.deletingLastPathComponent().path == root.path, source.path != state.path,
              let sourceIdentity = NativeRecipeDirectoryIdentity(source),
              output.deletingLastPathComponent().path == root.path, output.lastPathComponent.count <= 128,
              output.path != fixture.path, lstat(output.path, &outputInfo) != 0, errno == ENOENT else { return nil }
        let marker = state.appendingPathComponent((publicPython ? "native-public-directory-" : "native-directory-") + nonce + ".json")
        guard let markerIdentity = NativeRecipeFileIdentity(marker), let proof = PrivateFile.read(marker, limit: 8192),
              Set(proof.keys) == ["schema_version", "fixture_nonce", "expires_at", "manager_file", "source_directory"],
              preparationInteger(proof["schema_version"]) == 1, preparationUUID(proof["fixture_nonce"]) == nonce,
              let expiry = preparationInteger(proof["expires_at"]), expiry > Int(Date().timeIntervalSince1970), expiry <= Int(Date().timeIntervalSince1970) + 300,
              let expectedManager = proof["manager_file"] as? [String: Any],
              let managerIdentity = NativeRecipeFileIdentity(state.appendingPathComponent("manager-connection.json")),
              managerIdentity.matches(expectedManager), let expectedSource = proof["source_directory"] as? [String: Any],
              Set(expectedSource.keys) == ["device", "inode"],
              preparationInteger(expectedSource["device"]) == Int(sourceIdentity.device),
              preparationInteger(expectedSource["inode"]) == Int(sourceIdentity.inode),
              NativeRecipeFileIdentity(fixture) == fixtureIdentity, NativeRecipeFileIdentity(marker) == markerIdentity else { return nil }
        self.fixture = fixture; self.state = state; self.output = output; self.source = source; self.nonce = nonce
        self.marker = marker; self.expiry = expiry; self.fixtureIdentity = fixtureIdentity; self.markerIdentity = markerIdentity
        self.managerIdentity = managerIdentity; self.rootIdentity = rootIdentity; self.stateIdentity = stateIdentity
        self.workspaceIdentity = workspaceIdentity; self.sourceIdentity = sourceIdentity; self.object = object
        self.publicPython = publicPython
    }

    func current() -> Bool {
        Int(Date().timeIntervalSince1970) < expiry && NativeRecipeDirectoryIdentity(state) == stateIdentity &&
            NativeRecipeDirectoryIdentity(fixture.deletingLastPathComponent()) == rootIdentity &&
            NativeRecipeDirectoryIdentity(fixture.deletingLastPathComponent().deletingLastPathComponent()) == workspaceIdentity &&
            NativeRecipeDirectoryIdentity(source) == sourceIdentity && NativeRecipeFileIdentity(fixture) == fixtureIdentity &&
            NativeRecipeFileIdentity(marker) == markerIdentity && NativeRecipeFileIdentity(state.appendingPathComponent("manager-connection.json")) == managerIdentity
    }

    func claim() -> Bool {
        guard current() else { return false }
        let path = marker.deletingPathExtension().appendingPathExtension("claimed")
        let fd = open(path.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }; defer { close(fd) }
        let bytes = Array("claimed\n".utf8)
        return bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) == $0.count } && fsync(fd) == 0 && current()
    }

    func report(_ receipt: [String: Any]) -> Bool {
        guard current(), let data = try? JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys]), data.count <= 262144 else { return false }
        let fd = open(output.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }; defer { close(fd) }
        return data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) == $0.count } && fsync(fd) == 0 && current()
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

    init?(fixture: URL, agent: Bool = false, service: Bool = false, liveAgent: Bool = false) {
        guard (!liveAgent || agent), !(agent && service), let fixtureIdentity = NativeRecipeFileIdentity(fixture),
              Self.privateDirectory(fixture.deletingLastPathComponent()), let object = PrivateFile.read(fixture, limit: 262144),
              Set(object.keys) == (agent ? ["schema_version", "state_dir", "output_path", "fixture_nonce", "draft_id", "agent_scope", "answers_by_label"] :
                  service ? ["schema_version", "state_dir", "output_path", "fixture_nonce", "service", "answers_by_label"] :
                  ["schema_version", "state_dir", "output_path", "fixture_nonce", "recipes", "forms"]),
              preparationInteger(object["schema_version"]) == 1,
              let nonce = preparationUUID(object["fixture_nonce"]),
              let statePath = preparationText(object["state_dir"], maximum: 4096), statePath.hasPrefix("/"),
              let outputPath = preparationText(object["output_path"], maximum: 4096), outputPath.hasPrefix("/") else { return nil }
        let state = URL(fileURLWithPath: statePath), output = URL(fileURLWithPath: outputPath)
        let root = fixture.deletingLastPathComponent()
        let workspace = root.deletingLastPathComponent()
        var outputInfo = stat()
        guard workspace.lastPathComponent.hasPrefix("meecp-native-onboarding-"), let workspaceIdentity = NativeRecipeDirectoryIdentity(workspace),
              let rootIdentity = NativeRecipeDirectoryIdentity(root),
              state.path == root.appendingPathComponent("state").path, let stateIdentity = NativeRecipeDirectoryIdentity(state),
              output.deletingLastPathComponent().path == root.path, output.lastPathComponent.count <= 128,
              output.path != fixture.path, lstat(output.path, &outputInfo) != 0, errno == ENOENT else { return nil }
        if service {
            guard let selected = object["service"] as? [String: Any],
                  Set(selected.keys) == ["name", "model", "port", "secret_reference"],
                  preparationText(selected["name"], maximum: 256) != nil,
                  let model = preparationText(selected["model"], maximum: 128),
                  model.range(of: "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$", options: .regularExpression) != nil,
                  let port = preparationInteger(selected["port"]), (1...65535).contains(port),
                  preparationUUID(selected["secret_reference"]) != nil,
                  let answers = object["answers_by_label"] as? [String: Any],
                  Set(answers.keys).isSubset(of: ["App name"]) else { return nil }
        } else if agent {
            guard preparationUUID(object["draft_id"]) != nil,
                  let scope = object["agent_scope"] as? [String: Any],
                  AgentPreparationView.acceptanceScopeValid(scope, live: liveAgent),
                  let answers = object["answers_by_label"] as? [String: Any], answers.count <= 256 else { return nil }
        } else {
            guard let recipes = object["recipes"] as? [[String: Any]], recipes.count <= 8,
                  let forms = object["forms"] as? [[String: Any]], forms.count <= 16, !recipes.isEmpty || !forms.isEmpty else { return nil }
        }
        let marker = state.appendingPathComponent((agent ? (liveAgent ? "native-agent-live-" : "native-agent-") : service ? "native-service-" : "native-recipes-") + nonce + ".json")
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
        let path = marker.deletingPathExtension().appendingPathExtension("claimed")
        let fd = open(path.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }; defer { close(fd) }
        let bytes = Array("claimed\n".utf8)
        return bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) == $0.count } && fsync(fd) == 0 && current()
    }

    func report(_ data: Data) -> Bool {
        guard current(), data.count <= 262144 else { return false }
        let fd = open(output.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return false }; defer { close(fd) }
        return data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) == $0.count } && fsync(fd) == 0 && current()
    }

    static func contractSelfTest() -> Bool {
        guard let temporary = nativeRecipeCanonicalPath(FileManager.default.temporaryDirectory.path) else { return false }
        let workspace = URL(fileURLWithPath: temporary).appendingPathComponent("meecp-native-onboarding-" + UUID().uuidString)
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

extension MeecpApp {
    /// Checks selection through panel visibility and nested context-menu tracking.
    fileprivate static func statusButtonSelfTest() -> Bool {
        guard let screen = NSScreen.main else { return false }
        let app = MeecpApp()
        app.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        defer { app.dashboard.dismiss(); NSStatusBar.system.removeStatusItem(app.statusItem) }
        guard let button = app.statusItem.button, let window = button.window else { return false }
        let buttonFrame = window.convertToScreen(button.convert(button.bounds, to: nil))
        // The status window belongs to this item, including native highlight padding.
        guard window.frame.width >= buttonFrame.width,
              window.frame.width <= buttonFrame.width + 32 else { return false }
        let highlight = dashboardHighlightAnchor(button: buttonFrame, statusItem: window.frame)
        guard highlight.minX == window.frame.minX - 2,
              highlight.minX < buttonFrame.minX,
              highlight.minY == buttonFrame.minY else { return false }
        app.configureDashboard()
        let anchor = NSRect(x: screen.visibleFrame.maxX - 24, y: screen.visibleFrame.maxY, width: 24, height: 24)
        app.dashboard.toggle(anchor: anchor, screen: screen)
        guard button.state == .on, button.cell?.isHighlighted == true else { return false }
        app.dashboard.dismiss()
        guard button.state == .off, button.cell?.isHighlighted == false else { return false }
        app.statusMenuTracking = true
        app.dashboard.dismiss()
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        guard button.state == .on, button.cell?.isHighlighted == true else { return false }
        app.statusMenuTracking = false; app.updateStatusButtonSelection()
        return button.state == .off && button.cell?.isHighlighted == false
    }

    /// Developer-only acceptance: dialog choices are simulated by an owned
    /// fixture; production handlers and authenticated owner socket are real.
    /// Checks the sole native preparation workflow and shared draft/review controls.
    fileprivate static func onboardingControlsSelfTest() -> Bool {
        let app = MeecpApp()
        let workspace = app.makeSetupWorkspace(); app.refreshWindow()
        func containsRetiredController(_ view: NSView) -> Bool {
            if view is PreparationWorkspaceView || view is AgentPreparationView ||
                view is ArtifactCaptureView || view is ServiceConnectionView || view is PreparationRecipesView { return true }
            return view.subviews.contains(where: containsRetiredController)
        }
        guard !containsRetiredController(workspace),
              !app.responds(to: NSSelectorFromString("chooseOtherSetupWay:")),
              !app.responds(to: NSSelectorFromString("copyAgentInstructions")),
              !app.responds(to: NSSelectorFromString("exportDraft")),
              !app.responds(to: NSSelectorFromString("saveDraft")),
              !app.responds(to: NSSelectorFromString("showPreparationRecipes")) else { return false }
        guard app.savedSetupsRow?.isHidden == true, app.sourceOptionsStack?.isHidden == true,
              app.piSetupCard?.isHidden == false, app.installationReviewCard?.isHidden == true else { return false }
        app.intentField?.string = "Keep this app description while viewing its source."
        app.pathField?.stringValue = "/tmp/setup-source"
        app.piSourceURL?.stringValue = "https://example.com/project"
        app.sourceOptionsExpanded = true; app.conversationView?.revealSourceOptions(); app.refreshWindow()
        guard app.sourceOptionsStack?.isHidden == false,
              app.intentField?.string == "Keep this app description while viewing its source.",
              app.pathField?.stringValue == "/tmp/setup-source",
              app.piSourceURL?.stringValue == "https://example.com/project" else { return false }
        app.setupRequest = true; app.pollInFlight = true; app.importsPollInFlight = true
        app.detailRequests = ["stale"]; app.pendingAppRequests = ["stale"]
        app.resetHostRequestState()
        guard !app.setupRequest && !app.pollInFlight && !app.importsPollInFlight,
              app.detailRequests.isEmpty && app.pendingAppRequests.isEmpty else { return false }
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
        guard app.draftField?.selectedItem?.representedObject as? String == id,
              app.draftField?.titleOfSelectedItem == "Newer name — Prepared package received" else { return false }
        let brief: [String: Any] = ["draft_id": id, "revision": 1, "state": "waiting_for_agent", "name": "New App",
            "source": ["kind": "document", "locator": app.stateDirectory.appendingPathComponent("pi-setup/briefs/synthetic/app.md").path],
            "intent": "Synthetic app behavior", "document_role": "instructions", "start": false, "start_policy": "manual"]
        guard let briefDraft = ImportDraft(brief), app.isDescriptionDraft(briefDraft) else { return false }
        app.currentDraft = briefDraft; app.intentField?.string = briefDraft.intent; app.refreshWindow()
        return app.documentRoleField?.isHidden == true && app.savedSetupsRow?.isHidden == false
            && app.briefSummary?.stringValue == "Requested: Synthetic app behavior"
    }

    /// Replies must remain transient and cannot dirty a reviewed draft. This
    /// fixture also keeps installed-app navigation independent from setup.
    fileprivate static func conversationalWorkspaceSelfTest() -> Bool {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("meecp-conversation-ui-" + UUID().uuidString)
        guard (try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
              attributes: [.posixPermissions: 0o700])) != nil else { return false }
        let previous = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", directory.path, 1)
        defer {
            if let previous { setenv("MCPME_STATE_DIR", previous, 1) } else { unsetenv("MCPME_STATE_DIR") }
            try? FileManager.default.removeItem(at: directory)
        }
        let app = MeecpApp(); let setup = app.makeSetupWorkspace()
        guard setup === app.conversationView else { return false }
        guard app.modelFileNeedsResources(directory.appendingPathComponent("fixture.GGUF")),
              !app.modelFileNeedsResources(directory.appendingPathComponent("source.md")),
              app.documentRoleField?.item(at: 1)?.isEnabled == false else { return false }
        let id = "11111111-2222-4333-8444-555555555555"
        let source = "sha256:" + String(repeating: "a", count: 64)
        var row: [String: Any] = ["draft_id": id, "revision": 1, "state": "waiting_for_agent", "name": "Fixture App",
            "source": ["kind": "document", "revision": source,
                "locator": directory.appendingPathComponent("pi-setup/briefs/fixture/app.md").path],
            "intent": "Public synthetic setup requirement", "document_role": "instructions", "start": false, "start_policy": "manual"]
        guard let draft = ImportDraft(row), let context = PreparationContext(importID: id, revision: 1, sourceRevision: source) else { return false }
        var startupRow = row
        startupRow["source"] = ["kind": "document", "revision": source,
            "locator": directory.appendingPathComponent("source/setup.md").path]
        startupRow["start_policy"] = "host"; startupRow["start"] = true
        guard let startupDraft = ImportDraft(startupRow) else { return false }
        let preloaded = MeecpApp()
        preloaded.displayDraft(startupDraft)
        guard preloaded.intentField == nil else { return false }
        preloaded.makeManagementWindow()
        guard preloaded.intentField?.string == startupDraft.intent,
              preloaded.nameField?.stringValue == startupDraft.name,
              preloaded.pathField?.stringValue == startupDraft.sourcePath,
              preloaded.documentRoleField?.indexOfSelectedItem == 2,
              preloaded.selectedPolicy == "host", preloaded.startCheck?.state == .on,
              !preloaded.draftDirty else { return false }
        preloaded.intentField?.string = ""
        preloaded.textDidChange(Notification(name: NSText.didChangeNotification, object: preloaded.intentField))
        guard preloaded.draftDirty, preloaded.requestedAppIntent.isEmpty else { return false }
        preloaded.window?.orderOut(nil)
        app.displayDraft(draft); app.conversationComposerContext = context
        app.intentField?.string = "Public synthetic live reply"
        app.setupPlan = ["plan_id": "99999999-2222-4333-8444-555555555555"]
        let generation = app.importViewGeneration
        app.textDidChange(Notification(name: NSText.didChangeNotification, object: app.intentField))
        guard app.requestedAppIntent == draft.intent, !app.hasDraftChanges(draft), !app.draftDirty,
              app.setupPlan != nil, app.importViewGeneration == generation else { return false }
        app.displayDraft(draft)
        guard app.intentField?.string == "Public synthetic live reply", app.requestedAppIntent == draft.intent else { return false }
        app.editingConversationRequest = true; app.conversationComposerContext = nil
        app.intentField?.string = "Public revised setup requirement"
        app.textDidChange(Notification(name: NSText.didChangeNotification, object: app.intentField))
        guard app.draftDirty, app.setupPlan == nil, app.requestedAppIntent != draft.intent else { return false }
        let oldJob: [String: Any] = ["id": "99999999-2222-4333-8444-555555555555", "import_id": id,
            "revision": 1, "source_revision": source, "status": "failed", "stage": "failed", "code": "pi_provider_failed"]
        app.piSetupView.adopt(oldJob, context: context)
        guard app.intentField?.string == "Public revised setup requirement", app.conversationComposerContext == nil else { return false }
        row["revision"] = 2; row["intent"] = "Public revised setup requirement"
        guard let revised = ImportDraft(row) else { return false }
        app.displayDraft(revised)
        guard !app.usesConversationReplyComposer, app.requestedAppIntent == revised.intent else { return false }
        var document = row
        document["document_role"] = nil
        document["source"] = ["kind": "document", "revision": source,
            "locator": directory.appendingPathComponent("source/setup.md").path]
        guard let unclassified = ImportDraft(document) else { return false }
        app.displayDraft(unclassified)
        guard app.sourceOptionsExpanded, app.documentRoleField?.isHiddenOrHasHiddenAncestor == false else { return false }
        var legacyDocument = document; legacyDocument["document_role"] = "reference"
        guard let legacyDraft = ImportDraft(legacyDocument) else { return false }
        app.displayDraft(legacyDraft)
        guard app.requiresNewAgentDraft, app.setupLabel?.isHidden == false,
              app.draftStatus(legacyDraft).contains("retired setup workflow"),
              app.currentDraft?.intent == legacyDraft.intent else { return false }
        app.displayDraft(revised)
        let testWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 780),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        testWindow.isReleasedWhenClosed = false; app.window = testWindow
        let tabs = NSTabView(frame: testWindow.contentView!.bounds); tabs.tabViewType = .noTabsNoBorder
        testWindow.contentView!.addSubview(tabs); app.managementTabs = tabs
        let installed = NSTabViewItem(identifier: "installed"); installed.view = ManagementDashboard(); tabs.addTabViewItem(installed)
        let adding = NSTabViewItem(identifier: "setup"); adding.view = setup; tabs.addTabViewItem(adding)
        app.workspacePicker = NSSegmentedControl(labels: ["Installed Apps", "Add App"], trackingMode: .selectOne, target: nil, action: nil)
        app.selectWorkspace(1); setup.layoutSubtreeIfNeeded()
        guard tabs.selectedTabViewItem?.identifier as? String == "setup", testWindow.title == "meecp — Add App",
              app.currentDraft?.id == id,
              abs(setup.frame.width - tabs.contentRect.width) < 1,
              abs(setup.frame.height - tabs.contentRect.height) < 1 else { return false }
        app.selectWorkspace(0)
        return tabs.selectedTabViewItem?.identifier as? String == "installed" && testWindow.title == "meecp — Manage Apps"
            && app.currentDraft?.id == id && app.currentDraft?.revision == 2
    }

    /// Verifies recovery button availability without starting a host or issuing requests.
    fileprivate static func recoveryControlsSelfTest() -> Bool {
        let app = MeecpApp()
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

// These native entrypoints belonged to the retired alternative setup workflows.
// Host CLI/API compatibility commands are handled separately below.
if ["--artifact-capture-ui-self-test", "--preparation-ui-self-test", "--preparation-native-acceptance",
    "--agent-native-acceptance", "--agent-live-native-acceptance", "--directory-native-acceptance",
    "--public-directory-native-acceptance", "--service-native-acceptance", "--recipes-native-acceptance"]
    .contains(CommandLine.arguments.dropFirst().first ?? "") {
    print("native_setup_workflow_retired"); exit(2)
}

if CommandLine.arguments.dropFirst().first == "--dashboard-ui-self-test" {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    application.finishLaunching()
    let passed = dashboardViewSelfTest() && MeecpApp.statusButtonSelfTest()
    print(passed ? "dashboard_ui_contracts_passed" : "dashboard_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--management-ui-self-test" {
    _ = NSApplication.shared
    let passed = managementViewSelfTest() && setupControlsSelfTest() && MeecpApp.recoveryControlsSelfTest() && MeecpApp.onboardingControlsSelfTest()
    print(passed ? "management_ui_contracts_passed" : "management_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--agent-preparation-ui-self-test" {
    _ = NSApplication.shared
    let passed = runAgentPreparationContractSelfTest()
    print(passed ? "agent_preparation_ui_contracts_passed" : "agent_preparation_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

/// Metadata for the explicit acceptance command; never a handoff authority gate.
func nativeAgentHandoffObservation(proposal: AgentPreparationJob?, form: OnboardingFormV1?, pending: Int,
    agentBusy: Bool, ordinaryBusy: Bool, agentError: String?, ordinaryError: String?,
    preparationJobPresent: Bool, resolutionJobPresent: Bool) -> [String: Bool] {
    let paired = proposal != nil && form != nil
    return ["proposal_present": proposal != nil, "proposal_succeeded": proposal?.state == "succeeded",
        "proposal_settled": proposal?.settled == true, "requests_complete": pending == 0,
        "agent_idle": !agentBusy, "ordinary_idle": !ordinaryBusy,
        "agent_error_absent": agentError == nil, "ordinary_error_absent": ordinaryError == nil,
        "form_present": form != nil,
        "form_import_matches": paired && form?.importID == proposal?.context.importID,
        "form_revision_matches": paired && form?.revision == proposal?.context.revision,
        "form_source_matches": paired && form?.sourceRevision == proposal?.context.sourceRevision,
        "resolution_required": form?.resolutionRequired == true,
        "resolution_result_absent": form != nil && form?.resolutionDigest == nil,
        "preparation_job_absent": !preparationJobPresent, "resolution_job_absent": !resolutionJobPresent]
}

func nativeAgentHandoffObservationSelfTest() -> Bool {
    let row: [String: Any] = ["schema_version": 1, "job_id": PreparationTestData.jobID,
        "import_id": PreparationTestData.importID, "revision": 1, "source_revision": PreparationTestData.source,
        "state": "succeeded", "local_io_settled": true, "proposal_digest": PreparationTestData.recipeDigest,
        "candidate": NSNull(), "verification": NSNull(), "installed": false, "error_code": NSNull()]
    guard let proposal = AgentPreparationJob(row), let form = OnboardingFormV1(PreparationTestData.resolutionForm()) else { return false }
    func observe(_ proposal: AgentPreparationJob?, _ form: OnboardingFormV1?, pending: Int = 0,
        agentBusy: Bool = false, ordinaryBusy: Bool = false, agentError: String? = nil, ordinaryError: String? = nil,
        preparation: Bool = false, resolution: Bool = false) -> [String: Bool] {
        nativeAgentHandoffObservation(proposal: proposal, form: form, pending: pending,
            agentBusy: agentBusy, ordinaryBusy: ordinaryBusy, agentError: agentError, ordinaryError: ordinaryError,
            preparationJobPresent: preparation, resolutionJobPresent: resolution)
    }
    let baseline = observe(proposal, form)
    guard baseline.count == 16, baseline.values.allSatisfy({ $0 }),
          observe(nil, nil)["form_import_matches"] == false, observe(nil, nil)["resolution_result_absent"] == false,
          observe(proposal, nil)["form_present"] == false, observe(nil, form)["proposal_present"] == false else { return false }
    for (key, result) in [("requests_complete", observe(proposal, form, pending: 1)),
        ("agent_idle", observe(proposal, form, agentBusy: true)), ("ordinary_idle", observe(proposal, form, ordinaryBusy: true)),
        ("agent_error_absent", observe(proposal, form, agentError: "agent_source_identity_mismatch")),
        ("ordinary_error_absent", observe(proposal, form, ordinaryError: "preparation_form_unavailable")),
        ("preparation_job_absent", observe(proposal, form, preparation: true)),
        ("resolution_job_absent", observe(proposal, form, resolution: true))] {
        guard result[key] == false, result.filter({ !$0.value }).count == 1 else { return false }
    }
    for (field, value, key) in [("import_id", "99999999-2222-4333-8444-555555555555" as Any, "form_import_matches"),
        ("revision", 2 as Any, "form_revision_matches"), ("source_revision", String(repeating: "e", count: 64) as Any, "form_source_matches")] {
        var changed = PreparationTestData.resolutionForm(); changed[field] = value
        guard let mismatch = OnboardingFormV1(changed), observe(proposal, mismatch)[key] == false else { return false }
    }
    guard let resolved = OnboardingFormV1(PreparationTestData.resolutionForm(resolved: true)),
          observe(proposal, resolved)["resolution_required"] == false,
          observe(proposal, resolved)["resolution_result_absent"] == false else { return false }
    return true
}

if CommandLine.arguments.dropFirst().first == "--agent-handoff-observation-self-test" {
    guard CommandLine.arguments.count == 2 else { exit(2) }
    let passed = nativeAgentHandoffObservationSelfTest()
    print(passed ? "agent_handoff_observation_contracts_passed" : "agent_handoff_observation_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--preparation-form-error-self-test" {
    guard CommandLine.arguments.count == 2 else { exit(2) }
    _ = NSApplication.shared
    let passed = PreparationWorkspaceView.formErrorObservationSelfTest()
    print(passed ? "preparation_form_error_contracts_passed" : "preparation_form_error_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--agent-live-acceptance-preflight" {
    guard CommandLine.arguments.count == 2 else { exit(2) }
    print("agent_live_acceptance_preflight_v1"); exit(0)
}

if CommandLine.arguments.dropFirst().first == "--agent-live-acceptance-self-test" {
    guard CommandLine.arguments.count == 2 else { exit(2) }
    _ = NSApplication.shared
    let passed = AgentPreparationView.liveAcceptanceSelfTest()
    print(passed ? "agent_live_acceptance_contracts_passed" : "agent_live_acceptance_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--recipes-native-context-self-test" {
    let passed = NativeRecipeAcceptanceContext.contractSelfTest()
    print(passed ? "recipes_native_context_passed" : "recipes_native_context_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--pi-setup-ui-self-test" {
    _ = NSApplication.shared
    let passed = PiSetupView.contractSelfTest()
    print(passed ? "pi_setup_ui_contracts_passed" : "pi_setup_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--pi-resources-ui-self-test" {
    _ = NSApplication.shared
    let passed = PiResourcesView.selfTest()
    print(passed ? "pi_resources_ui_contracts_passed" : "pi_resources_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--add-app-conversation-ui-self-test" {
    _ = NSApplication.shared
    let results = ["conversation": PiSetupView.conversationSelfTest(),
                   "presentation": AddAppConversationView.selfTest(),
                   "workspace": MeecpApp.conversationalWorkspaceSelfTest()]
    let passed = results.values.allSatisfy { $0 }
    if !passed { print("failed_checks:" + results.filter { !$0.value }.keys.sorted().joined(separator: ",")) }
    print(passed ? "add_app_conversation_ui_contracts_passed" : "add_app_conversation_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--authorization-ui-self-test" {
    _ = NSApplication.shared
    let passed = ExternalAuthorizationCoordinator.contractSelfTest()
    print(passed ? "authorization_ui_contracts_passed" : "authorization_ui_contracts_failed")
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
    guard let executable = Bundle.main.resourceURL?.appendingPathComponent("host/bin/meecp").path else { exit(2) }
    let values = [executable] + cliArguments
    let pointers = values.map { strdup($0) } + [nil]
    pointers.withUnsafeBufferPointer { pointer in _ = execv(executable, pointer.baseAddress!) }
    for pointer in pointers { if let pointer = pointer { free(pointer) } }
    exit(2)
}

let application = NSApplication.shared
let delegate = MeecpApp()
application.delegate = delegate
application.run()
