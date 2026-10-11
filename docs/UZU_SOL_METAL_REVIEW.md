# Sol review of the deeper Uzu Metal scorer

Reviewed on 2026-10-11. This is a static review of the combined candidate in `runtime/uzu-rust/deep-optimization/all/source/upstream`, compared with `runtime/uzu-rust/upstream`, and the tracked [Metal patch](../experiments/uzu-rust/uzu-deep-metal.patch). The [deeper optimization record](UZU_RUST_DEEP_OPTIMIZATION.md) supplies the existing acceptance scope: the combined JevK5 BF16 scorer has exact output acceptance on 243 cases and 48 boundary requests. This review ran no builds, tests, inference, profiling or model hashing. It independently inspected source behavior; it did not re-audit those acceptance artifacts.

No blocking correctness defect was identified in the accepted scorer path. The direct convolution's history snapshot, disjoint projection buffers and state indexing are coherent. Larger opportunities remain in final-layer dead tiles, unused query/gate work, projection traffic and scratch lifetimes. Every proposed performance benefit below is unmeasured, and every changed executable would need fresh exact acceptance.

## Source notation

In the references below, `C` means `runtime/uzu-rust/deep-optimization/all/source/upstream/crates/uzu-engine`; `O` means `runtime/uzu-rust/upstream/crates/uzu-engine`. Paths and line numbers describe the reviewed local source. The candidate's default-disabled Cargo features and pinned reconstruction are documented by the owning experiment; generated runtime files are ignored artifacts.

The source comparison found the added direct-convolution kernels and changes to DeltaNet, attention mode, transformer/layer and scoring. Existing Metal attention, GEMM/GEMV selection, GDN recurrence, norm/gate and allocation code are unchanged by this patch chain.

## Correctness assessment

### Direct convolution preserves the relevant storage and arithmetic contract

`C/src/encodable_block/mixer/delta_net.rs:561-582` copies the preceding F32 state into scratch, allocates a distinct output projection and passes the original projection read-only. This prevents early-token history reads from racing with next-state writes, and prevents later tokens from reading already-convolved values. Each output/state element has one writer.

In `C/src/backends/metal/kernel/gdn/conv_direct.metal:25-50`, let `L` be suffix length and `H = kernel_size - 1`. An output row `t < L` reads padded rows `t + tap`. Rows below `H` come from the snapshot; remaining rows read input row `t + tap - H`. The largest input row is `L - 1`. The state writers use token indices `L..L+H-1`, yielding state taps `0..H-1`. For `L < H`, the first state taps retain old history at indices `L + tap`; later taps use the original suffix. These indices match the legacy pack/scan composition in `O/src/backends/metal/kernel/ssm/conv1d.metal:10-31` and `O/src/backends/metal/kernel/gdn/conv_scan.metal:39-56`.

Both scan forms initialize from the optional bias, visit taps in ascending order, accumulate in F32, apply the same SiLU helper and cast once to the outer type. Untouched channels copy their original outer-type bits. Source equivalence still depends on compiler arithmetic choices; the existing whole-vocabulary BF16 acceptance is the applicable executable evidence.

The wider `row_stride` dispatch has no apparent rounded-tail bounds defect. `C/build/metal/bindgen/dispatch.rs:44-76` emits `dispatch_threads_threads_per_threadgroup` with the exact axis extents. The direct kernel therefore receives precisely `L+H` by `row_stride` logical threads. Its interface relies on `state_stride == kernel_size - 1`, which the production caller supplies. A custom caller must retain this invariant.

### Hazard tracking supports the snapshot and later scratch reuse

The copy declares a read of state and write of history in `C/src/backends/metal/command_buffer.rs:155-183`. Generated kernel access records mark const buffers as reads and mutable buffers as writes (`C/build/metal/bindgen/arguments.rs:371-383`). The dispatch therefore triggers the required copy-to-read and state-read-to-write ordering through `command_buffer.rs:107-131`.

Dropping a scratch handle returns its page to the pool immediately (`C/src/backends/common/allocator/pool.rs:61-64`). Reuse within one command buffer is protected by GPU-address access tracking and barriers. Separate in-flight command buffers still need distinct pools, as the scorer supplies at `C/src/engine/language_model/scoring.rs:136-146`. Any allocation optimization must preserve both protections.

