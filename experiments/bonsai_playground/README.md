# Bonsai Playground

A small native macOS app for trying Bonsai 1.7B, MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic Q4_K_M, Qwen3.5-2B in Q4_K_M or full-precision BF16, Qwen3-0.6B in full-precision BF16, Falcon3-1B-Instruct Q4_K_M, and Falcon-H1-Tiny-90M-Instruct Q4_K_M. Choose a model above the conversation, compose a message, and click Send. Each model keeps its own completed turns, transcript, draft, file selection, context size, response budget, thinking controls, sampling preset, and temperature in memory. Switching back restores that chat. New Chat clears only the selected model's draft and conversation. The current system instruction is sent once with each request.

The app uses the downloaded model files and the existing Homebrew llama.cpp installation. Bonsai uses `llama-completion`. The other models use the sibling `llama-server` binary for native chat templates, with reasoning limits for Qwen and MiniCPM. The Falcon weights were downloaded from TII's official GGUF repositories and verified against their published sizes and SHA-256 hashes. No new runtime or Python dependencies were added. Messages and responses stay in memory; the app has no saved history or telemetry. Closing the app ends its own active generation. The model selector is disabled while a reply is being generated.

Command-Return sends, Command-N starts New Chat, and Command-Z / Command-Shift-Z undo and redo edits in the message and system editors. Enter inserts a newline. The system editor grows from one to three visual lines and then scrolls. The message composer and conversation transcript remain in bounded, scrollable panes instead of expanding the window. If the model reports that the context is full, the draft and conversation are preserved; raise the Context setting and send again.

## Build and launch

Run from this worktree using its existing project Python environment. No Python dependencies are added.

```sh
uv run --locked --no-sync python experiments/bonsai_playground/build.py \
  --output .build/bonsai-playground-qwen3 --open
```

The build creates an ad-hoc-signed Apple silicon app at `.build/bonsai-playground-qwen3/Bonsai Playground.app`. The `--open` option launches a new instance, preserving any older running session. Double-click the app to reopen it later. It requires macOS 14 or later and the existing local model/runtime paths; the path controls allow selecting another local installation. For models other than Bonsai, the selected runtime directory must also contain a compatible `llama-server`.

All profiles use Metal, six CPU threads, and seed 42. Falcon-H1 uses flash attention auto so the runtime selects a supported path for its hybrid Transformer/Mamba architecture; the other profiles retain flash attention on. The system instruction and runtime path are shared. Show settings exposes model-specific sampling, a temperature override, and thinking controls. Selecting a sampling preset or changing Qwen's thinking mode resets the temperature to that preset's recommendation. Temperature changes are then retained for that model. Qwen3.5's Coding preset enables thinking and uses its vendor's precise-coding sampler. Qwen3 0.6B offers its own Recommended preset, with distinct settings for thinking on and off.

| Profile | Thinking default | Thinking limit | Temperature | Top k | Top p | Presence penalty | Repeat penalty |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bonsai | Off | Not applicable | 0.2 | 40 | 0.95 | 0 | 1.0 |
| Falcon3 and Falcon-H1 Tiny | Off; direct models | Not applicable | 0.2 | 40 | 0.95 | 0 | 1.0 |
| MiniCPM5 Fable | On; thinking-only model | 1024 | 1.0 | 40 | 0.95 | 0 | 1.0 |
| Qwen3.5 Recommended, direct | Off | Not used | 1.0 | 20 | 1.00 | 2.0 | 1.0 |
| Qwen3.5 Recommended, thinking | On when selected | 2048 | 1.0 | 20 | 0.95 | 1.5 | 1.0 |
| Qwen3.5 Coding | On | Retains selected limit | 0.6 | 20 | 0.95 | 0 | 1.0 |
| Qwen3 0.6B, direct | Off | Not used | 0.7 | 20 | 0.80 | 0 | 1.0 |
| Qwen3 0.6B, thinking | On when selected | 1024 | 0.6 | 20 | 0.95 | 0 | 1.0 |

