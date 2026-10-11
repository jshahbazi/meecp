# Experimental Uzu JevK5 probability scoring

Completed on 2026-10-10 on the development Mac. Rust Uzu now supports exact-token probability scoring, and the full JevK5 comparison is complete. Keep it experimental: fresh decisions are faster, but the probability-parity targets failed. The installed JevK5 add-on continues to use its existing engine.

The [main aggregate](../experiments/uzu-rust/jev-scoring/RESULTS.json) records the repeat performed with the background Rust build idle. The [initial aggregate](../experiments/uzu-rust/jev-scoring/RESULTS_INITIAL.json) preserves the first run. Each uses all 231 public cases and 12 synthetic checks, two engine-order-reversed rounds, and fresh plus immediate-repeat requests: 1,944 scoring measurements per run, 3,888 in total.

## Measured performance

The machine was an Apple M5 Max with 128 GiB RAM and macOS 26.7.1. The main run's resident decision wall times include tokenization, transport, and scoring; model load is excluded. Each table row has 486 samples, including the separate synthetic checks.

| Request | llama.cpp p50 / p95 | Uzu p50 / p95 |
| --- | ---: | ---: |
| Fresh | 133.08 / 1,257.36 ms | 75.72 / 725.48 ms |
| Identical repeat | 20.68 / 27.78 ms | 75.76 / 725.65 ms |

The median ratio of paired fresh wall times is **1.73× in Uzu's favor**. For identical repeats, llama.cpp is **3.77× faster** by the paired wall-time ratio. Uzu recomputes every request; llama.cpp reuses the immediately preceding identical sequence. Actual llama.cpp timing records show four evaluated prompt tokens on every repeat, versus 104–4,033 on fresh calls. These are request-latency measurements, without a generation-throughput claim. The reported llama.cpp one-token timer was 1 microsecond throughout, so it does not provide a useful separate decode-cost estimate.

Fresh paired speed ratios are 1.85× below 256 input tokens, 1.47× at 256–1,023, and 1.60× at 1,024–4,095. Inputs ranged from 104 to 4,033 tokens; the 4,096-plus bucket is empty. Sampled process peak RSS was 8.14–8.15 GiB for Uzu and 16.14–16.38 GiB for llama.cpp. RSS is a process measurement, without a claim about total system GPU memory. Recorded startup was approximately 0.98–1.00 seconds for Uzu and 0.70–0.71 seconds for llama.cpp, with filesystem caches warmed by artifact verification.

The engines ran serially and were completely reaped between loads. After the Mac restarted, MLX-Serve, the separate llama.cpp service, and installed JevK5 were stopped; no suspension or restoration was needed. During the initial run, a separate Rust compilation used about one CPU core, alongside macOS indexing and UI activity. The full protocol was repeated after that build finished, with Cargo/rustc checked absent during the repeat. Median wall times fell by 1.7–3.5% across the four engine/condition populations. The initial fresh paired ratio was 1.62×; the repeat's was 1.73×. Background activity and GPU utilization were not continuously instrumented, so this remains a local performance screen. Statistical aggregation ran after inference finished.

## Probability, quality, and calibration results

All measurements passed exact prompt/input-token/candidate-token parity, complete option coverage, independent calibration-formula checks within 1e-12, and typed-answer validation. There were no truncations. Both engines returned identical probabilities across their own fresh/repeat calls and rounds; the two full runs also produced identical scoring results.

On the 231 public cases, ordered argmax agreed on **229/231 (99.13%)**. Only **92/231** cases matched every option within 0.001, and **189/231** matched within 0.01. Thus 139 cases failed strict parity and 42 failed even the close-match tolerance. Mean option absolute error was 0.003045, or 0.305 percentage points; the largest was **0.066516, or 6.65 percentage points**. Maximum confidence difference was 0.065919, true/false probability difference 0.031726, and expected-score difference 0.037457 score units. The 99% argmax target passed; probability and typed-scalar parity targets failed.

| Public-set metric | llama.cpp | Uzu |
| --- | ---: | ---: |
| Argmax accuracy | 202/231 (87.45%) | 202/231 (87.45%) |
| Typed decision accuracy, score uses rounded expected value | 202/231 (87.45%) | 203/231 (87.88%) |
| Multiclass Brier sum, lower is better | 0.183197 | 0.185154 |
| Top-label ECE, 10 bins, lower is better | 0.023832 | 0.030787 |
| Rounded expected-score accuracy | 16/18 (88.89%) | 16/18 (88.89%) |
| Expected-score ordinal MAE | 0.164604 | 0.168760 |

Choice accuracy was 122/139 for llama.cpp and 123/139 for Uzu; true/false accuracy was 64/74 for both. Score argmax accuracy was 16/18 versus 15/18, while rounded expected-value accuracy was equal. An independent descriptive calculation using JevBench's score rounding therefore gives 202/231 versus 203/231 for mixed typed decisions. The protocol's primary quality comparison remains argmax accuracy. All 12 synthetic decisions were correct for both engines and are excluded from the public quality figures.

The Uzu-minus-llama paired differences and 20,000-draw bootstrap 95% intervals were: argmax accuracy 0.00 percentage points [−1.30, +1.30], Brier +0.001957 [−0.000035, +0.004096], and ECE +0.006955 [−0.011686, +0.021074]. Uzu's calibration point estimates were slightly worse. The predeclared point-estimate screening margins passed, but the accuracy and ECE intervals do not establish the corresponding noninferiority margins. Public development cases and missing gold probability distributions prevent a production-calibration claim.

