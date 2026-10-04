# Phase 3 goals and subgoals

Snapshot: October 3, 2026. Integration branch:
`codex/phase3-llm-integration`, source checkpoint `2dc8508`, with the
prior status update at `2090d73`.
Common probe and restricted text-generation guards have cleared independent
checkpoint review and are integrated. The repaired agent host checkpoint is
integrated; shared native registration has cleared independent checkpoint review.
The separately published CLI repair `753bc17` awaits independent clearance and
integration. Locked generated adapters are published at `33fe6e0` with an
author-verified actual pipeline; independent review reproduced a derived-input
selection race that must be repaired. Unlocked generated adapters are being
implemented in a separate worktree. Final artifact acceptance
is open. This table tracks implementation and acceptance separately; no entire
A0–A5 release gate is closed.

**Phase 3 is in progress. Public automatic preparation remains disabled.**
A passing checkpoint establishes its stated scope on its recorded source or
artifact. Final acceptance requires the combined installed artifact and the
independent whole-phase adversarial review.

Status terms: **Verified checkpoint** has passing recorded evidence;
**In review** has implementation awaiting independent clearance;
**In progress** is being implemented or integrated; **Open** has required
acceptance work remaining; **In repair** has an independently reproduced issue
being fixed; **Separate gate** covers additional capability work.

## Acceptance goals

