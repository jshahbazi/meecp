import AppKit
import CoreFoundation

private func authorizationInteger(_ value: Any?) -> Int? {
    guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
          value.doubleValue.isFinite, value.doubleValue.rounded() == value.doubleValue,
          value.doubleValue >= 0, value.doubleValue < Double(Int.max) else { return nil }
    return value.intValue
}

private func authorizationLabel(_ value: Any?, maximum: Int = 128) -> String? {
    guard let value = value as? String, !value.isEmpty, value.utf8.count <= maximum,
          !value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else { return nil }
    return value
}

private func authorizationBoolean(_ value: Any?) -> Bool? {
    guard let value = value as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() else { return nil }
    return value.boolValue
}

private func authorizationDigest(_ value: Any?) -> String? {
    guard let value = value as? String, value.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { return nil }
    return value
}

private func authorizationNames(_ value: Any?) -> Bool {
    guard let values = value as? [String], values.count <= 2048 else { return false }
    return values.allSatisfy { authorizationLabel($0, maximum: 4096) != nil }
}

private func authorizationUpgradePackage(_ value: [String: Any], generationKey: String) -> Bool {
    let fields: Set<String> = ["name", "package_id", "version", "package_digest", "profile", "start", "start_policy", "resources",
        "declared_access", "tools", "configuration_fields", "secret_reference_names", "installed_bytes", "public_port", generationKey]
    guard Set(value.keys) == fields,
          authorizationLabel(value["name"], maximum: 1024) != nil,
          authorizationLabel(value["package_id"], maximum: 128) != nil,
          let version = authorizationLabel(value["version"], maximum: 128),
          version.range(of: "^[0-9]+\\.[0-9]+\\.[0-9]+(?:-[A-Za-z0-9.-]+)?$", options: .regularExpression) != nil,
          authorizationDigest(value["package_digest"]) != nil,
          ["mcp-stdio", "mcp-http", "model-service", "host-command"].contains(value["profile"] as? String ?? ""),
          ["manual", "host"].contains(value["start_policy"] as? String ?? ""),
          authorizationBoolean(value["start"]) != nil,
          authorizationInteger(value[generationKey]) != nil,
          let port = authorizationInteger(value["public_port"]), (1...65535).contains(port),
          authorizationInteger(value["installed_bytes"]) != nil,
          authorizationNames(value["tools"]), authorizationNames(value["configuration_fields"]),
          authorizationNames(value["secret_reference_names"]),
          let resources = value["resources"] as? [String: Any], Set(resources.keys) == ["memory_mb", "cpu_only", "gpu"],
          let memory = authorizationInteger(resources["memory_mb"]), memory > 0,
          authorizationBoolean(resources["cpu_only"]) != nil, authorizationBoolean(resources["gpu"]) != nil,
          let access = value["declared_access"] as? [String: Any], Set(access.keys) == ["filesystem", "network", "secrets", "writes"],
          authorizationNames(access["filesystem"]), authorizationNames(access["network"]), authorizationNames(access["secrets"]),
          authorizationBoolean(access["writes"]) != nil else { return false }
    return true
}

private func authorizationUpgradeEffects(_ effects: [String: Any], action: [String: Any]) -> Bool {
    guard Set(effects.keys) == ["slot_id", "current", "candidate", "data_policy", "rollback_policy", "validation"],
          let slot = effects["slot_id"] as? String, UUID(uuidString: slot) != nil, action["slot_id"] as? String == slot,
          effects["data_policy"] as? String == "clone_before_probe",
          effects["rollback_policy"] as? String == "before_exposure_only",
          authorizationLabel(effects["validation"], maximum: 2048) != nil,
          let current = effects["current"] as? [String: Any], authorizationUpgradePackage(current, generationKey: "generation"),
          let candidate = effects["candidate"] as? [String: Any], authorizationUpgradePackage(candidate, generationKey: "expected_generation"),
          authorizationInteger(current["generation"]) == authorizationInteger(candidate["expected_generation"]),
          authorizationInteger(current["generation"]) == authorizationInteger(action["expected_generation"]),
          authorizationInteger(current["public_port"]) == authorizationInteger(candidate["public_port"]),
          authorizationDigest(candidate["package_digest"]) == authorizationDigest(action["package_digest"]),
          current["name"] as? String == candidate["name"] as? String,
          current["profile"] as? String == candidate["profile"] as? String,
          current["package_id"] as? String == candidate["package_id"] as? String else { return false }
    return true
}

