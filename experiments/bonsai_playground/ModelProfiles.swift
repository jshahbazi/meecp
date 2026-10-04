import Foundation

enum SamplingPreset: String, CaseIterable {
    case recommended
    case coding

    var displayName: String {
        switch self { case .recommended: return "Recommended"; case .coding: return "Coding" }
    }
}

struct SamplingParameters: Equatable {
    let temperature: Double
    let topK: Int
    let topP: Double
    let minP: Double
    let presencePenalty: Double
    let repetitionPenalty: Double
}

enum ModelProfile: String, CaseIterable {
    case bonsai
    case minicpm
    case minicpmQ4 = "minicpm-q4"
    case qwen35 = "qwen35"
    case qwen35Full = "qwen35-full"
    case qwen3Small = "qwen3-0.6b"
    case falcon3 = "falcon3"
    case falconH1Tiny = "falcon-h1-tiny"

    // Keep the failed IQ2 profile available only for reproducible CLI checks.
    static let selectableProfiles: [ModelProfile] = [.bonsai, .minicpmQ4, .qwen35, .qwen35Full, .qwen3Small, .falcon3, .falconH1Tiny]

    var displayName: String {
        switch self {
        case .bonsai: return "Bonsai 1.7B"
        case .minicpm: return "MiniCPM5 2B Fable Agentic · IQ2_XXS"
        case .minicpmQ4: return "MiniCPM5 2B Fable Agentic · Q4_K_M"
        case .qwen35: return "Qwen3.5 2B Reasoning · Q4_K_M"
        case .qwen35Full: return "Qwen3.5 2B Reasoning · BF16"
        case .qwen3Small: return "Qwen3 0.6B · BF16"
        case .falcon3: return "Falcon3 1B Instruct · Q4_K_M"
        case .falconH1Tiny: return "Falcon-H1 Tiny 90M Instruct · Q4_K_M"
        }
    }

    var speakerName: String {
        switch self {
        case .bonsai: return "Bonsai"
        case .minicpm, .minicpmQ4: return "MiniCPM5"
        case .qwen35, .qwen35Full: return "Qwen3.5"
        case .qwen3Small: return "Qwen3"
        case .falcon3: return "Falcon3"
        case .falconH1Tiny: return "Falcon-H1 Tiny"
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
        case .qwen3Small:
            return "/Users/johnshahbazian/development/mcpme/models/qwen3-0.6b/Qwen3-0.6B-BF16.gguf"
        case .falcon3:
            return "/Users/johnshahbazian/development/mcpme/models/falcon3-1b/Falcon3-1B-Instruct-q4_k_m.gguf"
        case .falconH1Tiny:
            return "/Users/johnshahbazian/development/mcpme/models/falcon-h1-tiny-90m/Falcon-H1-Tiny-90M-Instruct-Q4_K_M.gguf"
        }
    }

    var isQwen: Bool { isQwen35 || self == .qwen3Small }
    var isQwen35: Bool { self == .qwen35 || self == .qwen35Full }
    var isDirectAnswerModel: Bool { self == .falcon3 || self == .falconH1Tiny }

    var defaultThinkingEnabled: Bool { supportsThinking && !isQwen }
    var supportsThinkingToggle: Bool { isQwen }
    var supportsThinking: Bool { self == .minicpm || self == .minicpmQ4 || isQwen }
    var samplingPresets: [SamplingPreset] { isQwen35 ? [.recommended, .coding] : [.recommended] }
    var defaultThinkingBudget: Int { isQwen35 ? 2048 : (supportsThinking ? 1024 : 0) }
    var thinkingBudgets: [Int] {
        if isQwen35 { return [128, 256, 512, 1024, 2048, 4096, 8192, 16384] }
        if self == .qwen3Small { return [128, 256, 512, 1024, 2048, 4096, 8192] }
        return supportsThinking ? [128, 256, 512, 1024, 2048] : []
    }

