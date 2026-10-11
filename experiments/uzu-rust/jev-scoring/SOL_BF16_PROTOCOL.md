# Full BF16 Sol optimization experiment

Freeze this protocol, build identities, native acceptance, helpers, monitor, independent audit and dataset before timing. Preserve all earlier runs and artifacts.

## Population and arithmetic

Use all frozen 243 cases from the native reference, in original order. Report the 231 public development cases separately from 12 synthetic cases. Use the same JevK5 source checkpoint, Uzu BF16 export and llama.cpp BF16 GGUF from the accepted experiment; temperature 1.22, context 8192, original prompt rendering and candidate tokens, llama.cpp top-40 readout. Model export layouts and arithmetic still differ across engines.

## Controls and ordering

Run three engines: unchanged llama.cpp, the previously accepted `deep-optimization/all` Uzu worker (`uzu-control`), and a newly accepted additive Sol candidate (`uzu`). Only one owned model process runs at a time. Each resident load gets three checked fresh warmups followed by all 243 cases, each with fresh then immediately identical-repeat input. Uzu uses fresh state in both conditions. llama.cpp must actually evaluate the full input for fresh and fewer input tokens for the identical repeat.

Two mirrored six-block cycles use the following orders:

1. llama.cpp, Uzu control, candidate, candidate, Uzu control, llama.cpp.
2. Uzu control, candidate, llama.cpp, llama.cpp, candidate, Uzu control.

This gives four independent loads per engine, 5832 measured responses and 36 checked warmups. Compute llama.cpp/candidate and control/candidate ratios of population means within each cycle, averaging each engine's two loads per case. Also report paired case median ratios, public-only results, fixed token buckets, per-block timing, startup, RSS and pooled quantiles. Ratios above one favor the candidate. Two cycles describe local performance and drift; do not claim precise steady-state statistical significance. Do not multiply speedups from unrelated historical baselines.

## Exactness and timing isolation

Before timing, rerun native acceptance including all 486 fresh/repeat case outputs, 48 context boundaries through 8192 and 534 whole-vocabulary digests. Every native scalar must be identical by binary64 representation to the sealed original Uzu reference. Recheck native equality for both Uzu engines on all measured responses and warmups. Quality/calibration must reproduce reference metrics exactly. Independently recalculate public metrics, timing, identity, seals and probability differences with the standard library.

Coordinate the already authorized chats for a window without local model work or compilers. Sample process metadata every five seconds; abort the owned run on detected competing models/compilers, simultaneous owned models, or any sampled external process above 200% CPU. Record background CPU/RSS aggregates without command lines, prompts or responses. Sampling does not measure continuous GPU/thermal isolation. Exclude incomplete attempts and preserve them separately. Resume coordinated work after completion or interruption.

## Privacy and sealing

Raw requests, responses, case records and configuration stay ignored under private directories (0700; files 0600). Standard output contains fixed stages and counts. Bind and rehash the complete model/runtime/source closure before and after each completed experiment. Publish scalar summaries and provenance only. Candidate selection may reject bit-different or slower optimizations; retain the current accepted engine as the control and default experimental engine until a measured replacement is justified.
