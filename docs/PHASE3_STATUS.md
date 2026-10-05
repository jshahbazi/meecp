# Phase 3 goals and subgoals

Snapshot: 2026-10-04. **Supported Phase 3 source/runtime acceptance is complete;
verified working-branch publication is complete.** The [final review](PHASE3_FINAL_REVIEW.md)
has no concrete blocking finding, and final app/source/pinned-asset preservation
passes. The ledger has 101 in-scope Complete rows, including D09, and three
Separate gate rows. It does not mark all 104 rows
Complete or admit additional providers/runtimes.

Runtime/source anchor:
24bae0f8fd0aa91ebaaba5ee2b0d7d4020cc9498, 811 tracked files, signed v10.
The later documentation recording commit is a different revision; it records
this exact runtime anchor without rebuilding or rewriting artifact receipts.
Producer13 full Python source is 3ddaaddc5a69293bcfaad97656be0335a5131339.
TextGen engine/task/recipe 1.0.13 and policy SHA-256
8a9a8fd69217352b49e0e338d4d652df8cebcac86609edae1190a88cd2d9a592
remain frozen.

## Accepted evidence

| Checkpoint | Result | Scope |
| --- | --- | --- |
| Full Python v6 | 4,223 passed, zero failures, one filesystem skip; all 27 private opt-ins pass | Exact source 3ddaaddc/signed v9. Simulated private providers remain simulated. Not relabeled as a full 24bae/v10 rerun. |
| Current Swift v6 | 21 passed, zero failures/skips | Source 24bae; synthetic installer fixtures are separate from actual signed workflow evidence. |
| Scanner | 15 unit/eight actual new13 controls and full history/source pass; first delivery scan passes in 3.392 seconds | Exact public-value AND path exception; 813 tracked files/four staged documents at runtime source 24bae before first publication. |
| V9 ordinary Python/MCP/ONNX | Five cases pass, 464.734s | Callable/dynamic MCP/mixed content/two CPU ONNX paths; actual native/probe/attachment/installed/source-free restart/relocation, with qualified transfer. |
| V9 first service | Passes, 90.940s; zero real remote calls | Owned numeric-loopback fixture, no-folder intake, secret remap/revoke/drain and installed relocation. |
| V9 native TLS | Eight cases pass; 22 evaluations/five owned errors/five releases/zero outstanding | Actual native positive/concurrent and trust/hostname/expiry/fetch-denial controls; zero AIA fetches or Keychain changes. |
| V10 signed artifact | Complete root/peer checks pass | 7,722 app/7,578 host members/811 source Git blobs; exact Developer ID/Team/timestamp/signatures and four actual direct entitlement reads. |
| V10 management | Completed/revoked pass, 109.076s; six SDK sessions exit | Exact owner grants/idempotency/restart/revoke; known children/source/original app preserved. No management relocated-app preservation claim. |
| V10 selected live agent | Passes, 107.209s; four real calls/zero retries | Sole selected OpenAI Responses gpt-6.1-sol; native pending handoff→ordinary resolution/preparation→probe/attach/install/source-free/restart/relocation. |
| V10 CLI | Passes, 155.352s; cross-build upgrade true | Current native/installed helper path; original and older upgrade apps/source preserved, known children joined. |
| V10 TextGen | Create/supplement pass, 449.402s | Actual pinned Qwen CPU inference; source/model/original app preserved and known children joined. |
| V10 recipe lifecycle | All ten pass, 250.090s | Public defaults plus explicitly private barriers; source clean/preserved, known children joined, zero external calls/timeouts/forced stop. |
| Final preservation | Accepted after all five current v10 flows | All 7,722 app path/size/mode/SHA records exact, 811 clean source files/five pinned assets unchanged and strict signature valid. Zero provider calls. |
| Final whole-phase review | Supported source/runtime scope clear | Actual repairs and independent checkpoint/root/peer boundaries are recorded; no universal failure/security/quality guarantee is inferred. |

