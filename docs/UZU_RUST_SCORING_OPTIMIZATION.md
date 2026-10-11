# Uzu Rust scoring optimization

Three Astra agents inspected the pinned Rust and Metal scoring path and implemented two isolated candidates. The complete population retained exact decision outputs, but its timing did **not establish an overall speedup**. A separate balanced control favored the combined candidate in all four quartets, with a median ratio corresponding to about **2.58% lower mean latency on 12 selected cases**. These changes remain experimental and opt-in.

The [deeper Rust and Metal follow-up](UZU_RUST_DEEP_OPTIMIZATION.md) implements earlier batch submission, final-layer cache-only execution and direct DeltaNet convolution. Its combined BF16 candidate preserves exact probabilities, calibration, decisions and whole-vocabulary hashes across the complete 243-case population and boundary checks. Resumed balanced timing measures about 3.12% lower fresh latency against this earlier combined worker on twelve selected cases, with a separate direct historical comparison and full-population timing still unestablished.

## Implementation and provenance

The [hotspot inspection](UZU_RUST_HOTSPOTS.md) records the source findings and deferred work. Both candidates preserve the JevK5 BF16 export, tokenization, temperature 1.22, full-vocabulary normalization, context limit 8192, and fresh state for every request.

- [Direct topology](../experiments/uzu-rust/uzu-scoring-topology.patch) constructs the scorer's equivalent linear GPU topology directly, removes unused trie/token-history work, and omits final state acceptance when the fresh state will be destroyed. Acceptance between chunks is retained.
- [Deferred initialization](../experiments/uzu-rust/uzu-scoring-state-init.patch) adds the default-disabled `uzu-engine/deferred-state-init` feature. It encodes both recurrent-state zero fills before the first consumer and removes 24 independent submit-and-wait cycles. Zeroing work remains in prefill. State must be discarded if native work fails or is abandoned; this does not implement state reuse.

The [isolated builder](../experiments/uzu-rust/build_optimized_scoring.py) reconstructs Uzu 0.6.1 revision `8d01581474760bd1fc7f4a547ff1d02f7dada437`, applies the original scoring patch and ordered additive patches, and uses the existing byte-identical dependency lock. Build variants serially: the builder shares a Cargo target directory. Generated manifests, source trees, executables, and build receipts remain ignored under `runtime/uzu-rust/optimization/`.

The timed baseline is the preserved historical executable, SHA-256 `af047d5ae3fcf0c088443aad40fbe0f3d35cf137e4cb15e390a1f922103ea04a`. The combined executable is `11eb2da42e123a86707ac83cb0d4e0b2c7e0f298554e15f5244703e3ea69111a`. The original scoring executable, generation manifests, model exports, and installed JevK5 package were preserved. The aggregate binds source/patch, binary, model, protocol, recipe, manifest, lock, and receipt hashes. This is build traceability, without a claim of hermetic reproducibility across ambient compiler configurations.

## Exactness and quality

The frozen [full protocol](../experiments/uzu-rust/jev-scoring/OPTIMIZATION_PROTOCOL.md) and [aggregate](../experiments/uzu-rust/jev-scoring/OPTIMIZATION_RESULTS.json) cover all 231 public JevBench v1.2 cases and 12 separate synthetic cases, two reversed-order resident rounds, and fresh plus identical-repeat requests: **1,944 timed responses**. All input/candidate token IDs, candidate logits, candidate log probabilities, candidate probabilities, and typed outputs matched exactly between the current baseline and combined worker, including repeats and rounds. No truncation or cache reuse occurred.

All **192 boundary responses** matched, including sequence changes followed by restoration, at lengths 1, 2, 255, 256, 257, 1023, 1024, 1025, 2048, 2049, 4096, and 8192. Independent Astra audit recalculated the calibration formula with maximum error `2.22e-16`.

