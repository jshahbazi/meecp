// This file contains comments only and contributes no runtime code.
// Retired native Manual setup and Have your agent do it integration.
// Preserved from codex/agent-add-app-ui at bc63e09a49ad00a10a5c2e99540a9898971a14f3.
// Shared source/resource import, installation/recovery and Manage Apps remain active.
// Restoring a workflow requires rechecking its current ownership and authorization
// contracts before reconnecting its actions, timers or saved-state restoration.

/* Retired integration reference 1
        let methods = NSSegmentedControl(labels: ["Pi agent", "Manual setup", "Have your agent do it"], trackingMode: .selectOne,
                                         target: self, action: #selector(selectSetupMethod))
        methods.selectedSegment = 0; methods.segmentStyle = .rounded
        methods.setAccessibilityLabel("App setup method"); setupMethodPicker = methods
        methods.isHidden = true
        content.addArrangedSubview(methods); workspaceFill(methods, in: content, inset: 28)

        let starter = button("Try JSON Utilities", action: #selector(useStarterApp)); self.starterButton = starter
        let starterLabel = workspaceLabel("Included with meecp. No download or build setup required.", size: 12, secondary: true)
        self.starterLabel = starterLabel
        let starterCard = workspaceCard("Try an included app", subtitle: "Give your assistant a tool that validates JSON and reports its top-level type.",
                                       views: [starterLabel, starter])
        content.addArrangedSubview(starterCard); workspaceFill(starterCard, in: content, inset: 28)

        serviceConnectionView.request = { [weak self] path, method, body, completion in
            self?.request(path, method: method, body: body, completion: completion)
        }
        serviceConnectionView.available = { [weak self] in self?.intakeEnabled == true }
        serviceConnectionView.beforeCreate = { [weak self] completion in self?.saveDraftBeforeNavigation(completion: completion) }
        serviceConnectionView.onCreated = { [weak self] object in
            guard let self, self.intakeEnabled, let draft = ImportDraft(object), draft.editable,
                  draft.sourceKind == "service_connection", draft.packagePath == nil else { return }
            self.displayDraft(draft); self.pollImports()
        }
        serviceConnectionView.onCreateSecret = { [weak self] in self?.createSecret("Local service credential") }
        let serviceCard = workspaceCard("Connect a local service", subtitle: "Use an authenticated service on this Mac without a source folder.", views: [serviceConnectionView])
        content.addArrangedSubview(serviceCard); workspaceFill(serviceCard, in: content, inset: 28)
*/

/* Retired integration reference 2
        let sourceOptionsButton = button("Add files or a project link (optional)", action: #selector(toggleSourceOptions))
        sourceOptionsButton.isBordered = false; sourceOptionsButton.imagePosition = .imageLeading
        sourceOptionsButton.alignment = .left
        sourceOptionsButton.setAccessibilityLabel("Show or hide optional app source")
        self.sourceOptionsButton = sourceOptionsButton
*/

/* Retired integration reference 3
        artifactCaptureView.available = { [weak self] in
            guard let self else { return false }
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting && !self.piSetupView.blocksNavigation && (!self.agentPreparationView.blocksNavigation || self.artifactCaptureView.blocksNavigation)
        }
        artifactCaptureView.request = { [weak self] path, method, body, completion in self?.request(path, method: method, body: body, completion: completion) }
        artifactCaptureView.beforeCapture = { [weak self] completion in self?.saveDraftBeforeNavigation(completion: completion) }
        artifactCaptureView.choices = { [weak self] in
            guard let self else { return [:] }
            var choices: [String: Any] = ["intent": self.requestedAppIntent, "start": self.startCheck?.state == .on, "start_policy": self.selectedPolicy]
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
        let draftActions = workspaceStack([save, cancel], horizontal: true, spacing: 8)
        draftActionsStack = draftActions
        let handoff = workspaceStack([copy, export], horizontal: true, spacing: 8)
        let engine = button("Choose Model Engine…", action: #selector(chooseModelEngine)); self.modelEngineButton = engine
        let profile = button("Use JevK5 Decision Tools", action: #selector(selectModelProfile)); self.modelProfileButton = profile
        let modelControls = workspaceStack([engine, profile], horizontal: true)
        let recipes = button("Manage Recipes…", action: #selector(showPreparationRecipes))
        preparationView.available = { [weak self] in
            guard let self else { return false }
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting && !self.piSetupView.blocksNavigation && (!self.agentPreparationView.blocksNavigation || self.preparationView.hasActiveJob)
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
            return !uiPreviewMode && self.connection() != nil && self.hostAvailable && !self.setupRequest && !self.stopping && !self.quitting && !self.piSetupView.blocksNavigation &&
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
        let prepareCard = workspaceCard("Prepare from your source", subtitle: "Choose source files above, save your draft, then use a supported preparation recipe.",
            views: [recipes, preparationView, workspaceCard("Use a configured preparation agent", subtitle: "Review the captured files and provider before sending them. The result returns through preparation review.", views: [agentPreparationView]), modelControls])
        let externalAgentCard = workspaceCard("Have your agent do it", subtitle: "Enter a description above, then copy the instructions to your coding agent. Your draft is saved automatically. No model setup in meecp is needed.",
            views: [handoff, workspaceLabel("When your agent finishes, import its prepared package to review and add the app. You can also import a package you already have.", size: 12, secondary: true), attach])
        externalAgentSetupCard = externalAgentCard
*/