The [final acceptance index](PHASE3_FINAL_ACCEPTANCE.md) binds every current
receipt and failed attempt by SHA-256. The final preservation receipt is
f24145dcf9720f7cb4d63cacdf753174ad7b9b79f356664929f043bb011c7b0a.

## Goal summary

| Goal | Accepted scope | Status |
| --- | --- | --- |
| A0: contracts and authority | Common identity/intake/authority/privacy/ownership/confinement contracts. | Complete in supported scope |
| A0R: recipe extensibility | Declarative authoring, independent recipe, selection and lifecycle. | Complete; executable workers separately gated |
| A1: automatic Python/MCP | Callable, foreground dynamic MCP and mixed directory to installed app. | Complete in supported scope |
| A2: automatic ONNX | Both model capture paths, exact CPU inference/workloads and installed app. | Complete; recognition quality unmeasured |
| A3: native CLI | Public native preparation/SDK/installed/source-free/cross-build upgrade. | Complete within measured same-user scope |
| A4: local text generation | Pinned CPU generation, queue/cancel/capacity and native/installed flows. | Complete in supported scope |
| A5: integrated agent | Private locked/unlocked controls and sole selected real-provider public workflow. | Complete for exact accepted tuple |
| A6: broader modular coverage | Component seams and first numeric-loopback service connector. | Initial scope complete; other providers/runtimes separately gated |
| Delivery | Runtime/evidence/review and first verified branch publication complete. | D09 Complete |

Exactly 104 original IDs/labels are retained: 71 acceptance, 24 cross-family and
nine delivery. **Complete** means accepted within its recorded scope and limits.
**Separate gate** means additional capability remains future. D09 is Complete
from the verified first publication described below; it does not claim a rebuilt
artifact, remote CI or notarized/public distribution.

## Acceptance goals

