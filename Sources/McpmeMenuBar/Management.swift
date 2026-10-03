import AppKit

struct ManagementOperation: Equatable {
    let appID: String?
    let title: String
    let status: String
    let action: String
}

enum ManagementFilter: Int {
    case all, running, attention
}

/// A deterministic list keeps selection attached to an app as status groups change.
func managementVisibleApps(_ apps: [DashboardApp], query: String, filter: ManagementFilter) -> [DashboardApp] {
    let search = query.trimmingCharacters(in: .whitespacesAndNewlines)
    return apps.filter { app in
        let matchesQuery = search.isEmpty || app.name.localizedStandardContains(search)
        let matchesFilter = filter == .all || (filter == .running && app.running) || (filter == .attention && app.needsAttention)
        return matchesQuery && matchesFilter
    }.sorted { first, second in
        let firstGroup = first.needsAttention ? 0 : first.running ? 1 : 2
        let secondGroup = second.needsAttention ? 0 : second.running ? 1 : 2
        if firstGroup != secondGroup { return firstGroup < secondGroup }
        let nameOrder = first.name.localizedCaseInsensitiveCompare(second.name)
        return nameOrder == .orderedSame ? first.id < second.id : nameOrder == .orderedAscending
    }
}

func managementCanControl(_ app: DashboardApp, available: Bool) -> Bool {
    available && !app.busy && !app.blocked && !app.transitioning
}

func managementSelfTest() -> Bool {
    func app(_ id: String, _ name: String, _ state: String, error: String? = nil, busy: Bool = false) -> DashboardApp {
        DashboardApp(id: id, name: name, status: state, state: state, policy: "Manual", activity: nil, error: error, busy: busy)
    }
    let apps = [app("z", "Zulu", "ready"), app("b", "Beta", "stopped"), app("a2", "Alpha", "ready"),
                app("a1", "alpha", "ready"), app("f", "Failed app", "failed")]
    guard managementVisibleApps(apps, query: "", filter: .all).map(\.id) == ["f", "a1", "a2", "z", "b"],
          managementVisibleApps(apps, query: "  ALPHA  ", filter: .all).map(\.id) == ["a1", "a2"],
          managementVisibleApps(apps, query: "", filter: .running).map(\.id) == ["a1", "a2", "z"],
          managementVisibleApps(apps, query: "", filter: .attention).map(\.id) == ["f"],
          managementVisibleApps(apps, query: "missing", filter: .all).isEmpty else { return false }
    return managementCanControl(app("s", "Stopped", "stopped"), available: true)
        && !managementCanControl(app("s", "Stopped", "stopped"), available: false)
        && !managementCanControl(app("s", "Starting", "starting"), available: true)
        && !managementCanControl(app("s", "Updating", "updating"), available: true)
        && !managementCanControl(app("s", "Busy", "ready", busy: true), available: true)
        && !managementCanControl(app("s", "Blocked", "failed", error: "removal_recovery_required"), available: true)
        && !managementCanControl(app("s", "Blocked", "stopped", error: "installation_recovery_required"), available: true)
        && !managementCanControl(app("s", "Blocked", "stopped", error: "upgrade_recovery_required"), available: true)
}

private func managementLabel(_ text: String = "", size: CGFloat = 13, weight: NSFont.Weight = .regular,
                             color: NSColor = .labelColor, wrapping: Bool = false) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.font = .systemFont(ofSize: size, weight: weight)
    field.textColor = color
    field.lineBreakMode = wrapping ? .byWordWrapping : .byTruncatingTail
    field.maximumNumberOfLines = wrapping ? 0 : 1
    field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    field.setContentCompressionResistancePriority(.required, for: .vertical)
    return field
}

private final class ManagementDocument: NSStackView {
    override var isFlipped: Bool { true }
}

private func managementStack(_ views: [NSView], spacing: CGFloat = 8, horizontal: Bool = false,
                             document: Bool = false) -> NSStackView {
    let stack = document ? ManagementDocument(views: views) : NSStackView(views: views)
    stack.orientation = horizontal ? .horizontal : .vertical
    stack.alignment = horizontal ? .centerY : .leading
    stack.distribution = .fill
    stack.spacing = spacing
    stack.translatesAutoresizingMaskIntoConstraints = false
    return stack
}

private func managementFill(_ view: NSView, in parent: NSView) {
    view.translatesAutoresizingMaskIntoConstraints = false
    let inset = (parent as? NSStackView)?.edgeInsets ?? NSEdgeInsetsZero
    NSLayoutConstraint.activate([
        view.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset.left),
        view.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset.right),
    ])
}

private func managementSeparator() -> NSBox {
    let line = NSBox(); line.boxType = .separator; return line
}

private func managementNumber(_ value: Int?) -> String { value?.formatted(.number) ?? "—" }

private func managementStatusColor(_ app: DashboardApp) -> NSColor {
    if app.needsAttention { return .systemOrange }
    if app.state == "ready" { return .systemGreen }
    return app.running ? .systemBlue : .secondaryLabelColor
}

private final class ManagementAction: NSButton {
    var handler: (() -> Void)?
    init(_ title: String, symbol: String? = nil) {
        super.init(frame: .zero)
        self.title = title
        bezelStyle = .rounded
        font = .systemFont(ofSize: 12, weight: .medium)
        target = self; action = #selector(invoke)
        if let symbol { image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil); imagePosition = .imageLeading }
        setAccessibilityLabel(title)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func invoke() { handler?() }
}