/* Retired integration reference 4
        for card in [starterCard, serviceCard] { content.removeArrangedSubview(card); card.removeFromSuperview() }
        let manual = workspaceStack([starterCard, serviceCard, captureCard, publisherToggle, downloadCard, prepareCard])
        manualSetupStack = manual
        for card in [starterCard, serviceCard, captureCard, publisherToggle, downloadCard, prepareCard] { workspaceFill(card, in: manual) }
        for card in [label, configureCard, reviewCard] {
            conversation.reviewContainer.addArrangedSubview(card)
            workspaceFill(card, in: conversation.reviewContainer)
        }
        for view in [draftsRow, host, methods] {
            content.removeArrangedSubview(view); view.removeFromSuperview()
            conversation.contextContainer.addArrangedSubview(view)
            workspaceFill(view, in: conversation.contextContainer)
        }
        for card in [externalAgentCard, draftActions, manual] {
            conversation.alternateContainer.addArrangedSubview(card)
            workspaceFill(card, in: conversation.alternateContainer)
        }
*/

/* Retired integration reference 5
    @objc private func chooseOtherSetupWay(_ sender: NSMenuItem) {
        guard intakeEnabled, (0...2).contains(sender.tag) else { return }
        if usesConversationReplyComposer {
            intentField?.string = currentDraft?.intent ?? ""; conversationComposerContext = nil
        }
        setupMethodPicker?.selectedSegment = sender.tag; selectSetupMethod()
    }

    private func showOtherSetupWays() {
        guard intakeEnabled, let view = conversationView else { return }
        let menu = NSMenu(title: "Add app method")
        for (index, title) in ["MeeCP agent", "Manual setup", "Have your agent do it"].enumerated() {
            let item = NSMenuItem(title: title, action: #selector(chooseOtherSetupWay(_:)), keyEquivalent: "")
            item.tag = index; item.target = self
            item.state = setupMethodPicker?.selectedSegment == index ? .on : .off; menu.addItem(item)
        }
        menu.popUp(positioning: nil, at: NSPoint(x: 16, y: view.bounds.height - 12), in: view)
    }
*/

/* Retired integration reference 6
    private var bundledStarterPackage: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let package = resources.appendingPathComponent("starter-packages/json-utilities", isDirectory: true)
        var directory: ObjCBool = false
        let manifest = package.appendingPathComponent("meecp.package.json")
        let lock = package.appendingPathComponent("meecp.lock.json")
        let legacyManifest = package.appendingPathComponent("mcpme.package.json")
        let legacyLock = package.appendingPathComponent("mcpme.lock.json")
        guard FileManager.default.fileExists(atPath: package.path, isDirectory: &directory), directory.boolValue,
              (FileManager.default.fileExists(atPath: manifest.path) && FileManager.default.fileExists(atPath: lock.path)) ||
              (FileManager.default.fileExists(atPath: legacyManifest.path) && FileManager.default.fileExists(atPath: legacyLock.path)) else { return nil }
        return package
    }

    @objc private func useStarterApp() {
        guard intakeEnabled else { return }
        guard let package = bundledStarterPackage else {
            setupLabel?.stringValue = "The included app is unavailable in this copy of meecp."; return
        }
        beginImport(package, name: "JSON Utilities")
    }

    @objc private func togglePublisherDownload() { refreshWindow() }
    @objc private func selectSetupMethod() {
        if let draft = currentDraft, !draftDirty, setupPlan == nil, setupOperation == nil {
            setupLabel?.stringValue = draftStatus(draft)
        }
        refreshWindow()
    }
    @objc private func toggleSourceOptions() {
        sourceOptionsExpanded.toggle()
        refreshWindow()
    }
*/

/* Retired integration reference 7
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
*/

/* Retired integration reference 8
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
*/

/* Retired integration reference 9
    private func exportedDraft(completion: @escaping ([String: Any]) -> Void) {
        saveHandoffDraft { [weak self] saved in
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

    private func saveHandoffDraft(completion: @escaping (Bool) -> Void) {
        if currentDraft == nil { createPiBrief(completion: completion) }
        else { saveCurrentDraft(completion: completion) }
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
            let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "meecp-setup.json"
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
*/

/* Retired integration reference 10
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
*/

