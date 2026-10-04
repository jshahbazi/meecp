# Public preparation execution admission

This checkpoint adds the internal admission contract for public automatic
preparation. The release source hook still returns `None`, so ordinary CLI and
native launches continue to report execution unavailable. Actual final signed
artifact acceptance and an independent review must precede source activation.

`PublicPreparationAdmission` is an immutable in-process value created by trusted
host source. It specifies exact family `(id, version, digest)` keys, complete
recipe component closures, and ordinary Python resolution candidate kinds.
Management payloads, user configuration, recipe manifests, and environment
variables cannot construct or select it. A host with this admission cannot also
inject private development runner or agent factories. Existing explicit private
acceptance factories retain their previous behavior.

Preparation checks the complete transitive component closure for both descriptor
schemas. Enabling one composite family no longer admits every legacy descriptor.
Legacy Python execution requires its own exact admitted closure. Independent
declarative recipe IDs can use an admitted closure without dispatcher changes;
their source binding, form validation, recipe epoch, owner authority, verification,
and attachment checks still apply. Recipe revocation and disabled execution
runners remain authoritative.

Public resolution uses `ResolutionStore` and `OwnedResolutionRunner`. It admits
only the selected original inspected Python callable or MCP candidates. It does
not select the agent proposal resolver. Planning and start validate the captured
source, complete candidate metadata, and current producer. Each controlled
execution and interface discovery repeats those checks before launching work.
Publication, result binding, and later consumption require the same admitted
candidate and exact current producer. The bounded atomic binding commit preserves
its checked proof and current draft/job metadata fences; it performs no file IO.

The public resolution producer is explicitly `python-resolution-v1@1.0.1`, with
the admission digest included in its implementation digest. Old private producers
remain version 1.0.0. Public adoption rejects old, unknown future, or changed
producer identities. Existing requests, plans, jobs, results, and accepted binding
schemas remain unchanged. Rejected records are retained; the host never silently
rewrites accepted historical jobs or receipts. A reviewed metadata revision can
carry an identical immutable result through the existing checked binding flow.

The management capabilities response uses the actual host and its exact admitted
live runners. Preparation availability requires an enabled recipe using an
admitted closure. Resolution availability requires the ordinary runner and its
matching store admission. Closing a runner or host removes execution availability.
Prepared-package installation retains its existing path and authority.

The admission module participates in the shared preparation and resolution
producer closures. Changing the admitted recipe set changes a preparation
execution producer; changing resolution candidate admission changes its producer.
The frozen ResolvedPython four-file component closure, ONNX component/runtime,
and agent component producer arrays remain unchanged in this checkpoint.

TextGen binds the complete backend module closure. Integration therefore requires
a newly minted 1.0.9 component and recipe after all backend changes are finalized.
The existing 1.0.8 and earlier profile/recipe bytes remain immutable, and 1.0.8
will fail fresh execution compatibility after these source changes. No new
TextGen version is minted here. The existing signed asset may be reused only if
its own exact producer and runtime contract remains unchanged.

Consequently the existing all-recipes-ready assertion in
`test_recipe_component_evidence.py` remains an integration failure until that
mint. Its expected compatibility denial is retained rather than rewriting the
historical recipe or reducing the assertion.

The contract tests exercise complete recipe closure, custom IDs, schema-1 denial,
family independence, producer changes, stale plans, agent metadata rejection,
publication/adoption/consumption, metadata revision carry, before-execution denial,
and live capabilities. They establish authority behavior. Final installation,
real SDK calls, source-free restart, relocation, signing, resource measurements,
and the remaining Phase 3 gates still need evidence from the final integrated
artifact. This checkpoint makes no hard aggregate quota or lifetime containment
claim.