Separate untimed [digest probes](../experiments/uzu-rust/probes/score_digest.rs) produced **1,068 full-vocabulary hash responses**, 534 per implementation. They hash little-endian f32 logit bits. Every cross-implementation hash and repeated/restored sequence matched. This compares a **rebuilt original-core probe** with an optimized-core probe. The preserved historical timed worker exposes candidate scores only; its candidate-output equality supplies the connection to the timed comparison.

| Public-set metric | Current baseline | Combined |
| --- | ---: | ---: |
| Argmax accuracy | 202/231 (87.45%) | 202/231 (87.45%) |
| Multiclass Brier sum | 0.185154 | 0.185154 |
| Top-label ECE, 10 bins | 0.030787 | 0.030787 |
| Rounded expected-score accuracy | 16/18 | 16/18 |
| Expected-score ordinal MAE | 0.168760 | 0.168760 |

All 12 synthetic cases were correct. These checks preserve Uzu's measured outputs; the earlier [same-checkpoint llama.cpp probability and calibration differences](UZU_JEVK5_BF16_CONTROL.md) remain. Historical Uzu token IDs, logits, and log probabilities also matched. Its older harness stored Python-recalibrated probabilities, so 196 of 972 historical probability/decision comparisons differ by at most `2.22e-16` from the current worker-returned representation. Historical probability-object bit equality is therefore not claimed.

## Full-population timing

The machine is an Apple M5 Max with 128 GiB memory and macOS 26.7.1. Wall time includes tokenization, transport, and scoring; startup, warmups, boundary checks, and digest probes are excluded. Each request condition has 486 observations per implementation.

| Request | Baseline p50 / p95 | Combined p50 / p95 | Median paired baseline/combined ratio |
| --- | ---: | ---: | ---: |
| Fresh | 79.09 / 675.61 ms | 66.02 / 697.03 ms | 0.972 |
| Identical repeat | 79.56 / 686.66 ms | 66.76 / 700.56 ms | 0.975 |

The lower combined marginal p50 does not establish a gain. The paired ratio compares the same cases and favors baseline slightly overall. Fresh ratios changed from **0.907** in round one to **1.062** in round two. Same-case median native score time increased 40.6% between baseline rounds and 21.3% between combined rounds. The second-round advantage also decayed across the block. The cause was not instrumented; thermal, clock, power, and background-load explanations remain hypotheses.

Fresh state-creation p50 fell from **5.005 ms to 0.184 ms**, demonstrating removal of synchronous initialization overhead. Some work moved into prefill, so that component change alone cannot establish lower complete-request latency. The [smoke](../experiments/uzu-rust/jev-scoring/OPTIMIZATION_SMOKE.json) reported a 1.076 fresh paired ratio on 12 synthetic cases; it is a separate small check and does not supersede the full result.

## Timing control

The separate [short-block protocol](../experiments/uzu-rust/jev-scoring/LATENCY_PROTOCOL.md) selected 12 public cases using token-length ranks alone, four per short/medium/long bucket. Twenty serial resident blocks produced **480 measured responses**: four baseline-only controls and four comparison quartets in ABBA, BAAB, ABBA, BAAB order. Each block used identical warmups and case ordering. Every selected output matched the sealed full-run baseline exactly.

Within each quartet, average each case's two observations per implementation, then compare their mean latencies. The quartet is the replication unit; no request-level bootstrap interval is reported. The [aggregate](../experiments/uzu-rust/jev-scoring/LATENCY_RESULTS.json) records the individual quartets and baseline controls.

| Quartet | Fresh baseline/combined mean-latency ratio | Identical-repeat ratio |
| --- | ---: | ---: |
| 1, ABBA | 1.02988 | 1.01358 |
| 2, BAAB | 1.02405 | 1.02380 |
| 3, ABBA | 1.02891 | 1.03194 |
| 4, BAAB | 1.01238 | 1.01479 |
| Median of four quartets | **1.02648** | **1.01929** |

