# Supportive review of automatic preparation

Reviewed 2026-10-02. Plan commit: `3bae17b0259ab404bd39bace1a747fbca87042a7`. This review treats the proposed direction as worth pursuing and looks for changes that improve delivery. It does not treat proposed behavior as implemented.

The architecture is a sound basis for implementation. Complete, immutable recipe bindings can support preparation without an LLM; the optional agent route has a useful analysis phase for incomplete inputs; both routes return through the existing package boundary. The plan also gives recipe extension an observable acceptance test. I found no fundamental architectural blocker. Two concrete integration gaps should be closed explicitly before advertising the first automatic milestone: the existing attachment path copies packages inside a database transaction and the event loop, and the reused Python builder does not automatically reproduce the baseline's supplemented dependency notices.

No code was changed and no tests were run. Evidence comes from the plan, its recorded hotdog baseline, and focused inspection of current source at the stated commit.

## Strong decisions to preserve

- **Static intake and explicit bindings:** “Common preparation contract,” lines 33–58, separates portable inputs from host authority, prohibits import-based discovery, pins identities, and keeps the package receipt outside strict lock-schema extension. This gives automatic preparation a tractable eligibility rule.
- **Additive recipe extension:** “Adding and managing recipes,” lines 62–125, separates descriptors, bundles and registry state; fixes recipe versions to bytes; provides native forms and a development kit; and requires an independently added recipe with zero host edits. These are useful product and engineering acceptance conditions.
- **Honest execution boundary:** “Preparation jobs and authority,” lines 157–161, distinguishes preparation from installation and states the same-user trust limit. “Bundle and stable runner contract,” lines 87–89, makes process ownership a runner concern. The CLI release gate correctly requires a topology proof rather than assuming the current stdio supervisor covers arbitrary command trees.
- **Correct model seams:** “YOLO/ONNX” and “Local LLM,” lines 187–205, separate inference engines from task interpretation. CPU ONNX fits the current schema without invented vision context metadata; generic generation does not inherit JevK5 scoring/cache semantics. Current [backend/mcpme/contracts.py:327–334](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/contracts.py#L327-L334) supports this scope choice.
- **Bounded evidence:** “Acceptance and release gates,” lines 319–325, requires the actual sealed runtime, relocation, before/after digest equality and independent checks. The hotdog baseline, lines 55–63, correctly distinguishes small negative-image inference, positive postprocessing tensors and unmeasured maximum-size inputs.
- **Parallel delivery with controlled integration:** “Parallel execution graph,” lines 282–300, assigns shared-contract ownership and independent verification while letting runtime families advance separately. The release flags let a passed capability ship without waiting for all later families.

## Findings and recommendations

### S1 — P1: Make attachment staging a first-milestone integration task

**Plan location:** “Preparation jobs and authority,” lines 143, 154–165; lane A/B, lines 284–285; A1–A2, lines 308–309.

**Current evidence:** [backend/mcpme/imports.py:540–558](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/imports.py#L540-L558) runs `validate_package` and `snapshot_package` while `_transaction()` holds the import lock and a `BEGIN IMMEDIATE` transaction (`imports.py:385–391`). [backend/mcpme/supervisor.py:1461](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/supervisor.py#L1461) calls that method directly from the management request route. The hotdog closure is approximately 199 MB (`HOTDOG_PREPARATION_BASELINE.md:14`), and later model packages can be much larger.

**Delivery risk:** Reusing that method as-is at step 6 leaves package copying on the event loop and holds the database writer transaction during the copy. This conflicts with the new responsiveness/cancellation intent even if the earlier preparation worker is asynchronous. Calling the whole current method in a thread removes event-loop blocking but retains the long transaction and lock.

**Recommended change:** Name a two-phase attachment refactor in the immediate execution packet: validate and copy to host-owned staging outside the database transaction; then perform a short admission transaction that rechecks draft revision, cancellation, authority/recipe revocation and the expected verified digest before publishing the attachment. Preserve current external attachment compatibility and digest/idempotency rules. Give this integration to the shared-contract owner with the worker lane, because it spans imports and the job store.

**Acceptance:** Deliberately slow a synthetic package copy. Management status and cancellation remain responsive, unrelated database writes proceed, cancellation/revision changes can win admission, and the losing staged candidate is eventually collected after its copy settles. A process crash between copy completion and publication cannot expose an admissible attachment accidentally.

**Blocker:** Required before the first automatic path is advertised; it does not prevent other lanes from starting.

### S2 — P1: Specify how the first recipe supplies missing dependency notices

**Plan location:** “Artifact acquisition and source lifetime,” lines 129–135; “Python and existing MCP,” line 173; A2, line 309.

**Current evidence:** [templates/prepare.py:146–153](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/templates/prepare.py#L146-L153) copies Python's notice and writes a generic dependency inventory pointing to wheel metadata/notices. It does not check that every installed distribution actually has the relevant notice or acquire an absent one. The hotdog baseline, line 17, records that FlatBuffers and HTTPX wheel notices needed separate versioned upstream supplements.

**Delivery risk:** Reusing the current runtime builder alone cannot reproduce the baseline's complete notice set. The first acquisition scope supports local files and the wheel resolver, while general pinned HTTPS acquisition is allowed to arrive later. The first hotdog gate therefore needs an explicit source for its known missing notices.

**Recommended change:** Add a pinned notice inventory/resolver to the first recipe path. For the initial known dependency set, ship or select exact notice artifacts in a recipe/component bundle, or include narrowly scoped immutable upstream retrieval as part of the declared resolver. Bind distribution version, notice hash, origin and artifact references. A generic `DEPENDENCIES.md` must not satisfy the completeness gate by itself. Retain the existing plan's explicit acquisition authority and hash-versus-publisher distinction.

**Acceptance:** Build from a clean dependency cache using the baseline wheel set. Every distribution maps to retained notice artifacts; intentionally remove a wheel notice and require the pinned supplement or a precise blocker. Change the supplement bytes/version and require failure. Preparation must not rely on the reviewer manually downloading a notice afterward.

**Blocker:** Required to pass the first hotdog closure gate.

### S3 — P2: Turn lifecycle prose into a transition and reservation contract at A0

**Plan location:** “Preparation jobs and authority,” lines 145–165; A0, line 306; race coverage, line 317.

The plan contains the important lifecycle rules. It leaves their representation and transition guards to implementation. In particular, `ownership_lost` is described separately from the listed job states, while interrupted jobs can be retried and reservations can remain held. Several agents will implement stores, processes, cancellation and UI against this seam.

**Recommended change:** Add a versioned transition table or executable fake store to the A0 packet. Each transition should name its admission guard, owner, durable evidence, acquired/retained/released reservations and recovery action. Represent ownership explicitly so `interrupted` with unverified live work cannot be treated as an ordinary retryable failure. Define which attachment/cancel/revocation decision is atomic across the affected records.

**Acceptance:** A table-driven race harness proves that terminal cancellation has no owned work or admissible result, ownership loss keeps reservations and blocks replacement, and attachment success publication recovers by exact digest. The current spawn-acknowledgment shielding in [backend/mcpme/processes.py:200–217](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/processes.py#L200-L217) is a useful invariant to preserve.

### S4 — P2: Choose a concrete declarative extension witness for the first milestone

**Plan location:** “Bundle and stable runner contract,” lines 72–89; “Extensibility acceptance gate,” lines 121–125; A0R, line 307.

The fresh-recipe test is strong, but the minimum witness is unspecified. The plan admits declarative composition and later executable preparation workers. A first extension witness that accidentally requires executable-worker support would put the broadest ownership/service-interface work on the first milestone's critical path.

**Recommended change:** Specify a small independently authored declarative recipe as the first A0R witness, using supported steps and generated native fields. It should change a meaningful callable contract or task mapping, carry its own fixtures and immutable component references, and exercise add/enable/disable/update/restart retention. Keep executable workers behind their explicit admission/cancellation gate; the public versioned contract may be seeded before that kind is enabled.

**Acceptance:** The witness is authored outside the host tree after the registry interface is frozen, added through both CLI and UI, prepared and attached, and requires zero host source edits. A version collision and unsupported capability fail with focused errors. This preserves the user's easy-add requirement while keeping the initial implementation bounded.

### S5 — P2: Define the repeatability promised by “deterministic” preparation

**Plan location:** “Intended result,” line 9; canonical identities, line 52; receipt, line 56; idempotency, line 163.

The plan clearly means that complete recipes run through predetermined steps without an LLM. It does not explicitly promise byte-for-byte reproducible packages. Those properties differ: runtime toolchain receipts, generated build paths or install metadata can vary despite pinned dependencies. Current [templates/prepare.py:141–145](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/templates/prepare.py#L141-L145) includes the preparing `uv` version in a locked runtime receipt.

**Recommended change:** State the first-release guarantee precisely: deterministic selection/steps and immutable input/output verification, with reproducible package bytes only for recipe families that pass a separate rebuild check. Specify which metadata is intentionally attempt-specific and keep it outside the portable sealed closure where practical. Do not weaken final artifact hashing to accommodate variability.

**Acceptance:** Rebuild a supported synthetic recipe in two clean workspaces. Compare tool/manifest semantics and the complete artifact diff, record expected variability explicitly, and require equal final digests only when that recipe declares reproducible output. This makes future cache and retry behavior easier to implement honestly.

### S6 — P2: Make the parallel contract seed a small, runnable integration packet

**Plan location:** “Parallel execution graph,” lines 282–300; “Immediate execution packet,” lines 329–331.

The lane boundaries are well chosen. The main avoidable coordination cost is parallel workers interpreting prose differently before the shared interfaces become runnable. This matters especially for recipe-worker service requests, descriptor form normalization, sanitized errors and attachment admission.

**Recommended change:** The first integration packet should include representative descriptor/bundle/job/receipt JSON, error codes, a fake runner with launch/cancel/resource hooks, a fake management response stream, and contract tests executable by every lane. Pin packet revision in every task. Let independent research/spikes run before the seed, but merge API-dependent implementation only against a named revision. Include separate synthetic Python and existing-MCP witnesses; the graph's “Python end-to-end gate” label should not hide A1's separate existing-MCP requirement.

**Acceptance:** UI, registry, builder and verifier lanes can each run their contract tests without the real job runner. A schema change breaks those tests immediately and sends targeted repair work to affected owners. This gives the coordinator measurable integration progress while the limited agent pool rotates through ready lanes.

## Recommended disposition

Proceed with implementation planning around this architecture. Add S1 and S2 to the first release's concrete work/evidence packet. Incorporate S3–S6 while freezing A0 rather than opening unrelated workstreams. Preserve the existing distinction between code-free inspection, preparation execution, independent sealed validation, static attachment and separately authorized installation.

Reviewed document: [automatic preparation plan](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/AUTOMATIC_PREPARATION.md).
