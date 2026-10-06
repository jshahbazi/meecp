import AppKit

struct DashboardActivity: Equatable {
    let received: Int
    let succeeded: Int
    let failed: Int
    let inFlight: Int
    let lastMinute: Int
    let lastLatencyMS: Double?
    let lastRequestAt: Double?
    let startedAt: Double?
}

struct DashboardApp {
    let id: String
    let name: String
    let status: String
    let state: String
    let policy: String
    let activity: DashboardActivity?
    let error: String?
    let busy: Bool

    var needsAttention: Bool { state == "failed" || state == "ownership_lost" || error != nil }
    var blocked: Bool { state == "ownership_lost" || ["installation_recovery_required", "removal_recovery_required", "upgrade_recovery_required"].contains(error ?? "") }
    var transitioning: Bool { ["starting", "loading", "warming_up", "stopping", "draining", "updating"].contains(state) }
    var running: Bool { ["starting", "loading", "warming_up", "ready", "stopping", "draining", "updating"].contains(state) }
}

struct DashboardSection: Equatable {
    let title: String
    let body: String
}

struct DashboardDetails: Equatable {
    let appID: String
    let subtitle: String
    let sections: [DashboardSection]
}

/// Screens use AppKit's global coordinate system. Keep the menu aligned with its
/// status button, then open details on the side with room without shifting it.
func dashboardFrames(anchor: NSRect, screen: NSRect, showsDetails: Bool) -> (menu: NSRect, details: NSRect?) {
    let inset: CGFloat = 8
    let gap: CGFloat = 10
    let safe = screen.insetBy(dx: inset, dy: inset)
    let menuWidth = min(350, safe.width)
    let menuHeight = min(650, safe.height)
    let detailsHeight = min(650, safe.height)
    let x = min(max(anchor.maxX - menuWidth, safe.minX), safe.maxX - menuWidth)
    let top = min(max(anchor.minY - 5, safe.minY + menuHeight), safe.maxY)
    let menu = NSRect(x: x, y: top - menuHeight, width: menuWidth, height: menuHeight)
    guard showsDetails else { return (menu, nil) }
    let leftSpace = max(0, menu.minX - gap - safe.minX)
    let rightSpace = max(0, safe.maxX - menu.maxX - gap)
    let detailsY = min(max(top - detailsHeight, safe.minY), safe.maxY - detailsHeight)
    // On compact screens a side panel would become unreadable. Cover the
    // dropdown with a full-width detail panel until its close button is used.
    if max(leftSpace, rightSpace) < min(300, safe.width) {
        return (menu, NSRect(x: safe.minX, y: detailsY, width: safe.width, height: detailsHeight))
    }
    let opensLeft = leftSpace > rightSpace
    let detailsWidth = min(470, opensLeft ? leftSpace : rightSpace)
    let detailsX = opensLeft ? menu.minX - gap - detailsWidth : menu.maxX + gap
    return (menu, NSRect(x: detailsX, y: detailsY, width: detailsWidth, height: detailsHeight))
}

private func dashboardLabel(_ text: String = "", size: CGFloat = 13, weight: NSFont.Weight = .regular,
                            color: NSColor = .labelColor) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.font = .systemFont(ofSize: size, weight: weight)
    field.textColor = color
    field.lineBreakMode = .byTruncatingTail
    field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    return field
}

private final class DashboardDocumentStack: NSStackView {
    override var isFlipped: Bool { true }
}

private func dashboardStack(_ views: [NSView], spacing: CGFloat = 8, horizontal: Bool = false,
                            flipped: Bool = false) -> NSStackView {
    let stack = flipped ? DashboardDocumentStack(views: views) : NSStackView(views: views)
    stack.orientation = horizontal ? .horizontal : .vertical
    stack.alignment = horizontal ? .centerY : .leading
    stack.distribution = .fill
    stack.spacing = spacing
    stack.translatesAutoresizingMaskIntoConstraints = false
    return stack
}

private func dashboardFillWidth(_ child: NSView, in parent: NSView) {
    child.translatesAutoresizingMaskIntoConstraints = false
    let inset = (parent as? NSStackView)?.edgeInsets ?? NSEdgeInsetsZero
    NSLayoutConstraint.activate([
        child.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset.left),
        child.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset.right),
    ])
}

private func dashboardSeparator() -> NSBox {
    let line = NSBox()
    line.boxType = .separator
    return line
}

private func dashboardNumber(_ number: Int?) -> String {
    guard let number else { return "—" }
    return number.formatted(.number)
}

/// Counters are nonnegative, but their sum can exceed a machine integer even
/// when each individual host value is valid. A saturated display stays usable.
func dashboardTotal(_ values: [Int]) -> Int {
    values.reduce(0) { total, value in
        let result = total.addingReportingOverflow(max(0, value))
        return result.overflow ? Int.max : result.partialValue
    }
}

private func dashboardStatusColor(_ app: DashboardApp) -> NSColor {
    if app.needsAttention { return .systemOrange }
    if app.state == "ready" { return .systemGreen }
    if app.running { return .systemBlue }
    return .tertiaryLabelColor
}

