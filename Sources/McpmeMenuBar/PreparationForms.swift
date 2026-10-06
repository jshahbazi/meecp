import AppKit
import CoreFoundation
import Foundation

typealias PreparationRequest = (String, String, [String: Any]?, @escaping ([String: Any]?, String?) -> Void) -> Void

func preparationInteger(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
          abs(number.doubleValue) <= 9_007_199_254_740_991 else { return nil }
    return number.intValue
}

func preparationNumber(_ value: Any?) -> Double? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite else { return nil }
    return number.doubleValue
}

func preparationBoolean(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
    return number.boolValue
}

func preparationText(_ value: Any?, maximum: Int = 4096, empty: Bool = false) -> String? {
    guard let text = value as? String, text.unicodeScalars.count <= maximum, empty || !text.isEmpty,
          !text.unicodeScalars.contains(where: { $0.value == 0 || ($0.value < 32 && $0 != "\n" && $0 != "\t") }) else { return nil }
    return text
}

func preparationDigest(_ value: Any?) -> String? {
    guard let text = value as? String, text.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else { return nil }; return text
}

func preparationUUID(_ value: Any?) -> String? {
    guard let text = value as? String, UUID(uuidString: text)?.uuidString.lowercased() == text else { return nil }; return text
}

func preparationCode(_ value: Any?) -> String? {
    guard let text = value as? String, text.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression) != nil else { return nil }; return text
}

func preparationJSON(_ value: Any, maximum: Int = 262144) -> String? {
    guard JSONSerialization.isValidJSONObject(value),
          let compact = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes]),
          compact.count <= maximum, let compactText = String(data: compact, encoding: .utf8) else { return nil }
    // Contracts bound canonical bytes. Formatting must not change admission, or
    // allocate an enormous indented string for a deeply nested bounded object.
    var pending: [(Any, Int)] = [(value, 0)]
    while let (item, depth) = pending.popLast() {
        if depth > 32 { return compactText }
        if let object = item as? [String: Any] { pending.append(contentsOf: object.values.map { ($0, depth + 1) }) }
        else if let list = item as? [Any] { pending.append(contentsOf: list.map { ($0, depth + 1) }) }
    }
    guard let pretty = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes]),
          pretty.count <= maximum * 4, let text = String(data: pretty, encoding: .utf8) else { return compactText }
    return text
}

struct PreparationChoice {
    let id: String
    let label: String
}

struct PreparationField {
    let id: String
    let label: String
    let description: String
    let kind: String
    let required: Bool
    let constraints: [String: Any]
    let choices: [PreparationChoice]
    let preview: [String: Any]?

    init?(id: String, label: String, description: String = "", kind: String, required: Bool, constraints: [String: Any] = [:],
          choices: [PreparationChoice] = [], preview: [String: Any]? = nil) {
        guard preparationText(id, maximum: 128) != nil, !id.unicodeScalars.contains(where: { $0.value < 32 }),
              preparationText(label, maximum: 256) != nil,
              preparationText(description, maximum: 4096, empty: true) != nil,
              ["string", "integer", "number", "boolean", "choice", "file", "schema-preview", "object"].contains(kind),
              Set(constraints.keys).isSubset(of: ["min_length", "max_length", "minimum", "maximum"]),
              choices.count <= 128, Set(choices.map(\.id)).count == choices.count,
              choices.allSatisfy({ preparationText($0.id, maximum: 128) != nil && preparationText($0.label, maximum: 256) != nil }),
              kind != "choice" || !choices.isEmpty,
              kind == "choice" || choices.isEmpty,
              kind != "schema-preview" || preview != nil && preparationJSON(preview!, maximum: 16384) != nil,
              kind == "schema-preview" || preview == nil else { return nil }
        for key in constraints.keys {
            if ["min_length", "max_length"].contains(key) {
                guard ["string", "file"].contains(kind), let number = preparationInteger(constraints[key]), (0...16384).contains(number) else { return nil }
            } else {
                guard ["integer", "number"].contains(kind), preparationNumber(constraints[key]) != nil else { return nil }
            }
        }
        if let minimum = preparationNumber(constraints["minimum"]), let maximum = preparationNumber(constraints["maximum"]), minimum > maximum { return nil }
        if let minimum = preparationInteger(constraints["min_length"]), let maximum = preparationInteger(constraints["max_length"]), minimum > maximum { return nil }
        self.id = id; self.label = label; self.description = description; self.kind = kind; self.required = required
        self.constraints = constraints; self.choices = choices; self.preview = preview
    }

    func accepts(_ value: Any) -> Bool {
        switch kind {
        case "string", "file":
            guard let text = preparationText(value, maximum: 65536, empty: true),
                  text.unicodeScalars.count >= (preparationInteger(constraints["min_length"]) ?? 0),
                  text.unicodeScalars.count <= (preparationInteger(constraints["max_length"]) ?? 16384) else { return false }
            return kind != "file" || text.hasPrefix("/")
        case "integer", "number":
            guard let number = preparationNumber(value), kind != "integer" || preparationInteger(value) != nil,
                  number >= (preparationNumber(constraints["minimum"]) ?? -Double.greatestFiniteMagnitude),
                  number <= (preparationNumber(constraints["maximum"]) ?? Double.greatestFiniteMagnitude) else { return false }
            return true
        case "boolean": return preparationBoolean(value) != nil
        case "choice": return (value as? String).map { value in choices.contains { $0.id == value } } == true
        case "object": return value is [String: Any] && preparationJSON(value, maximum: 16384) != nil
        case "schema-preview": return false
        default: return false
        }
    }
}

final class PreparationFormView: NSStackView, NSTextFieldDelegate, NSTextViewDelegate {
    var onChange: (() -> Void)?
    private var fields: [PreparationField] = []
    private var controls: [String: NSView] = [:]
    private var included: [String: NSButton] = [:]
    private var editable = false
    private var generation: UInt64 = 0

