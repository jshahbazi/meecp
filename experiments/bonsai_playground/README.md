# Bonsai Playground

A small native macOS app for trying Bonsai 1.7B and MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic, IQ2_XXS. Choose a model above the conversation, compose a message, and click Send. Each model keeps its own completed turns, transcript, draft, file selection, and response budget in memory. Switching back restores that chat. New Chat clears only the selected model's draft and conversation. The current system instruction is sent once with each request.

The app uses the downloaded model files and Homebrew `llama-completion`. Messages, conversation turns, and runtime diagnostics stay in memory; the app has no saved history or telemetry. Closing the app ends its own active generation. The model selector is disabled while a reply is being generated.

Command-Return sends, Command-N starts New Chat, and Command-Z / Command-Shift-Z undo and redo edits in the message and system editors. Enter inserts a newline. The system editor grows from one to three visual lines and then scrolls. The message composer and conversation transcript remain in bounded, scrollable panes instead of expanding the window. If the model reports that the context is full, the draft and conversation are preserved; raise the Context setting and send again.

## Build and launch

Run from this worktree using its existing project Python environment. No Python dependencies are added.

```sh
uv run --locked --no-sync python experiments/bonsai_playground/build.py \
  --output .build/bonsai-playground-models --open
```

The build creates an ad-hoc-signed Apple silicon app at `.build/bonsai-playground-models/Bonsai Playground.app`. The `--open` option launches a new instance, preserving any older running session. Double-click the app to reopen it later. It requires macOS 14 or later and the existing local model/runtime paths; the path controls allow selecting another local installation.

Both profiles use Metal, six CPU threads, 2K context, temperature 0.2, and seed 42. The system instruction, sampling settings, context, and runtime are shared, so you can compare the models under the same settings. Bonsai defaults to 512 output tokens and its published empty thinking prefill. MiniCPM defaults to 2048 output tokens to allow room for thinking; reasoning is streamed with the response and retained in that model's chat. Each profile uses its own prompt format. Increase context to 4K or 8K for longer chats.

The existing Bonsai file occupies 490,163,968 bytes. The downloaded MiniCPM IQ2_XXS file occupies 799,476,064 bytes and is shared across app instances. Models remain outside the app bundle. [Model provenance and licenses](MODELS.md) records immutable revisions, download links, byte counts, and SHA-256 hashes. The [Bonsai onboarding evaluation](../../docs/BONSAI_ONBOARDING_EVALUATION.md) describes that checkpoint's observed limitations.

The application is an independent experiment. Its source and build recipe are committed; generated app bundles and model files remain ignored.

## Local verification

The v0.3.0 native build and strict ad-hoc signature verification passed. All 24 conversation/profile serialization checks and 30 offscreen UI checks passed, including model selection, separate chat restoration, New Chat isolation, generation locking, and terminal control-token removal. The earlier editor checks cover Undo/Redo actions and shortcut bindings, bounded composer/transcript scrolling, system editor growth and overflow, Enter, and editing controls during generation. These UI checks exercise the real controls in an undisplayed window; they do not send system keyboard events or alter another running session.

The two-turn Bonsai runner test passed. The exact MiniCPM quant loaded and exited normally, but failed the arithmetic check across the tested generation prefixes and sampling settings. Its failure has not been attributed to a proven cause. This quant remains selectable for local testing; the result does not establish onboarding quality. An empty reply preserves the draft and completed history and shows a fixed error message.

The model runner smoke test checks an arithmetic answer and a follow-up that adds one to the earlier answer. Run each model separately:

```sh
'.build/bonsai-playground-models/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --smoke-test --profile bonsai
'.build/bonsai-playground-models/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --smoke-test --profile minicpm
'.build/bonsai-playground-models/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --conversation-self-test
'.build/bonsai-playground-models/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --ui-self-test
```