### Recurrence dimensions and batch policy are essential

DeltaNet's constructor restricts key/value head dimensions to 128 and convolution kernels to at least two taps (`C/src/encodable_block/mixer/delta_net.rs:177-194`). Those checks make the recurrence's unguarded four-value-row loads/stores valid: each group covers 16 value rows, and the host dispatches eight groups for a 128-row value head (`delta_net.rs:660-675`; `C/src/backends/metal/kernel/gdn/prefill.metal:43-67,106-110`).

The direct path preserves the existing selection of single-token update, sequential prefill and MXU chunked prefill. The chunked path starts at 256 tokens when MXU support exists (`C/src/backends/metal/kernel/gdn/chunked/mod.rs:20-24,37-88`). This distinction matters: the single-token update uses a different algebraic output formula and normalizes its F32 intermediate, while multi-token prefill casts the recurrence output to BF16 before the separate norm/gate kernel. Changing batches or forcing a recurrence family can change output bits even when the mathematical recurrence is equivalent.

### Inherited configuration-validation gap

The DeltaNet constructor does not validate positive head/group counts or `num_heads % num_groups == 0`. The config fields are plain `u32` values (`C/src/config/token_mixer/delta_net.rs:14-18`); the `uzu_config` macro adds strict deserialization, without relational validation. Kernels divide by `num_v_heads / num_k_heads` (`prefill.metal:47`, `update.metal:46`) and preparation loops over that quotient (`prefill_prep.metal:37,67-79`). Zero counts can fail during allocation or division. Nondivisible counts can map late value heads beyond the available key heads and leave preparation outputs unwritten.

This is an inherited loader/kernel contract gap, also present in `O`. It does not invalidate the accepted fixed JevK5 configuration. Before broader model admission, reject zero groups, zero heads and nondivisible head/group counts before allocation or dispatch. This finding is source-established; this review did not execute a malformed configuration.

### Feature and failure scope

`direct-delta-conv` is a compile-time capability: it affects eligible flat multi-token DeltaNet calls throughout a built engine. Single-token calls use the existing update kernel and tree calls return through tree verification before the direct branch. Generation, trees, F32 and other models retain separate acceptance requirements.

Deferred initialization clears its flag when fills are encoded (`delta_net.rs:509-521`). Reusing that state after abandoning or failing the command buffer could expose uninitialized state. The isolated fresh scorer discards failed state and drains pending commands; a general reusable-state API needs an explicit failure/reset contract before enabling this behavior more broadly.

## Unmeasured optimization candidates

### 1. Retain complete live tiles in the final visible batch

This is the strongest structural opportunity found. Current pruning handles final-layer calls with no observable output. In the visible final batch, attention, output projection and MLP still run for every row (`C/src/encodable_block/mixer/attention/mode.rs:76-157`; `C/src/encodable_block/transformer_layer.rs:417-440`), while the transformer normalizes only the requested output range (`C/src/encodable_block/transformer.rs:367-382`).

Propagate a scorer-only live-row range through that final layer. Keep the original suffix length, `m/n/k`, strides, tile coordinates, attention family, alignment flags and split count. Remove only complete dead output tiles. Retain the union of complete input tiles needed by each live consumer, including the neighboring rows of the final MLP/output-projection tile. Preserve every K/V row and all earlier-layer outputs. In grouped attention, query-head rows are flattened with suffix length, so the final row of each query head can occupy a different tile (`C/src/backends/metal/kernel/attention/gemm_grouped/mod.rs:95-97`).

The host dispatchers make a one-row substitution unsafe for exactness: attention changes family above eight rows, GEMM enables MXU at 64 rows, and grouped attention selects suffix-dependent split geometry. Matmul also selects GEMV/GEMM and tilings from the full shape (`C/src/backends/metal/kernel/attention/mod.rs:123-164`; `attention/gemm.rs:142-181`; `matmul/mod.rs:103-193`). A tile-preserving implementation is substantial work, but addresses compute beyond the current early-batch optimization.

### 2. Eliminate unused query/gate work from cache-only final-layer batches

