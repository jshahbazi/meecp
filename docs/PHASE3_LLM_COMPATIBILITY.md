# Phase 3 local text generation compatibility

## Fixture selected before acceptance

Selected on 2026-10-03: the publisher's [Qwen2.5-0.5B-Instruct GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/tree/9217f5db79a29953eb74d5343926648285ec7e67), revision `9217f5db79a29953eb74d5343926648285ec7e67`, file `qwen2.5-0.5b-instruct-q4_k_m.gguf`. The publisher's immutable LFS identity is SHA-256 `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`, exactly 491,400,032 bytes. The publisher supplies Apache-2.0 licensing. This small general text model is independent of JevK5's decision model.

The selected engine is the existing privately packaged llama.cpp 0.5.0 build 11146, upstream commit `7fe450e19`, compiled with AppleClang 21.0.0.21000334 for Darwin arm64. Acceptance must bind every executable/library/plugin and its bundled notices, relocate that closure into a fresh directory, and use an explicit CPU profile. Tokenizer, embedded template and stop identities will be measured from the selected GGUF before adapter acceptance. The engine's current upstream documentation is research context; only this exact engine's observed behavior establishes compatibility.

The initial contract is bounded, nonstreaming `text-generate-v1`: one user message, a pinned template, integer output budget, an object result with text, input/output token counts and stop reason, and fixed error codes. Complete tokenization and generation share one bounded serialized owner. Caller cancellation must retain ownership until the outstanding operation settles or verified engine shutdown reaps it. No public recipe or capability is enabled by this spike.

A4 remains open until actual installed/native managed preparation, shared resource admission, restart/reconciliation, CPU memory bounds and JevK5 parity independently pass. Metal, other model families, arbitrary GGUF files, streaming, tools, embeddings, multimodal input and structured-output guarantees remain outside this fixture.

## Observed exact compatibility

The development harness passed on macOS 26.7.1 arm64. It copied and verified 21 private engine and license files (23,525,328 bytes), moved them into a fresh unrelated directory, then verified their identities again before launch. The engine closure's canonical SHA-256 is `a3899f1da4b66849a29997d8991ef3fdfeaeb503db599d8020d89358b8c70a37`; individual packaged hashes are in [engine-closure.json](../tests/fixtures/text_generation/engine-closure.json). This includes the existing native parent-watching supervisor. No engine executable or library comes from PATH. The original build and model still exist outside the test; filesystem/network denial and a private relocated Python adapter package are future installed acceptance work.

| Identity | Pinned value |
| --- | --- |
| Tokenizer metadata | `c828d4ced3ffa97554baf70310ae1b232a03bb63eff517952a9222567b44b5b8` |
| Vocabulary | 151,936 entries |
| Embedded chat template | `d5495a1e5db0611132a97e46a65dbb64a642a499421228b9c8b93229097fa9a4` |
| Engine-reported chat template | `4e9918361c284a93880606d182d64da6a9fe97cdc1f5c5a78c1c8840444246fc` |
| End-of-message token | `<\|im_end\|>`, ID 151645 |
| End-of-text token | `<\|endoftext\|>`, ID 151643 |
| Message-start token | `<\|im_start\|>`, ID 151644 |

The tokenizer hash covers every `tokenizer.*` GGUF metadata value using sorted, compact, ASCII-escaped JSON. The engine removes the embedded template's trailing newline, accounting for the two template hashes. The adapter implements only the single-user, default-system-message branch. Actual `/apply-template` and `/tokenize` outputs match that renderer for simple, multiline and Unicode fixtures. Parsed token sequences include the exact message delimiters; disabling special-token parsing produces a different sequence. Structural delimiters in user input are rejected.

The CPU profile explicitly selects `--device none`, zero GPU layers, no KV offload, one server slot, context 512, batch/microbatch 128 and two CPU threads. Context shifting and the web UI are disabled. The private loopback engine uses a random key file. Nine generation cases exercised output budgets 1, 8 and 64; input counts were 35 or 40 and generated counts ranged from 1 to 64. A 440-token prompt plus the maximum 64-token output reservation and eight-token guard passed. The next prompt token failed before completion dispatch. These initial checks were functional fixture measurements. The packaged workload measurement below adds native peak RSS evidence; no OS-enforced CPU/memory ceiling is claimed.

