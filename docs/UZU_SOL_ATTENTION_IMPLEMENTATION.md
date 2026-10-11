# Sol attention KV-only implementation

Implemented on 2026-10-11 as an additive experiment against the accepted combined source at `runtime/uzu-rust/deep-optimization/all/source/upstream`. The immutable baseline, its patches, builders, executable and historical reports are preserved. The isolated source is `runtime/uzu-rust/sol-optimization/attention/source/upstream`; [the tracked patch](../experiments/uzu-rust/patches/uzu-sol-attention.patch) reconstructs this candidate relative to that baseline.

## Scope and activation

`sol-attention-kv-only` is disabled by default and depends on the existing `scoring-last-layer-pruning` feature. It changes only the already eligible cache-only path: an unobserved actual final layer during an earlier fresh-scoring batch, flat topology, owned full attention, no shared K/V and no mixer convolution. The inherited transformer and layer eligibility checks are unchanged. Every final visible batch retains ordinary full attention and all requested-row semantics. Ordinary generation retains its existing state defaults and does not enable the scorer pruning flag.

The candidate retains the complete QKVG linear projection with its original dimensions, GEMM selection, tiles, reductions and BF16 output. It omits query normalization dispatches and uses a dedicated `AttentionPrepareKVOnly` kernel with no query output allocation or binding. Its Metal grid retains all original query/key/value head coordinates and the original head-dimension and batch axes; query lanes return before input loads, RoPE or stores. K/V normalization uses the original QKVG row stride and query-head offsets. K/V RoPE and cache stores retain the original expressions and casts.

The existing tightly packed appended-KV path is unchanged. Passing zero query heads to that helper would select the wrong stride and offsets for a full QKVG projection; this candidate never does so.

## Exactness argument

QKV normalization dispatches independently per query, key and value head. Removing the query call cannot alter key/value input slices, head offsets, per-head SIMD reductions, scaling modes or casts. The dedicated preparation kernel shares the existing `apply_rope` function and retains each key/value element's arithmetic, source index and cache index. Preparation contains no cross-head reduction or threadgroup state, so dead query lanes supply no data to surviving lanes. Optional gate channels remain outside preparation's head axes.

The caller still accepts every K/V row before encoding the next chunk. State ownership, pending submissions, RoPE table construction, cache position advancement, full-vocabulary readout, calibration and typed decisions are unchanged. These facts justify executable equality testing; they do not substitute for compiler or native acceptance evidence. Source exactness cannot rule out compiler specialization effects from the new kernel.

## Focused checks and remaining acceptance

The patch adds backend unit checks comparing ordinary and KV-only K/V bits, including unchanged poisoned cache prefixes, poisoned dead query/gate channels, unequal query/KV head counts, no/partial/full RoPE, and lengths 2, 9, 65, 1023, 1024 and 1025. A normalization check compares ordinary and projected-KV BF16 outputs while verifying unchanged query/gate bits and both configured upcast modes. Existing query-only, appended-KV and row-stride checks remain in place. These tests emit authored diagnostic messages only.

Rust formatting completed in the isolated source. The implementation workstream performed source checks only; the owning experiment subsequently built the combined worker/probe and passed all 12 focused CPU/Metal tests. Full native acceptance preserved all exposed logits, log probabilities, probabilities and typed decisions bit-for-bit across 486 case outputs and 48 boundaries, with all 534 whole-vocabulary digests identical. See the [combined experiment](UZU_SOL_OPTIMIZATION.md) and its independent audit. The initial normalization fixture has unscaled heads; full native acceptance exercises the admitted model's real normalization configuration. Individual attention latency remains unmeasured.

## Rejected expansion and risks

Final-visible-row tile pruning remains unimplemented. A safe version must propagate complete live tiles through attention, output projection, normalization and MLP while retaining original matrix dimensions, attention family, split count and reduction order. Flattened grouped-query tiles and adjacent consumer tiles complicate that proof; reducing a batch to one row would change kernel selection and is inadmissible for bit-exact acceptance. Q/G projection-column pruning is also deferred because it requires preserving shared K/V boundary tiles and original GEMM reduction geometry.

The benefit is limited to eligible earlier batches beyond the preserved scorer chunk boundary. Dead query threads still dispatch to retain geometry, and the full QKVG projection remains. The added kernel requires matching CPU/Metal generated bindings and may add construction/compile overhead. A constructor is created only for owned full attention when the feature is enabled. No speedup is established by implementation alone.
