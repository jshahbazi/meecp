# Deeper Uzu Rust and Metal scoring experiment

Three Astra workstreams implemented additive candidates after the [previous scoring optimizations](UZU_RUST_SCORING_OPTIMIZATION.md). The combined candidate preserves exact JevK5 BF16 outputs across all 243 evaluation cases and 48 boundary requests. Full-vocabulary hashes, calibrated probabilities and typed decisions match the sealed original Uzu reference. After Brekko finished, two balanced twelve-case comparisons measured a modest local gain: about **3.12% lower fresh latency against the earlier optimized worker** and **3.94% against original Uzu**. Full-population improvement remains unestablished. The checkpoint, exact token sequences, temperature 1.22, context 8192, full-vocabulary normalization and fresh state remain fixed.

## Candidates

| Candidate | Removed or overlapped work | Scope and cost |
| --- | --- | --- |
| [Earlier submission](../experiments/uzu-rust/uzu-deep-submission.patch) | Submits each complete 1024-token batch immediately; subsequent CPU encoding can overlap GPU execution. | Batch shapes and final split remain unchanged. Independent scratch pools and retained pending handles protect asynchronous lifetimes. Additional submissions and retained scratch can increase overhead and memory. Prompts up to 1024 tokens have no overlap opportunity. |
| [Final-layer pruning](../experiments/uzu-rust/uzu-deep-last-layer.patch) | Omits final attention, gating, output projection and following MLP when an earlier batch has no observable output. | Default-disabled `scoring-last-layer-pruning`; only the fresh scorer enables its state flag. Preserves full-batch QKVG, normalization, RoPE, KV preparation and acceptance. Requires the actual final layer, owned full attention, flat topology and no layer convolution or hidden-feature request. For JevK5 it activates only above 1024 tokens. |
| [Direct convolution](../experiments/uzu-rust/uzu-deep-metal.patch) | Replaces a full-width padded F32 convolution input with reads from the original projection, a small state snapshot and a separate output projection. | Default-disabled `direct-delta-conv`. Preserves tap order, F32 accumulation, SiLU, casts and untouched channels. At 1024 tokens the temporary logical allocation falls from about 48.4 MiB to 24.2 MiB per active layer. Additional copy threads may offset traffic savings. Generation and tree behavior need separate acceptance. |

The submission guard drains every pending command, including after an earlier completion error, before transformer state or readout storage is destroyed. Every batch uses the same Metal queue, whose existing command-buffer boundary barriers provide device-visible ordering. Independent pools prevent host encoding from recycling an earlier batch's in-flight scratch. Partial or failed state is discarded.

The final-layer candidate retains every K/V row because later queries need them. It removes work after those writes only when no consumer can observe the final layer's output. Earlier layers still produce every position. Preparing unused queries remains part of the unchanged fused path.

The direct convolution uses an immutable snapshot of the preceding F32 convolution state. This prevents races between early-token history reads and next-state writes. Input and output projections are distinct. The downstream GDN recurrence and state update retain their original kernels and dimensions.

## Boundaries of the investigation

Shrinking final attention or MLP to a one-row batch was rejected during source review. The existing attention dispatcher changes kernel family and split count with suffix length; MLP projection geometry can also change its kernel. Mathematical row independence does not establish identical floating-point results. A future tile-pruning path would need to retain the original dispatch policy and reduction tree while removing complete dead tiles.

The GDN intermediate outputs feed the following layer and cannot generally be omitted. Reassociating recurrent arithmetic, changing prefill batches, quantizing weights, or changing precision would need a different semantic acceptance requirement. These candidates retain the original arithmetic policy.

## Identity and validation

The [separate builder](../experiments/uzu-rust/build_deep_scoring.py) reconstructs pinned Uzu 0.6.1 revision `8d01581474760bd1fc7f4a547ff1d02f7dada437`, applies the original scoring patch, the existing topology/state-initialization patches, and an ordered deeper patch chain. It preserves the original manifest, lock, worker sources, probe and historical executables. Schema-2 receipts bind each new binary to its patches, sources, features, recipes and generated project. Build variants serially because they share a Cargo target directory. Receipts establish source traceability; ambient compiler configuration is not a hermetic build guarantee.

