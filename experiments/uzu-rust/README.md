# Experimental Uzu worker

This worker links Uzu's direct Metal language-model engine and Hanashi prompt renderer. It does not initialize the higher-level `uzu`/`nagare` engine, registry, telemetry, or download manager. The source is pinned to official Uzu 0.6.1 commit `8d01581474760bd1fc7f4a547ff1d02f7dada437`; `Cargo.lock` pins the original dependency graph. The separate scoring build uses [scoring/Cargo.toml](scoring/Cargo.toml) and its own lockfile, enabling `probability-scoring` and a prepared local upstream override. Its extension is retained as [uzu-next-token-scoring.patch](uzu-next-token-scoring.patch), applied to a clean export of that revision by [build_scoring.py](build_scoring.py). The original MIT license is retained in `licenses/LICENSE-UZU`.

Text generation remains scoped to the Qwen 3.5 experiment. It loads the supplied local `config.json`, `tokenizer.json`, and `model.safetensors`. It uses Hanashi's Qwen 3.5 field mappings with the model's exact exported chat template, the system text `You are a helpful assistant.`, and disabled thinking. Each request creates fresh state and uses greedy sampling. The separate scoring mode accepts exact causal-model token IDs with no chat rendering. Both modes reject a model directory containing a speculator.

## Build and checks

Use Apple silicon macOS, Rust 1.98 or newer, and Xcode's Metal Toolchain component. If Xcode reports that component missing, install it with `xcodebuild -downloadComponent MetalToolchain`.

```sh
CARGO_TARGET_DIR="$PWD/runtime/uzu-rust/target" cargo build --release --locked --manifest-path experiments/uzu-rust/Cargo.toml
CARGO_TARGET_DIR="$PWD/runtime/uzu-rust/target" cargo test --release --locked --manifest-path experiments/uzu-rust/Cargo.toml
uv run --locked python experiments/uzu-rust/build_scoring.py --test
```

The first two commands build and test the original text-generation worker at `runtime/uzu-rust/target/release/meecp-uzu-worker`, without any prepared local source dependency or scoring feature. The scoring helper builds `scoring/Cargo.toml` into `runtime/uzu-rust/scoring-target/release/meecp-uzu-worker`, preserving the original executable. Both directories are ignored. The native executables link Apple system frameworks and libraries and embed their compiled Metal kernels. The scoring helper locates the pinned revision in Cargo's Git cache or fetches that exact revision, exports it into an ignored directory, applies the tracked patch, and builds with `--locked` using `scoring/Cargo.lock`. It never changes Cargo's cached checkout. An ignored `runtime/uzu-rust/SCORING_BUILD_RECEIPT.json` records upstream archive, patch, lockfile, license, and executable hashes. `--prepare-only` prepares source without compiling; `--upstream-source PATH` uses an existing Git repository containing the pinned commit.

## Host-owned HTTP mode

The host starts the executable with `--meecp-config-fd FD`; `--mcpme-config-fd` is a compatibility alias. The inherited descriptor contains the endpoint, API key, generation, nonce, and model declaration. The worker consumes and closes it. Its working directory is the immutable package root; the model artifact is relative to that root.

The HTTP server binds only `127.0.0.1`. All routes require bearer authentication and the expected loopback Host before reading a body. An Origin, when present, must match the endpoint's loopback HTTP origin. Responses disable caching. Headers and body share one absolute five-second read deadline; receiving fragments does not extend it. The body limit is 32 KiB; generation accepts at most 8192 characters, 16 KiB of UTF-8 text, and 256 output tokens. The engine serializes requests with four waiting slots and rejects a full queue. A client disconnect does not make active model state available to another request before pending GPU work finishes.

- `GET /health` and `GET /props` return the ready flag and injected ownership/model identity.
- `POST /generate` accepts `{"text":"...","max_output_tokens":128}` and returns text, token counts, stop reason, and timing metadata.
- An exact tokenizer preflight reserves eight tokens in addition to requested output. Context overflow returns HTTP 400 with `{"error":"context_limit"}` before generation starts.

## Line protocol mode

