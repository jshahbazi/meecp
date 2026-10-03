# Phase 2 completion audit

This audit follows the Phase 2 delivery and acceptance requirements in
[MCPME_DESIGN.md](MCPME_DESIGN.md#phase-2-agent-installation-and-recovery).
It records implementation and verification separately. Phase 2 is still in
progress until the final committed artifacts pass the remaining checks below.

## Requirements and evidence

| Requirement | Implementation | Verification status |
| --- | --- | --- |
| Versioned package manifests, complete immutable locks, safe paths and artifact closure | Static package validator and private sealed snapshots; stdio, model and native HTTP profiles | Existing regression suite; final full suite pending |
| Raw Python intake and external agent preparation without hand-written manifests | Persistent selected-source drafts, setup export, versioned generator, revision-bound attachment | Bundled Python acceptance verifier previously passed; final artifact rerun pending |
| Raw JevK5 weights and engine intake and preparation | Pinned GGUF and engine snapshots, engine selection after draft restart, export-driven generator, exact preparation receipt | Real 4.48 GB model acceptance passed 18 checks; final artifact rerun pending |
| Resume drafts, reject stale or modified package returns, cancel and expire inactive drafts | Durable revisions and selected snapshots; explicit settings-only attachment retention revalidates sealed artifacts | Static and intake tests; final native and bundled reruns pending |
| Typed configuration and secret references | Primitive schema, owner-only secret metadata, private descriptor delivery, reviewed generation upgrades for installed changes | Previous configuration milestone passed real bundled-client checks; final upgrade integration pending |
| Scoped management MCP and explicit plan/apply authority | Existing bounded grants and typed management bridge; upgrade and cancellation/recovery mutations remain owner-only | Existing management acceptance; new owner/grant upgrade tests pass |
| Native setup and installation progress | Download, typed configuration, secret selector, raw model and engine selection, upgrade review and recovery controls | Swift tests and inert native previews passed; final committed native build pending |
| Pinned downloads, partial failure, cancel and retry | Size and SHA-256 pins, bounded safe ZIP closure, private durable jobs and cancellation writer barriers | Merged download milestone verified; final bundled rerun pending |
| Duplicate applies create one installation or upgrade intent | Durable plan/import idempotency and atomic candidate registration/selection | Installation and upgrade concurrency tests pass; final real-client rerun pending |
| Every install/upgrade interruption stage recovers safely | Durable selection and exposure markers, retained generation data, completion receipts from identity-holding native reapers | Unit fault matrices and real supervisor-death preflight pass; final artifact rerun pending |
| Pre-exposure rollback preserves old data and endpoint | Quiesce old writers, snapshot into a private candidate data directory, verify before selection, restore old pointers only before exposure | Upgrade stage tests and real persistent-service preflight pass; final artifact rerun pending |
| Post-exposure writes survive routing failure | Registry exposure permission is committed before ordinary admission; failure retains selected data and offers candidate recovery | All 12 bundled upgrade preflight checks pass, including accepted SDK writes, Stop/Recover and supervisor death; final artifact rerun pending |
| Tampered artifacts, unsafe paths, port conflict, unavailable secrets, MCP incompatibility, exhausted disk and failed probes cannot activate a broken generation | Validation gates, owned readiness and protocol probes, private settings binding, reservations, bounded data cloning | Focused regressions and fault tests; final complete suite pending |
| Removal retains data unless deletion is explicitly requested | Durable stop/reap, credential revocation, unregister with retained packages and all generation data | Existing removal and restart tests; final bundled regression pending |
| Authoring documentation and versioned preparation templates | Python function, existing MCP and JevK5/llama.cpp templates ship with the relocatable host | Static/template checks and raw model preparation passed; final authoring review pending |
| Relocation, independence and content-free diagnostics | Bundled runtimes/templates, sealed selected-source identity, suppressed process streams and raw exception output | Existing acceptance plus real model relocation passed; final artifact and privacy checks pending |

## Recovery boundary

The exposure marker records permission to admit ordinary traffic. It is written
before the gateway opens. A crash after that marker must retain and recover the
selected generation, even if the host cannot prove whether a client write reached
it. Completed stopped installations also commit this permission for a later
explicit Start. Cancellation is available before this boundary; Stop and recovery
operate on the selected generation afterward.

An upgrade never probes against the old writable data directory. The host reaps
old writers before cloning it, verifies the candidate against its clone, and
switches durable pointers while retaining the stable slot UUID, port and invocation
credential. A pre-exposure rollback advances the runtime generation to invalidate
stale sessions while restoring the original package, settings and data pointers.

The native reaper writes a private, instance- and nonce-bound completion receipt
only after waiting for its held child, or after proving that no child was forked.
The next supervisor can reconcile that receipt without discovering or signaling
a PID. A missing or invalid receipt retains `ownership_lost` and blocks replacement.

## Explicitly later work

[PROJECT_ONBOARDING.md](PROJECT_ONBOARDING.md#optional-integrated-agent) places
integrated provider preparation in a later release. Reference-document onboarding
also requires a document-service template before it can be advertised. Both remain
visible unsupported capabilities; accepting Markdown/text setup instructions does
not claim a reference library or an integrated coding agent.

Phase 3 covers a second model engine and measured resource contention. Aggregate
MCP routing, remote workers, containers, a marketplace, automatic updates, shared
engines across slots, Intel and remote exposure remain explicitly deferred in the
architecture. Partial byte-range download resumption and first-call wake loading
are not advertised by this Phase 2 implementation; durable retry and explicit
manual/host start policies are supported.

## Final gate

Before opening the completion PR:

1. Finish independent review of the install/upgrade, completion-receipt and HTTP
   profile changes; fix every reproduced defect.
2. Run the full locked Python suite, Swift tests and native self-tests, plus the
   existing example regressions and privacy scanner.
3. Commit the intended source changes and build the final relocatable native app
   and CLI host from that exact clean commit.
4. Run bundled Python, model, HTTP, upgrade/recovery, configuration, downloads and
   management acceptance verifiers sequentially against those artifacts.
5. Replace pending entries in this audit with exact commit, artifact identity and
   measured check outcomes. Open and attach the PR only after this gate passes.