private final class DashboardPanel: NSPanel {
    var onEscape: (() -> Void)?
    var onArrow: ((Int) -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onEscape?() }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 { onEscape?(); return }
        if event.keyCode == 125 { onArrow?(1); return }
        if event.keyCode == 126 { onArrow?(-1); return }
        super.keyDown(with: event)
    }
}

private final class DashboardAction: NSButton {
    var handler: (() -> Void)?
    init(_ title: String, symbol: String? = nil, handler: (() -> Void)? = nil) {
        super.init(frame: .zero)
        self.title = title
        self.handler = handler
        bezelStyle = .rounded
        controlSize = .regular
        font = .systemFont(ofSize: 12, weight: .medium)
        target = self
        action = #selector(invoke)
        if let symbol { image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil); imagePosition = .imageLeading }
        setAccessibilityLabel(title)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func invoke() { handler?() }
}

private final class DashboardMetric: NSView {
    let value = dashboardLabel("—", size: 23, weight: .semibold)
    private let caption: NSTextField
    init(_ title: String, compact: Bool = false) {
        caption = dashboardLabel(title, size: 11, color: .secondaryLabelColor)
        super.init(frame: .zero)
        value.font = .monospacedDigitSystemFont(ofSize: compact ? 21 : 23, weight: .semibold)
        let stack = dashboardStack([value, caption], spacing: 3)
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor), stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor), stack.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        setAccessibilityElement(true)
        setAccessibilityRole(.staticText)
        setAccessibilityLabel(title)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(_ text: String) { value.stringValue = text; setAccessibilityValue(text) }
}

private final class DashboardRow: NSButton {
    let appID: String
    var handler: ((String) -> Void)?
    private let nameLabel = dashboardLabel(size: 13, weight: .semibold)
    private let statusLabel = dashboardLabel(size: 11, color: .secondaryLabelColor)
    private let countLabel = dashboardLabel(size: 12, weight: .medium, color: .secondaryLabelColor)
    private let countCaption = dashboardLabel("requests", size: 10, color: .tertiaryLabelColor)
    private let chevron = NSImageView()
    private let dot = NSView()
    private(set) var selected = false

    init(id: String) {
        appID = id
        super.init(frame: .zero)
        title = ""; isBordered = false; target = self; action = #selector(selectApp)
        wantsLayer = true; layer?.cornerRadius = 9
        focusRingType = .exterior
        dot.wantsLayer = true; dot.layer?.cornerRadius = 3.5
        dot.translatesAutoresizingMaskIntoConstraints = false
        dot.widthAnchor.constraint(equalToConstant: 7).isActive = true
        dot.heightAnchor.constraint(equalToConstant: 7).isActive = true
        let names = dashboardStack([nameLabel, statusLabel], spacing: 4)
        let counts = dashboardStack([countLabel, countCaption], spacing: 2)
        counts.alignment = .trailing
        countLabel.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        chevron.image = NSImage(systemSymbolName: "chevron.right", accessibilityDescription: nil)
        chevron.contentTintColor = .tertiaryLabelColor
        chevron.widthAnchor.constraint(equalToConstant: 9).isActive = true
        names.setContentHuggingPriority(.defaultLow, for: .horizontal)
        counts.setContentHuggingPriority(.required, for: .horizontal)
        counts.setContentCompressionResistancePriority(.required, for: .horizontal)
        [dot, names, counts, chevron].forEach { $0.translatesAutoresizingMaskIntoConstraints = false; addSubview($0) }
        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            dot.centerYAnchor.constraint(equalTo: centerYAnchor),
            names.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 10),
            names.centerYAnchor.constraint(equalTo: centerYAnchor),
            names.trailingAnchor.constraint(equalTo: counts.leadingAnchor, constant: -10),
            counts.centerYAnchor.constraint(equalTo: centerYAnchor),
            counts.trailingAnchor.constraint(equalTo: chevron.leadingAnchor, constant: -10),
            chevron.centerYAnchor.constraint(equalTo: centerYAnchor),
            chevron.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            heightAnchor.constraint(equalToConstant: 62),
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var acceptsFirstResponder: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { super.hitTest(point) == nil ? nil : self }
    override var focusRingMaskBounds: NSRect { bounds }
    override func drawFocusRingMask() { NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill() }
    override func draw(_ dirtyRect: NSRect) {
        (isHighlighted ? NSColor.controlAccentColor.withAlphaComponent(0.18) : selected
            ? NSColor.controlAccentColor.withAlphaComponent(0.12) : NSColor.clear).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill()
    }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 36 || event.keyCode == 49 { performClick(nil); return }
        super.keyDown(with: event)
    }
    @objc private func selectApp() { handler?(appID) }
    func update(_ app: DashboardApp, selected: Bool, available: Bool) {
        self.selected = selected
        nameLabel.stringValue = app.name
        let inFlight = app.activity?.inFlight ?? 0
        let activityStatus = app.status + (inFlight > 0 ? " · \(inFlight) in flight" : "")
        statusLabel.stringValue = available ? (app.busy && !app.blocked ? "Updating…" : activityStatus) : "Status unavailable"
        countLabel.stringValue = available ? dashboardNumber(app.activity?.received) : "—"
        dot.layer?.backgroundColor = (available ? dashboardStatusColor(app) : .tertiaryLabelColor).cgColor
        chevron.contentTintColor = selected ? .controlAccentColor : .tertiaryLabelColor
        setAccessibilityLabel("\(app.name), \(statusLabel.stringValue), \(countLabel.stringValue) requests")
        setAccessibilityHelp("Open app activity, details, and controls")
        needsDisplay = true
    }
}

