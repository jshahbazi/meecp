# Consecutive score cache acceptance protocol

This protocol is frozen before native model acceptance. Timing is deferred. No latency, throughput, memory improvement, or speed claim is an acceptance result. The scope is the isolated experimental native worker and probe; production HTTP/MCP integration, KV reuse, and prefix reuse remain outside this work.

Historical sources, original validators, patch stacks, binaries, private references, and published reports remain immutable. A fresh versioned build is required at `runtime/uzu-rust/consecutive-cache/builds/sol-cache-v2`. Preserve the failed, unsealed v1 build attempt separately; v2 corrects a unit test to compare values through the same serialized protocol path before native model acceptance. Its receipt binds the pinned Uzu revision and archive, the complete original/deeper/Sol combined patch stack, original worker/probe references, the sealed Sol control, new isolated worker/probe/cache sources, copied build sources, builder, locked dependencies, binaries, and completed native unit-test receipt. The new validator also binds every imported historical helper, the independent auditor, acceptance/audit test sources, the protocol, model files, process owner, reaper, dataset provenance, and all sealed reference files. Rehash every binding after execution and before any aggregate publication.

## Contract

`--score-model-path PATH` defaults to fresh state. Only an explicit `--consecutive-cache` enables reuse. Readiness must state `cache_enabled`, `cache_policy` (`fresh-state` or `consecutive-full-vocabulary-logits-v1`), and `cache_key: exact-token-ids+context-tokens+model-instance`. Worker mode is `exact-token-scoring-cache-v1`; digest mode is `exact-token-digest-cache-v1`.

One entry stores the complete raw f32 vocabulary logits, exact input token vector, and context limit for that model instance. A successful consecutive score with exactly equal tokens and context may reuse the distribution. Any different token vector or context replaces the entry. Candidate IDs, candidate order, and temperature belong to the current readout and do not belong to the cache key. Every hit runs the unchanged candidate readout again with the current request. A/B/A is three native executions. Valid tokenize helpers preserve the last successful score entry.

Every parse, validation, native execution, readout, serialization, write, or flush failure invalidates reuse. A pending score result cannot publish an entry or commit a hit before the successful reply flush. Native/readout failure fences the worker. EOF and restart discard process-local state. `reset_cache` clears only the entry and replies with `cache_reset: true`; it preserves cumulative telemetry. Every readiness and normal reply includes `cache: {enabled, hits, native_score_calls}`. Native calls count actual `score_next_token` invocations, including attempted invocations that fail; these are not allocation counters. A successful hit increments `hits` in its flushed reply and has no additional native call. Hit state-creation and prefill duration fields must be zero; current readout is computed again. Timing fields may remain in private wire records solely for structural validation and are never aggregated or published.

## Preregistered functional run

Use only the sealed `test-artifacts/uzu-optimization-combined` native reference: 243 cases, 486 case outputs, 48 boundary requests through 8192 tokens, and 534 full-vocabulary digests. Validate original reference seals before launching a model.

1. Launch the new worker enabled. Tokenize all sixteen candidate letters and require unique single-token IDs. Execute three retained warmups for the first case. Reset explicitly after warmups. For each of the 243 cases, tokenize the actual rendered prompt, reset, execute a cold score, then the identical score. Retain 486 outputs. Both members must equal their respective frozen native reference outputs by types and floating-point bits for candidate IDs, logits, log probabilities, probabilities, and typed decisions. Equality to the fresh reference cannot substitute for checking the paired repeat reference. Compute expected reuse using actual exact tokens and context, never case IDs.
2. For each of the 48 frozen boundary requests, reset and execute cold/hit pairs, retaining 96 outputs. Both members must match the frozen boundary candidate outputs exactly. Stop and reap the worker before another process launches.
3. Launch the new digest probe enabled. It must share the worker's cache, candidate readout, validation, and publication path. Retain three warmups, then reset explicitly. For each of the 534 original reference requests, reset and execute cold/hit pairs. Retain 1068 full-vocabulary digests. Each must equal the original full f32 vocabulary digest (`to_bits`, little-endian), vocabulary size, token digest, and input count. Stop and reap the probe.
4. Launch the same newly compiled worker with the flag omitted. Check the default-off handshake and A/A smoke: both execute native scoring and both outputs match exactly. In this independent default-off process, execute every successful score request in the sequence below to obtain fresh native control outputs, including all candidate and context/temperature variants. Stop and reap this control before launching the enabled sequence worker.
5. Start an enabled worker with an explicit reset. A is the first reference case's actual token vector; B changes its final token to a distinct valid candidate token. Use the first two candidates for A/B calls; changed candidate set uses the next two. Changed context is `max(len(A), 8191)`, required to differ from the original 8192 context. Changed temperature is exactly half the reference 1.22. Execute these fixed steps in order:

   | Step | Expected behavior |
   | --- | --- |
   | A, A | miss, hit |
   | B, A | miss, miss |
   | Valid tokenize of A's candidate letter, A | tokenize preserves entry, hit |
   | Changed context A twice, original context A | miss, hit, miss |
   | Reversed candidates, different candidate set, half temperature | three hits with current readout |
   | Malformed JSON line, A | fixed parse error, miss |
   | Duplicate candidate IDs, A | fixed validation error, miss |
   | Empty tokenize text, A | fixed tokenize validation error, miss |
   | Tokenize with unknown field, A | fixed parse error, miss |

   Every successful score's candidate output must match its independent uncached native control by type and floating-point bits. Validate cumulative hit/native-call telemetry after every response, including successful tokenize, reset, and fixed errors. Each error must invalidate the next score. End this process by closing stdin and require normal EOF exit zero before reaping it. Start a new enabled process and require the first A to miss and equal its fresh control. Stop and reap it.

The main reference run retains 486 case responses, 96 boundary responses, and 1068 whole-vocabulary responses, plus warmups and the separately declared sequence, default-off controls, and restart smoke. Native tests use a deterministic mock backend to cover error paths not safely inducible in real model execution: native/readout failures; serialization, write, newline, and flush failures; deferred publication; malformed/invalid score and tokenize; reset; default off; exact-key changes; EOF/restart isolation. Bind the completed test receipt into the configuration before model acceptance.

## Evidence and publication

All processes are serialized through `ProcessOwner` with the supervised reaper. Scratch and output directories are mode 0700; datasets, requests, responses, configuration, snapshots, and seals are mode 0600 under ignored locations. Do not print raw requests/responses, prompts, options, evidence, model output, native exceptions, or captured native logs. Progress uses fixed stages and scalar counts. Unexpected failure prints only a fixed authored error; preserve that private attempt and rerun in a new directory after repairing the bound source.

Seal configuration and artifact snapshot before model execution. Retain private `worker.json`, `digest.json`, and `sequences.json`, then a post-run seal binding their hashes and settled ownership. The independent audit must reject missing rows, altered requests, invalid expected hits, altered candidate output bits/types, any full-vocabulary mismatch, false default-off/restart claims, counter inconsistencies, and changed bound artifacts. Public output contains only counts, hashes, scalar native quality, exactness booleans, and `timing_deferred: true`. Model quality must equal the native reference. A successful cache acceptance establishes functional distribution reuse and native-call avoidance only; precise timings require a separately preregistered later protocol.
