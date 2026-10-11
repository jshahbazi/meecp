# Sol GDN compact projection experiment

Implemented on 2026-10-11 against the accepted combined source at `runtime/uzu-rust/deep-optimization/all/source/upstream`. This isolated candidate adds the default-disabled `compact-delta-conv` Cargo feature, which depends on `direct-delta-conv`. Its [patch](../experiments/uzu-rust/patches/uzu-sol-gdn.patch) uses upstream-relative `a/` and `b/` paths. Candidate source is in the ignored `runtime/uzu-rust/sol-optimization/gdn/source/upstream` directory. The accepted source, executable, historical patches, builders and reports remain unchanged.

Status: implemented and statically inspected by this workstream, then built and tested in the owning combined experiment. All 12 focused CPU/Metal tests and complete native bit-exact acceptance passed for the new combined executable. See the [combined experiment](UZU_SOL_OPTIMIZATION.md) and its independent audit. Individual GDN performance remains unmeasured; acceptance covers the fixed JevK5 scoring scope.

## Change and eligibility

For an eligible flat DeltaNet batch with more than one token, direct convolution writes only the convolved Q/K/V prefix into a compact outer-type buffer. The original full-width projection remains immutable and supplies gate, beta and decay channels. Single-token update and tree verification retain their full-width representation. Disabling the new feature retains the full-width direct-convolution representation; disabling direct convolution retains the packed scan path.

The internal preparation kernel now takes separate Q/K/V and control inputs, with an explicit Q/K/V row stride. Sequential prefill and chunked output/state kernels also take that stride for V reads. Preparation's Q/K normalization, beta/decay formulas and optional compact-V copy remain in their existing kernels. Norm/gate receives the original projection with its original total stride and channel offsets. Tree preparation passes its same full projection for both inputs. CPU reference interfaces and all known unit call sites are updated with the matching arguments.

The Metal direct-convolution dispatch still launches the original `suffix_len + kernel_size - 1` by `total_proj_dim` logical extent and threadgroup geometry. Threads beyond the compact output width return without copying the unchanged channels. State-update threads and the immutable history snapshot retain their original indexing. Sequential recurrence keeps its 128-thread groups and four value rows per SIMD group. Chunked recurrence keeps the original 64-token chunks, MXU selection, fragment layout and threadgroup reductions. Neither the 256-token chunked threshold nor any model projection shape changes.

## Exactness argument

For each convolved channel, convolution reads the same original input and F32 history samples, initializes from the same optional bias, visits taps in the same order, performs the same F32 operations and SiLU, and casts once to the same outer type. Only the output address changes from `token * total_proj_dim + channel` to `token * conv_dim + channel`. The next convolution state uses unchanged input/history addresses and F32 assignments, including suffixes shorter than retained history.

Q/K/V readers recover those same outer-type bits using the new stride. Beta, decay and gate channels come from the original projection; the previous direct convolution copied those outer-type bits without arithmetic. Their consumers preserve the original offsets, casts, formulas and reductions. Recurrence and norm/gate arithmetic, intermediate outer-type round trips, lane mapping and dispatch selection are unchanged in source. Matmul reductions, attention, RoPE, batch boundaries, calibration and whole-vocabulary normalization are outside this patch.

This is a source-equivalence argument. Changed buffer bindings and address calculations can still affect compiler code generation, so executable bit equality is required before accepting the candidate.

## Traffic and lifetime limits

For suffix length `L`, original projection width `P`, convolution width `C` and outer-type byte width `w`, the output allocation requests `w * L * C` bytes instead of `w * L * P`. It removes `w * L * (P - C)` bytes of unchanged-channel reads and the same number of writes in direct convolution. The idle tail threads remain dispatched.

The original full-width projection must remain live through preparation and norm/gate. The previous implementation released its original projection handle after encoding convolution and could reuse its full-width allocation for later scratch. Consequently the compact candidate can retain more simultaneously live storage later in the layer, and the pool's power-of-two bins may require additional physical allocations. A smaller convolution output request does not establish lower peak GPU memory or faster inference. Measure logical requests, pool allocation bins, resident memory and latency separately.

Both projections remain owned through their final encoded reads. Host handles can be released when encoding ends because the command buffer and pending submission retain the allocation pool; the scorer retains independent pools for different in-flight batches. Existing Metal access tracking supplies read/write barriers. This patch introduces no scratch reuse, early pool retirement or cross-command mutable sharing. Its feature has the same broad compiled-engine reach as direct convolution, so general generation, trees, reusable failed state and other model configurations need separate acceptance. The inherited head/group validation gap remains outside this traffic experiment.

## Verification and remaining work

`git apply --check` succeeded against the accepted source. Rustfmt parsed the changed Rust files using edition 2024; installed stable rustfmt reported that the repository's nightly-only import grouping options were unavailable. A residual source comparison found changes only in the feature declaration, DeltaNet/GDN interfaces and kernels, and their unit tests.

The ordered RoPE, GDN and attention patches also applied successfully to a separate copy of the accepted source. Independent source review found no overlapping arithmetic, stride or storage-lifetime contract between GDN's compact representation and attention's packed KV-only preparation. The attention candidate preserves full packed QKVG offsets and per-head K/V normalization, applies the existing RoPE helper, retains preparation's original axis extents and writes the same K/V locations. The earlier final-layer eligibility guard still controls its cache-only invocation. This review supplies no executable equality evidence for their combination.

The owning experiment ran the added or extended native tests successfully:

- The existing direct-convolution test compares full and compact BF16 Q/K/V bits and F32 next-state bits on every available backend, checks that the original input stays unchanged, and retains its packed-scan comparison. It covers bias on/off and lengths 1, 2, 3, 4, 63, 64, 255, 256, 257 and 1024.
- `compact_delta_prefill_preserves_bf16_bits` and `compact_delta_prefill_preserves_f32_bits` compare full and compact preparation, sequential recurrence, norm/gate output and F32 state on every available backend, with nonzero initial state at lengths 2, 3, 63, 64 and 65.
- Metal `compact_projection_preserves_prefill_bits` compares each recurrence family with its own full-width control at lengths 2, 3, 63, 64, 65, 255, 256, 257, 1023 and 1024. It checks BF16 output and F32 state bits after norm/gate, skipping only the chunked branch on devices without its support. These representation tests do not assert that sequential and chunked recurrence are interchangeable.

The combined worker/probe build and fixed-checkpoint executable acceptance completed in separate new artifact paths, including changed/repeated/restored requests and 1024-token batch transitions. Recheck excluded single-token/tree and disabled-feature paths as appropriate for broader admission. Combined timing is recorded by the owning experiment; individual GDN latency and GPU memory effects remain unmeasured. Recurrent tiling, norm/gate fusion and shared-pool recycling were not included because they introduce arithmetic or lifetime risks beyond this storage change.