/* Retired integration reference 11
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
        let app = MeecpApp(); _ = app.makeSetupWorkspace()
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
    fileprivate static func preparationNativeAcceptance(fixture: URL, agent: Bool = false, liveAgent: Bool = false,
                                                        directoryContext: NativeDirectoryAcceptanceContext? = nil,
                                                        serviceContext: NativeRecipeAcceptanceContext? = nil) -> Bool {
        let agentContext = agent ? NativeRecipeAcceptanceContext(fixture: fixture, agent: true, liveAgent: liveAgent) : nil
        guard !liveAgent || agent, !agent || (agentContext != nil && directoryContext == nil && serviceContext == nil),
              serviceContext == nil || directoryContext == nil, serviceContext?.current() != false,
              directoryContext?.current() != false, fixture.isFileURL,
              let data = try? Data(contentsOf: fixture), data.count <= 262144,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              agent || serviceContext != nil || Set(object.keys).isSubset(of: directoryContext == nil
                ? ["state_dir", "draft_id", "output_path", "answers_by_label", "candidate_id", "recipe_id", "capture_model_path", "capture_license_path", "capture_format"]
                : ["schema_version", "state_dir", "output_path", "source_directory", "fixture_nonce", "answers_by_label"]),
              let state = preparationText(object["state_dir"], maximum: 4096), state.hasPrefix("/"),
              object["draft_id"] == nil || preparationUUID(object["draft_id"]) != nil,
              let output = preparationText(object["output_path"], maximum: 4096), output.hasPrefix("/"),
              !FileManager.default.fileExists(atPath: output),
              let answers = object["answers_by_label"] as? [String: Any], answers.count <= 256,
              preparationJSON(answers, maximum: 262144) != nil,
              object["candidate_id"] == nil || preparationDigest(object["candidate_id"]) != nil,
              object["recipe_id"] == nil || preparationText(object["recipe_id"], maximum: 128) != nil else { return false }
        if let context = directoryContext {
            guard context.current(), state == context.state.path, output == context.output.path,
                  object["source_directory"] as? String == context.source.path else { return false }
        }
        let captured = object["capture_model_path"] != nil || object["capture_license_path"] != nil
        let modelPath = preparationText(object["capture_model_path"], maximum: 4096)
        let licensePath = preparationText(object["capture_license_path"], maximum: 4096)
        let modelFormat = object["capture_format"] as? String ?? "onnx"
        guard captured ? modelPath?.hasPrefix("/") == true && licensePath?.hasPrefix("/") == true && ["onnx", "gguf"].contains(modelFormat) : (object["draft_id"] != nil || directoryContext != nil || serviceContext != nil) && object["capture_format"] == nil else { return false }
        if let serviceContext {
            guard serviceContext.current(), state == serviceContext.state.path, output == serviceContext.output.path,
                  NSDictionary(dictionary: object).isEqual(to: serviceContext.object) else { return false }
        }
        var draftID = preparationUUID(object["draft_id"])
        let prior = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", state, 1)
        defer { if let prior { setenv("MCPME_STATE_DIR", prior, 1) } else { unsetenv("MCPME_STATE_DIR") } }
        let app = MeecpApp(); _ = app.makeSetupWorkspace()
        var stage = "connection", routes: [String] = [], pending = 0
        var handoffProposal: AgentPreparationJob?
        var receipt: [String: Any] = ["schema_version": 1]
        if let draftID { receipt["draft_id"] = draftID }
        func report(_ passed: Bool) -> Bool {
            receipt["passed"] = passed; receipt["stage"] = stage; receipt["routes"] = routes
            if agent, stage == "agent_handoff" {
                receipt["handoff_postconditions"] = nativeAgentHandoffObservation(
                    proposal: handoffProposal, form: app.preparationView.form, pending: pending,
                    agentBusy: app.agentPreparationView.busy, ordinaryBusy: app.preparationView.busy,
                    agentError: app.agentPreparationView.errorCode, ordinaryError: app.preparationView.formErrorCode,
                    preparationJobPresent: app.preparationView.job != nil, resolutionJobPresent: app.preparationView.resolutionJob != nil)
            }
            if let code = app.agentPreparationView.errorCode ?? app.preparationView.formErrorCode ?? app.artifactCaptureView.errorCode ?? app.artifactCaptureView.job?.error ?? app.preparationView.job?.error ?? app.preparationView.resolutionJob?.error { receipt["error_code"] = code }
            if let context = directoryContext { return context.report(receipt) && passed }
            guard let result = try? JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys]), result.count <= 262144 else { return false }
            if let serviceContext { return serviceContext.report(result) && passed }
            if let agentContext { return agentContext.report(result) && passed }
            guard
                  (try? result.write(to: URL(fileURLWithPath: output), options: .atomic)) != nil,
                  (try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: output)) != nil else { return false }
            return passed
        }
        var tracedStage: String?
        func awaitCondition(_ seconds: Double = 330, _ condition: () -> Bool) -> Bool {
            if agent && tracedStage != stage {
                // Fixed stage codes only, consumed in RAM by the developer
                // harness. No source, model, provider or request values.
                print("native_agent_stage=" + stage); fflush(stdout); tracedStage = stage
            }
            let deadline = Date().addingTimeInterval(directoryContext == nil || directoryContext?.publicPython == true ? seconds : min(seconds, 30))
            while !condition(), Date() < deadline, directoryContext?.current() != false,
                  agentContext?.current() != false, serviceContext?.current() != false { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
            return directoryContext?.current() != false && agentContext?.current() != false && serviceContext?.current() != false && condition()
        }
        if let serviceContext, !serviceContext.claim() { return false }
        if let agentContext, !agentContext.claim() { return false }
        guard let connectionObject = PrivateFile.read(app.stateDirectory.appendingPathComponent("manager-connection.json")),
              let connection = ManagerConnection(connectionObject) else { return report(false) }
        app.nativeAcceptanceConnection = connection; app.hostAvailable = true
        if let context = directoryContext {
            var scopedRequests = 0
            app.nativeAcceptanceScope = { context.current() }
            app.nativeAcceptanceRoute = { path, method in
                let parts = path.split(separator: "/")
                let allowed = path == "v1/imports" && ["GET", "POST"].contains(method) ||
                    path == "v1/preparations" && method == "GET" ||
                    path == "v1/secrets" && method == "GET" ||
                    parts.count == 3 && parts[0] == "v1" && parts[1] == "imports" && preparationUUID(String(parts[2])) != nil && method == "GET" ||
                    parts.count == 4 && parts[0] == "v1" && parts[1] == "imports" && preparationUUID(String(parts[2])) != nil && parts[3] == "revise" && method == "POST" ||
                    ["v1/preparations/form", "v1/preparations/plan-form", "v1/preparations/start"].contains(path) && method == "POST" ||
                    parts.count == 3 && parts[0] == "v1" && parts[1] == "preparations" && preparationUUID(String(parts[2])) != nil && method == "GET"
                let publicResolution = context.publicPython && (
                    ["v1/resolutions/plan", "v1/resolutions/start"].contains(path) && method == "POST" ||
                    path == "v1/resolutions" && method == "GET" ||
                    parts.count == 3 && parts[0] == "v1" && parts[1] == "resolutions" && preparationUUID(String(parts[2])) != nil && method == "GET")
                guard allowed || publicResolution, context.current(), scopedRequests < (context.publicPython ? 4096 : 512) else { return false }
                scopedRequests += 1
                let route = method + " " + path
                if !routes.contains(route) { routes.append(route) }
                return true
            }
            stage = "source_selection"
            // beginImport is the exact handler invoked by Choose Files and drop.
            // The private fixture supplies the NSOpenPanel selection, not clicks.
            app.beginImport(context.source)
            guard awaitCondition(30, { !app.setupRequest && app.currentDraft != nil }), let imported = app.currentDraft,
                  imported.editable,
                  (context.publicPython ? ["local_folder", "application_directory"].contains(imported.sourceKind) : imported.sourceKind == "application_directory"),
                  imported.packagePath == nil,
                  imported.sourcePath == context.source.path else { return report(false) }
            draftID = imported.id; receipt["draft_id"] = imported.id
            receipt["native_directory_selection"] = true; receipt["initial_revision"] = imported.revision
            app.intentField?.string = context.publicPython ? "Expose the selected synthetic integer tools." : "Expose the selected bounded JSON command."
            app.startCheck?.state = .on; app.policyField?.selectItem(at: 1); app.setupChanged()
            var saved: Bool?
            app.saveCurrentDraft { saved = $0 }
            guard awaitCondition(30, { saved != nil }), saved == true else { return report(false) }
            receipt["source_selection_revision"] = app.currentDraft?.revision
        } else if let context = serviceContext {
            var scopedRequests = 0
            app.nativeAcceptanceScope = { context.current() }
            app.nativeAcceptanceRoute = { path, method in
                let parts = path.split(separator: "/")
                let allowed = method == "GET" && ["v1/service-connectors", "v1/secrets", "v1/imports", "v1/preparations", "v1/resolutions"].contains(path) ||
                    method == "POST" && ["v1/imports/service", "v1/preparations/form", "v1/preparations/plan-form", "v1/preparations/start"].contains(path) ||
                    method == "GET" && parts.count == 3 && parts[0] == "v1" && ["imports", "preparations"].contains(String(parts[1])) && preparationUUID(String(parts[2])) != nil ||
                    method == "POST" && parts.count == 4 && parts[0] == "v1" && parts[1] == "imports" && preparationUUID(String(parts[2])) != nil && parts[3] == "revise"
                guard allowed, context.current(), scopedRequests < 4096 else { return false }
                scopedRequests += 1
                let route = method + " " + path
                if !routes.contains(route) { routes.append(route) }
                return true
            }
            stage = "service_selection"
            guard let selected = object["service"] as? [String: Any], let secret = preparationUUID(selected["secret_reference"]) else { return report(false) }
            func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap { descendants($0) } }
            let views = descendants(app.serviceConnectionView)
            let fields = views.compactMap { $0 as? NSTextField }
            fields.first { $0.accessibilityLabel() == "Service app name" }?.stringValue = selected["name"] as? String ?? ""
            fields.first { $0.accessibilityLabel() == "Service model ID" }?.stringValue = selected["model"] as? String ?? ""
            fields.first { $0.accessibilityLabel() == "Service localhost port" }?.stringValue = String(preparationInteger(selected["port"]) ?? 0)
            app.serviceConnectionView.updateSecrets([(id: secret, name: "Synthetic service credential")])
            guard let picker = views.compactMap({ $0 as? NSPopUpButton }).first,
                  let choice = picker.itemArray.first(where: { $0.representedObject as? String == secret }),
                  let save = views.compactMap({ $0 as? NSButton }).first(where: { $0.title == "Save Service Connection" }) else { return report(false) }
            picker.select(choice); app.serviceConnectionView.refreshAvailability()
            guard save.isEnabled else { return report(false) }; save.performClick(nil)
            guard awaitCondition(30, { app.currentDraft != nil }), let imported = app.currentDraft,
                  imported.editable, imported.sourceKind == "service_connection", imported.sourcePath.isEmpty,
                  imported.packagePath == nil else { return report(false) }
            draftID = imported.id; receipt["draft_id"] = imported.id; receipt["initial_revision"] = imported.revision
            receipt["native_service_selection"] = true
            app.startCheck?.state = .on; app.policyField?.selectItem(at: 1); app.setupChanged()
            var saved: Bool?
            app.saveCurrentDraft { saved = $0 }
            guard awaitCondition(30, { saved != nil }), saved == true else { return report(false) }
        } else if let agentContext {
            app.nativeAcceptanceScope = { agentContext.current() }
            app.nativeAcceptanceRoute = { path, method in
                if method == "POST" {
                    return ["v1/agent-preparations/plans", "v1/agent-preparations/start", "v1/preparations/form",
                        "v1/preparations/plan-form", "v1/preparations/start", "v1/resolutions/plan", "v1/resolutions/start"].contains(path)
                }
                guard method == "GET" else { return false }
                return path == "v1/imports/" + (draftID ?? "") || ["v1/imports", "v1/resolutions", "v1/preparations"].contains(path) ||
                    ["v1/agent-preparations/jobs/", "v1/preparations/", "v1/resolutions/"].contains { prefix in
                        path.hasPrefix(prefix) && preparationUUID(String(path.dropFirst(prefix.count))) != nil
                    }
            }
        }
        let view = app.preparationView
        var requestsSent = 0
        let acceptedRequest: PreparationRequest = { path, method, body, complete in
            guard agentContext?.current() != false else { complete(nil, "native_agent_context_stale"); return }
            guard serviceContext?.current() != false else { complete(nil, "native_acceptance_context_stale"); return }
            guard requestsSent < 8192 else { complete(nil, "native_acceptance_route_limit"); return }
            requestsSent += 1
            let route = method + " " + path
            guard routes.contains(route) || routes.count < 512 else { complete(nil, "native_acceptance_route_limit"); return }
            if !routes.contains(route) { routes.append(route) }
            pending += 1
            app.request(path, method: method, body: body) { object, code in
                pending -= 1
                guard agentContext?.current() != false else { complete(nil, "native_agent_context_stale"); return }
            guard serviceContext?.current() != false else { complete(nil, "native_acceptance_context_stale"); return }
                complete(object, code)
            }
        }
        view.request = acceptedRequest; app.artifactCaptureView.request = acceptedRequest
        app.agentPreparationView.request = acceptedRequest
        func fetchDraft() -> ImportDraft? {
            guard let draftID else { return nil }
            var completed = false, received: ImportDraft?
            acceptedRequest("v1/imports/" + draftID, "GET", nil) { object, _ in received = object.flatMap(ImportDraft.init); completed = true }
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
        if agent {
            let agentView = app.agentPreparationView
            stage = "agent_options"; agentView.loadOptions()
            guard awaitCondition(30, { pending == 0 && !agentView.busy }),
                  let scope = object["agent_scope"] as? [String: Any], agentView.acceptanceSelect(scope, live: liveAgent) else { return report(false) }
            stage = "agent_review"; agentView.reviewScope()
            guard awaitCondition(60, { pending == 0 && !agentView.busy }), agentView.acceptanceReviewed(live: liveAgent, generation: scope["provider_generation"] as? String) else { return report(false) }
            stage = "agent_start"; agentView.startAnalysis()
            guard awaitCondition(60, { pending == 0 && !agentView.busy }), agentView.job != nil else { return report(false) }
            stage = "agent_poll"
            let deadline = Date().addingTimeInterval(210)
            while agentView.job?.active == true, Date() < deadline {
                agentView.poll()
                guard awaitCondition(30, { pending == 0 && !agentView.busy }) else { return report(false) }
                RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            }
            guard let proposal = agentView.job, proposal.state == "succeeded", proposal.settled,
                  proposal.candidateDigest == nil, let digest = proposal.proposalDigest,
                  proposal.context == PreparationContext(importID: draft.id, revision: draft.revision, sourceRevision: draft.sourceRevision ?? ""),
                  app.currentDraft?.revision == draft.revision, view.job == nil, view.resolutionJob == nil else { return report(false) }
            receipt["agent_job_id"] = proposal.id; receipt["proposal_digest"] = digest
            receipt["agent_revision"] = proposal.context.revision; receipt["simulated_provider"] = !liveAgent
            if liveAgent { receipt["provider_generation"] = scope["provider_generation"]; receipt["live_transport_selected"] = true }
            handoffProposal = proposal
            stage = "agent_handoff"; agentView.adoptCandidate()
            guard awaitCondition(60, { pending == 0 && !view.busy }), agentView.errorCode == nil,
                  let form = view.form, form.importID == proposal.context.importID,
                  form.revision == proposal.context.revision, form.sourceRevision == proposal.context.sourceRevision, form.resolutionRequired,
                  form.resolutionDigest == nil, view.job == nil, view.resolutionJob == nil else { return report(false) }
            receipt["pending_proposal_handoff"] = true
        } else {
            stage = "initial_form"; view.refreshForm()
        }
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
        if serviceContext != nil {
            stage = "service_review_only"
            guard !routes.contains("POST v1/preparations/start"), !routes.contains("POST v1/resolutions/start"), view.job == nil else { return report(false) }
            for (path, field) in [("v1/preparations", "preparations"), ("v1/resolutions", "resolutions")] {
                var fetched = false, empty = false
                acceptedRequest(path, "GET", nil) { object, _ in empty = (object?[field] as? [Any])?.isEmpty == true; fetched = true }
                guard awaitCondition(30, { fetched && pending == 0 }), empty else { return report(false) }
            }
            receipt["review_created_no_jobs"] = true
        }
        stage = "preparation_start"; view.startPreparation()
        guard awaitCondition(330, { pending == 0 && !view.busy }), view.job != nil else { return report(false) }
        stage = "preparation_poll"
        let deadline = Date().addingTimeInterval(directoryContext == nil ? 660 : 90)
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
        stage = "attached"; return report(directoryContext?.current() != false && agentContext?.current() != false && serviceContext?.current() != false)
    }

    fileprivate static func captureAdoptionSelfTest() -> Bool {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("meecp-capture-adoption-" + UUID().uuidString)
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
        let app = MeecpApp(); _ = app.makeSetupWorkspace()
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
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("meecp-native-form-test-" + UUID().uuidString, isDirectory: true)
        guard (try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])) != nil else { return false }
        let prior = ProcessInfo.processInfo.environment["MCPME_STATE_DIR"]
        setenv("MCPME_STATE_DIR", directory.path, 1)
        defer {
            if let prior { setenv("MCPME_STATE_DIR", prior, 1) } else { unsetenv("MCPME_STATE_DIR") }
            try? FileManager.default.removeItem(at: directory)
        }
        let app = MeecpApp(); _ = app.makeSetupWorkspace()
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

    /// Checks exclusive setup tabs and preserves shared draft inputs without contacting a host.
    fileprivate static func onboardingControlsSelfTest() -> Bool {
        let app = MeecpApp()
        _ = app.makeSetupWorkspace(); app.refreshWindow()
        guard app.starterButton?.action == #selector(useStarterApp), app.starterButton?.isEnabled == false,
              app.publisherDownloadCard?.isHidden == true, app.downloadStartButton?.isEnabled == false,
              app.savedSetupsRow?.isHidden == true, app.sourceOptionsStack?.isHidden == true,
              app.piSetupCard?.isHidden == false, app.installationReviewCard?.isHidden == true,
              app.setupMethodPicker?.selectedSegment == 0,
              app.manualSetupStack?.isHidden == true, app.externalAgentSetupCard?.isHidden == true,
              app.draftActionsStack?.isHidden == true else { return false }
        app.intentField?.string = "Keep this app description while choosing a setup method."
        app.pathField?.stringValue = "/tmp/setup-tab-source"
        app.piSourceURL?.stringValue = "https://example.com/project"
        app.setupMethodPicker?.selectedSegment = 1; app.selectSetupMethod()
        guard app.manualSetupStack?.isHidden == false, app.externalAgentSetupCard?.isHidden == true,
              app.piSetupCard?.isHidden == true, app.draftActionsStack?.isHidden == false else { return false }
        app.setupMethodPicker?.selectedSegment = 2; app.selectSetupMethod()
        guard app.manualSetupStack?.isHidden == true, app.externalAgentSetupCard?.isHidden == false,
              app.piSetupCard?.isHidden == true, app.appBriefCard?.isHidden == false,
              app.copyInstructionsButton?.action == #selector(copyAgentInstructions),
              app.exportDraftButton?.action == #selector(exportDraft),
              app.attachButton?.action == #selector(choosePackage) else { return false }
        app.setupMethodPicker?.selectedSegment = 0; app.selectSetupMethod()
        guard app.piSetupCard?.isHidden == false, app.manualSetupStack?.isHidden == true,
              app.externalAgentSetupCard?.isHidden == true, app.draftActionsStack?.isHidden == true,
              app.intentField?.string == "Keep this app description while choosing a setup method.",
              app.pathField?.stringValue == "/tmp/setup-tab-source",
              app.piSourceURL?.stringValue == "https://example.com/project" else { return false }
        app.sourceOptionsButton?.performClick(nil)
        guard app.sourceOptionsStack?.isHidden == false else { return false }
        app.sourceOptionsButton?.performClick(nil)
        guard app.sourceOptionsStack?.isHidden == true else { return false }
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
*/

