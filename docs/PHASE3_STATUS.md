# Phase 3 goals and subgoals

Snapshot: October 3, 2026, checked against integration commit `bc48c05` on
`codex/phase3-llm-integration` and the current separate provider workstreams.
Common confinement, CLI recipe 1.0.1, locked/unlocked generated adapters and
the reviewed agent request repair are integrated. The locked request-only
pipeline passed 147 focused checks and its actual private build/probe/attachment
case; unlocked integration passed 168 focused checks and its actual
dependency/build/probe/attachment/relocation case. Published component and recipe
bytes are preserved. All provider calls in this evidence are simulated.

Provider configuration backend checkpoint `a3e62bbb` is implemented and pushed
in a separate branch, awaiting independent review and integration. Secure native
provider configuration is still being implemented. Native integration uncovered
an API envelope mismatch: the shared native request handler requires
`api_version: 1`, while the separate agent route handler returns unversioned
envelopes. Repair and actual socket verification remain open. Compatible agent
recipe recommendations also remain open; the UI must not default to an unrelated
recipe. No A0–A5 goal has completed final release acceptance.

**Phase 3 is in progress. Public automatic preparation remains disabled.**
A passing checkpoint establishes its stated scope on its recorded source or
artifact. Final acceptance requires the combined installed artifact and the
independent whole-phase adversarial review.

Status terms: **Verified checkpoint** has passing recorded evidence;
**In review** has implementation awaiting independent clearance;
**In progress** is being implemented or integrated; **Open** has required
acceptance work remaining; **In repair** has an independently reproduced issue
being fixed; **Separate gate** covers additional capability work.

## Goal summary

| Goal | Status | Main subgoals | Remaining acceptance |
| --- | --- | --- | --- |
| A0: contracts and authority | In progress | Immutable source/package identities, scoped execution, durable jobs, cancellation, recovery, privacy and confinement have checkpoint evidence. | Final combined compatibility, race, ownership, resource and privacy checks. |
| A0R: recipe extensibility | In progress | Versioned recipes, generated forms, independent recipe addition and lifecycle management have checkpoint evidence. | Final catalog/native extension and lifecycle checks; executable worker extensions retain a separate gate. |
| A1: automatic Python/MCP | In progress | Callable and existing-MCP preparation, dependencies, SDK probes, attachment and installed calls have checkpoint evidence. | Separate callable and existing-MCP cases on the final native, installed and relocated artifact. |
| A2: automatic ONNX | In progress | Exact supported detector, real inference, native intake, tensor controls and limits have checkpoint evidence. | Repeat inference, maximum workloads, negative eligibility and lifecycle under current confinement on the final artifact. |
| A3: native CLI | In progress | Command contracts, immutable builder, measured verification, resource cleanup and ownership repairs have checkpoint evidence. | Final native directory-to-installed flow, topology/recovery/resource checks and individual capability enablement. |
| A4: local text generation | In progress | Pinned CPU runtime, actual generation, shared queue, cancellation, native preparation and parity have checkpoint evidence. | Mint the next immutable recipe after source freeze; repeat confined inference, workloads and signed installed acceptance. |
| A5: integrated agent | In progress | Scoped request review, parallel proposals, locked/unlocked adapter derivation, dependency resolution and actual local probes have checkpoint evidence. | Provider backend review, native setup, wire repair, compatible recipe choices, a live provider/model run and final native installed flow. |
| A6: broader modular coverage | Separate gate | Component/recipe extension seams have checkpoint evidence. | Each additional provider, provider-backed app, runtime, model family or execution capability needs its own acceptance before advertisement. |

## Acceptance goals

