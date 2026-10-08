import AppKit

final class WorkspaceDocumentStack: NSStackView {
    override var isFlipped: Bool { true }
}

func workspaceLabel(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular,
                    secondary: Bool = false) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = .systemFont(ofSize: size, weight: weight)
    label.textColor = secondary ? .secondaryLabelColor : .labelColor
    label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    return label
}

func workspaceStack(_ views: [NSView] = [], horizontal: Bool = false, spacing: CGFloat = 12) -> NSStackView {
    let stack = NSStackView(views: views)
    stack.orientation = horizontal ? .horizontal : .vertical
    stack.alignment = horizontal ? .centerY : .leading
    stack.spacing = spacing
    stack.translatesAutoresizingMaskIntoConstraints = false
    return stack
}

func workspaceFill(_ view: NSView, in parent: NSView, inset: CGFloat = 0) {
    view.translatesAutoresizingMaskIntoConstraints = false
    NSLayoutConstraint.activate([
        view.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset),
        view.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset),
    ])
}

/// A document section whose contents remain visible in native light and dark appearances.
func workspaceCard(_ title: String, subtitle: String, views: [NSView]) -> NSView {
    let box = NSBox()
    box.boxType = .custom; box.titlePosition = .noTitle
    box.borderWidth = 1
    box.borderColor = .separatorColor; box.fillColor = .controlBackgroundColor
    box.cornerRadius = 12; box.contentViewMargins = .zero
    let heading = workspaceLabel(title, size: 16, weight: .semibold)
    let caption = workspaceLabel(subtitle, size: 12, secondary: true)
    let stack = workspaceStack([heading, caption] + views)
    let content = box.contentView!
    content.addSubview(stack)
    NSLayoutConstraint.activate([
        stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
        stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18),
        stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
        stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18),
    ])
    for child in [heading, caption] + views { workspaceFill(child, in: stack) }
    return box
}
