# Resumed deeper Uzu timing

Frozen before timing on 2026-10-11. The user reported that Brekko finished. Read-only preflight found its former process absent, no recognized model engines or compilers, and no active Brekko Jev workload. No external process needs suspension. This resumes the previously deferred experiment; earlier acceptance and smoke evidence remains unchanged.

## Comparisons

Run two separate four-quartet experiments through the immutable `benchmark_uzu_scoring_deep_latency.py` and `monitor_uzu_experiment.py`:

1. Primary incremental comparison: deeper `all` candidate against the previously accepted `combined` worker and its schema-1 receipt.
2. Secondary direct comparison: the same `all` candidate against the preserved historical worker.

Bind each experiment's exact baseline and candidate identities before inference. Both comparisons use the same checkpoint, temperature, context, token sequences, twelve-case length-only selection, ABBA/BAAB schedule, warmups, seals and scalar timing definitions in `DEEP_LATENCY_PROTOCOL.md`. Each has 20 serial resident processes and 480 measured requests, including four baseline-only control processes. Fully reap each engine before the next starts. Do not combine observations or multiply ratios across the two experiments.

Existing complete-population acceptance covers these binaries. Each timing request must still equal the sealed historical candidate outputs; changed artifact identities or outputs reject the run. No component-specific conclusions about isolated variants are planned. Their individual complete-population acceptance remains outstanding.

## Analysis fixed before timing

Use the quartet as the replication unit. Report each quartet's baseline/candidate ratio of case means, median and range across four quartets, independently for fresh and identical-repeat requests. A ratio above one favors the deeper candidate. Preserve decision wall time, score-request time and native component metrics separately. Include independent baseline controls and resident process peak RSS.

Supplement the existing overall analysis with the same calculations within the fixed short, medium and long token buckets: respectively 137/156/177/207, 290/360/518/793 and 1275/2406/2635/3387 tokens. Submission overlap and final-layer pruning activate only for the long bucket. Report all buckets and both conditions, including contrary directions. Length stratification cannot attribute a combined result to an individual patch.

The twelve selected public cases are a controlled local sample. Overall ratio of means gives slower cases more influence; it is not a full-population estimate. Baseline process variation is descriptive, not a significance threshold or confidence bound. No request-level bootstrap, new quality claim, extrapolation to longer contexts, new llama.cpp comparison, or multiplication into the historical 1.73x ratio is permitted.

## Isolation and reporting

The monitor samples process metadata every five seconds, rejects recognized external engines/compilers or overlapping owned engines, and records scalar CPU/RSS observations. Review generic background workloads separately. It does not continuously measure GPU usage, thermal state, clock speed or power. Do not call this a fully controlled hardware measurement.

Keep raw cases, token sequences, responses and individual timings in new ignored owner-only directories. Public reports contain counts, hashes, approved hardware metadata, token lengths, scalar timing/memory summaries and exactness outcomes. Verify configuration, blocks, seals, reference outputs and host records independently after both inference experiments finish. Preserve all prior bound source and evidence.