    override init(frame: NSRect) { super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 10 }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func configure(fields: [PreparationField], values: [String: Any]) {
        generation &+= 1
        for view in arrangedSubviews { removeArrangedSubview(view); view.removeFromSuperview() }
        self.fields = fields; controls = [:]; included = [:]
        for field in fields {
            if field.kind == "schema-preview" {
                let text = NSTextField(wrappingLabelWithString: preparationJSON(field.preview ?? [:], maximum: 16384) ?? "Schema preview unavailable")
                text.isSelectable = true; text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
                let details = WorkspaceDisclosure(field.label, views: [text])
                addArrangedSubview(details); workspaceFill(details, in: self); continue
            }
            let include = NSButton(checkboxWithTitle: field.label + (field.required ? " (required)" : " (optional)"), target: self, action: #selector(changed))
            include.state = field.required || values[field.id] != nil ? .on : .off; included[field.id] = include
            let input: NSView
            switch field.kind {
            case "boolean", "choice":
                let picker = NSPopUpButton(); picker.target = self; picker.action = #selector(changed)
                if field.kind == "boolean" {
                    picker.addItems(withTitles: ["Off", "On"])
                    picker.item(at: 0)?.representedObject = false; picker.item(at: 1)?.representedObject = true
                    if preparationBoolean(values[field.id]) == true { picker.selectItem(at: 1) }
                } else {
                    picker.addItem(withTitle: "Choose…")
                    for choice in field.choices {
                        picker.addItem(withTitle: choice.label); picker.lastItem?.representedObject = choice.id
                        if values[field.id] as? String == choice.id { picker.select(picker.lastItem) }
                    }
                }
                input = picker
            case "object":
                let text = NSTextView(); text.delegate = self; text.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
                text.string = (values[field.id] as? [String: Any]).flatMap { preparationJSON($0, maximum: 16384) } ?? "{}"
                let scroll = NSScrollView(); scroll.borderType = .bezelBorder; scroll.hasVerticalScroller = true
                scroll.documentView = text; scroll.heightAnchor.constraint(equalToConstant: 90).isActive = true
                text.autoresizingMask = [.width]; text.textContainer?.widthTracksTextView = true; controls[field.id] = text
                input = scroll
            default:
                let text = NSTextField(); text.delegate = self
                text.placeholderString = field.kind == "integer" ? "Whole number" : field.kind == "number" ? "Number" : "Value"
                if let value = values[field.id] { text.stringValue = (value as? String) ?? String(describing: value) }
                if field.kind == "file" {
                    text.isEditable = false
                    let choose = NSButton(title: "Choose File…", target: self, action: #selector(chooseFile(_:)))
                    choose.identifier = NSUserInterfaceItemIdentifier(field.id); choose.bezelStyle = .rounded
                    controls[field.id] = text; input = workspaceStack([text, choose], horizontal: true)
                } else { input = text }
            }
            if controls[field.id] == nil { controls[field.id] = input }
            controls[field.id]?.setAccessibilityLabel(field.label)
            addArrangedSubview(include); addArrangedSubview(input); workspaceFill(input, in: self)
            if !field.description.isEmpty {
                let description = workspaceLabel(field.description, size: 12); description.textColor = .secondaryLabelColor
                addArrangedSubview(description); workspaceFill(description, in: self)
            }
        }
        setEditable(editable)
    }

    func setEditable(_ value: Bool) {
        editable = value
        for field in fields {
            included[field.id]?.isEnabled = value && !field.required
            let active = value && included[field.id]?.state == .on
            if let control = controls[field.id] as? NSControl { control.isEnabled = active }
            if let text = controls[field.id] as? NSTextView { text.isEditable = active }
        }
        for row in arrangedSubviews {
            if let stack = row as? NSStackView {
                for button in stack.arrangedSubviews.compactMap({ $0 as? NSButton }) { button.isEnabled = value && included[button.identifier?.rawValue ?? ""]?.state == .on }
            }
        }
    }

    func answers() -> [String: Any]? {
        var answers: [String: Any] = [:]
        for field in fields where field.kind != "schema-preview" && included[field.id]?.state == .on {
            let value: Any?
            if let picker = controls[field.id] as? NSPopUpButton { value = picker.selectedItem?.representedObject }
            else if let text = controls[field.id] as? NSTextView {
                guard text.string.utf8.count <= 65536, let data = text.string.data(using: .utf8) else { return nil }
                value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            } else if let text = controls[field.id] as? NSTextField {
                if field.kind == "integer" { value = Int(text.stringValue).map { NSNumber(value: $0) } }
                else if field.kind == "number" { value = Double(text.stringValue).map { NSNumber(value: $0) } }
                else { value = text.stringValue }
            } else { return nil }
            guard let value, field.accepts(value) else { return nil }; answers[field.id] = value
        }
        return answers
    }
    func controlTextDidChange(_ notification: Notification) { onChange?() }
    func textDidChange(_ notification: Notification) { onChange?() }
    @objc private func changed() { setEditable(editable); onChange?() }
    @objc private func chooseFile(_ button: NSButton) {
        guard editable, let id = button.identifier?.rawValue, included[id]?.state == .on else { return }
        let panel = NSOpenPanel(); panel.canChooseFiles = true; panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        let expected = generation
        panel.begin { [weak self] result in
            guard let self, self.generation == expected, self.editable, self.included[id]?.state == .on,
                  result == .OK, let url = panel.url, let text = self.controls[id] as? NSTextField else { return }
            text.stringValue = url.path; self.onChange?()
        }
    }
}

struct PreparationRecipeReference: Equatable {
    let id: String
    let version: String
    let digest: String
    init?(_ object: [String: Any]) {
        guard Set(object.keys) == ["id", "version", "digest"],
              let identity = PreparationRecipeIdentity(["recipe_id": object["id"] as Any, "version": object["version"] as Any, "digest": object["digest"] as Any]) else { return nil }
        id = identity.id; version = identity.version; digest = identity.digest
    }
    var object: [String: Any] { ["id": id, "version": version, "digest": digest] }
}

struct OnboardingFormV1 {
    let importID: String
    let revision: Int
    let sourceRevision: String
    let digest: String
    let candidateID: String?
    let recipe: PreparationRecipeReference?
    let candidates: [PreparationChoice]
    let recipes: [(choice: PreparationChoice, reference: PreparationRecipeReference)]
    let fields: [PreparationField]
    let values: [String: Any]
    let tools: [[String: Any]]
    let blockers: [String]
    let executionAvailable: Bool
    let resolutionRequired: Bool
    let resolutionOperations: [String]
    let resolutionDigest: String?
    let resolutionAvailable: Bool
    let resolutionPolicy: PreparationResolutionPolicy?

    var unavailableMessage: String? {
        if candidates.isEmpty {
            return "Guided setup couldn't find a supported app entry point in this folder. Choose Use my coding agent to create a prepared package."
        }
        guard candidateID != nil, recipe != nil else { return nil }
        if resolutionRequired ? !resolutionAvailable : !executionAvailable {
            return "This copy of mcpme can't prepare this app automatically. Choose Use my coding agent, then import the package it creates."
        }
        return nil
    }

    init?(_ object: [String: Any]) {
        let keys: Set<String> = ["schema_version", "import_id", "revision", "source_revision", "form_digest", "candidate_id", "recipe",
            "candidate_choices", "recipe_choices", "fields", "tools", "blockers", "execution_available"]
        let resolutionKeys: Set<String> = ["resolution_required", "resolution_operations", "resolution_digest", "resolution_available", "resolution_policy"]
        guard keys.isSubset(of: Set(object.keys)), Set(object.keys).isSubset(of: keys.union(resolutionKeys).union(["api_version"])),
              preparationInteger(object["schema_version"]) == 1, let id = preparationUUID(object["import_id"]),
              let revision = preparationInteger(object["revision"]), revision > 0,
              let source = preparationText(object["source_revision"], maximum: 256), let digest = preparationDigest(object["form_digest"]),
              let candidates = object["candidate_choices"] as? [[String: Any]], candidates.count <= 128,
              let recipes = object["recipe_choices"] as? [[String: Any]], recipes.count <= 128,
              let fieldRows = object["fields"] as? [[String: Any]], fieldRows.count <= 256,
              let tools = object["tools"] as? [[String: Any]], tools.count <= 128,
              let blockers = object["blockers"] as? [String], blockers.count <= 128, blockers.allSatisfy({ preparationCode($0) != nil }),
              let available = preparationBoolean(object["execution_available"]), preparationJSON(object, maximum: 1_048_576) != nil else { return nil }
        var candidateChoices: [PreparationChoice] = []
        for choice in candidates {
            guard Set(choice.keys) == ["id", "label"], let id = preparationDigest(choice["id"]), let label = preparationText(choice["label"], maximum: 256) else { return nil }
            candidateChoices.append(PreparationChoice(id: id, label: label))
        }
        var recipeChoices: [(choice: PreparationChoice, reference: PreparationRecipeReference)] = []
        for choice in recipes {
            guard Set(choice.keys) == ["id", "label", "recipe"], let id = preparationDigest(choice["id"]), let label = preparationText(choice["label"], maximum: 256),
                  let value = choice["recipe"] as? [String: Any], let reference = PreparationRecipeReference(value) else { return nil }
            recipeChoices.append((PreparationChoice(id: id, label: label), reference))
        }
        guard Set(candidateChoices.map(\.id)).count == candidateChoices.count, Set(recipeChoices.map { $0.choice.id }).count == recipeChoices.count else { return nil }
        let candidateID = preparationDigest(object["candidate_id"])
        if !(object["candidate_id"] is NSNull), candidateID == nil { return nil }
        let recipe = (object["recipe"] as? [String: Any]).flatMap(PreparationRecipeReference.init)
        if !(object["recipe"] is NSNull), recipe == nil { return nil }
        if let candidateID, !candidateChoices.contains(where: { $0.id == candidateID }) { return nil }
        if let recipe, !recipeChoices.contains(where: { $0.reference == recipe }) { return nil }
        var fields: [PreparationField] = [], values: [String: Any] = [:]
        for row in fieldRows {
            guard Set(row.keys) == ["id", "label", "description", "kind", "required", "constraints", "choices", "value", "schema"],
                  let id = preparationDigest(row["id"]), let label = preparationText(row["label"], maximum: 256),
                  let description = preparationText(row["description"], maximum: 4096, empty: true),
                  let kind = row["kind"] as? String, let required = preparationBoolean(row["required"]),
                  let constraints = row["constraints"] as? [String: Any], let choices = row["choices"] as? [[String: Any]], choices.count <= 128 else { return nil }
            let parsed = choices.compactMap { choice -> PreparationChoice? in
                guard Set(choice.keys) == ["id", "label"], let id = preparationText(choice["id"], maximum: 128), let label = preparationText(choice["label"], maximum: 256) else { return nil }
                return PreparationChoice(id: id, label: label)
            }
            guard parsed.count == choices.count, row["schema"] is NSNull || row["schema"] is [String: Any],
                  let field = PreparationField(id: id, label: label, description: description, kind: kind, required: required, constraints: constraints,
                    choices: parsed, preview: kind == "schema-preview" ? row["schema"] as? [String: Any] : nil) else { return nil }
            if !(row["value"] is NSNull), kind != "schema-preview" {
                guard let value = row["value"], field.accepts(value) else { return nil }; values[id] = value
            }
            fields.append(field)
        }
        guard Set(fields.map(\.id)).count == fields.count,
              tools.allSatisfy({ preparationText($0["name"], maximum: 128) != nil && $0["input_schema"] is [String: Any] }),
              Set(tools.compactMap { $0["name"] as? String }).count == tools.count else { return nil }
        self.importID = id; self.revision = revision; self.sourceRevision = source; self.digest = digest
        self.candidateID = candidateID; self.recipe = recipe; self.candidates = candidateChoices; self.recipes = recipeChoices
        self.fields = fields; self.values = values; self.tools = tools; self.blockers = blockers; self.executionAvailable = available
        if resolutionKeys.isDisjoint(with: Set(object.keys)) {
            resolutionRequired = false; resolutionOperations = []; resolutionDigest = nil; resolutionAvailable = false; resolutionPolicy = nil
        } else {
            guard resolutionKeys.isSubset(of: Set(object.keys)), let required = preparationBoolean(object["resolution_required"]),
                  let operations = object["resolution_operations"] as? [String], operations.count <= 2,
                  Set(operations).count == operations.count, Set(operations).isSubset(of: ["resolve_dependencies", "discover_interface"]),
                  let resolutionAvailable = preparationBoolean(object["resolution_available"]) else { return nil }
            let digest = preparationDigest(object["resolution_digest"])
            let policy = (object["resolution_policy"] as? [String: Any]).flatMap(PreparationResolutionPolicy.init)
            guard object["resolution_digest"] is NSNull || digest != nil,
                  object["resolution_policy"] is NSNull || policy != nil,
                  !required || (candidateID != nil && digest == nil && !operations.isEmpty && policy?.operations == operations),
                  !resolutionAvailable || policy != nil,
                  policy == nil || policy?.operations == operations else { return nil }
            resolutionRequired = required; resolutionOperations = operations; resolutionDigest = digest
            self.resolutionAvailable = resolutionAvailable; resolutionPolicy = policy
        }
    }
}

struct PreparationJob {
    let id: String
    let importID: String
    let revision: Int
    let state: String
    let progress: [String: Int]
    let stage: String
    let error: String?
    let sourceRevision: String?
    let updatedAt: Double
    static let terminal: Set<String> = ["succeeded", "canceled", "failed", "interrupted"]
    init?(_ object: [String: Any]) {
        let binding = object["binding"] as? [String: Any] ?? object
        guard let id = preparationUUID(object["job_id"]), let importID = preparationUUID(binding["import_id"]),
              let revision = preparationInteger(binding["revision"]), revision > 0,
              let state = preparationCode(object["state"]), ["queued", "snapshotting", "preparing", "verifying", "ready_to_attach", "attaching", "succeeded", "cancel_requested", "canceled", "failed", "interrupted"].contains(state),
              let updatedAt = preparationNumber(object["updated_at"]), updatedAt >= 0,
              let progress = object["progress"] as? [String: Any], let stage = preparationCode(progress["stage"]),
              ["queued", "snapshotting", "dependency_export", "dependency_acquisition", "dependency_install", "private_python_version", "private_uv_version", "private_python_metadata", "onnx_model_validation", "sealing", "verifying", "attaching", "settling", "succeeded"].contains(stage) else { return nil }
        var counters: [String: Int] = [:]
        let limits = ["commands_finished": 4096, "wheels_acquired": 4096, "bytes_acquired": 68_719_476_736, "files_checked": 200000, "fixtures_checked": 64]
        guard Set(progress.keys).isSubset(of: Set(limits.keys).union(["stage"])) else { return nil }
        for (key, limit) in limits {
            guard let value = preparationInteger(progress[key]), (0...limit).contains(value) else { return nil }; counters[key] = value
        }
        let error = preparationCode(object["error_code"])
        guard object["error_code"] == nil || object["error_code"] is NSNull || error != nil else { return nil }
        self.id = id; self.importID = importID; self.revision = revision; self.state = state; self.progress = counters; self.stage = stage; self.error = error
        self.sourceRevision = preparationText(binding["source_revision"], maximum: 256); self.updatedAt = updatedAt
    }
    var active: Bool { !Self.terminal.contains(state) }
    var summary: String {
        let stages = ["queued": "Waiting to begin", "snapshotting": "Saving selected inputs", "dependency_export": "Reviewing locked dependencies",
            "dependency_acquisition": "Acquiring reviewed dependencies", "dependency_install": "Preparing dependencies", "private_python_version": "Checking packaged runtime",
            "private_uv_version": "Checking packaged build tools", "private_python_metadata": "Checking runtime metadata", "onnx_model_validation": "Verifying the image detector", "sealing": "Sealing the prepared package",
            "verifying": "Verifying tools and package", "attaching": "Attaching the prepared package", "settling": "Finishing owned work", "succeeded": "Complete"]
        return state.replacingOccurrences(of: "_", with: " ").capitalized + " · " + stages[stage]! + "\n" +
            "Commands: \(progress["commands_finished"] ?? 0) · Wheels: \(progress["wheels_acquired"] ?? 0) · Bytes acquired: \(progress["bytes_acquired"] ?? 0)\n" +
            "Files checked: \(progress["files_checked"] ?? 0) · Fixtures checked: \(progress["fixtures_checked"] ?? 0)" + (error.map { "\n" + $0 } ?? "")
    }
}

struct PreparationContext: Equatable {
    let importID: String
    let revision: Int
    let sourceRevision: String
    init?(importID: String, revision: Int, sourceRevision: String) {
        guard preparationUUID(importID) != nil, revision > 0, preparationText(sourceRevision, maximum: 256) != nil else { return nil }
        self.importID = importID; self.revision = revision; self.sourceRevision = sourceRevision
    }
    init?(_ draft: [String: Any]) {
        guard let id = draft["draft_id"] as? String, let revision = preparationInteger(draft["revision"]),
              let source = draft["source"] as? [String: Any], let identity = source["revision"] as? String else { return nil }
        self.init(importID: id, revision: revision, sourceRevision: identity)
    }
}

struct PreparationReview {
    let context: PreparationContext
    let formDigest: String
    let planID: String?
    let planDigest: String?
    let expiresAt: Double?
    let executionAvailable: Bool
    let blockers: [String]
    let draft: [String: Any]
    let display: String

    init?(_ object: [String: Any], form: OnboardingFormV1) {
        let required: Set<String> = ["schema_version", "import_id", "revision", "source_revision", "form_digest", "execution_available", "plan_id", "plan_digest", "expires_at", "tools", "access", "resources", "downloads", "blockers", "draft"]
        let optional: Set<String> = ["api_version", "effects", "execution_policy", "recipe", "descriptor_digest", "resolution_digest"]
        guard required.isSubset(of: Set(object.keys)), Set(object.keys).isSubset(of: required.union(optional)),
              preparationInteger(object["schema_version"]) == 1, object["import_id"] as? String == form.importID,
              let revision = preparationInteger(object["revision"]), revision >= form.revision,
              object["source_revision"] as? String == form.sourceRevision,
              object["form_digest"] as? String == form.digest,
              let draft = object["draft"] as? [String: Any], let context = PreparationContext(draft),
              context.importID == form.importID, context.revision == revision, context.sourceRevision == form.sourceRevision,
              let available = preparationBoolean(object["execution_available"]),
              object["resolution_digest"] == nil || (object["resolution_digest"] is NSNull && form.resolutionDigest == nil) ||
                  (preparationDigest(object["resolution_digest"]) == form.resolutionDigest && form.resolutionDigest != nil),
              let blockers = object["blockers"] as? [String], blockers.count <= 128, blockers.allSatisfy({ preparationCode($0) != nil }),
              let tools = object["tools"] as? [[String: Any]], tools.count <= 128,
              tools.allSatisfy({ preparationText($0["name"], maximum: 128) != nil && $0["input_schema"] is [String: Any] }),
              Set(tools.compactMap { $0["name"] as? String }).count == tools.count,
              object["access"] is [String: Any], object["resources"] is [String: Any],
              let downloads = object["downloads"] as? [[String: Any]], downloads.count <= 128,
              preparationJSON(object, maximum: 1_048_576) != nil else { return nil }
        let planID = preparationUUID(object["plan_id"]), planDigest = preparationDigest(object["plan_digest"]), expiresAt = preparationNumber(object["expires_at"])
        guard object["plan_id"] is NSNull || planID != nil, object["plan_digest"] is NSNull || planDigest != nil,
              object["expires_at"] is NSNull || expiresAt.map({ $0 > 0 }) == true,
              (planID == nil && planDigest == nil && expiresAt == nil) || (planID != nil && planDigest != nil && expiresAt != nil),
              !available || (planID != nil && blockers.isEmpty) else { return nil }
        guard let display = PreparationReviewSummary.render(draft: draft, tools: tools,
            access: object["access"] as! [String: Any], resources: object["resources"] as! [String: Any],
            policy: object["execution_policy"] as? [String: Any], downloads: downloads, available: available, blockers: blockers) else { return nil }
        if let policy = object["execution_policy"] as? [String: Any], preparationInteger(policy["schema_version"]) == 2 {
            guard let grant = policy["service_probe"] as? [String: Any], let digest = preparationDigest(object["descriptor_digest"]),
                  grant["descriptor_digest"] as? String == digest else { return nil }
        }
        self.context = context; self.formDigest = form.digest; self.planID = planID; self.planDigest = planDigest; self.expiresAt = expiresAt
        self.executionAvailable = available; self.blockers = blockers; self.draft = draft; self.display = display
    }
    func canStart(at time: Double = Date().timeIntervalSince1970) -> Bool {
        executionAvailable && blockers.isEmpty && planID != nil && planDigest != nil && (expiresAt ?? 0) > time
    }
}

private enum PreparationReviewSummary {
    private static func names(_ value: Any?, maximum: Int = 4096) -> [String]? {
        guard let list = value as? [String], list.count <= 128, list.allSatisfy({ preparationText($0, maximum: maximum) != nil }) else { return nil }; return list
    }
    private static func list(_ values: [String]) -> String { values.isEmpty ? "None declared" : values.joined(separator: ", ") }
    private static func bytes(_ value: Int) -> String { ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .file) }
    static func render(draft: [String: Any], tools: [[String: Any]], access: [String: Any], resources: [String: Any],
                       policy: [String: Any]?, downloads: [[String: Any]], available: Bool, blockers: [String]) -> String? {
        guard let name = preparationText(draft["name"], maximum: 256), let files = names(access["filesystem"]),
              let network = names(access["network"]), let secrets = names(access["secrets"], maximum: 256), secrets.count <= 16,
              let writes = preparationBoolean(access["writes"]), let memory = preparationInteger(resources["memory_mb"]), (1...4096).contains(memory), downloads.isEmpty else { return nil }
        var lines = [name, "", "Tools"]
        if tools.isEmpty { lines.append("No tools resolved yet.") }
        for tool in tools {
            guard let name = preparationText(tool["name"], maximum: 128) else { return nil }
            let description: String
            if tool["description"] == nil || tool["description"] is NSNull { description = "" }
            else { guard let text = preparationText(tool["description"], maximum: 4096, empty: true) else { return nil }; description = text }
            lines.append("- " + name + (description.isEmpty ? "" : ": " + description))
        }
        lines += ["", "App access and resources", "Files: " + list(files), "Network: " + list(network),
            "Writes: " + (writes ? "Declared by this app" : "None declared"), "Saved secret access: " + list(secrets), "Memory: \(memory) MB"]
        lines += ["", "Preparation scope and limits"]
        if let policy {
            guard let policyVersion = preparationInteger(policy["schema_version"]), [1, 2].contains(policyVersion),
                  let operations = names(policy["operations"], maximum: 64), (1...2).contains(operations.count),
                  Set(operations).count == operations.count, Set(operations).isSubset(of: ["build", "probe"]),
                  let network = names(policy["network"], maximum: 256), network.count <= 32,
                  let seconds = preparationInteger(policy["wall_seconds"]), (1...3600).contains(seconds),
                  let disk = preparationInteger(policy["disk_bytes"]), (1...68_719_476_736).contains(disk),
                  let memory = preparationInteger(policy["memory_mb"]), (1...4096).contains(memory) else { return nil }
            lines += ["Work: " + list(operations.map { $0.replacingOccurrences(of: "_", with: " ").capitalized }),
                "Network: " + list(network), "Deadline: \(seconds) seconds", "Disk budget: " + bytes(disk), "Memory budget: \(memory) MB"]
            if policyVersion == 2 {
                guard Set(policy.keys) == ["schema_version", "operations", "network", "wall_seconds", "disk_bytes", "memory_mb", "service_probe"],
                      let service = ServiceConnectionContract.probeSummary(policy["service_probe"], draft: draft) else { return nil }
                lines += service
            } else if policy["service_probe"] != nil { return nil }
        } else {
            guard !available else { return nil }; lines.append("The host has not supplied an executable preparation scope.")
        }
        lines += ["", "Selected downloads"]
        lines += ["No additional app artifacts selected.", "Preparation may acquire locked dependencies from the reviewed hosts."]
        lines += ["", available ? "Preparation is available for this reviewed plan." : "Automatic preparation is unavailable on this host."]
        let explanations = ["preparation_runner_unavailable": "The preparation runner is unavailable.",
            "independent_runtime_verifier_unavailable": "Independent package verification is unavailable.",
            "candidate_required": "Choose a detected candidate.", "recipe_required": "Choose a compatible recipe.",
            "required_input_missing": "Complete the required questions.", "dynamic_tool_discovery_required": "The host must resolve the app's tools before preparing it."]
        for blocker in blockers { lines.append("- " + (explanations[blocker] ?? blocker.replacingOccurrences(of: "_", with: " ").capitalized)) }
        let display = lines.joined(separator: "\n")
        return display.utf8.count <= 1_048_576 ? display : nil
    }
}

final class PreparationWorkspaceView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var onUpdatedDraft: (([String: Any]) -> Bool)?
    var onStateChange: (() -> Void)?
    var onAttached: (() -> Void)?
    private let candidatePicker = NSPopUpButton()
    private let recipePicker = NSPopUpButton()
    private var candidateRow: NSView?
    private var recipeRow: NSView?
    private let formView = PreparationFormView()
    private let status = NSTextField(wrappingLabelWithString: "Save a draft to load its preparation questions.")
    private let progress = NSTextField(wrappingLabelWithString: "")
    private let reviewText = NSTextView()
    private let resolutionStatus = NSTextField(wrappingLabelWithString: "")
    private let resolutionProgress = NSTextField(wrappingLabelWithString: "")
    private var resolutionReviewButton: NSButton!
    private var resolutionStartButton: NSButton!
    private var resolutionCancelButton: NSButton!
    private var resolutionRecoverButton: NSButton!
    private var refreshButton: NSButton!
    private var reviewButton: NSButton!
    private var startButton: NSButton!
    private var cancelButton: NSButton!
    private var context: PreparationContext?
    private var saved = false
    private var eligible = false
    private var epoch: UInt64 = 0
    private var polling = false
    private(set) var busy = false
    private(set) var form: OnboardingFormV1?
    // Diagnostic only: this never participates in execution/control admission.
    private(set) var formErrorCode: String?
    private(set) var review: PreparationReview?
    private(set) var job: PreparationJob?
    private(set) var resolutionReview: PreparationResolutionReview?
    private(set) var resolutionJob: PreparationResolutionJob?
    private var resolutionStartKey: String?
    private var resolutionNotification: String?
    private var resolutionPolling = false
    private var startKey: String?
    private var attachedNotification: String?
    var hasActiveJob: Bool { job?.active == true || resolutionJob?.active == true }

