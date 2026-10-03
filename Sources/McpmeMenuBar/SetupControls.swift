import AppKit
import CoreFoundation
import Darwin

enum SetupChoiceError: Error { case invalid }

private func settingInteger(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
          abs(number.doubleValue) <= 9_007_199_254_740_992 else { return nil }
    return number.intValue
}

struct SetupField {
    let name: String
    let type: String
    let required: Bool
    let declaration: [String: Any]
    let choices: [Any]?
    var defaultValue: Any? { declaration["default"] }

    init?(name: String, declaration: [String: Any]) {
        guard name.range(of: "^[A-Za-z][A-Za-z0-9_]{0,63}$", options: .regularExpression) != nil,
              Set(declaration.keys).isSubset(of: ["type", "required", "default", "enum", "min_length", "max_length", "minimum", "maximum"]),
              let type = declaration["type"] as? String, ["string", "integer", "boolean"].contains(type),
              declaration["required"] == nil || (declaration["required"] as? NSNumber).map({ CFGetTypeID($0) == CFBooleanGetTypeID() }) == true else { return nil }
        self.name = name; self.type = type; self.required = declaration["required"] as? Bool ?? false
        self.declaration = declaration; self.choices = declaration["enum"] as? [Any]
        if declaration["enum"] != nil && (choices == nil || choices!.isEmpty || choices!.count > 32) { return nil }
        if let value = declaration["default"], !accepts(value) { return nil }
        if let values = choices, values.contains(where: { !accepts($0) }) { return nil }
        for key in ["min_length", "max_length", "minimum", "maximum"] where declaration[key] != nil {
            guard let bound = settingInteger(declaration[key]),
                  type == "string" && ["min_length", "max_length"].contains(key) && (0...4096).contains(bound) ||
                  type == "integer" && ["minimum", "maximum"].contains(key) else { return nil }
        }
        if let lower = settingInteger(declaration["min_length"]), let upper = settingInteger(declaration["max_length"]), lower > upper { return nil }
        if let lower = settingInteger(declaration["minimum"]), let upper = settingInteger(declaration["maximum"]), lower > upper { return nil }
    }

    func accepts(_ value: Any) -> Bool {
        switch type {
        case "string":
            guard let text = value as? String, !text.contains("\0"),
                  text.unicodeScalars.count >= (settingInteger(declaration["min_length"]) ?? 0),
                  text.unicodeScalars.count <= (settingInteger(declaration["max_length"]) ?? 4096) else { return false }
        case "integer":
            guard let value = settingInteger(value), value >= (settingInteger(declaration["minimum"]) ?? -9_007_199_254_740_992),
                  value <= (settingInteger(declaration["maximum"]) ?? 9_007_199_254_740_992) else { return false }
        case "boolean":
            guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return false }
        default: return false
        }
        if let choices = choices {
            return choices.contains { lhs in
                if type == "string" { return lhs as? String == value as? String }
                if type == "integer" { return settingInteger(lhs) == settingInteger(value) }
                return lhs as? Bool == value as? Bool
            }
        }
        return true
    }
}

struct SetupSecret: Equatable {
    let id: String
    let name: String
    init?(_ object: [String: Any]) {
        guard let id = object["secret_id"] as? String, UUID(uuidString: id)?.uuidString.lowercased() == id,
              let name = object["name"] as? String, !name.isEmpty, name.count <= 64,
              !name.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              object["status"] as? String == "active", object["value"] == nil else { return nil }
        self.id = id; self.name = name
    }
}

