# Optimized Uzu versus llama.cpp: complete BF16 population

The complete 243-case BF16 comparison passed acquisition and independent audit on 2026-10-11. Optimized Uzu was **1.52–1.65x faster for fresh decisions** in two counterbalanced quartets, preserving the original Uzu probabilities, calibration and typed decisions exactly. llama.cpp remained faster for identical cached repeats. The cross-engine probability and calibration differences remain. The [fixed protocol](../experiments/uzu-rust/jev-scoring/OPTIMIZED_BF16_PROTOCOL.md), [result](../experiments/uzu-rust/jev-scoring/OPTIMIZED_BF16_RESULTS.json) and [independent audit](../experiments/uzu-rust/jev-scoring/OPTIMIZED_BF16_AUDIT.json) retain the scope and evidence.

## Measured decision latency

The development Mac was Apple M5 Max with 128 GiB RAM, macOS 26.7.1. All eight serial loads completed: 3,888 measured responses and 24 checked warmups. Each engine contributes 972 fresh and 972 repeat observations. The primary comparison is the ratio of mean case wall latency within each quartet; values above one favor Uzu.

| Condition | Quartet 1, llama/Uzu ratio | Quartet 2, llama/Uzu ratio | Median quartet ratio |
| --- | --- | --- | --- |
| Fresh | 1.520680 | 1.652654 | 1.586667 |
| Identical repeat | 0.125720 | 0.139141 | 0.132431 |

The fresh median quartet ratio corresponds to about **37.0% lower Uzu latency**. Public-only ratios are 1.519111 and 1.647857. The median paired case ratios are 1.672267 and 1.761671; those describe a different weighting from the primary ratio of means. Both quartets favor Uzu for fresh input in every populated token bucket.

| Wall latency | llama.cpp p50 / p95 | Uzu p50 / p95 |
| --- | --- | --- |
| Fresh | 157.01 / 1,254.12 ms | 87.80 / 842.17 ms |
| Identical repeat | 26.93 / 34.88 ms | 87.11 / 825.35 ms |

llama.cpp benefits strongly from identical-input reuse; Uzu deliberately repeats full fresh-state scoring. The repeat median quartet ratio corresponds to roughly 7.55x lower mean latency for llama.cpp. The ratio of pooled medians is about 3.24x; the difference reflects the long-request tail. Do not describe the fresh gain as a gain for both cache conditions.

| Fixed input bucket | Cases | Fresh quartet 1 ratio | Fresh quartet 2 ratio |
| --- | --- | --- | --- |
| Under 256 tokens | 133 | 1.845559 | 1.970293 |
| 256–1023 | 66 | 1.426119 | 1.598291 |
| 1024–4095 | 44 | 1.461387 | 1.577285 |
| 4096 or more | 0 | Not measured | Not measured |

Median process peak RSS was **8.14 GiB for Uzu versus 19.86 GiB for llama.cpp**. These are process high-water estimates, not model-only or GPU allocations. Median startup was 1.20 versus 2.61 seconds; the first llama.cpp load took 14.85 seconds. Startup is excluded from the latency tables.

## Population and controls

The population contains 231 public JevBench v1.2 development cases and twelve separate synthetic checks. Two counterbalanced quartets run llama/Uzu/Uzu/llama, then Uzu/llama/llama/Uzu. Eight resident processes run serially, with three checked fresh warmups per process and a fresh/immediate-repeat pair for each of the 243 cases. The complete run requires 3,888 measured responses and 24 retained warmups. Startup and warmups are excluded from decision latency.

JevK5 revision `c4f7fdb3aeab5582336406e78d3bef11bf98833d`, temperature 1.22, context 8192, rendered prompts, input IDs, candidate IDs, option order and full-vocabulary normalization remain fixed. The [BF16 provenance](../experiments/uzu-rust/jev-scoring/BF16_CONTROL_PROVENANCE.json) binds both exports; BF16 matrices still use different layouts, retained F32 parameters and arithmetic across engines. The [deeper acceptance](../experiments/uzu-rust/jev-scoring/DEEP_VALIDATION_RESULTS.json) already established exact candidate outputs and whole-vocabulary logits for this unchanged Uzu executable.

