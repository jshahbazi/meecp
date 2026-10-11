# Uzu Rust scoring hotspot inspection

Inspected on 2026-10-10 against Uzu 0.6.1 revision `8d01581474760bd1fc7f4a547ff1d02f7dada437`, the original exact-token scoring patch, and the local JevK5 Qwen3.5-4B BF16 export. The [completed optimization measurements](UZU_RUST_SCORING_OPTIMIZATION.md) preserve exact outputs and show a modest benefit in a balanced 12-case control; the full-population timing does not establish an overall speedup. This document records source inspection and candidate review, without production acceptance.

## Scope and existing behavior

The target is fresh exact-token scoring on an Apple M5 Max with 128 GB memory, context limit 8192 and 1024-token prefill batches. The model has 32 layers: 24 DeltaNet recurrent layers and eight full-attention layers. The last layer is full attention. Prompt tokens, BF16 precision, scoring mathematics, full-vocabulary normalization, and fresh state must remain unchanged.

The original scorer already requests only the final prompt position. Output normalization and vocabulary readout process that position, and earlier batches have no vocabulary readout. It encodes all prefill batches into one command buffer and waits once at the end. Removing repeated readouts or per-batch prefill waits therefore offers no opportunity in this path.

Execution is encoded directly through the Metal backend. The workspace's legacy `shoji` crate supplies traits and types; this scoring path has no separate shoji graph scheduler to tune.

## Selected candidates

| Candidate | Source evidence and change | Expected limit and correctness conditions |
| --- | --- | --- |
| Direct flat topology | `src/engine/language_model/scoring.rs` constructs and linearizes a token trie solely to obtain flat topology. Construct equivalent GPU nodes directly and omit unused token-history extension. Skip final acceptance when the fresh state will be destroyed. | Removes host allocation, traversal and unused seed derivation. Savings are bounded by that host work. Node ranges retain inclusive ends, heights and parents; every chunk followed by another chunk still accepts state. |
| Deferred DeltaNet initialization | `src/encodable_block/mixer/delta_net.rs` independently submits and waits for zeroing in each fresh recurrent layer. An opt-in `deferred-state-init` feature records both fills before the first mixer encode. | Removes 24 synchronous initialization submissions per score. GPU zeroing remains necessary and moves into the prefill timing bucket. Compare complete score latency. Fill both buffers exactly once, before flat or tree consumers, through the normal hazard-tracked command buffer. |

Paths in this table are relative to the pinned upstream `crates/uzu-engine` directory. The additive implementations are [the topology patch](../experiments/uzu-rust/uzu-scoring-topology.patch) and [the initialization patch](../experiments/uzu-rust/uzu-scoring-state-init.patch).

Neither candidate changes arithmetic or kernel geometry, so validation should require bit-identical outputs. Skipping final acceptance is safe within the scorer because recurrent state updates have already been encoded, logits do not depend on subsequent acceptance, and state remains alive through GPU completion. A split final batch must still accept its first part. Deferred initialization records encoding, not completion: callers must discard state after abandoned or failed native work. The existing scorer's process failure fence and fresh-state ownership satisfy this restriction.

These changes target overhead. They cannot eliminate the dominant matrix multiplication, attention or recurrent computation. No percentage gain is justified before measurement, and long-prompt gains may be modest.

## Deferred investigation

- **Earlier submission of prefill batches:** the current GPU starts after the entire prompt has been encoded. Per-batch submission could overlap host encoding and execution. Validate ordering, allocation lifetimes and all completion errors before considering this change; its upside is bounded by overlapable host encoding time.
- **Final-layer dead work:** `transformer_layer.rs` already identifies unused final-layer output projection and following work outside the requested output suffix. A cache-only guard could skip the final MLP for earlier batches. A broader path could omit unused attention queries while preserving every key/value cache entry. The straightforward MLP guard probably offers only a few percent at most for this 32-layer model and expands the interface scope; it is not part of the selected patches.
- **Prefill batch geometry:** benchmark 256, 512 and 1024 only after establishing the overhead result. Attention state has a hardcoded 1024-token suffix capacity, and grouped d256 prefill dispatch also caps its suffix at 1024. Increasing the batch constant alone is unsafe. Different batch geometry also requires numerical revalidation.
- **DeltaNet kernels:** the Metal chunked path uses 64-token internal chunks and staged preparation, prefix sum, Gram, inverse and output/state kernels. Output/state scans chunks serially per value-head tile while retaining F32 state in threadgroup memory. Parallelizing that recurrence, changing tile sizes, or fusing stages requires dedicated kernel evidence and numerical validation. No quantization or reduced-precision shortcut is proposed.
- **Metal barriers:** `backends/metal/command_buffer.rs` uses address-range hazard tracking and broad barriers. Narrower barriers require evidence about actual dependencies and alias reuse; removing them speculatively risks correctness.

## Verification boundary

The isolated builder reconstructs immutable upstream source plus the original and ordered additive patches. Historical binaries and installed packages remain separate. The [optimization protocol](../experiments/uzu-rust/jev-scoring/OPTIMIZATION_PROTOCOL.md) defines paired timing, identity checks and boundary scenarios.

Require repeated fresh calls, changed inputs followed by restoration, and lengths around the recurrent threshold and 1024-token batch boundary. Aggregate full-vocabulary digest equality can verify all logits between the separately built baseline and candidate probes without emitting vectors. The preserved historical worker exposes only selected candidate scores; its full vocabulary is not directly observed. Reports must distinguish historical-worker candidate equality from rebuilt-probe whole-vocabulary equality.

The opted-in initialization feature applies to DeltaNet throughout that binary. First-encode tree ordering has been reviewed in source; runtime tree verification, general generation acceptance, installed addon integration and production scorer acceptance remain outside this investigation. This scan introduces no new roadmap commitment.
