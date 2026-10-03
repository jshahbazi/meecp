import AppKit
import Darwin
import UniformTypeIdentifiers

struct ArtifactCaptureContext: Equatable {
    let importID: String
    let revision: Int
    let sourceRevision: String
    var target: [String: Any] { ["kind": "attach-to-import", "import_id": importID, "revision": revision] }
}

struct ArtifactCaptureSelection {
    let model: URL
    let license: URL
    let format: String
    let bytes: Int
    static let maximumFileBytes = 16 * 1024 * 1024 * 1024

    static func fileBytes(_ url: URL) -> Int? {
        guard fileError(url) == nil else { return nil }
        var info = stat(); guard lstat(url.path, &info) == 0 else { return nil }
        return Int(info.st_size)
    }
    static func fileError(_ url: URL) -> String? {
        let parts = url.path.split(separator: "/").map(String.init)
        guard url.isFileURL, url.path.hasPrefix("/"), !parts.isEmpty, url.path.utf8.count <= 4096,
              url.path == "/" + parts.joined(separator: "/"), !parts.contains("."), !parts.contains("..") else { return "artifact_unsafe_path" }
        // Foundation rewrites /private/var aliases on macOS. Check the actual
        // directory entries instead; the host reopens and verifies them again.
        var directory = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard directory >= 0 else { return "artifact_unsafe_path" }
        defer { close(directory) }
        for part in parts.dropLast() {
            let next = openat(directory, part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard next >= 0 else { return "artifact_unsafe_path" }
            close(directory); directory = next
        }
        guard url.lastPathComponent.count <= 256,
              !url.lastPathComponent.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains) else { return "artifact_unsafe_path" }
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1 else { return "artifact_unsafe_source" }
        guard info.st_size > 0, info.st_size <= maximumFileBytes else { return "artifact_size_limit" }
        return nil
    }

    init?(model: URL, license: URL, format: String) {
        guard ["onnx", "gguf"].contains(format), model != license,
              let modelBytes = Self.fileBytes(model), let licenseBytes = Self.fileBytes(license),
              licenseBytes <= 16 * 1024 * 1024 else { return nil }
        self.model = model; self.license = license; self.format = format; bytes = modelBytes + licenseBytes
    }

    var request: [String: Any] {
        ["schema_version": 1, "kind": "local-files", "entries": [
            ["name": "weights", "role": "model-weights", "format": format, "path": model.path, "licenses": ["license"]],
            ["name": "license", "role": "license", "format": "text", "path": license.path]
        ]]
    }
    var policy: [String: Any] {
        ["schema_version": 1, "disk_ceiling_bytes": bytes + 1024 * 1024, "wall_seconds": 300, "memory_mb": 64]
    }
}

struct ArtifactCaptureJob {
    let id: String
    let state: String
    let settled: Bool
    let retained: Bool
    let digest: String?
    let importID: String?
    let revision: Int?
    let target: ArtifactCaptureContext?
    let error: String?
    static let terminal: Set<String> = ["succeeded", "canceled", "failed", "interrupted"]
    var active: Bool { !Self.terminal.contains(state) }

    init?(_ object: [String: Any], expectedTarget: ArtifactCaptureContext? = nil) {
        guard preparationInteger(object["schema_version"]) == 1, let id = preparationUUID(object["capture_id"]),
              let state = preparationCode(object["state"]),
              ["queued", "copying", "published", "admitting", "succeeded", "cancel_requested", "canceled", "failed", "interrupted"].contains(state),
              let settled = preparationBoolean(object["ownership_settled"]),
              let retained = preparationBoolean(object["reservation_retained"]),
              let target = object["target"] as? [String: Any], let kind = target["kind"] as? String,
              ["create-import", "attach-to-import"].contains(kind) else { return nil }
        var bound: ArtifactCaptureContext?
        if kind == "attach-to-import" {
            guard let importID = preparationUUID(target["import_id"]), let revision = preparationInteger(target["revision"]),
                  revision > 0, expectedTarget == nil || expectedTarget?.importID == importID && expectedTarget?.revision == revision else { return nil }
            bound = ArtifactCaptureContext(importID: importID, revision: revision, sourceRevision: expectedTarget?.sourceRevision ?? "")
        } else if expectedTarget != nil { return nil }
        let digest = preparationDigest(object["collection_digest"])
        guard object["collection_digest"] == nil || object["collection_digest"] is NSNull || digest != nil else { return nil }
        var importID: String?, revision: Int?
        if let result = object["result"] as? [String: Any] {
            guard Set(result.keys) == ["import_id", "revision"], let identity = preparationUUID(result["import_id"]),
                  let number = preparationInteger(result["revision"]), number > 0,
                  bound == nil || identity == bound?.importID && number == bound!.revision + 1 else { return nil }
            importID = identity; revision = number
        } else if !(object["result"] == nil || object["result"] is NSNull) { return nil }
        let error = preparationCode(object["error_code"])
        guard object["error_code"] == nil || object["error_code"] is NSNull || error != nil,
              state != "succeeded" || (digest != nil && importID != nil),
              !(settled && retained) else { return nil }
        self.id = id; self.state = state; self.settled = settled; self.retained = retained
        self.digest = digest; self.importID = importID; self.revision = revision; self.target = bound; self.error = error
    }
}