/* Retired integration reference 12
        serviceConnectionView.refreshAvailability()
        let captureContext = currentDraft.flatMap { draft -> ArtifactCaptureContext? in
            guard draft.editable, draft.packagePath == nil, !draftDirty,
                  ["local_folder", "document", "captured_artifact_collection"].contains(draft.sourceKind),
                  let source = draft.sourceRevision else { return nil }
            return ArtifactCaptureContext(importID: draft.id, revision: draft.revision, sourceRevision: source)
        }
        artifactCaptureView.bind(captureContext); artifactCaptureView.refreshAvailability()
        recipesView.refreshAvailability()
*/

/* Retired integration reference 13
        preparationView.bind(preparationContext, saved: !draftDirty, eligible: currentDraft?.editable == true && currentDraft?.packagePath == nil)
        preparationView.refreshAvailability()
        let agentContext = currentDraft.flatMap { draft -> PreparationContext? in
            guard draft.editable, draft.packagePath == nil, ["local_folder", "application_directory"].contains(draft.sourceKind) else { return nil }
            return preparationContext
        }
        agentPreparationView.bind(agentContext, saved: !draftDirty); agentPreparationView.refreshAvailability()
*/

/* Retired integration reference 14
        starterButton?.isEnabled = intakeEnabled && bundledStarterPackage != nil
        starterLabel?.stringValue = bundledStarterPackage == nil ? "The included app is unavailable in this copy of meecp." :
            "Included with meecp. Review its tool and access before adding it."
        publisherDownloadCard?.isHidden = publisherDownloadToggle?.state != .on && downloadRows.isEmpty
        let manualWorkActive = preparationView.busy || preparationView.hasActiveJob || artifactCaptureView.blocksNavigation
        let agentWorkActive = agentPreparationView.blocksNavigation
        if manualWorkActive || agentWorkActive {
            setupMethodPicker?.selectedSegment = 1
        } else if piSetupView.blocksNavigation {
            setupMethodPicker?.selectedSegment = 0
        }
        let method = setupMethodPicker?.selectedSegment ?? 0
        setupMethodPicker?.isEnabled = !setupRequest && !manualWorkActive && !agentWorkActive && !piSetupView.blocksNavigation
        manualSetupStack?.isHidden = method != 1
        externalAgentSetupCard?.isHidden = method != 2
        draftActionsStack?.isHidden = method == 0
*/

