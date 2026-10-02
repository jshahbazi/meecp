# Automatic preparation: three independent reviews

Reviewed 2026-10-02 against [plan revision `3bae17b`](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/AUTOMATIC_PREPARATION.md) and its [hotdog baseline](https://github.com/jshahbazi/mcpme/blob/3bae17b0259ab404bd39bace1a747fbca87042a7/docs/HOTDOG_PREPARATION_BASELINE.md). Three agents reviewed the same immutable revision concurrently, using supportive, neutral and adversarial perspectives. The coordinator reconciled duplicate findings; a separate read-only source check verified the cited implementation constraints.

The design is a credible basis for contract work and bounded capability spikes. The reviews found no reason to abandon deterministic preparation or additive recipes. Five high-priority items need explicit first-release work or corrected contracts. The original plan and runtime code remain unchanged by this review.

## Reviewer conclusions

| Perspective | Conclusion | High-priority findings |
| --- | --- | --- |
| [Supportive](AUTOMATIC_PREPARATION_SUPPORTIVE_REVIEW.md) | Preserve the architecture and make its delivery prerequisites explicit. | Async attachment staging; automatic collection of missing dependency notices. |
| [Neutral](AUTOMATIC_PREPARATION_NEUTRAL_REVIEW.md) | Feasible and ready for the contract seed; several interfaces and first-release choices remain unfrozen. | A pinned distributed preparation toolchain; attachment transaction refactor. |
| [Adversarial](AUTOMATIC_PREPARATION_ADVERSARIAL_REVIEW.md) | Proceed after resolving the authority and admission contracts. | Attachment transaction refactor; accurate same-user authority promises; one atomic admission winner across stores. |

## Consolidated priorities

P1 means a contract or implementation prerequisite for the first advertised automatic capability. P2 means clarification to settle while freezing the initial interfaces. These are design-review priorities, not reports of newly implemented runtime defects.

| Priority | Finding and evidence | Required outcome | Review support |
| --- | --- | --- | --- |
| P1 | Existing attachment validates and copies under `BEGIN IMMEDIATE`, and its management handler calls it synchronously. | Stage/validate asynchronously outside the write transaction; use a short admission/publication commit with crash-safe cleanup. Slow-copy tests must leave management and unrelated writes responsive. | All three; supportive S1, neutral N2, adversarial attachment finding. |
| P1 | Preparation invokes external `uv` and managed Python; the distributed app does not bundle that resolver. | Select a pinned, licensed preparation toolchain and cold acquisition policy. Prepare from the distributed app without development PATH, checkout or managed-Python cache. | Neutral N1. |
| P1 | The reused builder creates a generic license inventory; the successful detector needed separately supplemented FlatBuffers/HTTPX notices. | Provide pinned notice artifacts or a declared resolver, and verify every distribution's retained notices. Missing/tampered supplements must fail precisely. | Supportive S2; neutral coverage assessment. |
| P1 | The plan says executable workers cannot obtain owner credentials while explicitly retaining same-user execution. | State the supported coordinator/service boundary accurately. Do not delegate owner capabilities; trust same-user code to respect the boundary unless enforceable isolation is separately implemented and verified. | Adversarial authority finding. |
| P1 | Job/import/grant/recipe checks span separate state domains; repeated checks alone cannot enforce a cancellation/revocation winner. | Define an authoritative atomic admission/invalidation mechanism and recovery semantics. Barrier tests must show invalidation prevents a late commit when it wins. | Adversarial admission finding; supportive S3 and neutral N2 support transition clarity. |
| P2 | The first extension witness and executable-worker release scope are ambiguous. | Name an initial capability matrix and an independently authored declarative recipe witness. Keep executable enablement conditional on its ownership gate. Preserve the zero-host-edit workflow. | Supportive S4; neutral N3. |
| P2 | “Exactly one compatible recipe” could be interpreted to invalidate an explicitly pinned descriptor when another variant is added. | Apply unique selection to discovery. Validate the explicitly selected immutable recipe independently; test overlapping variants. | Neutral N4. |
| P2 | Worker messages, transitions, reservations and evidence compatibility are still prose contracts. | Publish versioned JSON examples, a transition/admission table, message/resource limits, fake runner and conformance tests. Bind admission evidence to applicable runner/verifier/policy identities. | Supportive S3/S6; neutral N5; adversarial evidence finding. |
| P2 | “Deterministic” does not specify whether package bytes must be reproducible. | Define automatic preparation, byte reproducibility, replay safety and model-output determinism separately. Use clean rebuild comparisons only for the guarantees claimed. | Supportive S5; adversarial determinism finding. |
| P2 | The graph omits registry dependencies for CLI/LLM release gates. | Add those edges and require common registry/authority/verifier foundations for every released recipe family. Research/spikes can still advance concurrently. | Neutral N5. |

## Execution recommendation

Freeze a small runnable contract packet around the P1 admission and authority decisions. It should include descriptor/bundle/job/receipt examples, worker framing and error contracts, transition/reservation rules, a fake runner, and shared acceptance fixtures. Assign central import/authority edits to the integration owner; distribute toolchain, notices, registry, UI and family work against that packet in parallel.

Use an independently authored declarative recipe for the first extension witness, and state whether executable-worker support is deferred. Continue CLI topology, generic-LLM and agent-backend spikes without treating their research progress as release admission. Verify Python and existing MCP separately, then the exact CPU detector with its maximum accepted input limits measured.

The plan already addresses several tempting criticisms: static inspection executes no source; an agent cannot promote its own recipe through supported services; local hashes do not prove publisher trust; runtime attestations avoid a package-digest cycle; and small negative detector inputs do not establish recognition accuracy. Preserve those safeguards.

## Validation and limits

This review used source inspection and the recorded baseline. No imported code, model inference, runtime tests, credential files or production inputs were executed/read. Source checks confirmed the attachment transaction, external toolchain, license inventory, owner/scoped-credential behavior and separate transaction domains at the reviewed commit. This evidence establishes implementation constraints; it does not prove proposed preparation features or an exploit.

The three original review texts are preserved in the linked reports, with source references converted to immutable repository links and a private memory-context note omitted from the published supportive report. Findings have not been silently applied to the plan. Runtime implementation has not started.