The calibration formula and published temperatures match the official contract. The actual decision probabilities do not meet the parity tolerances. Uzu uses BF16 weights and a BF16 readout; the existing reference is a separately converted Q8_0 GGUF. Their cards identify JevK5 v0.3 lineage, but separate artifact pins do not cryptographically prove identical pre-quantization weights. A same-source, same-precision control and a readout-precision investigation would be needed to attribute differences to the engine itself.

## Implementation

The tracked [extension](../experiments/uzu-rust/uzu-next-token-scoring.patch) adds synchronized next-token logits to pinned Uzu 0.6.1. It prefills exact caller-supplied token IDs and reads the final prompt position without generating a token. The [Rust worker](../experiments/uzu-rust/src/scoring.rs) exposes this through its stdin protocol, including candidate logits, full-vocabulary log probabilities, candidate probabilities, and timing metadata. Every request creates fresh model state.

The readout is BF16, matching Uzu's internal engine dtype. Candidate normalization uses f64 and the supplied temperature. JevK5's published letter temperature remains 1.22; its knockout temperature remains 0.93. The latter is inactive for the supported 2–16 candidate decisions. No temperature is refitted to the benchmark.

For candidate logits `z`, the decision probabilities are `softmax(z / 1.22)`. Applying the official temperature power transform to full-vocabulary log probabilities gives the same candidate-normalized result because the common vocabulary normalizer cancels. Tests independently check this formula, stable ordered argmax, boolean probability, confidence, and score expected value. Matching the formula does not imply that two engines return identical logits.

Build with `uv run --locked python experiments/uzu-rust/build_scoring.py --test`. The script applies the patch to a clean export of the pinned upstream revision, builds the separate `scoring/Cargo.toml` with its own lockfile, and records an ignored build receipt. The scoring binary is `runtime/uzu-rust/scoring-target/release/meecp-uzu-worker`. The original generation manifest retains its unpatched Git dependencies and disables the scoring feature; its executable remains in `runtime/uzu-rust/target`.

## Comparison protocol

The frozen [protocol](../experiments/uzu-rust/jev-scoring/PROTOCOL.md) uses all 231 public JevBench v1.2 cases plus 12 separate synthetic checks. The public files contain gold decisions, without gold probability distributions. Report top-label ECE, multiclass Brier score, decision accuracy, expected-score accuracy, and ordinal error as a development-set screen.

The [model manifest](../experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json) pins the merged JevK5 v0.3 source and the installed Q8_0 reference. Uzu receives an unquantized BF16 Lalamo export. This differs in precision and conversion path from the Q8_0 GGUF, so differences cannot be attributed solely to engine implementation.

Require exact rendered prompts, input token IDs, option order, candidate token IDs, complete option coverage, and no truncation. Compare each probability and typed decision. The predefined strict tolerance is 0.001 absolute probability error; 0.01 is a separate descriptive close-match tolerance. Quality uses each case's first fresh call. Later measurements test stability.

Two resident rounds reverse engine load order. Fresh requests and immediate identical repeats have separate timing populations. llama.cpp may reuse only the immediately preceding identical token sequence; Uzu currently recomputes both. The llama.cpp readout uses one output token, while Uzu performs no generation. These timings describe decision requests, without implying generation throughput. Resident processes are launched serially and reaped before the next model is loaded.

Raw cases, token IDs, logits, probabilities, and decisions stay in owner-only ignored files. Tracked reports contain scalar aggregates, artifact identities, and counts. Aggregation verifies the configuration, artifact hashes, and private result seals.

## Reproduction

Acquire and verify source artifacts from the repository root:

```sh
uv run --locked python scripts/acquire_jev_uzu_models.py
uv run --locked python scripts/acquire_jev_uzu_models.py --offline
git clone https://github.com/trymirai/lalamo.git runtime/uzu-rust/lalamo
git -C runtime/uzu-rust/lalamo checkout --detach 831accb8252a6b6606c378048caeb44178f1a050
uv sync --project runtime/uzu-rust/lalamo --locked --no-dev
uv run --project runtime/uzu-rust/lalamo --locked --no-dev python scripts/convert_jev_uzu_model.py
```

Reuse an existing clean Lalamo checkout at that exact revision. The converter verifies source hashes before conversion and runs JAX on CPU. Its only source metadata adaptation is `qwen3_5_text` to Lalamo's `qwen3_5` architecture name. Original source weights remain unchanged.

Prepare cases with `uv run --locked python scripts/benchmark_jev_uzu.py prepare`. Run the comparison with `uv run --locked python scripts/benchmark_jev_uzu.py run --uzu-model models/uzu-jevk5/bf16 --output test-artifacts/jev-uzu-screen`. Aggregate with `uv run --locked python scripts/benchmark_jev_uzu.py aggregate --run test-artifacts/jev-uzu-screen --report NEW_REPORT_PATH`. Use a fresh output directory and report path for a new run; existing reports are never overwritten. The measured repeat used `test-artifacts/jev-uzu-screen-idle-build`.

Validation passed 94 focused Python tests, nine native scoring tests, three original generation-worker tests, a 12-case JevK5 GPU smoke check, and both complete paired runs. The earlier native boundary smoke also covered prompt lengths 1,023, 1,024, 1,025, and 2,049. No raw evaluation inputs or per-case outputs are tracked.

## Remaining scope

Host provider integration, authenticated scoring routes, MCP exposure, installed-package acceptance, and reuse of identical scoring state remain future work. Resolve the probability differences with suitable controls before considering a replacement. This experiment establishes a fresh-call performance gain for the tested artifacts; it does not justify replacing the installed JevK5 engine.
