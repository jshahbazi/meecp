# Experimental Uzu text generation

This experiment adds a second installable text-generation package using Uzu's direct Rust Metal engine. The host continues to use the existing model-service process graph: a native engine and an authenticated Python MCP adapter, both owned and reaped by Meecp. The package exposes `text_generate` and `text_status` without changing host component contracts.

The implementation pins Uzu 0.6.1 at `8d01581474760bd1fc7f4a547ff1d02f7dada437` and locks the Rust and Python dependencies. The recent Python SDK release was blocked by the local dependency age policy. The direct Rust core avoids the SDK's registry/download/telemetry initialization and consumes already verified local model files. The package does not acquire models during startup.

## Scope

- Apple silicon, Metal, and the Qwen3.5-0.8B Mirai-L export only.
- One resident GPU package admitted through Meecp's existing exclusive policy.
- A 4096-token context, up to 256 output tokens, 8192 input characters and 16 KiB of input text.
- Greedy generation with thinking disabled, fresh model state for each request, one active request, and four waiting requests.
- A 4096 MiB declared memory reservation, authenticated loopback routes, generation/nonce/engine identity checks, and fail-closed retirement after uncertain native outcomes.

This is an opt-in experimental package. It is separate from the built-in CPU text-generation recipe. JevK5 is outside this experiment. Full Phase 4 acceptance, quality evaluation, additional model families, speculative decoding, concurrent resident GPU models, sleep/wake, pressure testing, and public distribution remain outside the measured scope.

## Reproduce

Run from the repository root on Apple silicon with managed CPython 3.12, `uv`, Rust 1.98, and Xcode with its Metal Toolchain component. Runtime, model, package, and raw acceptance artifacts stay in ignored directories. Model source revisions, sizes, hashes, licenses, and the separate conversion caveat are recorded in [MODEL_PROVENANCE.json](../experiments/uzu-rust/MODEL_PROVENANCE.json). Rust dependency notices are in [THIRD_PARTY.md](../experiments/uzu-rust/THIRD_PARTY.md).

```sh
uv run --locked python scripts/acquire_uzu_models.py
CARGO_TARGET_DIR="$PWD/runtime/uzu-rust/target" cargo build --release --locked --manifest-path experiments/uzu-rust/Cargo.toml
CARGO_TARGET_DIR="$PWD/runtime/uzu-rust/target" cargo test --release --locked --manifest-path experiments/uzu-rust/Cargo.toml
/usr/bin/clang -O2 -Wall -Wextra -o runtime/uzu-rust/meecp-reap scripts/meecp-reap.c
uv run --locked python scripts/build_uzu_package.py
uv run --locked --group dev python scripts/verify_uzu_package.py --output test-artifacts/uzu-package-acceptance.json
uv run --locked python scripts/benchmark_uzu.py --report test-artifacts/uzu-benchmark.json
```

The acquisition helper downloads only pinned artifacts and verifies both length and SHA-256 before publishing a file. It refuses to replace an existing mismatched artifact. Use `--offline` to verify existing files. The package builder also refuses to overwrite a destination. Move or remove an earlier generated package before rebuilding.

The benchmark needs the closed llama.cpp runtime at `runtime/llama-relocated/llama/llama-server`; pass `--llama-binary` to use another compatible server and retain its identity in the report. Both engines run sequentially with foreground process owners. Their processes are reaped before the next engine loads. One warmup is excluded per load, engine order is reversed in the second round, and every measured request uses fresh state with prompt caching disabled. Reports retain aggregate metrics and identity hashes without prompts, responses, token arrays, credentials, or exception dumps.

## Interpreting the comparison

The models are independently converted eight-bit artifacts: Mirai-L for Uzu and Q8_0 for llama.cpp. Their exact official source-weight revisions have not been verified. The comparison therefore measures each engine with its artifact; it cannot assign a difference solely to the engine or establish generated-output parity.