/// Model selection is data intake. Only the owner host captures and validates bytes.
final class ArtifactCaptureView: NSStackView {
    var available: (() -> Bool)?
    var request: PreparationRequest?
    var beforeCapture: ((@escaping (Bool) -> Void) -> Void)?
    var choices: (() -> [String: Any])?
    var onAdopt: (([String: Any], ArtifactCaptureContext?) -> Bool)?
    var onStateChange: (() -> Void)?
    var onJobChanged: ((String?) -> Void)?
    private(set) var job: ArtifactCaptureJob?
    private(set) var busy = false
    private(set) var adopted = false
    private(set) var errorCode: String?
    private var context: ArtifactCaptureContext?
    private var submittedTarget: ArtifactCaptureContext?
    private var model: URL?, license: URL?
    private var pendingStart: [String: Any]?
    private var restoredID: String?
    private var adoptInFlight = false
    private var generation: UInt64 = 0
    private let format = NSPopUpButton()
    private let supplement = NSButton(checkboxWithTitle: "Add model to the saved draft", target: nil, action: nil)
    private let modelLabel = workspaceLabel("No model file selected", size: 12, secondary: true)
    private let licenseLabel = workspaceLabel("A license file is required", size: 12, secondary: true)
    private let reviewLabel = workspaceLabel("Choose model weights and their license. Publisher trust is unverified for local files.", size: 12, secondary: true)
    private let statusLabel = workspaceLabel("", size: 12)
    private let modelButton = NSButton(title: "Choose Model File…", target: nil, action: nil)
    private let licenseButton = NSButton(title: "Choose License File…", target: nil, action: nil)
    private let captureButton = NSButton(title: "Capture Selected Model", target: nil, action: nil)
    private let cancelButton = NSButton(title: "Cancel Capture", target: nil, action: nil)
    private let recoverButton = NSButton(title: "Recover Capture", target: nil, action: nil)
    private let retryButton = NSButton(title: "Retry Capture Status", target: nil, action: nil)
    var blocksNavigation: Bool { busy || pendingStart != nil || restoredID != nil || job.map { $0.active || !$0.settled || $0.retained } == true }

