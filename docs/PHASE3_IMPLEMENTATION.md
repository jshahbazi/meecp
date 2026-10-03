# Phase 3 implementation and verification ledger

Objective: complete [automatic and agent-assisted app preparation](AUTOMATIC_PREPARATION.md), verify each implementation checkpoint, then run and remediate an independent adversarial review of the complete result. Phase 3 remains in progress. This ledger does not replace or reduce the plan's acceptance gates.

## Components and controlled preparation primitives

The common component registry resolves exact ID, version and implementation digest. Source-specific validation belongs to a host-owned component. The existing schema-1 planner uses that seam for Python source validation; its descriptor digest is unchanged. A separate closed schema-2 envelope and a synthetic native component exercise routing without Python dependency/module fields. Schema 2 is not yet a public planning or execution capability. See [component details](PHASE3_COMPONENTS.md).

Static inspection reads the selected immutable snapshot, verifies its original file identity and proposes bounded Python callable or declared MCP candidates. It does not import source, invoke matching hooks, resolve dependencies or claim a verified interface. Ambiguous roots, missing expected results, unresolved effects, missing locks and dynamic interface discovery stay visible.

The distribution builder includes a pinned private uv executable, its upstream licenses, a copied private CPython runtime, trusted generator templates and a closed artifact manifest. Signing refreshes packaged hashes before the app's outer signature and host receipt are sealed. The managed builder uses injected host-selected commands, hash-locked wheels, offline installation and retained distribution notices. It has no global executable or cache fallback.

Wheel acquisition uses the existing proxy-independent, public-address-pinned HTTPS transport with a separately bound exact host allowlist. Redirects and credentials are rejected. Each sink enforces expected bytes/hash, a disk ceiling, cancellation and elapsed deadline, and deletes failed attempt files through its held directory descriptor.

The preparation executor launches reviewed foreground steps in an opt-in native process group. The native wrapper holds its unreaped leader identity through same-group cleanup, including natural exit, cancellation, timeout and parent death. Repeated asyncio and AnyIO cancellation cannot abandon launch acknowledgement or cleanup. Observed wrapper loss interrupts blocked stream/protocol waits and retains the process reservation. Daemonizing or group-escaping executable workers remain unsupported; this facility does not establish arbitrary descendant-tree isolation.

The independent probe uses a real locked MCP SDK client, full tool definition comparison, coverage of declared tools by synthetic fixtures, strict JSON fixture comparison, post-shutdown package revalidation and native foreground peak RSS measurement. An observed peak above the descriptor's reservation fails. The measurement is the foreground target's peak, not aggregate simultaneous descendant memory or an OS-enforced memory ceiling. Filesystem/network denial and installed-host integration remain separate acceptance work.

## Previous checkpoint: durable controlled execution

An internally enabled coordinator now connects immutable inputs, the private builder, bounded wheel acquisition, SDK verification and same-revision attachment. An execution plan binds exact source/descriptor/recipe/policy and actual runner, verifier, compiled reaper, dependency closure and private-toolchain bytes. A single-use expiring intention creates a durable idempotent attempt. Execution has a whole-job deadline, private workspace disk monitoring and supervisor reservations alongside app memory and aggregate preparation disk demand. This checkpoint creates no scoped management execution grant or installation authority.

Owner-only API and CLI operations cover static inspection, plan/start, bounded progress, cancellation and receipt reconciliation. Production execution remains disabled. New routes cannot be called with old scoped management grants. Native ownership journals are persisted before launch; exact native completion or a proven failure to exec settles them. Unknown ownership keeps resources and the host lock. Recovery never adopts a PID or replays a step. It can reconcile completion arriving after a previous recovery attempt, and failed shutdown can be retried after ownership is resolved.

Threads that inspect, hash, copy or publish results remain owned until joined across repeated cancellation. Verification publication and attachment admission hold reservations through off-loop full-package validation. A short database transaction chooses the winner between valid attachment, cancellation, draft invalidation and recipe revocation. Full SDK reports must match the sealed package, declared tool/fixture counts, protocol and foreground memory declaration. Static recipe enablement cannot manufacture that report.

## Current checkpoint: generated setup and immutable artifacts