/* Retired integration reference 15
        modelEngineButton?.isHidden = !model; modelProfileButton?.isHidden = !model
        modelEngineButton?.isEnabled = editable && model; modelProfileButton?.isEnabled = editable && model
        downloadStartButton?.isEnabled = intakeEnabled
        downloadPicker?.isEnabled = !uiPreviewMode && !setupRequest
        let downloadStatus = selectedDownload?["status"] as? String
        downloadCancelButton?.isEnabled = intakeEnabled && ["pending", "running"].contains(downloadStatus ?? "")
        downloadRetryButton?.isEnabled = intakeEnabled && ["failed", "canceled"].contains(downloadStatus ?? "")
        downloadUseButton?.isEnabled = intakeEnabled && downloadStatus == "completed" && selectedDownload?["package_path"] is String
*/

/* Retired integration reference 16
    private var sourceOptionsButton: NSButton?
*/

/* Retired integration reference 17
    private var saveDraftButton: NSButton?
*/

/* Retired integration reference 18
    private var copyInstructionsButton: NSButton?
*/

/* Retired integration reference 19
    private var exportDraftButton: NSButton?
*/

/* Retired integration reference 20
    private var attachButton: NSButton?
*/

/* Retired integration reference 21
    private var cancelDraftButton: NSButton?
*/