    override init(frame: NSRect) {
        super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 8
        format.addItems(withTitles: ["ONNX model", "GGUF model"]); format.target = self; format.action = #selector(selectionChanged)
        format.setAccessibilityLabel("Model file format")
        supplement.target = self; supplement.action = #selector(selectionChanged)
        for (button, selector) in [(modelButton, #selector(chooseModel)), (licenseButton, #selector(chooseLicense)),
                                  (captureButton, #selector(startCapture)), (cancelButton, #selector(cancelCapture)),
                                  (recoverButton, #selector(recoverCapture)), (retryButton, #selector(retryCapture))] {
            button.bezelStyle = .rounded; button.target = self; button.action = selector
            button.setAccessibilityLabel(button.title)
        }
        for label in [modelLabel, licenseLabel, reviewLabel, statusLabel] { label.lineBreakMode = .byWordWrapping; label.maximumNumberOfLines = 0 }
        addArrangedSubview(workspaceStack([format, modelButton, licenseButton], horizontal: true, spacing: 8))
        for view in [modelLabel, licenseLabel, supplement, reviewLabel, statusLabel] { addArrangedSubview(view) }
        addArrangedSubview(workspaceStack([captureButton, cancelButton, recoverButton, retryButton], horizontal: true, spacing: 8))
        statusLabel.setAccessibilityLabel("Model capture status"); refreshAvailability()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func bind(_ value: ArtifactCaptureContext?) {
        context = value
        if value == nil { supplement.state = .off }
        refreshAvailability()
    }
    func restore(_ id: String) {
        guard preparationUUID(id) != nil, job == nil, pendingStart == nil else { return }
        restoredID = id; refreshAvailability()
    }
    private var selectedFormat: String { format.indexOfSelectedItem == 1 ? "gguf" : "onnx" }
    private var selection: ArtifactCaptureSelection? {
        guard let model, let license else { return nil }
        return ArtifactCaptureSelection(model: model, license: license, format: selectedFormat)
    }
    func select(model: URL, license: URL, format value: String, supplement attach: Bool = false) -> Bool {
        guard !blocksNavigation else { fail("artifact_capture_busy"); return false }
        guard let selected = ArtifactCaptureSelection(model: model, license: license, format: value) else {
            fail(ArtifactCaptureSelection.fileError(model) ?? ArtifactCaptureSelection.fileError(license) ?? "artifact_capture_contract_invalid"); return false
        }
        guard !attach || context != nil else { fail("stale_import_revision"); return false }
        self.model = selected.model; self.license = selected.license
        format.selectItem(at: value == "gguf" ? 1 : 0); supplement.state = attach ? .on : .off
        selectionChanged(); return true
    }
    @objc private func selectionChanged() {
        guard !blocksNavigation else { return }
        generation &+= 1; job = nil; restoredID = nil; adopted = false; errorCode = nil; onJobChanged?(nil)
        refreshAvailability(); onStateChange?()
    }
    @objc private func chooseModel() { choose(licenseFile: false) }
    @objc private func chooseLicense() { choose(licenseFile: true) }
    private func choose(licenseFile: Bool) {
        guard available?() == true, !blocksNavigation, let window else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        if !licenseFile { panel.allowedContentTypes = [UTType(filenameExtension: selectedFormat) ?? .data] }
        panel.message = licenseFile ? "Choose the license text supplied with this model" : "Choose the model weights to capture"
        let expected = generation
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self, result == .OK, let url = panel.url, self.generation == expected, !self.blocksNavigation else { return }
            guard ArtifactCaptureSelection.fileBytes(url) != nil else { self.fail("artifact_unsafe_source"); return }
            if licenseFile { self.license = url } else { self.model = url }
            self.selectionChanged()
        }
    }
    func refreshAvailability() {
        let enabled = available?() == true, editable = enabled && !blocksNavigation
        format.isEnabled = editable; modelButton.isEnabled = editable; licenseButton.isEnabled = editable
        supplement.isHidden = context == nil; supplement.isEnabled = editable && context != nil
        modelLabel.stringValue = model.map { "Model: " + $0.lastPathComponent } ?? "No model file selected"
        licenseLabel.stringValue = license.map { "License: " + $0.lastPathComponent } ?? "A license file is required"
        if let selected = selection {
            let total = ByteCountFormatter.string(fromByteCount: Int64(selected.bytes), countStyle: .file)
            reviewLabel.stringValue = "Copy \(total) into private storage. Capture allows 5 minutes and reserves 64 MiB memory. Publisher trust is unverified. Compatible setup is checked after capture."
        } else { reviewLabel.stringValue = "Choose model weights and their license. Publisher trust is unverified for local files." }
        captureButton.isEnabled = editable && selection != nil
        cancelButton.isHidden = job?.active != true; cancelButton.isEnabled = enabled && !busy && job?.active == true
        recoverButton.isHidden = job.map { !$0.active && (!$0.settled || $0.retained) } != true
        recoverButton.isEnabled = enabled && !busy
        retryButton.isHidden = pendingStart == nil && restoredID == nil && (errorCode == nil || job == nil)
        retryButton.isEnabled = enabled && !busy
        if let code = errorCode { statusLabel.stringValue = Self.message(code) }
        else if let job {
            let names = ["queued": "Waiting to copy selected files", "copying": "Copying and verifying model files", "published": "Finishing the verified capture", "admitting": "Saving the captured draft", "succeeded": adopted ? "Captured model saved. Continue with the detected setup questions." : "Finishing captured draft setup", "cancel_requested": "Canceling capture and finishing owned work", "canceled": "Capture canceled", "failed": "Capture failed", "interrupted": "Capture interrupted"]
            statusLabel.stringValue = names[job.state] ?? "Checking capture"
            if !job.active && (!job.settled || job.retained) { statusLabel.stringValue += ". Recovery must settle owned work before another capture." }
        } else { statusLabel.stringValue = busy ? "Reviewing capture admission…" : "" }
    }
    private static func message(_ code: String) -> String {
        switch code {
        case "resource_blocked": return "Capture cannot reserve enough memory or disk space. Free resources and retry."
        case "stale_import_revision": return "The saved draft changed before capture started. Review the saved draft and select the files again."
        case "capture_draft_changed": return "The saved draft changed. Captured files remain saved; choose the draft and review it again."
        case "artifact_unsafe_source", "artifact_unsafe_path", "artifact_source_changed", "artifact_identity_mismatch": return "The selected files changed or cannot be captured safely. Select the original model and license again."
        case "artifact_capture_reconciliation_required", "artifact_capture_owner_live": return "Capture ownership remains unsettled. Recover this capture before starting another."
        default: return "Capture could not finish. Retry its saved status before starting another capture."
        }
    }
    private func fail(_ code: String) { errorCode = preparationCode(code) ?? "artifact_capture_failed"; refreshAvailability(); onStateChange?() }
    @objc func startCapture() {
        guard available?() == true, !blocksNavigation, selection != nil else { return }
        busy = true; refreshAvailability(); onStateChange?()
        let expected = generation
        let begin: (Bool) -> Void = { [weak self] ready in
            guard let self, self.generation == expected else { return }
            self.busy = false
            guard ready, let selected = self.selection else { self.fail("artifact_unsafe_source"); return }
            let target: [String: Any]
            if self.supplement.state == .on {
                guard let context = self.context else { self.fail("stale_import_revision"); return }
                self.submittedTarget = context; target = context.target
            } else {
                self.submittedTarget = nil
                let choices = self.choices?() ?? [:]
                let policy = choices["start_policy"] as? String ?? "manual"
                guard Set(choices.keys).isSubset(of: ["name", "intent", "start", "start_policy"]),
                      let name = preparationText(choices["name"] ?? selected.model.deletingPathExtension().lastPathComponent, maximum: 256),
                      let intent = preparationText(choices["intent"] ?? "", maximum: 16384, empty: true),
                      let start = preparationBoolean(choices["start"] ?? false),
                      ["manual", "host"].contains(policy) else { self.fail("artifact_capture_contract_invalid"); return }
                target = ["kind": "create-import", "name": name, "intent": intent, "start": start, "start_policy": policy]
            }
            self.job = nil; self.adopted = false; self.restoredID = nil
            self.pendingStart = ["request": selected.request, "policy": selected.policy, "target": target, "idempotency_key": UUID().uuidString.lowercased()]
            self.submit()
        }
        if let beforeCapture { beforeCapture(begin) } else { begin(true) }
    }
    private func submit() {
        guard !busy, let payload = pendingStart, let request else { return }
        busy = true; errorCode = nil; refreshAvailability(); onStateChange?()
        request("v1/artifact-captures", "POST", payload) { [weak self] object, code in
            guard let self else { return }; self.busy = false
            guard let object, let job = ArtifactCaptureJob(object, expectedTarget: self.submittedTarget) else {
                // The host validates this exact target before creating a job.
                // Unknown responses retain their idempotent request for retry.
                if object == nil && code == "stale_import_revision" { self.pendingStart = nil; self.submittedTarget = nil }
                self.fail(code ?? "artifact_capture_contract_invalid"); return
            }
            self.pendingStart = nil; self.receive(job)
        }
    }
    private func receive(_ value: ArtifactCaptureJob) {
        guard job == nil || job?.id == value.id else { fail("artifact_capture_identity_mismatch"); return }
        job = value; restoredID = nil; errorCode = value.error; onJobChanged?(value.id)
        refreshAvailability(); onStateChange?()
        if value.state == "succeeded", value.settled, !value.retained, !adopted { adopt(value) }
    }
    private func adopt(_ value: ArtifactCaptureJob) {
        guard !adoptInFlight, let identity = value.importID, let revision = value.revision, let request else { return }
        adoptInFlight = true; busy = true; refreshAvailability()
        request("v1/imports/" + identity, "GET", nil) { [weak self] object, code in
            guard let self else { return }; self.adoptInFlight = false; self.busy = false
            guard self.job?.id == value.id, let object,
                  preparationUUID(object["draft_id"]) == identity, preparationInteger(object["revision"]) == revision,
                  self.onAdopt?(object, value.target) == true else { self.fail(code ?? "capture_draft_changed"); return }
            self.adopted = true; self.errorCode = nil; self.refreshAvailability(); self.onStateChange?()
        }
    }
    func poll(force: Bool = false) {
        guard available?() == true, !busy, pendingStart == nil, let request,
              let identity = job?.id ?? restoredID, force || job?.active == true || job?.retained == true || job?.settled == false || restoredID != nil else { return }
        busy = true; refreshAvailability()
        request("v1/artifact-captures/" + identity, "GET", nil) { [weak self] object, code in
            guard let self else { return }; self.busy = false
            guard let object, let value = ArtifactCaptureJob(object, expectedTarget: self.submittedTarget), value.id == identity else { self.fail(code ?? "artifact_capture_contract_invalid"); return }
            self.receive(value)
        }
    }
    @objc func cancelCapture() { control("cancel") }
    @objc func recoverCapture() { control("recover") }
    private func control(_ action: String) {
        guard available?() == true, !busy, let job, let request,
              action == "cancel" && job.active || action == "recover" && (!job.settled || job.retained) else { return }
        busy = true; refreshAvailability()
        request("v1/artifact-captures/" + job.id + "/" + action, "POST", [:]) { [weak self] object, code in
            guard let self else { return }; self.busy = false
            guard let object, let value = ArtifactCaptureJob(object, expectedTarget: self.submittedTarget), value.id == job.id else { self.fail(code ?? "artifact_capture_contract_invalid"); return }
            self.receive(value)
        }
    }
    @objc func retryCapture() {
        guard available?() == true, !busy else { return }
        if pendingStart != nil { submit() }
        else if let job, job.state == "succeeded", job.settled, !job.retained, !adopted { errorCode = nil; adopt(job) }
        else { poll(force: true) }
    }

    /// Exercise the visible buttons and their handlers without executing source.
    static func selfTest() -> Bool {
        var root = FileManager.default.temporaryDirectory.appendingPathComponent("mcpme-capture-ui-" + UUID().uuidString)
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            defer { try? FileManager.default.removeItem(at: root) }
            guard let canonical = realpath(root.path, nil) else { return false }
            root = URL(fileURLWithPath: String(cString: canonical)); free(canonical)
            let model = root.appendingPathComponent("model.onnx"), license = root.appendingPathComponent("LICENSE")
            try Data([1, 2, 3]).write(to: model); try Data("Synthetic permission notice.".utf8).write(to: license)
            let link = root.appendingPathComponent("linked.onnx")
            try FileManager.default.createSymbolicLink(at: link, withDestinationURL: model)
            guard ArtifactCaptureSelection(model: link, license: license, format: "onnx") == nil,
                  ArtifactCaptureSelection(model: model, license: model, format: "onnx") == nil,
                  ArtifactCaptureSelection(model: model, license: license, format: "unknown") == nil else { return false }
            let identity = UUID().uuidString.lowercased(), draft = UUID().uuidString.lowercased()
            let target: [String: Any] = ["kind": "create-import", "name": "Capture test", "intent": "", "start": false, "start_policy": "manual"]
            func summary(_ state: String, settled: Bool, retained: Bool, result: Bool = false) -> [String: Any] {
                ["schema_version": 1, "capture_id": identity, "state": state, "ownership_settled": settled,
                 "reservation_retained": retained, "target": target, "collection_digest": result ? String(repeating: "a", count: 64) : NSNull(),
                 "result": result ? ["import_id": draft, "revision": 1] : NSNull(), "error_code": NSNull()]
            }
            let view = ArtifactCaptureView(); view.available = { true }; view.choices = { ["name": "Capture test"] }
            guard !view.captureButton.isEnabled, view.select(model: model, license: license, format: "onnx") else { return false }
            view.refreshAvailability()
            guard view.captureButton.isEnabled, view.reviewLabel.stringValue.contains("64 MiB"),
                  view.reviewLabel.stringValue.contains("unverified") else { return false }
            var keys: [String] = [], capturedRequest: [String: Any]?, routes: [String] = []
            var starts = 0, adopted = 0
            view.onAdopt = { _, _ in adopted += 1; return true }
            view.request = { path, method, body, completion in
                routes.append(method + " " + path)
                if path == "v1/artifact-captures" {
                    starts += 1; capturedRequest = body; keys.append(body?["idempotency_key"] as? String ?? "")
                    completion(starts == 1 ? nil : summary("copying", settled: false, retained: true), starts == 1 ? "manager_request_failed" : nil)
                } else if path.hasSuffix("/cancel") { completion(summary("canceled", settled: false, retained: true), nil) }
                else if path.hasSuffix("/recover") { completion(summary("canceled", settled: true, retained: false), nil) }
                else { completion(summary("succeeded", settled: true, retained: false, result: true), nil) }
            }
            view.captureButton.performClick(nil)
            guard view.blocksNavigation, view.pendingStart != nil, !view.captureButton.isEnabled else { return false }
            view.retryButton.performClick(nil)
            guard keys.count == 2, keys[0] == keys[1], preparationUUID(keys[0]) != nil,
                  let request = capturedRequest?["request"] as? [String: Any], request["kind"] as? String == "local-files",
                  let entries = request["entries"] as? [[String: Any]], entries.count == 2,
                  entries[0]["licenses"] as? [String] == ["license"],
                  let policy = capturedRequest?["policy"] as? [String: Any], preparationInteger(policy["memory_mb"]) == 64,
                  preparationInteger(policy["wall_seconds"]) == 300, view.job?.active == true else { return false }
            view.cancelButton.performClick(nil)
            guard view.blocksNavigation, !view.recoverButton.isHidden else { return false }
            view.recoverButton.performClick(nil)
            guard !view.blocksNavigation, view.job?.state == "canceled", adopted == 0,
                  routes.contains("POST v1/artifact-captures/" + identity + "/cancel"),
                  routes.contains("POST v1/artifact-captures/" + identity + "/recover") else { return false }
            let restored = ArtifactCaptureView(); restored.available = { true }; restored.restore(identity)
            guard restored.blocksNavigation else { return false }
            restored.onAdopt = { _, _ in adopted += 1; return true }
            restored.request = { path, _, _, completion in
                completion(path.hasPrefix("v1/imports/") ? ["draft_id": draft, "revision": 2] : summary("succeeded", settled: true, retained: false, result: true), nil)
            }
            restored.poll()
            guard restored.errorCode == "capture_draft_changed", !restored.adopted, adopted == 0, restored.job?.id == identity else { return false }
            restored.request = { path, _, _, completion in
                completion(path.hasPrefix("v1/imports/") ? ["draft_id": draft, "revision": 1] : summary("succeeded", settled: true, retained: false, result: true), nil)
            }
            restored.retryButton.performClick(nil)
            guard restored.adopted, adopted == 1, !restored.blocksNavigation else { return false }
            let stale = ArtifactCaptureView(); stale.available = { true }
            stale.bind(ArtifactCaptureContext(importID: draft, revision: 3, sourceRevision: "sha256:" + String(repeating: "b", count: 64)))
            guard stale.select(model: model, license: license, format: "onnx", supplement: true) else { return false }
            stale.request = { _, _, _, completion in completion(nil, "stale_import_revision") }
            stale.captureButton.performClick(nil)
            guard stale.job == nil, stale.pendingStart == nil, !stale.blocksNavigation,
                  stale.errorCode == "stale_import_revision", stale.retryButton.isHidden else { return false }
            let blocked = ArtifactCaptureView(); blocked.available = { true }
            guard blocked.select(model: model, license: license, format: "onnx") else { return false }
            blocked.request = { _, _, _, completion in
                var result = summary("failed", settled: false, retained: true)
                result["error_code"] = "resource_blocked"; completion(result, nil)
            }
            blocked.captureButton.performClick(nil)
            guard blocked.blocksNavigation, blocked.job?.id == identity, !blocked.recoverButton.isHidden,
                  blocked.errorCode == "resource_blocked" else { return false }
            let binding = ArtifactCaptureContext(importID: draft, revision: 3, sourceRevision: "sha256:" + String(repeating: "b", count: 64))
            var supplementJob = summary("succeeded", settled: true, retained: false, result: true)
            supplementJob["target"] = binding.target; supplementJob["result"] = ["import_id": draft, "revision": 4]
            guard ArtifactCaptureJob(supplementJob, expectedTarget: binding)?.target == binding else { return false }
            supplementJob["result"] = ["import_id": draft, "revision": 5]
            return ArtifactCaptureJob(supplementJob, expectedTarget: binding) == nil
        } catch { return false }
    }
}