Rendered prompt hashes and input-token sequence hashes must match before a complete benchmark is accepted. End-to-end wall time is the primary comparable latency metric. It includes the loopback request, generation, and response decoding; the llama.cpp path additionally uses its tokenizer endpoint. Native prefill and decode timing boundaries differ between engines. Uzu's prefill measurement ends when the first token is returned, including asynchronous GPU completion and first-token work. Its decode throughput excludes that first token and includes stream teardown synchronization. Completion counts include a returned stop token where present. Native prefill values are diagnostic rather than exact cross-engine equivalents.

Startup measurements are repeated process starts with warm filesystem caches, not reboot-cold disk or shader-cache measurements. Process peak RSS includes resident engine memory and is recorded after native reaping; Uzu's native allocation counter is a different measurement. The benchmark is a small synthetic performance screen, not a text-quality or production-load test.

## Measurements on this Mac

Measured on 2026-10-10 with macOS 26.7.1, an Apple M5 Max, 18 CPU cores, 40 GPU cores, and 128 GiB of unified memory. The llama.cpp baseline is build `11146-7fe450e19305b828c199d602c23a8337aaa1f03b`; its native binary and Metal libraries match the existing signed runtime closure. Both engines used Metal, a 4096-token context and one active generation. Each row has six samples per engine across two reversed-order loads. All 36 measured generations returned 256 output tokens, and the input-token sequence hashes matched for every workload.

| Input tokens | llama.cpp median wall time | Uzu median wall time | Uzu time reduction | llama.cpp decode tokens/s | Uzu decode tokens/s |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 51 | 0.894 s | 0.613 s | 31.4% | 292 | 425 |
| 436 | 0.907 s | 0.642 s | 29.2% | 292 | 416 |
| 1204 | 0.974 s | 0.678 s | 30.3% | 287 | 410 |

Uzu's measured decode throughput was 42–46% higher. Native engine peak RSS was 1.047–1.048 GiB for Uzu and 1.087–1.088 GiB for llama.cpp; these figures exclude the host gateway and MCP adapter. Startup ranged from 0.734–1.449 s for Uzu and 0.601–0.604 s for llama.cpp. The [aggregate benchmark record](../experiments/uzu-rust/BENCHMARK_RESULTS.json) retains every timing sample, artifact and harness hashes, token-sequence identity hashes, and reaping results.

These results justify keeping Uzu available as an experimental engine. They do not establish a general replacement for llama.cpp: model coverage, output quality, longer contexts, concurrent load, memory pressure, and distribution still need evaluation.

## Installed-package acceptance

The [installed-package acceptance record](../experiments/uzu-rust/ACCEPTANCE.json) passed through an isolated Meecp Supervisor and real MCP SDK clients. The package was relocated to a path with spaces, installed into a fresh host state, and kept running after its source location moved. Exact tool schemas, generation identity, repeated greedy output, five invalid/boundary requests, and eleven transport rejection probes passed. Actual SDK cancellation returned to the client while the owned graph and GPU reservation remained present until native generation settled. Explicit Stop reaped the engine, adapter and active gateway; host close also reaped the stopped gateway. A restarted host loaded a fresh generation and completed another call. Final cleanup released every reservation and reaped the full graph.

The tested package declares 4096 MiB and uses a private standalone Python runtime with locked adapter dependencies. Its package digest is `0fe07a4d15927fbd4053311428c4f114928f8ba92fe14b21ba9cd4c842bc3229`. The builder applies an ad-hoc signature to the copied native executable, so its package binary hash differs from the original benchmark executable; both identities are retained in their respective records. This is local experimental packaging, not a Developer ID signed distribution.

The focused adapter and existing text-generation transport/cancellation suite passed 63 checks; the Rust protocol/parser suite passed three checks. The acceptance harness did not test simultaneous resident GPU packages, queue saturation, pressure, crash recovery, sleep/wake, or the complete Phase 4 regression matrix. Those remain explicit follow-up scope.

Use Meecp's prepared-package import flow with `dist/uzu-textgen-experimental` to install it in the normal app after reviewing its declaration. This experiment exercised a separate host state; it did not change the user's installed app slots or default engine.