private final class ManagementMetric: NSView {
    private let value = managementLabel("—", size: 20, weight: .semibold)
    private let title: String
    init(_ title: String) {
        self.title = title
        super.init(frame: .zero)
        value.font = .monospacedDigitSystemFont(ofSize: 20, weight: .semibold)
        let caption = managementLabel(title, size: 10, color: .secondaryLabelColor)
        let stack = managementStack([value, caption], spacing: 4)
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor), stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor), stack.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        setAccessibilityElement(true); setAccessibilityRole(.staticText); setAccessibilityLabel(title)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(_ text: String, color: NSColor = .labelColor) {
        value.stringValue = text; value.textColor = color; setAccessibilityValue(text)
    }
}

private final class ManagementRow: NSButton {
    let appID: String
    var onSelect: ((String) -> Void)?
    var onMove: ((Int) -> Void)?
    private let nameLabel = managementLabel(size: 13, weight: .semibold)
    private let stateLabel = managementLabel(size: 11, color: .secondaryLabelColor)
    private let countLabel = managementLabel(size: 12, weight: .medium, color: .secondaryLabelColor)
    private let dot = NSView()
    private var selected = false

    init(id: String) {
        appID = id
        super.init(frame: .zero)
        title = ""; isBordered = false; target = self; action = #selector(choose)
        focusRingType = .exterior
        dot.wantsLayer = true; dot.layer?.cornerRadius = 3
        let names = managementStack([nameLabel, stateLabel], spacing: 5)
        let counts = managementStack([countLabel, managementLabel("requests", size: 9, color: .tertiaryLabelColor)], spacing: 3)
        counts.alignment = .trailing
        countLabel.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        counts.setContentCompressionResistancePriority(.required, for: .horizontal)
        counts.setContentHuggingPriority(.required, for: .horizontal)
        [dot, names, counts].forEach { $0.translatesAutoresizingMaskIntoConstraints = false; addSubview($0) }
        NSLayoutConstraint.activate([
            heightAnchor.constraint(equalToConstant: 64),
            dot.widthAnchor.constraint(equalToConstant: 6), dot.heightAnchor.constraint(equalToConstant: 6),
            dot.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10), dot.centerYAnchor.constraint(equalTo: centerYAnchor),
            names.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 9), names.centerYAnchor.constraint(equalTo: centerYAnchor),
            names.trailingAnchor.constraint(equalTo: counts.leadingAnchor, constant: -8),
            counts.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -12), counts.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var acceptsFirstResponder: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { super.hitTest(point) == nil ? nil : self }
    override var focusRingMaskBounds: NSRect { bounds }
    override func drawFocusRingMask() { NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill() }
    override func draw(_ dirtyRect: NSRect) {
        (isHighlighted ? NSColor.controlAccentColor.withAlphaComponent(0.2) : selected
            ? NSColor.controlAccentColor.withAlphaComponent(0.13) : NSColor.clear).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 9, yRadius: 9).fill()
    }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 125 { onMove?(1); return }
        if event.keyCode == 126 { onMove?(-1); return }
        if event.keyCode == 36 || event.keyCode == 49 { performClick(nil); return }
        super.keyDown(with: event)
    }
    @objc private func choose() { window?.makeFirstResponder(self); onSelect?(appID) }
    func update(_ app: DashboardApp, selected: Bool, available: Bool) {
        self.selected = selected
        nameLabel.stringValue = app.name
        let flight = app.activity?.inFlight ?? 0
        stateLabel.stringValue = !available ? "Status unavailable" : app.busy && !app.blocked ? "Updating…"
            : app.status + (flight > 0 ? " · \(flight) in flight" : "")
        countLabel.stringValue = available ? managementNumber(app.activity?.received) : "—"
        dot.layer?.backgroundColor = (available ? managementStatusColor(app) : .tertiaryLabelColor).cgColor
        setAccessibilityLabel("\(app.name), \(stateLabel.stringValue), \(countLabel.stringValue) requests")
        setAccessibilityValue(selected ? "Selected" : "")
        setAccessibilityHelp("Select to manage this app")
        toolTip = app.name
        needsDisplay = true
    }
}

/// The installed-app workspace owns presentation only. Every mutation is delegated
/// to the host controller, which retains transport checks and confirmation sheets.
final class ManagementDashboard: NSView, NSSearchFieldDelegate {
    var onSelect: ((String) -> Void)?
    var onControl: ((String, String) -> Void)?
    var onCopy: ((String) -> Void)?
    var onPolicy: ((String, String) -> Void)?
    var onRemove: ((String) -> Void)?
    var onUpgrade: ((String) -> Void)?
    var onConfigure: ((String) -> Void)?
    var onOperation: ((String) -> Void)?
    var onAdd: (() -> Void)?
    var onRetry: (() -> Void)?
    private(set) var selectedID: String?

