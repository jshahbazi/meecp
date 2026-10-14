import AppKit
import Darwin
import Foundation

struct PiResourceSelection {
    let request: [String: Any]
    let selectedBytes: Int?

    init?(_ url: URL, suffix: String = String(UUID().uuidString.prefix(8)).lowercased()) {
        let parts = url.path.split(separator: "/").map(String.init)
        guard url.isFileURL, url.path.hasPrefix("/"), !parts.isEmpty,
              url.path.utf8.count <= 4096, url.path == "/" + parts.joined(separator: "/"),
              !parts.contains("."), !parts.contains(".."),
              !url.path.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              suffix.range(of: "^[a-z0-9]{1,12}$", options: .regularExpression) != nil else { return nil }
        var directory = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard directory >= 0 else { return nil }
        defer { close(directory) }
        for part in parts.dropLast() {
            let next = openat(directory, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard next >= 0 else { return nil }
            close(directory); directory = next
        }
        var info = stat()
        guard fstatat(directory, parts.last!, &info, AT_SYMLINK_NOFOLLOW) == 0 else { return nil }
        let tree = info.st_mode & S_IFMT == S_IFDIR
        guard tree || info.st_mode & S_IFMT == S_IFREG && info.st_nlink == 1,
              tree || info.st_size >= 0 && info.st_size <= 16 * 1024 * 1024 * 1024 else { return nil }
        let ext = url.pathExtension.lowercased(), basename = url.lastPathComponent.lowercased()
        // Match the host's opaque archive classification; capture retains these
        // bytes without opening containers or extracting their contents.
        let archive = !tree && ["zip", "tar", "tgz", "gz", "xz", "bz2", "7z", "rar", "whl", "npz", "zst", "lz4"].contains(ext)
        let model = ["gguf", "onnx", "safetensors"].contains(ext) || tree &&
            ["model", "models", "weights", "checkpoint", "checkpoints"].contains(basename)
        let notice = !tree && (basename == "license" || basename == "notice" || basename == "copying" ||
            basename.hasPrefix("license.") || basename.hasPrefix("notice."))
        guard !model && !notice || tree || info.st_size > 0 else { return nil }
        var slug = basename.replacingOccurrences(of: "[^a-z0-9_-]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-_"))
        if slug.isEmpty || slug.first?.isLetter != true { slug = "resource-" + slug }
        slug = String(slug.prefix(48)) + "-" + suffix
        let format = archive ? "archive" : ["gguf", "onnx", "safetensors", "json", "npy", "png", "jpeg"].contains(ext) ? ext :
            notice || ["txt", "md"].contains(ext) ? "text" : "binary"
        request = ["name": slug, "kind": tree ? "tree" : archive ? "archive" : "file", "path": url.path,
                   "role": notice ? "license" : model ? "model-weights" : "runtime-file", "format": format,
                   "licenses": [String](), "origin": ["kind": "local-selection", "publisher_trust": "publisher_unverified"]]
        selectedBytes = tree ? nil : Int(info.st_size)
    }
}

struct PiResourceSummary {
    let id: String
    let name: String
    let bytes: Int
    let fileCount: Int
    let licenseStatus: String

    init?(_ object: [String: Any]) {
        guard let id = preparationDigest(object["resource_id"]),
              let name = object["name"] as? String,
              name.range(of: "^[a-z][a-z0-9_-]{0,63}$", options: .regularExpression) != nil,
              let bytes = preparationInteger(object["bytes"]), (0...32 * 1024 * 1024 * 1024).contains(bytes),
              let count = preparationInteger(object["file_count"]), (1...60064).contains(count),
              let license = object["license_status"] as? String, ["declared", "missing"].contains(license),
              let kind = object["kind"] as? String, ["file", "tree", "archive"].contains(kind),
              let role = object["role"] as? String, ["model-weights", "runtime-file", "validation", "license"].contains(role),
              object["removed"] == nil || preparationBoolean(object["removed"]) == false else { return nil }
        self.id = id; self.name = name; self.bytes = bytes; fileCount = count; licenseStatus = license
    }
}

struct PiResourceInventory {
    let context: PreparationContext
    let resources: [PiResourceSummary]
    let bytes: Int

