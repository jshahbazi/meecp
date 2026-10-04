import Foundation

enum ModelProfile: String, CaseIterable {
    case bonsai
    case minicpm
    case minicpmQ4 = "minicpm-q4"
    case qwen35 = "qwen35"
    case qwen35Full = "qwen35-full"

    // Keep the failed IQ2 profile available only for reproducible CLI checks.
    static let selectableProfiles: [ModelProfile] = [.bonsai, .minicpmQ4, .qwen35, .qwen35Full]

    var displayName: String {
        switch self {
        case .bonsai: return "Bonsai 1.7B"
        case .minicpm: return "MiniCPM5 2B Fable Agentic · IQ2_XXS"
        case .minicpmQ4: return "MiniCPM5 2B Fable Agentic · Q4_K_M"
        case .qwen35: return "Qwen3.5 2B Reasoning · Q4_K_M"
        case .qwen35Full: return "Qwen3.5 2B Reasoning · BF16"
        }
    }

    var speakerName: String {
        switch self {
        case .bonsai: return "Bonsai"
        case .minicpm, .minicpmQ4: return "MiniCPM5"
        case .qwen35, .qwen35Full: return "Qwen3.5"
        }
    }

    var defaultModelPath: String {
        switch self {
        case .bonsai:
            return "/Users/johnshahbazian/development/mcpme/models/bonsai-1.7b/Ternary-Bonsai-1.7B-Q2_0_g64.gguf"
        case .minicpm:
            return "/Users/johnshahbazian/development/mcpme/models/minicpm5-2b-agentic/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-IQ2_XXS.gguf"
        case .minicpmQ4:
            return "/Users/johnshahbazian/development/mcpme/models/minicpm5-2b-agentic/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-Q4_K_M.gguf"
        case .qwen35:
            return "/Users/johnshahbazian/development/mcpme/models/qwen3.5-2b/Qwen3.5-2B-Q4_K_M.gguf"
        case .qwen35Full:
            return "/Users/johnshahbazian/development/mcpme/models/qwen3.5-2b/Qwen3.5-2B-BF16.gguf"
        }
    }

    var defaultMaximumOutput: Int { self == .bonsai ? 512 : 2048 }

    /// Thinking profiles continue a block opened in their generation prompt.
    var assistantOutputPrefix: String { self == .bonsai ? "" : "<think>\n" }

    var runtimeArguments: [String] { self == .bonsai ? [] : ["--special"] }

    /// With special output enabled, the CLI includes its terminal control token.
    func cleanCompletionTail(_ text: String) -> String {
        guard self != .bonsai else { return text }
        var result = text
        while true {
            let cleaned = result.replacingOccurrences(of: "(?:<\\|im_end\\|>|<\\|endoftext\\|>|</s>)\\s*\\z",
                with: "", options: .regularExpression)
            if cleaned == result { return result }
            result = cleaned
        }
    }

    func render(system: String, prompt: String, conversation: Conversation) -> String {
        if self == .bonsai { return conversation.render(system: system, prompt: prompt) }
        if self == .qwen35 || self == .qwen35Full {
            return renderQwen(system: system, prompt: prompt, conversation: conversation)
        }
        var messages = "<s><|im_start|>system\n\(system)<|im_end|>\n"
        for turn in conversation.turns {
            messages += "<|im_start|>user\n\(turn.user)<|im_end|>\n"
            messages += "<|im_start|>assistant\n\(normalizedAssistant(turn.assistant))<|im_end|>\n"
        }
        messages += "<|im_start|>user\n\(prompt)<|im_end|>\n"
        messages += "<|im_start|>assistant\n<think>\n"
        return messages
    }