llama.cpp generates one token to expose probabilities and permits caching only for the immediately preceding identical input. The benchmark checks that a fresh request evaluates every input token and a repeat evaluates fewer tokens. Uzu creates fresh state in both conditions. Wall time includes tokenization, transport, scoring checks and typed-answer formatting. This measures decision latency; native operation timings and generation throughput have different meanings.

Within each quartet, average each case's two process observations per engine, then divide llama.cpp's mean case latency by Uzu's. Report the two quartet ratios, public-only results, token buckets, per-block results and pooled p50/p95 separately. Two quartets provide descriptive local timing. This comparison cannot isolate the additional gain over original Uzu without a contemporaneous original-Uzu control, and its ratio cannot be multiplied into the historical 1.73x result.

## Probability and calibration acceptance

Every Uzu response must match the sealed original native-scoring reference bit-for-bit: logits, full-vocabulary-normalized candidate log probabilities, native candidate probabilities and typed decisions. The original BF16 helper recalculated probabilities in Python and differs from the native representation by up to `2.22e-16`; the new harness retains native values. Independent recalibration from logits and log probabilities must agree within `1e-12`.

Public accuracy, multiclass Brier sum, ten-bin top-label ECE, ordinal MAE and rounded expected-score accuracy use only the first fresh observation per case and engine. Later observations check stability. Synthetic checks remain separate. Cross-engine probability differences retain the earlier 0.001 strict and 0.01 close thresholds. Frozen public development cases do not establish production calibration or generalization.

| Public quality, 231 cases | llama.cpp | Optimized Uzu |
| --- | --- | --- |
| Argmax correct / accuracy | 202/231 / 87.4459% | 202/231 / 87.4459% |
| Brier score | 0.1836781370917067 | 0.18515405977818739 |
| ECE, ten bins | 0.023855123271241202 | 0.030787072608270183 |
| Ordinal MAE, eighteen cases | 0.16528212372009554 | 0.16875954742751598 |
| Rounded expected-score accuracy | 16/18 | 16/18 |

All **1,944 Uzu measured responses and twelve Uzu warmups** matched the sealed native original outputs, including binary64 probabilities and decisions. Uzu's public quality summary also matched exactly. Both engines had zero observed probability drift across their 1,944 measured responses. Maximum independent recalibration error was `2.220446049250313e-16`. Existing whole-vocabulary acceptance was verified for the unchanged binary; this timing run did not rerun vocabulary probes.

The engines agree on 229/231 public argmax decisions (99.1342%). Only 98/231 cases meet the strict 0.001 probability tolerance, and 199/231 meet the 0.01 tolerance. Maximum option probability difference is **0.027506755884793344**, or 2.7507 percentage points; mean option difference is 0.002262210147877876. Maximum confidence, true/false probability and expected-score differences are 0.02739054644478678, 0.02498910530068188 and 0.04920883114482644, respectively. Uzu-minus-llama Brier is +0.0014759226864806962 and ECE is +0.006931949337028981 (0.6932 percentage points). These reproduce the earlier BF16 differences. There is no additional Uzu quality reduction from the accepted optimizations on this population, and cross-engine probability/calibration parity still fails. Both engines answer all twelve synthetic checks correctly; they remain excluded from public quality.

The [independent audit](../scripts/audit_jev_optimized.py) uses standard-library calculations and imports neither the evaluator nor aggregate helpers. It checks exact coverage/order, retained warmups, source/model/runtime/reference hashes, pre/post seals, binary64 output bits, typed decisions, calibration, all reported scalar groups and the continuous host-monitor receipt.

## Acquisition and host limits

Two attempts were stopped by the monitor and remain excluded from timing conclusions. The first completed four blocks before compiler activity appeared during the fifth: 75 host samples, two compiler-positive samples, zero recognized external model samples, at most one owned model and zero active models afterward. The retry stopped during its first block: fourteen host samples, two compiler-positive samples and one external-model sample. The owned benchmark processes were reaped; the external model exited before inspection. No external process was suspended.

The completed first quartet retained 1,944 measured responses and twelve checked warmups. A Sol cross-check reproduced 26 scalar groups and validated all 1,956 responses without importing aggregate calculations into the independent implementation. This checks acquisition and arithmetic; it does not turn that incomplete attempt into the final result.