Min p is explicitly zero for all profiles. Qwen's **Think before answering** checkbox selects the official thinking or non-thinking template. MiniCPM's checkbox stays on because its model is thinking-only. Bonsai and the Falcons answer directly. The [Qwen model card](https://huggingface.co/Qwen/Qwen3.5-2B#best-practices) and [MiniCPM fine-tune card](https://huggingface.co/GnLOLot/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic#sampling-recommendations) describe the sampling recommendations. Top k 40 for MiniCPM retains the runtime default. The Falcon values are conservative playground defaults; their pinned official generation configurations do not prescribe numeric sampling values.

The **Thinking limit** is a native token budget, separate from Max response. At the limit, llama.cpp forces the end of thinking and continues the answer in the same generation. This bounds reasoning; it does not establish the correctness or completeness of the resulting answer. The effective limit is also clamped to leave at least half of a small response allowance, or 512 tokens of a larger allowance, for the answer. The adjacent hint shows that effective limit. Qwen3.5 offers thinking limits from 128 through 16,384; Qwen3 0.6B from 128 through 8192; MiniCPM from 128 through 2048.

Reasoning is streamed with the response. Complete ordered messages are sent through the model's native template; Qwen history includes only the final answers from prior turns. Falcon history contains ordinary assistant answers with no synthetic thinking block. Bonsai and the Falcons default to 4096 context and 512 response tokens; MiniCPM retains 4096 context and 2048 response tokens. Falcon3 offers context choices through its native 8192 limit. Falcon-H1 Tiny offers choices through 32,768 while retaining a small default.

Both Qwen3.5 profiles retain **32,768 response tokens** and **65,536 context tokens** by default. Show settings exposes response choices through **81,920** and context choices through **131,072**. Thinking and the answer count toward Max response; the separate thinking limit prevents reasoning from consuming that entire allowance. Increasing Max response automatically raises Context when needed, targeting at least 8192 tokens of room for conversation. Reducing Context lowers an oversized response selection. Long history can still reach the configured context limit. Larger contexts use more memory.

Qwen3 0.6B starts with **2048 response tokens**, **8192 context tokens**, and thinking off. Its full-precision download contains BF16 and F32 tensors. Its context choices stop at the official 32,768 limit, and response choices stop at 16,384. Increasing its response budget reserves 2048 tokens of room for conversation. Its mode-specific sampling follows the [official Qwen3 model card](https://huggingface.co/Qwen/Qwen3-0.6B#best-practices); the mode switch uses the GGUF's native template. Its history includes final answers from all completed turns.

Qwen's generation deadline scales with the requested response budget, from five minutes to a maximum of thirty minutes. Qwen3.5's default allows about sixteen minutes; Qwen3 0.6B's default allows five. Stop and Escape remain available throughout generation. Bonsai, MiniCPM, and the Falcons use a ninety-second deadline.

The model files remain outside the app bundle and are shared across instances:

| Selectable model | File size |
| --- | ---: |
| Bonsai 1.7B | 490,163,968 bytes |
| MiniCPM5 2B Fable Agentic Q4_K_M | 1,561,319,776 bytes |
| Qwen3.5 2B Reasoning Q4_K_M | 1,280,835,840 bytes |
| Qwen3.5 2B Reasoning BF16 | 3,775,709,216 bytes |
| Qwen3 0.6B BF16 | 1,198,182,848 bytes |
| Falcon3 1B Instruct Q4_K_M | 1,057,044,608 bytes |
| Falcon-H1 Tiny 90M Instruct Q4_K_M | 58,600,064 bytes |

The failed MiniCPM IQ2_XXS artifact remains on disk for reproducible comparison and is excluded from the GUI selector. [Model provenance and licenses](MODELS.md) records immutable revisions, download links, byte counts, and SHA-256 hashes. The [Bonsai onboarding evaluation](../../docs/BONSAI_ONBOARDING_EVALUATION.md) describes that checkpoint's observed limitations.

The application is an independent experiment. Its source and build recipe are committed; generated app bundles and model files remain ignored.

## Reasoning runtime and privacy

For each native chat request, the app owns one llama.cpp server, with one generation slot, bound only to an owner-only Unix socket in a private temporary directory. It exposes no TCP listener or web UI. A transient owner-only key file supplies authentication; requests carry the key in memory. Server logging and prompt caching are disabled. The runtime applies native Jinja formatting and reasoning-budget enforcement for thinking models. The server is unloaded and its temporary connection state removed after generation or cancellation. A native supervisor watches the app's lifetime and reaps its owned server if the app exits unexpectedly. Prompt, response, and reasoning content are never written to files.

## Local verification

Version 0.9.0 built and passed strict code-signature verification. All 120 checks passed: 45 conversation/profile checks, 66 native UI checks, and 9 streaming/runtime checks. The updated app was opened with Qwen3 0.6B BF16 selected.

The full-precision Qwen3 0.6B artifact passed byte-count/hash verification and contains only BF16/F32 tensors. Its direct greeting and a simple two-turn code-word recall passed. The strict arithmetic fixture failed: direct mode included the correct integer with extra prose, and thinking mode with a 128-token reasoning budget returned an incorrect integer after closing its thinking block. Both modes loaded and completed without a runtime error. These results establish basic generation and one working conversation fixture, with arithmetic and exact-format limitations on the quick probes; the reasoning test does not show whether that budget was exhausted.

The version 0.7 native runtime was verified with Qwen BF16 and MiniCPM Q4 at zero and positive thinking budgets, streamed cancellation, authenticated routes, private connection permissions, and parent-process crash cleanup. Version 0.8 adds the Falcon profiles and direct streaming checks. This change does not benchmark inference engines.

The version 0.8.0 Apple silicon bundle built and passed strict code-signature verification. All 114 checks passed: 43 conversation/profile checks, 62 native UI checks, and 9 streaming/runtime checks. The GUI selector was verified to include both Falcon entries, and the new app was opened with Falcon3 selected.

The pinned Falcon downloads passed local size/hash verification. With the installed llama.cpp build 11146 (`7fe450e19`) and the app's native server path, Falcon3 passed the greeting and strict two-turn arithmetic checks (`42`, then `43`). Falcon-H1 Tiny loaded and completed normally, but failed the literal hello/hi/hey greeting classifier and integer-only arithmetic formatting check. Its arithmetic answer contained the correct integer with extra prose. A separate two-turn recall check passed, again with extra prose. These narrow fixtures establish working generation and history, with weaker exact-format compliance for Tiny; they do not establish onboarding suitability.

Run model checks sequentially. They print fixed test identifiers, booleans, and numeric metadata. Qwen defaults to direct mode; `--thinking` enables reasoning and `--thinking-budget` selects a bounded native budget. No prompts or generated responses are printed.

```sh
playground_binary='.build/bonsai-playground-qwen3/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground'
"$playground_binary" --conversation-self-test
"$playground_binary" --ui-self-test
"$playground_binary" --reasoning-self-test
"$playground_binary" --greeting-test --profile qwen3-0.6b --max-response 256
"$playground_binary" --smoke-test --profile qwen3-0.6b --max-response 256
"$playground_binary" --smoke-test --profile qwen3-0.6b --thinking --thinking-budget 128 --max-response 512
"$playground_binary" --greeting-test --profile falcon3 --max-response 128
"$playground_binary" --smoke-test --profile falcon3 --max-response 128
"$playground_binary" --greeting-test --profile falcon-h1-tiny --max-response 128
"$playground_binary" --smoke-test --profile falcon-h1-tiny --max-response 128
"$playground_binary" --smoke-test --profile qwen35-full
"$playground_binary" --smoke-test --profile qwen35-full --thinking --thinking-budget 128
"$playground_binary" --smoke-test --profile minicpm-q4
```

Previous versions reproduced an unfinished Qwen BF16 thinking loop under greedy sampling. Qwen's official card warns that the 2B model can loop even with recommended settings. This version adds native reasoning limits and direct mode; a complete answer remains subject to the model's capabilities. Interrupted or unfinished reasoning stays visible, restores the draft, and is excluded from completed history. The failed MiniCPM IQ2_XXS artifact remains a negative control; its Q4 replacement passed the earlier narrow greeting and conversation checks.