For a standalone probe, start the worker with `--model-path MODEL_DIRECTORY`. It emits one readiness JSON object, then accepts one JSON request per stdin line containing `text`, `max_output_tokens`, and optional `context_tokens` (default 4096). The line protocol supports larger benchmark limits than the HTTP mode. Each request yields one result or a fixed error code. Do not persist or print response text in benchmark reports.

## Exact-token scoring mode

Start the scoring executable with `--score-model-path MODEL_DIRECTORY`. It loads the local causal-model export without Hanashi or any chat template. Readiness identifies `exact-token-scoring-v1`, the upstream revision, the scoring patch SHA-256, vocabulary size, and `fresh-state` cache policy.

The isolated stdin protocol accepts two operations, each returning one JSON reply. No scoring HTTP or MCP route is added.

- `{"operation":"tokenize","text":"...","add_special_tokens":false}` returns `token_ids`, `token_count`, their little-endian `u32` hash, vocabulary size, and tokenizer time. The model's tokenizer JSON handles explicit special-token strings. `add_special_tokens` controls tokenizer post-processing; it defaults to false. Compare the exact sequence with llama.cpp before inference.
- `{"operation":"score","input_token_ids":[...],"candidate_token_ids":[...],"context_tokens":8192,"temperature":1.22}` scores the exact input sequence. It returns candidate IDs in their requested order, raw `logits`, full-vocabulary normalized `log_probabilities`, candidate-normalized `candidate_probabilities`, the supplied temperature, counts/hash, timing metrics, and peak native memory. `cache_hit` and `truncated` are always false. Candidate count must be 2–16, IDs must be unique and in vocabulary, input must be nonempty and fit the declared context, context is bounded to 16384 and the model limit, and temperature must be finite and positive.

The upstream extension submits the same 1024-token prefill chunks and trailing-layer split as ordinary Uzu streaming. It reads only the final prompt position and waits for GPU completion before accessing BF16 readout. It does not sample a token, start a generated-token forward pass, or apply a generation penalty, grammar, or temperature to raw logits. Full-vocabulary log probabilities use a stable f64 log-sum-exp. Candidate probabilities use `softmax(candidate_logits / temperature)` in f64, equivalent to Jev's temperature power transform followed by candidate normalization. The caller must select the model's official calibration temperature and independently verify the formula and decision formatting.

Each score creates fresh state and destroys it after synchronized readout. Identical repeats recompute the prompt; changed inputs never reuse a prefix. Native scoring/readout failure fences the process after a sanitized error reply. Request content, token arrays, logits, and probabilities belong only to the private caller's protocol stream and must never become diagnostic logs or public benchmark data.

Scoring timings distinguish state creation, synchronized prefill, readout (native copy plus f64 normalization), score execution, and complete request execution. They exclude process load and stdin/stdout transport. The comparison harness measures end-to-end request latency separately. Prior text-generation timings include first-token sampling and cannot be interpreted as this scoring API's prefill timings.

## Timing and memory definitions

- `ttft_seconds`: request execution start through the first returned native token. It includes rendering, tokenization, state creation, and prefill.
- `prefill_seconds`: native stream creation start through the first returned token. Uzu submits GPU prefill asynchronously; measuring constructor return alone would omit GPU completion.
- `decode_seconds`: first returned token through final synchronization of queued GPU work. Stream teardown is included.
- `total_seconds`: request execution start through final text decoding.
- `prefill_tps`: prompt-token count divided by `prefill_seconds`. This is an approximate prefill rate through first-token availability; rendering, tokenization, and state creation occur before this interval.
- `decode_tps`: returned completion tokens after the first divided by decode time.

Completion-token counts include a returned stop token; response text excludes it. `peak_memory_bytes` is Uzu's process-lifetime peak native allocation count. Measure process RSS separately when comparing memory with llama.cpp.

The metadata includes SHA-256 of the rendered prompt's UTF-8 bytes and of concatenated little-endian `u32` prompt-token IDs. These support prompt/token parity checks without persisting prompts or token arrays. The worker writes no diagnostic logs, suppresses panic/Metal diagnostics on stderr, and disables Uzu's optional GPU capture environment settings.