struct ExternalAuthorizationRequest: Equatable {
    let id: String
    let profileID: String?
    let label: String
    let kind: String
    let scopes: [String]
    let scopeDigest: String
    let policyRevision: Int
    let generation: String
    let deadline: Date
    let review: String
    let completeReview: Bool

    init?(_ object: [String: Any]) {
        guard let id = object["request_id"] as? String, UUID(uuidString: id) != nil,
              object["status"] as? String == "pending",
              object["identity_mode"] as? String == "account_scoped_integration",
              let label = authorizationLabel(object["label"] ?? object["client_name"]),
              let kind = object["kind"] as? String, ["base", "preparation", "resolution", "install", "upgrade"].contains(kind),
              let scopes = object["scopes"] as? [String], kind != "base" || !scopes.isEmpty,
              Set(scopes).isSubset(of: ["inspect", "plan"]), Set(scopes).count == scopes.count,
              !scopes.contains("plan") || scopes.contains("inspect"),
              let digest = object["scope_digest"] as? String,
              digest.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil,
              let revision = authorizationInteger(object["policy_revision"]),
              let generation = authorizationLabel(object["generation"], maximum: 64),
              let deadline = (object["deadline"] ?? object["expires_at"]) as? NSNumber,
              CFGetTypeID(deadline) != CFBooleanGetTypeID(), deadline.doubleValue.isFinite else { return nil }
        let profile = object["profile_id"] as? String
        guard profile == nil || UUID(uuidString: profile!) != nil else { return nil }
        self.id = id; profileID = profile
        if let client = object["client"] as? [String: Any], let name = authorizationLabel(client["name"]), name != label {
            self.label = name + " (" + label + ")"
        } else { self.label = label }
        self.kind = kind; self.scopes = scopes
        scopeDigest = digest; policyRevision = revision; self.generation = generation
        self.deadline = Date(timeIntervalSince1970: deadline.doubleValue)
        var lines = ["Request: " + id, "Scope digest: " + digest]
        var complete = kind == "base"
        if kind != "base" {
            guard let action = object["action"] as? [String: Any] ?? object["review"] as? [String: Any],
                  let plan = action["plan_id"] as? String, UUID(uuidString: plan) != nil,
                  let planDigest = action["plan_digest"] as? String,
                  planDigest.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else { return nil }
            lines.insert("Operation: " + kind, at: 0)
            lines.insert("Plan: " + plan + "\nPlan digest: " + planDigest, at: 1)
            if let attempt = authorizationLabel(action["idempotency_key"] ?? action["attempt_key"], maximum: 128) { lines.append("Attempt: " + attempt) }
            if ["preparation", "resolution"].contains(kind) {
                guard let execution = action["execution"] as? [String: Any], let operations = execution["operations"] as? [String],
                      operations == (kind == "preparation" ? ["build", "probe"] : ["resolve"]) else { return nil }
                lines.insert("Permitted execution: " + operations.joined(separator: ", "), at: 2)
                if let effects = action["effects"] as? [String: Any],
                   let data = try? JSONSerialization.data(withJSONObject: effects, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
                   data.count <= 32768, let text = String(data: data, encoding: .utf8) {
                    lines.insert("Execution effects\n" + text, at: 2); complete = true
                }
            } else if kind == "install", let effects = action["install_effects"] as? [String: Any],
                      Set(effects.keys) == ["name", "profile", "start", "start_policy", "resources", "declared_access", "configuration_fields", "secret_reference_names", "installed_bytes"],
                      authorizationLabel(effects["name"], maximum: 1024) != nil,
                      ["manual", "host"].contains(effects["start_policy"] as? String ?? ""), effects["start"] is Bool,
                      let data = try? JSONSerialization.data(withJSONObject: effects, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
                      data.count <= 32768, let text = String(data: data, encoding: .utf8) {
                lines.insert("Installation effects\n" + text, at: 2); complete = true
            } else if kind == "upgrade", let effects = action["upgrade_effects"] as? [String: Any],
                      authorizationUpgradeEffects(effects, action: action),
                      let data = try? JSONSerialization.data(withJSONObject: effects, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
                      data.count <= 32768, let text = String(data: data, encoding: .utf8) {
                lines.insert("Upgrade existing app\n" + text, at: 2); complete = true
            }
            // The broker supplies only independently validated review metadata.
            for key in ["summary", "provider_disclosure", "access_disclosure"] {
                if let text = authorizationLabel(action[key], maximum: 2048) { lines.append(text) }
            }
        }
        review = lines.joined(separator: "\n\n")
        completeReview = complete
    }

    func sameBinding(as other: ExternalAuthorizationRequest) -> Bool {
        id == other.id && profileID == other.profileID && kind == other.kind && scopes == other.scopes &&
        scopeDigest == other.scopeDigest && policyRevision == other.policyRevision && generation == other.generation && deadline == other.deadline
    }

    func decision(remember: Bool, scopes: [String]) -> [String: Any] {
        ["remember": remember, "account_scoped_consent": remember, "scopes": scopes,
         "scope_digest": scopeDigest, "policy_revision": policyRevision, "generation": generation]
    }
}

/// App-owned presentation state. The supervisor remains the authority for every decision.
final class ExternalAuthorizationCoordinator: NSObject, NSWindowDelegate {
    enum State: Equatable { case idle, queued, presenting, submitting, settled }
    typealias Request = (String, String, [String: Any]?, @escaping ([String: Any]?, String?) -> Void) -> Void
    var request: Request?
    var available: (() -> Bool)?
    var presentationBlocked: (() -> Bool)?
    var onCopyConnection: (() -> Void)?
    private(set) var state: State = .idle
    private(set) var epoch: UInt64 = 0
    private var polling = false
    private var selected: ExternalAuthorizationRequest?
    private var panel: NSPanel?
    private var connectionsWindow: NSWindow?
    private var rowsStack: NSStackView?
    private var remember: NSButton?
    private var planning: NSButton?
    private var allowButton: NSButton?
    private var denyButton: NSButton?
    private var notice: NSTextField?

    func invalidate() {
        epoch &+= 1; polling = false; selected = nil; state = .idle
        panel?.orderOut(nil); panel = nil
        rowsStack?.arrangedSubviews.forEach { rowsStack?.removeArrangedSubview($0); $0.removeFromSuperview() }
    }

    func poll() {
        if let selected, selected.deadline <= Date() {
            epoch &+= 1; self.selected = nil; panel?.orderOut(nil); panel = nil; state = .settled; polling = false
        }
        guard available?() == true, !polling, let request else { return }
        polling = true; let expected = epoch
        request("v1/authorization/requests", "GET", nil) { [weak self] object, _ in
            guard let self, expected == self.epoch else { return }
            self.polling = false
            guard let rows = object?["requests"] as? [[String: Any]] else { return }
            self.receive(rows.compactMap(ExternalAuthorizationRequest.init))
        }
        if connectionsWindow?.isVisible == true { refreshConnections() }
    }

    private func receive(_ rows: [ExternalAuthorizationRequest]) {
        let pending = rows.filter { $0.deadline > Date() }
        if let selected, !pending.contains(where: { $0.sameBinding(as: selected) }) {
            epoch &+= 1; self.selected = nil; panel?.orderOut(nil); panel = nil; state = .settled
        }
        guard selected == nil, state != .submitting else { return }
        guard let next = pending.first else { state = .idle; return }
        state = .queued
        guard presentationBlocked?() != true, let request else { return }
        selected = next; let expected = epoch
        // Inspect again before presentation; a queued row is never itself consent.
        request("v1/authorization/requests/" + next.id, "GET", nil) { [weak self] object, _ in
            guard let self, self.epoch == expected, self.selected == next else { return }
            guard let object, let inspected = ExternalAuthorizationRequest(object["request"] as? [String: Any] ?? object),
                  inspected.sameBinding(as: next), inspected.completeReview, inspected.deadline > Date(), self.presentationBlocked?() != true else {
                self.selected = nil; self.state = .queued; return
            }
            self.selected = inspected
            self.present(inspected)
        }
    }

    private func present(_ item: ExternalAuthorizationRequest) {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 600, height: item.kind == "base" ? 540 : 640),
                            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        panel.title = item.kind == "upgrade" ? "Authorize App Upgrade" : "Authorize Connection"
        panel.isReleasedWhenClosed = false; panel.delegate = self
        let content = workspaceStack([], spacing: 14)
        let container = NSView(); panel.contentView = container; container.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 24),
            content.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -24),
            content.topAnchor.constraint(equalTo: container.topAnchor, constant: 24),
            content.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor, constant: -24)])
        let heading = workspaceLabel(item.label + (item.kind == "upgrade" ? " requests an app upgrade" : " requests access"), size: 20, weight: .semibold)
        heading.maximumNumberOfLines = 2; heading.lineBreakMode = .byTruncatingTail; content.addArrangedSubview(heading)
        let explanation = item.kind == "base"
            ? "Allow this local integration to inspect apps" + (item.scopes.contains("plan") ? " and create setup plans" : "") + ". Execution requires a separate review."
            : item.kind == "upgrade"
                ? "Review the current app and replacement package. This upgrades the existing entry and validates a private data clone. The agent must call the upgrade again after approval."
                : "Review this exact operation before allowing it. The agent must call it again after approval."
        let text = workspaceLabel(explanation, size: 13); text.maximumNumberOfLines = 3
        content.addArrangedSubview(text)
        let identity = workspaceLabel("Application identity is unverified. Access is scoped to this meecp installation and macOS account.", size: 12, secondary: true)
        identity.maximumNumberOfLines = 3; content.addArrangedSubview(identity)
        let planning = NSButton(checkboxWithTitle: "Create setup plans and edit setup drafts", target: nil, action: nil)
        planning.state = item.scopes.contains("plan") ? .on : .off
        planning.isEnabled = item.kind == "base" && item.scopes.contains("plan")
        planning.isHidden = item.kind != "base"; self.planning = planning; content.addArrangedSubview(planning)
        let remember = NSButton(checkboxWithTitle: "Remember this integration for this macOS account", target: nil, action: nil)
        remember.state = item.profileID != nil && item.kind == "base" ? .on : .off
        remember.isEnabled = item.profileID != nil && item.kind == "base"; remember.isHidden = item.kind != "base"
        self.remember = remember; content.addArrangedSubview(remember)
        let disclosure = NSScrollView(); disclosure.hasVerticalScroller = true; disclosure.borderType = .bezelBorder
        let details = NSTextView(frame: NSRect(x: 0, y: 0, width: 552, height: 100)); details.isEditable = false; details.isSelectable = true
        details.isVerticallyResizable = true; details.isHorizontallyResizable = false; details.autoresizingMask = [.width]
        details.textContainer?.widthTracksTextView = true
        details.font = .monospacedSystemFont(ofSize: 11, weight: .regular); details.string = item.review
        details.textContainerInset = NSSize(width: 8, height: 8); disclosure.documentView = details
        disclosure.heightAnchor.constraint(equalToConstant: item.kind == "base" ? 100 : 260).isActive = true
        disclosure.setAccessibilityLabel("Authorization review details"); content.addArrangedSubview(disclosure)
        let notice = workspaceLabel("Requests expire after two minutes.", size: 12, secondary: true); self.notice = notice
        content.addArrangedSubview(notice)
        let deny = NSButton(title: "Deny", target: self, action: #selector(denyAccess))
        let allow = NSButton(title: "Allow", target: self, action: #selector(allowAccess))
        allow.bezelColor = .controlAccentColor; self.allowButton = allow; self.denyButton = deny
        content.addArrangedSubview(workspaceStack([deny, allow], horizontal: true))
        for view in content.arrangedSubviews { workspaceFill(view, in: content) }
        self.panel = panel; state = .presenting; panel.center(); panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func allowAccess() { submit(allow: true) }
    @objc private func denyAccess() { submit(allow: false) }
    private func submit(allow: Bool) {
        guard state == .presenting, let selected, let request else { return }
        guard selected.deadline > Date() else { invalidate(); return }
        state = .submitting; allowButton?.isEnabled = false; denyButton?.isEnabled = false
        let scopes = selected.kind == "base" && planning?.state != .on ? ["inspect"] : selected.scopes
        let expected = epoch
        let body = allow ? selected.decision(remember: remember?.state == .on && selected.kind == "base", scopes: scopes) : [:]
        request("v1/authorization/requests/" + selected.id + (allow ? "/approve" : "/deny"), "POST", body) { [weak self] object, code in
            guard let self, expected == self.epoch, self.selected == selected else { return }
            if object == nil {
                self.notice?.stringValue = "Decision could not complete. Refreshing request status."
                self.state = .presenting; self.denyButton?.isEnabled = true; self.allowButton?.isEnabled = true
                self.poll(); return
            }
            self.panel?.orderOut(nil); self.panel = nil; self.selected = nil; self.state = .settled; self.poll()
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === panel { denyAccess(); return false }
        return true
    }

    func showConnections() {
        if connectionsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 480),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.title = "Approved Connections"; window.isReleasedWhenClosed = false; window.center()
            let root = workspaceStack([], spacing: 14)
            let container = NSView(); window.contentView = container; container.addSubview(root)
            NSLayoutConstraint.activate([root.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 24),
                root.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -24), root.topAnchor.constraint(equalTo: container.topAnchor, constant: 24),
                root.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -24)])
            let copy = NSButton(title: "Copy Management Connection", target: self, action: #selector(copyConnection))
            root.addArrangedSubview(copy)
            let subtitle = workspaceLabel("Copying creates an unapproved profile. Saved approvals grant inspection and planning; each execution needs review.", size: 12, secondary: true)
            subtitle.maximumNumberOfLines = 3; root.addArrangedSubview(subtitle)
            let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.drawsBackground = false
            let rows = WorkspaceDocumentStack(); rows.orientation = .vertical; rows.alignment = .leading; rows.spacing = 12
            rows.translatesAutoresizingMaskIntoConstraints = false; scroll.documentView = rows
            rows.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor).isActive = true
            root.addArrangedSubview(scroll); scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true
            for view in root.arrangedSubviews { workspaceFill(view, in: root) }
            self.rowsStack = rows; connectionsWindow = window
        }
        refreshConnections(); connectionsWindow?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    @objc private func copyConnection() { onCopyConnection?() }

    private func refreshConnections() {
        guard available?() == true, let request else { return }
        let expected = epoch
        request("v1/authorization/profiles", "GET", nil) { [weak self] object, _ in
            guard let self, expected == self.epoch, let profiles = object?["profiles"] as? [[String: Any]] else { return }
            request("v1/authorization/sessions", "GET", nil) { [weak self] object, _ in
                guard let self, expected == self.epoch, let sessions = object?["sessions"] as? [[String: Any]], let stack = self.rowsStack else { return }
                stack.arrangedSubviews.forEach { stack.removeArrangedSubview($0); $0.removeFromSuperview() }
                for row in profiles {
                    guard let id = row["profile_id"] as? String, UUID(uuidString: id) != nil,
                          let label = authorizationLabel(row["label"]) else { continue }
                    let button = NSButton(title: "Revoke Access", target: self, action: #selector(revokeProfile(_:))); button.identifier = NSUserInterfaceItemIdentifier(id)
                    button.isEnabled = row["revoked_at"] == nil || row["revoked_at"] is NSNull
                    let revoked = row["revoked_at"] != nil && !(row["revoked_at"] is NSNull)
                    let state = revoked ? "Access revoked" : (row["approved"] as? Bool == true ? "Remembered for this macOS account" : "Awaiting approval")
                    stack.addArrangedSubview(workspaceStack([workspaceLabel(label, size: 14, weight: .medium), workspaceLabel(state, size: 12, secondary: true), button]))
                }
                for row in sessions {
                    guard let id = row["session_id"] as? String, UUID(uuidString: id) != nil else { continue }
                    let button = NSButton(title: "Disconnect", target: self, action: #selector(disconnectSession(_:))); button.identifier = NSUserInterfaceItemIdentifier(id)
                    stack.addArrangedSubview(workspaceStack([workspaceLabel(authorizationLabel(row["label"]) ?? "Active connection", size: 14), button], horizontal: true))
                }
                if stack.arrangedSubviews.isEmpty { stack.addArrangedSubview(workspaceLabel("No saved connections.", size: 13, secondary: true)) }
                for view in stack.arrangedSubviews { workspaceFill(view, in: stack) }
            }
        }
    }
    @objc private func revokeProfile(_ sender: NSButton) { mutate("profiles/" + (sender.identifier?.rawValue ?? "") + "/revoke") }
    @objc private func disconnectSession(_ sender: NSButton) { mutate("sessions/" + (sender.identifier?.rawValue ?? "") + "/disconnect") }
    private func mutate(_ path: String) {
        let expected = epoch
        request?("v1/authorization/" + path, "POST", [:]) { [weak self] _, _ in
            guard let self, expected == self.epoch else { return }; self.refreshConnections(); self.poll()
        }
    }

    static func contractSelfTest() -> Bool {
        let row: [String: Any] = ["request_id": UUID().uuidString.lowercased(), "profile_id": UUID().uuidString.lowercased(),
            "label": "Synthetic client", "kind": "base", "status": "pending", "identity_mode": "account_scoped_integration",
            "scopes": ["inspect", "plan"], "scope_digest": String(repeating: "a", count: 64), "policy_revision": 0,
            "generation": UUID().uuidString.lowercased(), "deadline": Date().addingTimeInterval(120).timeIntervalSince1970]
        guard let parsed = ExternalAuthorizationRequest(row), parsed.decision(remember: true, scopes: ["inspect"])["account_scoped_consent"] as? Bool == true else { return false }
        for (key, value) in [("identity_mode", "verified_launcher" as Any), ("scopes", ["owner"] as Any), ("policy_revision", true as Any), ("label", "unsafe\ntext" as Any)] {
            var invalid = row; invalid[key] = value; guard ExternalAuthorizationRequest(invalid) == nil else { return false }
        }
        let coordinator = ExternalAuthorizationCoordinator(); var blocked = true; var callbacks: [([String: Any]?, String?) -> Void] = []
        var paths: [String] = []; var submitted: [String: Any]?
        coordinator.presentationBlocked = { blocked }
        coordinator.request = { path, _, body, callback in paths.append(path); submitted = body; callbacks.append(callback) }
        coordinator.receive([parsed]); guard coordinator.state == .queued, callbacks.isEmpty else { return false }
        blocked = false; coordinator.receive([parsed]); guard callbacks.count == 1 else { return false }
        coordinator.invalidate(); callbacks[0](["request": row], nil)
        guard coordinator.state == .idle, coordinator.panel == nil else { return false }
        var expired = row; expired["deadline"] = Date().addingTimeInterval(-1).timeIntervalSince1970
        coordinator.receive([ExternalAuthorizationRequest(expired)!]); guard coordinator.state == .idle else { return false }
        coordinator.receive([parsed]); callbacks.last?(["request": row], nil)
        guard coordinator.state == .presenting, coordinator.remember?.state == .on else { return false }
        var secondRow = row; secondRow["request_id"] = UUID().uuidString.lowercased()
        let second = ExternalAuthorizationRequest(secondRow)!
        coordinator.receive([parsed, second]); guard coordinator.selected == parsed else { return false }
        coordinator.planning?.state = .off; coordinator.remember?.state = .off; coordinator.allowAccess()
        guard coordinator.state == .submitting, submitted?["scopes"] as? [String] == ["inspect"],
              submitted?["account_scoped_consent"] as? Bool == false, paths.last?.hasSuffix("/approve") == true else { return false }
        let count = callbacks.count; coordinator.allowAccess(); guard callbacks.count == count else { return false }
        let staleDecision = callbacks.last!; coordinator.invalidate(); staleDecision(["api_version": 1], nil)
        guard coordinator.state == .idle, coordinator.panel == nil else { return false }
        coordinator.receive([second]); callbacks.last?(["request": secondRow], nil)
        guard coordinator.state == .presenting, coordinator.selected == second else { return false }
        guard let content = coordinator.panel?.contentView, let allow = coordinator.allowButton else { return false }
        content.layoutSubtreeIfNeeded()
        let buttonFrame = allow.convert(allow.bounds, to: content)
        guard buttonFrame.width > 0, buttonFrame.minY >= 0, buttonFrame.maxY <= content.bounds.height else { return false }
        coordinator.receive([]); guard coordinator.panel == nil, coordinator.state == .idle else { return false }
        var install = row; install["kind"] = "install"; install["scopes"] = [String]()
        install["review"] = ["family": "install", "plan_id": UUID().uuidString.lowercased(), "plan_digest": String(repeating: "b", count: 64)]
        guard let compact = ExternalAuthorizationRequest(install), !compact.completeReview else { return false }
        var review = install["review"] as! [String: Any]
        review["install_effects"] = ["name": "Synthetic app", "profile": "mcp-stdio", "start": true, "start_policy": "manual",
            "resources": ["memory_mb": 64, "cpu_only": true, "gpu": false], "declared_access": ["filesystem": [], "network": [], "writes": false, "secrets": []],
            "configuration_fields": [], "secret_reference_names": [], "installed_bytes": 1024] as [String: Any]
        install["review"] = review
        guard let detailed = ExternalAuthorizationRequest(install), detailed.completeReview, detailed.sameBinding(as: compact) else { return false }
        coordinator.receive([compact]); callbacks.last?(["request": install], nil)
        guard coordinator.state == .presenting, coordinator.selected?.completeReview == true else { return false }
        coordinator.receive([compact]); guard coordinator.state == .presenting else { return false }
        coordinator.allowAccess()
        guard submitted?["scopes"] as? [String] == [], submitted?["remember"] as? Bool == false else { return false }
        coordinator.invalidate()
        var upgrade = row; upgrade["kind"] = "upgrade"; upgrade["scopes"] = [String]()
        let slot = UUID().uuidString.lowercased()
        var upgradeReview: [String: Any] = ["family": "upgrade", "plan_id": UUID().uuidString.lowercased(),
            "plan_digest": String(repeating: "c", count: 64), "slot_id": slot, "expected_generation": 7,
            "package_digest": String(repeating: "d", count: 64)]
        upgrade["review"] = upgradeReview
        guard let compactUpgrade = ExternalAuthorizationRequest(upgrade), !compactUpgrade.completeReview else { return false }
        var current = review["install_effects"] as! [String: Any]
        current["profile"] = "mcp-http"
        current["package_id"] = "org.example.synthetic"; current["version"] = "1.0.0"; current["package_digest"] = String(repeating: "e", count: 64)
        current["generation"] = 7; current["public_port"] = 19385; current["tools"] = ["validate_json"]
        var candidate = current; candidate.removeValue(forKey: "generation"); candidate["expected_generation"] = 7
        candidate["package_digest"] = String(repeating: "d", count: 64)
        candidate["version"] = "2.0.0"
        let upgradeEffects: [String: Any] = ["slot_id": slot, "current": current, "candidate": candidate,
            "data_policy": "clone_before_probe", "rollback_policy": "before_exposure_only",
            "validation": "Initialize and validate the candidate against a private app-data clone before exposure."]
        upgradeReview["upgrade_effects"] = upgradeEffects; upgrade["review"] = upgradeReview
        for profile in ["mcp-stdio", "mcp-http", "model-service", "host-command"] {
            var supportedCurrent = current; supportedCurrent["profile"] = profile
            var supportedCandidate = candidate; supportedCandidate["profile"] = profile
            var supportedEffects = upgradeEffects
            supportedEffects["current"] = supportedCurrent; supportedEffects["candidate"] = supportedCandidate
            var supportedReview = upgradeReview; supportedReview["upgrade_effects"] = supportedEffects
            var supportedUpgrade = upgrade; supportedUpgrade["review"] = supportedReview
            guard let supported = ExternalAuthorizationRequest(supportedUpgrade), supported.completeReview,
                  supported.sameBinding(as: compactUpgrade) else { return false }
            supportedCandidate["profile"] = profile == "mcp-stdio" ? "mcp-http" : "mcp-stdio"
            supportedEffects["candidate"] = supportedCandidate; supportedReview["upgrade_effects"] = supportedEffects
            supportedUpgrade["review"] = supportedReview
            guard ExternalAuthorizationRequest(supportedUpgrade)?.completeReview == false else { return false }
        }
        guard let detailedUpgrade = ExternalAuthorizationRequest(upgrade), detailedUpgrade.completeReview,
              detailedUpgrade.sameBinding(as: compactUpgrade) else { return false }
        var changedReview = upgradeReview; changedReview["expected_generation"] = 8
        var changedUpgrade = upgrade; changedUpgrade["review"] = changedReview
        guard ExternalAuthorizationRequest(changedUpgrade)?.completeReview == false else { return false }
        var incompleteEffects = upgradeEffects; incompleteEffects.removeValue(forKey: "current")
        changedReview = upgradeReview; changedReview["upgrade_effects"] = incompleteEffects; changedUpgrade["review"] = changedReview
        guard ExternalAuthorizationRequest(changedUpgrade)?.completeReview == false else { return false }
        var unknownFields = candidate; unknownFields["settings"] = ["configuration": ["private": "synthetic"]]
        var injectedEffects = upgradeEffects; injectedEffects["candidate"] = unknownFields
        changedReview = upgradeReview; changedReview["upgrade_effects"] = injectedEffects; changedUpgrade["review"] = changedReview
        guard ExternalAuthorizationRequest(changedUpgrade)?.completeReview == false else { return false }
        coordinator.receive([compactUpgrade]); callbacks.last?(["request": changedUpgrade], nil)
        guard coordinator.state == .queued, coordinator.panel == nil else { return false }
        coordinator.receive([compactUpgrade]); callbacks.last?(["request": upgrade], nil)
        guard coordinator.state == .presenting, coordinator.selected?.completeReview == true else { return false }
        coordinator.allowAccess()
        guard submitted?["scopes"] as? [String] == [], submitted?["remember"] as? Bool == false else { return false }
        coordinator.invalidate(); return true
    }
}