    func sampling(preset: SamplingPreset = .recommended, thinking: Bool) -> SamplingParameters {
        if self == .qwen3Small {
            return SamplingParameters(temperature: thinking ? 0.6 : 0.7, topK: 20,
                                      topP: thinking ? 0.95 : 0.8, minP: 0.0,
                                      presencePenalty: 0.0, repetitionPenalty: 1.0)
        }
        if isQwen35 {
            if preset == .coding && thinking {
                return SamplingParameters(temperature: 0.6, topK: 20, topP: 0.95, minP: 0.0,
                                          presencePenalty: 0.0, repetitionPenalty: 1.0)
            }
            if thinking {
                return SamplingParameters(temperature: 1.0, topK: 20, topP: 0.95, minP: 0.0,
                                          presencePenalty: 1.5, repetitionPenalty: 1.0)
            }
            return SamplingParameters(temperature: 1.0, topK: 20, topP: 1.0, minP: 0.0,
                                      presencePenalty: 2.0, repetitionPenalty: 1.0)
        }
        return SamplingParameters(temperature: supportsThinking ? 1.0 : 0.2, topK: 40, topP: 0.95, minP: 0.0,
                                  presencePenalty: 0.0, repetitionPenalty: 1.0)
    }

    var defaultMaximumOutput: Int { isQwen35 ? 32768 : (self == .qwen3Small ? 2048 : (self == .bonsai || isDirectAnswerModel ? 512 : 2048)) }

    var defaultContext: Int { isQwen35 ? 65536 : (self == .qwen3Small ? 8192 : 4096) }

    var contextSizes: [Int] {
        if isQwen35 { return [4096, 8192, 16384, 32768, 65536, 131072] }
        if self == .qwen3Small { return [4096, 8192, 16384, 32768] }
        if self == .falconH1Tiny { return [2048, 4096, 8192, 16384, 32768] }
        return [2048, 4096, 8192]
    }

    var maximumOutputs: [Int] {
        isQwen35 ? [128, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 81920]
            : self == .qwen3Small ? [128, 512, 1024, 2048, 4096, 8192, 16384]
            : [128, 512, 1024, 2048]
    }

    func generationTimeout(maximumOutput: Int) -> TimeInterval {
        isQwen ? max(300, min(1800, Double(maximumOutput) / 40 + 120)) : 90
    }

    /// Qwen3 emits its own thinking opener; other thinking templates prefill it.
    var assistantOutputPrefix: String { assistantOutputPrefix(thinkingEnabled: defaultThinkingEnabled) }