    override init(frame: NSRect) {
        super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 10
        candidatePicker.target = self; candidatePicker.action = #selector(selectionChanged(_:))
        recipePicker.target = self; recipePicker.action = #selector(selectionChanged(_:))
        candidatePicker.setAccessibilityLabel("Detected candidate"); recipePicker.setAccessibilityLabel("Exact preparation recipe version")
        for picker in [candidatePicker, recipePicker] {
            picker.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            picker.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
            picker.widthAnchor.constraint(lessThanOrEqualToConstant: 300).isActive = true
        }
        let candidateRow = workspaceStack([workspaceLabel("App to prepare", size: 12), candidatePicker], horizontal: true)
        let recipeRow = workspaceStack([workspaceLabel("Setup template", size: 12), recipePicker], horizontal: true)
        self.candidateRow = candidateRow; self.recipeRow = recipeRow
        for row in [candidateRow, recipeRow] { addArrangedSubview(row); workspaceFill(row, in: self) }
        addArrangedSubview(resolutionStatus); workspaceFill(resolutionStatus, in: self)
        resolutionReviewButton = NSButton(title: "Review Dependency Resolution", target: self, action: #selector(reviewResolution))
        resolutionStartButton = NSButton(title: "Resolve Reviewed Inputs", target: self, action: #selector(startResolution))
        resolutionCancelButton = NSButton(title: "Cancel Resolution", target: self, action: #selector(cancelResolution))
        resolutionRecoverButton = NSButton(title: "Recover Resolution", target: self, action: #selector(recoverResolution))
        for button in [resolutionReviewButton!, resolutionStartButton!, resolutionCancelButton!, resolutionRecoverButton!] { button.bezelStyle = .rounded }
        addArrangedSubview(workspaceStack([resolutionReviewButton, resolutionStartButton, resolutionCancelButton, resolutionRecoverButton], horizontal: true))
        addArrangedSubview(resolutionProgress); workspaceFill(resolutionProgress, in: self)
        addArrangedSubview(formView); workspaceFill(formView, in: self)
        formView.onChange = { [weak self] in
            guard let self else { return }; self.epoch &+= 1; self.polling = false; self.review = nil; self.startKey = nil
            self.reviewText.string = ""; self.status.stringValue = "Review these answers before preparing."; self.updateControls(); self.onStateChange?()
        }
        addArrangedSubview(status); workspaceFill(status, in: self)
        refreshButton = NSButton(title: "Continue Setup", target: self, action: #selector(refreshForm))
        reviewButton = NSButton(title: "Review Preparation", target: self, action: #selector(reviewForm))
        startButton = NSButton(title: "Prepare Automatically", target: self, action: #selector(startPreparation))
        cancelButton = NSButton(title: "Cancel Preparation", target: self, action: #selector(cancelPreparation))
        for button in [refreshButton!, reviewButton!, startButton!, cancelButton!] { button.bezelStyle = .rounded }
        addArrangedSubview(workspaceStack([refreshButton, reviewButton, startButton, cancelButton], horizontal: true))
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        reviewText.isEditable = false; reviewText.isSelectable = true; reviewText.font = .systemFont(ofSize: 13)
        reviewText.autoresizingMask = [.width]; reviewText.isVerticallyResizable = true; reviewText.textContainer?.widthTracksTextView = true
        reviewText.textContainerInset = NSSize(width: 8, height: 8); reviewText.setAccessibilityLabel("Reviewed preparation tools, access, resources and downloads")
        scroll.documentView = reviewText; scroll.heightAnchor.constraint(equalToConstant: 190).isActive = true
        addArrangedSubview(scroll); workspaceFill(scroll, in: self)
        addArrangedSubview(progress); workspaceFill(progress, in: self); updateControls()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func bind(_ next: PreparationContext?, saved: Bool, eligible: Bool) {
        if context != next || self.saved != saved || self.eligible != eligible {
            let sameSource = context?.importID == next?.importID && context?.sourceRevision == next?.sourceRevision
            epoch &+= 1; busy = false; polling = false; resolutionPolling = false; form = nil; formErrorCode = nil; review = nil; startKey = nil; resolutionReview = nil; resolutionStartKey = nil
            formView.configure(fields: [], values: [:]); candidatePicker.removeAllItems(); recipePicker.removeAllItems(); reviewText.string = ""
            if !sameSource { job = nil; attachedNotification = nil; progress.stringValue = ""; resolutionJob = nil; resolutionNotification = nil; resolutionProgress.stringValue = "" }
            resolutionStatus.stringValue = ""
            context = next; self.saved = saved; self.eligible = eligible
            status.stringValue = next == nil ? "Choose a source to begin." : !saved ? "Save the draft to load preparation questions." : !eligible ? "Use the prepared package review below." : "Load the questions detected by the host."
        }
        updateControls()
    }

    // Dropping connection state never cancels a durable host job.
    func invalidate() {
        epoch &+= 1; busy = false; polling = false; resolutionPolling = false; form = nil; formErrorCode = nil; review = nil; startKey = nil; resolutionReview = nil; resolutionStartKey = nil
        formView.configure(fields: [], values: [:]); reviewText.string = ""; updateControls()
    }
    func refreshAvailability() { updateControls() }
    func registryChanged() { if !busy && !hasActiveJob { refreshForm() } }

    /// Rediscover the host's current proposal without carrying an older
    /// candidate, recipe or dependency result into the new review.
    func reviewAgentProposal(_ expected: PreparationContext) -> Bool {
        guard context == expected, saved, eligible, available?() == true,
              request != nil, !busy, !hasActiveJob else { return false }
        job = nil; attachedNotification = nil; progress.stringValue = ""
        resolutionJob = nil; resolutionNotification = nil
        resolutionStatus.stringValue = ""; resolutionProgress.stringValue = ""
        loadForm(candidateID: nil, recipe: nil, resolutionDigest: nil)
        return true
    }

    private func updateControls() {
        let enabled = available?() == true && saved && eligible && context != nil && !busy && !hasActiveJob
        let currentForm = form.map { $0.importID == context?.importID && $0.revision == context?.revision && $0.sourceRevision == context?.sourceRevision } == true
        candidatePicker.isEnabled = enabled && currentForm; recipePicker.isEnabled = enabled && currentForm
        let questionsAvailable = currentForm && form?.unavailableMessage == nil
        formView.isHidden = !questionsAvailable
        formView.setEditable(enabled && questionsAvailable)
        refreshButton?.isEnabled = enabled
        reviewButton?.isEnabled = enabled && questionsAvailable && form?.candidateID != nil && form?.recipe != nil && form?.resolutionRequired != true && form?.blockers.isEmpty == true && formView.answers() != nil
        startButton?.isEnabled = enabled && review?.context == context && review?.canStart() == true
        candidateRow?.isHidden = form == nil || form?.candidates.isEmpty == true || (form?.candidates.count ?? 0) < 2 && form?.candidateID != nil
        recipeRow?.isHidden = form == nil || form?.recipes.isEmpty == true || (form?.recipes.count ?? 0) < 2 && form?.recipe != nil
        refreshButton?.isHidden = hasActiveJob
        refreshButton?.title = form == nil ? "Continue Setup" : "Reload Questions"
        reviewButton?.isHidden = !questionsAvailable || review?.canStart() == true || form?.resolutionRequired == true || hasActiveJob
        startButton?.isHidden = review?.canStart() != true || hasActiveJob
        resolutionStartButton?.isHidden = resolutionReview == nil || hasActiveJob
        reviewText.enclosingScrollView?.isHidden = reviewText.string.isEmpty
        progress.isHidden = progress.stringValue.isEmpty
        resolutionProgress.isHidden = resolutionProgress.stringValue.isEmpty
        if enabled, let review, review.executionAvailable, !review.canStart() {
            status.stringValue = "The preparation review expired. Load the current questions and review again."
        }
        cancelButton?.isEnabled = available?() == true && !busy && job?.active == true && job?.state != "cancel_requested"
        cancelButton?.isHidden = job == nil
        let requiresResolution = currentForm && form?.resolutionRequired == true
        resolutionStatus.isHidden = (!requiresResolution || !questionsAvailable) && resolutionReview == nil && resolutionJob == nil
        resolutionReviewButton.isHidden = !requiresResolution || !questionsAvailable || resolutionReview?.canStart() == true || hasActiveJob
        resolutionStartButton.isHidden = !requiresResolution || resolutionReview?.canStart() != true || hasActiveJob
        resolutionReviewButton.isEnabled = enabled && questionsAvailable && requiresResolution && form?.resolutionPolicy != nil
        resolutionStartButton.isEnabled = enabled && requiresResolution && form?.resolutionAvailable == true && resolutionReview?.context == context && resolutionReview?.canStart() == true
        resolutionCancelButton.isHidden = resolutionJob == nil || resolutionJob?.active != true || resolutionJob?.needsRecovery == true
        resolutionCancelButton.isEnabled = available?() == true && !busy && resolutionJob?.active == true && resolutionJob?.state != "cancel_requested" && resolutionJob?.needsRecovery != true
        resolutionRecoverButton.isHidden = resolutionJob?.needsRecovery != true
        resolutionRecoverButton.isEnabled = available?() == true && !busy && resolutionJob?.needsRecovery == true
        if enabled, let resolutionReview, resolutionReview.executionAvailable, !resolutionReview.canStart() {
            resolutionStatus.stringValue = "The resolution review expired. Review the current dependency scope again."
        }
    }

    @objc func refreshForm() {
        let successful = resolutionJob.flatMap { $0.state == "succeeded" && !$0.active ? $0 : nil }
        loadForm(candidateID: form?.candidateID ?? successful?.candidateID, recipe: form?.recipe,
                 resolutionDigest: form?.resolutionDigest ?? successful?.resultDigest)
    }

    private func loadForm(candidateID: String?, recipe: PreparationRecipeReference?, resolutionDigest: String? = nil) {
        guard !busy, !hasActiveJob, saved, eligible, available?() == true, let context, let request else { return }
        epoch &+= 1; let expected = epoch; busy = true; polling = false; resolutionPolling = false
        form = nil; formErrorCode = nil; review = nil; startKey = nil; formView.configure(fields: [], values: [:]); reviewText.string = ""
        resolutionReview = nil; resolutionStartKey = nil
        var body: [String: Any] = ["import_id": context.importID, "revision": context.revision]
        if let candidateID { body["candidate_id"] = candidateID }; if let recipe { body["recipe"] = recipe.object }
        if let resolutionDigest { body["resolution_digest"] = resolutionDigest }
        status.stringValue = "Loading preparation questions…"; updateControls(); onStateChange?()
        request("v1/preparations/form", "POST", body) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.saved else { return }; self.busy = false
            guard let object, let form = OnboardingFormV1(object), form.importID == context.importID,
                  form.revision == context.revision, form.sourceRevision == context.sourceRevision,
                  candidateID == nil || candidateID == form.candidateID, recipe == nil || recipe == form.recipe,
                  resolutionDigest == nil || resolutionDigest == form.resolutionDigest else {
                let safeError = preparationCode(code) ?? "unsupported_preparation_form"
                self.formErrorCode = safeError
                self.status.stringValue = "Questions unavailable (\(safeError))."; self.updateControls(); self.onStateChange?(); return
            }
            self.form = form; self.candidatePicker.removeAllItems(); self.recipePicker.removeAllItems()
            self.candidatePicker.addItem(withTitle: "Choose…"); self.recipePicker.addItem(withTitle: "Choose…")
            for choice in form.candidates {
                self.candidatePicker.addItem(withTitle: choice.label); self.candidatePicker.lastItem?.representedObject = choice.id
                if choice.id == form.candidateID { self.candidatePicker.select(self.candidatePicker.lastItem) }
            }
            for choice in form.recipes {
                self.recipePicker.addItem(withTitle: choice.choice.label); self.recipePicker.lastItem?.representedObject = choice.choice.id
                if choice.reference == form.recipe { self.recipePicker.select(self.recipePicker.lastItem) }
            }
            self.formView.configure(fields: form.fields, values: form.values)
            self.resolutionStatus.stringValue = form.resolutionRequired ? "The host must resolve dependencies" + (form.resolutionOperations.contains("discover_interface") ? " and discover this App's tools" : "") + " before package preparation. Review the scope below." : form.resolutionDigest != nil ? "Resolved dependencies and tools are bound to this source and candidate." : ""
            if form.resolutionRequired, let policy = form.resolutionPolicy { self.resolutionStatus.stringValue += "\n" + policy.display }
            self.status.stringValue = form.unavailableMessage ?? (form.blockers.isEmpty ?
                "Detected: " + (form.candidates.first(where: { $0.id == form.candidateID })?.label ?? "app") + ". Answer the questions, then review your app's tools and access." :
                "To continue: " + form.blockers.map { $0.replacingOccurrences(of: "_", with: " ") }.joined(separator: "; ") + ".")
            self.updateControls(); self.onStateChange?()
        }
    }

    @objc private func selectionChanged(_ sender: NSPopUpButton) {
        guard let form else { return }
        let candidate = candidatePicker.selectedItem?.representedObject as? String
        let recipeID = recipePicker.selectedItem?.representedObject as? String
        let recipe = sender === candidatePicker ? nil : form.recipes.first { $0.choice.id == recipeID }?.reference
        let digest = candidate == form.candidateID ? form.resolutionDigest : nil
        loadForm(candidateID: candidate, recipe: recipe, resolutionDigest: digest)
    }

    // Explicit native acceptance entry points apply only values represented by
    // the generated form. They cannot supply descriptors, policies or locks.
    func acceptanceSelect(candidateID: String?, recipeID: String?) -> Bool {
        guard let form, candidateID == nil || form.candidates.contains(where: { $0.id == candidateID }) else { return false }
        let recipe = recipeID.flatMap { id in form.recipes.first { $0.reference.id == id }?.reference }
        guard recipeID == nil || recipe != nil else { return false }
        loadForm(candidateID: candidateID ?? form.candidateID, recipe: recipe ?? form.recipe, resolutionDigest: form.resolutionDigest)
        return true
    }

    func acceptanceSelectExactRecipe(_ reference: PreparationRecipeReference) -> Bool {
        guard !busy, !hasActiveJob, saved, eligible, available?() == true, request != nil,
              let form, let choice = form.recipes.first(where: { $0.reference == reference }),
              let item = recipePicker.itemArray.first(where: { $0.representedObject as? String == choice.choice.id }) else { return false }
        recipePicker.select(item); selectionChanged(recipePicker)
        return true
    }

    var acceptanceReviewEnabled: Bool { reviewButton?.isEnabled == true }

    func acceptanceAnswers(_ overrides: [String: Any]) -> Bool {
        guard !busy, !hasActiveJob, let form, !form.resolutionRequired, overrides.count <= 256,
              preparationJSON(overrides, maximum: 262144) != nil else { return false }
        var values = form.values
        for (label, value) in overrides {
            let fields = form.fields.filter { $0.label == label && $0.kind != "schema-preview" }
            guard fields.count == 1, let field = fields.first, field.accepts(value) else { return false }
            values[field.id] = value
        }
        formView.configure(fields: form.fields, values: values); updateControls()
        return formView.answers() != nil
    }

    @objc func reviewForm() {
        guard !busy, !hasActiveJob, saved, eligible, available?() == true, let context, let form,
              form.importID == context.importID, form.revision == context.revision, form.sourceRevision == context.sourceRevision,
              !form.resolutionRequired, form.unavailableMessage == nil, form.blockers.isEmpty,
              let candidate = form.candidateID, let recipe = form.recipe, let answers = formView.answers(), let request else { return }
        epoch &+= 1; let expected = epoch; busy = true; polling = false; review = nil; startKey = nil; reviewText.string = ""
        status.stringValue = "Reviewing the exact preparation inputs…"; updateControls(); onStateChange?()
        var body: [String: Any] = ["import_id": context.importID, "revision": context.revision, "candidate_id": candidate,
            "recipe": recipe.object, "form_digest": form.digest, "answers": answers]
        if let digest = form.resolutionDigest { body["resolution_digest"] = digest }
        request("v1/preparations/plan-form", "POST", body) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.saved, self.form?.digest == form.digest else { return }; self.busy = false
            guard let object, let review = PreparationReview(object, form: form) else {
                self.status.stringValue = "Preparation review failed (\(preparationCode(code) ?? "invalid_preparation_review"))."; self.updateControls(); self.onStateChange?(); return
            }
            // Adopt the host's revision before the parent displays its returned draft.
            // Its normal binding pass must see this same context, not invalidate this response.
            self.context = review.context
            guard self.onUpdatedDraft?(review.draft) == true else {
                self.invalidate(); self.status.stringValue = "The returned draft could not be accepted."; self.onStateChange?(); return
            }
            guard self.epoch == expected, self.context == review.context, self.saved else { return }
            self.review = review; self.reviewText.string = review.display
            self.status.stringValue = review.executionAvailable ? "Review the tools and declared effects, then prepare this exact plan." : "Automatic preparation is unavailable on this host. The reviewed metadata is shown below."
            self.updateControls(); self.onStateChange?()
        }
    }

    @objc func startPreparation() {
        guard !busy, !hasActiveJob, saved, eligible, available?() == true, let context, let review,
              review.context == context, review.canStart(), let id = review.planID, let digest = review.planDigest, let request else { return }
        let key = startKey ?? UUID().uuidString.lowercased(); startKey = key
        epoch &+= 1; let expected = epoch; busy = true; polling = false; status.stringValue = "Submitting the reviewed preparation…"; updateControls(); onStateChange?()
        request("v1/preparations/start", "POST", ["plan_id": id, "plan_digest": digest, "idempotency_key": key]) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.review?.planID == id, self.review?.planDigest == digest else { return }; self.busy = false
            guard let object, let job = PreparationJob(object), job.importID == context.importID, job.revision == context.revision,
                  job.sourceRevision == context.sourceRevision else {
                self.status.stringValue = "Preparation could not start (\(preparationCode(code) ?? "invalid_preparation_job"))."; self.updateControls(); self.onStateChange?(); self.poll(); return
            }
            self.accept(job); self.updateControls(); self.onStateChange?()
        }
    }

    @objc func reviewResolution() {
        guard !busy, !hasActiveJob, saved, eligible, available?() == true, let context, let form,
              form.importID == context.importID, form.revision == context.revision, form.sourceRevision == context.sourceRevision,
              form.resolutionRequired, let candidate = form.candidateID, let policy = form.resolutionPolicy, let request else { return }
        epoch &+= 1; let expected = epoch; busy = true; polling = false; resolutionPolling = false
        resolutionReview = nil; resolutionStartKey = nil; review = nil; startKey = nil; reviewText.string = ""
        resolutionStatus.stringValue = "Reviewing the exact dependency and tool discovery scope…"; updateControls(); onStateChange?()
        request("v1/resolutions/plan", "POST", ["import_id": context.importID, "revision": context.revision, "candidate_id": candidate, "policy": policy.object]) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.form?.digest == form.digest else { return }; self.busy = false
            guard let object, let review = PreparationResolutionReview(object, context: context, candidateID: candidate, policy: policy) else {
                self.resolutionStatus.stringValue = "Resolution review failed (\(preparationCode(code) ?? "invalid_resolution_plan"))."; self.updateControls(); self.onStateChange?(); return
            }
            self.resolutionReview = review
            self.resolutionStatus.stringValue = policy.display + "\n" + (review.executionAvailable ? "Start authorizes this exact source, candidate, download scope and budgets once." : "Automatic resolution is unavailable on this host.")
            self.updateControls(); self.onStateChange?()
        }
    }

    @objc func startResolution() {
        guard !busy, !hasActiveJob, saved, eligible, available?() == true, let context, let form, form.resolutionRequired,
              form.resolutionAvailable, let review = resolutionReview, review.context == context,
              review.candidateID == form.candidateID, review.canStart(), let request else { return }
        let key = resolutionStartKey ?? UUID().uuidString.lowercased(); resolutionStartKey = key
        epoch &+= 1; let expected = epoch; busy = true; polling = false; resolutionPolling = false
        resolutionStatus.stringValue = "Submitting the reviewed resolution…"; updateControls(); onStateChange?()
        request("v1/resolutions/start", "POST", ["plan_id": review.planID, "plan_digest": review.planDigest, "idempotency_key": key]) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.resolutionReview?.planID == review.planID,
                  self.resolutionReview?.planDigest == review.planDigest else { return }; self.busy = false
            guard let object, let job = PreparationResolutionJob(object), job.importID == context.importID,
                  job.revision == context.revision, job.sourceRevision == context.sourceRevision, job.candidateID == review.candidateID else {
                self.resolutionStatus.stringValue = "Resolution could not start (\(preparationCode(code) ?? "invalid_resolution_job"))."; self.updateControls(); self.onStateChange?(); self.pollResolution(); return
            }
            self.acceptResolution(job); self.updateControls(); self.onStateChange?()
        }
    }

    private func pollResolution() {
        guard form?.resolutionPolicy != nil || resolutionJob != nil || form?.resolutionRequired == true else { return }
        guard !resolutionPolling, !busy, saved, available?() == true, let context, let request else { return }
        resolutionPolling = true; let expected = epoch
        request("v1/resolutions", "GET", nil) { [weak self] object, _ in
            guard let self, self.epoch == expected, self.context == context else { return }
            guard let rows = object?["resolutions"] as? [[String: Any]], rows.count <= 1024 else { self.resolutionPolling = false; return }
            let jobs = rows.compactMap(PreparationResolutionJob.init)
            guard jobs.count == rows.count, Set(jobs.map(\.id)).count == jobs.count else { self.resolutionPolling = false; return }
            let related = jobs.filter { $0.importID == context.importID && $0.revision <= context.revision && $0.sourceRevision == context.sourceRevision }
            // An unresolved owned job has priority over a later terminal result.
            let latest = related.filter(\.active).max(by: { $0.updatedAt < $1.updatedAt }) ?? related.max(by: { $0.updatedAt < $1.updatedAt })
            guard let latest else { self.resolutionPolling = false; return }
            if self.resolutionJob?.id == latest.id, (self.resolutionJob?.updatedAt ?? -1) >= latest.updatedAt { self.resolutionPolling = false; return }
            request("v1/resolutions/" + latest.id, "GET", nil) { [weak self] object, _ in
                guard let self, self.epoch == expected, self.context == context else { return }; self.resolutionPolling = false
                guard let object, let job = PreparationResolutionJob(object), job.id == latest.id, job.importID == context.importID,
                      job.revision <= context.revision, job.sourceRevision == context.sourceRevision, job.updatedAt >= latest.updatedAt,
                      self.resolutionJob?.id != job.id || job.updatedAt >= (self.resolutionJob?.updatedAt ?? 0) else { return }
                self.acceptResolution(job); self.updateControls(); self.onStateChange?()
            }
        }
    }

    @objc func cancelResolution() { changeResolution(recover: false) }
    @objc func recoverResolution() { changeResolution(recover: true) }

    private func changeResolution(recover: Bool) {
        guard !busy, available?() == true, let context, let job = resolutionJob, let request,
              recover ? job.needsRecovery : job.active && !job.needsRecovery && job.state != "cancel_requested" else { return }
        epoch &+= 1; let expected = epoch; busy = true; polling = false; resolutionPolling = false; updateControls(); onStateChange?()
        request("v1/resolutions/" + job.id + (recover ? "/recover" : "/cancel"), "POST", [:]) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.resolutionJob?.id == job.id else { return }; self.busy = false
            guard let object, let updated = PreparationResolutionJob(object), updated.id == job.id, updated.importID == job.importID,
                  updated.revision == job.revision, updated.sourceRevision == context.sourceRevision, updated.candidateID == job.candidateID,
                  updated.updatedAt >= job.updatedAt else {
                self.resolutionStatus.stringValue = "Resolution " + (recover ? "recovery" : "cancellation") + " failed (\(preparationCode(code) ?? "invalid_resolution_job"))."; self.updateControls(); self.onStateChange?(); return
            }
            self.acceptResolution(updated); self.updateControls(); self.onStateChange?()
        }
    }

    private func acceptResolution(_ next: PreparationResolutionJob) {
        resolutionJob = next; resolutionProgress.stringValue = "Draft revision \(next.revision) · " + next.summary
        resolutionStatus.stringValue = next.active ? "Resolution is owned by the host. Closing this window keeps it running." : next.state == "succeeded" ? "Dependencies and tools resolved. Loading the generated preparation questions…" : "Resolution " + next.state + ". The draft is preserved."
        if next.state == "succeeded", !next.active, resolutionNotification != next.id, let digest = next.resultDigest {
            resolutionNotification = next.id
            loadForm(candidateID: next.candidateID, recipe: form?.recipe, resolutionDigest: digest)
        }
    }

    func poll() {
        pollResolution()
        guard !polling, !busy, saved, available?() == true, let context, let request else { return }
        polling = true; let expected = epoch
        request("v1/preparations", "GET", nil) { [weak self] object, _ in
            guard let self, self.epoch == expected, self.context == context else { return }
            guard let rows = object?["preparations"] as? [[String: Any]], rows.count <= 1024 else { self.polling = false; return }
            let jobs = rows.compactMap(PreparationJob.init)
            guard jobs.count == rows.count, Set(jobs.map(\.id)).count == jobs.count else { self.polling = false; return }
            guard let latest = jobs.filter({ $0.importID == context.importID && $0.revision <= context.revision && $0.sourceRevision == context.sourceRevision }).max(by: { $0.updatedAt < $1.updatedAt }) else { self.polling = false; return }
            if self.job?.id == latest.id, (self.job?.updatedAt ?? -1) >= latest.updatedAt { self.polling = false; return }
            request("v1/preparations/" + latest.id, "GET", nil) { [weak self] object, _ in
                guard let self, self.epoch == expected, self.context == context else { return }; self.polling = false
                guard let object, let job = PreparationJob(object), job.id == latest.id, job.importID == context.importID,
                      job.revision <= context.revision, job.sourceRevision == context.sourceRevision, job.updatedAt >= latest.updatedAt,
                      self.job?.id != job.id || job.updatedAt >= (self.job?.updatedAt ?? 0) else { return }
                self.accept(job); self.updateControls(); self.onStateChange?()
            }
        }
    }

    @objc func cancelPreparation() {
        guard !busy, available?() == true, let context, let job, job.active, job.state != "cancel_requested", let request else { return }
        epoch &+= 1; let expected = epoch; busy = true; polling = false; updateControls(); onStateChange?()
        request("v1/preparations/" + job.id + "/cancel", "POST", [:]) { [weak self] object, code in
            guard let self, self.epoch == expected, self.context == context, self.job?.id == job.id else { return }; self.busy = false
            guard let object, let updated = PreparationJob(object), updated.id == job.id, updated.importID == job.importID,
                  updated.revision == job.revision, updated.sourceRevision == context.sourceRevision, updated.updatedAt >= job.updatedAt else {
                self.status.stringValue = "Cancellation failed (\(preparationCode(code) ?? "invalid_preparation_job"))."; self.updateControls(); self.onStateChange?(); return
            }
            self.accept(updated); self.updateControls(); self.onStateChange?()
        }
    }

    private func accept(_ next: PreparationJob) {
        job = next; progress.stringValue = "Draft revision \(next.revision) · " + next.summary
        status.stringValue = next.active ? "Preparation is owned by the host. Closing this window keeps it running." : next.state == "succeeded" ? "Prepared package attached. Continue with package review and installation below." : "Preparation " + next.state + ". The draft is preserved."
        if next.state == "succeeded", attachedNotification != next.id { attachedNotification = next.id; onAttached?() }
    }
}

struct PreparationRecipeIdentity: Equatable {
    let id: String
    let version: String
    let digest: String
    init?(_ object: [String: Any]) {
        guard let id = preparationText(object["recipe_id"], maximum: 128),
              let version = preparationText(object["version"], maximum: 64),
              let digest = preparationDigest(object["digest"]),
              !id.unicodeScalars.contains(where: { $0.value < 32 }), !version.unicodeScalars.contains(where: { $0.value < 32 }) else { return nil }
        self.id = id; self.version = version; self.digest = digest
    }
    var payload: [String: Any] { ["recipe_id": id, "version": version] }
    var label: String { id + " · " + version }
}

final class PreparationRecipesView: NSStackView {
    var request: PreparationRequest?
    var available: (() -> Bool)?
    var onRegistryChange: (() -> Void)?
    private let picker = NSPopUpButton()
    private let status = NSTextField(wrappingLabelWithString: "Recipe metadata is unavailable.")
    private let summary = NSTextField(wrappingLabelWithString: "Choose a recipe to see what it does.")
    private let detail = NSTextView()
    private var rows: [PreparationRecipeIdentity] = []
    private var selected: PreparationRecipeIdentity?
    private var buttons: [NSButton] = []
    private var epoch: UInt64 = 0
    private var busy = false
    private var inspectedState: String?

    override init(frame: NSRect) {
        super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 12
        let explanation = workspaceLabel("Recipes are reusable setup templates. Most apps use the included templates automatically. Create a starter when you need to extend app support.", size: 13, secondary: true)
        addArrangedSubview(explanation); workspaceFill(explanation, in: self)
        picker.target = self; picker.action = #selector(inspectRecipe)
        picker.setAccessibilityLabel("Recipe version and implementation identity")
        picker.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        picker.widthAnchor.constraint(greaterThanOrEqualToConstant: 160).isActive = true
        picker.widthAnchor.constraint(lessThanOrEqualToConstant: 400).isActive = true
        let add = NSButton(title: "Import Recipe…", target: self, action: #selector(addRecipe)); add.bezelStyle = .rounded
        let create = NSButton(title: "Create Recipe…", target: self, action: #selector(createRecipe)); create.bezelStyle = .rounded
        let refresh = NSButton(title: "Refresh", target: self, action: #selector(refreshRecipes)); refresh.bezelStyle = .rounded
        buttons = [add, create, refresh]
        let top = workspaceStack([picker, add, create, refresh], horizontal: true)
        addArrangedSubview(top); workspaceFill(top, in: self); addArrangedSubview(status); workspaceFill(status, in: self)
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        detail.isEditable = false; detail.isSelectable = true; detail.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        detail.isVerticallyResizable = true; detail.autoresizingMask = [.width]; detail.textContainer?.widthTracksTextView = true
        detail.textContainerInset = NSSize(width: 8, height: 8); scroll.documentView = detail
        scroll.heightAnchor.constraint(equalToConstant: 310).isActive = true
        summary.font = .systemFont(ofSize: 13); summary.textColor = .secondaryLabelColor
        addArrangedSubview(summary); workspaceFill(summary, in: self)
        addArrangedSubview(WorkspaceDisclosure("Show technical details", views: [scroll]))
        let actions = ["enable", "disable", "deprecate", "revoke", "remove"].map { action -> NSButton in
            let button = NSButton(title: action.capitalized, target: self, action: #selector(changeRecipe(_:)))
            button.bezelStyle = .rounded; button.identifier = NSUserInterfaceItemIdentifier(action); buttons.append(button); return button
        }
        addArrangedSubview(WorkspaceDisclosure("Advanced recipe lifecycle", views: [workspaceStack(actions, horizontal: true)]))
        updateControls()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func invalidate() { epoch &+= 1; busy = false; updateControls() }
    func refreshAvailability() { updateControls() }

    private func updateControls() {
        let enabled = available?() == true && !busy
        picker.isEnabled = enabled
        for button in buttons { button.isEnabled = enabled && (button.identifier == nil || selected != nil) }
    }

    @objc func refreshRecipes() {
        guard !busy, available?() == true, let request else { updateControls(); return }
        epoch &+= 1; let expected = epoch; busy = true; updateControls()
        request("v1/recipes", "GET", nil) { [weak self] object, code in
            guard let self, self.epoch == expected else { return }; self.busy = false
            guard let items = object?["recipes"] as? [[String: Any]], items.count <= 1024 else {
                self.status.stringValue = "Recipes could not be loaded (\(preparationCode(code) ?? "invalid_recipe_response"))."; self.updateControls(); return
            }
            let identities = items.compactMap(PreparationRecipeIdentity.init)
            guard identities.count == items.count, Set(identities.map { $0.id + "\u{0}" + $0.version }).count == identities.count else {
                self.status.stringValue = "The host returned an unsupported recipe list."; self.updateControls(); return
            }
            let prior = self.selected; self.rows = identities; self.picker.removeAllItems()
            for row in identities { self.picker.addItem(withTitle: row.label); self.picker.lastItem?.representedObject = row.digest }
            if let prior, let index = identities.firstIndex(of: prior) { self.picker.selectItem(at: index) }
            self.selected = nil; self.inspectedState = nil; self.status.stringValue = identities.isEmpty ? "No recipes are registered. Add a local recipe folder." : "Choose a version to inspect its exact identity and compatibility."
            self.updateControls(); if !identities.isEmpty { self.inspectRecipe() }
        }
    }

    @objc private func inspectRecipe() {
        guard !busy, available?() == true, rows.indices.contains(picker.indexOfSelectedItem), let request else { return }
        let identity = rows[picker.indexOfSelectedItem]; selected = identity; inspectedState = nil
        epoch &+= 1; let expected = epoch; busy = true; updateControls()
        request("v1/recipes/inspect", "POST", identity.payload) { [weak self] object, code in
            guard let self, self.epoch == expected, self.selected == identity else { return }; self.busy = false
            guard let object, PreparationRecipeIdentity(object) == identity, let state = preparationCode(object["state"]),
                  let manifest = object["manifest"] as? [String: Any], let metadata = preparationJSON([
                    "identity": ["recipe_id": identity.id, "version": identity.version, "digest": identity.digest],
                    "compatibility": manifest["compatibility"] ?? [:], "effects": manifest["effects"] ?? [:],
                    "documentation": manifest["documentation"] ?? [], "evidence": object["evidence"] ?? NSNull(),
                    "blockers": object["blockers"] ?? []]) else {
                self.selected = nil; self.detail.string = ""; self.status.stringValue = "Recipe inspection failed (\(preparationCode(code) ?? "recipe_identity_changed"))."; self.updateControls(); return
            }
            self.detail.string = metadata
            self.inspectedState = state
            let runnable = object["runnable"] as? Bool == true
            let blockers = object["blockers"] as? [Any] ?? []
            let stateText: String
            switch state {
            case "enabled": stateText = runnable ? "Enabled and ready to use." : "Enabled, but execution is unavailable."
            case "disabled": stateText = "Disabled. Enable it when you want to use it."
            case "deprecated": stateText = "Deprecated. Existing references remain pinned."
            case "revoked": stateText = "Revoked. It cannot be enabled again."
            case "removed": stateText = "Removed from the registry."
            default: stateText = runnable ? "Ready to use." : "Metadata is valid; execution is unavailable."
            }
            self.summary.stringValue = identity.id + " v" + identity.version + "\n" + stateText +
                (blockers.isEmpty ? "" : "\nNeeds attention before use.")
            self.status.stringValue = "Recipe loaded."
            self.updateControls()
        }
    }

    @objc private func addRecipe() {
        guard !busy, available?() == true else { return }
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.allowsMultipleSelection = false
        panel.message = "Select a local recipe folder. Adding validates and snapshots its metadata and bytes."
        let panelEpoch = epoch
        panel.begin { [weak self] result in
            guard result == .OK, let url = panel.url else { return }
            _ = self?.selectedRecipeFolder(url, panelEpoch: panelEpoch)
        }
    }

    @objc private func createRecipe() {
        guard !busy, available?() == true else { return }
        let alert = NSAlert(); alert.messageText = "Create a recipe"
        alert.informativeText = "Choose a name and starter type. mcpme will generate the manifest, license records, and integrity lock for you."
        let name = NSTextField(string: ""); name.placeholderString = "Recipe name (for example, My Tool)"; name.setAccessibilityLabel("Recipe name")
        let component = NSPopUpButton(); component.addItems(withTitles: ["Python function", "Existing MCP app"])
        component.setAccessibilityLabel("Recipe starter type")
        let form = NSStackView(views: [name, component]); form.orientation = .vertical; form.spacing = 8
        form.translatesAutoresizingMaskIntoConstraints = false
        form.widthAnchor.constraint(equalToConstant: 360).isActive = true
        form.heightAnchor.constraint(equalToConstant: 58).isActive = true
        alert.accessoryView = form; alert.addButton(withTitle: "Choose Folder…"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let displayName = name.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let slug = displayName.lowercased().replacingOccurrences(of: "[^a-z0-9]+", with: "-", options: .regularExpression).trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        let recipeID = "local." + slug
        guard !slug.isEmpty, recipeID.count <= 128 else {
            status.stringValue = "Enter a short recipe name."; return
        }
        let panel = NSSavePanel(); panel.canCreateDirectories = true; panel.nameFieldStringValue = recipeID
        panel.message = "Choose where to create the new recipe folder. An existing folder will not be replaced."
        let panelEpoch = epoch
        panel.begin { [weak self] result in
            guard let self, result == .OK, let url = panel.url, url.isFileURL, !url.path.isEmpty,
                  self.epoch == panelEpoch, !self.busy, self.available?() == true, let request = self.request else { return }
            let components = ["python-function-v1", "existing-mcp-v1"]
            let componentID = components[min(max(component.indexOfSelectedItem, 0), components.count - 1)]
            self.epoch &+= 1; let expected = self.epoch; self.busy = true; self.updateControls()
            request("v1/recipes/scaffold", "POST", ["path": url.path, "recipe_id": recipeID, "component": componentID]) { [weak self] object, code in
                guard let self, self.epoch == expected else { return }; self.busy = false
                guard let object, let identity = PreparationRecipeIdentity(object), identity.id == recipeID,
                      identity.version == "1.0.0", object["path"] as? String == url.path else {
                    self.status.stringValue = "Recipe could not be created (" + (preparationCode(code) ?? "invalid_recipe_response") + ")."; self.updateControls(); return
                }
                self.status.stringValue = "Created " + displayName + " at " + url.path + ". Review its README, then use Import Recipe… to register it."
                NSWorkspace.shared.activateFileViewerSelecting([url])
                self.updateControls()
            }
        }
    }

    // Both the real file dialog and the closed developer acceptance driver
    // enter this same selection-completion handler. Selection itself is static.
    @discardableResult private func selectedRecipeFolder(_ url: URL, panelEpoch: UInt64) -> Bool {
        guard url.isFileURL, self.epoch == panelEpoch, !busy, available?() == true, let request else { return false }
        epoch &+= 1; let expected = epoch; busy = true; updateControls()
        request("v1/recipes", "POST", ["path": url.path]) { [weak self] object, code in
            guard let self, self.epoch == expected else { return }; self.busy = false
            if let object, let identity = PreparationRecipeIdentity(object) {
                self.selected = identity; self.onRegistryChange?(); self.refreshRecipes()
            } else { self.status.stringValue = "Recipe could not be added (\(preparationCode(code) ?? "invalid_recipe_response"))."; self.updateControls() }
        }
        return true
    }

    @objc private func changeRecipe(_ sender: NSButton) {
        guard !busy, available?() == true, let identity = selected,
              let action = sender.identifier?.rawValue, ["enable", "disable", "deprecate", "revoke", "remove"].contains(action), request != nil else { return }
        if ["revoke", "remove"].contains(action) {
            let alert = NSAlert(); alert.messageText = action.capitalized + " this recipe version?"
            alert.informativeText = identity.label + "\nImplementation: " + identity.digest + "\nGenerated apps retain their package identities and data."
            alert.addButton(withTitle: action.capitalized); alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        performRecipeChange(action, identity: identity)
    }

    private func performRecipeChange(_ action: String, identity: PreparationRecipeIdentity) {
        guard !busy, available?() == true, selected == identity,
              ["enable", "disable", "deprecate", "revoke", "remove"].contains(action), let request else { return }
        epoch &+= 1; let expected = epoch; busy = true; updateControls()
        request("v1/recipes/" + action, "POST", identity.payload) { [weak self] object, code in
            guard let self, self.epoch == expected, self.selected == identity else { return }; self.busy = false
            guard let object, PreparationRecipeIdentity(object) == identity else {
                self.status.stringValue = "Recipe action failed (\(preparationCode(code) ?? "recipe_identity_changed"))."; self.updateControls(); return
            }
            self.onRegistryChange?(); self.refreshRecipes()
        }
    }

    // These entry points take no source directives. Only main.swift's private,
    // nonce-bound acceptance CLI supplies their simulated dialog choices.
    var acceptanceBusy: Bool { busy }
    var acceptanceIdentity: PreparationRecipeIdentity? { selected }
    var acceptanceState: String? { inspectedState }

    func acceptanceChooseRecipeFolder(_ folder: URL) -> Bool {
        selectedRecipeFolder(folder, panelEpoch: epoch)
    }

    func acceptanceSelectRecipe(_ identity: PreparationRecipeIdentity) -> Bool {
        guard !busy, available?() == true, request != nil, let index = rows.firstIndex(of: identity) else { return false }
        picker.selectItem(at: index); inspectRecipe()
        return true
    }

    func acceptanceConfirmedChange(_ action: String, identity: PreparationRecipeIdentity) -> Bool {
        guard !busy, available?() == true, request != nil, selected == identity,
              ["enable", "disable", "deprecate", "revoke", "remove"].contains(action) else { return false }
        performRecipeChange(action, identity: identity)
        return true
    }
}

private struct PreparationTestRequest {
    let path: String
    let method: String
    let body: [String: Any]?
    let complete: ([String: Any]?, String?) -> Void
}

/// Synthetic fixtures shared by native contract self-tests.
enum PreparationTestData {
    static let importID = "11111111-2222-4333-8444-555555555555"
    static let jobID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    static let planID = "99999999-2222-4333-8444-555555555555"
    static let candidate = String(repeating: "a", count: 64)
    static let recipeDigest = String(repeating: "b", count: 64)
    static let formDigest = String(repeating: "c", count: 64)
    static let source = String(repeating: "d", count: 64)
    static let planDigest = String(repeating: "e", count: 64)
    static var recipe: [String: Any] { ["id": "example.synthetic", "version": "1.0.0", "digest": recipeDigest] }
    static var context: PreparationContext { PreparationContext(importID: importID, revision: 1, sourceRevision: source)! }
    static func form(revision: Int = 1, available: Bool = false) -> [String: Any] {
        let types: [(String, Any)] = [("string", "café"), ("integer", 3), ("number", 0.25), ("boolean", true),
            ("choice", "opaque-choice"), ("file", "/tmp/synthetic-input"), ("object", ["enabled": true]), ("schema-preview", NSNull())]
        let fields = types.enumerated().map { index, pair -> [String: Any] in
            ["id": String(repeating: String(index + 1), count: 64), "label": "Field \(index + 1)", "description": "A typed question from recipe metadata.",
             "kind": pair.0, "required": true, "constraints": pair.0 == "string" ? ["min_length": 1, "max_length": 4] : [:],
             "choices": pair.0 == "choice" ? [["id": "opaque-choice", "label": "Display label"]] : [], "value": pair.1,
             "schema": pair.0 == "schema-preview" ? ["type": "object"] : NSNull()]
        }
        return ["schema_version": 1, "import_id": importID, "revision": revision, "source_revision": source, "form_digest": formDigest,
            "candidate_id": candidate, "recipe": recipe, "candidate_choices": [["id": candidate, "label": "Synthetic source candidate"]],
            "recipe_choices": [["id": recipeDigest, "label": "Synthetic recipe · 1.0.0", "recipe": recipe]], "fields": fields,
            "tools": [["name": "synthetic", "input_schema": ["type": "object"]]], "blockers": [], "execution_available": available]
    }
    static func review(revision: Int = 1, available: Bool = false) -> [String: Any] {
        ["schema_version": 1, "import_id": importID, "revision": revision, "source_revision": source, "form_digest": formDigest,
         "execution_available": available, "plan_id": available ? planID : NSNull(), "plan_digest": available ? planDigest : NSNull(),
         "expires_at": available ? Date().timeIntervalSince1970 + 600 : NSNull(),
         "tools": [["name": "synthetic", "description": "A synthetic tool.", "input_schema": ["type": "object"]]],
         "access": ["filesystem": ["/tmp/selected-input"], "network": ["example.test"], "secrets": [], "writes": false], "resources": ["memory_mb": 128],
         "downloads": [], "blockers": available ? [] : ["preparation_runner_unavailable"],
         "effects": ["writes": ["private_workspace"]], "execution_policy": ["schema_version": 1, "operations": ["build", "probe"], "network": ["downloads.example.test"], "wall_seconds": 60, "disk_bytes": 104857600, "memory_mb": 256],
         "draft": ["draft_id": importID, "revision": revision, "name": "Synthetic app", "source": ["revision": source]]]
    }
    static func job(state: String = "preparing", revision: Int = 1, updated: Double = 1) -> [String: Any] {
        ["job_id": jobID, "binding": ["import_id": importID, "revision": revision, "source_revision": source], "state": state,
         "updated_at": updated, "error_code": NSNull(), "progress": ["stage": state == "succeeded" ? "succeeded" : "dependency_install",
            "commands_finished": 2, "wheels_acquired": 3, "bytes_acquired": 4096, "files_checked": 4, "fixtures_checked": 1]]
    }
    static func summary(_ job: [String: Any]) -> [String: Any] {
        let binding = job["binding"] as! [String: Any]
        return ["job_id": job["job_id"]!, "import_id": binding["import_id"]!, "revision": binding["revision"]!, "source_revision": binding["source_revision"]!,
            "state": job["state"]!, "updated_at": job["updated_at"]!, "error_code": job["error_code"]!, "progress": job["progress"]!]
    }
    static var resolutionPolicy: [String: Any] { ["schema_version": 1, "operations": ["resolve_dependencies", "discover_interface"], "network": ["pypi.org", "files.pythonhosted.org"], "wall_seconds": 120, "disk_bytes": 1_073_741_824, "memory_mb": 512] }
    static func resolutionForm(revision: Int = 1, resolved: Bool = false) -> [String: Any] {
        var value = form(revision: revision, available: true)
        value["resolution_required"] = !resolved; value["resolution_operations"] = ["resolve_dependencies", "discover_interface"]
        value["resolution_available"] = true; value["resolution_policy"] = resolutionPolicy
        value["resolution_digest"] = resolved ? String(repeating: "f", count: 64) : NSNull(); return value
    }
    static func resolutionPlan(available: Bool = true, expired: Bool = false) -> [String: Any] {
        ["schema_version": 1, "plan_id": planID, "plan_digest": planDigest, "expires_at": Date().timeIntervalSince1970 + (expired ? -1 : 120),
         "execution_available": available, "request": ["import_id": importID, "revision": 1, "source_revision": source,
            "candidate_id": candidate, "candidate_kind": "python-mcp", "policy": resolutionPolicy]]
    }
    static func resolutionJob(state: String = "running", updated: Double = 1, settled: Bool = false) -> [String: Any] {
        ["job_id": jobID, "request": ["import_id": importID, "revision": 1, "source_revision": source, "candidate_id": candidate],
         "state": state, "stage": state == "succeeded" ? "succeeded" : "discovering", "updated_at": updated,
         "ownership_settled": settled, "reservation_retained": !settled, "result_digest": state == "succeeded" ? String(repeating: "f", count: 64) : NSNull(),
         "error_code": NSNull(), "progress": ["commands_finished": 3, "wheels_acquired": 2, "bytes_acquired": 4096, "files_checked": 5, "tools_discovered": 1]]
    }
}

extension PreparationWorkspaceView {
    static func formErrorObservationSelfTest() -> Bool {
        let context = PreparationTestData.context, view = PreparationWorkspaceView()
        var calls: [PreparationTestRequest] = []
        view.available = { true }; view.bind(context, saved: true, eligible: true)
        view.request = { calls.append(PreparationTestRequest(path: $0, method: $1, body: $2, complete: $3)) }
        func load() -> PreparationTestRequest? {
            let count = calls.count; view.refreshForm()
            guard calls.count == count + 1, view.busy, view.formErrorCode == nil,
                  calls.last?.path == "v1/preparations/form", calls.last?.method == "POST" else { return nil }
            return calls.last
        }
        guard let rejected = load() else { return false }
        rejected.complete(nil, "agent_pending_proposal_unavailable")
        guard !view.busy, view.form == nil, view.formErrorCode == "agent_pending_proposal_unavailable" else { return false }
        // A current malformed success response is still an unavailable form;
        // untrusted error text is reduced to the existing fixed fallback.
        guard let malformed = load() else { return false }
        malformed.complete(["schema_version": 1], "untrusted diagnostic text")
        guard !view.busy, view.form == nil, view.formErrorCode == "unsupported_preparation_form" else { return false }
        guard let recovered = load() else { return false }
        recovered.complete(PreparationTestData.resolutionForm(), nil)
        guard !view.busy, view.form != nil, view.formErrorCode == nil else { return false }
        guard let stale = load() else { return false }
        let next = PreparationContext(importID: context.importID, revision: 2, sourceRevision: context.sourceRevision)!
        view.bind(next, saved: true, eligible: true)
        stale.complete(nil, "agent_pending_proposal_unavailable")
        guard !view.busy, view.form == nil, view.formErrorCode == nil else { return false }
        guard let current = load() else { return false }
        stale.complete(nil, "agent_generated_form_invalid")
        guard view.busy, view.form == nil, view.formErrorCode == nil else { return false }
        current.complete(PreparationTestData.resolutionForm(revision: 2), nil)
        guard !view.busy, view.form?.revision == 2, view.formErrorCode == nil else { return false }
        guard let invalidated = load() else { return false }
        view.invalidate(); invalidated.complete(nil, "agent_generated_form_invalid")
        guard !view.busy, view.form == nil, view.formErrorCode == nil else { return false }
        guard let failed = load() else { return false }
        failed.complete(nil, "preparation_form_unavailable")
        guard view.formErrorCode == "preparation_form_unavailable" else { return false }
        view.bind(context, saved: false, eligible: true)
        return view.formErrorCode == nil && view.form == nil && !view.busy && view.job == nil && view.resolutionJob == nil
    }

    static func agentProposalReviewSelfTest() -> Bool {
        let context = PreparationTestData.context, view = PreparationWorkspaceView()
        var calls: [PreparationTestRequest] = []
        view.available = { true }; view.bind(context, saved: true, eligible: true)
        view.request = { calls.append(PreparationTestRequest(path: $0, method: $1, body: $2, complete: $3)) }
        // An older completed resolution must never supply a candidate or digest
        // to fresh discovery of the current staged proposal.
        view.form = OnboardingFormV1(PreparationTestData.resolutionForm(resolved: true))
        view.resolutionJob = PreparationResolutionJob(PreparationTestData.resolutionJob(state: "succeeded", settled: true))
        view.resolutionNotification = "old-result"; view.job = PreparationJob(PreparationTestData.job(state: "succeeded"))
        view.attachedNotification = "old-package"
        guard view.form != nil, view.resolutionJob != nil, view.job != nil, view.reviewAgentProposal(context), calls.count == 1,
              calls[0].path == "v1/preparations/form", calls[0].method == "POST",
              Set(calls[0].body!.keys) == ["import_id", "revision"], view.form == nil,
              view.job == nil, view.resolutionJob == nil, view.attachedNotification == nil, view.resolutionNotification == nil else { return false }
        guard !view.reviewAgentProposal(context), calls.count == 1 else { return false }
        let delayed = calls.removeFirst(); view.bind(context, saved: false, eligible: true)
        delayed.complete(PreparationTestData.resolutionForm(), nil)
        guard view.form == nil, !view.busy, !view.reviewAgentProposal(context), calls.isEmpty else { return false }
        view.bind(context, saved: true, eligible: false)
        guard !view.reviewAgentProposal(context), calls.isEmpty else { return false }
        view.bind(context, saved: true, eligible: true); view.available = { false }
        guard !view.reviewAgentProposal(context), calls.isEmpty else { return false }
        view.available = { true }; view.request = nil
        guard !view.reviewAgentProposal(context) else { return false }
        view.request = { calls.append(PreparationTestRequest(path: $0, method: $1, body: $2, complete: $3)) }
        for other in [PreparationContext(importID: UUID().uuidString.lowercased(), revision: 1, sourceRevision: context.sourceRevision),
                      PreparationContext(importID: context.importID, revision: 2, sourceRevision: context.sourceRevision),
                      PreparationContext(importID: context.importID, revision: 1, sourceRevision: "changed-source")] {
            guard let other, !view.reviewAgentProposal(other), calls.isEmpty else { return false }
        }
        view.job = PreparationJob(PreparationTestData.job())
        guard view.hasActiveJob, !view.reviewAgentProposal(context), calls.isEmpty else { return false }
        view.job = nil; view.resolutionJob = PreparationResolutionJob(PreparationTestData.resolutionJob())
        guard view.hasActiveJob, !view.reviewAgentProposal(context), calls.isEmpty else { return false }
        // Terminal state alone cannot drop unresolved resolution ownership.
        view.resolutionJob = PreparationResolutionJob(PreparationTestData.resolutionJob(state: "interrupted", settled: false))
        guard view.hasActiveJob, !view.reviewAgentProposal(context), calls.isEmpty else { return false }
        view.resolutionJob = nil
        guard view.reviewAgentProposal(context), calls.count == 1 else { return false }
        let current = calls.removeFirst()
        view.bind(PreparationContext(importID: context.importID, revision: 2, sourceRevision: context.sourceRevision), saved: true, eligible: true)
        current.complete(PreparationTestData.resolutionForm(), nil)
        return view.form == nil && !view.busy && calls.isEmpty
    }
    static func resolutionContractSelfTest() -> Bool {
        var invalid = PreparationTestData.resolutionForm()
        invalid["resolution_required"] = "true"; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.resolutionForm(); invalid["resolution_digest"] = String(repeating: "f", count: 64)
        guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.resolutionForm(); var policy = PreparationTestData.resolutionPolicy; policy["network"] = ["arbitrary.test"]
        invalid["resolution_policy"] = policy; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.resolutionJob(); var counters = invalid["progress"] as! [String: Any]; counters["tools_discovered"] = 129
        invalid["progress"] = counters; guard PreparationResolutionJob(invalid) == nil else { return false }
        guard let parsedPolicy = PreparationResolutionPolicy(PreparationTestData.resolutionPolicy),
              parsedPolicy.display.contains("pypi.org"), parsedPolicy.display.contains("Deadline: 120 seconds"),
              let expired = PreparationResolutionReview(PreparationTestData.resolutionPlan(expired: true), context: PreparationTestData.context, candidateID: PreparationTestData.candidate, policy: parsedPolicy), !expired.canStart() else { return false }
        let view = PreparationWorkspaceView(); var calls: [PreparationTestRequest] = []
        view.available = { true }; view.request = { calls.append(PreparationTestRequest(path: $0, method: $1, body: $2, complete: $3)) }
        view.onUpdatedDraft = { object in
            guard let context = PreparationContext(object) else { return false }; view.bind(context, saved: true, eligible: true); return true
        }
        view.bind(PreparationTestData.context, saved: true, eligible: true); view.refreshForm()
        calls.removeFirst().complete(PreparationTestData.resolutionForm(), nil)
        guard view.resolutionReviewButton.isEnabled, !view.reviewButton.isEnabled, !view.startButton.isEnabled else { return false }
        view.reviewForm(); guard calls.isEmpty else { return false }
        view.reviewResolution(); guard calls.count == 1, calls[0].path == "v1/resolutions/plan", Set(calls[0].body!.keys) == ["import_id", "revision", "candidate_id", "policy"] else { return false }
        let stale = calls.removeFirst(); view.bind(PreparationContext(importID: PreparationTestData.importID, revision: 2, sourceRevision: PreparationTestData.source), saved: true, eligible: true)
        stale.complete(PreparationTestData.resolutionPlan(), nil); guard view.resolutionReview == nil, !view.busy else { return false }
        view.bind(PreparationTestData.context, saved: true, eligible: true); view.refreshForm(); calls.removeFirst().complete(PreparationTestData.resolutionForm(), nil)
        view.reviewResolution(); calls.removeFirst().complete(PreparationTestData.resolutionPlan(available: false), nil)
        view.startResolution(); guard calls.isEmpty, !view.resolutionStartButton.isEnabled else { return false }
        view.reviewResolution(); calls.removeFirst().complete(PreparationTestData.resolutionPlan(), nil)
        guard view.resolutionStartButton.isEnabled, view.resolutionStatus.stringValue.contains("once") else { return false }
        view.startResolution(); guard calls.count == 1, calls[0].path == "v1/resolutions/start", Set(calls[0].body!.keys) == ["plan_id", "plan_digest", "idempotency_key"], calls[0].body?["plan_digest"] as? String == PreparationTestData.planDigest else { return false }
        let key = calls[0].body?["idempotency_key"] as? String; calls.removeFirst().complete(nil, "connection_unavailable")
        guard calls.count == 1, calls[0].path == "v1/resolutions" else { return false }; calls.removeFirst().complete(["resolutions": []], nil)
        view.startResolution(); guard calls[0].body?["idempotency_key"] as? String == key else { return false }
        calls.removeFirst().complete(PreparationTestData.resolutionJob(), nil)
        guard view.hasActiveJob, view.resolutionCancelButton.isEnabled, !view.refreshButton.isEnabled, view.resolutionJob?.progress["tools_discovered"] == 1 else { return false }
        view.cancelResolution(); guard calls.count == 1, calls[0].path == "v1/resolutions/" + PreparationTestData.jobID + "/cancel", calls[0].body?.isEmpty == true else { return false }
        calls.removeFirst().complete(PreparationTestData.resolutionJob(state: "cancel_requested", updated: 2), nil)
        guard view.hasActiveJob, !view.resolutionCancelButton.isEnabled else { return false }
        view.poll(); guard calls.count == 2 else { return false }
        let preparations = calls.remove(at: 1); preparations.complete(["preparations": []], nil)
        var foreign = PreparationTestData.resolutionJob(updated: 99); var request = foreign["request"] as! [String: Any]
        request["source_revision"] = "foreign-source"; foreign["request"] = request; foreign["job_id"] = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        let pending = PreparationTestData.resolutionJob(state: "interrupted", updated: 3)
        calls.removeFirst().complete(["resolutions": [foreign, pending]], nil)
        guard calls.count == 1, calls[0].path == "v1/resolutions/" + PreparationTestData.jobID else { return false }
        calls.removeFirst().complete(pending, nil)
        guard view.hasActiveJob, view.resolutionRecoverButton.isEnabled, !view.resolutionRecoverButton.isHidden else { return false }
        view.recoverResolution(); guard calls.count == 1, calls[0].path == "v1/resolutions/" + PreparationTestData.jobID + "/recover" else { return false }
        calls.removeFirst().complete(PreparationTestData.resolutionJob(state: "interrupted", updated: 4, settled: true), nil)
        guard !view.hasActiveJob else { return false }
        // A successful result is supplied to the host for a new generated form.
        view.acceptResolution(PreparationResolutionJob(PreparationTestData.resolutionJob(state: "succeeded", updated: 5, settled: true))!)
        guard calls.count == 1, calls[0].path == "v1/preparations/form", calls[0].body?["resolution_digest"] as? String == String(repeating: "f", count: 64), calls[0].body?["candidate_id"] as? String == PreparationTestData.candidate else { return false }
        calls.removeFirst().complete(PreparationTestData.resolutionForm(resolved: true), nil)
        guard view.reviewButton.isEnabled, view.form?.resolutionDigest != nil else { return false }
        var carriedReview = PreparationTestData.review(revision: 2, available: true)
        carriedReview["resolution_digest"] = String(repeating: "f", count: 64)
        guard PreparationReview(carriedReview, form: view.form!) != nil else { return false }
        carriedReview["resolution_digest"] = String(repeating: "0", count: 64)
        guard PreparationReview(carriedReview, form: view.form!) == nil else { return false }
        view.reviewForm(); guard calls.count == 1, calls[0].body?["resolution_digest"] as? String == view.form?.resolutionDigest else { return false }
        calls.removeFirst().complete(PreparationTestData.review(revision: 2, available: true), nil)
        guard view.review?.context.revision == 2, view.resolutionJob?.revision == 1, view.startButton.isEnabled else { return false }
        view.bind(PreparationContext(importID: PreparationTestData.importID, revision: 3, sourceRevision: "new-source"), saved: true, eligible: true)
        return view.resolutionJob == nil && view.resolutionReview == nil && !view.hasActiveJob
    }

    static func contractSelfTest() -> Bool {
        var invalid = PreparationTestData.form()
        invalid["schema_version"] = 2; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.form(); invalid["unknown"] = "must fail"; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.form(); invalid["candidate_id"] = String(repeating: "f", count: 64); guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.form(); var fields = invalid["fields"] as! [[String: Any]]
        fields[0]["kind"] = "executable"; invalid["fields"] = fields; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.form(); fields = invalid["fields"] as! [[String: Any]]
        fields[1]["value"] = true; invalid["fields"] = fields; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = PreparationTestData.form(); fields = invalid["fields"] as! [[String: Any]]
        fields[1]["id"] = fields[0]["id"]; invalid["fields"] = fields; guard OnboardingFormV1(invalid) == nil else { return false }
        var boundary = PreparationTestData.form()
        var boundedFields = (0..<256).map { index -> [String: Any] in
            ["id": String(format: "%064x", index), "label": "Bounded question", "description": "", "kind": "string", "required": false,
             "constraints": [:], "choices": [], "value": NSNull(), "schema": NSNull()]
        }
        let boundedChoices = (0..<128).map { ["id": "choice-\($0)", "label": "Choice \($0)"] }
        boundedFields[0]["kind"] = "choice"; boundedFields[0]["choices"] = boundedChoices
        boundary["fields"] = boundedFields; boundary["blockers"] = (0..<128).map { "blocker_\($0)" }
        guard let bounded = OnboardingFormV1(boundary), bounded.fields.count == 256, bounded.fields[0].choices.count == 128, bounded.blockers.count == 128 else { return false }
        invalid = boundary; invalid["blockers"] = (0..<129).map { "blocker_\($0)" }; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = boundary; var overflow = boundedFields; var extra = boundedFields[1]; extra["id"] = String(format: "%064x", 256)
        overflow.append(extra); invalid["fields"] = overflow; guard OnboardingFormV1(invalid) == nil else { return false }
        invalid = boundary; overflow = boundedFields; overflow[0]["choices"] = boundedChoices + [["id": "choice-128", "label": "Overflow"]]
        invalid["fields"] = overflow; guard OnboardingFormV1(invalid) == nil else { return false }
        let unicodeLabel = String(repeating: "界", count: 256)
        guard preparationText(unicodeLabel, maximum: 256) != nil, preparationText(unicodeLabel + "界", maximum: 256) == nil else { return false }
        let fullObject: [String: Any] = ["value": String(repeating: "x", count: 16372)]
        guard let compactObject = try? JSONSerialization.data(withJSONObject: fullObject), compactObject.count == 16384,
              let prettyObject = preparationJSON(fullObject, maximum: 16384), prettyObject.utf8.count > 16384,
              let objectField = PreparationField(id: "object", label: "Object", kind: "object", required: true), objectField.accepts(fullObject),
              !objectField.accepts(["value": String(repeating: "x", count: 16373)]) else { return false }
        let objectRenderer = PreparationFormView(); objectRenderer.configure(fields: [objectField], values: ["object": fullObject]); objectRenderer.setEditable(true)
        guard objectRenderer.answers()?["object"] is [String: Any] else { return false }
        guard let parsed = OnboardingFormV1(PreparationTestData.form()), parsed.fields.count == 8,
              parsed.fields[0].accepts("café"), !parsed.fields[0].accepts("longer"), !parsed.fields[1].accepts(1.2),
              !parsed.fields[1].accepts(true), !parsed.fields[2].accepts(Double.infinity), !parsed.fields[3].accepts(1),
              !parsed.fields[4].accepts("Display label"), !parsed.fields[5].accepts("relative/file"), !parsed.fields[6].accepts([1, 2]),
              !parsed.fields[7].accepts([:]), PreparationField(id: "test", label: "Test", kind: "string", required: true)?.accepts("") == true,
              PreparationField(id: "test", label: "Test", kind: "object", required: true, constraints: ["max_length": 3]) == nil else { return false }
        let renderer = PreparationFormView(); renderer.configure(fields: parsed.fields, values: parsed.values); renderer.setEditable(true)
        guard let answers = renderer.answers(), answers.count == 7, answers[String(repeating: "5", count: 64)] as? String == "opaque-choice",
              preparationBoolean(answers[String(repeating: "4", count: 64)]) == true,
              answers[String(repeating: "7", count: 64)] is [String: Any], answers[String(repeating: "8", count: 64)] == nil else { return false }
        invalid = PreparationTestData.review(); invalid["form_digest"] = String(repeating: "f", count: 64)
        guard PreparationReview(invalid, form: parsed) == nil else { return false }
        guard let disabled = PreparationReview(PreparationTestData.review(), form: parsed), !disabled.canStart(),
              disabled.display.contains("Synthetic app"), disabled.display.contains("synthetic: A synthetic tool."),
              disabled.display.contains("/tmp/selected-input"), disabled.display.contains("example.test"), disabled.display.contains("Memory: 128 MB"),
              disabled.display.contains("Work: Build, Probe"), disabled.display.contains("Deadline: 60 seconds"), disabled.display.contains("Disk budget:"),
              disabled.display.contains("Memory budget: 256 MB"), disabled.display.contains("No additional app artifacts selected."),
              disabled.display.contains("Preparation may acquire locked dependencies from the reviewed hosts."),
              !disabled.display.contains(PreparationTestData.importID), !disabled.display.contains(PreparationTestData.source),
              !disabled.display.contains(PreparationTestData.recipeDigest), !disabled.display.contains("input_schema"),
              !disabled.display.contains("form_digest"), !disabled.display.contains("wall_seconds"), !disabled.display.contains("recipe_id") else { return false }
        invalid = PreparationTestData.review(); invalid["downloads"] = [["unknown": "future artifact shape"]]
        guard PreparationReview(invalid, form: parsed) == nil else { return false }
        invalid = PreparationTestData.job(); invalid["progress"] = ["stage": "unknown_stage", "commands_finished": 0, "wheels_acquired": 0, "bytes_acquired": 0, "files_checked": 0, "fixtures_checked": 0]
        guard PreparationJob(invalid) == nil else { return false }
        var detectorJob = PreparationTestData.job()
        var detectorProgress = detectorJob["progress"] as! [String: Any]
        detectorProgress["stage"] = "onnx_model_validation"; detectorJob["progress"] = detectorProgress
        guard let detector = PreparationJob(detectorJob), detector.active,
              detector.summary.contains("Verifying the image detector") else { return false }
        invalid = PreparationTestData.job(); var counters = invalid["progress"] as! [String: Any]; counters["files_checked"] = 200001; invalid["progress"] = counters
        guard PreparationJob(invalid) == nil else { return false }

        let view = PreparationWorkspaceView(); var calls: [PreparationTestRequest] = []; var attached = 0
        view.available = { true }; view.request = { calls.append(PreparationTestRequest(path: $0, method: $1, body: $2, complete: $3)) }
        view.onAttached = { attached += 1 }
        view.onUpdatedDraft = { draft in
            guard let context = PreparationContext(draft) else { return false }; view.bind(context, saved: true, eligible: true); return true
        }
        view.bind(PreparationTestData.context, saved: false, eligible: true); view.refreshForm(); guard calls.isEmpty else { return false }
        view.bind(PreparationTestData.context, saved: true, eligible: true); view.refreshForm()
        guard calls.count == 1, calls[0].path == "v1/preparations/form", calls[0].method == "POST", Set(calls[0].body!.keys) == ["import_id", "revision"] else { return false }
        let stale = calls.removeFirst()
        let second = PreparationContext(importID: PreparationTestData.importID, revision: 2, sourceRevision: PreparationTestData.source)!
        view.bind(second, saved: true, eligible: true); stale.complete(PreparationTestData.form(), nil)
        guard view.form == nil, !view.busy else { return false }
        view.refreshForm(); calls.removeFirst().complete(PreparationTestData.form(revision: 2), nil)
        guard view.form?.revision == 2, !view.reviewButton.isEnabled, view.reviewButton.isHidden, view.formView.isHidden,
              view.status.stringValue.contains("can't prepare this app automatically") else { return false }
        view.reviewForm(); guard calls.isEmpty else { return false }
        var unsupported = PreparationTestData.form(revision: 2)
        unsupported["candidate_choices"] = []; unsupported["candidate_id"] = NSNull()
        unsupported["recipe_choices"] = []; unsupported["recipe"] = NSNull(); unsupported["fields"] = []
        unsupported["tools"] = []; unsupported["blockers"] = ["supported_interface_selection_required"]
        view.bind(nil, saved: true, eligible: true); view.bind(second, saved: true, eligible: true)
        view.refreshForm(); calls.removeFirst().complete(unsupported, nil)
        guard !view.reviewButton.isEnabled, view.formView.isHidden, view.candidateRow?.isHidden == true, view.recipeRow?.isHidden == true,
              view.status.stringValue.contains("couldn't find a supported app entry point") else { return false }
        view.refreshForm(); calls.removeFirst().complete(PreparationTestData.form(revision: 2, available: true), nil)
        guard view.form?.revision == 2, view.reviewButton.isEnabled, !view.startButton.isEnabled else { return false }
        view.reviewForm(); guard calls.count == 1, calls[0].path == "v1/preparations/plan-form", Set(calls[0].body!.keys) == ["import_id", "revision", "candidate_id", "recipe", "form_digest", "answers"],
            (calls[0].body?["answers"] as? [String: Any])?.count == 7 else { return false }
        calls.removeFirst().complete(PreparationTestData.review(revision: 3), nil)
        guard view.review?.context.revision == 3, !view.startButton.isEnabled else { return false }
        view.startPreparation(); guard calls.isEmpty else { return false }
        view.refreshForm(); calls.removeFirst().complete(PreparationTestData.form(revision: 3, available: true), nil)
        view.reviewForm(); calls.removeFirst().complete(PreparationTestData.review(revision: 4, available: true), nil)
        guard view.startButton.isEnabled, view.review?.context.revision == 4 else { return false }
        view.startPreparation(); guard calls.count == 1, calls[0].path == "v1/preparations/start",
            calls[0].body?["plan_id"] as? String == PreparationTestData.planID, calls[0].body?["plan_digest"] as? String == PreparationTestData.planDigest,
            Set(calls[0].body!.keys) == ["plan_id", "plan_digest", "idempotency_key"], preparationUUID(calls[0].body?["idempotency_key"]) != nil else { return false }
        let key = calls[0].body?["idempotency_key"] as? String
        calls.removeFirst().complete(nil, "connection_unavailable")
        guard calls.count == 1, calls[0].path == "v1/preparations" else { return false }; calls.removeFirst().complete(["preparations": []], nil)
        view.startPreparation(); guard calls[0].body?["idempotency_key"] as? String == key else { return false }
        calls.removeFirst().complete(PreparationTestData.job(revision: 4), nil)
        guard view.hasActiveJob, view.job?.progress["bytes_acquired"] == 4096, !view.startButton.isEnabled, view.cancelButton.isEnabled else { return false }
        // A newer update for another source must not mask this source's active job.
        view.poll(); var otherSource = PreparationTestData.job(revision: 4, updated: 99)
        otherSource["job_id"] = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        otherSource["binding"] = ["import_id": PreparationTestData.importID, "revision": 4, "source_revision": "different-source"]
        let ownedUpdate = PreparationTestData.job(revision: 4, updated: 1.5)
        calls.removeFirst().complete(["preparations": [PreparationTestData.summary(otherSource), PreparationTestData.summary(ownedUpdate)]], nil)
        guard calls.count == 1, calls[0].path == "v1/preparations/" + PreparationTestData.jobID else { return false }
        calls.removeFirst().complete(ownedUpdate, nil)
        guard view.job?.id == PreparationTestData.jobID, view.job?.updatedAt == 1.5, view.hasActiveJob else { return false }
        view.cancelPreparation(); guard calls.count == 1, calls[0].path == "v1/preparations/" + PreparationTestData.jobID + "/cancel", calls[0].body?.isEmpty == true else { return false }
        calls.removeFirst().complete(PreparationTestData.job(state: "cancel_requested", revision: 4, updated: 2), nil)
        guard view.hasActiveJob, !view.cancelButton.isEnabled else { return false }
        view.poll(); let finished = PreparationTestData.job(state: "succeeded", revision: 4, updated: 3)
        calls.removeFirst().complete(["preparations": [PreparationTestData.summary(finished)]], nil)
        guard calls.count == 1, calls[0].path == "v1/preparations/" + PreparationTestData.jobID else { return false }
        calls.removeFirst().complete(finished, nil)
        guard !view.hasActiveJob, view.job?.state == "succeeded", attached == 1 else { return false }
        view.poll(); calls.removeFirst().complete(["preparations": [PreparationTestData.summary(PreparationTestData.job(revision: 4))]], nil)
        guard calls.isEmpty, view.job?.state == "succeeded" else { return false }
        // Attachment advances the draft revision while the owned job keeps its binding.
        view.bind(PreparationContext(importID: PreparationTestData.importID, revision: 5, sourceRevision: PreparationTestData.source), saved: true, eligible: false)
        view.poll(); let newer = PreparationTestData.job(state: "succeeded", revision: 4, updated: 4)
        calls.removeFirst().complete(["preparations": [PreparationTestData.summary(newer)]], nil); calls.removeFirst().complete(newer, nil)
        guard view.job?.revision == 4, attached == 1 else { return false }
        view.poll(); var foreign = PreparationTestData.job(state: "preparing", revision: 4, updated: 5)
        foreign["binding"] = ["import_id": PreparationTestData.importID, "revision": 4, "source_revision": "different-source"]
        calls.removeFirst().complete(["preparations": [PreparationTestData.summary(foreign)]], nil)
        guard calls.isEmpty, view.job?.state == "succeeded", !view.hasActiveJob else { return false }
        view.invalidate(); guard calls.isEmpty, view.job?.state == "succeeded", view.review == nil else { return false }
        view.bind(PreparationTestData.context, saved: true, eligible: true); view.refreshForm(); calls.removeFirst().complete(nil, "caller\nsecret")
        guard !view.status.stringValue.contains("secret") else { return false }
        var unavailable = PreparationTestData.form()
        unavailable["recipe"] = NSNull(); unavailable["blockers"] = ["recipe_selection_required"]
        view.bind(PreparationTestData.context, saved: true, eligible: true)
        for _ in 0..<3 {
            view.refreshForm(); guard calls.count == 1 else { return false }
            calls.removeFirst().complete(unavailable, nil)
            guard view.form?.recipe == nil, view.recipePicker.selectedItem?.representedObject == nil,
                  !view.acceptanceReviewEnabled, view.recipeRow?.isHidden == false,
                  view.status.stringValue.contains("recipe selection required") else { return false }
        }
        return true
    }
}

extension PreparationRecipesView {
    static func contractSelfTest() -> Bool {
        let view = PreparationRecipesView(); var calls: [PreparationTestRequest] = []
        view.available = { true }; view.request = { calls.append(PreparationTestRequest(path: $0, method: $1, body: $2, complete: $3)) }
        let identity: [String: Any] = ["recipe_id": "example.synthetic", "version": "1.0.0", "digest": PreparationTestData.recipeDigest]
        var record = identity; record["state"] = "disabled"; record["runnable"] = false
        record["manifest"] = ["compatibility": ["worker_api_version": 1], "effects": ["network": []], "documentation": ["README.md"]]
        record["evidence"] = NSNull(); record["blockers"] = ["recipe_disabled"]
        view.refreshRecipes(); guard calls.count == 1, calls[0].path == "v1/recipes", calls[0].method == "GET" else { return false }
        calls.removeFirst().complete(["recipes": [identity]], nil)
        guard calls.count == 1, calls[0].path == "v1/recipes/inspect", Set(calls[0].body!.keys) == ["recipe_id", "version"] else { return false }
        calls.removeFirst().complete(record, nil)
        guard view.selected?.digest == PreparationTestData.recipeDigest, view.detail.string.contains("README.md"), !view.status.stringValue.contains("Execution admitted") else { return false }
        let button = NSButton(); button.identifier = NSUserInterfaceItemIdentifier("enable"); view.changeRecipe(button)
        guard calls.count == 1, calls[0].path == "v1/recipes/enable", calls[0].body?["version"] as? String == "1.0.0", Set(calls[0].body!.keys) == ["recipe_id", "version"] else { return false }
        calls.removeFirst().complete(record, nil); guard calls.count == 1 else { return false }
        let stale = calls.removeFirst(); view.invalidate(); stale.complete(["recipes": []], nil)
        guard view.selected?.digest == PreparationTestData.recipeDigest else { return false }
        view.refreshRecipes(); calls.removeFirst().complete(["recipes": [identity]], nil)
        record["digest"] = String(repeating: "f", count: 64); calls.removeFirst().complete(record, nil)
        guard view.selected == nil, view.detail.string.isEmpty else { return false }
        return true
    }
}

func preparationFormsSelfTest(fixture: URL? = nil) -> Bool {
    if let fixture {
        guard fixture.isFileURL, fixture.path.hasPrefix("/"),
              let size = try? fixture.resourceValues(forKeys: [.fileSizeKey]).fileSize, size <= 1_048_576,
              let data = try? Data(contentsOf: fixture), data.count <= 1_048_576,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return false }
        if let formObject = object["form"] as? [String: Any], let reviewObject = object["review"] as? [String: Any] {
            guard Set(object.keys) == ["form", "review"], let form = OnboardingFormV1(formObject), PreparationReview(reviewObject, form: form) != nil else { return false }
        } else if OnboardingFormV1(object) == nil { return false }
    }
    return PreparationWorkspaceView.contractSelfTest() && PreparationWorkspaceView.resolutionContractSelfTest() && PreparationRecipesView.contractSelfTest()
}
