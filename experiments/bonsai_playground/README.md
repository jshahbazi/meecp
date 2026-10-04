# Bonsai Playground

A small native macOS app for trying the downloaded Bonsai 1.7B model. Compose a message, optionally change the system instruction or sampling settings, and click Send. Completed user and assistant turns stay in memory for the current chat, so follow-up messages can use the conversation. New Chat clears the draft and conversation. The current system instruction is sent once with each request.

The app uses the existing model file and Homebrew `llama-completion`. It does not copy them, install a runtime, start a server, or change mcpme. Messages, conversation turns, and runtime diagnostics stay in memory; the app has no saved history or telemetry. Closing the app ends its own active generation.

Command-Return sends, Command-N starts New Chat, and Command-Z / Command-Shift-Z undo and redo edits in the message and system editors. Enter inserts a newline. The system editor grows from one to three visual lines and then scrolls. The message composer and conversation transcript remain in bounded, scrollable panes instead of expanding the window. If the model reports that the context is full, the draft and conversation are preserved; raise the Context setting and send again.

## Build and launch

Run from this worktree using its existing project Python environment. No Python dependencies are added.

```sh
/Users/johnshahbazian/development/mcpme/.venv/bin/python \
  experiments/bonsai_playground/build.py \
  --output .build/bonsai-playground-chat --open
```

The build creates an ad-hoc-signed Apple silicon app at `.build/bonsai-playground-chat/Bonsai Playground.app`. The `--open` option launches a new instance, preserving any older running session. Double-click the app to reopen it later. It requires macOS 14 or later and the existing local model/runtime paths; the path controls allow selecting another local installation.

Defaults use Metal, six CPU threads, the pinned GGUF's published ChatML prefix, 2K context, 512 output tokens, temperature 0.2, and seed 42. The [onboarding evaluation](../../docs/BONSAI_ONBOARDING_EVALUATION.md) describes this checkpoint's observed limitations. Increase context to 4K or 8K for longer prompts.

The application is an independent experiment. Its source and build recipe are committed; generated app bundles and model files remain ignored.

## Local verification

The v0.2.0 native build and strict ad-hoc signature verification passed. All 11 conversation serialization checks and 23 offscreen UI checks passed, covering ordered history, New Chat, Undo/Redo actions and shortcut bindings, bounded composer/transcript scrolling, system editor growth and overflow, Enter, and editing controls during generation. These UI checks exercise the real controls in an undisplayed window; they do not send system keyboard events or alter another running session.

The two-turn test using the application's model runner also passed: the initial arithmetic answer was `42` and the follow-up answer was `43`.

```sh
'.build/bonsai-playground-chat/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --smoke-test
'.build/bonsai-playground-chat/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --conversation-self-test
'.build/bonsai-playground-chat/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --ui-self-test
```