The [full protocol](../experiments/uzu-rust/jev-scoring/DEEP_OPTIMIZATION_PROTOCOL.md) and [balanced timing protocol](../experiments/uzu-rust/jev-scoring/DEEP_LATENCY_PROTOCOL.md) use separate versioned harnesses, preserving the earlier evidence and its helpers. The [monitor](../scripts/monitor_uzu_experiment.py) observes process metadata, rejects observed external engines/compilers and overlapping owned engines, and never alters external processes. Five-second sampling does not continuously measure GPU use, clocks, power or temperature, and generic process names may conceal workloads.

Raw requests, scores, token sequences and case identities remain in owner-only ignored directories. Tracked reports contain aggregate statistics, counts, identities and hashes. Whole-vocabulary probes hash little-endian f32 logit bits. They compare rebuilt original-core and candidate probes; the historical worker exposes candidate outputs, whose equality bridges the timing comparison.

The submission [smoke result](../experiments/uzu-rust/jev-scoring/DEEP_SUBMISSION_SMOKE.json) and combined [smoke result](../experiments/uzu-rust/jev-scoring/DEEP_ALL_SMOKE.json) each passed 48 case-scoring responses, 96 boundary responses and 144 whole-vocabulary digest responses across baseline and candidate. Each checks 12 synthetic cases and changed/repeated/restored sequences at lengths through 8192. An independent Astra audit verified every combined-run hash, seal and output, with maximum independent recalibration error `2.22e-16`. Smoke timing is excluded from speed conclusions.

The separate [untimed acceptance protocol](../experiments/uzu-rust/jev-scoring/DEEP_VALIDATION_PROTOCOL.md) and [full result](../experiments/uzu-rust/jev-scoring/DEEP_VALIDATION_RESULTS.json) validate the combined candidate against the sealed first-round historical worker and original-core probe. All 486 fresh/repeat case responses, 48 boundary responses and 534 whole-vocabulary digests match exactly, with three additional checked warmups per process. The worker is fully reaped before the probe starts. Independent recalibration from logits and log probabilities has maximum absolute formula error `2.220446049250313e-16`.

An [independent Astra audit](../experiments/uzu-rust/jev-scoring/DEEP_VALIDATION_AUDIT.json) rehashed all 66 bound artifacts, confirmed all 42 prior reference artifact hashes remained unchanged, and verified seals, ordered records, floating-point bit equality, vocabulary digests and quality aggregates using separate standard-library calculations.

| Public quality metric | Original Uzu and deeper combined candidate |
| --- | --- |
| Cases | 231 |
| Argmax correct | 202/231 (87.4459%) |
| Brier score | 0.18515405977818739 |
| ECE, 10 bins | 0.030787072608270183 |
| Ordinal MAE, 18 cases | 0.16875954742751598 |
| Rounded expected-value accuracy | 16/18 |

The 12 synthetic cases remain separate and all retain correct argmax decisions. Exact output preservation establishes unchanged quality on this frozen population. It does not supply new generalization or calibration evidence. The 187 focused Python tests passed, including receipt/patch identity rejection and output, decision, boundary, digest and privacy checks.

The [native CPU test record](../experiments/uzu-rust/jev-scoring/DEEP_NATIVE_TESTS.json) covers two submission-draining tests and one test each for pruning eligibility, topology equivalence and convolution bit preservation. All five passed. The custom upstream library framework used test-only `RUSTC_BOOTSTRAP=uzu_engine`; production worker/probe builds used ordinary installed Rust 1.98.1. Native scoring-worker tests also passed for the constructed candidates.

