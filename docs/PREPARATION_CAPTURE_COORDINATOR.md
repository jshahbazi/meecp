# Durable local artifact capture

`preparation_capture.py` owns asynchronous capture attempts around the immutable
`ArtifactStore`. Capture creates no runnable package, installs no slot and grants
no execution authority. The legacy `artifact_collection` model intake stays
unchanged; the additive source kind is `captured_artifact_collection`.

## Host integration

Construct `ArtifactCaptureStore(imports, artifact_store, storage_policy=...)` and
`ArtifactCaptureCoordinator(store, admit_target=..., validate_target=...,
reserve_resources=..., release_resources=...)`. The store shares ImportStore's
SQLite connection, lock and transaction. The asynchronous coordinator exposes
`start(request, policy, target, idempotency_key)`, `cancel`, `cancel_target`,
`recover` and retryable `close`.

`CreateImportTargetV1` contains name, intent, document role, start choices and
bounded requirements. `AttachImportTargetV1` names an exact import revision.
The synchronous host callbacks run inside an already-open import transaction;
they must mutate only that connection's metadata and must never commit, begin a
nested transaction, copy files or execute source. `admit_target` consumes the
coordinator's verified collection and returns exactly `import_id` and `revision`.
The coordinator acquires the resulting draft reference and saves success before
the same transaction commits. A caller-supplied manifest is not admission proof.

`CapturePolicyV1` bounds disk to 32 GiB plus the manifest allowance, wall time to
3600 seconds and copy memory to a fixed 64 MiB reservation. Captures serialize;
at most four attempts can be pending. The storage policy separately bounds
retained bytes and retention. Shared host resource callbacks aggregate captures
with model and preparation reservations. `recovery_policy` independently binds
the persisted request, target and policy; `unsettled_reservations` returns exact
retained costs. Filesystem free space already reflects committed object bytes.

## Ownership and restart

The durable request, nonce, policy, target and exact staging name precede directory
creation. A private OS lease spans copying, admission and cleanup. ArtifactStore's
host-only `CaptureHooks` persist the held stage identity before writes and the
exact candidate before atomic publication. Ordinary acquisitions accept no hooks
from JSON. Cancellation signals and joins threads, including admission hashes and
cleanup. Disconnecting a start caller still schedules its persisted queued work.

Recovery reads known journal records and validates their private owner files and
inode identities. It never replays an acquisition or adopts a directory found by
scanning. Unproven stage creation, changed identities, unknown tree entries or lost
owner proof retain the reservation and report
`artifact_capture_reconciliation_required`. A complete published object survives
late cancellation; recovery indexes its exact persisted candidate without
attaching it to a canceled or stale draft.

## References and collection

`ArtifactReferenceV1` names draft, job or read owners. Draft reference reconciliation
transfers surviving inputs to the current revision and removes inactive draft
references while preserving job/read pins. Call `_reconcile_draft_references`
inside an import transaction or `reconcile_draft_references` on its own. Workers
must acquire a job reference before consumption and release it only after all
owned work has joined. `open_artifact` adds a durable reader lease while yielding
verified captured bytes; restart releases that reference only after OS ownership
proof. Original selection paths are never worker inputs.

Collection requires no references and an elapsed retention interval. A durable
intent precedes exclusive quarantine of the exact indexed inode. An object-directory
lease serializes deletion, recovery and new publication; the journal is removed
before a new publication can proceed. Partial deletion resumes only from known
intents and allowed names. Unknown objects/stages are preserved. `collect` is
synchronous and must run through host-owned, joined static work.

This checkpoint provides intake ownership and retention. It does not complete A2:
the exact ONNX compatibility validator, runtime/task adapter, installed-host build,
real inference fixtures and maximum-image resource measurements remain required.
