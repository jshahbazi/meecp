# Neutral review of automatic preparation

Reviewed plan commit: `3bae17b0259ab404bd39bace1a747fbca87042a7`.

Verdict: the architecture is feasible within its declared macOS/schema/MCP boundary and is coherent enough to begin the contract seed and capability spikes. It is not yet a frozen implementation specification. Resolve the preparation-toolchain bootstrap and attachment transaction design before advertising the first milestone. Freeze the extension scope and runner contract before dispatching their dependent implementation lanes.

This is a read-only design review of [docs/AUTOMATIC_PREPARATION.md](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/AUTOMATIC_PREPARATION.md), [docs/HOTDOG_PREPARATION_BASELINE.md](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/HOTDOG_PREPARATION_BASELINE.md) and supporting source. No runtime tests were run. Current behavior below was checked in this checkout; planned behavior remains proposed.

## Findings

### N1 — P1: Preparation needs its own reproducible toolchain bootstrap

Plan references: “Verified starting point,” lines 21–22; “Artifact acquisition and source lifetime,” lines 129–135; “Python and existing MCP,” line 173; “Acceptance and release gates,” lines 308–309 and 319.

The plan reuses the existing Python runtime/wheel resolver but does not choose how a distributed app acquires and runs that resolver. Today [templates/prepare.py:122–132](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/templates/prepare.py#L122-L132) invokes `uv` by name, finds managed CPython outside the app and installs locked wheels. [docs/PACKAGE_AUTHORING.md:30–32](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/PACKAGE_AUTHORING.md#L30-L32) explicitly places those build tools in an external agent workspace. [scripts/build_mcpme_host.py:156–180](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/scripts/build_mcpme_host.py#L156-L180) bundles Python, the backend, templates and lock files; it does not bundle the `uv` executable. The native app embeds that host at [scripts/build_mcpme_app.py:124–125](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/scripts/build_mcpme_app.py#L124-L125).

A development-machine run could pass the proposed automatic path while depending on the user's uv installation/cache. The final-package relocation probe would not expose that preparation dependency because it runs after construction.

Action: select a pinned preparation-toolchain distribution in the contract seed. Define whether the app bundles its resolver and copies its own compatible CPython, or admits separately hash-verified toolchain artifacts into private preparation storage. Record toolchain provenance/licenses and route its invocations through the supervisor. Include its acquisition destinations and storage in review. Keep writes outside the signed app bundle.

Acceptance addition: prepare synthetic Python and the exact detector from the distributed app with no uv on PATH, no checkout/development environment and no managed-Python cache. If cold acquisition is required, use only admitted destinations; if offline support is claimed, repeat with networking unavailable and the explicitly supported cache. This is a first-release blocker, not evidence that the existing package runtime is incomplete.

### N2 — P1: Attachment reuse needs an explicit two-phase transaction boundary

Plan references: “Preparation jobs and authority,” lines 143, 153–155 and 165; “Acceptance and release gates,” line 317.

Line 143 requires short transactions and forbids waiting for copying while holding a SQLite transaction. Line 154 reuses the current attachment validation/snapshot implementation behind the new admission check. Today `ImportStore.attach` enters its transaction at [backend/mcpme/imports.py:541](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/imports.py#L541), validates the package at line 544, and copies it at line 556 before saving. `_transaction` issues `BEGIN IMMEDIATE` at line 389. The management route calls attachment synchronously at [backend/mcpme/supervisor.py:1461](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/supervisor.py#L1461). Reusing that operation unchanged would retain the long lock and event-loop blockage. Moving the whole operation to a thread removes event-loop blockage but retains the transaction spanning the copy.

Action: explicitly assign a refactor to the contract/worker integration owners. Validate and copy the expected sealed digest to job-owned temporary staging outside the commit transaction. After the copy settles, perform a bounded admission transaction that rechecks draft revision/state, cancellation, authority, registry revocation and expected digest, then publishes the verified staging identity. Define private staging ownership, atomic publication and orphan recovery so draft cancellation cannot delete a live copy.

Acceptance addition: pause a large attachment copy and verify management requests remain responsive and unrelated draft writes proceed. Race revision, cancel and revoke against final admission; crash immediately before and after publication. Every outcome must preserve the declared commit winner and exact verified digest. This is a first-release blocker unless the implementation explicitly replaces the current transaction boundary.

### N3 — P2: Specify which recipe extension kind is required for the first release

Plan references: “Intended result,” line 13; “Adding and managing recipes,” lines 72–89 and 119–125; “Acceptance and release gates,” lines 307 and 325.

The plan supports declarative compositions and executable workers. Executable admission is correctly blocked on full descendant ownership and cancellation proof. The first-release extension gate requires a fresh minimal recipe, while its listed cases include worker cancellation/reaping and incompatible worker APIs. It does not say whether the first release must admit third-party executable workers or whether a fresh declarative recipe satisfies the additive-extension requirement.

That choice materially changes the first milestone. The current native reaper owns one child: [scripts/mcpme-reap.c:134–146](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/scripts/mcpme-reap.c#L134-L146) waits for that child, and lines 164–169 signal it. The plan already acknowledges the topology gap for CLI at line 181. Generic executable extensions need the same capability proof.

Action: publish an initial recipe capability matrix. Name the supported declarative step/component catalog and explicitly state whether executable-worker admission ships at A0R or remains disabled behind its own gate. For each extensibility test, mark the required extension kind and applicable release gate. The fresh recipe must use ordinary published runner/components and generated forms; its ID must have no host special case.

Acceptance addition: the declared first-release kind passes the fresh-recipe workflow. An unavailable executable kind produces a stable compatibility blocker. If executable workers are included, their ownership gate becomes an explicit critical-path dependency. This is a scope decision, not a reason to remove extensibility.

### N4 — P2: Separate automatic matching ambiguity from an explicitly pinned recipe

Plan references: “Common preparation contract,” lines 42 and 54; “Adding and managing recipes,” lines 101 and 110–117.

The descriptor selects an exact recipe/version/digest, but eligibility requires “exactly one compatible recipe.” New recipes can legitimately overlap the same source shape. If that means exactly one compatible entry in the registry, adding a variant could block an existing fully pinned descriptor. That would conflict with the promise that existing descriptors/jobs retain their implementation digest. If it means validating the single explicitly selected entry, there is no conflict, but the rule should say so.

Action: require unique selection only for automatic discovery. An explicitly selected pinned recipe must independently satisfy compatibility, complete bindings and admission; other compatible entries should not invalidate it. Ambiguous discovery returns candidates and requires selection.

Acceptance addition: register two compatible variants. Discovery requires a choice; a descriptor pinned to either admitted version prepares reproducibly. Adding a third variant cannot change a running job or block an existing pinned request.

### N5 — P2: Close the release dependencies and freeze the runner protocol before parallel implementation

Plan references: “Bundle and stable runner contract,” lines 70–89; “Parallel execution graph,” lines 258–272 and 284–294; “Immediate execution packet,” line 329.

The plan correctly puts all recipes behind a registry and runner contract, but the Mermaid graph omits registry R from the CLI and generic-LLM gates K/L. The text also enables A3–A5 independently without explicitly repeating their common foundation gates. Capability spikes can proceed early; release admission still requires the common registry, authority and verifier to be accepted.

The runner's JSON/stdin-stdout interface and scoped service handles are architectural descriptions. Independent workers need a shared message contract before implementation: framing/message bounds, request IDs, service names and arguments, immutable input handles, effect/resource narrowing, progress/error vocabulary, cancellation acknowledgement, invocation settlement and artifact return semantics. Lane A is assigned the freeze, so this is an immediate deliverable rather than an unacknowledged feature.

Action: add R → K/L and state that every production recipe capability requires the applicable common A0/A0R/runner authority gates plus its family gate. Publish a small versioned runner protocol and fake conformance harness from A before B/R/D/H implement their interacting interfaces. Keep CLI/LLM research and executable fixtures parallel while that contract is frozen.

Acceptance addition: two independently implemented workers conform to the same fake runner; unknown services, oversized messages, duplicate/stale replies and cancellation races fail predictably. A family gate cannot enable a recipe whose registry/runner capability is unavailable.

## Coverage assessment

| Area | Assessment |
| --- | --- |
| Deterministic preparation | The current generators make the Python/existing-MCP path credible. Exact interface selection, locked closure and a common descriptor are sensible. N1 is the missing distributed preparation dependency. |
| Recipe extensibility | Versioned closed-tree bundles, generated forms, local addition and immutable registry lifecycle fit the user requirement. N3 bounds the first deliverable; N4 prevents additive recipes from disrupting pinned requests. |
| Authority and ownership | The plan preserves metadata-only intake and separate installation authority, and explicitly discloses same-user execution. Full descendant ownership remains a capability proof, not current behavior. |
| Provenance and licenses | Artifact identities, provenance distinctions, notice closure and receipts are appropriately explicit. The baseline's supplemented wheel notices are part of the workload the automatic recipe must reproduce; a hash is not publisher attestation. |
| State and cancellation | Durable jobs, late-result rejection, reservation retention and attachment commit winners cover the principal races. N2 is needed to implement those rules without long transactions. |
| MCP and model runtime | CPU ONNX under stdio respects schema 1. Generic LLM fixture selection and CLI topology are correctly gated spikes. The baseline distinguishes small negative-model checks from positive-tensor checks and unmeasured maximum inputs. |
| Acceptance and proportionality | Gates are concrete and make no broad accuracy/compatibility claim. The initial registry/SDK/UI plus three automatic paths is substantial; explicit extension-kind scope keeps that milestone reviewable. |
| Parallel orchestration | Owned lanes, fake interfaces and independent verification permit useful parallel work with the available slots. N5 makes the release dependency graph reflect the common contracts. |

## Decisions to settle in the first contract packet

1. The pinned preparation toolchain and cold/offline acquisition policy.
2. The two-phase attachment staging/publication and crash-recovery contract.
3. Initial extension kinds, named runner/component capabilities, and required independent gate evidence.
4. Exact worker message protocol, state transitions, and quantitative message/artifact/disk/queue limits.

The reviewed hotdog baseline is useful functional evidence for one prepared package. Its documented limits are accurate. It does not establish automatic preparation, executable extension ownership, generic LLM compatibility or first-release readiness.

Reviewed document: [automatic preparation plan](https://github.com/jshahbazi/meecp/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/AUTOMATIC_PREPARATION.md).