After explicit user authorization, three other active chats held builds and local model runs. A new coordinated run completed in its own directory, with 167 five-second host samples: zero recognized external engines, zero generic Python inference flags, zero compilers, at most one owned model and zero active models afterward. The monitor itself suspended no process; other chats handled their authorized holds and received resume messages immediately after the timing window finished.

Background CPU activity remains a material limitation. Seven samples recorded a background process above 100% CPU and three exceeded 200%; the maximum was **560.8%** at elapsed 405.62 seconds while three blocks had completed, during the fourth llama.cpp load. The monitor retains counts and CPU/RSS aggregates, so that historical process cannot be identified afterward. All observations remain included; no post hoc filtering was applied. A single memory-pressure check during Uzu scoring reported 87% available memory. Neither that sample nor process monitoring continuously establishes thermal, clock, power or GPU isolation. The two quartets establish a local directional fresh-input advantage, without a precise steady-state significance claim.

The final independent audit passed: ninety bound artifacts rehashed, every measured response/warmup validated, native Uzu output bits preserved, all public scalar groups exactly reproduced, seals and host receipts checked. The raw acquisition, aborted attempts and host samples remain in owner-only ignored directories. Historical artifacts and validators were not overwritten.

All 285 focused Python checks passed, including sixteen new benchmark/audit tests. Native binaries, historical helpers and sealed reports were preserved unchanged. The current harness binds ninety source, model, runtime and reference artifacts before acquisition.

## Sol reviews and further candidates

Three Sol workstreams independently reviewed [Rust and state ownership](UZU_SOL_RUST_REVIEW.md), [Metal kernels](UZU_SOL_METAL_REVIEW.md) and [evaluation](UZU_SOL_EVAL_REVIEW.md). They found no demonstrated blocking defect in the accepted fixed JevK5 BF16 scorer. Their proposed speed gains remain unimplemented and unmeasured.

The larger compute candidate is final-layer live-tile pruning: remove only complete dead output tiles while preserving the original batch shape, dispatch family and reductions. A one-row substitution can change floating-point results. A dedicated KV preparation path could omit unused queries while retaining the original packed QKVG stride; passing zero query heads to the current helper with that layout reads the wrong offsets. Compact convolution output buffers and shorter scratch lifetimes could reduce memory traffic. Immutable RoPE/topology/kernel setup reuse offers smaller CPU opportunities.

The Metal review also found an inherited malformed-configuration gap: DeltaNet does not reject zero or nondivisible head/group counts before kernels rely on their quotient. The fixed JevK5 configuration is valid; broader model admission needs those checks. Deferred initialization and asynchronous state ownership need separate acceptance before reusable generation-state integration. The Rust review confirmed that downstream MLP convolution has no cross-batch state; the pruning guard should be described as excluding mixer convolution, with partial-row convolution dependencies considered separately.

## Reproduction

Preserve existing binaries, receipts and reports. With ignored model/runtime artifacts and the sealed native reference available, use new output and report names:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/monitor_jev_optimized.py --output test-artifacts/jev-optimized-bf16-new --harness benchmark_jev_optimized.py -- --n-probs 40
uv run --locked --group dev --python 3.12.13 python scripts/benchmark_jev_optimized.py aggregate --output test-artifacts/jev-optimized-bf16-new --report experiments/uzu-rust/jev-scoring/OPTIMIZED_BF16_NEW.json
uv run --locked --group dev --python 3.12.13 python scripts/audit_jev_optimized.py --output test-artifacts/jev-optimized-bf16-new --source-report experiments/uzu-rust/jev-scoring/OPTIMIZED_BF16_NEW.json --report experiments/uzu-rust/jev-scoring/OPTIMIZED_BF16_AUDIT_NEW.json
```

Raw records remain in owner-only ignored directories; public reports contain scalar aggregates and hashes. The monitor rejects observed external engines, generic Python model/training flags, compilers and overlapping owned models. Process sampling does not continuously establish GPU, thermal, clock or power isolation. Production scorer/HTTP/MCP integration, installed JevK5 add-on acceptance and broader Phase 4 scheduling remain outstanding.
