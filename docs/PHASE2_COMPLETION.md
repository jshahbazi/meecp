# Phase 2 completion audit

This audit follows the Phase 2 delivery and acceptance requirements in
[MCPME_DESIGN.md](MCPME_DESIGN.md#phase-2-agent-installation-and-recovery).
It records implementation and verification separately. The normative Phase 2
implementation and acceptance checks passed on Apple silicon macOS 26 on
2026-10-02. The verified runtime implementation commit is
`b28edab85fcc5ced3e76117caf8e43b622f7e776`. Subsequent changes record these results
and correct the HTTP verifier's required MCP request header; runtime, native,
template and build sources remain identical to that commit.

## Requirements and evidence

| Requirement | Implementation | Verification status |
| --- | --- | --- |
| Versioned package manifests, complete immutable locks, safe paths and artifact closure | Static package validator and private sealed snapshots; stdio, model and native HTTP profiles | Full locked suite passed: 1,447 tests; one filesystem casefold skip |
| Raw Python intake and external agent preparation without hand-written manifests | Persistent selected-source drafts, setup export, versioned generator, revision-bound attachment | Final bundled Python verifier passed intake/export/generator/return/install, HTTP/stdio and draft restart |
| Raw JevK5 weights and engine intake and preparation | Pinned GGUF and engine snapshots, engine selection after draft restart, export-driven generator, exact preparation receipt | Final real 4.48 GB model acceptance passed all 18 checks; upstream probability difference was zero |
| Resume drafts, reject stale or modified package returns, cancel and expire inactive drafts | Durable revisions and selected snapshots; explicit settings-only attachment retention revalidates sealed artifacts | Full suite, native self-tests and bundled intake verifiers passed |
| Typed configuration and secret references | Primitive schema, owner-only secret metadata, private descriptor delivery, reviewed generation upgrades for installed changes | Final settings verifier passed private delivery, restart, scoped denial and revocation; upgrade verifier passed settings changes |
| Scoped management MCP and explicit plan/apply authority | Existing bounded grants and typed management bridge; upgrade and cancellation/recovery mutations remain owner-only | Final management verifier and full owner/grant regression suite passed |
| Native setup and installation progress | Download, typed configuration, secret selector, raw model and engine selection, upgrade review and recovery controls | Swift: 18 passed, three opt-in installer lifecycle skips; all three release-binary self-tests passed; inert previews reviewed |
| Pinned downloads, partial failure, cancel and retry | Size and SHA-256 pins, bounded safe ZIP closure, private durable jobs and cancellation writer barriers | Final bundled download verifier passed pinning, duplicates, interruption, cancellation, restart and installed SDK calls |
| Duplicate applies create one installation or upgrade intent | Durable plan/import idempotency and atomic candidate registration/selection | Full concurrency suite and final Python, settings and management verifiers passed |
| Every install/upgrade interruption stage recovers safely | Durable selection and exposure markers, retained generation data, completion receipts from identity-holding native reapers | Full stage fault matrices and actual receipt/reaper-loss tests passed; final bundled SIGKILL recovery passed |
| Pre-exposure rollback preserves old data and endpoint | Quiesce old writers, snapshot into a private candidate data directory, verify before selection, restore old pointers only before exposure | Final persistent-service verifier passed failed smoke rollback, old accepted data and stable endpoint checks |
| Post-exposure writes survive routing failure | Registry exposure permission is committed before ordinary admission; failure retains selected data and offers candidate recovery | Final bundled upgrade verifier passed all 12 checks, including accepted writes, Stop/Recover and supervisor death |
| Tampered artifacts, unsafe paths, port conflict, unavailable secrets, MCP incompatibility, exhausted disk and failed probes cannot activate a broken generation | Validation gates, owned readiness and protocol probes, private settings binding, reservations, bounded data cloning | Full suite, actual stdio smoke rejection and bundled upgrade/HTTP verification passed |
| Removal retains data unless deletion is explicitly requested | Durable stop/reap, credential revocation, unregister with retained packages and all generation data | Full removal, credential and restart regression suite passed |
| Authoring documentation and versioned preparation templates | Python function, existing MCP and JevK5/llama.cpp templates ship with the relocatable host | Full static/template suite, all three bundled generators and documentation/link review passed |
| Relocation, independence and content-free diagnostics | Bundled runtimes/templates, sealed selected-source identity, suppressed process streams and raw exception output | All seven final bundled verifiers passed relocation/independence; privacy and credential checks passed |

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

## Verified artifacts and checks

Both artifacts were built from the clean implementation commit above; their
receipts record `source_tree_dirty: false`. These are ad hoc development artifacts.
Public signing/notarization and enabled next-login behavior were not exercised by
this acceptance run.

| Artifact receipt | SHA-256 |
| --- | --- |
| `dist/mcpme-phase2-completion-final.app.build.json` | `4e245043ce185a089e8e1aa729e9d377734a2b582c246bb6226b61b959688c43` |
| Native app's `Contents/Resources/host/build.json` | `a55f87c0b8d08424515584ad691cd9a85de8d00d2abffb1f034256ad1ffb76cb` |
| `dist/mcpme-phase2-completion-final-host/build.json` | `9f3b37d8571bddaedb8f92b662b757ede282f7530552274e532b83ef137b3d40` |

The raw model run used the pinned JevK5-4B v0.3 Q8_0 weights, SHA-256
`aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30`,
and the verified llama.cpp build `11146-7fe450e19305b828c199d602c23a8337aaa1f03b`.
Supplied original weights and engine files remained untouched. The external
generator, installed package and model runtime worked after removing the
verifier's selected-source and external preparation copies.

| Check | Result |
| --- | --- |
| `uv sync --locked` | Passed |
| Full locked Python suite | 1,447 passed; one casefolding-filesystem skip; 115.20 seconds |
| Hotdog example suite | 45 passed |
| Swift tests | 18 passed; three opt-in native installer lifecycle tests skipped |
| Final native binary self-tests | Native, dashboard and management contracts passed |
| Privacy/credential scanner | `secret_scan_passed` |
| `verify_phase2.py` on native bundled host | Passed |
| `verify_model_phase2.py` on CLI host | 18 checks passed; maximum absolute upstream probability difference 0 |
| `verify_http_phase2.py` on native bundled host | 15 checks passed, including private/public auth, shared resident, session deletion, restart key/nonce rotation and stale-session rejection |
| `verify_upgrade_phase2.py` on CLI host | Passed, including real SDK writes, independent-slot responsiveness, rollback, saved Stop and SIGKILL receipt recovery |
| `verify_settings.py` on native bundled host | Passed |
| `verify_downloads.py` on CLI host | Passed |
| `verify_management.py` on native bundled host | Passed |

All bundled verifiers ran sequentially. Independent review reproduced installation
Stop/recovery races, an unavailable-journal admission gap, and mutating smoke
replay; those defects were fixed and retained as regressions. Actual acceptance
also reproduced held-gateway recovery and startup-ledger ordering defects; both
were fixed. The final independent review found no remaining actionable defect.
The source commit and artifact checks establish this acceptance boundary; they do
not establish model accuracy or enabled login-item transfer behavior.
