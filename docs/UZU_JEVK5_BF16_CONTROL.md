# JevK5 same-checkpoint BF16 engine control

Completed on 2026-10-10 on the development Mac. Matching both engines to the same BF16 checkpoint reduced the maximum option probability gap from 6.65 to 2.75 percentage points. Probability and typed-scalar parity still failed. Public argmax accuracy was equal; Uzu's Brier and ECE point estimates remained higher. The recorded decision timings favored Uzu for fresh requests and llama.cpp for identical repeats, with substantial timing variation across the interrupted session.

The [complete BF16 aggregate](../experiments/uzu-rust/jev-scoring/RESULTS_BF16.json) records all 231 public cases, 12 separate synthetic checks, two rounds, and 1,944 scoring measurements. The [independent audit and host record](../experiments/uzu-rust/jev-scoring/HOST_ISOLATION_BF16.json) binds the aggregate, private seals, migration ledger, and final isolation receipt; it records zero verification failures and the per-round timing limits. The [frozen BF16 protocol](../experiments/uzu-rust/jev-scoring/PROTOCOL_BF16.md) defines the comparison. The [BF16 conversion manifest](../experiments/uzu-rust/jev-scoring/BF16_CONTROL_PROVENANCE.json) binds both exports to the unchanged [Uzu source/export manifest](../experiments/uzu-rust/jev-scoring/MODEL_PROVENANCE.json). The historical [Q8_0 report](../experiments/uzu-rust/jev-scoring/RESULTS.json) and [earlier scoring analysis](UZU_JEVK5_SCORING.md) remain separate records.

## Artifact provenance

Both engines derive from `alibiserikbay/JevK5` revision `c4f7fdb3aeab5582336406e78d3bef11bf98833d`, an Apache-2.0 merged Qwen3.5-4B checkpoint. The original `model.safetensors` is 8,411,558,400 bytes with SHA-256 `13824e47f2e40fe052f06943976cf742cb366ba305741a111e75a8ebae907a9c`. Its header contains 426 BF16 tensors and no multi-token prediction tensors. All pinned source files are verified before and after llama.cpp conversion; source weights remain unchanged.

| Artifact | Format and conversion |
| --- | --- |
| Uzu | Existing unquantized BF16 export through Lalamo revision `831accb8252a6b6606c378048caeb44178f1a050`; export file hashes and recipe are unchanged |
| llama.cpp | GGUF from official converter revision `86d86ed4396b4130922f7b9af26e3d9fc11a591b`, with `--outtype bf16 --no-mtp`; 249 BF16 tensors and 177 converter-selected F32 tensors, 426 total |

The GGUF is `models/uzu-jevk5/JevK5-4B-v0.3-BF16.gguf`, 8,424,393,504 bytes, SHA-256 `9fb252465834f2113eb74dd4fc4240ebf3cfab17ab48c0a23a615bbc81b07dc4`. It contains no quantized tensor types. The BF16 control manifest records the source, output, converter archive, every converter Python source file, wrapper recipe, dependency lock, and project hashes. The converter's [MIT license](../experiments/uzu-rust/jev-scoring/licenses/LLAMA_CPP_CONVERTER_LICENSE) is tracked.

The [conversion wrapper](../scripts/convert_jev_llama_bf16.py) exports immutable Git objects from the pinned converter revision. It excludes unrelated changes in the user's existing llama.cpp checkout. It rejects altered source files, symlinked source artifacts, additional weight shards, and unverified shard indexes that could redirect the upstream loader to a different checkpoint. A separate [locked Python 3.12 project](../experiments/uzu-rust/jev-scoring/llama-converter/pyproject.toml) supplies dependencies, including PyTorch 2.11.0 and Transformers 4.57.6. Conversion runs on CPU with upstream console output discarded.

The source configuration declares one multi-token prediction layer despite containing no MTP weights. The converter's default would describe a nonexistent 33rd block. Explicit `--no-mtp` produces the checkpoint's actual 32 blocks; verification requires `qwen35.block_count=32` and absent or zero `nextn_predict_layers`. Both engines score the base checkpoint without MTP.

## Scoring and measurement

The control retains all 231 public JevBench v1.2 cases: 48 easy, 72 original, and 111 hard. Twelve authored known-answer synthetic cases are reported separately. No cases are selected using model outputs. The official prompt, option order, letter temperature 1.22, and inactive 0.93 knockout temperature remain fixed. Context is 8,192 tokens.