| ID | Goal | Subgoal | Status | Evidence and accepted scope |
| --- | --- | --- | --- | --- |
| A0.01 | A0: contracts and authority | Strict descriptors, component versions, canonical digests and unknown-field rejection | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.02 | A0 | Preserve legacy source, import/export and package identities | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.03 | A0 | Inspect ordinary directories without executing selected source | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.04 | A0 | Capture text, binaries, models and licenses with exact immutable identities | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.05 | A0 | Durable jobs, exact scopes, idempotency, deadlines and declared resource reservations | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.06 | A0 | Cancellation, crash recovery, owned threads and native completion receipts | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.07 | A0 | Reject old scoped grants on execution routes | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.08 | A0 | Prevent stale or canceled results from attaching | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.09 | A0 | Keep source, prompts, responses, credentials and raw diagnostics out of journals | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.10 | A0 | Managed private Python/ONNX probe confinement | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0.11 | A0 | Current job authority and complete package identities at native creation | Complete | Recorded common contracts and producer13 full source regression pass. Current signed authority/management and final review preserve the original scope. |
| A0R.01 | A0R: recipe extensibility | Immutable recipe bundles, component composition and version collisions | Complete | Declarative authoring/independent-recipe checkpoints and all ten current v10 lifecycle cases pass. Private barriers remain private; executable workers retain their separate gate. |
| A0R.02 | A0R | Scaffold, check, add, inspect and manage recipes | Complete | Declarative authoring/independent-recipe checkpoints and all ten current v10 lifecycle cases pass. Private barriers remain private; executable workers retain their separate gate. |
| A0R.03 | A0R | Add an independent recipe without host dispatcher, import or UI edits | Complete | Declarative authoring/independent-recipe checkpoints and all ten current v10 lifecycle cases pass. Private barriers remain private; executable workers retain their separate gate. |
| A0R.04 | A0R | Generated fields, explicit recipe selection and compatible defaults | Complete | Declarative authoring/independent-recipe checkpoints and all ten current v10 lifecycle cases pass. Private barriers remain private; executable workers retain their separate gate. |
| A0R.05 | A0R | Enable/disable/revoke/remove/deprecate, restart retention and lifecycle races | Complete | Declarative authoring/independent-recipe checkpoints and all ten current v10 lifecycle cases pass. Private barriers remain private; executable workers retain their separate gate. |
| A0R.06 | A0R | Additional executable preparation workers | Separate gate | Additional executable preparation workers require separate capability, confinement and descendant-ownership acceptance before enablement. |
| A1.01 | A1: automatic Python/MCP | Ordinary callable directory to resolved interface and private dependency closure | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A1.02 | A1 | Declared foreground MCP server, including dynamic discovery | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A1.03 | A1 | Separate acceptance evidence for existing MCP and callable applications | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A1.04 | A1 | Mixed directory containing Python, images and other payloads | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A1.05 | A1 | Native questions, build, real SDK probe and matching-revision attachment | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A1.06 | A1 | Installation, HTTP/stdio calls, original source removal and restart | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A1.07 | A1 | Restricted probes and cancellation/revision/crash/resource behavior | Complete | All three callable/dynamic MCP/mixed-content signed v9 cases pass, with qualified unchanged-code transfer and current v10 shared-helper evidence. |
| A2.01 | A2: automatic ONNX | Exact supported weights, license, runtime and CPU eligibility | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A2.02 | A2 | Captured model and folder plus model through native preparation | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A2.03 | A2 | Actual inference, complete tool schemas and controlled tensor cases | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A2.04 | A2 | Reject incompatible weights, tensor layouts and class definitions | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A2.05 | A2 | Maximum image bytes/pixels, deadlines, concurrency and measured memory | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A2.06 | A2 | Attachment, install, HTTP/stdio calls, source removal and relocation | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A2.07 | A2 | Full lifecycle and confinement integration | Complete | Both signed v9 CPU ONNX paths pass actual inference/contracts/workloads/installed source-free relocation, with qualified unchanged-code transfer. Recognition quality is unmeasured. |
| A3.01 | A3: native CLI | Static executable eligibility and bounded JSON/argv/interface questions | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.02 | A3 | Bounded input/output, exit mapping and declared effects | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.03 | A3 | Default family and recipe registration from exact helper inventory | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.04 | A3 | Copy builder, notices, source closure and command adapter | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.05 | A3 | Timeout/cancel, descendants, process-group cleanup and durable recovery | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.06 | A3 | Resource monitoring and threshold cleanup | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.07 | A3 | Positive measured verification and matching attachment | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.08 | A3 | Resource scope and fork-capable ownership acceptance | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.09 | A3 | Native directory to attached, installed and relocated app | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A3.10 | A3 | Public CLI capability enablement | Complete | Current signed v10 public command and cross-build upgrade pass. Source/private ownership controls retain their recorded scope; unknown-owner reconciliation and measured-resource limits remain explicit. |
| A4.01 | A4: local text generation | Pinned CPU model, engine, tokenizer, template and runtime closure | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.02 | A4 | Actual generation, token/output limits, one engine and bounded shared queue | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.03 | A4 | Native captured model and folder plus model preparation | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.04 | A4 | Matching attachment, installed HTTP/stdio calls, restart and relocation | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.05 | A4 | Official SDK cancellation and reservation settlement | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.06 | A4 | Measured engine/adapter memory, capacity denial and JevK5 parity | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.07 | A4 | Restricted engine/adapter probes and current host-owned bindings | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A4.08 | A4 | Immutable recipe upgrade with preserved lifecycle choices | Complete | Current signed v10 create/supplement paths pass pinned Qwen CPU generation, native preparation/probes/attachment/installed source-free operation. Model quality and hard aggregate/lifetime bounds are unclaimed. |
| A5.01 | A5: integrated agent | Concrete provider transport and parallel task coordinator | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.02 | A5 | Integrate parallel subtask outputs into one reviewed proposal | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.03 | A5 | Review exact source transfer, provider/model, recipe and usage limits | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.04 | A5 | Durable jobs, cancellation, restart and stale-result rejection | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.05 | A5 | Host-validated candidate adoption through ordinary preparation | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.06 | A5 | Native agent controls and owner-only shared routes | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.07 | A5 | Unfamiliar supported Python source through proposal, build, SDK probe and attachment | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.08 | A5 | Owner-only provider configuration and private credentials | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.09 | A5 | Secure native provider setup and explicit model selection | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.10 | A5 | Native/agent API response compatibility | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.11 | A5 | Compatible agent recipe choices for ordinary directories | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.12 | A5 | Live backend, selected credentials and model compatibility | Complete | Current signed v10 passes four real OpenAI Responses gpt-6.1-sol calls in 107.209 seconds, zero retries and full native/installed/source-free acceptance. Earlier failed v9 cause/count remain unknown; eight calls are verified across successful batches plus that unknown failed batch. |
| A5.13 | A5 | Governed adapter derivation and licensed derived artifacts | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.14 | A5 | Locked unfamiliar Python directory to generated adapter and app | Complete | Recorded private locked pipeline/build/SDK/attachment/installed/source-free controls pass with simulated providers. Their unchanged-code scope transfers; this row does not claim a separate public locked-provider live replay. |
| A5.15 | A5 | Unlocked unfamiliar directories needing a generated adapter | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.16 | A5 | Native agent-to-ordinary-preparation resolution handoff | Complete | Current signed v10 pending-proposal handoff and downstream ordinary resolution/preparation/install pass; diagnostic metadata and production callback controls grant no authority. Earlier v9 cause remains unexplained. |
| A5.17 | A5 | Final packaged/native generated-adapter flow | Complete | Current selected gpt-6.1-sol v10 native/installed flow and unchanged-source/private controls pass. Only the exact admitted provider/model/component tuple is accepted. |
| A5.18 | A5 | Integrated installed/native path and final adversarial acceptance | Complete | Supported source/runtime and final whole-phase review are clear at 24bae/signed10; final preservation passes. Publication is separately tracked by D09. |
| A6.01 | A6: broader modular coverage | Host component/recipe extension seams | Complete | Reviewed extension seams and the first signed v9 numeric-loopback service fixture pass with qualified unchanged-code transfer. Additional providers/runtimes retain separate gates. |
| A6.02 | A6 | Additional preparation providers: DeepSeek, GLM, Bedrock or local service | Separate gate | Additional preparation providers, including DeepSeek, GLM, Bedrock and local services, need their own transport, credential, usage and compatibility acceptance. |
| A6.03 | A6 | An app whose runtime calls another LLM service | Complete | First no-folder numeric-loopback OpenAI-compatible bearer connector passes actual signed v9 fixture/SDK/secret-remap/revoke/drain/source-free acceptance. Zero real remote-provider/model calls; broader compatibility is unclaimed. |
| A6.04 | A6 | Additional runtimes, model families, Metal/GPU, resident vision or GUI automation | Separate gate | Additional runtimes, model families, Metal/GPU, resident vision and GUI automation need independent compatibility, authority and lifecycle acceptance. |

