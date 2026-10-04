# Agent preparation host integration checkpoint

This additive checkpoint starts from `68a7533ce99098a7540bc75bf3f3f01f497c72a2`. It imports the reviewed provider contracts, OpenAI Responses transport and proposal coordinator from `540421edde905da9b455574d9c7e240d82bb48dd` without changing those three files. It adds a host authority layer, native controls and a checked descriptor-selection split. Live provider execution remains disabled by default. The transport tests use a deterministic `httpx.MockTransport` and a synthetic credential reference; they prove no remote model behavior.

## Authority and routes

`AgentPreparationHost` receives trusted provider configurations keyed by `provider_id`. Each configuration has a concrete backend, an exact finite model list and an opaque credential reference resolved only by that backend's host-supplied resolver. Requests cannot supply credentials, credential references or destinations. The current concrete backend is `openai-responses-v1` at `https://api.openai.com/v1/responses`; this checkpoint does not register GLM, DeepSeek, Bedrock, local model or CLI providers.

The root-owned Supervisor/native registration patch is in `docs/patches/agent-preparation-registration.patch`. It wraps the existing authenticated `LocalPolicy` and uses the same Unix-socket transport, Host/Origin checks and owner principal. Existing preparation grants receive `grant_scope` on every new route. No environment or request field enables execution.

| Route | Closed request | Effect |
| --- | --- | --- |
| `POST /v1/agent-preparations/plans` | Import ID and exact revision | Static configured-provider, enabled-recipe and captured-file metadata; no transfer |
| Same route | Exact import/revision, enabled recipe reference, configured provider/model, sorted selected paths, bounded limits | Hash and UTF-8 check the selected captured bytes; issue a reviewed scope plan |
| `POST /v1/agent-preparations/start` | `action=analyze`, host plan ID/digest, idempotency UUID | Consume one reviewed authority; capture exact selected bytes and run one proposal attempt |
| Same route | `action=adopt-candidate`, job ID and exact proposal digest | Independently validate and select a supported declarative candidate; return the advanced draft for ordinary preparation review |
| `GET /v1/agent-preparations/jobs/<id>` | No body | Bounded metadata, durable usage reservations and a cached candidate digest; no raw proposal text |
| `POST /v1/agent-preparations/jobs/<id>/cancel` | Empty object | Request cancellation and join all owned local I/O before reporting settlement |

Each plan binds the exact draft, source metadata digest/revision, intent digest through the request digest, recipe ID/version/digest/epoch, provider configuration digest, model, selected file hashes/sizes, limits, expiry and one authority UUID. Each consumed plan binds one host job and one coordinator attempt/generation. The host rechecks these bindings before dispatch, after replies, before publication and at candidate adoption. An interrupted attempt is never replayed automatically. Uncertain remote usage stays reserved in the attempt journal.

The selected source limit is 32 sorted unique UTF-8 files, each 16 KiB and at most 128 KiB total. Reads use held no-follow directory/file descriptors, reject special files and credentials detected by the existing source scanner, compare private captured membership before and after, and re-read the selected bytes/identities. The host stores metadata-only plans/jobs. Typed bounded proposal artifacts occupy the separate attempt artifact table; raw provider response bodies, source, intent/prompts and credentials are not journaled.

## Candidate and native behavior