Accepted engine pairs require identical rendered prompts, exact input and candidate token IDs, complete candidate coverage, finite scores, and no truncation. Uzu's probabilities must independently equal candidate softmax at temperature 1.22 within `1e-12`. Typed decisions preserve stable ordered argmax, confidence, true probability, and expected score. The engineering tolerances remain 0.001 for strict probability parity and 0.01 for descriptive close matches; the argmax agreement target remains 99%.

Two resident rounds reverse engine load order. Each of the 243 cases receives a fresh request followed immediately by an identical repeat. Four engine blocks yield 1,944 scoring measurements. Only the first fresh observation per public case contributes to quality and calibration; later observations test stability. llama.cpp can reuse the immediately preceding identical sequence, while Uzu creates fresh scoring state for every call.

The engines run serially, with complete owned-process cleanup before the next load. Startup is excluded from resident decision latency. The harness binds the Uzu binary and model, build receipt, llama.cpp executable and library closure, and process reaper to the historical Q8_0 report. The llama.cpp GGUF is the controlled runtime artifact change. Aggregation verifies configuration, dataset, provenance, model, runtime, and private result seals.

## Results

All accepted measurements passed exact prompt/input-token/candidate-token parity, complete option coverage, independent calibration-formula checks within `1e-12`, and typed-answer validation. No inputs were truncated. Each engine's probabilities were identical across its own fresh/repeat calls and rounds: maximum within-engine probability difference was zero, with zero argmax disagreements. An independent comparison also found all 972 Uzu probability outputs identical to the historical Q8_0 control's Uzu outputs.

On public cases, ordered argmax agreed on **229/231 (99.13%)**, meeting the 99% screening target. Every option matched within 0.001 on **98/231** cases and within 0.01 on **199/231**. Therefore, 133 cases failed strict parity and 32 failed the close-match tolerance. Mean option absolute difference was **0.002262**, or **0.226 percentage points**; maximum difference was **0.027507**, or **2.75 percentage points**. Mean L1 distance was 0.008314 and mean total variation was 0.004157.

Maximum confidence difference was **0.027391**, true/false probability difference **0.024989**, and expected-score difference **0.049209 score units**. All three maximum typed-scalar differences exceeded the 0.001 parity target. All 12 synthetic argmax decisions were correct for both engines and are excluded from public quality. Seven synthetic cases met strict probability parity, 11 met the close tolerance, and their maximum option difference was 0.010092. The separate initial 12-case native GPU smoke check passed the same token/decision checks, and its owned processes were reaped.

| Public-set metric | llama.cpp BF16 | Uzu BF16 |
| --- | ---: | ---: |
| Argmax accuracy | 202/231 (87.45%) | 202/231 (87.45%) |
| Typed decision accuracy, score uses rounded expected value | 202/231 (87.45%) | 203/231 (87.88%) |
| Multiclass Brier sum, lower is better | 0.183678 | 0.185154 |
| Top-label ECE, 10 bins, lower is better | 0.023855 | 0.030787 |
| Rounded expected-score accuracy | 16/18 (88.89%) | 16/18 (88.89%) |
| Expected-score ordinal MAE | 0.165282 | 0.168760 |

Choice accuracy was 122/139 for llama.cpp and 123/139 for Uzu; true/false accuracy was 64/74 for both. Score argmax accuracy was 16/18 versus 15/18, while rounded expected-value accuracy was 16/18 for both. The mixed typed-decision count above uses that score rounding; primary protocol accuracy uses argmax. Both engines scored 48/48 easy, 68/72 original, and 86/111 hard cases correctly under the argmax definition.

| Uzu minus llama.cpp | Point difference | Paired bootstrap 95% interval |
| --- | ---: | ---: |
| Argmax accuracy | 0.00 percentage points | −1.30 to +1.30 percentage points |
| Brier | +0.001476 | +0.000044 to +0.002992 |
| ECE | +0.006932 | −0.012167 to +0.021444 |

The bootstrap used 20,000 paired draws and seed 20261010. All point estimates met the predefined accuracy/Brier/ECE screening margins. The Brier interval fits its 0.01 margin and lies above zero; the accuracy and ECE intervals cross their respective noninferiority margins. These results do not establish noninferiority for all metrics or production calibration. The formula and temperatures match the official calibration contract; the engines' actual decision probabilities remain different.

Compared with the historical Q8_0 reference, maximum option difference fell from 0.066516 to 0.027507 and mean difference from 0.003045 to 0.002262. Strict matches rose from 92 to 98 cases, close matches from 189 to 199, and argmax agreement remained 229/231. The ECE difference barely changed: +0.006955 with Q8_0 and +0.006932 with BF16. Improvements were not uniform across typed scalars: maximum expected-score difference increased from **0.037457 to 0.049209 score units**. The contemporaneous BF16 pair is the primary controlled comparison.