/// Reads a selected secret without placing its value in a text field, setup record, or diagnostic.
/// Both the opened descriptor and selected leaf must remain the same owner-only regular file.
func readSetupSecret(_ url: URL) -> String? {
    let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard fd >= 0 else { return nil }; defer { close(fd) }
    var before = stat(), after = stat(), leaf = stat()
    guard fstat(fd, &before) == 0, before.st_mode & S_IFMT == S_IFREG,
          before.st_uid == getuid(), before.st_nlink == 1, before.st_mode & 0o777 == 0o600,
          before.st_size > 0, before.st_size <= 4096 else { return nil }
    var bytes = Data(count: Int(before.st_size)), offset = 0
    let success = bytes.withUnsafeMutableBytes { buffer -> Bool in
        while offset < buffer.count {
            let count = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
            if count <= 0 { return false }; offset += count
        }
        return true
    }
    guard success, fstat(fd, &after) == 0, lstat(url.path, &leaf) == 0,
          before.st_dev == after.st_dev, before.st_ino == after.st_ino,
          before.st_size == after.st_size, before.st_mode == after.st_mode,
          before.st_uid == after.st_uid, before.st_nlink == after.st_nlink,
          before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec, before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
          before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec, before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec,
          after.st_dev == leaf.st_dev, after.st_ino == leaf.st_ino, after.st_mode == leaf.st_mode,
          after.st_size == leaf.st_size, after.st_uid == leaf.st_uid, after.st_nlink == leaf.st_nlink,
          after.st_mtimespec.tv_sec == leaf.st_mtimespec.tv_sec, after.st_mtimespec.tv_nsec == leaf.st_mtimespec.tv_nsec,
          after.st_ctimespec.tv_sec == leaf.st_ctimespec.tv_sec, after.st_ctimespec.tv_nsec == leaf.st_ctimespec.tv_nsec,
          let value = String(data: bytes, encoding: .utf8), !value.contains("\0") else { return nil }
    return value
}

/// A bounded metadata preview for a selected replacement package. This does not
/// validate artifacts or grant execution; the owner plan verifies the full tree.
func readSetupPackageMetadata(_ url: URL) -> [String: Any]? {
    let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard fd >= 0 else { return nil }; defer { close(fd) }
    var before = stat(), after = stat(), leaf = stat()
    guard fstat(fd, &before) == 0, before.st_mode & S_IFMT == S_IFREG, before.st_nlink == 1,
          before.st_mode & 0o7022 == 0, before.st_size > 0, before.st_size <= 1_048_576 else { return nil }
    var bytes = Data(count: Int(before.st_size)), offset = 0
    let success = bytes.withUnsafeMutableBytes { buffer -> Bool in
        while offset < buffer.count {
            let count = Darwin.read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
            if count <= 0 { return false }; offset += count
        }
        return true
    }
    guard success, fstat(fd, &after) == 0, lstat(url.path, &leaf) == 0,
          before.st_dev == after.st_dev, before.st_ino == after.st_ino, before.st_size == after.st_size,
          before.st_mode == after.st_mode, before.st_nlink == after.st_nlink,
          before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec, before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
          before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec, before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec,
          after.st_dev == leaf.st_dev, after.st_ino == leaf.st_ino, after.st_size == leaf.st_size,
          after.st_mode == leaf.st_mode, after.st_nlink == leaf.st_nlink,
          after.st_mtimespec.tv_sec == leaf.st_mtimespec.tv_sec, after.st_mtimespec.tv_nsec == leaf.st_mtimespec.tv_nsec,
          after.st_ctimespec.tv_sec == leaf.st_ctimespec.tv_sec, after.st_ctimespec.tv_nsec == leaf.st_ctimespec.tv_nsec else { return nil }
    return (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any]
}

final class SetupConfigurationView: NSStackView, NSTextFieldDelegate {
    var onChange: (() -> Void)?
    var onCreateSecret: ((String) -> Void)?
    private var fields: [SetupField] = []
    private var inputs: [String: NSControl] = [:]
    private var included: [String: NSButton] = [:]
    private var references: [String: NSPopUpButton] = [:]
    private var createButtons: [NSButton] = []
    private var secretChoices: [SetupSecret] = []
    private var savedReferences: [String: String] = [:]
    private var editable = true
    private(set) var validContract = true

