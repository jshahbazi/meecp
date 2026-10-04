import AppKit

private final class SystemPromptTextView: NSTextView {
    private let edits = UndoManager()
    override var undoManager: UndoManager? { edits }
}

/// A plain-text editor that grows to three visual lines, then scrolls.
final class SystemPromptEditor: NSScrollView, NSTextViewDelegate {
    let textView: NSTextView
    private var editorHeight: NSLayoutConstraint!
    private var boundedHeight: CGFloat = 0
    private var updatingHeight = false

    var string: String {
        get { textView.string }
        set {
            textView.string = newValue
            refreshHeight()
        }
    }

    var isEditable: Bool {
        get { textView.isEditable }
        set { textView.isEditable = newValue }
    }

    override var intrinsicContentSize: NSSize {
        NSSize(width: NSView.noIntrinsicMetric, height: boundedHeight)
    }

    init(text: String) {
        textView = SystemPromptTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 30))
        super.init(frame: NSRect(x: 0, y: 0, width: 400, height: 30))
        translatesAutoresizingMaskIntoConstraints = false
        hasVerticalScroller = true
        hasHorizontalScroller = false
        autohidesScrollers = true
        borderType = .noBorder
        wantsLayer = true
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor
        layer?.cornerRadius = 3
        textView.isEditable = true
        textView.isSelectable = true
        textView.isRichText = false
        textView.allowsUndo = true
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.isAutomaticSpellingCorrectionEnabled = false
        textView.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        textView.textContainerInset = NSSize(width: 8, height: 5)
        textView.minSize = .zero
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude,
                                 height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: 400,
                                                      height: CGFloat.greatestFiniteMagnitude)
        textView.delegate = self
        documentView = textView
        editorHeight = heightAnchor.constraint(equalToConstant: 30)
        editorHeight.isActive = true
        string = text
    }

    required init?(coder: NSCoder) {
        fatalError("SystemPromptEditor is initialized in code")
    }

    override func layout() {
        super.layout()
        refreshHeight()
    }

    func textDidChange(_ notification: Notification) {
        refreshHeight()
        textView.scrollRangeToVisible(textView.selectedRange())
    }

    func refreshHeight() {
        guard !updatingHeight,
              let layoutManager = textView.layoutManager,
              let container = textView.textContainer,
              let font = textView.font else { return }
        updatingHeight = true
        defer { updatingHeight = false }

        let width = max(1, contentSize.width)
        if abs(textView.frame.width - width) > 0.5 {
            textView.setFrameSize(NSSize(width: width, height: textView.frame.height))
        }
        layoutManager.ensureLayout(for: container)
        var lines = 0
        layoutManager.enumerateLineFragments(
            forGlyphRange: NSRange(location: 0, length: layoutManager.numberOfGlyphs)
        ) { _, _, _, _, _ in lines += 1 }
        if layoutManager.extraLineFragmentTextContainer != nil { lines += 1 }

        let lineHeight = layoutManager.defaultLineHeight(for: font)
        let verticalInset = textView.textContainerInset.height * 2
        let height = ceil(CGFloat(min(3, max(1, lines))) * lineHeight + verticalInset)
        if abs(boundedHeight - height) > 0.5 {
            boundedHeight = height
            editorHeight.constant = height
            invalidateIntrinsicContentSize()
        }

        let usedHeight = max(layoutManager.usedRect(for: container).maxY,
                             layoutManager.extraLineFragmentRect.maxY)
        let documentHeight = max(height, ceil(usedHeight + verticalInset))
        if abs(textView.frame.height - documentHeight) > 0.5 {
            textView.setFrameSize(NSSize(width: width, height: documentHeight))
        }
        reflectScrolledClipView(contentView)
    }
}
