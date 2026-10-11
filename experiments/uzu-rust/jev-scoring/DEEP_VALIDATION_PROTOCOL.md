# Untimed deeper Uzu exact-output acceptance

Frozen before this acceptance run. Leave the user's background Brekko process running. Timing is deferred: this experiment collects no decision, request, or tokenization durations and makes no timing-isolation or speed claim. Native metrics already returned by the worker may remain in private responses for validity checks; omit them from public results.

## Fixed reference and candidate

Validate the complete sealed reference at `test-artifacts/uzu-optimization-combined` through the immutable original helper chain. It contains all 231 public and 12 synthetic cases, first-round historical-baseline fresh/repeat outputs, 48 boundary requests, and 534 original-core whole-vocabulary digests. Verify all reference seals and artifact bindings before use.

The candidate defaults to the `all` variant under `runtime/uzu-rust/deep-optimization/all`. Require its schema-2 build receipt, exact ordered patch chain and features, worker/probe hashes, source hashes, build recipes, generated manifest and lock, and unchanged BF16 model provenance. The worker and digest probe must share that receipt. Preserve the original upstream revision, official prompt, exact input/candidate IDs and order, temperature 1.22, context 8192, fresh state, and full-vocabulary-normalized candidate log probabilities.

## Serialized validation

Launch only the candidate worker. Retokenize each official case, verify exact reference input and candidate IDs, and issue fresh then immediate identical-repeat scores for all 243 cases: 486 responses. Then replay the reference's 48 synthetic boundary/state-contamination requests, covering lengths 1, 2, 255, 256, 257, 1023, 1024, 1025, 2048, 2049, 4096, and 8192 with fresh, repeat, changed, and return-to-original sequences. Require exact candidate logits, normalized log probabilities, reported probabilities, and typed decisions. Independently recompute calibration and report its maximum absolute formula error.

Fully reap the worker before launching the candidate digest probe. Replay the same 534 case/boundary requests in the same order and require every whole-vocabulary little-endian-f32 digest, vocabulary size, input count, and prompt-token digest to equal the preserved original-core probe. Perform and retain three checked warmups before each process's validation sequence, using the first reference request. Warmups are separate from the 534-response counts.

Whole-vocabulary equality compares the candidate probe with the preserved original-core probe. The historical worker exposes candidate outputs only, which are checked separately. This acceptance does not claim unexposed historical-worker vocabulary bits were captured.

## Evidence and reporting

Create a new ignored private output directory with mode 0700 and files mode 0600. Save schema-3 configuration, artifact snapshot, and pre-run seal before inference. Bind the new harness and protocol, imported helper chain, reference records/seals, model files, native binaries, receipt/source/build files, and reaper. Verify file identities after inference and again during aggregation. Require two complete reaped-process records, exact ordered population coverage, checked warmups, and post-run hashes. Missing records, changed identities, invalid values, or any output difference fail acceptance.

Raw tokens, responses, logits, probabilities, case IDs, and native metrics stay private. Console output contains authored counts and status codes. Public reports contain sanitized relative artifact paths and hashes, scalar counts, equality outcomes, maximum calibration error, and quality aggregates. Public quality uses the first fresh response for each of 231 public cases; the 12 synthetic cases remain separate. Require both quality populations to equal the reference. This establishes output preservation, not new calibration or generalization evidence. State explicitly that Brekko remained active and timing was deferred.