A new Brekko Jev process started during the combined smoke run. Process sampling recorded a maximum background process-average CPU value of 627.1%, zero recognized external model services/compilers, at most one owned model and none afterward. Generic Python process names conceal workload identity from the monitor's engine-name check. The additional workload was identified separately from private command metadata without exposing arguments. Smoke timing is therefore excluded from speed conclusions. The user chose to leave this workload running and defer timing; no process was suspended. The earlier approved workload had exited.

The optional `--baseline-receipt` supports incremental balanced timing against the previously accepted `combined` worker. Use the historical worker and original-core probe for the full/digest harness: its retained report scope labels assume that baseline. Run the four-quartet timing protocol for primary timing results; its two-quartet smoke summary retains a generic four-control wording despite containing only two controls. These limits do not affect the completed smoke exactness checks.

Untimed complete-population acceptance passed for the combined BF16 scorer. The isolated variants still need complete-population acceptance before individual performance conclusions. The installed JevK5 add-on and production scorer are outside this experiment. Earlier [llama.cpp probability and calibration differences](UZU_JEVK5_BF16_CONTROL.md) remain; preserving Uzu outputs does not resolve those differences.

## Resumed timing

The user reported that Brekko finished on 2026-10-11. Read-only inspection confirmed its former process was absent and found no active model engines, compilers or residual Brekko workload. The [resumed protocol](../experiments/uzu-rust/jev-scoring/DEEP_TIMING_RESUMED_PROTOCOL.md) fixed two separate comparisons before timing. Each used the existing four-quartet ABBA/BAAB schedule, four independent baseline-only controls, three warmups per process and twelve public cases selected by input length. The two runs totaled 40 serial resident processes, 960 measured requests and 120 checked warmups. Startup and warmups are excluded from request timings.

| Baseline | Fresh median quartet ratio, baseline/candidate | Fresh lower latency | Repeat median ratio | Repeat lower latency |
| --- | --- | --- | --- | --- |
| Earlier optimized `combined` | 1.032182 | 3.12% | 1.021936 | 2.15% |
| Original Uzu historical worker | 1.040987 | 3.94% | 1.042926 | 4.12% |

For each quartet and condition, average the two process observations of each case, then divide the baseline's mean case latency by the candidate's mean. The table takes the median of the four quartet ratios; lower latency is `1 - 1 / median_ratio`. Fresh ratios ranged from 1.022670 to 1.087153 versus `combined`, and 1.039175 to 1.047726 versus historical. All four quartets favored the candidate in both conditions and both comparisons. The raw timing aggregates are [versus combined](../experiments/uzu-rust/jev-scoring/DEEP_LATENCY_VS_COMBINED.json) and [versus historical](../experiments/uzu-rust/jev-scoring/DEEP_LATENCY_VS_HISTORICAL.json).

| Fixed bucket | Public input lengths | Fresh lower latency vs combined | Fresh lower latency vs historical |
| --- | --- | --- | --- |
| Short | 137, 156, 177, 207 | 0.51% | 8.46% |
| Medium | 290, 360, 518, 793 | 1.09% | 5.00% |
| Long | 1275, 2406, 2635, 3387 | 3.68% | 3.16% |

The longer cases show the larger incremental gain. Bucket results cannot attribute it to an individual patch. Native component directions also differ: state creation and readout were slightly slower versus `combined`, while prefill improved. Against historical, short-case prefill was slightly slower despite lower whole-request latency. Earlier deferred initialization removes independent waits and moves initialization into prefill; its roughly 25.9x state-creation ratio is not an inference speedup.

The median per-case baseline-control range divided by its mean was **10.69% fresh / 12.83% repeat** versus `combined`, and **7.16% fresh / 8.67% repeat** versus historical. These describe variation across four processes, with some cases varying more; they are not significance thresholds or confidence bounds. The twelve-case selection and ratio of means favor the influence of slower cases. This establishes a small local directional gain, with no precise steady-state or population-wide claim. The direct historical result is measured separately; do not multiply it by the earlier optimization ratio or the historical llama.cpp 1.73x result.