    override init(frame: NSRect) {
        super.init(frame: frame); orientation = .vertical; alignment = .leading; spacing = 10
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func configure(package: [String: Any]?, settings: [String: Any]) {
        for view in arrangedSubviews { removeArrangedSubview(view); view.removeFromSuperview() }
        fields = []; inputs = [:]; included = [:]; references = [:]; createButtons = []
        savedReferences = settings["secret_references"] as? [String: String] ?? [:]; validContract = true
        guard let package else { validContract = false; isHidden = true; return }; isHidden = false
        let declaration = package["configuration"] as? [String: Any] ?? ["schema_version": 1, "fields": [String: Any]()]
        let rows = declaration["fields"] as? [String: [String: Any]] ?? [:]
        let parsed = rows.map { SetupField(name: $0.key, declaration: $0.value) }
        let names = (package["access"] as? [String: Any])?["secrets"] as? [String] ?? []
        guard (package["configuration"] == nil || package["configuration"] is [String: Any]),
              Set(declaration.keys).isSubset(of: ["schema_version", "fields"]), settingInteger(declaration["schema_version"] ?? 1) == 1,
              (declaration["fields"] == nil || declaration["fields"] is [String: [String: Any]]),
              rows.count <= 32, parsed.allSatisfy({ $0 != nil }), names.count <= 16,
              Set(names).count == names.count, names.allSatisfy({ $0.range(of: "^[A-Za-z][A-Za-z0-9_]{0,63}$", options: .regularExpression) != nil }) else {
            validContract = false; addArrangedSubview(workspaceLabel("The package configuration contract is unavailable.")); return
        }
        fields = parsed.compactMap { $0 }.sorted { $0.name < $1.name }
        let selected = settings["configuration"] as? [String: Any] ?? [:]
        if fields.isEmpty && names.isEmpty { addArrangedSubview(workspaceLabel("This package needs no configuration or secrets.", size: 12, secondary: true)); return }
        addArrangedSubview(workspaceLabel("App configuration", size: 13, weight: .semibold))
        for field in fields {
            let value = selected[field.name] ?? field.defaultValue
            let use = NSButton(checkboxWithTitle: (field.required ? "" : "Set ") + field.name + (field.required ? " (required)" : ""), target: self, action: #selector(changed))
            use.state = field.required || value != nil ? .on : .off; use.isEnabled = !field.required
            use.setAccessibilityLabel("Include setting " + field.name); included[field.name] = use
            use.toolTip = "Unchecked uses the package default when provided; otherwise this optional setting is omitted."
            let input: NSControl
            if field.choices != nil || field.type == "boolean" {
                let picker = NSPopUpButton(); let values = field.choices ?? [false, true]
                for item in values {
                    picker.addItem(withTitle: field.type == "boolean" ? ((item as? Bool == true) ? "On" : "Off") : (item as? String) ?? String(describing: item))
                    picker.lastItem?.representedObject = item
                }
                if let value, let index = values.firstIndex(where: { NSDictionary(dictionary: ["v": $0]).isEqual(to: ["v": value]) }) { picker.selectItem(at: index) }
                picker.target = self; picker.action = #selector(changed); input = picker
            } else {
                let text = NSTextField(); text.delegate = self
                text.placeholderString = field.type == "integer" ? "Whole number" : "Value"
                text.stringValue = value.map { ($0 as? String) ?? String(describing: $0) } ?? ""
                input = text
            }
            input.setAccessibilityLabel("Value for " + field.name); inputs[field.name] = input
            let row = workspaceStack([use, input], horizontal: true)
            input.setContentHuggingPriority(.defaultLow, for: .horizontal)
            addArrangedSubview(row); workspaceFill(row, in: self)
        }
        for name in names.sorted() {
            let picker = NSPopUpButton(); picker.target = self; picker.action = #selector(changed)
            picker.setAccessibilityLabel("Secret reference for " + name); references[name] = picker
            let create = NSButton(title: "Add Secret from File…", target: self, action: #selector(createSecret(_:)))
            create.bezelStyle = .rounded; create.identifier = NSUserInterfaceItemIdentifier(name); createButtons.append(create)
            let row = workspaceStack([workspaceLabel(name + " (secret)", size: 12), picker, create], horizontal: true)
            picker.setContentHuggingPriority(.defaultLow, for: .horizontal)
            addArrangedSubview(row); workspaceFill(row, in: self)
        }
        updateSecrets(secretChoices); setEditable(editable)
    }

    func updateSecrets(_ choices: [SetupSecret]) {
        secretChoices = choices
        for (name, picker) in references {
            let selected = picker.selectedItem?.representedObject as? String ?? savedReferences[name]
            picker.removeAllItems(); picker.addItem(withTitle: "Choose a saved secret")
            for secret in choices {
                picker.addItem(withTitle: secret.name + " · " + String(secret.id.prefix(8)))
                picker.lastItem?.representedObject = secret.id
            }
            if let selected {
                if let index = picker.itemArray.firstIndex(where: { $0.representedObject as? String == selected }) { picker.selectItem(at: index) }
                else { picker.addItem(withTitle: "Unavailable secret · " + String(selected.prefix(8))); picker.lastItem?.representedObject = selected; picker.lastItem?.isEnabled = false; picker.selectItem(at: picker.numberOfItems - 1) }
            }
        }
    }

    func selectSecret(_ secret: SetupSecret, for name: String) {
        savedReferences[name] = secret.id
        updateSecrets(secretChoices.filter { $0.id != secret.id } + [secret])
        if let picker = references[name], let index = picker.itemArray.firstIndex(where: { $0.representedObject as? String == secret.id }) { picker.selectItem(at: index) }
        onChange?()
    }

    func setEditable(_ enabled: Bool) {
        editable = enabled
        for field in fields {
            included[field.name]?.isEnabled = enabled && !field.required
            inputs[field.name]?.isEnabled = enabled && included[field.name]?.state == .on
        }
        references.values.forEach { $0.isEnabled = enabled }; createButtons.forEach { $0.isEnabled = enabled }
    }

    func settings() throws -> [String: Any] {
        guard validContract else { throw SetupChoiceError.invalid }
        var configuration: [String: Any] = [:], secrets: [String: String] = [:]
        for field in fields where included[field.name]?.state == .on {
            let value: Any
            if let text = inputs[field.name] as? NSTextField {
                if field.type == "integer" {
                    guard text.stringValue.range(of: "^-?[0-9]+$", options: .regularExpression) != nil,
                          let number = Int(text.stringValue) else { throw SetupChoiceError.invalid }; value = number
                } else { value = text.stringValue }
            } else if let picker = inputs[field.name] as? NSPopUpButton, let chosen = picker.selectedItem?.representedObject { value = chosen }
            else { throw SetupChoiceError.invalid }
            guard field.accepts(value) else { throw SetupChoiceError.invalid }; configuration[field.name] = value
        }
        for (name, picker) in references {
            guard let id = picker.selectedItem?.representedObject as? String,
                  secretChoices.contains(where: { $0.id == id }) else { throw SetupChoiceError.invalid }; secrets[name] = id
        }
        let object: [String: Any] = ["schema_version": 1, "configuration": configuration, "secret_references": secrets]
        guard let bytes = try? JSONSerialization.data(withJSONObject: configuration), bytes.count <= 16_384 else { throw SetupChoiceError.invalid }
        return object
    }

    @objc private func changed() { setEditable(editable); onChange?() }
    func controlTextDidChange(_ notification: Notification) { changed() }
    @objc private func createSecret(_ button: NSButton) { if let name = button.identifier?.rawValue { onCreateSecret?(name) } }
}

func setupControlsSelfTest() -> Bool {
    let package: [String: Any] = ["configuration": ["schema_version": 1, "fields": [
        "Label": ["type": "string", "required": true, "min_length": 1, "default": "Synthetic"],
        "Count": ["type": "integer", "minimum": -2, "maximum": 4, "default": -1],
        "Enabled": ["type": "boolean", "default": false]]], "access": ["secrets": ["API_KEY"]]]
    let id = "11111111-2222-4333-8444-555555555555"
    guard let secret = SetupSecret(["secret_id": id, "name": "Synthetic", "status": "active"]),
          SetupSecret(["secret_id": id, "name": "Synthetic", "status": "active", "value": "never_display"]) == nil,
          SetupField(name: "Count", declaration: ["type": "integer", "default": true]) == nil,
          SetupField(name: "Label", declaration: ["type": "string", "default": NSNull()]) == nil else { return false }
    let form = SetupConfigurationView(frame: NSRect(x: 0, y: 0, width: 650, height: 300))
    form.configure(package: package, settings: ["configuration": [:], "secret_references": ["API_KEY": id]])
    guard (try? form.settings()) == nil else { return false }
    form.updateSecrets([secret])
    guard let settings = try? form.settings(), let values = settings["configuration"] as? [String: Any],
          values["Label"] as? String == "Synthetic", settingInteger(values["Count"]) == -1,
          values["Enabled"] as? Bool == false, settings["secret_references"] as? [String: String] == ["API_KEY": id] else { return false }
    form.setEditable(false); form.updateSecrets([])
    return (try? form.settings()) == nil && SetupConfigurationView.verifyChoices() && verifyPrivateSecretInput()
}

extension SetupConfigurationView {
    fileprivate static func verifyChoices() -> Bool {
        let form = SetupConfigurationView(frame: NSRect(x: 0, y: 0, width: 600, height: 300))
        let package: [String: Any] = ["configuration": ["fields": [
            "Count": ["type": "integer", "required": true, "minimum": -2, "maximum": 4],
            "Label": ["type": "string", "required": true, "min_length": 2, "max_length": 4],
            "Mode": ["type": "string", "enum": ["small", "large"]],
            "Flag": ["type": "boolean"]]], "access": ["secrets": []]]
        form.configure(package: package, settings: ["configuration": ["Count": -2, "Label": "ok"], "secret_references": [:]])
        guard let count = form.inputs["Count"] as? NSTextField, let label = form.inputs["Label"] as? NSTextField,
              (try? form.settings()) != nil else { return false }
        for text in ["1.2", "true", "-3", "5", "9007199254740993"] {
            count.stringValue = text
            guard (try? form.settings()) == nil else { return false }
        }
        count.stringValue = "4"; label.stringValue = "x"
        guard (try? form.settings()) == nil else { return false }
        label.stringValue = "length"
        guard (try? form.settings()) == nil else { return false }
        label.stringValue = "pass"; form.included["Mode"]?.state = .on
        guard let choices = try? form.settings(), let configuration = choices["configuration"] as? [String: Any],
              configuration["Mode"] as? String == "small", configuration["Flag"] == nil else { return false }
        form.setEditable(false)
        guard form.inputs.values.allSatisfy({ !$0.isEnabled }), form.included.values.allSatisfy({ !$0.isEnabled }) else { return false }
        form.configure(package: ["configuration": ["fields": ["Count": ["type": "integer", "minimum": 4, "maximum": -1]]]], settings: [:])
        return !form.validContract && (try? form.settings()) == nil
    }
}

private func verifyPrivateSecretInput() -> Bool {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("mcpme-native-secret-test-" + UUID().uuidString, isDirectory: true)
    do {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("synthetic.txt"), link = directory.appendingPathComponent("link.txt")
        try Data("synthetic\n".utf8).write(to: file)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        guard readSetupSecret(file) == "synthetic\n", readSetupSecret(directory) == nil else { return false }
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
        guard readSetupSecret(link) == nil else { return false }
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: file.path)
        guard readSetupSecret(file) == nil else { return false }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        try Data(repeating: 65, count: 4097).write(to: file)
        guard readSetupSecret(file) == nil else { return false }
        try Data([0xff]).write(to: file)
        guard readSetupSecret(file) == nil else { return false }
        try Data([0]).write(to: file)
        return readSetupSecret(file) == nil
    } catch { return false }
}