## Required cross-family verification subgoals

| ID | Requirement | Status | Evidence and accepted scope |
| --- | --- | --- | --- |
| X01 | Source/artifact copy races and source mutation | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X02 | Dependency resolution cancellation, changes and failures | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X03 | Seal, probe, attachment and success-publication races | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X04 | Revise/cancel/expiry versus start or attach | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X05 | Authority expiry while queued or awaiting resource admission | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X06 | Cancellation versus spawn acknowledgement | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X07 | Provider disconnect versus local tool completion | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X08 | Duplicate starts and idempotent attempts | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X09 | Disk exhaustion and reservation settlement | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X10 | Symlink and ancestor replacement | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X11 | Missing dependency/model notices | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X12 | Tampered packages, bindings, receipts and measurement proofs | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X13 | Raw log, credential, prompt, response and source leakage | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X14 | Unsupported capabilities and component versions | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X15 | Crash after sealing or attachment publication | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X16 | Restart while an old worker is still running | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X17 | Terminal cancellation and unresolved ownership | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X18 | Real SDK probes from an unrelated directory | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X19 | Original source/build/global runtime/network unavailable during sealed probes | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X20 | Deny sealed-package writes where policy permits | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X21 | Functional calls and expected outputs | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X22 | Resource/deadline measurements and package mutation controls | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X23 | Separate declared budgets from observed measurements | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |
| X24 | Separate functional behavior from task/model quality | Complete | Recorded cross-family source/fixture controls and applicable artifact/runtime evidence are accepted in the [final review](PHASE3_FINAL_REVIEW.md). The scope and limits below remain explicit. |

