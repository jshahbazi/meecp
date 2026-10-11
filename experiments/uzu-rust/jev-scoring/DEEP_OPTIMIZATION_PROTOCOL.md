# Fresh Uzu scoring optimization protocol

Frozen before optimization inference on 2026-10-10. This experiment compares the preserved Uzu 0.6.1 scoring worker with a separately built candidate using the same JevK5 BF16 export. It does not change the installed JevK5 package or previous benchmarks.

## Identity and isolation

Bind the original upstream revision `8d01581474760bd1fc7f4a547ff1d02f7dada437`, original scoring patch, ordered candidate patch chain, worker sources, dependency lock and manifests, build receipts, binaries, harness and helper sources, protocol, dataset provenance, reaper, and every model export file before inference. The preserved baseline executable has SHA-256 `af047d5ae3fcf0c088443aad40fbe0f3d35cf137e4cb15e390a1f922103ea04a`. Reverify bound files after inference and aggregation. Never overwrite historical artifacts.

Raw results reside only in a new ignored `test-artifacts` directory with mode 0700 and files mode 0600. Console output contains authored status codes and counts. Each resident native process is fully reaped before the next starts. No concurrent GPU experiment is permitted.

## Fixed population and semantics

Use all 231 public cases and 12 separate synthetic cases from the sealed dataset at `test-artifacts/jev-uzu-screen-bf16-resumed/dataset.json`. Preserve official rendering, option order, exact input and candidate token IDs, temperature 1.22, context 8192, and no truncation. Score fresh state for every request. Preserve full-vocabulary normalization of returned candidate log probabilities.

Require exact equality of candidate logits, full-vocabulary-normalized candidate log probabilities, reported candidate probabilities, and typed decisions across baseline/candidate, immediate repeats, and rounds. Verify each response independently against the calibration formula. Candidate equality alone does not prove equality of every vocabulary logit.

Optional separately built digest probes hash all final vocabulary logits as little-endian f32 values, exposing only a digest and scalar identity fields. If both probes are supplied, verify their binaries and source receipts and run them in separate untimed resident blocks on every selected case and boundary scenario. Require exact digest equality, token identity, and vocabulary size. Missing probes must be reported as whole-vocabulary equality unverified. Digest probes never contribute latency measurements.

Whole-vocabulary equality compares the rebuilt original-core probe with the optimized-core probe. The preserved historical timing worker exposes candidate outputs only; their exact equality supplies the connection to the historical implementation.

## Timing

Two resident rounds run baseline then candidate, followed by candidate then baseline. Each of 243 cases receives a fresh request and an immediate identical repeat, producing 1,944 timed measurements. Perform three scoring warmups per block and exclude startup and warmups. Decision wall time includes tokenization and transport. Report separate score-request and native state-creation, prefill, readout, score, and total metrics.

Report sample counts, p50/p95, paired baseline/candidate ratios, token buckets, and per-round timing. Attribute improvements only to this local measurement. Block order reversal limits one source of bias; it does not establish a precise steady-state speed guarantee or continuously measure background load.

## Boundary and state-contamination validation

Outside timing, tokenize a fixed benign synthetic sentence and deterministically repeat its tokens to lengths 1, 2, 255, 256, 257, 1023, 1024, 1025, 2048, 2049, 4096, and 8192. At each length score the sequence, its immediate repeat, a sequence with one changed token, and the original sequence again. Require exact outputs across implementations and repeated originals. These checks exercise chunk boundaries and changing prior requests without persisting cached prompt state. They are not quality cases.

## Smoke and reporting

`run --smoke` uses the 12 synthetic cases, one baseline/candidate round, and all boundary checks. Its 48 timed measurements form a separate report and never replace the full population. Build variants and ablations use separate output directories and reports.

Quality uses only each engine's first fresh observation per case. Public accuracy, Brier, ECE, and score metrics remain separate from synthetic checks. Exact equality preserves the measured baseline outputs; it supplies no new production-calibration claim.

Incomplete blocks, missing measurements, identity changes, invalid timing, privacy failures, altered seals, or output differences fail aggregation. Store only scalar aggregates, identities, hashes, and counts in tracked reports.

## Deeper experiment boundary

This is a separate 2026-10-10 experiment using schema-2 receipts from `build_deep_scoring.py` and additive patches after the accepted topology and deferred state initialization patches. Historical builders, protocols, harnesses and native artifacts remain byte-for-byte preserved. The deeper full-population harness is `benchmark_uzu_scoring_deep.py`; the balanced timing harness is `benchmark_uzu_scoring_deep_latency.py`. Every new candidate needs its own exact candidate-output and whole-vocabulary checks. Semantic rejection overrides any timing gain.