The cache-only path projects full QKVG, normalizes Q and prepares a full query buffer whose result is discarded (`C/src/encodable_block/mixer/attention/mode.rs:59-72,231-235`). Introduce a dedicated KV-only preparation path using the existing packed projection stride and original K/V head offsets. Suppress query normalization and query/RoPE stores. The current helper's `num_q_heads = 0` mode assumes tightly packed KV (`mode.rs:225-230`), so passing zero with the existing QKVG projection would read the wrong channels.

Then consider skipping complete Q/G output-column tiles in the projection, while retaining the original GEMM policy, tile IDs and reductions for surviving K/V columns. Keep any boundary tile shared with K/V. QKV normalization is per head (`C/src/backends/metal/kernel/attention/qkv_norm.metal:7-11`), which gives a clear independence boundary for removing entire unused query head groups. Both stages need acceptance; their benefit is limited to cache-only final-layer batches, which occur only above the preserved batch boundary for this model.

### 3. Split convolved Q/K/V storage from unchanged gate/control channels

Direct convolution allocates a full-width outer-type output and dispatches copy threads for all channels beyond `conv_dim` (`conv_direct.metal:26,30-32`). A two-buffer internal representation could hold compact convolved Q/K/V and retain gate, beta and decay inputs in the original projection. Update GDN preparation, recurrence/chunked output and norm/gate readers to select the appropriate buffer and stride. Preserve casts and arithmetic exactly.

For outer-type width `w`, this removes `w * L * (row_stride - conv_dim)` logical output bytes and the corresponding unchanged-channel read/write traffic. Physical savings depend on power-of-two allocation bins. This removes work in every eligible DeltaNet layer, but expands several kernel interfaces and needs careful lifetime accounting for the retained original projection.

### 4. Allocate chunked scratch near use and release handles after their final read

The chunked encoder allocates ten intermediates up front and retains every handle until return (`C/src/backends/metal/kernel/gdn/chunked/mod.rs:100-119,137-181`). `log_decay` is dead after cumsum; `kk` is dead after diagonal inversion; `a_packed` and `a_inv` are dead after causal inversion. Allocate later outputs near their producer and explicitly release those dead handles after encoding their last consumer. Preserve access tracking so a later overwrite orders after the earlier GPU read.

This can reduce simultaneously live scratch and reuse allocation bins without changing arithmetic. The pool rounds fresh allocations to powers of two (`C/src/backends/common/allocator/pool.rs:21-26`), so report physical allocated bytes separately from logical tensor sizes. Any more aggressive arena plan must preserve independent pools for concurrently submitted batches. Additional reuse barriers can reduce overlap; latency improvement needs measurement.

### 5. Measure repeated GDN loads before changing recurrence geometry

Sequential prefill reloads Q/K, beta and decay for each value group and value head (`prefill.metal:45-55,69-73`). A larger value tile or staged Q/K reuse could reduce repeated loads while retaining each value row's F32 operation sequence, lane mapping and SIMD reductions. The existing kernel already keeps four value states in registers per SIMD group and explicitly unrolls them to avoid compiler spills (`prefill.metal:59-67`). Larger tiles can increase register pressure and lose occupancy; caches may already service repeated loads efficiently.

Norm/gate fusion also needs a BF16 round trip at the existing intermediate boundary and the original cross-value reduction. Its producer spans multiple independent value tiles, so fusion requires coordination and can increase threadgroup storage. These are profiling-led candidates with greater arithmetic and occupancy risk than scratch-lifetime changes.

## Readout and acceptance constraints

Full-vocabulary readout already receives the requested row count and the entire vocabulary (`C/src/encodable_block/decoder.rs:180-189`). Computing only option logits would change normalization and remove the whole-vocabulary digest contract. CPU conversion from BF16 to F32 occurs after pending GPU completion (`C/src/engine/language_model/scoring.rs:183-192`). Source inspection supplies no evidence that this conversion dominates request latency.

Prioritize the final live-tile path for potential compute reduction, the KV-only path for removal of clearly unused work, and scratch lifetime changes for the smallest arithmetic risk. Keep proposals isolated and default-disabled. Before interpreting timing, require exact BF16 vocabulary digests, calibrated outputs and typed decisions on the full frozen population and boundaries. Add targeted checks for tile/dispatch transitions, short retained history and poisoned/abandoned state where the candidate changes those contracts. Keep original batches, temperature, full-vocabulary normalization and fresh-state policy fixed. Existing combined acceptance applies to the unchanged combined executable only.
