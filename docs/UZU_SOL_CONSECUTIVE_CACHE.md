# Sol Uzu consecutive scoring cache

The additive worker under `experiments/uzu-rust/consecutive-cache/` adds opt-in reuse of the most recent successfully returned complete vocabulary logits. It keeps the accepted Sol engine patch stack and raw scoring computation unchanged. Caching is disabled by default. Production JevK5 scoring, HTTP/MCP integration and precise timing remain outside this implementation.

The cache stores one exact input-token vector, its context allocation bound and a complete validated `Vec<f32>` of vocabulary logits. It retains no native transformer or KV state. On an identical consecutive scoring request, it skips `LanguageModel::score_next_token` and recalculates the unchanged candidate readout and calibration from the saved vocabulary. This preserves requested candidate order and temperature; a cached candidate response is never reused. Source checks confirm that both math function bodies are byte-identical to the historical worker.

## Mechanism and llama.cpp comparison

The existing native scorer allocates fresh transformer state on every call, evaluates the prompt and destroys the state. Its final prompt chunk intentionally omits state acceptance because no subsequent chunk needs that state. Retaining it for KV continuation would require an additional engine contract and acceptance work.

The llama.cpp scorer sends a new completion request with `cache_prompt=true` only when the complete token sequence matches its previous successful decision. The retained BF16 records show 972/972 repeats evaluating exactly four native prompt tokens, whereas 972 fresh calls evaluated their complete 104–4033-token inputs. These structural counts remain useful even though the run's timings were excluded after Mac sleep. The [scalar trace](../experiments/uzu-rust/jev-scoring/CONSECUTIVE_CACHE_TRACE.json) binds the record seal and source revision.