/* Retired integration reference 22
    private let artifactCaptureView = ArtifactCaptureView()
*/

/* Retired integration reference 23
    private var downloadRows: [[String: Any]] = []
*/

/* Retired integration reference 24
    private var selectedDownload: [String: Any]?
*/

/* Retired integration reference 25
    private var downloadsPolling = false
*/

/* Retired integration reference 26
    private var downloadPicker: NSPopUpButton?
*/

/* Retired integration reference 27
    private var downloadLabel: NSTextField?
*/

/* Retired integration reference 28
    private var downloadStartButton: NSButton?
*/

/* Retired integration reference 29
    private var downloadCancelButton: NSButton?
*/

/* Retired integration reference 30
    private var downloadRetryButton: NSButton?
*/

/* Retired integration reference 31
    private var downloadUseButton: NSButton?
*/

/* Retired integration reference 32
    private var starterButton: NSButton?
*/

/* Retired integration reference 33
    private var starterLabel: NSTextField?
*/

/* Retired integration reference 34
    private var publisherDownloadToggle: NSButton?
*/

/* Retired integration reference 35
    private var publisherDownloadCard: NSView?
*/

/* Retired integration reference 36
    private var draftActionsStack: NSStackView?
*/

/* Retired integration reference 37
    private var setupMethodPicker: NSSegmentedControl?
*/