Owner-only generated questions now bind source identity, candidate, enabled recipe inventory, typed fields and defaults through an exact form digest. Opaque answers resolve into a host-validated descriptor; a short transaction records the selection and advances the draft revision. Saved candidates, recipes and answers survive restart, while later app renames and memory limits take precedence. Changed selections invalidate earlier plans. Unknown fields, stale forms, invalid primitive types and incomplete required recipe metadata fail explicitly. See [the form contract](PREPARATION_NATIVE_FORMS.md).

A cold host seeds a closed, pinned Python callable recipe for ordinary setup. Users do not author a recipe bundle to get supported questions. Host-supplied preferred references resolve that default among compatible third-party recipes; explicit and saved choices take precedence. Separate seed records preserve disable, revoke, remove, immutable version collisions and restart tombstones. Metadata enablement never enables execution. See [bundled defaults](BUILTIN_PREPARATION_RECIPES.md).

The native workspace renders generic typed questions, manages recipe lifecycle, adopts reviewed draft revisions atomically, starts only exact reviewed plans, and polls actual host stages/counters. Same-source jobs remain visible through attachment revisions. Source-filtered summaries prevent a stale-source job from masking current work. The review presents tools, access, memory and preparation budgets in human language. Closing a window leaves durable work owned by the host; cancellation explicitly targets its job.

Local binary intake now has a separate immutable capture store with FD-relative no-follow reads, original and copied byte verification, exact license references, streaming use, bounded disk/deadline/cancellation and atomic no-replace publication. A crash cannot leave a partial digest claim. Late cancellation rejects the caller's result while retaining complete committed bytes for other users of that object. Unknown staging ownership remains retained for reconciliation. This standalone store does not yet supply a durable capture coordinator, reference/retention accounting, a draft source variant or an ONNX builder. See [artifact capture](PREPARATION_ARTIFACT_CAPTURE.md).

Separate family-neutral input, derivation, builder-result and receipt contracts preserve schema-1 identities and distinguish original source from derived locks/interfaces. They grant no runtime capability. Real binary, CLI, LLM, service-only and agent integrations still require their own implementations and acceptance evidence. See [common bindings](PREPARATION_COMMON_INPUTS.md).

## Full acceptance scope

| Gate | Evidence required by the original plan | Current state |
| --- | --- | --- |
| A0 | Strict contracts and identities; legacy compatibility; no inspection execution; exact receipts; old grants cannot run code | Component/inspection and existing foundation regression checks available; complete integrated job/receipt audit remains open |
| A0R | Independently authored recipe added through CLI/native forms, prepared, attached and retained after restart with no host edits; lifecycle/race gates | Generic native recipe/questions and owner lifecycle routes tested; actual independent recipe preparation/attachment/restart gate remains open |
| A1 | Separate Python callable and foreground MCP native/API paths; private closure; full MCP probes; relocation; matching attachment; install/invoke/restart; cancellation/crash/revision races | Callable questions and native review/job controls tested; MCP dynamic interface resolution, dependency resolution and complete native/API installed gate remain open |
| A2 | Explicit ONNX artifact; exact CPU detector compatibility; real inference; tensor tests; complete notices; maximum image limits measured | Standalone immutable binary capture tested; durable capture/source/recipe/native/measurement gate remains open |
| A3 | Bounded supervised CLI; argv/input/output/exit contract; descendants, cancellation, crash/restart and closure | Process-group primitive tested; CLI integration and complete topology acceptance remain open |
| A4 | Pinned general model/engine/template; actual inference; token limits; shared serialized queue; resource/cancellation settlement; JevK5 parity | Open |
| A5 | Concrete programmatic agent backend; unfamiliar source end to end; scoped transfer/execution; bounded usage; parallel integration; cancellation/restart; stale-result rejection | Open |
| A6 | Each additional runtime/provider/family has its own compatibility and authority evidence before advertisement | Schema-2 fake component is a contract test; no additional runtime/provider capability advertised |

The first automatic capability remains disabled until A0, A0R, A1 and A2 pass from the installed/relocated artifact. CLI, generic LLM and integrated agents retain their independent gates. Public starts currently fail with `preparation_runner_unavailable`; static inspection, metadata and recovery remain available.