| Goal | Subgoal | Status | Work remaining |
| --- | --- | --- | --- |
| A0: contracts and authority | Strict descriptors, component versions, canonical digests and unknown-field rejection | Verified checkpoint | Recheck the final integrated implementation. |
| A0 | Preserve legacy source, import/export and package identities | Verified checkpoint | Final compatibility regression run. |
| A0 | Inspect ordinary directories without executing selected source | Verified checkpoint | Cold-directory repair d649a07 passed 66 inspection/forms/adapter/default-CLI checks and the separate unlocked actual pipeline advanced through intake successfully. Final packaged controls remain. |
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
| A3 | Copy builder, notices, source closure and command adapter | Verified checkpoint | Immutable command recipe 1.0.1 is minted and integrated; published 1.0.0 bundle/helper bytes and lifecycle opt-outs are preserved. Final artifact acceptance remains. |
| A3 | Timeout/cancel, descendants, process-group cleanup and durable recovery | Verified checkpoint | Final combined installed topology/crash tests. |
| A3 | Resource monitoring and threshold cleanup | Verified checkpoint | Independent 183-check review cleared repair 753bc17; the reproduced launch race creates zero native processes and settles ownership. Integrated with unchanged common guards. Hard aggregate and lifetime guarantees remain false; final installed acceptance remains. |
| A3 | Positive measured verification and matching attachment | Verified checkpoint | Actual measured SDK preparation, attachment, relocation and installed HTTP/stdio source checks passed. Trusted admission and immutable recipe 1.0.1 are integrated; combined checkpoint 0635560 cleared 159 independent checks. Final bundled/native acceptance remains. |
| A3 | Resource scope and fork-capable ownership acceptance | Verified checkpoint | Original same-user scope is documented. Independent overload, cancellation and actual SIGKILL recovery controls passed; sampled footprint remains an observed window measurement. Final combined installed ownership acceptance remains. |
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
| A5: integrated agent | Concrete provider transport and parallel task coordinator | Verified checkpoint | Repair 9de90fc independently passed 147 focused checks, release Swift/native self-test and an actual request-only confined pipeline; integrated through 5d87e93. Reviewed binary identities transfer no content. Live provider and final installed acceptance remain. |
| A5 | Integrate parallel subtask outputs into one reviewed proposal | Verified checkpoint | Interface/dependencies run in parallel; fixtures consume the agreed interface and adapter consumes all proposals. Both locked and unlocked encoded-request-only pipelines reached actual build, SDK probe and attachment. Live compatibility and final native acceptance remain. |
| A5 | Review exact source transfer, provider/model, recipe and usage limits | Verified checkpoint | Repaired checkpoint 06f253f6 cleared independent review with all 15 hashes matched; integrated as 86ff1a5. Final artifact acceptance remains. |
| A5 | Durable jobs, cancellation, restart and stale-result rejection | Verified checkpoint | All four reproduced source, cancellation, pending-cap and worker-handoff defects passed independent re-review. Final combined lifecycle checks remain. |
| A5 | Host-validated candidate adoption through ordinary preparation | Verified checkpoint | Locked and unlocked adoption preserve source/resolver proof continuity; independent repeated-selection, mutation and joined-shutdown controls passed. Final combined artifact controls remain. |
| A5 | Native agent controls and owner-only shared routes | Verified checkpoint | Independent owner-route and actual parent-handler checks passed. New Draft/navigation controls preserve the owned draft. These checks do not close the newly identified API envelope mismatch or final socket/native acceptance. |
| A5 | Unfamiliar supported Python source through proposal, build, SDK probe and attachment | Verified checkpoint | Independent actual private build, SDK probe, attachment and relocation passed with four simulated provider calls and zero live calls. Live-provider and generated-adapter acceptance remain open. |
| A5 | Owner-only provider configuration and private credentials | In review | Separate pushed backend checkpoint a3e62bbb has 320 author-reported passing checks. Independent review, integration and final packaged CRUD/rotation/restart acceptance remain; configuration itself performs no provider calls. |
| A5 | Secure native provider setup and explicit model selection | In progress | Refresh/Add/Edit/Remove, transient secure key entry, generation checks and reconnect handling are implemented in an unfrozen native worktree. Final checks, independent review and actual owner-socket integration remain. |
| A5 | Native/agent API response compatibility | In repair | Add the required versioned response envelope for agent routes and verify success/error paths through the production native request handler. Existing fake-transport UI checks do not establish this. |
| A5 | Compatible agent recipe choices for ordinary directories | In progress | Add bounded host metadata recommendations and native explicit choice/default behavior. Cold unlocked directories must select a compatible enabled recipe; ambiguous or unsupported sources need an unresolved choice. No checkpoint evidence yet. |
| A5 | Live backend, selected credentials and model compatibility | Open | Complete a configured provider/model run after host/native setup and integration. Current evidence uses simulated calls only; no live dispatch has occurred. |
| A5 | Governed adapter derivation and licensed derived artifacts | Verified checkpoint | Locked/unlocked selection and source/resolver mutation controls are independently cleared and integrated. Frozen component/bundle bytes remain unchanged. Final signed artifact and combined race acceptance remain. |
| A5 | Locked unfamiliar Python directory to generated adapter and app | Verified checkpoint | Independent encoded-request-only proposals reached actual private build, confined SDK, matching attachment, source/state removal and confined relocation. Four simulated calls and zero live calls; live provider and final native/installed acceptance remain. |
| A5 | Unlocked unfamiliar directories needing a generated adapter | Verified checkpoint | Independent review cleared f10e13a; integrated through b7c1e21. The actual combined request-only pipeline passed in 26.306 seconds with ordinary dependency resolution, build, confined SDK, revision-2 attachment and relocation after source/state removal. It retained an unknown binary asset and inferred an unpinned dependency only from reviewed source. Native/installed/live acceptance remains. |
| A5 | Final packaged/native generated-adapter flow | Open | Request and locked/unlocked source integration are complete. Finish provider setup, compatible recipe choices, native-to-resolution handoff and actual final packaged flow. |
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
| Revise/cancel/expiry versus start or attach | Open | Derived selection race is repaired and independently cleared; complete all families on the final combined artifact. |
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
| Publish verified work to the working branch | In progress | Reviewed common/A4/A5 wiring, CLI repair, locked/unlocked adapters, request repair and cold inspection are integrated on codex/phase3-llm-integration. Provider host/native configuration remains in separate active lanes awaiting review and integration. |

## Current critical path

1. Complete normal-user provider configuration, independently review and
   integrate host and native setup. Repair the native API envelope, add compatible
   recipe choices, finish the native agent-to-resolution handoff and run selected
   live-provider acceptance after explicit provider/model selection.
2. Freeze the complete source composition. CLI recipe 1.0.1 is minted;
   mint the coherent next text-generation version after backend freeze and run
   the full combined regression suite.
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
implementation-lane reports. Current request and unlocked evidence is recorded in
[request checkpoint review](AGENT_REQUEST_INDEPENDENT_REVIEW.md) and
[unlocked integration review](AGENT_UNLOCKED_INTEGRATION_REVIEW.md). Provider
backend and native workstream reports describe separate, unintegrated changes.
