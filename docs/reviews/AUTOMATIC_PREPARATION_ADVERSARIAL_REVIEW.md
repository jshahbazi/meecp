# Adversarial review of automatic preparation

Reviewed commit: `3bae17b0259ab404bd39bace1a747fbca87042a7`.

Scope: [docs/AUTOMATIC_PREPARATION.md](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/AUTOMATIC_PREPARATION.md), the recorded hotdog baseline, and supporting implementation in the same worktree. Read-only review; no repository changes, execution of imported source, credential reads, or runtime tests. All new preparation/registry behavior is proposed. Existing code below is evidence of an integration constraint, not evidence that the proposed feature has already failed.

The plan has credible scope controls and a useful acceptance structure. Three contract issues should be settled in the initial contract seed: attachment must be split into asynchronous staging and short admission; authority promises must match the same-user trust boundary; and result admission needs a shared linearization rule across import, job, grant and recipe state. Two further clarifications would keep extensibility evidence and deterministic claims assessable.

## P1: Reusing attachment unchanged violates the worker's responsiveness and transaction contract

**Plan evidence:** lines 143 and 149–155 require short transactions, no copying inside SQLite transactions, asynchronous work, and reuse of current static validation/snapshotting behind attachment admission.

**Implemented integration constraint:** [backend/mcpme/imports.py:386–391](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/imports.py#L386-L391) opens `BEGIN IMMEDIATE`. `ImportStore.attach` at lines 540–558 performs full package validation and `snapshot_package` while holding that transaction. [backend/mcpme/private.py:135–175](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/private.py#L135-L175) copies every file, calls `fsync` for each, revalidates source/staged/destination trees, and publishes the snapshot. The async management dispatch calls this synchronous method directly at [backend/mcpme/supervisor.py:1460–1461](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/supervisor.py#L1460-L1461). The hotdog baseline has 4,432 artifacts and about 199 MB of artifact bytes ([docs/HOTDOG_PREPARATION_BASELINE.md:14](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/HOTDOG_PREPARATION_BASELINE.md#L14)).

**Failure scenario:** a prepared package reaches `attaching`; invoking the existing method stalls management request processing during copying and prevents a draft cancellation or revocation from obtaining the import transaction. Moving that call to a thread improves event-loop responsiveness but still holds the SQLite write transaction across the whole copy. Larger model-service packages aggravate this problem.

**Required contract:** reuse the validators and snapshot implementation, not the existing attachment transaction unchanged. Persist an attachment intent bound to job/revision/expected digest, copy and validate into an unpublished attempt-owned tree outside the import transaction, then perform a short admission/publish operation. Define cleanup and restart reconciliation for a copied tree whose commit never happened. The same-revision idempotency rule must survive competing attempts.

**Acceptance test:** pause the copy after several files; show that unrelated management reads, preparation cancellation and draft revocation remain responsive. Resume with cancellation/revision change and prove the staged tree cannot attach. Kill the host before and after the short publication transaction, then recover without losing an already committed attachment or leaking unreferenced trees.

## P1: An absolute credential/installation prohibition contradicts retained same-user execution

**Plan evidence:** lines 87 and 159 say workers receive no manager credential and cannot obtain owner credentials or submit installation. Lines 161 and 97 explicitly retain current-user execution and acknowledge the lack of enforceable isolation for executable extensions.

**Implemented evidence:** the owner CLI reads `state_dir / "manager-connection.json"` and selects its owner token when no scoped credential is supplied ([backend/mcpme/__main__.py:45–57](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/__main__.py#L45-L57)). The host accepts the matching token as owner authority ([backend/mcpme/supervisor.py:1363–1365](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/supervisor.py#L1363-L1365)). Scoped credential loading intentionally has no owner fallback ([backend/mcpme/grants.py:29–43](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/grants.py#L29-L43)). This review inspected source only; it did not open the connection file or any credential.

**Failure scenario:** a permitted recipe worker, imported Python module, or agent-generated adapter runs as the same macOS user. Ordinary filesystem permissions that permit the owner's CLI to read its state also permit that code to read it. The code can then call the owner management interface rather than the runner's scoped services. A missing environment variable or scoped service handle does not prevent that path.

**Required contract:** distinguish the supported host-service authorization boundary from confinement of arbitrary code. State that the coordinator does not delegate owner credentials or installation capabilities, and that same-user executable code is trusted to respect that boundary. If third-party code is promised an enforceable inability to acquire owner authority, make a verified isolation/credential-separation capability a release gate for that claim. This does not require adding isolation to the first trusted/declarative recipe milestone; it requires removing an inaccurate absolute promise.

**Acceptance test:** supported runner calls cannot request installation, owner credentials, arbitrary ports or unbound inputs, and scoped credentials cannot gain owner fallback. Separately test the chosen isolation boundary if it is implemented. Do not describe the scoped-interface tests as proof against arbitrary same-user filesystem access.

## P1: Revocation and cancellation need a single result-admission winner across stores

**Plan evidence:** registry revocation blocks result admission (lines 114 and 117); attachment must recheck job authority, revision and cancellation (line 154); cancellation versus attachment has a defined commit winner (line 165). The plan lists the relevant race tests at lines 123 and 317, but does not identify the transaction or epoch that linearizes all these state changes.

**Implemented constraint:** attachment currently commits only the import store's state. `GrantStore` explicitly shares the registry transaction for atomic installation grant/operation binding (`backend/mcpme/grants.py:51–57,251–263`), whereas `ImportStore` has its own transaction ([backend/mcpme/imports.py:386–391](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/backend/mcpme/imports.py#L386-L391)). The proposed preparation store and recipe registry introduce additional state domains.

**Failure scenario:** a worker passes a revocation/authority check, then an administrator revokes the recipe or the user cancels the preparation, then the worker commits attachment in the import store using the earlier check. All checks ran, but the promised late-result exclusion failed. Performing another check immediately before attachment still leaves a gap unless admission and invalidation share a linearization mechanism.

**Required contract:** define the commit winner for cancel, draft revise/expiry, authority expiry/revocation and recipe revocation. Use one authoritative admission transaction or a durable, versioned admission token/epoch with an atomic compare-and-set under a common lock and explicit recovery semantics. An ordinary disable operation remains distinct: its already executing job may finish under the stated policy. Freeze lock/transaction ordering before worker and registry agents implement independently.

**Acceptance test:** place a barrier after the last optimistic check, commit each invalidation, then release attachment. Admission must reject if invalidation won. Reverse the order and retain the committed attachment as completion, matching the documented policy. Include crash recovery and a recipe registry update/revoke occurring while the copied result waits for admission.

## P2: Acceptance evidence lacks an explicit verifier/runner compatibility lifetime

**Plan evidence:** enablement depends on independent gate evidence (lines 97,110,123). Evidence is tied to bundle/component digests. Runtime attestation is external to the package (lines 56 and 153). Recipe manifests declare API/platform requirements (line 70), but the plan does not explicitly bind admission evidence to the host verifier/runner implementation that established process ownership and other guarantees.

**Failure scenario:** a recipe remains enabled after a host runner upgrade that changes supervision or a verifier-policy update that adds a mandatory check. Its bundle and API version remain unchanged, so the old evidence appears valid even though it does not establish the new runner/policy behavior. This matters most for executable-worker admission and reused compatibility evidence.

**Recommended contract:** give gate evidence a versioned schema binding exact bundle/component digests, required gate versions, verifier/runner identity, execution-policy digest, applicable platform profile, outcome and retention/compatibility policy. Define which host updates preserve evidence and which require revalidation or disablement. A fresh preparation still runs all mandatory per-job checks; registry evidence should not substitute for them.

**Acceptance test:** retain a recipe bundle unchanged while upgrading a fake runner/verifier from one incompatible policy identity to another. Old evidence must not enable execution automatically. Compatible evidence migrations should be explicit and testable.

## P2: Define what “deterministic” promises before making it a capability claim

**Plan evidence:** the deterministic builder is reused (line 21), dependency/artifact identities are frozen, receipt identities are sealed, and different same-revision results cannot replace one another (line 163). The plan does not say whether determinism means absence of an LLM, identical normalized decisions, bit-identical package production, replay-safe side effects, or stable model outputs.

**Failure scenario:** two attempts with identical source/descriptor inputs produce different package bytes because of a runtime/toolchain/build metadata variation. Both are well sealed, but only one can attach to the same revision. Conversely, a fixed model invocation can vary in its result while deterministic preparation remains correct. An undifferentiated “deterministic” label would make both issues difficult to classify.

**Recommended contract:** separate these promises explicitly. Automatic preparation means the recipe needs no LLM once required bindings are complete. Reproducible packaging, if claimed, needs the complete builder/toolchain/runtime inputs and a same-input digest test. Replay safety concerns effects and restart behavior. Model determinism is an adapter-specific property. Keep job-specific timestamps/attempt IDs in the external job record if they would otherwise defeat a desired reproducible digest.

**Acceptance test:** prepare the same supported recipe twice in clean workspaces. Either require equal package digests under a named reproducibility profile, or state the permissible sources of variation and confirm that different digests follow the existing revision/idempotency rule. Do not apply byte equality to sampled LLM output unless the adapter explicitly promises it.

## Safeguards that defeat tempting criticisms

- **“The plan installs untrusted source during inspection.”** It explicitly prohibits source/recipe execution during match/normalize/plan, requires independent execution authority, and preserves installation's separate plan/apply flow. This is not a valid finding.
- **“A local hash proves publisher trust.”** The plan explicitly marks host-hashed local artifacts as publisher-unverified, retains origin/licenses, and describes signatures as additional provenance. This is already handled.
- **“An agent can bless its own new recipe.”** Proposed bundles are added statically; executable enablement and required independent evidence are separate. Development verification before enablement is digest-bound and cannot promote the bundle. The relevant authority wording needs the correction above, but the proposal/admission design is sound.
- **“Runtime verification mutates a self-referential package receipt.”** The receipt is an ordinary locked artifact, final digest/attestation are external, final sealed bytes are checked before and after probes, and attachment must compare the expected digest. The plan already avoids that cycle.
- **“The existing reaper proves full descendant ownership.”** The plan does not make that claim; it blocks executable-worker/CLI admission until topology guarantees pass. The present reaper supervises one foreground target and signals only that child (`scripts/mcpme-reap.c:1,132–169`). The topology spike remains a real gate, not an implemented capability.
- **“Concurrent hotdog requests necessarily decode large images simultaneously.”** The reused Python adapter serializes the entire selected callable (`templates/python-function-v1/adapter.py:41,54–62`), before the detector's decode step. That defeats this criticism for the reused adapter. Preserve that property if the new ONNX adapter differs. Maximum-size decoding/deadline measurement is already an explicit release gate.
- **“The three blank images prove useful recognition accuracy.”** Both documents explicitly reject that conclusion. The negative real-model/positive controlled-tensor distinction is honest; future accuracy requirements need a separate fixture set and threshold.
- **“New recipes require changing the host for every model variant.”** Declarative composition, pinned components, bounded generated forms and the zero-host-edit acceptance gate address that requirement. A novel authority/runtime/transport still needs host capability work, which is a coherent limit. The first extension acceptance fixture should exercise a genuinely additive recipe, rather than only registering a second name for identical bytes.

## Shipping assessment

Proceed with the contract seed after resolving the three P1 issues. The first milestone is feasible as an explicitly bounded trusted/declarative recipe path with Python, existing MCP and the pinned CPU detector. Keep executable-worker enablement conditional on the ownership capability gate; do not make a general arbitrary-code confinement claim for it. The parallel lanes are sensible once the admission and attachment contracts are frozen, because their independently produced state machines otherwise risk implementing incompatible commit winners.

Reviewed document: [automatic preparation plan](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/AUTOMATIC_PREPARATION.md).
