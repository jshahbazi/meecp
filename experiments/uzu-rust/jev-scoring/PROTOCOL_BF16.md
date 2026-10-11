# JevK5 same-checkpoint BF16 control

This protocol is fixed before inspecting the BF16 control's engine scoring outputs. It repeats the [original probability-scoring protocol](PROTOCOL.md) after replacing the Q8_0 llama.cpp model with a BF16 GGUF converted locally from the exact source checkpoint used for the Uzu BF16 export. The historical Q8_0 reports and their provenance remain separate. The purpose is to determine whether the measured probability and calibration differences persist after matching source weights and storage precision.

## Artifact identity

Both exports must derive from `alibiserikbay/JevK5` revision `c4f7fdb3aeab5582336406e78d3bef11bf98833d`. Its original `model.safetensors` has SHA-256 `13824e47f2e40fe052f06943976cf742cb366ba305741a111e75a8ebae907a9c`. Preserve every source-file hash, the calibration bytes, and the tokenizer bytes already recorded in `MODEL_PROVENANCE.json`. Bind that unchanged Uzu manifest by SHA-256 in `BF16_CONTROL_PROVENANCE.json`.

Convert the original source into a GGUF with the official llama.cpp converter at revision `86d86ed4396b4130922f7b9af26e3d9fc11a591b`, using `--outtype bf16` and locked dependencies. Preserve a clean source archive, converter source-file hashes, the wrapper recipe and dependency-lock hashes, output size and SHA-256, and verified tensor-type counts. Require BF16 tensors and permit F32 tensors used for one-dimensional or otherwise intentionally retained parameters. Reject quantized tensor types, a different checkpoint hash, an unfinished conversion, a changed recipe, or a changed converter. Record any metadata adaptation explicitly; it must preserve source weight bytes.

The source configuration declares one multi-token prediction layer, but the verified source safetensors contains no MTP tensors. Pass `--no-mtp` to prevent the converter from incorrectly including that nonexistent layer in GGUF metadata. Require exactly 32 transformer blocks and absent or zero `nextn_predict_layers`. Record the policy as `excluded: no MTP tensors in source checkpoint`; both engines score the base checkpoint without MTP.

Use the exact Uzu scoring binary, Uzu model files, native build receipt, llama.cpp runtime closure, and reaper from the completed `RESULTS.json` comparison. The new harness rejects a change to those artifact identities. Thus the llama.cpp weight artifact is the controlled runtime change. Matching source weights and BF16 storage does not guarantee identical accumulation, kernels, or final-logit readout precision. Retain Uzu's existing BF16 readout for this control and disclose any residual difference.

## Inputs and scoring

Use the same 231 MIT-licensed public JevBench v1.2 items: easy 48, original 72, hard 111. Use all 12 existing authored known-answer synthetic cases, reported separately. Preserve the existing ordered dataset and rendered prompts in `DATASET_PROVENANCE.json`; do not filter cases after examining outputs. Reject invalid criteria, incomplete candidate coverage, nonfinite values, token mismatches, over-context inputs, or truncation.

Use the unchanged vendored official prompt and typed decision mapping. Preserve option order, special-token parsing, and the closed thinking prefix. Letter temperature is **1.22** and knockout temperature is **0.93**. With 2–16 options knockout temperature remains inactive. Do not refit either temperature.

At the final prompt position, independently compute each candidate probability as `exp((logit - max_logit) / 1.22) / sum(...)`. Full-vocabulary log probabilities may replace logits because their common normalizer cancels. Require Uzu's returned candidate probabilities to match the independent formula within **1e-12 absolute error** and probability sums within **1e-12**. Verify upstream typed answers: confidence is max probability, noul is probability of true, choice is stable ordered argmax, and score is expected level.

Require identical rendered prompt hashes, exact input token IDs, and candidate token IDs in every accepted pair. Keep raw identities private and publish their hashes. Context remains **8192**, with no context shifting, Metal, one active call, and one resident engine. Uzu performs prefill/readout without generation. llama.cpp computes one sampled token to expose next-token log probabilities; this is a decision-latency comparison.

## Runtime comparison

Run **two** resident rounds in opposite load orders: llama.cpp then Uzu, followed by Uzu then llama.cpp. Completely reap one engine before loading the next. Run every case in the same fixed order. Each fresh call (`cache_prompt:false`) is followed immediately by the identical-repeat call. llama.cpp may reuse only the consecutive exact token sequence. Uzu creates fresh state for both calls and reports `cache_hit:false`.

Report fresh and repeat timing populations separately. Include startup time, peak resident memory, tokenization, scoring-request time, decision wall time, and available prefill/readout timings. Exclude startup from resident decision latency. Report p50/p95 and paired speed ratios. Break latency and ratios into the original token buckets: below 256, 256–1023, 1024–4095, and 4096 or more; publish zero counts for empty buckets. Run no concurrent GPU workload and wait for unrelated heavy compilation to finish before timing. Temporarily suspend authorized external model services if needed and restore them afterward.

## Probability and calibration comparison

Retain the original engineering tolerances: **strict match** if every paired option absolute difference is at most **1e-3**, **close match** if every difference is at most **1e-2**, and **different** otherwise. Report mean/max absolute difference, L1, total variation, argmax agreement, and confidence/noul/expected-score differences. Retain the screening targets of at least **99% argmax agreement** and maximum typed scalar differences **1e-3**. Report failure counts and near-tied decision changes.

Use only the first fresh observation per engine/case for public quality. Later observations measure stability. Report argmax accuracy, rounded expected-value accuracy for score items (Python round), ordinal MAE, multiclass Brier sum against one-hot labels, and top-label ECE with 10 equal-width bins using argmax correctness. Report tier and question-type breakdowns. Source files have gold decisions, not gold probability distributions.

Report Uzu minus llama.cpp paired metric differences and a **20,000-draw paired bootstrap 95% percentile interval**, seed **20261010**. Keep screening margins at **1 percentage point accuracy reduction** and **0.01 Brier/ECE increase**. Point-estimate margins alone do not establish noninferiority. Compare the BF16 result's residual probability and calibration differences with the separate historical Q8_0 report, while keeping the contemporaneous BF16 pair as the primary control. Do not infer universal calibration from public development items.

## Privacy and reproduction

Keep raw inputs, tokens, logits, probabilities, decisions, and inference responses in owner-only ignored `test-artifacts/` files. Console output contains authored status codes and counts only. Tracked reports contain source links, hashes, protocol/artifact identities, scalar metrics, and counts. Seal all private records and verify configuration, dataset, block, model, converter, and runtime identities before aggregation. Never overwrite the historical reports.

After converting and verifying the BF16 GGUF, run `uv run --locked python scripts/benchmark_jev_uzu.py run --llama-reference bf16 --uzu-model models/uzu-jevk5/bf16 --gguf BF16_GGUF_PATH --output test-artifacts/jev-uzu-screen-bf16`. Aggregate into a new report with `uv run --locked python scripts/benchmark_jev_uzu.py aggregate --run test-artifacts/jev-uzu-screen-bf16 --report experiments/uzu-rust/jev-scoring/RESULTS_BF16.json`. Default Q8_0 commands retain the original protocol. Resume only matching sealed configurations with complete engine blocks; partial blocks require a new output directory.