## Shared delivery subgoals

| ID | Requirement | Status | Evidence and accepted scope |
| --- | --- | --- | --- |
| D01 | Private CPython/uv, locked wheels, offline install and full dependency notices | Complete | Locked private toolchain/notice source and signed v9 27 private opt-ins pass with qualified unchanged-input transfer. Current v10 full artifact/toolchain and five fresh workflow results are independently bound. |
| D02 | Scoped artifact acquisition, verified TLS, disk limits and joined cancellation | Complete | Pure ownership controls and eight actual signed v9 native TLS cases pass: 22 evaluations, five owned error references/five releases, zero outstanding/AIA fetches. Source acquisition/cancel/disk controls pass; transfer is limited to unchanged scope. |
| D03 | Generated native forms, review, stage polling and explicit cancellation | Complete | Production native acceptance entries and current/stale/recovered form callbacks pass. Physical native clicks remain unverified. |
| D04 | Exact signed application and relocated payload hashes | Complete | Final v10 app/host/source/signature/direct entitlement checks and after-all-flows preservation pass: 7,722 app members, 811 source files/five pinned assets. Management relocated-app preservation is unclaimed; notarization/distribution are unclaimed. |
| D05 | Full combined Python and Swift regressions | Complete | Full Python 4,223/one filesystem skip/all 27 opt-ins bind source 3ddaaddc/signed v9; no full v10 rerun is claimed. Swift 21/no failures/skips binds 24bae. Six-path focused/scanner/native controls and independent input proof support qualified source transfer. |
| D06 | First public automatic Python/MCP/ONNX capability | Complete | All five actual signed v9 ordinary Python/MCP/ONNX cases pass with final accepted unchanged-code/shared-helper transfer. |
| D07 | Independent CLI, local LLM and agent capability enablement | Complete | Current signed v10 public CLI, local TextGen and sole selected agent independently pass capability/runtime acceptance; exact admission bounds are unchanged. |
| D08 | Final independent whole-phase adversarial review and repairs | Complete | Final supported-scope whole-phase review has no blocking finding. Reviewer-authored areas retain independent checkpoint/root/peer clearance; no formal all-lines security scan or universal failure exclusion is claimed. |
| D09 | Publish verified work to the working branch | Complete | Source and delivery commit 77e2828e077403291c605544efac73aa6041b573 was pushed to origin/codex/phase3-public-acceptance and exact remote SHA verified 2026-10-05 01:16:15 UTC. This later recording update does not claim its own future commit was built/tested or that remote CI ran. |


## Verified working-branch publication