/* Retired integration reference 38
    private var manualSetupStack: NSStackView?
*/

/* Retired integration reference 39
    private var externalAgentSetupCard: NSView?
*/

/* Retired integration reference 40
    private var modelEngineButton: NSButton?
*/

/* Retired integration reference 41
    private var modelProfileButton: NSButton?
*/

/* Retired integration reference 42
    private var recipesWindow: NSWindow?
*/

/* Retired integration reference 43
    private let recipesView = PreparationRecipesView()
*/

/* Retired integration reference 44
    private let preparationView = PreparationWorkspaceView()
*/

/* Retired integration reference 45
    private let serviceConnectionView = ServiceConnectionView()
*/

/* Retired integration reference 46
    private let agentPreparationView = AgentPreparationView()
*/

/* Retired integration reference 47
            if setupMethodPicker?.selectedSegment == 2 {
                return "Copy the instructions to your agent, then import its prepared package."
            }
            if setupMethodPicker?.selectedSegment == 1 {
                return "Save your draft, then choose a preparation method below."
            }
*/

/* Retired integration reference 48
if CommandLine.arguments.dropFirst().first == "--artifact-capture-ui-self-test" {
    _ = NSApplication.shared
    let passed = ArtifactCaptureView.selfTest() && MeecpApp.captureAdoptionSelfTest()
    print(passed ? "artifact_capture_ui_contracts_passed" : "artifact_capture_ui_contracts_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--preparation-ui-self-test" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count <= 2, arguments.count == 1 || arguments[1].hasPrefix("/") else { exit(2) }
    let fixture = arguments.count == 2 ? URL(fileURLWithPath: arguments[1]) : nil
    let passed = preparationFormsSelfTest(fixture: fixture) && MeecpApp.preparationBindingSelfTest(fixture: fixture)
    print(passed ? "preparation_ui_contracts_passed" : "preparation_ui_contracts_failed")
    exit(passed ? 0 : 1)
}
*/

