import AppKit
import Foundation

struct PiConversationEvent {
    let sequence: Int
    let role: String
    let text: String
    let messageID: String?

    init?(_ object: [String: Any]) {
        guard let sequence = preparationInteger(object["sequence"]), sequence > 0,
              let role = object["role"] as? String, ["user", "assistant"].contains(role),
              let text = preparationText(object["text"], maximum: 8192), text.utf8.count <= 8192 else { return nil }
        let messageID: String?
        if let value = object["message_id"], !(value is NSNull) {
            guard let id = preparationUUID(value) else { return nil }
            messageID = id
        } else { messageID = nil }
        self.sequence = sequence; self.role = role; self.text = text; self.messageID = messageID
    }
}

struct PiConversationBatch {
    let jobID: String
    let events: [PiConversationEvent]
    let cursor: Int
    let firstSequence: Int
    let reset: Bool
    let available: Bool
    let canMessage: Bool

    init?(_ object: [String: Any]) {
        guard preparationInteger(object["api_version"]) == 1,
              let jobID = preparationUUID(object["job_id"]),
              let cursor = preparationInteger(object["cursor"]), cursor >= 0,
              let first = preparationInteger(object["first_sequence"]), first >= 0,
              let reset = preparationBoolean(object["reset"]),
              let available = preparationBoolean(object["available"]),
              let canMessage = preparationBoolean(object["can_message"]),
              let rows = object["events"] as? [[String: Any]], rows.count <= 128 else { return nil }
        let events = rows.compactMap(PiConversationEvent.init)
        guard events.count == rows.count,
              events.reduce(0, { $0 + $1.text.utf8.count }) <= 262144,
              zip(events, events.dropFirst()).allSatisfy({ $0.sequence < $1.sequence }),
              events.allSatisfy({ $0.sequence >= first && $0.sequence <= cursor }),
              available ? (cursor == 0 ? first == 0 && events.isEmpty : first > 0 && first <= cursor)
                  : cursor == 0 && first == 0 && events.isEmpty && !canMessage else { return nil }
        self.jobID = jobID; self.events = events; self.cursor = cursor; firstSequence = first
        self.reset = reset; self.available = available; self.canMessage = canMessage
    }
}

func piConversationBubble(role: String, text: String) -> NSView {
    let label = workspaceLabel(text, size: 13)
    label.isSelectable = true; label.maximumNumberOfLines = 0
    label.setAccessibilityLabel(role == "user" ? "Your message" : "MeeCP agent message")
    let heading = workspaceLabel(role == "user" ? "You" : "MeeCP agent", size: 11, weight: .semibold, secondary: true)
    let content = workspaceStack([heading, label], spacing: 6)
    workspaceFill(heading, in: content); workspaceFill(label, in: content)
    let box = NSBox(); box.boxType = .custom; box.titlePosition = .noTitle
    box.fillColor = role == "user" ? .quaternaryLabelColor.withAlphaComponent(0.08) : .clear
    box.borderColor = .clear; box.borderWidth = 0; box.cornerRadius = 10; box.contentViewMargins = .zero
    let container = box.contentView!; container.addSubview(content)
    NSLayoutConstraint.activate([
        content.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 14),
        content.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -14),
        content.topAnchor.constraint(equalTo: container.topAnchor, constant: 12),
        content.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -12),
    ])
    return box
}

func piConversationTask(_ views: [NSView]) -> NSView {
    let card = NSBox(); card.boxType = .custom; card.titlePosition = .noTitle
    card.fillColor = .controlBackgroundColor; card.borderColor = .separatorColor
    card.borderWidth = 1; card.cornerRadius = 12; card.contentViewMargins = .zero
    let stack = workspaceStack(views, spacing: 10)
    for view in views { workspaceFill(view, in: stack) }
    let content = card.contentView!; content.addSubview(stack)
    NSLayoutConstraint.activate([
        stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
        stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
        stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
        stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
    ])
    return card
}