    private var apps: [DashboardApp] = []
    private var available = false
    private var operations: [ManagementOperation] = []
    private var hasChosenInitialApp = false
    private var filter: ManagementFilter = .all
    private let search = NSSearchField()
    private let filters = NSSegmentedControl(labels: ["All", "Running", "Attention"], trackingMode: .selectOne, target: nil, action: nil)
    private let summary = managementLabel("Your local apps", size: 12, color: .secondaryLabelColor)
    private let hostLabel = managementLabel("Connecting to local host…", size: 11, color: .secondaryLabelColor, wrapping: true)
    private let retryButton = ManagementAction("Retry Connection", symbol: "arrow.clockwise")
    private let addButton = ManagementAction("Add App", symbol: "plus")
    private let list = managementStack([], spacing: 3, document: true)
    private let listScroll = NSScrollView()
    private var rows: [String: ManagementRow] = [:]
    private var visibleIDs: [String] = []
    private var listSignature: [String] = []

    private let inspectorScroll = NSScrollView()
    private let inspector = managementStack([], spacing: 22, document: true)
    private let emptyInspector = managementStack([], spacing: 10)
    private let emptyTitle = managementLabel("Select an app", size: 22, weight: .semibold)
    private let emptyBody = managementLabel(size: 13, color: .secondaryLabelColor, wrapping: true)
    private let nameLabel = managementLabel(size: 26, weight: .semibold, wrapping: true)
    private let statusLabel = managementLabel(size: 12, weight: .medium)
    private let subtitleLabel = managementLabel(size: 12, color: .secondaryLabelColor, wrapping: true)
    private let warning = managementLabel(size: 12, color: .systemOrange, wrapping: true)
    private let primary = ManagementAction("Start", symbol: "play.fill")
    private let restart = ManagementAction("Restart", symbol: "arrow.clockwise")
    private let copy = ManagementAction("Copy Connection", symbol: "doc.on.doc")
    private let requestMetric = ManagementMetric("Requests")
    private let minuteMetric = ManagementMetric("Last minute")
    private let flightMetric = ManagementMetric("In flight")
    private let failureMetric = ManagementMetric("Failed")
    private let latencyMetric = ManagementMetric("Last latency")
    private let requestTime = managementLabel(size: 11, color: .secondaryLabelColor, wrapping: true)
    private let requestScope = managementLabel("Tool requests · since host launch", size: 10, color: .tertiaryLabelColor, wrapping: true)
    private let policy = NSPopUpButton()
    private let policyHint = managementLabel(size: 11, color: .secondaryLabelColor, wrapping: true)
    private let sections = managementStack([], spacing: 21)
    private var sectionTitles: [String] = []
    private var sectionBodies: [NSTextField] = []
    private let detailRetry = ManagementAction("Retry Details", symbol: "arrow.clockwise")
    private let operationSection = managementStack([], spacing: 8)
    private let operationBody = managementLabel(size: 12, color: .secondaryLabelColor, wrapping: true)
    private let remove = ManagementAction("Remove App…")
    private let upgrade = ManagementAction("Upgrade App…")
    private let configure = ManagementAction("Edit Configuration…")
    private let operationDetails = ManagementAction("View Operation…")
    private var details: DashboardDetails?
    private var loadFailed = false
    private var copiedID: String?
    private var copyReset: DispatchWorkItem?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        build()
        updateInspector()
    }
    convenience init() { self.init(frame: .zero) }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit { copyReset?.cancel() }

    private func build() {
        let sidebar = NSVisualEffectView()
        sidebar.material = .sidebar; sidebar.blendingMode = .withinWindow; sidebar.state = .followsWindowActiveState
        let sidebarTitle = managementLabel("Installed Apps", size: 20, weight: .semibold)
        let heading = managementStack([sidebarTitle, summary], spacing: 4)
        search.placeholderString = "Search apps"
        search.setAccessibilityLabel("Search installed apps")
        search.delegate = self
        search.sendsSearchStringImmediately = true
        filters.selectedSegment = 0; filters.segmentStyle = .rounded
        filters.target = self; filters.action = #selector(filterChanged)
        filters.setAccessibilityLabel("Filter installed apps")
        for segment in 0..<3 { filters.setWidth(78, forSegment: segment) }
        configureScroll(listScroll, document: list)
        list.edgeInsets = NSEdgeInsets(top: 0, left: 2, bottom: 4, right: 2)
        listScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 90).isActive = true
        retryButton.handler = { [weak self] in self?.onRetry?() }
        retryButton.isHidden = true
        addButton.handler = { [weak self] in self?.onAdd?() }
        addButton.toolTip = "Add a local app or continue a saved setup"
        let footer = managementStack([hostLabel, retryButton, managementSeparator(), addButton], spacing: 10)
        for view in footer.arrangedSubviews { managementFill(view, in: footer) }
        let sidebarRoot = managementStack([heading, search, filters, listScroll, footer], spacing: 15)
        sidebar.addSubview(sidebarRoot)
        for view in sidebarRoot.arrangedSubviews { managementFill(view, in: sidebarRoot) }
        NSLayoutConstraint.activate([
            sidebarRoot.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 18),
            sidebarRoot.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -18),
            sidebarRoot.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 24),
            sidebarRoot.bottomAnchor.constraint(equalTo: sidebar.bottomAnchor, constant: -20),
            sidebar.widthAnchor.constraint(equalToConstant: 292),
        ])

        let inspectorBackground = NSView()
        configureScroll(inspectorScroll, document: inspector)
        inspector.edgeInsets = NSEdgeInsets(top: 26, left: 30, bottom: 30, right: 30)
        let hero = managementStack([nameLabel, statusLabel, subtitleLabel], spacing: 6)
        for view in hero.arrangedSubviews { managementFill(view, in: hero) }
        let controls = managementStack([primary, restart, copy], spacing: 8, horizontal: true)
        primary.handler = { [weak self] in
            guard let self, let app = self.selectedApp, managementCanControl(app, available: self.available) else { return }
            self.onControl?(app.id, app.running ? "stop" : "start")
        }
        restart.handler = { [weak self] in
            guard let self, let app = self.selectedApp, app.running, managementCanControl(app, available: self.available) else { return }
            self.onControl?(app.id, "restart")
        }
        copy.handler = { [weak self] in
            guard let self, let app = self.selectedApp, self.available, !app.blocked else { return }
            self.onCopy?(app.id)
        }
        copy.toolTip = "Copy the MCP connection configuration for your assistant"
        let metrics = managementStack([requestMetric, minuteMetric, flightMetric, failureMetric, latencyMetric], spacing: 10, horizontal: true)
        metrics.distribution = .fillEqually
        let activity = managementStack([metrics, requestTime, requestScope], spacing: 9)
        for view in activity.arrangedSubviews { managementFill(view, in: activity) }
        let activityCard = card(activity)

        policy.addItems(withTitles: ["Manual", "Automatic", "On first call"])
        policy.menu?.item(at: 2)?.isEnabled = false
        policy.autoenablesItems = false
        policy.target = self; policy.action = #selector(policyChanged)
        policy.setAccessibilityLabel("App startup")
        policy.widthAnchor.constraint(equalToConstant: 150).isActive = true
        let policySpacer = NSView(); policySpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let policyHeader = managementStack([managementLabel("Startup", size: 13, weight: .semibold), policySpacer, policy], horizontal: true)
        let startup = managementStack([policyHeader, policyHint], spacing: 7)
        for view in startup.arrangedSubviews { managementFill(view, in: startup) }
        let startupCard = card(startup)

        detailRetry.handler = { [weak self] in
            guard let self, let id = self.selectedID, self.available else { return }
            self.loadFailed = false; self.renderSections(); self.onSelect?(id)
        }
        operationSection.addArrangedSubview(managementLabel("Recent Activity", size: 13, weight: .semibold))
        operationSection.addArrangedSubview(operationBody)
        managementFill(operationBody, in: operationSection)
        operationSection.isHidden = true
        let removalTitle = managementLabel("Remove this app", size: 12, weight: .medium, color: .secondaryLabelColor)
        let removalHint = managementLabel("Disconnects the app from mcpme. Source files and saved app data are kept.", size: 11, color: .secondaryLabelColor, wrapping: true)
        let removalText = managementStack([removalTitle, removalHint], spacing: 5)
        managementFill(removalHint, in: removalText)
        let removal = managementStack([removalText, remove], spacing: 12)
        managementFill(removalText, in: removal)
        remove.handler = { [weak self] in
            guard let self, let app = self.selectedApp, managementCanControl(app, available: self.available) else { return }
            self.onRemove?(app.id)
        }
        upgrade.handler = { [weak self] in
            guard let self, let app = self.selectedApp, managementCanControl(app, available: self.available) else { return }
            self.onUpgrade?(app.id)
        }
        configure.handler = { [weak self] in
            guard let self, let app = self.selectedApp, managementCanControl(app, available: self.available) else { return }
            self.onConfigure?(app.id)
        }
        operationDetails.handler = { [weak self] in
            guard let self, let app = self.selectedApp, self.available, self.operations.contains(where: { $0.appID == app.id && ["install", "upgrade"].contains($0.action) }) else { return }
            self.onOperation?(app.id)
        }
        let updates = managementStack([configure, upgrade, operationDetails], spacing: 8)
        for view in [hero, controls, warning, activityCard, startupCard, sections, detailRetry, operationSection, updates, managementSeparator(), removal] {
            inspector.addArrangedSubview(view)
            if view !== controls && view !== detailRetry { managementFill(view, in: inspector) }
        }
        let emptyIcon = NSImageView(image: NSImage(systemSymbolName: "square.stack.3d.up", accessibilityDescription: nil) ?? NSImage())
        emptyIcon.contentTintColor = .tertiaryLabelColor
        emptyIcon.widthAnchor.constraint(equalToConstant: 42).isActive = true
        emptyIcon.heightAnchor.constraint(equalToConstant: 42).isActive = true
        emptyBody.alignment = .center
        emptyInspector.alignment = .centerX
        [emptyIcon, emptyTitle, emptyBody].forEach { emptyInspector.addArrangedSubview($0) }
        emptyInspector.widthAnchor.constraint(lessThanOrEqualToConstant: 360).isActive = true
        inspectorBackground.addSubview(inspectorScroll); inspectorBackground.addSubview(emptyInspector)
        inspectorScroll.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            inspectorScroll.leadingAnchor.constraint(equalTo: inspectorBackground.leadingAnchor),
            inspectorScroll.trailingAnchor.constraint(equalTo: inspectorBackground.trailingAnchor),
            inspectorScroll.topAnchor.constraint(equalTo: inspectorBackground.topAnchor),
            inspectorScroll.bottomAnchor.constraint(equalTo: inspectorBackground.bottomAnchor),
            emptyInspector.centerXAnchor.constraint(equalTo: inspectorBackground.centerXAnchor),
            emptyInspector.centerYAnchor.constraint(equalTo: inspectorBackground.centerYAnchor),
            emptyInspector.leadingAnchor.constraint(greaterThanOrEqualTo: inspectorBackground.leadingAnchor, constant: 36),
            emptyInspector.trailingAnchor.constraint(lessThanOrEqualTo: inspectorBackground.trailingAnchor, constant: -36),
        ])
        let divider = NSBox(); divider.boxType = .separator
        [sidebar, divider, inspectorBackground].forEach { $0.translatesAutoresizingMaskIntoConstraints = false; addSubview($0) }
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: leadingAnchor), sidebar.topAnchor.constraint(equalTo: topAnchor), sidebar.bottomAnchor.constraint(equalTo: bottomAnchor),
            divider.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor), divider.topAnchor.constraint(equalTo: topAnchor), divider.bottomAnchor.constraint(equalTo: bottomAnchor), divider.widthAnchor.constraint(equalToConstant: 1),
            inspectorBackground.leadingAnchor.constraint(equalTo: divider.trailingAnchor), inspectorBackground.trailingAnchor.constraint(equalTo: trailingAnchor),
            inspectorBackground.topAnchor.constraint(equalTo: topAnchor), inspectorBackground.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    private func configureScroll(_ scroll: NSScrollView, document: NSStackView) {
        scroll.drawsBackground = false; scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true; scroll.autohidesScrollers = true
        scroll.documentView = document
        document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor).isActive = true
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)
    }

    private func card(_ content: NSStackView) -> NSView {
        let box = NSBox()
        box.boxType = .custom; box.titlePosition = .noTitle; box.borderWidth = 0
        box.fillColor = .controlBackgroundColor; box.cornerRadius = 11
        box.contentViewMargins = .zero
        let inner = NSView(); box.contentView = inner; inner.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: inner.leadingAnchor, constant: 16),
            content.trailingAnchor.constraint(equalTo: inner.trailingAnchor, constant: -16),
            content.topAnchor.constraint(equalTo: inner.topAnchor, constant: 15),
            content.bottomAnchor.constraint(equalTo: inner.bottomAnchor, constant: -15),
        ])
        return box
    }

    func update(apps: [DashboardApp], hostMessage: String, available: Bool, operations: [ManagementOperation]) {
        let availabilityChanged = self.available != available
        self.apps = apps; self.available = available; self.operations = operations
        let running = apps.filter { $0.state == "ready" }.count
        summary.stringValue = available ? "\(apps.count) installed · \(running) running" : "Host unavailable"
        hostLabel.stringValue = hostMessage.isEmpty ? (available ? "Connected to your local host" : "Reconnecting to the local host…") : hostMessage
        hostLabel.textColor = available ? .secondaryLabelColor : .systemOrange
        retryButton.isHidden = available
        addButton.isEnabled = available
        if availabilityChanged { renderSections() }
        refreshList()
        if !hasChosenInitialApp, let first = visibleIDs.first {
            hasChosenInitialApp = true; select(id: first)
        } else { updateInspector() }
    }

    func select(id: String) {
        guard apps.contains(where: { $0.id == id }) else { return }
        if !visibleIDs.contains(id) {
            search.stringValue = ""; filter = .all; filters.selectedSegment = 0
            refreshList()
        }
        hasChosenInitialApp = true
        let changed = selectedID != id
        let retry = loadFailed
        if changed {
            selectedID = id; details = nil; loadFailed = false; copiedID = nil
            copyReset?.cancel()
            renderSections()
            inspectorScroll.contentView.scroll(to: .zero)
        } else if retry { loadFailed = false; renderSections() }
        updateRows(); updateInspector()
        if changed || retry { onSelect?(id) }
    }

    func updateDetails(_ details: DashboardDetails) {
        guard details.appID == selectedID else { return }
        guard self.details != details || loadFailed else { return }
        self.details = details; loadFailed = false
        subtitleLabel.stringValue = details.subtitle
        renderSections()
    }

    func detailsFailed(id: String) {
        guard id == selectedID else { return }
        loadFailed = true; renderSections()
    }

    func connectionCopied(id: String) {
        guard selectedID == id else { return }
        copiedID = id; updateInspector()
        copyReset?.cancel()
        let reset = DispatchWorkItem { [weak self] in
            self?.copiedID = nil; self?.updateInspector()
        }
        copyReset = reset
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: reset)
    }

    func controlTextDidChange(_ obj: Notification) { refreshList(); updateInspector() }
    @objc private func filterChanged() {
        filter = ManagementFilter(rawValue: filters.selectedSegment) ?? .all
        refreshList(); updateInspector()
    }
    @objc private func policyChanged() {
        guard let app = selectedApp, managementCanControl(app, available: available), app.policy != "On first call" else { return }
        let value = policy.indexOfSelectedItem == 1 ? "host" : "manual"
        let current = app.policy == "Automatic" ? "host" : "manual"
        if value != current { onPolicy?(app.id, value) }
    }

    private var selectedApp: DashboardApp? { apps.first { $0.id == selectedID } }

    private func refreshList() {
        let visible = managementVisibleApps(apps, query: search.stringValue, filter: filter)
        visibleIDs = visible.map(\.id)
        if let selectedID, !visibleIDs.contains(selectedID) {
            self.selectedID = nil; details = nil; loadFailed = false; copiedID = nil
            copyReset?.cancel(); renderSections()
        }
        // Only membership and grouping change the view hierarchy; activity ticks update labels.
        let signature = visible.map { ($0.needsAttention ? "attention:" : $0.running ? "running:" : "stopped:") + $0.id }
            + (visible.isEmpty ? [apps.isEmpty ? (available ? "empty" : "offline") : "filtered"] : [])
        if signature != listSignature {
            let origin = listScroll.contentView.bounds.origin
            let focused = (window?.firstResponder as? ManagementRow)?.appID
            let oldRows = rows
            list.arrangedSubviews.forEach { list.removeArrangedSubview($0); $0.removeFromSuperview() }
            rows = [:]
            var previousGroup = ""
            for app in visible {
                let group = app.needsAttention ? "NEEDS ATTENTION" : app.running ? "RUNNING" : "STOPPED"
                if group != previousGroup {
                    let count = visible.filter { ($0.needsAttention ? "NEEDS ATTENTION" : $0.running ? "RUNNING" : "STOPPED") == group }.count
                    let label = managementLabel("\(group)  \(count)", size: 10, weight: .semibold, color: .secondaryLabelColor)
                    let header = managementStack([label], spacing: 0)
                    header.edgeInsets = NSEdgeInsets(top: 11, left: 10, bottom: 4, right: 0)
                    list.addArrangedSubview(header); managementFill(header, in: list)
                    previousGroup = group
                }
                let row = oldRows[app.id] ?? ManagementRow(id: app.id)
                row.onSelect = { [weak self] in self?.select(id: $0) }
                row.onMove = { [weak self] in self?.moveSelection($0) }
                rows[app.id] = row
                list.addArrangedSubview(row); managementFill(row, in: list)
            }
            if visible.isEmpty {
                let title = apps.isEmpty ? (available ? "Add your first app" : "Connecting to your apps") : "No matching apps"
                let body = apps.isEmpty ? (available ? "Add an MCP app to manage its tools and watch incoming requests." : "Your installed apps will appear when the host reconnects.")
                    : "Try another name or choose All."
                let text = managementLabel(body, size: 12, color: .secondaryLabelColor, wrapping: true)
                let empty = managementStack([managementLabel(title, size: 13, weight: .semibold), text], spacing: 8)
                empty.edgeInsets = NSEdgeInsets(top: 22, left: 10, bottom: 18, right: 10)
                list.addArrangedSubview(empty); managementFill(empty, in: list); managementFill(text, in: empty)
            }
            listSignature = signature
            list.layoutSubtreeIfNeeded()
            listScroll.contentView.scroll(to: origin); listScroll.reflectScrolledClipView(listScroll.contentView)
            if let focused, let row = rows[focused] { window?.makeFirstResponder(row) }
        }
        updateRows()
    }

    private func updateRows() {
        for app in apps { rows[app.id]?.update(app, selected: app.id == selectedID, available: available) }
    }

    private func moveSelection(_ step: Int) {
        guard !visibleIDs.isEmpty else { return }
        let currentID = (window?.firstResponder as? ManagementRow)?.appID ?? selectedID
        let current = currentID.flatMap { visibleIDs.firstIndex(of: $0) } ?? (step > 0 ? -1 : visibleIDs.count)
        let index = min(max(current + step, 0), visibleIDs.count - 1)
        let id = visibleIDs[index]
        select(id: id)
        if let row = rows[id] {
            window?.makeFirstResponder(row)
            list.scrollToVisible(row.convert(row.bounds.insetBy(dx: -3, dy: -3), to: list))
        }
    }

    private func updateInspector() {
        guard let app = selectedApp else {
            inspectorScroll.isHidden = true; emptyInspector.isHidden = false
            [primary, restart, copy, remove, upgrade, configure, operationDetails, detailRetry].forEach { $0.isEnabled = false }
            policy.isEnabled = false
            if apps.isEmpty {
                emptyTitle.stringValue = available ? "Add your first app" : "Connecting to your apps"
                emptyBody.stringValue = available ? "Choose Add App to connect local tools and models. Your app activity and controls will live here."
                    : "Your installed apps and their controls will appear when the local host is available."
            } else {
                emptyTitle.stringValue = visibleIDs.isEmpty ? "No matching apps" : "Select an app"
                emptyBody.stringValue = visibleIDs.isEmpty ? "Try another search or choose All to see your installed apps."
                    : "Choose an app to see requests, manage startup, and review its tools and connection."
            }
            return
        }
        inspectorScroll.isHidden = false; emptyInspector.isHidden = true
        nameLabel.stringValue = app.name
        statusLabel.stringValue = available ? (app.busy && !app.blocked ? "Updating…" : app.status) : "Status unavailable"
        statusLabel.textColor = available ? managementStatusColor(app) : .secondaryLabelColor
        subtitleLabel.stringValue = details?.subtitle ?? "Loading app information…"
        primary.title = app.running ? "Stop" : "Start"
        primary.image = NSImage(systemSymbolName: app.running ? "stop.fill" : "play.fill", accessibilityDescription: nil)
        primary.setAccessibilityLabel(primary.title + " " + app.name)
        primary.isEnabled = managementCanControl(app, available: available)
        restart.isEnabled = primary.isEnabled && app.running
        restart.setAccessibilityLabel("Restart " + app.name)
        copy.isEnabled = available && !app.blocked
        copy.title = copiedID == app.id ? "Copied" : "Copy Connection"
        copy.image = NSImage(systemSymbolName: copiedID == app.id ? "checkmark" : "doc.on.doc", accessibilityDescription: nil)
        copy.setAccessibilityLabel(copiedID == app.id ? "Connection copied" : "Copy connection for " + app.name)
        remove.isEnabled = primary.isEnabled
        upgrade.isEnabled = primary.isEnabled; configure.isEnabled = primary.isEnabled
        operationDetails.isEnabled = available && operations.contains(where: { $0.appID == app.id && ["install", "upgrade"].contains($0.action) })
        upgrade.setAccessibilityLabel("Upgrade " + app.name)
        configure.setAccessibilityLabel("Edit configuration for " + app.name)
        remove.setAccessibilityLabel("Remove " + app.name)
        let firstCall = app.policy == "On first call"
        policy.selectItem(at: firstCall ? 2 : app.policy == "Automatic" ? 1 : 0)
        policy.isEnabled = primary.isEnabled && !firstCall
        policyHint.stringValue = firstCall ? "This app uses on-first-call startup. This setting cannot be changed here."
            : app.policy == "Automatic" ? "Starts when mcpme opens. Stop stays in effect until you start the app again."
            : "Starts when you choose Start. You can switch to Automatic for future mcpme sessions."

        let activity = available ? app.activity : nil
        requestMetric.update(managementNumber(activity?.received))
        minuteMetric.update(managementNumber(activity?.lastMinute))
        flightMetric.update(managementNumber(activity?.inFlight), color: (activity?.inFlight ?? 0) > 0 ? .controlAccentColor : .labelColor)
        failureMetric.update(managementNumber(activity?.failed), color: (activity?.failed ?? 0) > 0 ? .systemOrange : .labelColor)
        if let latency = activity?.lastLatencyMS {
            latencyMetric.update(latency >= 1000 ? String(format: "%.1f s", latency / 1000) : String(format: "%.0f ms", latency))
        } else { latencyMetric.update("—") }
        if let activity {
            requestTime.stringValue = lastRequest(activity.lastRequestAt)
            requestScope.stringValue = "Tool requests · " + (activity.startedAt.map { "since host launch at " + Date(timeIntervalSince1970: $0).formatted(date: .omitted, time: .shortened) } ?? "since host launch")
        } else {
            requestTime.stringValue = available ? "Request activity is unavailable from this host." : "Activity will refresh when the host reconnects."
            requestScope.stringValue = "Tool requests · counts reset when the host restarts"
        }
        if !available { warning.stringValue = "The local host is unavailable. App controls will return after reconnecting." }
        else if app.error == "removal_recovery_required" { warning.stringValue = "Removal needs recovery. Further changes are paused to protect this app's state." }
        else if app.error == "installation_recovery_required" { warning.stringValue = "Installation needs recovery. Open View Operation to review the available Stop and Recover actions." }
        else if app.error == "upgrade_recovery_required" { warning.stringValue = "Upgrade needs recovery. Open View Operation to review the available Stop and Recover actions." }
        else if app.state == "ownership_lost" { warning.stringValue = "mcpme can no longer verify ownership of this app's process. Controls are paused until ownership is recovered." }
        else if app.error == "readiness_timeout" { warning.stringValue = "The app did not become ready in time. Choose Start to try again." }
        else if let error = app.error { warning.stringValue = error.replacingOccurrences(of: "_", with: " ").capitalized + ". Review app details and recent activity below." }
        else { warning.stringValue = "" }
        warning.isHidden = warning.stringValue.isEmpty
        let recent = operations.filter { $0.appID == app.id }.prefix(4)
        operationBody.stringValue = recent.map(\.title).joined(separator: "\n")
        operationSection.isHidden = recent.isEmpty
        detailRetry.isEnabled = available
    }

    private func lastRequest(_ epoch: Double?) -> String {
        guard let epoch else { return "No requests yet. Connect an assistant to get started." }
        let seconds = max(0, Int(Date().timeIntervalSince1970 - epoch))
        if seconds < 5 { return "Last request just now" }
        if seconds < 60 { return "Last request \(seconds) seconds ago" }
        if seconds < 3600 { return "Last request \(seconds / 60) minutes ago" }
        return "Last request " + Date(timeIntervalSince1970: epoch).formatted(date: .abbreviated, time: .shortened)
    }

    private func renderSections() {
        let displayed: [DashboardSection]
        if loadFailed {
            displayed = [DashboardSection(title: "Details unavailable", body: "The host could not load this app's details. Try again to refresh tools, connection, and package information.")]
        } else if let details {
            displayed = details.sections.map {
                !available && $0.title == "Model"
                    ? DashboardSection(title: $0.title, body: "Model activity is unavailable while the host reconnects.") : $0
            }
        }
        else { displayed = [DashboardSection(title: "Loading app details…", body: "Tools, connection, resources, and package information will appear here.")] }
        let titles = displayed.map(\.title)
        let origin = inspectorScroll.contentView.bounds.origin
        if titles != sectionTitles {
            sections.arrangedSubviews.forEach { sections.removeArrangedSubview($0); $0.removeFromSuperview() }
            sectionBodies = []
            for item in displayed {
                let heading = managementLabel(item.title, size: 13, weight: .semibold)
                let body = managementLabel(item.body, size: 12, color: .secondaryLabelColor, wrapping: true)
                body.isSelectable = true
                let section = managementStack([heading, body], spacing: 8)
                sections.addArrangedSubview(section); managementFill(section, in: sections); managementFill(body, in: section)
                sectionBodies.append(body)
            }
            sectionTitles = titles
        } else {
            for (field, item) in zip(sectionBodies, displayed) where field.stringValue != item.body { field.stringValue = item.body }
        }
        detailRetry.isHidden = !loadFailed
        inspector.layoutSubtreeIfNeeded()
        inspectorScroll.contentView.scroll(to: origin); inspectorScroll.reflectScrolledClipView(inspectorScroll.contentView)
    }
}