Both runs sampled zero recognized external engines or compilers, at most one owned engine and none afterward, with no suspended processes. Each had 31 host samples; maximum background process-average CPU was 78.9% and 39.7%, respectively. A separate metadata check identified ordinary desktop/tooling activity and no residual Brekko workload. Sampling remains limited to process metadata and does not continuously establish GPU or thermal isolation. Resident process peak RSS was similar, approximately 8.12–8.14 GiB across comparison medians; it is a process high-water estimate rather than model-only or GPU allocation.

Every measured output remained exactly equal to the sealed reference, with maximum independent calibration formula error `2.220446049250313e-16`. The [independent analyzer](../scripts/analyze_uzu_deep_timing.py) reproduces the entire original aggregate using standard-library calculations, verifies source/reference/record/seal identities and host receipts, and reports every bucket, condition and native component. Its [combined audit](../experiments/uzu-rust/jev-scoring/DEEP_TIMING_AUDIT_VS_COMBINED.json) and [historical audit](../experiments/uzu-rust/jev-scoring/DEEP_TIMING_AUDIT_VS_HISTORICAL.json) both passed. Warmups were checked during acquisition; their response bodies are not retained by the timing harness. Existing full-vocabulary acceptance covers these unchanged binaries; timing did not rerun the probes. All **196 focused Python tests** passed, including nine tests of independent timing arithmetic and rejection checks.

## Reproduction and remaining work

Build the combined candidate, then run untimed acceptance using new output and report names. The builder refuses to overwrite existing variants, and the validator rejects a nonempty output directory or an existing report. The sealed reference and ignored model/runtime artifacts must already be present.

```sh
uv run --locked --group dev --python 3.12.13 python experiments/uzu-rust/build_deep_scoring.py --variant all --test
uv run --locked --group dev --python 3.12.13 python scripts/validate_uzu_scoring_deep.py run --output test-artifacts/uzu-deep-validation-new
uv run --locked --group dev --python 3.12.13 python scripts/validate_uzu_scoring_deep.py aggregate --output test-artifacts/uzu-deep-validation-new --report experiments/uzu-rust/jev-scoring/DEEP_VALIDATION_NEW.json
```

For a new balanced comparison against the earlier optimized worker, complete builds and host preflight first, then use fresh paths:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/monitor_uzu_experiment.py --output test-artifacts/uzu-deep-latency-new --harness benchmark_uzu_scoring_deep_latency.py -- --candidate-binary runtime/uzu-rust/deep-optimization/all/bin/meecp-uzu-worker --candidate-receipt runtime/uzu-rust/deep-optimization/all/BUILD_RECEIPT.json --baseline-binary runtime/uzu-rust/optimization/combined/bin/meecp-uzu-worker --baseline-receipt runtime/uzu-rust/optimization/combined/BUILD_RECEIPT.json --quartets 4
uv run --locked --group dev --python 3.12.13 python scripts/benchmark_uzu_scoring_deep_latency.py aggregate --output test-artifacts/uzu-deep-latency-new --report experiments/uzu-rust/jev-scoring/DEEP_LATENCY_NEW.json
uv run --locked --group dev --python 3.12.13 python scripts/analyze_uzu_deep_timing.py --output test-artifacts/uzu-deep-latency-new --source-report experiments/uzu-rust/jev-scoring/DEEP_LATENCY_NEW.json --report experiments/uzu-rust/jev-scoring/DEEP_TIMING_AUDIT_NEW.json
```

Use separate new paths and omit both baseline arguments to compare directly against historical Uzu. The monitor does not suspend external processes and rejects recognized competing engines or compilers.

The combined timing comparisons are complete. Remaining work includes separate acceptance and timing for isolated candidates, broader population controls, and kernel profiling to identify whether reduced traffic or CPU/GPU overlap provides the measured benefit. Larger gains have not been demonstrated. Any further optimization must preserve probabilities, calibration, decisions and vocabulary output. Keep production integration and broader generation/tree acceptance separate.