final class MenuDashboard: NSObject, NSWindowDelegate {
    var onSelect: ((String) -> Void)?
    var onControl: ((String, String) -> Void)?
    var onCopy: ((String) -> Void)?
    var onManage: (() -> Void)?
    var onAdd: (() -> Void)?
    var onQuit: (() -> Void)?
    var onStopAll: (() -> Void)?
    var onRetry: (() -> Void)?
    var onLogin: (() -> Void)?
    var onVisibilityChange: ((Bool) -> Void)?
    private(set) var selectedID: String?
    var isVisible: Bool { menuPanel.isVisible }

    private let menuPanel = DashboardPanel(contentRect: .zero, styleMask: [.borderless], backing: .buffered, defer: false)
    private let detailsPanel = DashboardPanel(contentRect: .zero, styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
    private let hostLabel = dashboardLabel("Starting local host…", size: 11, color: .secondaryLabelColor)
    private let summaryLabel = dashboardLabel("Your local apps", size: 12, color: .secondaryLabelColor)
    private let scopeLabel = dashboardLabel("Tool requests · since host launch", size: 10, color: .tertiaryLabelColor)
    private let totalMetric = DashboardMetric("Requests")
    private let rateMetric = DashboardMetric("Last minute")
    private let activeMetric = DashboardMetric("In flight")
    private let list = dashboardStack([], spacing: 3, flipped: true)
    private let listScroll = NSScrollView()
    private let retryButton = DashboardAction("Retry connection", symbol: "arrow.clockwise")
    private let loginButton = DashboardAction("Launch at Login")
    private let stopAllButton = DashboardAction("Stop All")
    private var rows: [String: DashboardRow] = [:]
    private var rowOrder: [String] = []
    private var listSignature = ""
    private var apps: [DashboardApp] = []
    private var available = false
    private var anchor = NSRect.zero
    private var screenFrame = NSRect.zero
    private var localMonitor: Any?
    private var globalMonitor: Any?
    private let detailName = dashboardLabel(size: 23, weight: .semibold)
    private let detailStatus = dashboardLabel(size: 12, color: .secondaryLabelColor)
    private let detailSubtitle = dashboardLabel(size: 11, color: .secondaryLabelColor)
    private let detailScope = dashboardLabel("Tool requests · since host launch", size: 10, color: .tertiaryLabelColor)
    private let detailRequests = DashboardMetric("Requests", compact: true)
    private let detailFailed = DashboardMetric("Failed", compact: true)
    private let detailFlight = DashboardMetric("In flight", compact: true)
    private let detailLatency = DashboardMetric("Last latency", compact: true)
    private let detailActivity = dashboardLabel(size: 11, color: .secondaryLabelColor)
    private let detailWarning = dashboardLabel(size: 11, color: .systemOrange)
    private let primaryButton = DashboardAction("Start", symbol: "play.fill")
    private let restartButton = DashboardAction("Restart", symbol: "arrow.clockwise")
    private let copyButton = DashboardAction("Copy Connection", symbol: "doc.on.doc")
    private let detailSections = dashboardStack([], spacing: 18, flipped: true)
    private let detailScroll = NSScrollView()
    private var details: DashboardDetails?
    private var detailsLoadFailed = false
    private var selectedMissing = false
    private var copiedID: String?
    private var copyReset: DispatchWorkItem?

    override init() {
        super.init()
        configure(menuPanel)
        configure(detailsPanel)
        menuPanel.hasShadow = true
        menuPanel.backgroundColor = .clear
        menuPanel.isOpaque = false
        detailsPanel.title = "App details"
        detailsPanel.titleVisibility = .hidden
        detailsPanel.titlebarAppearsTransparent = true
        detailsPanel.delegate = self
        detailsPanel.standardWindowButton(.zoomButton)?.isHidden = true
        detailsPanel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        menuPanel.onEscape = { [weak self] in self?.dismiss() }
        detailsPanel.onEscape = { [weak self] in self?.dismiss() }
        menuPanel.onArrow = { [weak self] step in self?.moveSelection(step) }
        buildMenu()
        buildDetails()
    }

    deinit {
        if let localMonitor { NSEvent.removeMonitor(localMonitor) }
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
        copyReset?.cancel()
    }

    private func configure(_ panel: DashboardPanel) {
        panel.level = .popUpMenu
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.isMovable = false
        panel.collectionBehavior = [.transient, .moveToActiveSpace, .fullScreenAuxiliary]
        panel.animationBehavior = .utilityWindow
    }

    private func material(in panel: NSPanel, rounded: Bool = false) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .popover
        view.blendingMode = .behindWindow
        view.state = .active
        if rounded { view.wantsLayer = true; view.layer?.cornerRadius = 13; view.layer?.masksToBounds = true }
        panel.contentView = view
        return view
    }

