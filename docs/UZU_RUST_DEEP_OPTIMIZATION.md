# Deeper Uzu Rust and Metal scoring experiment

Three Astra workstreams implemented additive candidates after the [previous scoring optimizations](UZU_RUST_SCORING_OPTIMIZATION.md). The combined candidate preserves exact JevK5 BF16 outputs across all 243 evaluation cases and 48 boundary requests. Full-vocabulary hashes, calibrated probabilities and typed decisions match the sealed original Uzu reference. Clean timing is deferred at the user's request, so no larger speed gain has been established. The checkpoint, exact token sequences, temperature 1.22, context 8192, full-vocabulary normalization and fresh state remain fixed.

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

Untimed complete-population acceptance passed for the combined BF16 scorer. Clean balanced timing and speed conclusions are explicitly deferred at the user's request. The isolated variants still need complete-population acceptance before individual performance conclusions. The installed JevK5 add-on and production scorer are outside this experiment. Earlier [llama.cpp probability and calibration differences](UZU_JEVK5_BF16_CONTROL.md) remain; preserving Uzu outputs does not resolve those differences.

## Reproduction and remaining work

Build the combined candidate, then run untimed acceptance using new output and report names. The builder refuses to overwrite existing variants, and the validator rejects a nonempty output directory or an existing report. The sealed reference and ignored model/runtime artifacts must already be present.

```sh
uv run --locked --group dev --python 3.12.13 python experiments/uzu-rust/build_deep_scoring.py --variant all --test
uv run --locked --group dev --python 3.12.13 python scripts/validate_uzu_scoring_deep.py run --output test-artifacts/uzu-deep-validation-new
uv run --locked --group dev --python 3.12.13 python scripts/validate_uzu_scoring_deep.py aggregate --output test-artifacts/uzu-deep-validation-new --report experiments/uzu-rust/jev-scoring/DEEP_VALIDATION_NEW.json
```

Once timing resumes, measure the isolated candidates and combined candidate with the four-quartet protocol, using both the accepted earlier combined worker and historical worker as separate controls. Compare long-context cases separately because submission and final-layer pruning activate only beyond one batch. Report memory pressure and the sampled workload limitations alongside latency; reject any probability, calibration, decision or vocabulary difference. Kernel profiling should identify whether reduced allocation/traffic or CPU/GPU overlap actually saves time before pursuing further changes.