    /// Qwen's text-only thinking template removes reasoning from prior turns.
    private func renderQwen(system: String, prompt: String, conversation: Conversation) -> String {
        func trimmed(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
        var messages = "<|im_start|>system\n\(trimmed(system))<|im_end|>\n"
        for turn in conversation.turns {
            messages += "<|im_start|>user\n\(trimmed(turn.user))<|im_end|>\n"
            var answer = trimmed(turn.assistant)
            if let close = answer.range(of: "</think>", options: .backwards) {
                answer = trimNewlines(String(answer[close.upperBound...]), trailing: false)
            }
            messages += "<|im_start|>assistant\n\(answer)<|im_end|>\n"
        }
        return messages + "<|im_start|>user\n\(trimmed(prompt))<|im_end|>\n<|im_start|>assistant\n<think>\n"
    }

    func finalAnswer(_ text: String) -> String {
        guard self != .bonsai else { return text }
        guard let close = text.range(of: "</think>", options: .backwards) else { return "" }
        return String(text[close.upperBound...])
    }

    private func normalizedAssistant(_ original: String) -> String {
        var content = original
        var reasoning = ""
        if let firstClose = original.range(of: "</think>") {
            reasoning = trimNewlines(String(original[..<firstClose.lowerBound]), trailing: true)
            if let lastOpen = reasoning.range(of: "<think>", options: .backwards) {
                reasoning = String(reasoning[lastOpen.upperBound...])
            }
            reasoning = trimNewlines(reasoning, trailing: true)
            if let lastClose = original.range(of: "</think>", options: .backwards) {
                content = trimNewlines(String(original[lastClose.upperBound...]), trailing: false)
            }
        }
        if !reasoning.isEmpty {
            return "<think>\n\(reasoning)\n</think>\n\n\(trimNewlines(content, trailing: false))"
        }
        if !content.contains("<think>") && !content.contains("</think>") {
            return "<think>\n\n</think>\n\n\(trimNewlines(content, trailing: false))"
        }
        return content
    }

    /// Match the upstream template's strip/lstrip of LF, preserving other space.
    private func trimNewlines(_ value: String, trailing: Bool) -> String {
        var scalars = value.unicodeScalars[...]
        while scalars.first?.value == 10 { scalars.removeFirst() }
        if trailing {
            while scalars.last?.value == 10 { scalars.removeLast() }
        }
        return String(scalars)
    }
}

/// Synthetic profile checks contain no user prompts or generated responses.
enum ModelProfileChecks {
    static func run() -> [String: Bool] {
        var conversation = Conversation()
        conversation.append(user: "Remember 17.", assistant: "\n<think>\nCount 17.\n</think>\n\nStored 17.")
        conversation.append(user: "Add 4.", assistant: "\nThe value is 21.")
        let rendered = ModelProfile.minicpm.render(system: "Be concise.", prompt: "What value?",
                                                   conversation: conversation)
        let expected = "<s><|im_start|>system\nBe concise.<|im_end|>\n"
            + "<|im_start|>user\nRemember 17.<|im_end|>\n"
            + "<|im_start|>assistant\n<think>\nCount 17.\n</think>\n\nStored 17.<|im_end|>\n"
            + "<|im_start|>user\nAdd 4.<|im_end|>\n"
            + "<|im_start|>assistant\n<think>\n\n</think>\n\nThe value is 21.<|im_end|>\n"
            + "<|im_start|>user\nWhat value?<|im_end|>\n"
            + "<|im_start|>assistant\n<think>\n"
        var checks = [
            "minicpm_bos_once": rendered.hasPrefix("<s><|im_start|>system\n")
                && rendered.components(separatedBy: "<s>").count == 2,
            "minicpm_full_history_in_order": rendered == expected,
            "minicpm_active_thinking_prefix": rendered.hasSuffix("<|im_start|>assistant\n<think>\n"),
            "bonsai_render_unchanged": ModelProfile.bonsai.render(system: "Be concise.", prompt: "What value?",
                conversation: conversation) == conversation.render(system: "Be concise.", prompt: "What value?"),
            "history_not_mutated": conversation.turns.count == 2
                && conversation.turns[0].assistant == "\n<think>\nCount 17.\n</think>\n\nStored 17."
                && conversation.turns[1].assistant == "\nThe value is 21.",
            "model_paths_distinct": ModelProfile.bonsai.defaultModelPath.contains("/bonsai-1.7b/")
                && ModelProfile.minicpm.defaultModelPath.hasSuffix("MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-IQ2_XXS.gguf"),
            "profile_output_defaults": ModelProfile.bonsai.defaultMaximumOutput == 512
                && ModelProfile.minicpm.defaultMaximumOutput == 2048
                && ModelProfile.bonsai.assistantOutputPrefix.isEmpty
                && ModelProfile.minicpm.assistantOutputPrefix == "<think>\n",
        ]
        func assistantHistory(_ answer: String) -> String {
            var history = Conversation()
            history.append(user: "Synthetic request", assistant: answer)
            return ModelProfile.minicpm.render(system: "Synthetic system", prompt: "Synthetic next",
                                               conversation: history)
        }
        checks["empty_reasoning_normalized"] = assistantHistory("<think>\n\n</think>\n\nAnswer")
            .contains("assistant\n<think>\n\n</think>\n\nAnswer<|im_end|>")
        checks["first_close_last_open_last_answer"] = assistantHistory("<think>discard<think>\nKeep.\n</think>discard</think>\nFinal")
            .contains("assistant\n<think>\nKeep.\n</think>\n\nFinal<|im_end|>")
        checks["incomplete_reasoning_preserved"] = assistantHistory("\n<think>\nUnfinished")
            .contains("assistant\n\n<think>\nUnfinished<|im_end|>")
        checks["answer_spaces_preserved"] = assistantHistory("\n  Answer\n")
            .contains("assistant\n<think>\n\n</think>\n\n  Answer\n<|im_end|>")
        checks["minicpm_terminal_controls_removed"] = ModelProfile.minicpm.cleanCompletionTail("<think>Reason</think>\nAnswer<|im_end|>\n</s>\n")
            == "<think>Reason</think>\nAnswer"
        checks["bonsai_output_controls_unchanged"] = ModelProfile.bonsai.cleanCompletionTail("Synthetic <|im_end|>\n")
            == "Synthetic <|im_end|>\n" && ModelProfile.bonsai.runtimeArguments.isEmpty
            && ModelProfile.minicpm.runtimeArguments == ["--special"]
        checks["minicpm_q4_uses_verified_thinking_template"] = ModelProfile.minicpmQ4.render(
            system: "Be concise.", prompt: "What value?", conversation: conversation) == rendered
            && ModelProfile.minicpmQ4.defaultModelPath.hasSuffix("i1-Q4_K_M.gguf")
        let qwen = ModelProfile.qwen35.render(system: "  Be concise.\n", prompt: " What value? ", conversation: conversation)
        let qwenExpected = "<|im_start|>system\nBe concise.<|im_end|>\n"
            + "<|im_start|>user\nRemember 17.<|im_end|>\n"
            + "<|im_start|>assistant\nStored 17.<|im_end|>\n"
            + "<|im_start|>user\nAdd 4.<|im_end|>\n"
            + "<|im_start|>assistant\nThe value is 21.<|im_end|>\n"
            + "<|im_start|>user\nWhat value?<|im_end|>\n<|im_start|>assistant\n<think>\n"
        checks["qwen_thinking_template_uses_final_history"] = qwen == qwenExpected && !qwen.contains("<s>")
        checks["qwen_final_answer_requires_closed_thinking"] = ModelProfile.qwen35.finalAnswer("<think>Reason</think>42") == "42"
            && ModelProfile.qwen35.finalAnswer("<think>Unfinished").isEmpty
        checks["qwen_bf16_uses_same_thinking_protocol"] = ModelProfile.qwen35Full.render(
            system: "  Be concise.\n", prompt: " What value? ", conversation: conversation) == qwen
            && ModelProfile.qwen35Full.assistantOutputPrefix == ModelProfile.qwen35.assistantOutputPrefix
            && ModelProfile.qwen35Full.runtimeArguments == ModelProfile.qwen35.runtimeArguments
            && ModelProfile.qwen35Full.defaultMaximumOutput == ModelProfile.qwen35.defaultMaximumOutput
        checks["qwen_bf16_is_distinct_from_q4"] = ModelProfile.qwen35Full.rawValue == "qwen35-full"
            && ModelProfile.qwen35Full.defaultModelPath.hasSuffix("/qwen3.5-2b/Qwen3.5-2B-BF16.gguf")
            && ModelProfile.qwen35Full.defaultModelPath != ModelProfile.qwen35.defaultModelPath
        checks["broken_iq2_is_excluded_from_selector"] = ModelProfile.selectableProfiles == [.bonsai, .minicpmQ4, .qwen35, .qwen35Full]
        return checks
    }
}