Forced real inference confirmed that each selected end token stops with EOS. A separate forced ordinary-text stop check confirmed that the stop text is excluded from returned content. The adapter passes the two fixed end-marker strings and keeps EOS enabled. No client-selected sampler, template, stop, grammar, tool or engine argument is accepted. The [exact engine revision's API documentation](https://github.com/ggml-org/llama.cpp/blob/7fe450e19305b828c199d602c23a8337aaa1f03b/tools/server/README.md) warns that an incomplete multibyte character can make `n_predict` overshoot slightly. The adapter verifies the actual returned token IDs/count and rejects any excess, retires that engine generation and returns no text. The guard is an admission margin; it is not a claim of an OS-enforced inference-work ceiling.

## Task and ownership contract

[The private adapter](../backend/mcpme_textgen/adapter.py) accepts exactly `{"text": string, "max_output_tokens": integer}`. Text is nonempty UTF-8, at most 8,192 bytes, without structural ChatML markers. Output budget is 1–64. The result is exactly `{"text": string, "input_tokens": integer, "output_tokens": integer, "stop_reason": "eos" | "stop" | "length"}`. Counts include generated end tokens reported by this engine. The result contains no chain-of-thought, tool-call or structured-output guarantee.

Errors contain only a fixed code: `invalid_request`, `input_limit`, `context_limit`, `queue_full`, `engine_unavailable`, `inference_failed`, `invalid_engine_response`, `output_limit` or `ownership_unsettled`. Transport bypasses proxies, rejects redirects and caps response bytes. Token lists reject booleans and out-of-vocabulary IDs. Completion must confirm full prompt evaluation, no truncation, a terminal stop and a matching output count.

One adapter belongs to one host-owned engine generation. Its default capacity is four total admitted callers, including the active operation. The lock covers tokenization, context admission, inference and result validation. Queued cancellation never dispatches. Active cancellation waits for the shielded thread and any retirement callback across repeated asyncio cancellation and AnyIO cancellation scopes. Actual-engine cancellation retained the first operation while another caller queued; peak concurrent completion calls remained one. A third caller at capacity two failed with `queue_full`.

A transport timeout does not prove inference has stopped. The adapter fences its engine generation and calls the host's async `retire` callback. That callback must return exactly `True` only after the host proves stop/reap. A real 1 ms transport timeout passed the harness's supervisor stop/reap case. Failure or an exception leaves `ownership_settled=False`; the host must retain its engine/resource reservation and reject replacement inference until ownership reconciliation succeeds. This spike supplies the seam and tests it; it does not implement host recovery or admission. After retirement a new engine generation requires a new adapter.

## JevK5 regression and reproduction

The actual JevK5 regression starts a separate engine after Qwen has stopped, using the unchanged pinned Q8 weights, context 8192, 99 GPU layers, batch 2048, microbatch 512, flash attention auto and no context shifting. It compares the unchanged `LocalScorer` with the independently instantiated vendored `JevK5GGUF` implementation using temperatures 1.22 and 0.93. Choice, boolean and score fixtures passed with maximum absolute probability difference **0**. Fresh/repeated/changed input cache behavior passed. No JevK5 source, profile or manifest changed.

Run from a locked development checkout:

```sh
uv run --frozen python scripts/verify_phase3_llm.py --engine-build /absolute/path/to/JevK5.app --weights /absolute/path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf --jevk5-weights /absolute/path/to/jevk5-4b-v0.3-Q8_0.gguf --report /absolute/path/to/ignored-report.json
uv run --frozen pytest -q tests/test_text_generation.py tests/test_scorer.py tests/test_runtime.py tests/test_mcpme_model_imports.py tests/test_mcpme_model_service.py --tb=no
```

The harness acquires nothing. Supply exactly the pinned existing artifacts; it verifies their bytes before execution and emits only fixed stage/check labels, hashes and counts. The reported run passed all 11 actual checks and 100 focused regression tests. The source selection was recorded before this acceptance run. Initial development runs exposed a private-temp-path normalization issue and the engine's EOS suppression when `ignore_eos=True`; both harness assumptions were corrected before the passing run. Explicit EOS and ordinary stop cases now test the actual behaviors separately.

## Remaining A4 integration gates

1. Add a host-owned exact engine recipe and task adapter package through the existing artifact/recipe contracts, including pinned private Python dependencies and all engine/model notices. Do not modify JevK5's component identity.
2. Bind the fixture, renderer, adapter implementation, engine closure, CPU profile and limits into immutable descriptor/recipe/package receipts. Admit only the selected model and tested profile.
3. Connect one shared adapter per engine generation to host resource reservations, lifecycle journals and verified retirement/reconciliation. Bound queue admission, engine memory and GPU policy through the common host owner.
4. Exercise native generated forms, managed build/probe/attachment, authorized install, real MCP invocation, app/host restart, revision/cancellation races and relocated installed closure with original inputs/build/global runtime unavailable.
5. Measure the maximum accepted workload's memory and deadlines, verify reservation enforcement and independently audit cancellation/disconnect/restart ownership. Repeat the JevK5 regression against the installed integrated artifact.

The initial harness sets `a4_installed_gate_passed` to `false`. It grants no enabled preparation recipe, installation authority or general GGUF/Metal support.

## Installed package and ownership checkpoint

The separate [exact package generator](../templates/text-generate-llamacpp-v1/README.md) now produces a sealed `model-service` package using the existing engine/adapter dependency topology. It carries private CPython, all 30 locked Python distributions and their license identities, the exact engine closure, Qwen license and weights, and native exec launchers. The development package contains 2,962 locked artifacts. No shared host schema, JevK5 implementation, recipe registry or native UI changed.

The MCP wrapper exposes `text_generate` and `text_status`. It preserves literal strings through SDK validation, rejects extra fields, authenticates every route and applies Host/Origin/body checks through the unchanged local policy. The engine bootstrap removes environment overrides and fixes the tested CPU profile. The adapter takes the host's inherited engine endpoint/key/instance identity and never launches another model or signals the engine PID.

On uncertain engine transport, the adapter fences admission and exits. Its retirement callback returns `False`; the adapter process cannot assert that its sibling engine has settled. The host's existing resident monitor owns graph teardown. The installed test stopped the live isolated engine with SIGSTOP, forced the real adapter's 10-second transport timeout, observed a tool error while the **exact engine instance remained in the ownership ledger and its reservation persisted**, then observed native engine reap and reservation release. The measured complete timeout-to-reap case took 15.560 seconds. Capacity became available to a second installed slot only after that cleanup.

[The installed harness](../scripts/verify_phase3_llm_installed.py) passed 11 checks against a clean private host from `5ed0e7673faa066ed8fc8dcaa67b54d38a0315e2`. It installed two copies through owner plan/apply calls in isolated state, deleted the temporary selected package, and enforced a host policy of 1,088 MiB (1,024 MiB declaration plus 64 MiB host headroom). While one engine was active, the second slot start failed with `resource_blocked`. Two HTTP MCP clients and one stdio client shared one engine instance and generated 64, 8 and 1 tokens. Exact tool definitions and route protections passed.

The same test then SIGSTOPed the current isolated engine, killed its owned host, and immediately started a replacement host against the same state. The replacement waited for the old native completion receipts, reconciled the old engine identity, and started a fresh generation on the stable public endpoint. A new real generation call passed. Final shutdown left both ownership ledgers empty and both reservations released. The harness retains temporary state if ownership cannot settle; it does not delete unresolved runtime state or adopt old PIDs.

This checkpoint exercises direct package installation. The original supplied package/build remain accessible outside the test's temporary directory; filesystem/network denial and managed native input-to-package acceptance are still open. The installed harness reports `a4_managed_native_gate_passed=false`.

## Packaged maximum-workload measurement

[The measurement harness](../scripts/measure_phase3_llm_package.py) runs the identical sealed package's engine and adapter under the existing native foreground-group owner. Native `wait4` captures each directly owned process's maximum RSS after joined shutdown. This separate harness does not change the installed host's process mode or resource policy.

Four simultaneous admitted MCP calls each used 440 input tokens and generated the full 64-token budget. The bounded queue completed in 5.820 seconds, below the package's 60-second operation deadline. Context overflow and the maximum byte-size tokenization path rejected excess context. The observed engine peak was 846,266,368 bytes; adapter peak was 72,237,056 bytes. Their individual-peak sum was **918,503,424 bytes**, below the declared 1,073,741,824 bytes. The sum is a conservative sum of separate process maxima for this workload, not a measured simultaneous aggregate, descendant-tree limit or OS-enforced ceiling. Additional accepted inputs and machine-load conditions remain subject to independent acceptance.

Reproduce the package checkpoint:

```sh
uv run --frozen python scripts/verify_phase3_llm_installed.py --host /absolute/private-host --package /absolute/sealed-package --report /absolute/ignored-installed-report.json
uv run --frozen python scripts/measure_phase3_llm_package.py --package /absolute/sealed-package --reaper /absolute/private-host/bin/mcpme-reap --report /absolute/ignored-memory-report.json
```

The new adapter/service tests plus unchanged scorer/runtime/model-import/model-service regressions passed **107 tests**. Final A4 still requires trusted assets and exact recipe registration through managed preparation, native forms/build/probe/attachment, matching captured source/recipe receipts, restricted relocated execution, and independent integrated acceptance. Installed lifecycle and workload measurements narrow those remaining gates; they do not enable generic LLM preparation.