`AgentPreparationView` provides configured provider/model and enabled-recipe pickers, captured-file checkboxes, transfer/output/deadline controls, explicit transfer review, send, cancel, retry status and candidate selection. The scope review displays the exact files, hashes, bytes, destination, provider/model and recipe. It states four calls with at most three parallel, cumulative serialized transfer bytes, per-call output-token ceiling and deadline. It explicitly states that a dollar ceiling is not enforced and remote usage after cancellation is unknown. No provider compatibility, dollar cost or zero-retention guarantee is inferred from `store=false` and `background=false`; those request flags match the current [Responses create contract](https://developers.openai.com/api/reference/cli/resources/responses/methods/create) and [Structured Outputs shape](https://developers.openai.com/api/docs/guides/structured-outputs).

An uncertain start keeps the same idempotency key and blocks navigation. A known job remains cancellable when parent revision/connection state changes. Adoption requires a settled successful job, current parent context and an independently validated candidate digest; the returned parent must advance exactly one revision with the same source identity. The normal preparation questions and plan remain the execution review.

The current capability is descriptor/fixture analysis for source that existing trusted components and generated form providers accept. Agent options, planning, dispatch and adoption require an unprepared draft; an actual attachment appearing after analysis blocks adoption and preserves that artifact. All roles must have resolved requirements, and the adapter must provide exactly one `proposed/adapter/preparation.json`. Proposed source code in any role blocks adoption. The proposed descriptor must pass the existing component/source/recipe checks and bind the real static candidate, plus the actual resolution proof when applicable. Provider artifacts never confer execution, recipe enablement, attachment, installation or a `VerificationReport`.

Repairing an unsupported signature or writing a new adapter remains a concrete gap. The next controlled component would need a host-owned derived-source snapshot that binds the original source, reviewed proposal/job/attempt, exact proposed file hashes, allowed destinations and licenses, followed by independent static checks and ordinary build/probe admission. This checkpoint does not execute generated files or quietly substitute them for original source.

## Worker settlement and selection split

Static recipe/source/candidate hashing runs in owned joined threads. Status and options use bounded metadata and cached candidate summaries. Options restore only a job with the current import, revision and source identity; older jobs remain inspectable by their exact ID.

`PreparationStore.prepare_descriptor_selection` hashes and prepares the same validated descriptor, observed draft and resolution proof used by the original method. It produces immutable serialized data minted by that exact store. `commit_descriptor_selection` accepts only the exact registered object, executes the existing metadata recheck, and requires the trusted callback to return the literal `True` inside the imports transaction before mutation. Foreign/replaced objects, false/truthy callbacks, stale revision/recipe state and canceled jobs fail. `cleanup_descriptor_selection` performs the original attachment deletion off-loop with an exact registered commit object. The original `select_descriptor` reuses all three steps. Agent adoption cannot enter this cleanup path with an old attachment because every authority recheck requires that the draft remains unprepared. Shutdown is idempotent and joins held workers under repeated caller cancellation before closing its journal.

Adoption persists its exact candidate/descriptor binding before commit. Cancellation that wins before the transaction cannot commit later; repeated caller cancellation waits for the held worker. A commit winner is persisted before cleanup awaits, and restart recognizes only the exact resulting selection/revision/source/form/descriptor identity. Retrying a successfully committed adoption returns that same draft and never executes a build.

## Evidence and open gates

The focused tests cover actual import/recipe stores, private FD snapshots, real authenticated ASGI routes and the pinned Responses transport with simulated replies. Actual AppKit button handlers are compiled and exercised by `uv run --locked python scripts/verify_agent_preparation_view.py`, including unknown acknowledgments, repeated idempotency keys, parent revision changes, cancellation and exact candidate handoff. This is handler evidence; visual layout and a packaged native socket flow remain separate gates.

The opt-in actual Python pipeline is `tests/test_preparation_agent_pipeline.py`. It creates an ordinary typed Python directory with a license and no authored project/lock/descriptor; the trusted resolver derives dependencies. The simulated provider proposes the exact generated descriptor, the host adopts it, normal generated questions restore it, and the ordinary private runner builds, performs real SDK fixture verification and attaches at the reviewed revision. Source and the original attachment are removed before an independently owned relocated probe. Its numerical receipt labels the provider simulation and source-only integration explicitly.

Run the opt-in case with `MCPME_AGENT_PIPELINE_HOST` set to an externally verified private host and `MCPME_AGENT_PIPELINE_RECEIPT` set to a fresh absolute private receipt path. That host supplies immutable private Python/uv/reaper bytes; new integration code runs from this source checkpoint. This does not prove that the earlier supplied host contains the new modules or native wiring.

Changing the shared `preparations.py` implementation intentionally invalidates the old current TextGen 1.0.5 implementation identity in this branch. Three existing derived-build tests stop at the aggregate default-catalog readiness assertion until the root mints its planned coherent replacement. No published recipe bytes are restamped and the opt-in Python pipeline selects only the exact unchanged current ResolvedPython bundle. Root must rerun the full catalog/regression suite after integration and minting.

Live provider choice, model/schema compatibility, actual remote transfer/usage, generated adapter support, packaged native full flow, installed invocation/restart, adversarial crash and authority races, filesystem/network confinement, hard aggregate resources, signing and the final whole-Phase-3 review remain open. Simulated integration does not close A5.

The final focused source packet passed 138 tests in 8.23 seconds. The actual AppKit handler checker passed; the existing Swift packet passed 18 with 3 opt-in skips. The opted source-only Python pipeline passed in 22.93 seconds against the immutable 68a private host: 4 simulated transport calls, 6 generated fields, 5 real build commands, 1 SDK tool/fixture, exact reviewed/attached revision 2, source and original attachment removal, and a relocated probe with all owners joined. Full membership and all 4,782 host hashes matched before and after execution. The final numerical receipt is `/private/tmp/mcpme-agent-pipeline-receipt-v2.json` (1139 bytes, mode 0600), SHA-256 `6b3cb9ffcbddc96d6ddae6a6e33b99168b665c026f8cc19b650555e161570b8d`. It records zero live provider calls and `a5_scope_complete=false`. Earlier harness failures after successful build/probe came from report-field and historical-task-map assertions; they did not authorize a product waiver.

## Independent review repairs

The initial frozen `14ef656` checkpoint had four reproducible races: a delayed native GET could overwrite a later settled cancel acknowledgment; source bytes could change after the joined hashing worker but before descriptor selection; adoption lacked the shared four-task cap; and shutdown could finish across the recipe-to-source worker handoff. The replacement checkpoint fences native replies by response generation and exact current job identity, shares the same four pending slots across analysis/adoption, and checks current authority before a new source worker can be registered after recipe validation. It preserves unknown start acknowledgments across parent revisions so they remain discoverable and cancellable.

For directory/document source selection, `SelectionSnapshotMetadata` records exact root and directory membership, held no-follow file identities, device/inode, mode/size and nanosecond modification/change times, plus ancestry continuity and private ownership/link requirements. The worker takes this baseline before its existing full byte validation and compares the same baseline afterwards. The actual selection transaction compares that same metadata proof and the final job/provider/recipe/revision authority before mutation; it does not read or hash source bytes or recapture them as a new baseline. Existing captured-model families retain their separate typed capability validation; agent adoption remains restricted to unprepared directory inputs.

The repaired packet passed 150 focused tests in 9.01 seconds and the actual AppKit handler checker. The existing Swift suite again passed 18 with 3 opt-in skips. All four independently authored controls passed on the repaired source: source mutation denied at revision 1/no selection, exactly four adoption workers, settled canceled UI/adoption disabled after a delayed GET, and shutdown with planning finished/no residual worker. Durable tests also cover mixed analysis/adoption capacity; restored source bytes and mtime still fail the ctime fence; file/root replacement, mode changes, unknown files/directories and FIFO substitution fail; and mutation during full validation cannot become a new baseline.

The repaired actual Python pipeline passed in 23.34 seconds with the same source-only/provider-simulated scope and joined ownership. Receipt `/private/tmp/mcpme-agent-pipeline-receipt-v3.json` (1139 bytes, mode 0600), SHA-256 `b77d6b1b274b719e55bf66410f1828153cb3ef0be68d820f2b2832f04af4abe1`, records the exact before/after immutable private host checks and leaves `a5_scope_complete=false`. Earlier receipt versions remain preserved.

Different-author replacement review cleared `06f253f6`: 150 focused checks,
the exact reproduced race controls and actual copied-source AppKit handlers
passed. The reviewer independently reran the source-only pipeline in 23.37
seconds, including real private building, SDK verification, attachment at
revision 2 and relocation after source/attachment removal. All 4,782 frozen
host hashes matched before and after execution. Provider calls remained four
simulated and zero live. Review receipt SHA-256:
`8aad8dd8bf7b5d8ffbb19ed6209a2298fe827fd93990b0583bb194447ff7c4c0`.
The repaired source is integrated as `86ff1a5`.

Root's applied owner-route/native registration passed 96 backend checks,
native release compilation and three UI self-tests. An additional integrated
route regression covers unauthenticated rejection, old scoped grants on all
four agent routes, Host/Origin denial and empty default providers. Independent
parent-navigation review, packaged native socket acceptance, generated adapters
and live-provider acceptance remain open.