The first source/delivery commit 77e2828e077403291c605544efac73aa6041b573
was pushed to codex/phase3-public-acceptance at
[the selected origin repository](https://github.com/jshahbazi/mcpme).
The exact remote SHA was verified 2026-10-05 01:16:15 UTC, closing D09.
[Publication proof](/private/tmp/mcpme-phase3-first-publication-1791162975548487000.json)
SHA-256 64ffc91a77d2ab9c403ffb1e163a05154160fbdb66ed9920edc266a7088766f2.
The [first delivery scan](/private/tmp/mcpme-phase3-final-secret-scan-metadata.json)
passes 813 tracked files/four staged documents at 24bae in 3.392 seconds,
SHA-256 8ffab87bf5b9f9ac4b052bc4c4e8595291c0163b6f0824969a2f16d58bf5b1bb.

This update is a later documentation recording step. Runtime/app evidence remains
at frozen 24bae/signed v10; neither the published documentation commit nor this
future recording commit is described as the built/tested runtime. Remote CI
status is unclaimed. The final review retains its original review-time snapshot.

## Evidence-transfer limits

Full Python/27 private host opt-ins bind source 3ddaaddc/signed v9. The approved
six-path delta to 24bae has its own focused/scanner/native checks. All 805 other
Git blobs, host Python bytes, 162 effective requirements and nonartifact toolchain
metadata match. Among 33 changed host Mach-O members, 32 loadable bodies match
with CMS excluded. The newly compiled host/bin/mcpme-handoff body differs even
with UUID masked; exact helper source/compile inputs are unchanged.

The final review accepts source-level transfer only within independently
verified unchanged-code/input scope. Fresh current v10 management/live/CLI/
TextGen/lifecycle independently exercise the current helper. No full v10
4,223/27 rerun, whole-host compiled-byte identity, signature-only rebuild or
transfer of old held plans/admission instances/artifact-specific job identity
is claimed. The failed investigative transfer packet remains valid negative
evidence, not a product finding.

## Continuing boundaries

A0R.06, A6.02 and A6.04 remain future: executable preparation workers;
additional preparation providers including DeepSeek, GLM, Bedrock and local
services; broader runtimes/model families, Metal/GPU, resident vision and GUI
automation. The first service fixture and sole accepted OpenAI tuple do not
close those capabilities.

The earlier signed v9 live batch failed at agent_handoff; cause and remote-call
count remain unknown. Current success does not identify a behavioral fix.
Eight real calls are verified across accepted historical/current batches plus
that failed batch of unknown count; eight is not the exact total. No further
provider replay is required.

Production native handlers are exercised; physical clicks remain unverified.
Functional SDK/tensor/generation/generated-adapter checks do not measure
recognition or general model quality. Remote/unowned work after cancellation
or revocation is unknown. Known owned work joins; deliberate unknown-owner
faults retain state/charges for reconciliation. Management relocated-app
preservation is unclaimed. Final preservation establishes current original
artifact integrity after all flows, without broadening those scopes.

The documented same-user/trusted-engine scope does not prove arbitrary hostile
worker or wildcard-listener containment. Foreground sampled RSS, separate role
peaks and reservations do not establish hard aggregate/full-lifetime enforcement.
CLI flags for those guarantees remain false. The sole casefold filesystem skip
is preserved; separate collision controls retain their narrower coverage.

Developer ID signing is verified. Notarization, public distribution and updating
the user's installed /Applications/mcpme.app are unclaimed. Publication means
pushing the verified working branch; D09 records its first exact remote verification.
Later metadata recording retains the runtime anchor and requires no receipt rewrite.

The final reviewer authored/supervised some repairs; independent checkpoint,
root and peer controls supply those boundaries. No new formal all-lines security
scan or proof excluding every conceivable failure is claimed. The original
private review packet is immutable; its Markdown copy may receive punctuation
and a documentation-recording note without claiming byte identity.

Sources: [final review](PHASE3_FINAL_REVIEW.md),
[final evidence](PHASE3_FINAL_ACCEPTANCE.md),
[release authority](PHASE3_RELEASE_POLICY.md),
[trust/resource scope](PHASE3_TRUST_AND_RESOURCE_SCOPE.md),
[original plan](AUTOMATIC_PREPARATION.md) and
[implementation ledger](PHASE3_IMPLEMENTATION.md).
