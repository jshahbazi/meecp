# Bonsai onboarding prompt experiment

Standalone local evaluation for the bounded tasks described in [the report](../../docs/BONSAI_ONBOARDING_EVALUATION.md). It uses the existing locked project Python environment and Homebrew llama.cpp. It does not import the app or change onboarding contracts.

## Inputs and grades

- `source_case.py` reads bounded excerpts of the original sealed-package onboarding snapshot, verifies its README and detector hashes, and keeps excerpts in memory. The original snapshot must exist at its recorded local import path for source-discovery stages.
- `cases.py` defines 10 development and eight held-out synthetic decision cases, their policy, and two code-generation contracts. Expected answers are excluded from model input. Held-out cases are variations of the development families.
- `prompt_variants.py` contains the user-authored step-by-step wrapper prompt.
- `code_worker.py` has 36 validator vectors and 14 wrapper vectors. Reference implementations and deliberately incorrect implementations check the grader itself.
- `benchmark.py` serializes local CLI inference. Model inputs, outputs, generated code, and stderr stay in memory. It retains only hashes, counters, booleans, and fixed grading IDs.
- `results_summary.json` retains the selected cohorts' grades and individual provenance hashes. `summarize.py` rebuilds it from ignored local numeric artifacts.

Exact decision correctness requires every requested field to match the gold. A false-readiness flag means a blocked/ambiguous/stale case was called eligible, or installation was claimed authorized. A zero flag does not imply the decision was correct.

Generated Python runs through a restricted source gate in a separate `sandbox-exec` worker with writes and network denied, a two-second CPU limit, six-second wall timeout, and a caller-monitored 1 GiB RSS threshold. This source gate is an experiment capability restriction. It is not a production sandbox or a complete proof of Python isolation. Source rejection is reported separately from semantic test failure.

## Reproduce on this machine

Run from this worktree. The model download remains in the original checkout. No server is started, and no new dependencies are required.

```sh
PYTHON=/Users/johnshahbazian/development/mcpme/.venv/bin/python

# Grader reference and deliberately incorrect implementation checks.
"$PYTHON" experiments/bonsai_onboarding/code_worker.py --self-test

# Full source discovery and five decision prompt arms.
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage decisions \
  --output .build/bonsai-onboarding/decisions-final.json

# Compact decision prompt and frozen held-out variations.
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage refinement \
  --output .build/bonsai-onboarding/refinement.json
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage holdout --arm compact_schema \
  --output .build/bonsai-onboarding/holdout.json

# Guided source extraction with two context allocations.
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage atomic \
  --output .build/bonsai-onboarding/atomic.json
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage atomic --context 2048 \
  --output .build/bonsai-onboarding/atomic-2048.json

# Two adapter tasks, three prompt arms; corrected 14-vector wrapper grader.
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage code \
  --output .build/bonsai-onboarding/code-final-14.json

# User-authored prompt, greedy and three sampled runs.
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage user_prompt \
  --output .build/bonsai-onboarding/user-prompt-final.json
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage user_prompt --temperature 0.2 --seeds 7 \
  --output .build/bonsai-onboarding/user-prompt-seed7.json
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage user_prompt --temperature 0.2 --seeds 19 \
  --output .build/bonsai-onboarding/user-prompt-seed19.json
"$PYTHON" experiments/bonsai_onboarding/benchmark.py --stage user_prompt --temperature 0.7 --seeds 43 \
  --output .build/bonsai-onboarding/user-prompt-seed43.json

"$PYTHON" experiments/bonsai_onboarding/summarize.py
```

Sampling fixes top-k 40, top-p 0.95, and min-p 0.0. Decision/source/atomic budgets are 512/384/128 tokens. Code gets 1,536 tokens. The default context capacity is 4,096. Each request starts a fresh process; filesystem caches are warm. `--seeds` supports multiple decision samples; code stages use its first seed.

The manual ChatML prefix matches the pinned GGUF's system/user/generation clauses. Its embedded template always emits an empty completed `<think>` block. `--thinking` is retained only as an experimental open-block prefix for code trials; it departs from the embedded template and is excluded from the reported main cohorts.

Development and source cases were frozen before inference. Prompt refinements were informed by development failures. A grader audit added the detector-`ValueError` case before the final code reruns. Old exploratory files remain ignored and are excluded from `results_summary.json`.