| Goal | Subgoal | Status | Work remaining |
| --- | --- | --- | --- |
| A0: contracts and authority | Strict descriptors, component versions, canonical digests and unknown-field rejection | Verified checkpoint | Recheck the final integrated implementation. |
| A0 | Preserve legacy source, import/export and package identities | Verified checkpoint | Final compatibility regression run. |
| A0 | Inspect ordinary directories without executing selected source | Verified checkpoint | Final packaged static-inspection controls. |
| A0 | Capture text, binaries, models and licenses with exact immutable identities | Verified checkpoint | Recheck final artifact intake and retention. |
| A0 | Durable jobs, exact scopes, idempotency, deadlines and declared resource reservations | Verified checkpoint | Complete combined lifecycle and admission matrix. |
| A0 | Cancellation, crash recovery, owned threads and native completion receipts | Verified checkpoint | Current common launch-boundary repairs are independently reviewed and integrated; final combined recovery matrix remains. |
| A0 | Reject old scoped grants on execution routes | Verified checkpoint | Applied agent routes also reject old preparation grants, wrong Host/Origin and unauthenticated calls. Final installed recheck remains. |
| A0 | Prevent stale or canceled results from attaching | Verified checkpoint | Recheck all families and agent adoption on the combined host. |
| A0 | Keep source, prompts, responses, credentials and raw diagnostics out of journals | Verified checkpoint | Final integrated privacy and credential scan. |
| A0 | Managed private Python/ONNX probe confinement | Verified checkpoint | Independent 66-case source/native guard run passed; integrated as c0051b6. Actual A2 inference still needs a new run. |
| A0 | Current job authority and complete package identities at native creation | Verified checkpoint | Independent native-task and graph-hash controls reject cancel/revoke/revision and file/directory changes before creation. Recheck the final combined artifact. |
| A0R: recipe extensibility | Immutable recipe bundles, component composition and version collisions | Verified checkpoint | Final catalog regression after the new text-generation version. |
| A0R | Scaffold, check, add, inspect and manage recipes | Verified checkpoint | Final packaged CLI/native recheck. |
| A0R | Add an independent recipe without host dispatcher, import or UI edits | Verified checkpoint | Recheck the independent recipe against the final host. |
| A0R | Generated fields, explicit recipe selection and compatible defaults | Verified checkpoint | Final mixed-family/native form tests. |
| A0R | Enable/disable/revoke/remove/deprecate, restart retention and lifecycle races | Verified checkpoint | Complete combined race matrix; nine installed/native lifecycle cases have earlier evidence. |
| A0R | Additional executable preparation workers | Separate gate | Capability and descendant-ownership acceptance before enabling this extension kind. |
| A1: automatic Python/MCP | Ordinary callable directory to resolved interface and private dependency closure | Verified checkpoint | Recheck current common confinement on the final host. |
| A1 | Declared foreground MCP server, including dynamic discovery | Verified checkpoint | Separate final installed/native case remains required. |
| A1 | Separate acceptance evidence for existing MCP and callable applications | Open | Keep two independently passing native/API cases on the final artifact; a callable result cannot close existing-MCP acceptance. |
| A1 | Mixed directory containing Python, images and other payloads | Verified checkpoint | Final combined-host recheck after common probe integration. |
| A1 | Native questions, build, real SDK probe and matching-revision attachment | Verified checkpoint | Recheck the newest integrated source. |
| A1 | Installation, HTTP/stdio calls, original source removal and restart | Verified checkpoint | Final signed and relocated artifact acceptance. |
| A1 | Restricted probes and cancellation/revision/crash/resource behavior | In progress | Common guard checkpoint independently cleared and integrated; full combined installed acceptance remains. |
| A2: automatic ONNX | Exact supported weights, license, runtime and CPU eligibility | Verified checkpoint | Recheck final artifact identities; different exports remain unsupported. |
| A2 | Captured model and folder plus model through native preparation | Verified checkpoint | Recheck the newest combined host. |
| A2 | Actual inference, complete tool schemas and controlled tensor cases | Verified checkpoint | Earlier host passed actual inference. Repeat through the newly integrated common confinement path. |
| A2 | Reject incompatible weights, tensor layouts and class definitions | Verified checkpoint | Repeat explicit negative eligibility controls on the final artifact. |
| A2 | Maximum image bytes/pixels, deadlines, concurrency and measured memory | Verified checkpoint | Recheck advertised 1 MiB / 1,048,576-pixel limits under the new restricted probe. |
| A2 | Attachment, install, HTTP/stdio calls, source removal and relocation | Verified checkpoint | Final signed artifact and separate confined relocation/max-workload probes. |
| A2 | Full lifecycle and confinement integration | In progress | Common guards integrated; relocation and maximum-workload harnesses now use them. Actual inference and combined race acceptance remain. |
| A3: native CLI | Static executable eligibility and bounded JSON/argv/interface questions | Verified checkpoint | Final packaged/native questions recheck. |
| A3 | Bounded input/output, exit mapping and declared effects | Verified checkpoint | Final actual-command contract and oversized/error-output cases. |
| A3 | Default family and recipe registration from exact helper inventory | Verified checkpoint | 102 independent checks passed; final artifact recheck. |
| A3 | Copy builder, notices, source closure and command adapter | Verified checkpoint | Measured-path integration needs a new immutable command recipe; preserve published 1.0.0 bytes. |
| A3 | Timeout/cancel, descendants, process-group cleanup and durable recovery | Verified checkpoint | Final combined installed topology/crash tests. |
| A3 | Resource monitoring and threshold cleanup | In review | Producer repair 753bc17 adds the final synchronous check before native creation and retains the original metadata baseline. Independently recheck the reproduced launch race and measured cleanup before integration. Hard aggregate and lifetime guarantees remain false. |
| A3 | Positive measured verification and matching attachment | In review | Author's actual measured SDK workload reaches attachment. Independently clear repair 753bc17, integrate trusted admission and mint command recipe 1.0.1. |
| A3 | Resource scope and fork-capable ownership acceptance | In review | Original same-user scope is documented. Review measured overload behavior and enforceable descendant cleanup; do not label sampled footprint as a hard quota or lifetime peak. |
| A3 | Native directory to attached, installed and relocated app | Open | Author's isolated measured API path includes installed HTTP/stdio and relocation evidence. Final integrated native-directory and signed-app acceptance remain. |
| A3 | Public CLI capability enablement | Open | Remains disabled until the independently reviewed measured path and final installed/native gates pass. |
| A4: local text generation | Pinned CPU model, engine, tokenizer, template and runtime closure | Verified checkpoint | New immutable recipe after all backend changes freeze. |
| A4 | Actual generation, token/output limits, one engine and bounded shared queue | Verified checkpoint | Recheck the combined artifact with restricted probes. |
| A4 | Native captured model and folder plus model preparation | Verified checkpoint | Both cases passed on the clean 68a7533 host; recheck the next coherent host. |
| A4 | Matching attachment, installed HTTP/stdio calls, restart and relocation | Verified checkpoint | Final signed artifact. |
| A4 | Official SDK cancellation and reservation settlement | Verified checkpoint | Latest native cases retain ownership until inference settles; final regression. |
| A4 | Measured engine/adapter memory, capacity denial and JevK5 parity | Verified checkpoint | Final combined workload and parity run. |
| A4 | Restricted engine/adapter probes and current host-owned bindings | Verified checkpoint | Independent 111-case policy/verifier run passed; integrated as 55781f0. Earlier 68a7533 inference passed; current confined inference awaits the next recipe/host. Wildcard-listener containment is unproved. |
| A4 | Immutable recipe upgrade with preserved lifecycle choices | In progress | Mint the coherent next version; retain all published 1.0.5 and earlier bytes. |
| A5: integrated agent | Concrete provider transport and parallel task coordinator | Verified checkpoint | OpenAI Responses transport has simulated tests; live provider/model acceptance remains open. |
| A5 | Integrate parallel subtask outputs into one reviewed proposal | Verified checkpoint | Coordinator checkpoint has simulated composition evidence; repeat through generated adapter preparation and the selected live backend. |
| A5 | Review exact source transfer, provider/model, recipe and usage limits | Verified checkpoint | Repaired checkpoint 06f253f6 cleared independent review with all 15 hashes matched; integrated as 86ff1a5. Final artifact acceptance remains. |
| A5 | Durable jobs, cancellation, restart and stale-result rejection | Verified checkpoint | All four reproduced source, cancellation, pending-cap and worker-handoff defects passed independent re-review. Final combined lifecycle checks remain. |
| A5 | Host-validated candidate adoption through ordinary preparation | Verified checkpoint | Original-source adoption retains its validated metadata through the short commit and denies actual mutation. Generated-adapter selection has a separately reproduced derived-input race, recorded below. |
| A5 | Native agent controls and owner-only shared routes | Verified checkpoint | Independent 50-case owner-route/backend recheck and actual parent handlers passed on 2dc8508. New Draft disables before transfer; button/direct navigation preserve the draft. Root release build and all three UI self-tests passed. Final installed/native flow remains. |
| A5 | Unfamiliar supported Python source through proposal, build, SDK probe and attachment | Verified checkpoint | Independent actual private build, SDK probe, attachment and relocation passed with four simulated provider calls and zero live calls. Live-provider and generated-adapter acceptance remain open. |
| A5 | Live backend, selected credentials and model compatibility | Open | Requires a selected configured provider; no credential discovery or live dispatch has occurred. |
| A5 | Governed adapter derivation and licensed derived artifacts | In repair | Locked checkpoint 33fe6e0 binds attempts, source, recipe epoch and proposal roles; stores licensed artifacts separately; includes cancellation, restart and retention. Independent review confirmed that changing the derived wrapper after full validation can still commit selection through both adoption paths. Repair the shared metadata check, then independently re-review and integrate. |
| A5 | Locked unfamiliar Python directory to generated adapter and app | In repair | Author's actual source-only pipeline passed: untyped source, four simulated provider calls, licensed adapter, private build, confined SDK, matching attachment and source-free confined relocation. Root matched the frozen checkpoint and passed 23 focused checks, then reproduced the selection race with two additional controls. Repair and final artifact acceptance remain. |
| A5 | Unlocked unfamiliar directories needing a generated adapter | In progress | Separate branch codex/phase3-agent-unlocked has 21 passing captured/static proposal and requirement checks; its new durable store is not yet tested or frozen. Explicit resolution, build/probe/adoption integration, actual dependency-bearing acceptance and independent review remain. Analysis staging executes no source or dependency resolution. |
| A5 | Final packaged/native generated-adapter flow | Open | Complete after the selection repair, locked/unlocked derivation review and integration. |
| A5 | Integrated installed/native path and final adversarial acceptance | Open | Complete after source integration and concrete live acceptance. |
| A6: broader modular coverage | Host component/recipe extension seams | Verified checkpoint | Each new capability needs its own implementation and evidence. |
| A6 | Additional preparation providers: DeepSeek, GLM, Bedrock or local service | Separate gate | Provider-specific transport, credential, usage and compatibility acceptance. |
| A6 | An app whose runtime calls another LLM service | Separate gate | Runtime secret/network/service contract and adapter acceptance; preparation-provider support does not supply this. |
| A6 | Additional runtimes, model families, Metal/GPU, resident vision or GUI automation | Separate gate | Independent compatibility, authority and lifecycle gates per capability. |

