# Short-block Uzu scoring latency protocol

Frozen before this latency experiment. The earlier complete comparison preserved exact outputs but its two resident rounds exhibited material timing drift. This follow-up measures shorter, counterbalanced resident blocks and independent baseline-process controls. It does not replace or modify earlier protocols, private records, or reports.

## Frozen selection

Select 12 public cases from the sealed complete comparison's first baseline round using input-token length only. Exclude synthetic cases. Use four cases in each bucket: below 256 tokens, 256–1023, and 1024–4095. Within a bucket, stable-sort by length, preserving original dataset order for ties. For bucket size n, select zero-based ranks floor(n/8), floor(3n/8), floor(5n/8), and floor(7n/8). These midpoint quartiles are fixed before new timing outputs. Require four unique cases per bucket. Preserve the resulting short-to-long case order in every block.

## Schedule and timing unit

Run four cycles. Each begins with a separate baseline-only control process and continues with a four-process comparison quartet. The quartet orders are ABBA, BAAB, ABBA, BAAB, where A is the preserved baseline and B is the candidate. Each process loads independently, performs three identical warmups using the first selected case, then scores all 12 cases with a fresh request and immediate identical repeat. Fully reap each process before starting the next.

This produces 20 resident blocks and 480 measured requests: 384 comparison measurements and 96 baseline-only control measurements. Model load, warmups, and process teardown are excluded from request timings. Decision wall time includes tokenization and transport. Preserve separate score-request and native state-creation, prefill, readout, score, and total metrics.

`--quartets 2` runs the first two cycles as a separate smoke experiment: ten blocks and 240 measured requests. Never merge smoke observations into the full experiment.

## Semantics, binding, and privacy

Preserve the same BF16 export, official prompt rendering, exact input and candidate token IDs, candidate order, temperature 1.22, context 8192, fresh state, and full-vocabulary normalization. Validate calibrated probabilities and require candidate logits, normalized log probabilities, probabilities, and typed decisions to equal the sealed complete baseline for every warmup and measured request.

Reuse the existing receipt validator, model verification, ProcessOwner, private-file helpers, and response checks. Bind the new harness, this protocol, all reference records and seals, model files, binaries, receipts, source files, build recipes, and helpers before inference; verify them afterward and during aggregation. A candidate path and receipt may be supplied for an existing supported isolated variant. Do not modify historical source or receipts to make a new candidate pass verification.

There is no new whole-vocabulary probe run. The sealed complete experiment establishes whole-vocabulary equality for its rebuilt original-core and candidate probes. The preserved historical worker exposes candidate outputs only. An alternate candidate binary needs separate whole-vocabulary validation; these latency measurements verify its selected candidate outputs.

Use a new ignored output directory with mode 0700 and files mode 0600. Raw requests, tokens, responses, scores, and individual case IDs remain private. Tracked reports contain sanitized relative artifact identities, hashes, counts, token lengths, and scalar statistics. Console output contains authored status codes and counts.

## Analysis

The independent comparison unit is the quartet. Within each quartet and condition, average each case's two baseline process observations and its two candidate process observations. Report their latency distributions, paired absolute differences, paired ratios, and ratio of case means, separately for wall, score-request, and native component times. Report each quartet and the distribution of quartet-level wall ratios.

For the independent baseline-only controls, report per-process timing and each case's observed process-to-process range, both in seconds and divided by its control mean. This describes local process/time variation; it is not a calibrated noise bound.

Do not bootstrap individual requests as independent samples or claim precise steady-state speed from this small local experiment. Interpret the direction, size, and consistency of changes alongside the baseline controls. Exact output preservation establishes semantic agreement on the selected cases, not new model quality or calibration evidence. Missing blocks, identity changes, invalid timings, altered seals, privacy failures, or output differences fail aggregation.

## Deeper experiment boundary

This is a separate 2026-10-10 experiment using schema-2 receipts from `build_deep_scoring.py` and additive patches after the accepted topology and deferred state initialization patches. Historical builders, protocols, harnesses and native artifacts remain byte-for-byte preserved. The deeper full-population harness is `benchmark_uzu_scoring_deep.py`; the balanced timing harness is `benchmark_uzu_scoring_deep_latency.py`. Every new candidate needs its own exact candidate-output and whole-vocabulary checks. Semantic rejection overrides any timing gain.

The timing baseline is fixed per experiment before inference. It may be the preserved historical worker or the previously accepted `combined` worker with its schema-1 receipt supplied through `--baseline-receipt`. The latter comparison measures incremental gains over the previous optimization. Both implementations must match the same sealed historical outputs. Artifact bindings name the exact baseline binary, receipt and source chain; results from different baselines are separate experiments.
