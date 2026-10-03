# Bonsai 1.7B onboarding evaluation

Measured locally on October 3, 2026. The tested Bonsai checkpoint did not produce a fully correct onboarding decision or an accepted adapter across the reported prompt trials. The user's step-by-step wrapper prompt improved one sampled result to **11/14 behavior checks**, with validation-error handling still failing. This checkpoint needs further evidence before it can support Phase 3 onboarding.

The [earlier speed benchmark](BONSAI_17B_BENCHMARK.md) establishes its small weight file and fast execution. This experiment tests bounded parts of the onboarding workflow, without installing an app or changing mcpme.

## Grounding in the previous onboarding

The referenced chat is [Prepare sealed mcpme package](codex://threads/01a0fd99-084d-7351-864d-c76598434a0e). Its initial task prepared the Hotdog-or-Not-Hotdog app for a sealed mcpme package. The original import snapshot exposes `detector.detect_hotdog(image_path, confidence=0.35)`. It requires a verified ONNX artifact, a portable locked Python runtime, an MCP stdio adapter, dependency notices, and checks after relocation.

The historical preparation included source hashing, model recovery and verification, adapter tests, a restricted relocated run, missing-notice remediation, and a final package digest. Its resulting state was ready to plan; the task did not authorize installation. Hashing, dependency resolution, licensing records, execution receipts, packaging, and installation authority require host verification.

The evaluation reads bounded excerpts from that original snapshot: README, detector implementation, model provenance, and six pinned dependencies. It verifies the README and detector hashes before inference. It does not use the later base64-enabled app as the original case. The model receives no gold answers or previous successful adapter.

## Model, runtime, and experimental controls

| Item | Value |
| --- | --- |
| Machine | Apple M5 Max; 18 CPU cores; 128 GB; macOS 26.7.1 |
| Runtime | Existing Homebrew llama.cpp 0.5.0, build 11146, commit `7fe450e19` |
| Backend | Metal; all layers offloaded; flash attention; six CPU threads |
| Model | `Ternary-Bonsai-1.7B-Q2_0_g64.gguf` |
| Model file | 490,163,968 bytes |
| Model SHA256 | `6d0ecb3d9055969b5cde332b6fdb60e67ed3599e9f73e56b977731e1467e5c91` |
| Publisher revision | `983b5dec2ff16aab79990711ba0f828a499a7e6a` |
| Main sampling | Greedy, seed 42; top-k 40; top-p 0.95; min-p 0.0 |
| Context | 4,096 tokens; guided extraction repeated at 2,048 |
| Output budgets | Decision 512; full source discovery 384; single field 128; code 1,536 |

The publisher identifies the base architecture as Qwen3-1.7B. The [publisher's current format guide](https://github.com/PrismML-Eng/Bonsai-demo/blob/main/Bonsai1_README.md#upstream-status-for-ternary-bonsai-1) confirms that the group-64 file runs on mainline llama.cpp. Model provenance and Apache 2.0 license links are in the speed report.

The pinned GGUF contains a 4,063-byte chat template with SHA256 `30a75d10e60b57e2f260420163dd59720dacf9f63b9a8de070d65dd80a7b30f7`. Its generation clause always closes an empty reasoning block. The main trials use that published prefix. Exploratory open-`<think>` trials departed from this template and often produced no closing marker. They are excluded from the main results and do not establish performance in a supported reasoning mode.

Every request starts a fresh CLI process, with warm filesystem caches. Wall times include startup and loading, and exclude code grading. Inference calls are serialized. Runtime inputs, generated responses, candidate code, and stderr remain in memory. Only numeric counters, hashes, booleans, and fixed grade identifiers are retained.

## Decision prompts

Ten synthetic development cases cover the historical preparation shape, a complete CPU function, an existing MCP server, ambiguous entrypoints, a mismatched model, missing notices, stale source, README instruction injection, signature disagreement, and an unavailable artifact. The policy and gold were frozen before inference. Policy facts and evidence are supplied explicitly; these tests do not ask the model to inspect the filesystem or actually verify hashes.

Each answer has six fields: status, supported template, module, function, blocker codes, and installation authority. An exact pass requires every field to match. False readiness means an ineligible case was called eligible, or the answer claimed installation authority. The same facts and policy are retained across prompt arms.

| Prompt arm | Exact decisions | Valid schema | False readiness |
| --- | ---: | ---: | ---: |
| Minimal | 0/10 | 9/10 | 0/10 |
| Explicit checklist | 0/10 | 1/10 | 0/10 |
| Unrelated few-shot example | 0/10 | 10/10 | 7/10 |
| Host-organized fact table | 0/10 | 10/10 | 4/10 |
| Fact table plus runtime JSON grammar | 0/10 | 10/10 | 4/10 |
| Compact output instructions plus grammar | 0/10 | 10/10 | 3/10 |
| Compact prompt, eight held-out variations | 0/8 | 8/8 | 0/8 |

The compact prompt was a development-informed refinement that avoids embedding a JSON Schema definition in the instructions. All eight held-out answers had incorrect blocker lists; three also had incorrect status and candidate fields. Their zero false-readiness count does not imply correct preparation decisions.

The held-out cases are synthetic variations of known families, with different names, missing notices, artifact failures, signatures, and injected text. They are not eight independent real apps. No case evaluates an authorized installation or the JevK5 model-service template.

## Original-source discovery and guided extraction

Full discovery asks for template, module, function, required parameter names, optional defaults, and model filename from the bounded original source packet. All five arms fail the complete answer. Only the runtime-constrained arm produces the required schema; it still gets the module and required parameters wrong.

Six separate field questions produce **4/6 correct** at both 4K and 2K context. The incorrect fields remain the module and required parameters. These are guided extraction questions: the template question says the interface is an ordinary Python function, the defaults schema supplies the `confidence` key, and the model-path question supplies the directory prefix. The result cannot be described as unassisted source discovery.

## Adapter generation and the user's revised prompt

Two tasks isolate adapter behavior from model execution:

1. `validate_arguments`: absolute path and confidence checks, no extra fields, unchanged input, fixed validation errors. The grader has 36 vectors, including booleans, nonfinite numbers, bounds, NULs, Unicode, and path length.
2. `invoke`: call trusted validation and the detector once, suppress both Python streams, preserve successful dict identity, map each error correctly, and avoid modifying arguments. The final grader has 14 vectors.

Generated code passes a restricted AST/import gate before execution in a separate worker. The worker denies writes and network, limits CPU and wall time, and has a caller-monitored RSS threshold. A source-gate rejection is not a semantic score and can reject otherwise usable Python outside the permitted subset.

With minimal, explicit, and unrelated-example instructions, none of the six drafts is accepted. All three validators are rejected by the source gate. All three wrappers reach execution but pass 0/14 complete behavior vectors. These are conjunction checks: a single recurring stream leak can fail every vector.

The user supplied the more concrete step-by-step prompt preserved verbatim in [`prompt_variants.py`](../experiments/bonsai_onboarding/prompt_variants.py). The same code-specific system instruction is retained. It directs Bonsai to create one `StringIO` sink, redirect stdout and stderr together, and handle validation and detector exceptions separately.

| Revised-prompt sample | Passed behavior vectors | CLI wall time |
| --- | ---: | ---: |
| Greedy, seed 42 | 0/14 | 0.686 s |
| Temperature 0.2, seed 7 | 0/14 | 0.687 s |
| Temperature 0.2, seed 19 | **11/14** | 0.703 s |
| Temperature 0.7, seed 43 | 0/14 | 0.602 s |

An independent in-memory replay of the greedy result identified the failures precisely. It suppresses stdout and restores both streams, but leaks stderr in every vector. It also propagates three validation failures instead of returning the specified errors. The signature, trusted call counts, result identity, detector error mapping, and non-dict result mapping work. The 11/14 sample fails the three validation-error vectors. None of these four samples passes the whole contract.

A canonical implementation of the user's exact steps passes **14/14**. During grader review, a missing detector-`ValueError` case was added: an incorrect combined validation/detector `try` previously passed 13/13 and now fails that case. Final code trials use the corrected worker SHA256 `cd8b0da1b387addc808ba499ff363dbc6444d596952e2fe437306c2a312e97f1`. Grader self-tests pass **16/16**, including reference validators, shared-sink wrappers, deliberate error-mapping failures, stream leaks, mutation, and disallowed imports. Strict JSON preflight also rejects duplicate keys and nonfinite constants, accepts the source gold, and rejects boolean numeric defaults.

Initial code trials had a conflicting decision-oriented system instruction. Those outputs, the earlier worker revision, and unsupported open-think trials are excluded from the selected code cohorts.

## Latency and footprint

| Selected workload | Mean CLI wall time | Highest peak RSS | Highest peak physical footprint |
| --- | ---: | ---: | ---: |
| Five source + 50 decision requests, 4K | 0.786 s | 1,131.07 MB | 708.94 MB |
| Compact development decisions, 4K | 0.634 s | 1,134.66 MB | 711.74 MB |
| Compact held-out decisions, 4K | 0.651 s | 1,134.44 MB | 711.89 MB |
| Guided extraction, 4K | 0.553 s | 1,139.57 MB | 716.83 MB |
| Guided extraction, 2K | 0.573 s | **906.23 MB** | **482.58 MB** |

The 2K extraction requests used 1,610–1,619 prompt tokens and did not exhaust the 128-token generation budget. Every selected request exited normally. One minimal validator draft exhausted its 1,536-token code budget; other selected requests did not exhaust their budgets.

RSS and physical footprint are overlapping macOS counters and must not be added. The figures describe individual process peaks, not machine-wide incremental memory. The 2K results support a small allocation for this bounded workload; they do not establish that a complete onboarding fits within 2K context or under 1 GB. The existing JevK5 service remained running, and no new server or runtime was installed.

## Phase 3 implications

Keep the host responsible for source identity, artifact hashes, supported contracts, license retention, package validation, and installation authority. Fixed adapter templates can implement stream suppression and error mapping reliably. An LLM could propose an entrypoint or explain missing information, with host validation before a proposal advances.

This particular Bonsai checkpoint does not yet justify taking that role: even guided source extraction misses two basic fields, and no tested complete decision is correct. The revised wrapper prompt shows that more concrete instructions can improve a draft. It also shows why acceptance must depend on behavior checks.

Fine-tuning was not tested. These 18 synthetic decision cases and one app are too small a basis for a training or production-readiness claim. Any adaptation would need several real app families, frozen unseen examples, and an explicit false-readiness acceptance bar while retaining the small local runtime budget.

## Artifacts and reproduction

The [experiment README](../experiments/bonsai_onboarding/README.md) gives commands and boundaries. [`results_summary.json`](../experiments/bonsai_onboarding/results_summary.json) preserves 95 selected requests across ten cohorts, with per-case grades, numeric metrics, and model/fixture/grader/harness hashes. Harness versions differ as stages were added; the individual hashes are retained.

Fixture SHA256: `61dd72026265db7175a30373c88946684864f21969d7b482c7f0974c4498689d`. Original-source loader SHA256: `d4f255cfe7b2f59979cf950d7bd3cca3f975c96fb3e0f2a26cfa33f65e8414f6`. Local full numeric artifacts are ignored under `.build/bonsai-onboarding/`. No generated adapter is installed, committed, or retained.