A6 records the modular expansion requested during the work. These additional
capabilities do not close or expand the first Python/MCP/ONNX release gate.
An app that calls an LLM service needs its own runtime implementation even when
that provider can already assist with preparation.

## Required cross-family verification subgoals

These are concrete acceptance requirements from the original plan. Existing
focused controls provide partial checkpoint evidence; every row remains open
until the final combined families and artifact have the applicable evidence.

| Subgoal | Status | Work remaining |
| --- | --- | --- |
| Source/artifact copy races and source mutation | Open | Repeat applicable family controls on the final source and artifact. |
| Dependency resolution cancellation, changes and failures | Open | Combined locked-resolution and new agent dependency-derivation controls. |
| Seal, probe, attachment and success-publication races | Open | Verify cancellation or revision wins without an admissible late result. |
| Revise/cancel/expiry versus start or attach | Open | Complete all families and derived-candidate adoption; repair the reproduced derived-input selection race first. |
| Authority expiry while queued or awaiting resource admission | Open | Final queued/admission controls with owned cleanup and retained charges. |
| Cancellation versus spawn acknowledgement | Open | Common boundary controls pass; repeat with each integrated native family. |
| Provider disconnect versus local tool completion | Open | Complete selected-provider and derived-preparation ownership controls. |
| Duplicate starts and idempotent attempts | Open | Combined native/API attempts and durable restart behavior. |
| Disk exhaustion and reservation settlement | Open | Build, acquisition, derivation, capture and publication failure controls. |
| Symlink and ancestor replacement | Open | Existing source/launch controls pass; complete final intake/build/adoption matrix. |
| Missing dependency/model notices | Open | Final sealed-package rejection and complete relocated notices. |
| Tampered packages, bindings, receipts and measurement proofs | Open | Existing controls pass; include new measured CLI and adapter identities. |
| Raw log, credential, prompt, response and source leakage | Open | Final integrated journal, diagnostics and credential scan. |
| Unsupported capabilities and component versions | Open | Final closed inventory and fail-before-execution controls. |
| Crash after sealing or attachment publication | Open | Final durable recovery and no-replay controls. |
| Restart while an old worker is still running | Open | Verify retained ownership, no PID adoption and no premature reservation release. |
| Terminal cancellation and unresolved ownership | Open | Join known work; retain reservations and require reconciliation when ownership is uncertain. |
| Real SDK probes from an unrelated directory | Open | Use actual bundled runtime and nonsecret fixtures for every supported family. |
| Original source/build/global runtime/network unavailable during sealed probes | Open | Repeat actual confined flows; record exact platform limitations. |
| Deny sealed-package writes where policy permits | Open | Final private-runtime and engine controls plus post-shutdown full identity validation. |
| Functional calls and expected outputs | Open | Actual supported-family fixtures; installation retains its own separate checks. |
| Resource/deadline measurements and package mutation controls | Open | Final measured workloads and before/after artifact identities. Preparation attestations cannot broaden installation claims. |
| Separate declared budgets from observed measurements | Open | Preserve sampled/foreground/aggregate distinctions; never infer hard or lifetime enforcement. |
| Separate functional behavior from task/model quality | Open | Keep hotdog negative-model/positive-tensor evidence explicit; recognition quality needs its own fixtures and thresholds. |