## Independent checkpoint findings and repairs

An independent reviewer reproduced six failures that existing focused tests missed. Each repair has a regression case:

| Finding | Repair and evidence |
| --- | --- |
| Second cancellation during spawn/stream setup abandoned a live child | Shield launch and reap settlement across repeated asyncio cancellation and AnyIO level cancellation; tests pause both launch boundaries |
| Python fixture equality accepted boolean/number conflation | Compare bounded canonical JSON; expected `true` cannot accept actual `1` |
| A memory declaration could pass despite a larger observed workload | Native `wait4` reports foreground peak RSS; verifier rejects peaks above the declared reservation; a 128 MiB allocation fails a 64 MiB declaration |
| Wrapper loss left a silent stream/protocol wait blocked until deadline | Race actions against the live ownership monitor; fail promptly with reservation retained |
| Acquisition inside a caller's exception handler deleted a successful wheel set | Use local completion state instead of ambient exception state |
| Buffered synchronous chunks bypassed the event-loop timeout | Check an explicit monotonic deadline around sink work and final publication |

Final adversarial review is still required after the full Phase 3 implementation and acceptance evidence exist.

The durable-job review additionally reproduced pre-execution recipe disable bypass, missing-wrapper ownership that could never settle, cached recovery/failed shutdown that missed later completion, cancellation during attachment and publication hashes, and initial/post-probe verifier hashing that outlived cancellation. Repairs add first-execution fencing, explicit proven-nonlaunch receipts, refreshable recovery/shutdown, and owned thread barriers. Each has a checked-in regression case; independent reproductions also exercise the publication responsiveness and cancellation winners. This review covers the checkpoint and does not replace the final complete Phase 3 review.

## Checkpoint verification

The complete Python regression suite passed 1,617 tests with one case-insensitive-filesystem skip. Swift verification passed 18 tests with three opt-in native installer lifecycle tests skipped. The independent reviewer reran the original six failure reproductions, confirmed each repair, and passed 42 process/verifier/acquisition regression checks. These results verify this checkpoint's primitives; they do not close the native/API A1 or ONNX A2 gates.

A composed production-primitive harness used five owned private-toolchain commands, 30 host-controlled pinned wheel acquisitions, offline installation and a real SDK functional fixture. It then removed the source, import snapshot and builder workspace and probed the relocated sealed package. The executor retained zero owned instances. The maximum observed build-step foreground peak was 93,814,784 bytes; the relocated callable probe peak was 68,829,184 bytes against a 128 MiB declaration. An independent reprobe of the separately built callable also passed full schema/fixture/mutation checks at 68,648,960 bytes. These are measured synthetic workloads, not application quality or maximum-input coverage.

The committed checkpoint at `0b47eea16ebbb59b4214e7fa3c1c3f8c27630a23` produced a clean CLI host with all 1,552 recorded artifact hashes matching. The durable `scripts/verify_phase3_primitives.py` harness passed against that host after its origin check was corrected to recognize only the exact pinned generator path and bundled namespace roots. It validated the recipe/source plan, used five owned commands and 30 wheels with a fresh private cache, removed source/state/builder inputs, verified the relocated package with the real SDK, retained zero owned steps and removed its temporary artifacts. Measured command/probe peaks were 89,702,400 and 69,074,944 bytes; peak private workspace size was 105,342,595 bytes. The harness is development verification; it does not exercise a public start route or install a slot.

The development DMG from the same clean commit passed image checksum, mounted/copied app signature, read-only mount, minimal-environment private runtime and eject checks. Its SHA-256 was `610be6e3df156d844a3c27482a3789bf55615585fccd07757fb3331a185d8f3d`; signing was ad hoc and notarization was not requested. The hotdog example passed 45 tests, all three native self-tests passed, and the credential scan passed. An additional independent signing review passed 69 focused tests and separate ad hoc/Developer ID fixtures that changed nested tool bytes and verified manifest/host receipt refresh before the outer seal. These fixtures verify ordering, not a notarized release or live Developer ID signing.

## Durable checkpoint verification

