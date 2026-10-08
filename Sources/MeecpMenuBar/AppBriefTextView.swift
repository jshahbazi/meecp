import AppKit

/// Keeps the starting point useful before the user has written an app brief.
final class AppBriefTextView: NSTextView {
    override var string: String {
        didSet { needsDisplay = true }
    }

    override func didChangeText() {
        super.didChangeText()
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty else { return }
        let placeholder = "Describe the tools you want. For example: search my notes, connect to an API, or wrap a command-line app."
        let inset = textContainerInset
        let padding = textContainer?.lineFragmentPadding ?? 5
        let rect = NSRect(x: inset.width + padding, y: inset.height,
                          width: max(0, bounds.width - 2 * (inset.width + padding)), height: bounds.height - 2 * inset.height)
        (placeholder as NSString).draw(in: rect, withAttributes: [
            .font: font ?? NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.placeholderTextColor
        ])
    }
}
