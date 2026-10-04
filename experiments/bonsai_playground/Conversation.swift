import Foundation

struct ConversationTurn {
    let user: String
    let assistant: String
}

/// Session history is retained only in memory and is never truncated here.
struct Conversation {
    private(set) var turns: [ConversationTurn] = []

    var isEmpty: Bool { turns.isEmpty }

    mutating func append(user: String, assistant: String) {
        turns.append(ConversationTurn(user: user, assistant: assistant))
    }

    mutating func reset() {
        turns.removeAll()
    }

    func render(system: String, prompt: String) -> String {
        var messages = "<|im_start|>system\n\(system)<|im_end|>\n"
        for turn in turns {
            messages += "<|im_start|>user\n\(turn.user)<|im_end|>\n"
            messages += "<|im_start|>assistant\n\(turn.assistant)<|im_end|>\n"
        }
        messages += "<|im_start|>user\n\(prompt)<|im_end|>\n"
        messages += "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        return messages
    }
}

/// Synthetic checks return fixed identifiers and booleans, never message text.
enum ConversationChecks {
    static func run() -> [String: Bool] {
        var conversation = Conversation()
        let first = conversation.render(system: "Be concise.", prompt: "Remember 17.")
        let firstExpected = "<|im_start|>system\nBe concise.<|im_end|>\n"
            + "<|im_start|>user\nRemember 17.<|im_end|>\n"
            + "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        var checks = [
            "new_session_empty": conversation.isEmpty,
            "initial_roles_and_prefill": first == firstExpected,
            "render_does_not_store_draft": conversation.turns.isEmpty,
        ]

        conversation.append(user: "Remember 17.", assistant: "Stored 17.")
        conversation.append(user: "Add 4.", assistant: "The value is 21.")
        let rendered = conversation.render(system: "Be concise.", prompt: "What value?")
        let expected = "<|im_start|>system\nBe concise.<|im_end|>\n"
            + "<|im_start|>user\nRemember 17.<|im_end|>\n"
            + "<|im_start|>assistant\nStored 17.<|im_end|>\n"
            + "<|im_start|>user\nAdd 4.<|im_end|>\n"
            + "<|im_start|>assistant\nThe value is 21.<|im_end|>\n"
            + "<|im_start|>user\nWhat value?<|im_end|>\n"
            + "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        checks["completed_turns_make_session_nonempty"] = !conversation.isEmpty
        checks["all_turns_in_role_order"] = rendered == expected
        checks["system_role_once"] = rendered.components(separatedBy: "<|im_start|>system\n").count == 2
        checks["history_answers_preserved"] = conversation.turns.count == 2
            && conversation.turns[0].assistant == "Stored 17."
            && conversation.turns[1].assistant == "The value is 21."
        checks["reasoning_prefill_only_for_new_answer"] = rendered.components(separatedBy: "<think>").count == 2

        var whitespace = Conversation()
        whitespace.append(user: "  request\n", assistant: "  answer\n")
        checks["history_whitespace_preserved"] = whitespace.render(system: "", prompt: "Next")
            .contains("<|im_start|>user\n  request\n<|im_end|>\n<|im_start|>assistant\n  answer\n<|im_end|>\n")

        conversation.reset()
        checks["reset_clears_history"] = conversation.isEmpty && conversation.turns.isEmpty
        checks["reset_restores_initial_render"] = conversation.render(system: "Be concise.", prompt: "Remember 17.") == firstExpected
        return checks
    }
}
