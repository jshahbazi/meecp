# Preparation prompt lab

Run small, editable onboarding questions against the existing Bonsai model. Each case supplies all its input, its output schema, and an independent expected answer. The harness sends only the system prompt, user prompt, and optional shape schema to the model. It grades strict JSON, schema validity, and exact values separately.

## Use

Run these commands from the repository root:

```sh
uv run --locked python scripts/test_preparation_prompts.py list
uv run --locked python scripts/test_preparation_prompts.py show choose_callable
uv run --locked python scripts/test_preparation_prompts.py run
```

`show` prints the authored system prompt, user prompt, schema, and gold answer for local inspection. `run` prints grades, failed field names, and elapsed time. Runtime prompts, responses, and engine diagnostics stay in memory; reports contain hashes and measurements.

Edit [cases.json](cases.json) to change a question or add a case. Edit [system.txt](system.txt) to change the shared system prompt. To try one replacement prompt while keeping its schema and expected answer fixed:

```sh
uv run --locked python scripts/test_preparation_prompts.py run \
  --case confidence_rules --prompt-file /absolute/path/to/prompt.txt
```

Use `--case` multiple times to select several cases. `--mode plain`, `--mode schema`, and the default `--mode both` compare unconstrained generation with llama.cpp JSON-schema constraints. The schema constrains shape and types; the loader rejects answer-bearing keywords such as `enum` and `const`.

```sh
uv run --locked python scripts/test_preparation_prompts.py run \
  --case confidence_rules --mode both --repeat 3 --temperature 0.2 \
  --output .build/prompt-lab/my-run.json
```

Repetitions increment the seed from 42; paired modes use the same seed. At temperature zero, repeats generally duplicate greedy output. Exit codes: `0` all answers passed, `1` at least one trial failed, `2` invalid inputs or unavailable pinned runtime, `130` interrupted.

Global `--cases` and `--system-file` options go before the command:

```sh
uv run --locked python scripts/test_preparation_prompts.py \
  --cases experiments/prompt_lab/cases_with_placeholders.json run \
  --output .build/prompt-lab/placeholders.json
```

## Frozen comparison

The 12 synthetic cases cover callable selection, parameter rules, nullable outputs, literal return values, runtime files, image limits, missing definitions, unknown values, a source-comment injection, empty requirements, and a boundary test value. Their miniature source snippets model the historical hotdog callable; they do not read an import or execute supplied code.

The first pack used literal `null` and empty-list response examples. Bonsai copied those placeholders in nine failing schema trials. The revised pack changes only the final response instructions to name the keys and types. Inputs, questions, schemas, expected answers, shared system prompt, and generation settings are identical.

| Prompt pack | Plain exact answers | Schema exact answers |
|---|---:|---:|
| [Placeholder examples](cases_with_placeholders.json) | 2/12 | 3/12 |
| [Field instructions](cases.json) | 8/12 | 11/12 |

These are single greedy runs on development cases. The revised schema pack still fails `missing_definition`. This comparison measures bounded extraction, not complete app preparation or detection accuracy. Numeric trial records are in [results.json](results.json).

## Runtime provenance

The runner reuses the chat template and invocation from `experiments/bonsai_onboarding/benchmark.py` at mcpme commit `70ec097792d3abf060aa84f8964ca0514ff52b14`, written in the linked “Assess a small onboarding model” chat.

- Model: `Ternary-Bonsai-1.7B-Q2_0_g64.gguf`, 490,163,968 bytes; SHA-256 `6d0ecb3d9055969b5cde332b6fdb60e67ed3599e9f73e56b977731e1467e5c91`.
- Default model location: `models/bonsai-1.7b/` in this checkout. Its existing Apache 2.0 license and notice remain alongside it.
- Engine: `/opt/homebrew/bin/llama-completion`, build 11146, commit `7fe450e19`; executable SHA-256 `3ea8c0a6fc8dd1d9f577211321d07daecaa073cd28e139dd533dd4a69a6c7a88`.
- Python dependencies: existing `uv.lock`; no dependency changes. Homebrew runtime libraries are supplied by the existing local installation. This is a developer tool, not a portable bundle.
- Defaults: 2,048 context tokens, 256 output tokens, temperature 0, seed 42, 99 GPU layers, six threads, flash attention on, top-k 40, top-p 0.95, min-p 0. Each trial starts a fresh process with the GGUF's completed empty reasoning block.

The runner verifies model and engine bytes before generation. `--model` and `--engine` accept relocated copies of those exact artifacts. Requests run sequentially. Timeout or interruption terminates the owned process group. The report records artifact, fixture, prompt, schema, gold, harness, and Python-lock hashes with decoding settings and numeric results.

## Verify the harness

```sh
uv run --locked python -m pytest -q tests/test_preparation_prompt_lab.py
```

The tests exercise rejection of malformed and misleading answers, answer leakage through schemas, fixed gold under prompt replacement, paired seeds, metadata-only reporting, and timeout cleanup with a synthetic local worker.