## Measured performance

The machine was an Apple M5 Max with 128 GiB RAM and macOS 26.7.1. Resident wall time includes prompt tokenization, transport, and scoring. Model startup, model/runtime hash verification, and initial candidate-letter pretokenization are excluded. There was no explicit scoring warmup; the first fresh observation is included. Each pooled row has 486 samples, including the synthetic checks.

| Request | llama.cpp BF16 p50 / p95 | Uzu BF16 p50 / p95 |
| --- | ---: | ---: |
| Fresh | 135.22 / 1,044.30 ms | 83.69 / 854.43 ms |
| Identical repeat | 25.96 / 30.96 ms | 83.87 / 854.06 ms |

The median paired fresh wall-time ratio was **1.55× in Uzu's favor**. On identical repeats, llama.cpp was **3.27× faster** by the reciprocal paired wall-time ratio. Fresh ratios by input-token bucket were 1.83× below 256, 1.32× at 256–1,023, and 1.34× at 1,024–4,095; the respective pair counts were 266, 132, and 88. Inputs ranged from 104 to 4,033 tokens, leaving the 4,096-plus bucket empty.

All 486 llama.cpp repeats reported cache reuse and actually evaluated exactly four prompt tokens. Fresh llama.cpp requests evaluated the full 104–4,033-token input. Uzu reported zero cache hits and created fresh state for both request types. llama.cpp's reported one-token timer was consistently one microsecond, so it does not provide a useful independent decode-cost estimate.

| Round and request | llama.cpp p50 / p95 | Uzu p50 / p95 | Median paired llama.cpp/Uzu ratio |
| --- | ---: | ---: | ---: |
| Round 1 fresh | 104.30 / 934.53 ms | 68.26 / 689.52 ms | 1.623 |
| Round 1 repeat | 25.25 / 30.38 ms | 68.04 / 738.53 ms | 0.366 |
| Round 2 fresh | 142.92 / 1,082.61 ms | 98.54 / 919.50 ms | 1.451 |
| Round 2 repeat | 26.35 / 31.27 ms | 98.15 / 966.57 ms | 0.262 |

Each per-round request row has 243 pairs. Both engines' fresh p50 latency was higher in round 2: **37.0% for llama.cpp and 44.4% for Uzu**. This is material host/session drift. The pooled numbers summarize the variation and provide preliminary local timing evidence; they do not establish a constant speed ratio under a steady machine state.

Recorded startup was 0.91–1.28 seconds for llama.cpp and 1.05–1.07 seconds for Uzu. File verification warmed filesystem caches before loading. Sampled process peak RSS was **19.91–20.07 GiB** for llama.cpp and **8.13 GiB** for Uzu. RSS describes the process measurement and does not establish total system GPU memory use.

## Session interruption and accepted data

The accepted measurements span an interrupted session. Three complete blocks from `test-artifacts/jev-uzu-screen-bf16-controlled` were migrated byte-for-byte into `test-artifacts/jev-uzu-screen-bf16-resumed`: round 1 llama.cpp, round 1 Uzu, and round 2 Uzu. Each contains 486 rows for all 243 cases, with settled process ownership. The copied configuration, dataset, pre-run seal, and block hashes match their source files. No partial rows, partial markers, or old post-run seal were retained.

Independent compiler activity interrupted the incomplete final llama.cpp block. Earlier partial or contended attempts were excluded following external model/compiler activity. The entire missing round 2 llama.cpp block was rerun after isolation, preserving the frozen configuration and dataset; a new post-run seal binds all four accepted blocks. Run exclusions were triggered by external activity, and every public case remains in the accepted result.

The ignored migration ledger is `test-artifacts/jev-uzu-timing/BF16_BLOCK_MIGRATION.json`; the final-block isolation receipt is `test-artifacts/jev-uzu-timing/BF16_RESUMED_ISOLATION.json`. The latter records a successful benchmark exit and 19 process samples with zero external model services and zero compilers observed. **Those checks cover only the resumed final llama.cpp block.** Per-block monitoring receipts were not preserved for the three retained blocks, so their file integrity is proven while isolation across all four blocks cannot be independently verified. The final controller records restoration of its one suspended process. A later identity audit found all 11 recorded BF16 suspensions had exited, with no surviving stopped processes. Other macOS/UI activity remained; the largest sampled `ps` average CPU reading for one background process was 92.1%. Five-second polling can miss shorter activity, and these checks do not continuously measure GPU use or thermal state.

The accepted timing population is therefore **not one continuous clean timing window**. Session gaps, intervening discarded attempts, thermal changes, and UI/background activity remain plausible timing confounds. Both accepted rounds still favor Uzu for fresh requests and llama.cpp for repeats. Probability outputs were exactly stable across the accepted blocks, supporting the residual probability/calibration finding despite the timing limitations.