    init?(_ object: [String: Any]) {
        guard preparationInteger(object["schema_version"]) == 1,
              let binding = object["binding"] as? [String: Any],
              let id = preparationUUID(binding["import_id"]), let revision = preparationInteger(binding["revision"]),
              let source = preparationText(binding["source_revision"], maximum: 256),
              let context = PreparationContext(importID: id, revision: revision, sourceRevision: source),
              let rows = object["resources"] as? [[String: Any]], rows.count <= 64,
              let bytes = preparationInteger(object["bytes"]), (0...32 * 1024 * 1024 * 1024).contains(bytes) else { return nil }
        let summaries = rows.compactMap(PiResourceSummary.init)
        guard summaries.count == rows.count, Set(summaries.map(\.name)).count == rows.count,
              summaries.reduce(0, { $0 + $1.bytes }) == bytes else { return nil }
        self.context = context; resources = summaries; self.bytes = bytes
    }
}

/// Optional selected inputs supplement MeeCP's automatic acquisition.
final class PiResourcesView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var beforeAttach: ((@escaping (PreparationContext?) -> Void) -> Void)?
    var onCaptured: (([String: Any]) -> Void)?
    var onStateChange: (() -> Void)?
    private(set) var busy = false
    private(set) var inventory: PiResourceInventory?
    private var context: PreparationContext?
    private var generation: UInt64 = 0
    private var polling = false
    private var submittedContext: PreparationContext?
    private var captureSerial: UInt64 = 0
    private var canceling = false
    private let chooseButton = NSButton(title: "Add Existing Files or Runtime…", target: nil, action: nil)
    private let refreshButton = NSButton(title: "Refresh Resources", target: nil, action: nil)
    private let cancelButton = NSButton(title: "Cancel Import", target: nil, action: nil)
    private let explanation = workspaceLabel("MeeCP acquires dependencies automatically. You can optionally reuse model files, runtimes, archives or notices you already have.", size: 12, secondary: true)
    private let summaryLabel = workspaceLabel("Resources are retained for this app across setup attempts.", size: 12, secondary: true)
    private let resourceLabel = workspaceLabel("No saved resources.", size: 12)
    private let statusLabel = workspaceLabel("", size: 12)
    private let detailLabel = workspaceLabel("", size: 11, secondary: true)
    private let spinner = NSProgressIndicator()
    var blocksNavigation: Bool { busy }
    var canChooseResources: Bool { !busy && available?() == true && request != nil }
    func chooseExistingResources() { chooseResources() }

    override init(frame: NSRect) {
        super.init(frame: frame)
        orientation = .vertical; alignment = .leading; spacing = 8
        for (button, selector) in [(chooseButton, #selector(chooseResources)), (refreshButton, #selector(refreshResources)),
                                  (cancelButton, #selector(cancelImport))] {
            button.bezelStyle = .rounded; button.target = self; button.action = selector
            button.setAccessibilityLabel(button.title)
        }
        spinner.style = .spinning; spinner.controlSize = .small; spinner.isDisplayedWhenStopped = false
        for label in [explanation, summaryLabel, resourceLabel, statusLabel, detailLabel] {
            label.lineBreakMode = .byWordWrapping; label.maximumNumberOfLines = 0
            addArrangedSubview(label)
        }
        addArrangedSubview(workspaceStack([chooseButton, refreshButton, cancelButton, spinner], horizontal: true, spacing: 8))
        resourceLabel.setAccessibilityLabel("Saved setup resources")
        statusLabel.setAccessibilityLabel("Setup resource capture status")
        refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func bind(_ context: PreparationContext?) {
        guard self.context != context else { refreshAvailability(); return }
        self.context = context; generation &+= 1; polling = false; inventory = nil
        if !busy { statusLabel.stringValue = ""; detailLabel.stringValue = "" }
        render(); if !busy { refresh() }
    }

    func connectionReset() {
        generation &+= 1; captureSerial &+= 1; polling = false; busy = false
        submittedContext = nil; canceling = false; spinner.stopAnimation(nil)
        statusLabel.stringValue = "Resources will be checked when MeeCP reconnects."
        refreshAvailability(); onStateChange?()
    }

    func refreshAvailability() {
        chooseButton.isEnabled = !busy && available?() == true && request != nil
        refreshButton.isEnabled = !busy && !polling && context != nil && available?() == true && request != nil
        cancelButton.isHidden = !busy || submittedContext == nil
        cancelButton.isEnabled = busy && submittedContext != nil && !canceling && available?() == true && request != nil
    }

    func poll() { refresh() }

    func refresh() {
        guard !busy, !polling, available?() == true, let context, let request else { refreshAvailability(); return }
        polling = true; let expectedGeneration = generation
        refreshAvailability()
        request("v1/pi/resources/" + context.importID, "GET", nil) { [weak self] object, code in
            guard let self, self.generation == expectedGeneration, self.context == context else { return }
            self.polling = false
            guard let body = object?["inventory"] as? [String: Any], let inventory = PiResourceInventory(body), inventory.context == context else {
                self.showError(code ?? "resource_inventory_invalid"); return
            }
            self.inventory = inventory
            if !self.detailLabel.stringValue.isEmpty { self.statusLabel.stringValue = "Resources checked."; self.detailLabel.stringValue = "" }
            self.render(); self.refreshAvailability()
        }
    }

    @objc private func refreshResources() { refresh() }

    @objc private func chooseResources() {
        guard !busy, available?() == true, let window else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true; panel.canChooseDirectories = true; panel.allowsMultipleSelection = true
        panel.message = "Choose existing model files, runtime folders, archives or license notices. MeeCP verifies and retains copies for this app."
        panel.prompt = "Add Resources"
        panel.beginSheetModal(for: window) { [weak self] response in
            guard response == .OK, let self else { return }
            let selections = panel.urls.compactMap { PiResourceSelection($0) }
            guard selections.count == panel.urls.count, (1...64).contains(selections.count) else {
                self.showError("resource_selection_invalid"); return
            }
            self.attach(selections)
        }
    }

    private func attach(_ selections: [PiResourceSelection]) {
        guard !busy, available?() == true, request != nil, (1...64).contains(selections.count) else { return }
        busy = true; spinner.startAnimation(nil); statusLabel.stringValue = "Preparing resource capture…"
        detailLabel.stringValue = ""; refreshAvailability(); onStateChange?()
        let ready: (PreparationContext?) -> Void = { [weak self] context in
            guard let self, self.busy else { return }
            guard let context else { self.finishError("resource_draft_required"); return }
            self.submit(selections, context: context)
        }
        if let beforeAttach { beforeAttach(ready) } else { ready(context) }
    }

    private func submit(_ selections: [PiResourceSelection], context: PreparationContext) {
        guard submittedContext == nil else { return }
        guard let request else { finishError("host_unavailable"); return }
        self.context = context; generation &+= 1; captureSerial &+= 1
        let expectedSerial = captureSerial; submittedContext = context
        statusLabel.stringValue = "Copying and verifying selected resources…"
        let bytes = selections.compactMap(\.selectedBytes).reduce(0, +)
        detailLabel.stringValue = "Selected files: " + Self.bytes(bytes) + ". MeeCP checks folder sizes and extra working storage before copying."
        refreshAvailability()
        request("v1/pi/resources", "POST", ["import_id": context.importID, "revision": context.revision,
                                           "resources": selections.map(\.request)]) { [weak self] object, code in
            guard let self, self.captureSerial == expectedSerial, self.submittedContext == context else { return }
            guard let object, let revised = PreparationContext(object), revised.importID == context.importID,
                  revised.revision == context.revision + 1, revised.sourceRevision == context.sourceRevision,
                  let body = object["resource_inventory"] as? [String: Any],
                  let inventory = PiResourceInventory(body), inventory.context == revised else {
                self.finishError(code ?? "resource_result_changed"); return
            }
            guard self.context == context || self.context == revised else { self.finishError("resource_result_changed"); return }
            if let value = object["job"], !(value is NSNull) {
                guard let job = value as? [String: Any], preparationUUID(job["id"]) != nil,
                      job["import_id"] as? String == revised.importID,
                      preparationInteger(job["revision"]) == revised.revision,
                      job["source_revision"] as? String == revised.sourceRevision else {
                    self.finishError("resource_result_changed"); return
                }
            }
            self.context = revised; self.generation &+= 1; self.inventory = inventory
            self.busy = false; self.submittedContext = nil; self.canceling = false; self.spinner.stopAnimation(nil)
            self.statusLabel.stringValue = "Resources saved. MeeCP can reuse them in the next setup attempt."
            self.detailLabel.stringValue = ""; self.render(); self.refreshAvailability()
            self.onCaptured?(object); self.onStateChange?()
        }
    }

    @objc private func cancelImport() {
        guard busy, !canceling, let context = submittedContext, let request, available?() == true else { return }
        let expectedSerial = captureSerial
        canceling = true; statusLabel.stringValue = "Stopping resource import…"
        detailLabel.stringValue = "MeeCP is waiting for owned file copying to finish."; refreshAvailability()
        request("v1/pi/resources/" + context.importID + "/cancel-capture", "POST", [:]) { [weak self] object, code in
            guard let self, self.busy, self.captureSerial == expectedSerial, self.submittedContext == context else { return }
            guard let requested = preparationBoolean(object?["cancel_requested"]) else {
                self.canceling = false; self.showError(code ?? "resource_cancel_failed"); return
            }
            self.statusLabel.stringValue = requested ? "Stopping resource import…" : "Resource import is settling…"
            self.detailLabel.stringValue = "MeeCP is waiting for the original capture request to finish."
            // The original POST owns completion. A cancellation acknowledgement
            // never releases navigation while a copying thread remains owned.
            self.refreshAvailability()
        }
    }

    private func render() {
        guard let inventory else {
            resourceLabel.stringValue = "No saved resources."
            summaryLabel.stringValue = "Resources are retained for this app across setup attempts."
            refreshAvailability(); return
        }
        summaryLabel.stringValue = "Retained storage: " + Self.bytes(inventory.bytes) + ". MeeCP checks extra working storage before preparation."
        resourceLabel.stringValue = inventory.resources.isEmpty ? "No saved resources." : inventory.resources.map {
            $0.name + " · " + Self.bytes($0.bytes) + " · " + String($0.fileCount) + ($0.fileCount == 1 ? " file" : " files") +
                ($0.licenseStatus == "declared" ? " · license notices retained" : " · license metadata pending")
        }.joined(separator: "\n")
    }

    private static func bytes(_ count: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(count), countStyle: .file)
    }

    private func finishError(_ code: String) {
        busy = false; submittedContext = nil; canceling = false
        spinner.stopAnimation(nil); showError(code); onStateChange?()
    }

    private func showError(_ code: String) {
        statusLabel.stringValue = [
            "resource_selection_invalid": "Choose regular files or folders without filesystem links. MeeCP checks their contents independently.",
            "resource_draft_required": "Describe the app first, then add existing resources.",
            "resource_result_changed": "This setup changed. Refresh the saved app to check its resources.",
            "pi_resource_binding_changed": "This setup changed. Refresh the saved app to check its resources.",
            "stale_import_revision": "This setup changed. Refresh the saved app to check its resources.",
            "pi_resource_disk_unavailable": "There is not enough available storage to retain these resources.",
            "pi_resource_disk_limit": "These resources exceed the admitted storage budget.",
            "resource_blocked": "Resources are currently reserved by other work. Try again after it finishes.",
            "pi_resource_blocked": "Resources are currently reserved by other work. Try again after it finishes.",
            "artifact_credentials_disallowed": "This selection contains credential material and cannot become a setup resource.",
            "pi_resource_canceled": "Resource import canceled. Previously saved resources remain available.",
            "pi_canceled": "Resource import canceled. Previously saved resources remain available.",
            "manager_request_failed": "The capture result is unavailable. Refresh the saved app before trying again."
        ][code] ?? "MeeCP could not complete the resource request. Refresh the saved app to check its current state."
        detailLabel.stringValue = "Details: " + (preparationCode(code) ?? "resource_request_failed")
        refreshAvailability()
    }

    /// Exercise source validation, binding and actual request handlers without opening a panel.
    static func selfTest() -> Bool {
        var root = FileManager.default.temporaryDirectory.appendingPathComponent("meecp-pi-resources-ui-" + UUID().uuidString)
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            defer { try? FileManager.default.removeItem(at: root) }
            guard let canonical = realpath(root.path, nil) else { return false }
            root = URL(fileURLWithPath: String(cString: canonical)); free(canonical)
            let model = root.appendingPathComponent("model.safetensors"), archive = root.appendingPathComponent("runtime.tar.xz")
            let tree = root.appendingPathComponent("models", isDirectory: true), link = root.appendingPathComponent("linked.bin")
            try Data([1, 2, 3]).write(to: model); try Data([4, 5]).write(to: archive)
            try FileManager.default.createDirectory(at: tree, withIntermediateDirectories: false)
            try FileManager.default.createSymbolicLink(at: link, withDestinationURL: model)
            guard let weights = PiResourceSelection(model, suffix: "test1"), let download = PiResourceSelection(archive, suffix: "test2"),
                  let runtime = PiResourceSelection(tree, suffix: "test3"), PiResourceSelection(link) == nil,
                  weights.request["role"] as? String == "model-weights", weights.request["format"] as? String == "safetensors",
                  download.request["kind"] as? String == "archive", runtime.request["kind"] as? String == "tree",
                  runtime.request["role"] as? String == "model-weights", runtime.selectedBytes == nil else { return false }
            for ext in ["zip", "tar", "tgz", "gz", "xz", "bz2", "7z", "rar", "whl", "npz", "zst", "lz4", "NPZ"] {
                let opaque = root.appendingPathComponent("opaque." + ext)
                try Data([6, 7]).write(to: opaque)
                guard let selected = PiResourceSelection(opaque, suffix: "opaque"),
                      selected.request["kind"] as? String == "archive", selected.request["format"] as? String == "archive",
                      selected.selectedBytes == 2 else { return false }
            }
            let id = "11111111-2222-4333-8444-555555555555", source = "sha256:" + String(repeating: "a", count: 64)
            guard let original = PreparationContext(importID: id, revision: 1, sourceRevision: source),
                  let revised = PreparationContext(importID: id, revision: 2, sourceRevision: source) else { return false }
            func inventory(_ revision: Int) -> [String: Any] {
                ["schema_version": 1, "binding": ["import_id": id, "revision": revision, "source_revision": source], "bytes": 3,
                 "resources": [["resource_id": String(repeating: "b", count: 64), "name": "weights", "kind": "file",
                                "role": "model-weights", "format": "safetensors", "bytes": 3, "file_count": 1, "license_status": "missing", "removed": false]]]
            }
            var bad = inventory(1); bad["bytes"] = 4
            guard PiResourceInventory(bad) == nil else { return false }
            let view = PiResourcesView(); view.available = { true }; view.bind(original)
            var submitted: [String: Any]?, posts = 0, adopted = 0, cancels = 0
            var completion: (([String: Any]?, String?) -> Void)?
            view.request = { path, method, body, done in
                if path.hasSuffix("/cancel-capture") && method == "POST" {
                    cancels += 1; done(["cancel_requested": true], nil)
                } else if method == "POST" && path == "v1/pi/resources" { posts += 1; submitted = body; completion = done }
                else { done(["inventory": inventory(2)], nil) }
            }
            view.beforeAttach = { done in done(original) }
            view.onCaptured = { object in if PreparationContext(object) == revised { adopted += 1 } }
            view.attach([weights]); view.attach([download])
            guard view.busy, view.blocksNavigation, !view.chooseButton.isEnabled, posts == 1,
                  submitted?["import_id"] as? String == id, preparationInteger(submitted?["revision"]) == 1,
                  let requests = submitted?["resources"] as? [[String: Any]], requests.count == 1,
                  requests[0]["path"] as? String == model.path else { return false }
            // Ordinary inventory polling may adopt the committed revision before
            // the capture HTTP response arrives. Its completion must still settle.
            view.bind(revised)
            completion?(["draft_id": id, "revision": 2, "source": ["revision": source], "resource_inventory": inventory(2)], nil)
            guard !view.busy, !view.blocksNavigation, adopted == 1, view.inventory?.context == revised,
                  view.resourceLabel.stringValue.contains("license metadata pending"), view.chooseButton.isEnabled else { return false }
            view.beforeAttach = { done in done(revised) }; view.attach([download])
            completion?(["draft_id": id, "revision": 2, "source": ["revision": source], "resource_inventory": inventory(2)], nil)
            guard !view.busy, adopted == 1, view.detailLabel.stringValue == "Details: resource_result_changed" else { return false }
            view.refreshButton.performClick(nil)
            guard view.inventory?.context == revised else { return false }
            view.beforeAttach = { done in done(nil) }; view.attach([download])
            guard !view.busy, posts == 2, view.detailLabel.stringValue == "Details: resource_draft_required" else { return false }
            view.beforeAttach = { done in done(revised) }; view.attach([download])
            guard view.busy, !view.cancelButton.isHidden, view.cancelButton.isEnabled else { return false }
            view.cancelButton.performClick(nil)
            guard cancels == 1, view.busy, view.blocksNavigation, !view.cancelButton.isEnabled,
                  view.statusLabel.stringValue == "Stopping resource import…" else { return false }
            completion?(nil, "pi_resource_canceled")
            guard !view.busy, !view.blocksNavigation, view.cancelButton.isHidden, adopted == 1,
                  view.detailLabel.stringValue == "Details: pi_resource_canceled" else { return false }
            return true
        } catch { return false }
    }
}