    private func buildMenu() {
        let background = material(in: menuPanel, rounded: true)
        let title = dashboardLabel("mcpme", size: 21, weight: .semibold)
        let icon = NSImageView(image: McpmeBrand.appIcon(size: 72))
        icon.widthAnchor.constraint(equalToConstant: 38).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 38).isActive = true
        let heading = dashboardStack([icon, dashboardStack([title, summaryLabel], spacing: 2)], spacing: 10, horizontal: true)
        let metrics = dashboardStack([totalMetric, rateMetric, activeMetric], spacing: 10, horizontal: true)
        metrics.distribution = .fillEqually
        let overview = dashboardStack([metrics, scopeLabel], spacing: 9)
        dashboardFillWidth(metrics, in: overview)
        listScroll.drawsBackground = false
        listScroll.borderType = .noBorder
        listScroll.hasVerticalScroller = true
        listScroll.autohidesScrollers = true
        list.edgeInsets = NSEdgeInsets(top: 4, left: 4, bottom: 4, right: 4)
        listScroll.documentView = list
        list.widthAnchor.constraint(equalTo: listScroll.contentView.widthAnchor).isActive = true
        listScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        listScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 90).isActive = true
        hostLabel.maximumNumberOfLines = 3
        hostLabel.lineBreakMode = .byWordWrapping
        retryButton.handler = { [weak self] in self?.onRetry?() }
        retryButton.isHidden = true
        let manage = DashboardAction("Manage Apps", symbol: "slider.horizontal.3") { [weak self] in self?.dismiss(); self?.onManage?() }
        let add = DashboardAction("Add App", symbol: "plus") { [weak self] in self?.dismiss(); self?.onAdd?() }
        let actions = dashboardStack([manage, add], spacing: 8, horizontal: true)
        actions.distribution = .fillEqually
        loginButton.bezelStyle = .inline
        loginButton.controlSize = .small
        loginButton.font = .systemFont(ofSize: 11)
        loginButton.handler = { [weak self] in self?.onLogin?() }
        stopAllButton.bezelStyle = .inline; stopAllButton.controlSize = .small; stopAllButton.font = .systemFont(ofSize: 11)
        stopAllButton.handler = { [weak self] in self?.onStopAll?() }
        let quit = DashboardAction("Quit") { [weak self] in self?.dismiss(); self?.onQuit?() }
        quit.bezelStyle = .inline; quit.controlSize = .small; quit.font = .systemFont(ofSize: 11)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let footer = dashboardStack([loginButton, spacer, stopAllButton, quit], spacing: 5, horizontal: true)
        let separator1 = dashboardSeparator(), separator2 = dashboardSeparator()
        let root = dashboardStack([heading, overview, separator1, listScroll, hostLabel, retryButton, separator2, actions, footer], spacing: 13)
        background.addSubview(root)
        for view in [overview, separator1, listScroll, hostLabel, separator2, actions, footer] { dashboardFillWidth(view, in: root) }
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 18),
            root.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -18),
            root.topAnchor.constraint(equalTo: background.topAnchor, constant: 20),
            root.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -13),
        ])
    }

    private func buildDetails() {
        let background = material(in: detailsPanel)
        detailName.maximumNumberOfLines = 2
        detailName.lineBreakMode = .byWordWrapping
        detailSubtitle.maximumNumberOfLines = 2
        detailSubtitle.lineBreakMode = .byWordWrapping
        detailWarning.maximumNumberOfLines = 3
        detailWarning.lineBreakMode = .byWordWrapping
        detailActivity.maximumNumberOfLines = 2
        detailActivity.lineBreakMode = .byWordWrapping
        let hero = dashboardStack([detailName, detailStatus, detailSubtitle], spacing: 6)
        for field in [detailName, detailStatus, detailSubtitle] { dashboardFillWidth(field, in: hero) }
        let metrics = dashboardStack([detailRequests, detailFailed, detailFlight, detailLatency], spacing: 10, horizontal: true)
        metrics.distribution = .fillEqually
        let overview = dashboardStack([metrics, detailActivity, detailScope], spacing: 9)
        dashboardFillWidth(metrics, in: overview)
        dashboardFillWidth(detailActivity, in: overview)
        primaryButton.handler = { [weak self] in
            guard let self, let app = self.selectedApp, self.canControl(app) else { return }
            self.onControl?(app.id, app.running ? "stop" : "start")
        }
        restartButton.handler = { [weak self] in
            guard let self, let app = self.selectedApp, self.canControl(app), app.running else { return }
            self.onControl?(app.id, "restart")
        }
        restartButton.toolTip = "Restart this app"
        copyButton.toolTip = "Copy the MCP connection configuration for a client"
        copyButton.handler = { [weak self] in
            guard let self, let app = self.selectedApp, self.available, !app.blocked else { return }
            self.onCopy?(app.id)
        }
        let controls = dashboardStack([primaryButton, restartButton, copyButton], spacing: 7, horizontal: true)
        let line = dashboardSeparator()
        detailScroll.drawsBackground = false
        detailScroll.hasVerticalScroller = true
        detailScroll.autohidesScrollers = true
        detailScroll.documentView = detailSections
        detailSections.widthAnchor.constraint(equalTo: detailScroll.contentView.widthAnchor).isActive = true
        detailScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 70).isActive = true
        detailScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        let root = dashboardStack([hero, overview, controls, detailWarning, line, detailScroll], spacing: 18)
        background.addSubview(root)
        for view in [hero, overview, detailWarning, line, detailScroll] { dashboardFillWidth(view, in: root) }
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 24),
            root.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -24),
            root.topAnchor.constraint(equalTo: background.topAnchor, constant: 38),
            root.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -20),
        ])
    }

    func toggle(anchor: NSRect, screen: NSScreen) {
        if isVisible { dismiss(); return }
        clearSelection()
        self.anchor = anchor
        screenFrame = screen.visibleFrame
        positionPanels()
        NSApp.activate(ignoringOtherApps: true)
        menuPanel.makeKeyAndOrderFront(nil)
        menuPanel.makeFirstResponder(menuPanel)
        installMonitors()
        onVisibilityChange?(true)
    }

    func dismiss() {
        menuPanel.orderOut(nil)
        clearSelection()
        if let localMonitor { NSEvent.removeMonitor(localMonitor); self.localMonitor = nil }
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor); self.globalMonitor = nil }
        onVisibilityChange?(false)
    }

    private func clearSelection() {
        selectedID = nil; details = nil; detailsLoadFailed = false
        selectedMissing = false; copiedID = nil
        copyReset?.cancel(); copyReset = nil
        detailsPanel.orderOut(nil)
        menuPanel.makeFirstResponder(menuPanel)
        updateRows()
    }

    private func installMonitors() {
        guard localMonitor == nil else { return }
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] event in
            guard let self, self.isVisible else { return event }
            if event.type == .keyDown {
                if event.keyCode == 53 { self.dismiss(); return nil }
                return event
            }
            if event.window !== self.menuPanel && event.window !== self.detailsPanel {
                // The status button handles its own toggle; dismissing here would reopen it.
                if !self.anchor.contains(NSEvent.mouseLocation) { self.dismiss() }
            }
            return event
        }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.dismiss()
        }
    }

    func update(apps: [DashboardApp], hostMessage: String, available: Bool, loginTitle: String) {
        self.apps = apps
        self.available = available
        stopAllButton.isEnabled = available && apps.contains { $0.running && !$0.busy && !$0.blocked && !$0.transitioning }
        loginButton.title = loginTitle
        loginButton.setAccessibilityLabel(loginTitle)
        let ready = apps.filter { $0.state == "ready" }.count
        let attention = apps.filter(\.needsAttention).count
        if !available { summaryLabel.stringValue = "Host unavailable" }
        else if apps.isEmpty { summaryLabel.stringValue = "Your local apps" }
        else { summaryLabel.stringValue = "\(ready) of \(apps.count) running" + (attention > 0 ? " · \(attention) \(attention == 1 ? "needs" : "need") attention" : "") }
        hostLabel.stringValue = hostMessage.isEmpty ? (available ? "Select an app to see activity and controls." : "Reconnecting to the local host…") : hostMessage
        hostLabel.textColor = available ? .secondaryLabelColor : .systemOrange
        retryButton.isHidden = available
        let activity = apps.compactMap(\.activity)
        let complete = activity.count == apps.count && available
        totalMetric.update(complete ? dashboardNumber(dashboardTotal(activity.map(\.received))) : "—")
        rateMetric.update(complete ? dashboardNumber(dashboardTotal(activity.map(\.lastMinute))) : "—")
        activeMetric.update(complete ? dashboardNumber(dashboardTotal(activity.map(\.inFlight))) : "—")
        scopeLabel.stringValue = available ? "Tool requests · since host launch" : "Activity paused · reconnecting"
        updateRows()
        updateDetailActivity()
    }

    private func updateRows() {
        let groups: [(String, [DashboardApp])] = [
            ("NEEDS ATTENTION", apps.filter(\.needsAttention)),
            ("RUNNING", apps.filter { !$0.needsAttention && $0.running }),
            ("STOPPED", apps.filter { !$0.needsAttention && !$0.running }),
        ].map { ($0.0, $0.1.sorted { first, second in
            let result = first.name.localizedCaseInsensitiveCompare(second.name)
            return result == .orderedSame ? first.id < second.id : result == .orderedAscending
        }) }
        let signature = groups.map { $0.0 + ":" + $0.1.map(\.id).joined(separator: ",") }.joined(separator: ";")
            + (apps.isEmpty && available ? "empty" : "")
        if signature != listSignature {
            let scrollOrigin = listScroll.contentView.bounds.origin
            let focusedID = (menuPanel.firstResponder as? DashboardRow)?.appID
            let oldRows = rows
            list.arrangedSubviews.forEach { list.removeArrangedSubview($0); $0.removeFromSuperview() }
            rows = [:]; rowOrder = []
            for (title, members) in groups where !members.isEmpty {
                let heading = dashboardLabel(title, size: 10, weight: .semibold, color: .secondaryLabelColor)
                let padded = dashboardStack([heading], spacing: 0)
                padded.edgeInsets = NSEdgeInsets(top: 7, left: 10, bottom: 3, right: 0)
                list.addArrangedSubview(padded)
                dashboardFillWidth(padded, in: list)
                for app in members {
                    let row = oldRows[app.id] ?? DashboardRow(id: app.id)
                    row.handler = { [weak self] id in self?.onSelect?(id) }
                    rows[app.id] = row; rowOrder.append(app.id)
                    list.addArrangedSubview(row)
                    dashboardFillWidth(row, in: list)
                }
            }
            if apps.isEmpty {
                let title = dashboardLabel(available ? "Make room for your first app" : "Connecting to your apps", size: 14, weight: .semibold)
                let body = dashboardLabel(available ? "Add an MCP app to see its status, incoming requests, and connection details here." : "Your apps will appear when the local host is available.", size: 12, color: .secondaryLabelColor)
                body.lineBreakMode = .byWordWrapping; body.maximumNumberOfLines = 5
                let empty = dashboardStack([title, body], spacing: 9)
                empty.edgeInsets = NSEdgeInsets(top: 22, left: 8, bottom: 16, right: 8)
                list.addArrangedSubview(empty); dashboardFillWidth(empty, in: list)
                dashboardFillWidth(body, in: empty)
            }
            listSignature = signature
            list.layoutSubtreeIfNeeded()
            listScroll.contentView.scroll(to: scrollOrigin)
            listScroll.reflectScrolledClipView(listScroll.contentView)
            if let focusedID, let row = rows[focusedID] { menuPanel.makeFirstResponder(row) }
        }
        for app in apps { rows[app.id]?.update(app, selected: app.id == selectedID, available: available) }
    }

    func showDetails(id: String) {
        guard let row = rows[id] else { return }
        let changed = selectedID != id
        selectedID = id
        if changed {
            details = nil; detailsLoadFailed = false; copiedID = nil; selectedMissing = false
            detailScroll.contentView.scroll(to: .zero)
            renderSections()
        }
        updateRows()
        menuPanel.makeFirstResponder(row)
        updateDetailActivity()
        positionPanels()
        if isVisible {
            // Keep keyboard navigation in the app list until the user clicks a detail control.
            detailsPanel.orderFront(nil)
        }
    }

    func updateDetails(_ details: DashboardDetails) {
        guard details.appID == selectedID else { return }
        guard self.details != details || detailsLoadFailed else { return }
        self.details = details
        detailsLoadFailed = false
        detailSubtitle.stringValue = details.subtitle
        renderSections()
    }

    func detailsFailed(id: String) {
        guard selectedID == id else { return }
        detailsLoadFailed = true
        renderSections()
    }

    func connectionCopied(id: String) {
        guard id == selectedID else { return }
        copiedID = id
        copyButton.title = "Copied"
        copyButton.image = NSImage(systemSymbolName: "checkmark", accessibilityDescription: nil)
        copyButton.setAccessibilityLabel("Connection copied")
        copyReset?.cancel()
        let reset = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.copiedID = nil
            self.updateDetailActivity()
        }
        copyReset = reset
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: reset)
    }

    private var selectedApp: DashboardApp? { apps.first { $0.id == selectedID } }
    private func canControl(_ app: DashboardApp) -> Bool { available && !app.busy && !app.blocked && !app.transitioning }

    private func updateDetailActivity() {
        guard selectedID != nil else { return }
        guard let app = selectedApp else {
            if !selectedMissing { selectedMissing = true; details = nil; renderSections() }
            detailName.stringValue = "App unavailable"
            detailStatus.stringValue = "This app is no longer in the host's app list."
            detailSubtitle.stringValue = ""
            detailWarning.stringValue = "Open Manage Apps to review your installed apps."
            detailWarning.isHidden = false
            detailActivity.stringValue = "Activity unavailable"
            [detailRequests, detailFailed, detailFlight, detailLatency].forEach { $0.update("—") }
            [primaryButton, restartButton, copyButton].forEach { $0.isEnabled = false }
            return
        }
        if selectedMissing { selectedMissing = false; renderSections() }
        detailName.stringValue = app.name
        detailsPanel.title = app.name
        detailStatus.stringValue = (available ? (app.busy && !app.blocked ? "Updating…" : app.status) : "Status unavailable") + " · " + app.policy
        detailStatus.textColor = available ? dashboardStatusColor(app) : .secondaryLabelColor
        detailSubtitle.stringValue = details?.subtitle ?? "App details"
        detailRequests.update(available ? dashboardNumber(app.activity?.received) : "—")
        detailFailed.update(available ? dashboardNumber(app.activity?.failed) : "—")
        detailFailed.value.textColor = available && (app.activity?.failed ?? 0) > 0 ? .systemOrange : .labelColor
        detailFlight.update(available ? dashboardNumber(app.activity?.inFlight) : "—")
        detailFlight.value.textColor = available && (app.activity?.inFlight ?? 0) > 0 ? .controlAccentColor : .labelColor
        if available, let latency = app.activity?.lastLatencyMS {
            detailLatency.update(latency >= 1000 ? String(format: "%.1f s", latency / 1000) : String(format: "%.0f ms", latency))
        } else { detailLatency.update("—") }
        if let activity = app.activity, available {
            detailActivity.stringValue = "\(dashboardNumber(activity.lastMinute)) in the last minute · " + lastActivity(activity.lastRequestAt)
            detailScope.stringValue = "Tool requests · " + (activity.startedAt.map { "since " + Date(timeIntervalSince1970: $0).formatted(date: .omitted, time: .shortened) } ?? "since host launch")
        } else {
            detailActivity.stringValue = available ? "Request activity is not available from this host." : "Activity will refresh when the host reconnects."
            detailScope.stringValue = "Tool requests · since host launch"
        }
        primaryButton.title = app.running ? "Stop" : "Start"
        primaryButton.image = NSImage(systemSymbolName: app.running ? "stop.fill" : "play.fill", accessibilityDescription: nil)
        primaryButton.setAccessibilityLabel(primaryButton.title + " " + app.name)
        primaryButton.toolTip = primaryButton.title + " " + app.name
        primaryButton.isEnabled = canControl(app)
        restartButton.isEnabled = canControl(app) && app.running
        copyButton.isEnabled = available && !app.blocked
        if copiedID != app.id {
            copyButton.title = "Copy Connection"
            copyButton.image = NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: nil)
            copyButton.setAccessibilityLabel("Copy connection for " + app.name)
        }
        if !available { detailWarning.stringValue = "The local host is unavailable. Controls will return after reconnecting." }
        else if app.error == "removal_recovery_required" { detailWarning.stringValue = "Removal needs recovery. Open Manage Apps to review this app before making further changes." }
        else if app.blocked { detailWarning.stringValue = "This app needs recovery before it can run. Open Manage Apps to review it." }
        else if app.error == "readiness_timeout" { detailWarning.stringValue = "The app did not become ready in time. Try Start again." }
        else if let error = app.error { detailWarning.stringValue = "\(error.replacingOccurrences(of: "_", with: " ").capitalized). Review the app in Manage Apps." }
        else { detailWarning.stringValue = "" }
        detailWarning.isHidden = detailWarning.stringValue.isEmpty
    }

    private func lastActivity(_ epoch: Double?) -> String {
        guard let epoch else { return "No requests yet" }
        let seconds = max(0, Int(Date().timeIntervalSince1970 - epoch))
        if seconds < 5 { return "Last request just now" }
        if seconds < 60 { return "Last request \(seconds)s ago" }
        if seconds < 3600 { return "Last request \(seconds / 60)m ago" }
        if seconds < 86400 { return "Last request \(seconds / 3600)h ago" }
        return "Last request " + Date(timeIntervalSince1970: epoch).formatted(date: .abbreviated, time: .shortened)
    }

    private func renderSections() {
        let origin = detailScroll.contentView.bounds.origin
        detailSections.arrangedSubviews.forEach { detailSections.removeArrangedSubview($0); $0.removeFromSuperview() }
        if selectedMissing {
            appendSection(title: "App no longer available", body: "Select another app, or open Manage Apps to review your installed apps.")
        } else if detailsLoadFailed {
            appendSection(title: "Details unavailable", body: "The host could not load this app's details. Select the app again to retry.")
        } else if let details {
            for section in details.sections { appendSection(title: section.title, body: section.body) }
        } else {
            appendSection(title: "Loading app details…", body: "Tools, connection, and package information will appear here.")
        }
        detailSections.layoutSubtreeIfNeeded()
        detailScroll.contentView.scroll(to: origin)
        detailScroll.reflectScrolledClipView(detailScroll.contentView)
    }

    private func appendSection(title: String, body: String) {
        let heading = dashboardLabel(title, size: 12, weight: .semibold)
        let text = dashboardLabel(body, size: 12, color: .secondaryLabelColor)
        text.isSelectable = true
        text.lineBreakMode = .byWordWrapping
        text.maximumNumberOfLines = 0
        text.setContentCompressionResistancePriority(.required, for: .vertical)
        let section = dashboardStack([heading, text], spacing: 7)
        section.edgeInsets = NSEdgeInsets(top: 0, left: 0, bottom: 2, right: 12)
        detailSections.addArrangedSubview(section)
        dashboardFillWidth(section, in: detailSections)
        dashboardFillWidth(text, in: section)
    }

    private func positionPanels() {
        guard !screenFrame.isEmpty else { return }
        let frames = dashboardFrames(anchor: anchor, screen: screenFrame, showsDetails: selectedID != nil)
        menuPanel.setFrame(frames.menu, display: true)
        if let frame = frames.details { detailsPanel.setFrame(frame, display: true) }
    }

    private func moveSelection(_ step: Int) {
        guard !rowOrder.isEmpty else { return }
        let focused = (menuPanel.firstResponder as? DashboardRow)?.appID ?? selectedID
        let current = focused.flatMap { rowOrder.firstIndex(of: $0) } ?? (step > 0 ? -1 : rowOrder.count)
        let index = min(max(current + step, 0), rowOrder.count - 1)
        let id = rowOrder[index]
        if let row = rows[id] {
            menuPanel.makeFirstResponder(row)
            let target = row.convert(row.bounds.insetBy(dx: -3, dy: -3), to: list)
            list.scrollToVisible(target)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === detailsPanel {
            clearSelection()
            positionPanels()
            menuPanel.makeKeyAndOrderFront(nil)
            menuPanel.makeFirstResponder(menuPanel)
            return false
        }
        return true
    }
}