## Interpretation limits

Matching the original checkpoint and BF16 storage removes the previous comparison's checkpoint-lineage and Q8_0 quantization confounds. Residual differences can still arise from export representation, runtime operations, accumulation, and final-logit precision. Uzu's existing BF16 readout is retained. This control does not independently establish a causal role for that readout.

The official GGUF converter first promotes source tensors to F32 for representation changes. It adds one to appropriate Qwen norm weights, transforms `A_log` to `-exp(A_log)`, and reorders linear-attention value heads. It retains some parameters, including norms and convolution weights, as F32. These intended conversions preserve the model's parameterization while using different stored layouts and arithmetic from Uzu. BF16 matrix weights alone do not establish identical computations.

JevBench supplies gold decisions without gold probability distributions. Multiclass Brier sum and 10-bin top-label ECE describe calibration on these public development cases. The predeclared paired bootstrap uses 20,000 draws and seed 20261010; accuracy, Brier, and ECE screening margins remain 1 percentage point, 0.01, and 0.01 respectively. Point estimates within those margins do not establish noninferiority when confidence intervals cross the margins.

Decision latency includes tokenization, transport, and scoring. llama.cpp computes one output token to expose log probabilities; Uzu reads the final prompt logits without generation. These measurements do not establish generation throughput. Process RSS does not measure total system GPU memory, and a local benchmark cannot establish performance on other hardware or under concurrent workloads.

This experimental control does not modify the installed JevK5 MCP add-on. Host scoring routes, provider integration, MCP exposure, installed-package acceptance, and Uzu scoring-state reuse remain future work.

## Reproduction

The measured run uses the existing verified artifacts on the development Mac. Replay requires those unchanged inference artifacts, the sealed source/export manifests, and the original baseline report. The BF16 profile deliberately rejects a rebuilt or substituted runtime whose hashes differ from that baseline.

The original fresh-state GGUF conversion recipe was:

```sh
uv sync --project experiments/uzu-rust/jev-scoring/llama-converter --locked --no-dev
uv run --project experiments/uzu-rust/jev-scoring/llama-converter --locked --no-dev python scripts/convert_jev_llama_bf16.py --converter-repository /Users/johnshahbazian/.unsloth/llama.cpp
```

The repository supplied to the wrapper must contain the pinned converter revision. The wrapper uses `git archive` at that revision, so the checkout's current branch and unrelated modifications do not enter the export. Source acquisition and the existing Uzu export recipe are documented in the [original scoring report](UZU_JEVK5_SCORING.md#reproduction).

The converter intentionally refuses an existing output or BF16 control manifest. Its recorded conversion command describes the fresh state before the manifest was created. For regeneration, use an isolated reproduction checkout whose GGUF output is absent. Back up its tracked `BF16_CONTROL_PROVENANCE.json` under ignored `test-artifacts/`, remove the original manifest in that reproduction checkout, and run the wrapper. Compare the recreated manifest and GGUF SHA-256 with the recorded copy. Preserve the historical record in the original checkout. Keep this preparation outside any timed inference run.

An experiment with rebuilt binaries or another machine needs its own explicitly declared baseline and protocol. It must preserve artifact identities within that experiment; the existing BF16 profile rejects silent runtime changes to this recorded control.

Run a fresh comparison using the verified local artifacts:

```sh
uv run --locked python scripts/benchmark_jev_uzu.py run --llama-reference bf16 --uzu-model models/uzu-jevk5/bf16 --gguf models/uzu-jevk5/JevK5-4B-v0.3-BF16.gguf --output test-artifacts/jev-uzu-screen-bf16-replay
uv run --locked python scripts/benchmark_jev_uzu.py aggregate --run test-artifacts/jev-uzu-screen-bf16-replay --report experiments/uzu-rust/jev-scoring/RESULTS_BF16_REPLAY.json
```

Use a new run directory and report path for a subsequent replay. Existing reports are never overwritten. Resume accepts only a matching configuration with complete sealed engine blocks; a partial block requires a new output directory. Run no other GPU inference or heavy compilation during timing. Temporarily suspend authorized external model services if present and restore them afterward.

Validation before timing passed **139 focused Python tests**, including 21 authored converter validation cases. The 12-case BF16 native GPU smoke check and all four accepted engine blocks completed, with process ownership settled. Raw prompts, token IDs, logits, probabilities, decisions, and responses remain in owner-only ignored `test-artifacts/` files; tracked reports contain hashes, scalar aggregates, and counts.
