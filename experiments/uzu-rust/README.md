# Experimental Uzu worker

This worker links Uzu's direct Metal language-model engine and Hanashi prompt renderer. It does not initialize the higher-level `uzu`/`nagare` engine, registry, telemetry, or download manager. The source is pinned to official Uzu 0.6.1 commit `8d01581474760bd1fc7f4a547ff1d02f7dada437`; `Cargo.lock` pins the dependency graph.

It is scoped to the Qwen 3.5 text-generation experiment. It loads the supplied local `config.json`, `tokenizer.json`, and `model.safetensors`. It uses Hanashi's Qwen 3.5 field mappings with the model's exact exported chat template, the system text `You are a helpful assistant.`, and disabled thinking. Each request creates fresh state and uses greedy sampling. A model directory containing a speculator is rejected.

## Build and checks

Use Apple silicon macOS, Rust 1.98 or newer, and Xcode's Metal Toolchain component. If Xcode reports that component missing, install it with `xcodebuild -downloadComponent MetalToolchain`.

```sh
CARGO_TARGET_DIR="$PWD/runtime/uzu-rust/target" cargo build --release --locked --manifest-path experiments/uzu-rust/Cargo.toml
CARGO_TARGET_DIR="$PWD/runtime/uzu-rust/target" cargo test --release --locked --manifest-path experiments/uzu-rust/Cargo.toml
```

The output executable is `runtime/uzu-rust/target/release/meecp-uzu-worker`. That directory is ignored. The native executable links Apple system frameworks and libraries; it embeds its compiled Metal kernels.

## Host-owned HTTP mode

The host starts the executable with `--meecp-config-fd FD`; `--mcpme-config-fd` is a compatibility alias. The inherited descriptor contains the endpoint, API key, generation, nonce, and model declaration. The worker consumes and closes it. Its working directory is the immutable package root; the model artifact is relative to that root.

The HTTP server binds only `127.0.0.1`. All routes require bearer authentication and the expected loopback Host before reading a body. An Origin, when present, must match the endpoint's loopback HTTP origin. Responses disable caching. Headers and body share one absolute five-second read deadline; receiving fragments does not extend it. The body limit is 32 KiB; generation accepts at most 8192 characters, 16 KiB of UTF-8 text, and 256 output tokens. The engine serializes requests with four waiting slots and rejects a full queue. A client disconnect does not make active model state available to another request before pending GPU work finishes.

- `GET /health` and `GET /props` return the ready flag and injected ownership/model identity.
- `POST /generate` accepts `{"text":"...","max_output_tokens":128}` and returns text, token counts, stop reason, and timing metadata.
- An exact tokenizer preflight reserves eight tokens in addition to requested output. Context overflow returns HTTP 400 with `{"error":"context_limit"}` before generation starts.

## Line protocol mode

For a standalone probe, start the worker with `--model-path MODEL_DIRECTORY`. It emits one readiness JSON object, then accepts one JSON request per stdin line containing `text`, `max_output_tokens`, and optional `context_tokens` (default 4096). The line protocol supports larger benchmark limits than the HTTP mode. Each request yields one result or a fixed error code. Do not persist or print response text in benchmark reports.

## Timing and memory definitions

- `ttft_seconds`: request execution start through the first returned native token. It includes rendering, tokenization, state creation, and prefill.
- `prefill_seconds`: native stream creation start through the first returned token. Uzu submits GPU prefill asynchronously; measuring constructor return alone would omit GPU completion.
- `decode_seconds`: first returned token through final synchronization of queued GPU work. Stream teardown is included.
- `total_seconds`: request execution start through final text decoding.
- `prefill_tps`: prompt-token count divided by `prefill_seconds`. This is an approximate prefill rate through first-token availability; rendering, tokenization, and state creation occur before this interval.
- `decode_tps`: returned completion tokens after the first divided by decode time.

Completion-token counts include a returned stop token; response text excludes it. `peak_memory_bytes` is Uzu's process-lifetime peak native allocation count. Measure process RSS separately when comparing memory with llama.cpp.

The metadata includes SHA-256 of the rendered prompt's UTF-8 bytes and of concatenated little-endian `u32` prompt-token IDs. These support prompt/token parity checks without persisting prompts or token arrays. The worker writes no diagnostic logs, suppresses panic/Metal diagnostics on stderr, and disables Uzu's optional GPU capture environment settings.