The [matching llama.cpp source](https://github.com/ggml-org/llama.cpp/blob/7fe450e19305b828c199d602c23a8337aaa1f03b/tools/server/server-context.cpp#L2941-L3525) reuses prompt state, restores checkpoints and creates a checkpoint four tokens before the prompt end. It then computes the current completion probabilities. The observed four-token evaluation is consistent with that checkpoint path; the exact branch was not instrumented.

This Uzu feature retains the completed raw distribution in CPU memory. It skips native scoring on a hit and performs vocabulary normalization and candidate calibration again. New speed comparisons need to identify these different mechanisms explicitly.

## Reuse lifecycle

The exact key is token-vector equality plus `context_tokens`, scoped to the worker's immutable loaded model instance. The token hash remains response provenance and is never sufficient to establish a hit. Every score request takes the previous entry before input/candidate/context/temperature validation. A mismatch immediately discards the old entry. A/B/A therefore runs native scoring for all three requests. Changed context is a miss even when tokens match.

Every request is processed sequentially, including response serialization and flush. A newly computed or reused entry becomes reusable only after a complete response has been serialized, written, terminated with a newline and flushed. Parse, validation, tokenization, native, readout, serialization and transport failures clear reuse. Native scoring or invalid-score faults retain fatal process fencing. The worker's single mutable request owner implements the experimental serialization requirement; the production single-decision-lock contract remains unchanged.

Successful tokenize helpers preserve the most recent scoring entry: a decision normally tokenizes before it scores. “Consecutive” consequently refers to scoring requests. Failed helpers clear reuse. `reset_cache` explicitly clears the entry while retaining cumulative counters, and a failed reset response leaves the entry cleared. EOF, process exit, unloading, restart and worker destruction drop the in-memory entry. No cache file or token/distribution diagnostic is written.

The entry is bounded by the model vocabulary and the existing maximum context of 16,384 tokens: approximately `4 * vocab_size + 4 * input_tokens` bytes, plus vector metadata. Request parsing still has its separate existing 1 MiB bound. Peak transient request/reply memory is separate from retained cache memory.

## Versioned protocol

Both binaries use the same `src/cache.rs`, `src/scoring.rs` and `src/protocol.rs`, including the cache and publication path. The digest probe computes SHA-256 over every actual returned raw logit as `f32::to_bits().to_le_bytes()`. It never exposes raw vocabulary values.

```sh
runtime/uzu-rust/consecutive-cache/builds/sol-cache-v2/bin/meecp-uzu-cache-worker \
  --score-model-path VERIFIED_MODEL_DIRECTORY \
  --consecutive-cache
```

Omitting `--consecutive-cache` preserves fresh native scoring on every request. The probe binary is `meecp-uzu-cache-score-digest` and accepts the same arguments. No environment variable implicitly enables caching. Unknown or duplicate flags are rejected.

The ready handshake declares:

- Worker mode `exact-token-scoring-cache-v1`; probe mode `exact-token-digest-cache-v1`.
- `cache_policy`: `consecutive-full-vocabulary-logits-v1` when enabled, otherwise `fresh-state`.
- `cache_enabled`: an explicit boolean.
- `cache_key`: `exact-token-ids+context-tokens+model-instance`.
- Original upstream revision and original scoring patch hash, vocabulary size and scalar cache telemetry.

Existing score request fields remain unchanged. The new experimental reset request is `{"operation":"reset_cache"}`; its successful reply contains `cache_reset:true` and scalar telemetry. All normal replies include `cache:{enabled,hits,native_score_calls}`. `native_score_calls` counts actual native scoring API dispatches, including failed attempts. It does not count internal allocations or kernel submissions. `hits` counts successfully flushed hit replies; the hit reply presents its anticipated count and the worker commits that count only after flush.

The worker retains `cache_hit` alongside original score response fields. A hit reports zero native state-creation/prefill time and newly measured readout, score-request and total time. Saved historical timings are never replayed. Nonfatal errors return fixed authored codes and cleared-cache telemetry. Native/readout faults return their fixed error reply, followed by fixed terminal framing and process exit. Process diagnostics and native captures cannot enter protocol output.

## Immutable build and verification

Current experimental scoring development uses the [standalone Stark dependency lock and helper](STARK_DEPENDENCY.md). The patch-based commands and acceptance records below retain their historical source identities for reproduction.

`experiments/uzu-rust/build_consecutive_cache_scoring.py` creates a fresh destination, reconstructs the pinned upstream Git archive and applies the existing original/deep/Sol patch chain. It uses copied versioned worker sources and the unchanged locked dependency graph. The old worker, probe, source tree, manifests, recipes, patches, binaries and reports are untouched.

```sh
uv run --locked --group dev --python 3.12.13 python \
  experiments/uzu-rust/build_consecutive_cache_scoring.py \
  --name new-fresh-build-name --test
```

The create-only `BUILD_RECEIPT.json` binds both new binaries, original Sol control and receipt, all original patch hashes, new/live and copied source hashes, prepared upstream sources, generated manifest/lock, compiler version and `UNIT_TESTS.json`. Build/test uses ordinary Rust without `RUSTC_BOOTSTRAP`. Unit tests use a fake scoring backend; neither build nor test loads a model. Receipt files are private and contain only scalar metadata and hashes. `candidate_identity()` revalidates these bindings for the new functional validator.

The unit suite covers default-off execution, actual native dispatch count, A/A and A/B/A, changed context, candidate order and temperature, complete vocabulary bit digests, successful/failed tokenization helpers, parse/validation failures, native/readout fencing, pending replies, write/flush failures and reset semantics. The accepted `sol-cache-v2` build passed 19 ordinary Rust tests in each binary, 38 executions total. The earlier unsealed `sol-cache-v1` attempt remains preserved: its test compared a parsed wire float with an unserialized float; the corrected test uses an independently fresh worker through the same protocol serialization. Scoring math was unchanged.

## Native acceptance

The [preregistered functional protocol](../experiments/uzu-rust/jev-scoring/CONSECUTIVE_CACHE_PROTOCOL.md) completed against the same BF16 checkpoint and sealed original native reference. The [independent audit](../experiments/uzu-rust/jev-scoring/CONSECUTIVE_CACHE_RESULTS.json) replayed actual tokens/context and cumulative counters, rehashed 1,612 bound artifacts and passed:

| Check | Result |
| --- | --- |
| 243 case cold/hit pairs | All 486 candidate outputs and typed decisions exactly equal their frozen references by type and binary64 bits. |
| 48 context-boundary cold/hit pairs | All 96 outputs exact, including requests through 8,192 tokens. |
| 534 full-vocabulary cold/hit pairs | All 1,068 raw f32 vocabulary digests exact. Worker and probe share the actual scoring/cache path. |
| Current candidates, order and temperature | Hit outputs exactly equal independent default-off native controls. |
| A/B/A, context changes and four fixed errors | Correct misses and invalidation; successful tokenize preserves the last score entry. |
| Default off, normal EOF and restart | Default-off calls always score natively; normal EOF exits zero; restarted first request is cold. |
| Actual execution avoidance | Every hit leaves `native_score_calls` unchanged and reports zero native state-creation/prefill work. |

The worker's 585 score replies including warmups used 292 native calls and 293 hits. The probe's 1,071 replies used 535 native calls and 536 hits. The separate sequence used nine native calls and six hits; all 15 score replies matched independent uncached controls. Counters include warmups and remain cumulative across explicit resets.

Quality and calibration stayed exactly equal to the native Uzu reference: public argmax 202/231, synthetic argmax 12/12, public Brier 0.18515405977818739 and ECE-10 0.030787072608270183. Maximum independently recomputed formula error was 2.220446049250313e-16. The preexisting differences from llama.cpp remain unchanged. All model processes were stopped and reaped before the post-run seal. The 78 focused Python builder, validator and independent auditor tests passed; independent Sol review found no runtime blocker.

The sealed protocol's unit-coverage sentence overstates serializer-specific, newline-specific and EOF/restart mock coverage. The Rust suite isolates initial-write and flush failures and verifies deferred publication; serialization/newline failure ordering was inspected in source. Normal EOF/restart was exercised by the native functional sequence and mocked Python launcher tests. This clarification leaves the preregistered protocol and its sealed identity unchanged.

This run establishes functional reuse and skipped native calls. Precise performance measurement remains deferred at the user's request. Production Meecp scorer/HTTP/MCP integration, installed package acceptance and native KV/prefix reuse remain future work. The earlier sleep-contaminated timing window remains excluded.
