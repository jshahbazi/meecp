# Phase 3 implementation and verification ledger

Objective: complete [automatic and agent-assisted app preparation](AUTOMATIC_PREPARATION.md), verify each implementation checkpoint, then run and remediate an independent adversarial review of the complete result. Phase 3 remains in progress. This ledger does not replace or reduce the plan's acceptance gates.

## Current checkpoint: components and controlled preparation primitives

The common component registry resolves exact ID, version and implementation digest. Source-specific validation belongs to a host-owned component. The existing schema-1 planner uses that seam for Python source validation; its descriptor digest is unchanged. A separate closed schema-2 envelope and a synthetic native component exercise routing without Python dependency/module fields. Schema 2 is not yet a public planning or execution capability. See [component details](PHASE3_COMPONENTS.md).

Static inspection reads the selected immutable snapshot, verifies its original file identity and proposes bounded Python callable or declared MCP candidates. It does not import source, invoke matching hooks, resolve dependencies or claim a verified interface. Ambiguous roots, missing expected results, unresolved effects, missing locks and dynamic interface discovery stay visible.

The distribution builder includes a pinned private uv executable, its upstream licenses, a copied private CPython runtime, trusted generator templates and a closed artifact manifest. Signing refreshes packaged hashes before the app's outer signature and host receipt are sealed. The managed builder uses injected host-selected commands, hash-locked wheels, offline installation and retained distribution notices. It has no global executable or cache fallback.

Wheel acquisition uses the existing proxy-independent, public-address-pinned HTTPS transport with a separately bound exact host allowlist. Redirects and credentials are rejected. Each sink enforces expected bytes/hash, a disk ceiling, cancellation and elapsed deadline, and deletes failed attempt files through its held directory descriptor. The coordinator still needs aggregate reservations and durable job integration.

The preparation executor launches reviewed foreground steps in an opt-in native process group. The native wrapper holds its unreaped leader identity through same-group cleanup, including natural exit, cancellation, timeout and parent death. Repeated asyncio and AnyIO cancellation cannot abandon launch acknowledgement or cleanup. Observed wrapper loss interrupts blocked stream/protocol waits and retains the process reservation. Daemonizing or group-escaping executable workers remain unsupported; this facility does not establish arbitrary descendant-tree isolation.

The independent probe uses a real locked MCP SDK client, full tool definition comparison, coverage of declared tools by synthetic fixtures, strict JSON fixture comparison, post-shutdown package revalidation and native foreground peak RSS measurement. An observed peak above the descriptor's reservation fails. The measurement is the foreground target's peak, not aggregate simultaneous descendant memory or an OS-enforced memory ceiling. Filesystem/network denial, whole-job resource admission and installed-host integration remain separate acceptance work.

## Full acceptance scope

| Gate | Evidence required by the original plan | Current state |
| --- | --- | --- |
| A0 | Strict contracts and identities; legacy compatibility; no inspection execution; exact receipts; old grants cannot run code | Component/inspection and existing foundation regression checks available; complete integrated job/receipt audit remains open |
| A0R | Independently authored recipe added through CLI/native forms, prepared, attached and retained after restart with no host edits; lifecycle/race gates | Static extension foundation exists; actual preparation/native/restart gate remains open |
| A1 | Separate Python callable and foreground MCP native/API paths; private closure; full MCP probes; relocation; matching attachment; install/invoke/restart; cancellation/crash/revision races | Private builder and owned SDK primitives implemented; durable coordinator, generated forms and end-to-end gate remain open |
| A2 | Explicit ONNX artifact; exact CPU detector compatibility; real inference; tensor tests; complete notices; maximum image limits measured | Existing external hotdog baseline only; automatic binary intake/recipe/native/measurement gate remains open |
| A3 | Bounded supervised CLI; argv/input/output/exit contract; descendants, cancellation, crash/restart and closure | Process-group primitive tested; CLI integration and complete topology acceptance remain open |
| A4 | Pinned general model/engine/template; actual inference; token limits; shared serialized queue; resource/cancellation settlement; JevK5 parity | Open |
| A5 | Concrete programmatic agent backend; unfamiliar source end to end; scoped transfer/execution; bounded usage; parallel integration; cancellation/restart; stale-result rejection | Open |
| A6 | Each additional runtime/provider/family has its own compatibility and authority evidence before advertisement | Schema-2 fake component is a contract test; no additional runtime/provider capability advertised |

The first automatic capability remains disabled until A0, A0R, A1 and A2 pass from the installed/relocated artifact. CLI, generic LLM and integrated agents retain their independent gates. Current public preparation routes remain metadata-only.

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

## Checkpoint verification

The complete Python regression suite passed 1,617 tests with one case-insensitive-filesystem skip. Swift verification passed 18 tests with three opt-in native installer lifecycle tests skipped. The independent reviewer reran the original six failure reproductions, confirmed each repair, and passed 42 process/verifier/acquisition regression checks. These results verify this checkpoint's primitives; they do not close the native/API A1 or ONNX A2 gates.

A composed production-primitive harness used five owned private-toolchain commands, 30 host-controlled pinned wheel acquisitions, offline installation and a real SDK functional fixture. It then removed the source, import snapshot and builder workspace and probed the relocated sealed package. The executor retained zero owned instances. The maximum observed build-step foreground peak was 93,814,784 bytes; the relocated callable probe peak was 68,829,184 bytes against a 128 MiB declaration. An independent reprobe of the separately built callable also passed full schema/fixture/mutation checks at 68,648,960 bytes. These are measured synthetic workloads, not application quality or maximum-input coverage.

## Next checkpoint

Connect the private builder, acquisition and independent verifier to durable job execution, job-bound authority, reservations, receipts and atomic attachment. Add plan/start/source-inspection API and CLI operations, then native generated selection/forms and real stage polling. Verify Python callable and existing MCP independently from a relocated host with an empty preparation cache before promoting any capability. Continue ONNX artifacts/recipes and remaining gates in parallel as their dependencies become available.
