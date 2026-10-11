# Sol review of optimized Uzu versus llama.cpp BF16 evaluation

Reviewed on 2026-10-11. The existing scorer and complete-population acceptance provide a sound basis for a new controlled comparison. The original BF16 harness cannot directly accept the optimized worker because it deliberately freezes the original Uzu executable. Use a separate versioned adapter, protocol, monitor and audit; retain historical files and artifacts unchanged.

This review covers local source, small sealed result records and one lightweight host inventory. It performs no model inference, compilation or model-file hashing. New comparison timings and their independent audit are pending when this review is written.

## Historical records and exactness

The [BF16 control](UZU_JEVK5_BF16_CONTROL.md) records 231 public cases and 12 separate synthetic cases. Its interrupted session and missing monitoring receipts for three retained blocks limit historical timing interpretation. Those limitations do not invalidate the recorded stable probability outputs, and they do not automatically apply to a new continuously monitored comparison.

[The BF16 runtime verifier](../scripts/benchmark_jev_uzu.py#L477) compares every runtime artifact except the GGUF against the historical Q8 report. Its rejection of a changed Uzu worker is intentional. The new adapter should obtain the historical BF16 binding using the preserved original worker, then bind the optimized `all` worker independently through the [schema-2 receipt verifier](../scripts/benchmark_uzu_scoring_deep.py#L42). Only Uzu's declared optimization identity changes in the comparison; llama.cpp executable/library closure, BF16 GGUF, Uzu model export, reaper, prompt and calibration remain fixed.

There is a small historical representation difference that matters to exact-output claims. [The BF16 measurement helper](../scripts/benchmark_jev_uzu.py#L663) stores independently recalculated Python probabilities, while [the optimization helper](../scripts/benchmark_uzu_scoring_deep.py#L210) stores the worker's reported probabilities. A private scalar-only comparison of all 486 first-round Uzu rows found:

| Field | BF16 record versus original optimization reference |
| --- | --- |
| Exact input and candidate token IDs | All equal |
| Candidate logits and log probabilities | All equal |
| Candidate probabilities and typed decisions | 98 rows differ |
| Maximum option probability difference | `2.220446049250313e-16` |

Therefore, the new optimized worker's bit-exact native probabilities and decisions must be compared with the sealed original optimization reference, which retains native probabilities. Independently recalibrate to audit the formula. The complete `all` acceptance record matches that original reference for all 486 candidate outputs, including f64 bit comparisons of logits, log probabilities and probabilities. The historical BF16 record remains suitable for unchanged llama.cpp identity, input parity and descriptive cross-engine comparison, with its recomputation disclosed.

Small private-record checks verified configuration pre/post seals, record hashes and settled ownership for the historical BF16 run, original optimization run and combined deep acceptance run. These checks did not rehash model/runtime files or re-run the full historical acceptance machinery.

## Controls required in the new adapter

Use every public and synthetic case in original dataset order. Preserve the [official prompt and option mapping](../vendor/jevk5/prompt.py), the closed thinking prefix, candidate-letter order, temperature 1.22, context 8192 and no truncation. Both engines must independently tokenize the same rendered prompt and produce exactly equal input and candidate IDs. Uzu tokenization always uses `add_special_tokens=False`; the helper's `parse_special=True` argument controls llama.cpp parsing and must not be interpreted as a Uzu special-token change.

Keep fresh and identical-repeat requests separate. [The completion helper](../scripts/benchmark_jev_uzu.py#L655) sends `cache_prompt=False` for fresh llama.cpp calls and enables reuse only for the immediately preceding identical repeat. Its `cache_hit` field records requested policy. Require actual native `prompt_tokens_evaluated` to equal the full input length for fresh calls and to be positive and smaller than the input length for repeats. Retain the observed counts. Uzu must report fresh state and zero cache hits in both conditions. Three excluded warmups per resident process should be checked and retained; use fresh requests so the first measured llama.cpp fresh request still evaluates its full prompt.

Retain the optimized worker's actual candidate logits, full-vocabulary-normalized candidate log probabilities, candidate probabilities and typed decision. [The unchanged Rust readout](../experiments/uzu-rust/src/scoring.rs#L121) calculates the full-vocabulary log partition in f64, then independently calibrates ordered candidate logits. Reapplying candidate softmax to normalized log probabilities is valid because the common log partition cancels. Candidate recalibration alone does not prove the absolute full-vocabulary partition; unchanged source identity, sealed native log probabilities and the prior whole-vocabulary digest acceptance provide that bridge.

Use two complete counterbalanced quartets, ABBA then BAAB. Eight serial resident processes produce 3,888 measured requests across all 243 cases and 24 checked warmups. This improves control of order and temporal drift compared with two reversed engine blocks. Report each quartet and engine block, token buckets, public/synthetic populations, p50/p95 and paired ratios. The process/quartet is the replication unit for host timing variation. Do not treat 972 case pairs as independent machine-state replications or bootstrap them into a steady-state significance claim.

Wall and scoring-request latencies use monotonic seconds. Convert to milliseconds only for presentation. Preserve tokenization, transport and typed-decision work in wall latency. [The llama.cpp helper](../scripts/benchmark_jev_uzu.py#L675) converts native milliseconds to seconds; native prompt/decode timers need separate labels because they cover different operations from Uzu's state creation, prefill and readout. Startup and warmups are excluded. llama.cpp performs one output-token operation to expose probabilities; Uzu reads final prompt logits. Report decision latency, not generation throughput.

Only the first fresh observation per public case contributes to quality and calibration. Later observations check stability. Report stable ordered argmax accuracy, multiclass Brier sum averaged over cases, ten-bin top-label ECE, true/false probability, confidence, expected score and separate rounded expected-score accuracy. Public cases lack gold probability distributions. Retain the frozen thresholds and distinguish descriptive engine differences from production calibration or new generalization evidence.

## Seal and independent-audit checks

The new adapter should require the exact block filename set, unique block identities, exact row order, all expected case/condition pairs, consistent warmup count and checked warmup responses. [The original aggregate](../scripts/benchmark_jev_uzu.py#L913) checks count and indexed uniqueness but does not independently require exact ordered rows or its pre-run seal. Strengthen these checks in the versioned adapter without rewriting the old helper.

Bind the historical BF16 configuration, reports, native reference records and seals, optimized receipt/patch chain, helper sources, new protocol/monitor and all runtime/model identities before and after the run. Store raw cases, responses and private host metadata only in owner-only ignored directories. Export allowlisted aggregate fields and hashes. A standard-library-only audit should independently reconstruct calibration, typed decisions, Brier, ECE, quality differences, quantiles and paired/quartet timing arithmetic; verify f64 output bits against the sealed native Uzu reference; and verify private seals, exact coverage, host sample counts and settled process ownership. Bind the audit source to its report.

## Host inventory and remaining uncertainty

At 13:22:31 UTC, the lightweight metadata sample observed zero recognized model engines, zero recognized compilers and zero generic Python processes with the inspected model/training flags. Ten processes referenced Brekko paths: three `claude`, three `disclaimer`, two `caffeinate`, and two Python orchestration scripts. The scripts were `recover.py` at 0.0% CPU and 151,600 KiB RSS and `deliver_results.py` at 0.0% CPU and 32,368 KiB RSS. They had no inspected model/training flags. Other Brekko-associated processes were at most 0.4% CPU. This supports no observed active contention; it does not establish that all Brekko processes have exited.

The largest sampled ordinary desktop CPU readings were WindowServer at 21.2%, Codex service at 9.0% and Codex renderer at 6.0%. These are process-average readings from one sample. The [existing monitor](../scripts/monitor_uzu_experiment.py#L15) recognizes engines and compilers by executable basename. Generic Python workers can evade that list, as the deep experiment already documented. The new monitor should privately classify command metadata for known model/training workloads and record only safe names/counts/CPU/RSS. No raw arguments, evidence, prompts, responses, tokens or exception dumps should be logged.

Even stronger process classification and five-second sampling do not continuously measure GPU activity, temperature, clocks or power. Reject observed competing models/compilers and any overlap of owned engines. Retain one continuous receipt for the complete new timing window and report residual desktop activity and host drift. No speed conclusion is established by this review itself.
