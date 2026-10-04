# Bonsai Playground

A small native macOS app for trying Bonsai 1.7B, MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic Q4_K_M, and Qwen3.5-2B in Q4_K_M or full-precision BF16 with thinking enabled. Choose a model above the conversation, compose a message, and click Send. Each model keeps its own completed turns, transcript, draft, file selection, and response budget in memory. Switching back restores that chat. New Chat clears only the selected model's draft and conversation. The current system instruction is sent once with each request.

The app uses the downloaded model files and Homebrew `llama-completion`. Messages, conversation turns, and runtime diagnostics stay in memory; the app has no saved history or telemetry. Closing the app ends its own active generation. The model selector is disabled while a reply is being generated.

Command-Return sends, Command-N starts New Chat, and Command-Z / Command-Shift-Z undo and redo edits in the message and system editors. Enter inserts a newline. The system editor grows from one to three visual lines and then scrolls. The message composer and conversation transcript remain in bounded, scrollable panes instead of expanding the window. If the model reports that the context is full, the draft and conversation are preserved; raise the Context setting and send again.

## Build and launch

Run from this worktree using its existing project Python environment. No Python dependencies are added.

```sh
uv run --locked --no-sync python experiments/bonsai_playground/build.py \
  --output .build/bonsai-playground-full-qwen-v2 --open
```

The build creates an ad-hoc-signed Apple silicon app at `.build/bonsai-playground-full-qwen-v2/Bonsai Playground.app`. The `--open` option launches a new instance, preserving any older running session. Double-click the app to reopen it later. It requires macOS 14 or later and the existing local model/runtime paths; the path controls allow selecting another local installation.

All profiles use Metal, six CPU threads, 4K context, temperature 0.2, and seed 42. The system instruction, sampling settings, context, and runtime are shared, so you can compare the models under the same settings. Bonsai defaults to 512 output tokens and its published empty thinking prefill. MiniCPM and Qwen default to 2048 output tokens to allow room for thinking. Reasoning is streamed with the response; Qwen's template includes only the final answer from prior turns in subsequent model requests. Each profile uses its verified prompt format. Increase context to 8K for longer chats.

The model files remain outside the app bundle and are shared across instances:

| Selectable model | File size |
| --- | ---: |
| Bonsai 1.7B | 490,163,968 bytes |
| MiniCPM5 2B Fable Agentic Q4_K_M | 1,561,319,776 bytes |
| Qwen3.5 2B Reasoning Q4_K_M | 1,280,835,840 bytes |
| Qwen3.5 2B Reasoning BF16 | 3,775,709,216 bytes |

The failed MiniCPM IQ2_XXS artifact remains on disk for reproducible comparison and is excluded from the GUI selector. [Model provenance and licenses](MODELS.md) records immutable revisions, download links, byte counts, and SHA-256 hashes. The [Bonsai onboarding evaluation](../../docs/BONSAI_ONBOARDING_EVALUATION.md) describes that checkpoint's observed limitations.

The application is an independent experiment. Its source and build recipe are committed; generated app bundles and model files remain ignored.

## Local verification

The v0.5.1 native build and strict ad-hoc signature verification passed. All 30 conversation/profile serialization checks and 38 offscreen UI checks passed, including model selection, separate chat restoration, New Chat isolation, generation locking, Qwen history formatting, BF16/Q4 conversation isolation, terminal control-token removal, and incomplete-answer handling. Editor checks cover Undo/Redo actions and shortcut bindings, bounded composer/transcript scrolling, system editor growth and overflow, Enter, and editing controls during generation. These UI checks exercise the real controls in an undisplayed window; they do not send system keyboard events or alter another running session.

The MiniCPM IQ2_XXS artifact produced incoherent output and failed arithmetic on both CPU and Metal. The Q4_K_M artifact passed with the same MiniCPM prompt format and Metal runtime, isolating the failure to the IQ2 artifact or its quantization.

Both MiniCPM Q4_K_M and Qwen Q4_K_M passed the application's literal `hi` greeting check, returned the expected arithmetic answer, and correctly added one to that answer on a follow-up containing real conversation history. The native runner greeting checks completed in about 0.64 and 0.61 seconds respectively. These are narrow functional checks. Empty replies preserve the draft and completed history and show a fixed error message.

The native GUI was also checked with both profiles: Command-Return sent `hi`, generation completed with a greeting and a closed thinking block, and no context-limit error occurred. Switching models restored the separate chats. The generated app occupies about 340 KiB; weights are loaded by the owned completion process for each request.

The full-precision Qwen BF16 profile passed the native runner's two-turn `42` to `43` arithmetic check and the actual GUI's `hi` greeting at its default temperature of 0.2. The greeting completed with a closed thinking block. Its temperature-zero CLI greeting check exhausted the 2048-token response budget without closing the thinking block. [Qwen's official model card](https://huggingface.co/Qwen/Qwen3.5-2B#best-practices) warns that the 2B model is prone to thinking loops and recommends temperature 1.0, top-p 0.95, top-k 20, presence penalty 1.5, and repetition penalty 1.0 for thinking. The playground keeps its shared comparison settings; Stop and the finite response budget remain available. An unfinished thinking response stays visible, restores the draft, and is excluded from completed history, including when stopped by the user.

Run model tests sequentially. The CLI model checks use temperature zero; the BF16 greeting currently fails as described above.

```sh
playground_binary='.build/bonsai-playground-full-qwen-v2/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground'
"$playground_binary" --greeting-test --profile minicpm-q4
"$playground_binary" --smoke-test --profile minicpm-q4
"$playground_binary" --greeting-test --profile qwen35
"$playground_binary" --smoke-test --profile qwen35
"$playground_binary" --greeting-test --profile qwen35-full
"$playground_binary" --smoke-test --profile qwen35-full
"$playground_binary" --conversation-self-test
"$playground_binary" --ui-self-test
```