## Shared delivery subgoals

| Subgoal | Status | Work remaining |
| --- | --- | --- |
| Private CPython/uv, locked wheels, offline install and full dependency notices | Verified checkpoint | Final combined artifact/toolchain closure check. |
| Scoped artifact acquisition, verified TLS, disk limits and joined cancellation | Verified checkpoint | Final packaged acquisition/cleanup regressions. |
| Generated native forms, review, stage polling and explicit cancellation | Verified checkpoint | Agent parent control repair independently cleared; final native flows remain. |
| Exact signed application and relocated payload hashes | Open | Build and validate the final combined signed candidate. Existing development hosts do not close this gate. |
| Full combined Python and Swift regressions | Open | Last complete coherent source run: 2,540 Python passes / 12 skips and 18 Swift passes / 3 opt-in skips. New integrations need another full run. |
| First public automatic Python/MCP/ONNX capability | Open | A0, A0R and A1–A2 must pass on the final installed/relocated artifact before enabling it. |
| Independent CLI, local LLM and agent capability enablement | Open | Enable individually after A3–A5 acceptance. |
| Final independent whole-phase adversarial review and repairs | Open | Review the complete integrated result; checkpoint reviews have already produced repairs. |
| Publish verified work to the working branch | In progress | Common/A4 guards and reviewed A5 registration are integrated. CLI repair 753bc17 is separately published pending independent clearance. Locked adapter 33fe6e0 is separately published with author pipeline evidence and a reproduced selection defect; unlocked adapters are active on a separate branch. |

## Current critical path

1. Independently recheck the published positive CLI launch-boundary repair;
   integrate cleared checkpoints without weakening the common launch guards.
2. Repair generated-adapter selection, complete governed adapter and dependency
   derivation, independently review and integrate them. Mint coherent immutable command/text-generation
   recipes and run the full combined regression suite.
3. Build one coherent artifact and rerun Python/MCP, ONNX and local LLM native,
   installed, restricted-probe and relocation acceptance.
4. Close final CLI resource/native acceptance and the selected live agent
   backend path. Live provider acceptance still needs configured provider/model
   selection.
5. Validate the signed candidate, complete independent whole-phase adversarial
   review, repair findings and enable each capability whose gates pass.

Sources: [original acceptance plan](AUTOMATIC_PREPARATION.md),
[implementation ledger](PHASE3_IMPLEMENTATION.md),
[trust and resource scope](PHASE3_TRUST_AND_RESOURCE_SCOPE.md),
[native Python/MCP acceptance](NATIVE_INSTALLED_ONBOARDING_ACCEPTANCE.md),
[native ONNX acceptance](NATIVE_INSTALLED_ONNX_ACCEPTANCE.md),
[native local LLM acceptance](NATIVE_INSTALLED_TEXTGEN_ACCEPTANCE.md),
[default CLI registration](COMMAND_DEFAULT_REGISTRATION.md), and current
implementation-lane reports. The initial provider/coordinator checkpoint and
current agent integration retain separate simulated-versus-live evidence.