The durable checkpoint's final Python regression run passed 1,690 tests with six skips (one case-insensitive-filesystem case and five opt-in private-host cases). Those five private-host cases were exercised separately in the 50-test runner/executor/verifier run. Swift passed 18 tests with three opt-in installer lifecycle skips; all three native contract self-tests and the credential scan passed. The independent reviewer passed 12 adversarial contract/race reproductions. A legacy gateway test now waits for owner cleanup and capacity release after child exit; the baseline failed and the corrected test passed under the same independently injected cleanup delay. Gateway runtime code is unchanged.

Clean commit `5629ac2eea695f60d725c31bf30cc33bfbaa2ed5` produced a development CLI host with all 1,554 artifact hashes matching. `scripts/verify_phase3_durable_runner.py` imported that host's actual implementation, SDK and native helper and used its private toolchain with global tools excluded from PATH. The default run passed in 35.928 seconds: callable and tools-only existing MCP preparation each used five commands, 30 wheels, 8,621,296 acquired dependency bytes and one full-schema SDK fixture before atomic same-revision attachment. Probe foreground peaks were 68,698,112 and 69,173,248 bytes against 128 MiB declarations. Peak observed temporary workspace size was 245,732,962 bytes. Real probe cancellation and native completion reconciliation without replay also passed; all owned work, pending receipts and reservations settled. Source/state/builder inputs were removed, two packages passed relocation validation, and temporary artifacts were deleted. This directly enables the private coordinator for development verification; it does not exercise native onboarding or install/invoke a slot.

An initial harness run returned an unclassified failure. Two fresh runs of the same frozen host passed, including the default cleanup path. The cause remains unknown; the harness now emits only bounded stage/case/exception-type diagnostics for unexpected failures, without messages, tracebacks or source payloads. This observation remains separate from the passing regression and host checks and from the still-open activation gates.

## Generated setup and artifact checkpoint verification

Frozen source commit `891646bb689f17c4b2cb5d06b7cad0a0772aad88` passed 1,873 Python tests with six skips (one case-insensitive-filesystem case and five opt-in private-host tests). Swift ran 21 tests: 18 passed and three opt-in installer lifecycle tests skipped. All four native contract/UI self-tests and the credential scan passed. An independent review reran the form/seed/selection/publication defects and found one additional late verification handoff gap. Four explicit budget checks and five new cancellation/deadline tests repair it; the reviewer independently reproduced both original failures as correct rejection and passed all 103 artifact tests.

The same clean source commit produced a development CLI host with all 1,576 recorded artifact hashes matching. Its actual backend and bundled dependencies passed owner HTTP intake, automatic default-recipe selection, generated questions and revision-2 form review without creating a job or slot. A native self-test consumed that built-host form/review response through the real draft-adoption callback. This verifies packaged contract integration; the UI self-test uses a fixture request callback and does not prove a live native-to-host preparation session.

The private durable runner harness passed against that frozen host in 36.891 seconds. Callable and foreground MCP cases each used five owned commands, 30 pinned wheels, 8,621,296 dependency bytes and a full-schema SDK fixture before same-revision attachment. Measured probe foreground peaks were 68,681,728 and 69,156,864 bytes; peak workspace size was 245,741,155 bytes. Actual cancellation, exact native receipt reconciliation without replay, source/state/builder removal and two relocated packages passed. Zero owned steps, pending receipts or retained reservations remained. This harness explicitly supplies descriptors and enables the private coordinator; it does not close native discovery, installation or public activation gates.

## Next acceptance work

Implement owned dependency and MCP interface resolution so ordinary directories can produce complete plans without hand-authored descriptors or locks. Verify callable and foreground MCP independently through actual native/API preparation, installation, invocation and restart from a relocated host with an empty private cache. The direct existing-MCP harness currently supplies its interface; generated forms correctly report dynamic discovery as unresolved.

Connect binary capture to a durable journal, shared reservations, exact recovery proofs and reference/retention accounting, then add the additive captured source variant and real ONNX recipe/worker. Binary materialization must stream verified captured FDs and survive removal of original paths. Preserve legacy GGUF and folder identities. Continue CLI, general LLM/service connectors and integrated-agent work under the original independent acceptance gates. Full Phase 3 and its final adversarial review remain required.