/// Exercises selection and action routing without showing a window or contacting a host.
/// The caller initializes NSApplication before invoking this AppKit regression check.
func managementViewSelfTest() -> Bool { ManagementDashboard.verifySelectionAndActions() }

extension ManagementDashboard {
    fileprivate static func verifySelectionAndActions() -> Bool {
        func app(_ id: String, _ state: String, error: String? = nil, busy: Bool = false, policy: String = "Manual") -> DashboardApp {
            DashboardApp(id: id, name: id.uppercased(), status: state, state: state, policy: policy, activity: nil, error: error, busy: busy)
        }
        let view = ManagementDashboard(frame: NSRect(x: 0, y: 0, width: 920, height: 660))
        var actions: [String] = []
        view.onControl = { actions.append($0 + ":" + $1) }
        view.onCopy = { actions.append($0 + ":copy") }
        view.onPolicy = { actions.append($0 + ":" + $1) }
        view.onRemove = { actions.append($0 + ":remove") }
        view.onUpgrade = { actions.append($0 + ":upgrade") }
        view.onConfigure = { actions.append($0 + ":configure") }
        view.onOperation = { actions.append($0 + ":operation") }
        func update(_ apps: [DashboardApp], available: Bool = true) {
            view.update(apps: apps, hostMessage: "", available: available, operations: [])
        }
        func invokeMutations() {
            view.primary.handler?(); view.restart.handler?(); view.remove.handler?(); view.upgrade.handler?(); view.configure.handler?()
            view.policy.selectItem(at: 1); view.policyChanged()
        }
        update([app("a", "stopped"), app("b", "ready")]); view.select(id: "a")
        view.primary.handler?()
        guard actions == ["a:start"] else { return false }
        update([app("b", "stopped"), app("a", "ready")])
        guard view.selectedID == "a" else { return false }
        invokeMutations(); view.copy.handler?()
        guard actions == ["a:start", "a:stop", "a:restart", "a:remove", "a:upgrade", "a:configure", "a:host", "a:copy"] else { return false }
        let details = DashboardDetails(appID: "a", subtitle: "Selected app", sections: [])
        view.updateDetails(details)
        view.updateDetails(DashboardDetails(appID: "b", subtitle: "Late response", sections: []))
        guard view.details == details, view.subtitleLabel.stringValue == "Selected app" else { return false }
        update([app("b", "stopped")])
        guard view.selectedID == nil, view.inspectorScroll.isHidden,
              !view.primary.isEnabled, !view.copy.isEnabled, !view.policy.isEnabled, !view.remove.isEnabled else { return false }
        actions = []; invokeMutations(); view.copy.handler?()
        guard actions.isEmpty else { return false }
        view.select(id: "b"); view.filters.selectedSegment = ManagementFilter.attention.rawValue; view.filterChanged()
        guard view.selectedID == nil, view.inspectorScroll.isHidden else { return false }
        view.select(id: "b")
        guard view.selectedID == "b", view.filter == .all, view.visibleIDs == ["b"] else { return false }
        view.search.stringValue = "unmatched"
        view.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: view.search))
        guard view.selectedID == nil else { return false }
        view.select(id: "b")
        guard view.selectedID == "b", view.search.stringValue.isEmpty else { return false }
        let cases: [(DashboardApp, Bool, Bool)] = [
            (app("b", "ready", busy: true), true, true), (app("b", "starting"), true, true),
            (app("b", "updating"), true, true),
            (app("b", "failed", error: "removal_recovery_required"), true, false), (app("b", "ready"), false, false),
            (app("b", "failed", error: "upgrade_recovery_required"), true, false),
            (app("b", "stopped", error: "installation_recovery_required"), true, false),
            (app("b", "stopped", error: "upgrade_recovery_required"), true, false),
        ]
        for (fixture, available, canCopy) in cases {
            update([fixture], available: available); actions = []
            guard !view.primary.isEnabled, !view.restart.isEnabled, !view.policy.isEnabled,
                  !view.remove.isEnabled, !view.upgrade.isEnabled, !view.configure.isEnabled,
                  view.copy.isEnabled == canCopy else { return false }
            invokeMutations(); view.copy.handler?()
            guard actions == (canCopy ? ["b:copy"] : []) else { return false }
        }
        update([app("b", "stopped", policy: "On first call")]); actions = []
        guard view.primary.isEnabled, !view.policy.isEnabled else { return false }
        view.policy.selectItem(at: 1); view.policyChanged()
        return actions.isEmpty
    }
}
