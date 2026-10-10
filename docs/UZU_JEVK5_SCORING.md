# Experimental Uzu JevK5 probability scoring

Status: native scoring implemented and smoke-tested; full JevK5 comparison pending model acquisition and conversion. This is an isolated Rust experiment. The installed JevK5 add-on continues to use its existing engine.

## Implementation

The tracked [extension](../experiments/uzu-rust/uzu-next-token-scoring.patch) adds synchronized next-token logits to pinned Uzu 0.6.1. It prefills exact caller-supplied token IDs and reads the final prompt position without generating a token. The [Rust worker](../experiments/uzu-rust/src/scoring.rs) exposes this through its stdin protocol, including candidate logits, full-vocabulary log probabilities, candidate probabilities, and timing metadata. Every request creates fresh model state.

The readout is BF16, matching Uzu's internal engine dtype. Candidate normalization uses f64 and the supplied temperature. JevK5's published letter temperature remains 1.22; its knockout temperature remains 0.93. The latter is inactive for the supported 2–16 candidate decisions. No temperature is refitted to the benchmark.

For candidate logits `z`, the decision probabilities are `softmax(z / 1.22)`. Applying the official temperature power transform to full-vocabulary log probabilities gives the same candidate-normalized result because the common vocabulary normalizer cancels. Tests independently check this formula, stable ordered argmax, boolean probability, confidence, and score expected value. Matching the formula does not imply that two engines return identical logits.

Build with `uv run --locked python experiments/uzu-rust/build_scoring.py --test`. The script applies the patch to a clean export of the pinned upstream revision, builds with Cargo's lockfile, and records an ignored build receipt. The scoring binary is `runtime/uzu-rust/scoring-target/release/meecp-uzu-worker`; the measured text-generation binary remains in `runtime/uzu-rust/target`.

## Comparison protocol

The frozen [protocol](../experiments/uzu-rust/jev-scoring/PROTOCOL.md) uses all 231 public JevBench v1.2 cases plus 12 separate synthetic checks. The public files contain gold decisions, without gold probability distributions. Report top-label ECE, multiclass Brier score, decision accuracy, expected-score accuracy, and ordinal error as a development-set screen.

The [model manifest](../experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json) pins the merged JevK5 v0.3 source and the installed Q8_0 reference. Uzu receives an unquantized BF16 Lalamo export. This differs in precision and conversion path from the Q8_0 GGUF, so differences cannot be attributed solely to engine implementation.

Require exact rendered prompts, input token IDs, option order, candidate token IDs, complete option coverage, and no truncation. Compare each probability and typed decision. The predefined strict tolerance is 0.001 absolute probability error; 0.01 is a separate descriptive close-match tolerance. Quality uses each case's first fresh call. Later measurements test stability.

Two resident rounds reverse engine load order. Fresh requests and immediate identical repeats have separate timing populations. llama.cpp may reuse only the immediately preceding identical token sequence; Uzu currently recomputes both. The llama.cpp readout uses one output token, while Uzu performs no generation. These timings describe decision requests, without implying generation throughput. Resident processes are launched serially and reaped before the next model is loaded.

Raw cases, token IDs, logits, probabilities, and decisions stay in owner-only ignored files. Tracked reports contain scalar aggregates, artifact identities, and counts. Aggregation verifies the configuration, artifact hashes, and private result seals.

## Reproduction

Acquire source artifacts with `uv run --locked python scripts/acquire_jev_uzu_models.py`. Install the pinned Lalamo checkout at `runtime/uzu-rust/lalamo` and sync its `uv.lock`. Run `scripts/convert_jev_uzu_model.py` through that checkout's locked environment; it verifies source hashes before conversion. Its only source metadata adaptation is `qwen3_5_text` to Lalamo's `qwen3_5` architecture name. Original source weights remain unchanged.

Prepare cases with `uv run --locked python scripts/benchmark_jev_uzu.py prepare`. Run the comparison with `uv run --locked python scripts/benchmark_jev_uzu.py run --uzu-model models/uzu-jevk5/bf16 --output test-artifacts/jev-uzu-screen`. Aggregate with `uv run --locked python scripts/benchmark_jev_uzu.py aggregate --run test-artifacts/jev-uzu-screen --report experiments/uzu-rust/jev-scoring/RESULTS.json`.

## Remaining scope

Complete full-model conversion, the paired scoring comparison, and the resulting performance/probability/calibration report. Host provider integration, authenticated scoring routes, MCP exposure, and reuse of identical scoring state remain future work. This experiment does not justify replacing the installed JevK5 engine.
