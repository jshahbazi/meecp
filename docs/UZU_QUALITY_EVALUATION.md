# Uzu blind quality evaluation

This screen compares the pinned Qwen3.5-0.8B Mirai-L/Uzu and Q8_0/llama.cpp configurations from the [Uzu experiment](UZU_EXPERIMENT.md), using the current 256-output-token profile. It evaluates the answers those configurations serve. Different model conversions prevent attributing a quality difference solely to the engine.

## Public prompt source and fixed method

The prompt source is Tatsu Lab's [AlpacaEval dataset](https://huggingface.co/datasets/tatsu-lab/alpaca_eval), pinned at revision `2edc6fad8be6b14ea7230aabfd08188da6b8b814`. The downloaded `alpaca_eval.json` contains 805 instructions and declares CC-BY-NC-4.0. The [AlpacaEval evaluator repository](https://github.com/tatsu-lab/alpaca_eval) has its own code license. This is a custom subset and judge protocol, not an official AlpacaEval score or leaderboard submission.

Selection occurred before generation. Exact duplicates and empty or over-limit instructions would be excluded mechanically; all 805 source rows were eligible. Seed `20261010` selects and shuffles 100 distinct instructions using proportional allocation across the five upstream source datasets. The allocation is 16 `helpful_base`, 20 `koala`, 23 `oasst`, 31 `selfinstruct`, and 10 `vicuna`. Source hashes, selected row IDs and prompt hashes are recorded in [dataset provenance](../experiments/uzu-rust/QUALITY_DATASET_PROVENANCE.json). Prompt wording is preserved.

The [protocol](../experiments/uzu-rust/QUALITY_PROTOCOL.md) was written and hash-bound before generation. Each engine answers every prompt once with the same rendered prompt and token sequence, greedy sampling, thinking disabled, fresh state, a 4096-token context, and a 256-token output limit. Two 50-case blocks reverse engine order; engines run serially and are reaped between loads. The harness rejects changed inputs/configuration on resume and requires all 200 answers before creating the blind view.

The judge is one fresh `gpt-6-astra` agent with no inherited chat history. Per-case A/B positions are privately randomized and exactly balanced 50/50. The judge receives only case IDs, prompts, A/B answer text, a neutral schema and the [judging rubric](../experiments/uzu-rust/BLIND_JUDGE_INSTRUCTIONS.md). It receives no identities, timings, token counts, stop reasons, source labels, reference answers or mapping. Files are copied to an owner-private temporary workspace with a neutral name. This is procedural blinding, not enforced OS isolation.

Astra rates each answer from 1 to 5 and selects A, B or tie. Correctness, relevance, requested constraints and completeness take precedence over style or length. Identical answers must tie; poor answers can also tie. Candidate instructions are untrusted data. All 100 unique judgments must pass schema and coverage checks before the private mapping is applied.

## Measured results

Completed on 2026-10-10 on the Apple M5 Max development Mac. All 200 answers passed prompt-token parity checks, with no generation/startup errors. All four engine loads settled ownership and reaped their native processes. The judge completed 100 unique valid decisions; the blind files remained unchanged, and judgments were sealed before unblinding. A separate read-only audit reproduced the counts and statistics.

| Metric | Uzu / Mirai-L | llama.cpp / Q8_0 |
| --- | ---: | ---: |
| Preferred answers | 21 | 26 |
| Tied pairs | 53 | 53 |
| Mean overall score, 1–5 | 1.98 | 1.95 |
| Answers scoring 4 or 5 | 9/100 | 9/100 |
| Answers scoring 1 or 2 | 69/100 | 72/100 |
| Answers reaching the 256-token cap | 68/100 | 69/100 |
| Empty answers / generation errors | 0 / 0 | 0 / 0 |

Uzu's preference share, counting ties as one half, was 47.5% with a paired bootstrap 95% interval of 41–54%. Among the 47 non-ties, Uzu won 21/47 (44.7%); the exact two-sided sign test gives `p = 0.560`. The mean paired score difference, Uzu minus llama.cpp, was +0.03 with a 95% interval of −0.11 to +0.17. Four pairs had byte-identical answers and correctly received ties. These results do not establish a meaningful quality advantage or equivalence.

Absolute judged quality was low for both configurations. A post-hoc qualitative follow-up by the same still-blinded Astra judge identified fabricated facts, incorrect explanations/calculations, missed constraints, task misreading, repetition and incomplete answers. It also observed substantive defects before many endings. Truncation reduced completeness, but the test did not measure how much a larger budget would help. Those qualitative findings supplement the sealed scores; they did not change any judgment.

The [content-free results record](../experiments/uzu-rust/QUALITY_RESULTS.json) contains all 100 ordinal results, source and A/B-position breakdowns, artifact/judge/seal hashes, score distributions, 20000-resample intervals, cap counts and verification status. Prompts, answers and reasons remain in owner-private ignored artifacts.

Uzu remains useful for engine experimentation: this screen found no clear relative-quality gap alongside the earlier speed improvement. The current 0.8B, 256-token profile needs stronger absolute-quality evidence before serving general assistant tasks. A larger supported model and a longer-output diagnostic would be sensible next experiments; neither has been tested here.

## Reproduce and continue

```sh
uv run --locked python scripts/prepare_uzu_quality.py
uv run --locked python scripts/evaluate_uzu_quality.py generate \
  --dataset test-artifacts/uzu_quality_100.json \
  --output test-artifacts/text-quality-screen
```

Use `--offline --dry-run` to verify the pinned source and selection without changing outputs. Use `generate --resume` with the same arguments only for an interrupted run; changed dataset, protocol, rubric, harness, runtime closure, model or configuration identities are rejected.

After generation, copy only `judge/cases.jsonl`, `judge/judge-schema.json`, and the neutral rubric into a new mode-0700 temporary directory; set files to mode 0600. Verify their hashes against the generated seal. Launch a fresh Astra subagent with `model="gpt-6-astra"` and `fork_turns="none"`, providing only that directory and the rubric. Have it write `judgments.jsonl` with mode 0600. Verify the supplied cases/schema/rubric remain byte-identical before aggregation.

```sh
uv run --locked python scripts/evaluate_uzu_quality.py aggregate \
  --run test-artifacts/text-quality-screen \
  --judgments /absolute/private/blind-workspace/judgments.jsonl \
  --report test-artifacts/text-quality-screen/aggregate.json \
  --judge-identity /root/blind_quality_judge
```

Keep prompts, answers, reasons, randomization seeds and the mapping in private ignored artifacts. Only source provenance, ordinal results, aggregate statistics and identity hashes are promoted to Git. Console/runtime logs contain no evaluation content or exception dumps. The harness's 19 focused tests cover blinding, tamper/resume checks, matched settings, judgment validation and statistical handling.

## Interpretation limits

This is one judge and one selected public prompt sample. It does not measure judge disagreement, benchmark contamination, higher-budget output quality, longer conversations, all model families or production safety. The 256-token cap is part of the tested deployment profile; a long requested answer can fail because of that cap. Truncation is reported and judged as served, with no forgiveness for an imagined continuation. Relative ties do not establish adequate absolute quality, and nonsignificance does not establish equivalence. Bootstrap intervals condition on this sample and this judge.