    func assistantOutputPrefix(thinkingEnabled: Bool) -> String {
        if self == .bonsai || isDirectAnswerModel { return "" }
        if isQwen && !thinkingEnabled { return "<think>\n\n</think>\n\n" }
        if self == .qwen3Small { return "" }
        return "<think>\n"
    }

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
        render(system: system, prompt: prompt, conversation: conversation,
               thinkingEnabled: defaultThinkingEnabled)
    }

    func render(system: String, prompt: String, conversation: Conversation,
                thinkingEnabled: Bool) -> String {
        if self == .bonsai { return conversation.render(system: system, prompt: prompt) }
        if isDirectAnswerModel { return renderDirect(system: system, prompt: prompt, conversation: conversation) }
        if self == .qwen3Small {
            return chatMessages(system: system, prompt: prompt, conversation: conversation).map {
                "<|im_start|>\($0["role"]!)\n\($0["content"]!)<|im_end|>\n"
            }.joined() + "<|im_start|>assistant\n" + assistantOutputPrefix(thinkingEnabled: thinkingEnabled)
        }
        if isQwen {
            return renderQwen(system: system, prompt: prompt, conversation: conversation,
                              thinkingEnabled: thinkingEnabled)
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

    private func renderDirect(system: String, prompt: String, conversation: Conversation) -> String {
        if self == .falcon3 {
            var messages = "<|system|>\n\(system)\n"
            for turn in conversation.turns {
                messages += "<|user|>\n\(turn.user)\n"
                messages += "<|assistant|>\n\(turn.assistant)<|endoftext|>\n"
            }
            return messages + "<|user|>\n\(prompt)\n<|assistant|>\n"
        }
        var messages = system.isEmpty ? "" : "<|im_start|>system\n\(system)<|im_end|>\n"
        for turn in conversation.turns {
            messages += "<|im_start|>user\n\(turn.user)<|im_end|>\n"
            messages += "<|im_start|>assistant\n\(turn.assistant)<|im_end|>\n"
        }
        return messages + "<|im_start|>user\n\(prompt)<|im_end|>\n<|im_start|>assistant\n"
    }

    /// Qwen's text-only thinking template removes reasoning from prior turns.
    private func renderQwen(system: String, prompt: String, conversation: Conversation,
                            thinkingEnabled: Bool) -> String {
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
        return messages + "<|im_start|>user\n\(trimmed(prompt))<|im_end|>\n<|im_start|>assistant\n"
            + assistantOutputPrefix(thinkingEnabled: thinkingEnabled)
    }

    func finalAnswer(_ text: String) -> String {
        guard self != .bonsai && !isDirectAnswerModel else { return text }
        if isQwen && !text.contains("<think>") { return text }
        guard let close = text.range(of: "</think>", options: .backwards) else { return "" }
        return String(text[close.upperBound...])
    }

    func chatMessages(system: String, prompt: String, conversation: Conversation) -> [[String: String]] {
        var result = [["role": "system", "content": system]]
        for turn in conversation.turns {
            result.append(["role": "user", "content": turn.user])
            result.append(["role": "assistant", "content": isQwen ? finalAnswer(turn.assistant) : (isDirectAnswerModel ? turn.assistant : normalizedAssistant(turn.assistant))])
        }
        result.append(["role": "user", "content": prompt])
        return result
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
        let qwen = ModelProfile.qwen35.render(system: "  Be concise.\n", prompt: " What value? ", conversation: conversation, thinkingEnabled: true)
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
            system: "  Be concise.\n", prompt: " What value? ", conversation: conversation, thinkingEnabled: true) == qwen
            && ModelProfile.qwen35Full.assistantOutputPrefix == ModelProfile.qwen35.assistantOutputPrefix
            && ModelProfile.qwen35Full.runtimeArguments == ModelProfile.qwen35.runtimeArguments
            && ModelProfile.qwen35Full.defaultMaximumOutput == ModelProfile.qwen35.defaultMaximumOutput
        checks["qwen_bf16_is_distinct_from_q4"] = ModelProfile.qwen35Full.rawValue == "qwen35-full"
            && ModelProfile.qwen35Full.defaultModelPath.hasSuffix("/qwen3.5-2b/Qwen3.5-2B-BF16.gguf")
            && ModelProfile.qwen35Full.defaultModelPath != ModelProfile.qwen35.defaultModelPath
        checks["broken_iq2_is_excluded_from_selector"] = !ModelProfile.selectableProfiles.contains(.minicpm)
            && ModelProfile.selectableProfiles.contains(.minicpmQ4)
        let qwens: [ModelProfile] = [.qwen35, .qwen35Full]
        checks["qwen_large_response_profiles_share_defaults_and_options"] = qwens.allSatisfy {
            $0.isQwen && $0.defaultMaximumOutput == 32768 && $0.defaultContext == 65536
                && $0.contextSizes == ModelProfile.qwen35.contextSizes
                && $0.maximumOutputs == ModelProfile.qwen35.maximumOutputs
                && $0.contextSizes.last == 131072 && $0.maximumOutputs.last == 81920
        }
        checks["default_response_fits_context_with_prompt_headroom"] = ModelProfile.allCases.allSatisfy {
            $0.defaultMaximumOutput < $0.defaultContext
                && $0.contextSizes.contains($0.defaultContext)
                && $0.maximumOutputs.contains($0.defaultMaximumOutput)
        }
        checks["qwen_generation_timeout_scales_and_is_bounded"] = qwens.allSatisfy {
            $0.generationTimeout(maximumOutput: 128) == 300
                && $0.generationTimeout(maximumOutput: $0.defaultMaximumOutput) > 900
                && $0.generationTimeout(maximumOutput: 81920) == 1800
                && $0.generationTimeout(maximumOutput: Int.max) == 1800
        }
        checks["smaller_profiles_keep_short_generation_limits"] = [ModelProfile.bonsai, .minicpm, .minicpmQ4].allSatisfy {
            !$0.isQwen && $0.defaultContext == 4096
                && $0.contextSizes == [2048, 4096, 8192]
                && $0.maximumOutputs == [128, 512, 1024, 2048]
                && $0.generationTimeout(maximumOutput: 81920) == 90
        }
        let direct = ModelProfile.qwen35.render(system: "  Be concise.\n", prompt: " What value? ", conversation: conversation, thinkingEnabled: false)
        checks["qwen_direct_mode_has_closed_empty_thinking_prefill"] = direct == qwenExpected.replacingOccurrences(of: "assistant\n<think>\n", with: "assistant\n<think>\n\n</think>\n\n")
        checks["thinking_controls_match_model_capabilities"] = qwens.allSatisfy {
            $0.supportsThinkingToggle && !$0.defaultThinkingEnabled && $0.defaultThinkingBudget == 2048
        } && ModelProfile.minicpmQ4.defaultThinkingEnabled && !ModelProfile.minicpmQ4.supportsThinkingToggle
            && !ModelProfile.bonsai.supportsThinking
        checks["qwen_sampling_uses_official_mode_defaults"] = qwens.allSatisfy {
            $0.sampling(thinking: true) == SamplingParameters(temperature: 1, topK: 20, topP: 0.95, minP: 0, presencePenalty: 1.5, repetitionPenalty: 1)
                && $0.sampling(thinking: false) == SamplingParameters(temperature: 1, topK: 20, topP: 1, minP: 0, presencePenalty: 2, repetitionPenalty: 1)
                && $0.sampling(preset: .coding, thinking: true).temperature == 0.6
                && $0.sampling(preset: .coding, thinking: true).presencePenalty == 0
        }
        let qwen3 = ModelProfile.qwen3Small
        checks["qwen3_small_has_separate_native_profile_defaults"] = qwen3.isQwen
            && !qwen3.isQwen35 && qwen3.supportsThinkingToggle && !qwen3.defaultThinkingEnabled
            && qwen3.defaultContext == 8192 && qwen3.defaultMaximumOutput == 2048
            && qwen3.contextSizes == [4096, 8192, 16384, 32768]
            && qwen3.thinkingBudgets == [128, 256, 512, 1024, 2048, 4096, 8192]
            && qwen3.sampling(thinking: true) == SamplingParameters(temperature: 0.6, topK: 20, topP: 0.95, minP: 0, presencePenalty: 0, repetitionPenalty: 1)
            && qwen3.sampling(thinking: false) == SamplingParameters(temperature: 0.7, topK: 20, topP: 0.8, minP: 0, presencePenalty: 0, repetitionPenalty: 1)
        checks["qwen3_small_uses_native_thinking_toggle_and_history"] = qwen3.render(
            system: "System", prompt: "Next", conversation: conversation, thinkingEnabled: true)
            .hasSuffix("<|im_start|>assistant\n")
            && qwen3.render(system: "System", prompt: "Next", conversation: conversation, thinkingEnabled: false)
                .hasSuffix("<|im_start|>assistant\n<think>\n\n</think>\n\n")
            && qwen3.chatMessages(system: "System", prompt: "Next", conversation: conversation)
                == ModelProfile.qwen35.chatMessages(system: "System", prompt: "Next", conversation: conversation)
        checks["minicpm_sampling_and_bonsai_defaults"] = ModelProfile.minicpmQ4.sampling(thinking: true).temperature == 1
            && ModelProfile.bonsai.sampling(thinking: false).temperature == 0.2
        let messages = ModelProfile.qwen35.chatMessages(system: "Be concise.", prompt: "Next.", conversation: conversation)
        checks["structured_messages_keep_all_turns_and_only_final_qwen_history"] = messages.map { $0["role"]! } == ["system", "user", "assistant", "user", "assistant", "user"]
            && messages[2]["content"] == "\n\nStored 17." && messages[4]["content"] == "\nThe value is 21."
            && messages[5]["content"] == "Next." && conversation.turns.count == 2
        checks["qwen_direct_final_answer_and_unfinished_reasoning_distinguished"] = ModelProfile.qwen35.finalAnswer("Direct answer") == "Direct answer"
            && ModelProfile.qwen35.finalAnswer("<think>Unfinished").isEmpty
        let falcons = [ModelProfile.falcon3, .falconH1Tiny]
        checks["falcon_profiles_are_direct_only"] = falcons.allSatisfy {
            !$0.supportsThinking && !$0.defaultThinkingEnabled && !$0.supportsThinkingToggle
                && $0.thinkingBudgets.isEmpty && $0.assistantOutputPrefix.isEmpty
                && $0.finalAnswer("Plain answer") == "Plain answer"
        }
        var directHistory = Conversation()
        directHistory.append(user: "First message", assistant: "First answer")
        directHistory.append(user: "Second message", assistant: "Second answer")
        checks["falcon_history_preserves_plain_ordered_messages"] = falcons.allSatisfy {
            $0.chatMessages(system: "System", prompt: "Third message", conversation: directHistory) == [
                ["role": "system", "content": "System"], ["role": "user", "content": "First message"],
                ["role": "assistant", "content": "First answer"], ["role": "user", "content": "Second message"],
                ["role": "assistant", "content": "Second answer"], ["role": "user", "content": "Third message"]
            ]
        }
        checks["falcon_context_choices_fit_model_limits"] = ModelProfile.falcon3.contextSizes.last == 8192
            && ModelProfile.falconH1Tiny.contextSizes.last == 32768
            && falcons.allSatisfy { $0.defaultContext == 4096 && $0.defaultMaximumOutput == 512 }
        return checks
    }
}