/// Exercises menu selection, presentation, and keyboard routing without contacting a host.
func dashboardViewSelfTest() -> Bool {
    for width in [CGFloat(300), CGFloat(360)] {
        let screen = NSRect(x: -360, y: 200, width: width, height: 700)
        let anchor = NSRect(x: screen.maxX - 24, y: screen.maxY, width: 24, height: 24)
        let frames = dashboardFrames(anchor: anchor, screen: screen, showsDetails: true)
        guard let details = frames.details,
              screen.contains(frames.menu), screen.contains(details),
              details.width >= min(300, screen.width - 16),
              frames.menu == dashboardFrames(anchor: anchor, screen: screen, showsDetails: false).menu else { return false }
    }
    return MenuDashboard.verifySelectionAndFocus()
}

extension MenuDashboard {
    fileprivate static func verifySelectionAndFocus() -> Bool {
        let dashboard = MenuDashboard()
        var visibilityChanges: [Bool] = []
        dashboard.onVisibilityChange = { visibilityChanges.append($0) }
        func app(_ id: String, state: String = "stopped") -> DashboardApp {
            DashboardApp(id: id, name: id.uppercased(), status: state, state: state,
                         policy: "Manual", activity: nil, error: nil, busy: false)
        }
        func update(_ apps: [DashboardApp]) {
            dashboard.update(apps: apps, hostMessage: "", available: true, loginTitle: "Start at Login: Off")
        }
        func isSelected(_ id: String) -> Bool {
            dashboard.selectedID == id &&
                (dashboard.menuPanel.firstResponder as? DashboardRow)?.appID == id &&
                dashboard.rows.values.filter(\.selected).map(\.appID) == [id]
        }
        func isClear() -> Bool {
            dashboard.selectedID == nil && dashboard.details == nil && !dashboard.detailsPanel.isVisible &&
                !(dashboard.menuPanel.firstResponder is DashboardRow) &&
                !dashboard.rows.values.contains(where: \.selected)
        }
        dashboard.onSelect = { [weak dashboard] id in dashboard?.showDetails(id: id) }
        update([app("a"), app("b")])
        guard isClear() else { return false }
        dashboard.moveSelection(1)
        guard let first = dashboard.menuPanel.firstResponder as? DashboardRow,
              first.appID == "a", dashboard.selectedID == nil else { return false }
        first.performClick(nil)
        guard isSelected("a") else { return false }
        dashboard.rows["b"]?.performClick(nil)
        guard isSelected("b") else { return false }
        let details = DashboardDetails(appID: "b", subtitle: "B", sections: [])
        dashboard.updateDetails(details)
        dashboard.updateDetails(DashboardDetails(appID: "a", subtitle: "Late response", sections: []))
        guard dashboard.details == details else { return false }
        update([app("b", state: "ready"), app("a")])
        guard isSelected("b") else { return false }
        dashboard.moveSelection(1)
        guard let next = dashboard.menuPanel.firstResponder as? DashboardRow, next.appID == "a" else { return false }
        next.performClick(nil)
        guard isSelected("a") else { return false }
        dashboard.dismiss()
        guard isClear() else { return false }
        dashboard.updateDetails(details)
        dashboard.detailsFailed(id: "a")
        update([app("a"), app("b")])
        guard isClear(), !dashboard.detailsLoadFailed else { return false }
        dashboard.showDetails(id: "missing")
        guard isClear(), let screen = NSScreen.main else { return false }
        let anchor = NSRect(x: screen.visibleFrame.midX, y: screen.visibleFrame.maxY, width: 24, height: 24)
        defer { dashboard.dismiss() }
        visibilityChanges.removeAll()
        dashboard.toggle(anchor: anchor, screen: screen)
        let openedMenuFrame = dashboard.menuPanel.frame
        // CLI/CI WindowServer sessions may decline application activation.
        // When active, opening details must leave the menu as the key window.
        func menuKeepsFocus() -> Bool { !NSApp.isActive || dashboard.menuPanel.isKeyWindow }
        guard isClear(), dashboard.isVisible, menuKeepsFocus(), visibilityChanges == [true] else { return false }
        dashboard.rows["a"]?.performClick(nil)
        guard isSelected("a"), dashboard.detailsPanel.isVisible,
              menuKeepsFocus(), dashboard.menuPanel.frame == openedMenuFrame,
              visibilityChanges == [true] else { return false }
        guard let down = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                                         windowNumber: dashboard.menuPanel.windowNumber, context: nil,
                                         characters: "\u{F701}", charactersIgnoringModifiers: "\u{F701}",
                                         isARepeat: false, keyCode: 125) else { return false }
        dashboard.menuPanel.sendEvent(down)
        guard (dashboard.menuPanel.firstResponder as? DashboardRow)?.appID == "b" else { return false }
        dashboard.rows["b"]?.performClick(nil)
        guard isSelected("b"), menuKeepsFocus() else { return false }
        dashboard.detailsPanel.performClose(nil)
        guard isClear(), dashboard.isVisible, dashboard.menuPanel.frame == openedMenuFrame,
              visibilityChanges == [true] else { return false }
        dashboard.showDetails(id: "a")
        dashboard.toggle(anchor: anchor, screen: screen)
        guard isClear(), !dashboard.isVisible, visibilityChanges == [true, false] else { return false }
        dashboard.toggle(anchor: anchor, screen: screen)
        return isClear() && dashboard.isVisible && menuKeepsFocus() && visibilityChanges == [true, false, true]
    }
}
