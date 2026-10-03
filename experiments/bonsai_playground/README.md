# Bonsai Playground

A small native macOS app for trying the downloaded Bonsai 1.7B model. Paste a prompt, optionally change the system instruction or sampling settings, and click Send. Responses appear live and can be selected and copied. Stop ends the current generation. Each Send starts a fresh request.

The app uses the existing model file and Homebrew `llama-completion`. It does not copy them, install a runtime, start a server, or change mcpme. Prompts, responses, and runtime diagnostics stay in memory; the app has no saved history or telemetry. Closing the app ends its own active generation.

## Build and launch

Run from this worktree using its existing project Python environment. No Python dependencies are added.

```sh
/Users/johnshahbazian/development/mcpme/.venv/bin/python \
  experiments/bonsai_playground/build.py --open
```

The build creates an ad-hoc-signed Apple silicon app at `.build/bonsai-playground/Bonsai Playground.app`. Double-click that app to launch it again. It requires macOS 14 or later and the existing local model/runtime paths; the path controls allow selecting another local installation.

Defaults use Metal, six CPU threads, the pinned GGUF's published ChatML prefix, 2K context, 512 output tokens, temperature 0.2, and seed 42. The [onboarding evaluation](../../docs/BONSAI_ONBOARDING_EVALUATION.md) describes this checkpoint's observed limitations. Increase context to 4K or 8K for longer prompts.

The application is an independent experiment. Its source and build recipe are committed; generated app bundles and model files remain ignored.

## Local verification

On October 3, 2026, the native build and strict ad-hoc signature verification passed. A headless smoke test using the application's own runner returned the expected arithmetic answer in 0.502 seconds. The same arithmetic prompt entered through the GUI returned the expected answer in about 0.40 seconds. Send, settings expansion, and Stop during a longer generation were exercised through native UI automation; Stop restored the idle controls and no completion process remained.

```sh
'.build/bonsai-playground/Bonsai Playground.app/Contents/MacOS/BonsaiPlayground' --smoke-test
```