All four quartets favored combined on these selected cases. The median ratios correspond to approximately **2.58% lower fresh latency** and **1.89% lower repeat latency**. The fresh within-quartet median paired savings ranged from 2.52 to 4.67 ms. This supports a modest local signal; it does not establish a full-population or precise steady-state improvement.

For baseline-only controls, the median per-case process range divided by its mean was **13.42% fresh** and **14.77% repeat**. These are descriptive variations across four processes, not confidence intervals or significance thresholds. Interpretation must retain the earlier complete-population result and its changing direction. This experiment does not provide a new llama.cpp timing comparison or justify multiplying its ratio into the historical 1.73× figure.

## Isolation, validation, and scope

The [isolation summary](../experiments/uzu-rust/jev-scoring/HOST_ISOLATION_OPTIMIZATION.json) records 136 process samples across the full run and 31 across the short-block control. Both observed zero external model services or compilers, at most one owned model process, and zero active models afterward. No external process was suspended. The two background processes exceeding 1 GiB RSS were Codex and its renderer. Other UI/system activity continued; maximum sampled process-average CPU readings were 342.5% full and 72.9% control. Five-second sampling can miss shorter activity and does not continuously measure GPU use, temperature, clocks, or power. Raw data remains owner-only in ignored `test-artifacts/` directories; tracked artifacts contain scalar aggregates, hashes, and counts.

Validation passed 163 focused Python tests, native scoring-worker tests, and the topology-equivalence library test. Upstream's library test uses nightly custom-test-framework features and requires its CPU backend. The local test-only invocation used `RUSTC_BOOTSTRAP=uzu_engine` with installed Rust 1.98.1; this compatibility setting did not enter worker/probe builds.

The experimental initialization feature affects DeltaNet throughout its opted-in binary. Generic generation/tree execution has source review but no new runtime acceptance. Installed-addon integration, production scoring routes, identical-state reuse, slot-boundary acceptance, and broader Phase 4 work remain outstanding. No generation-throughput improvement is claimed.

## Reproduction

From a checkout with the verified model export and sealed benchmark dataset, build into unused variant directories:

```sh
uv run --locked --python 3.12.13 python experiments/uzu-rust/build_optimized_scoring.py --variant baseline --test
uv run --locked --python 3.12.13 python experiments/uzu-rust/build_optimized_scoring.py --variant combined --test
uv run --locked --python 3.12.13 python scripts/benchmark_uzu_scoring_optimization.py run --output test-artifacts/uzu-optimization-replay --candidate-binary runtime/uzu-rust/optimization/combined/bin/meecp-uzu-worker --candidate-receipt runtime/uzu-rust/optimization/combined/BUILD_RECEIPT.json --baseline-digest-binary runtime/uzu-rust/optimization/baseline/bin/meecp-uzu-score-digest --candidate-digest-binary runtime/uzu-rust/optimization/combined/bin/meecp-uzu-score-digest
uv run --locked --python 3.12.13 python scripts/benchmark_uzu_scoring_optimization.py aggregate --output test-artifacts/uzu-optimization-replay --report experiments/uzu-rust/jev-scoring/OPTIMIZATION_REPLAY.json
uv run --locked --python 3.12.13 python scripts/benchmark_uzu_scoring_latency.py run --reference test-artifacts/uzu-optimization-replay --output test-artifacts/uzu-latency-replay --candidate-binary runtime/uzu-rust/optimization/combined/bin/meecp-uzu-worker --candidate-receipt runtime/uzu-rust/optimization/combined/BUILD_RECEIPT.json
uv run --locked --python 3.12.13 python scripts/benchmark_uzu_scoring_latency.py aggregate --output test-artifacts/uzu-latency-replay --report experiments/uzu-rust/jev-scoring/LATENCY_REPLAY.json
```

Run builds and tests before timing, keep engines serial, and use fresh output/report names. The builder and aggregate refuse to overwrite historical artifacts.