/* Retired integration reference 49
if ["--preparation-native-acceptance", "--agent-native-acceptance", "--agent-live-native-acceptance"].contains(CommandLine.arguments.dropFirst().first ?? "") {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count == 2, arguments[1].hasPrefix("/") else { exit(2) }
    let passed = MeecpApp.preparationNativeAcceptance(fixture: URL(fileURLWithPath: arguments[1]), agent: arguments[0] != "--preparation-native-acceptance", liveAgent: arguments[0] == "--agent-live-native-acceptance")
    print(passed ? "preparation_native_acceptance_passed" : "preparation_native_acceptance_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--directory-native-acceptance" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    let context = arguments.count == 2 ? NativeDirectoryAcceptanceContext(fixture: URL(fileURLWithPath: arguments[1])) : nil
    let passed = context.map { $0.claim() && MeecpApp.preparationNativeAcceptance(fixture: $0.fixture, directoryContext: $0) } ?? false
    print(passed ? "directory_native_acceptance_passed" : "directory_native_acceptance_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--public-directory-native-acceptance" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    let context = arguments.count == 2 ? NativeDirectoryAcceptanceContext(fixture: URL(fileURLWithPath: arguments[1]), publicPython: true) : nil
    let passed = context.map { $0.claim() && MeecpApp.preparationNativeAcceptance(fixture: $0.fixture, directoryContext: $0) } ?? false
    print(passed ? "public_directory_native_acceptance_passed" : "public_directory_native_acceptance_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--service-native-acceptance" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    let context = arguments.count == 2 ? NativeRecipeAcceptanceContext(fixture: URL(fileURLWithPath: arguments[1]), service: true) : nil
    let passed = context.map { MeecpApp.preparationNativeAcceptance(fixture: $0.fixture, serviceContext: $0) } ?? false
    print(passed ? "service_native_acceptance_passed" : "service_native_acceptance_failed")
    exit(passed ? 0 : 1)
}

if CommandLine.arguments.dropFirst().first == "--recipes-native-acceptance" {
    _ = NSApplication.shared
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count == 2, arguments[1].hasPrefix("/") else { exit(2) }
    let passed = MeecpApp.recipesNativeAcceptance(fixture: URL(fileURLWithPath: arguments[1]))
    print(passed ? "recipes_native_acceptance_passed" : "recipes_native_acceptance_failed")
    exit(passed ? 0 : 1)
}
*/

