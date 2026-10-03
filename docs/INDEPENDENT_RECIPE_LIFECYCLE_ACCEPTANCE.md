# Independent recipe lifecycle checkpoint

This development checkpoint exercises an independently scaffolded
`resolved-python-v1` recipe through actual owner routes and the native generated
form, resolution, review and preparation workflow. The harness uses a relocated
sealed host and the supplied native executable. It performs no model or ONNX
inference. Production automatic execution remains disabled.

The implementation is additive: [the driver](../scripts/verify_phase3_recipe_lifecycle.py)
and [its ownership checks](../tests/test_recipe_lifecycle_acceptance_harness.py)
require no backend dispatcher, import-store, component, policy or native UI
changes. Source fixtures contain a synthetic callable and license; they supply
no preparation descriptor, dependency lock or separate tool schema.

## Scheduling boundaries

Only the trusted development `PreparationRunner` factory selects a barrier.
It explicitly admits the selected bundled `resolved-python-v1@1.0.0` reference
key, including its digest. Enabling the runner alone does not admit that family.
No request, environment switch or recipe/source content selects the factory.
The ordinary bundled CLI is tested separately and creates no preparation or
resolution job when execution is unavailable.

Two inherited private pipes carry a fixed barrier notification and release
byte. The parent retains exact descriptors and held `Popen` objects, joins every
child and never discovers or signals another PID. Actual builder, verifier,
native-owner and attachment operations run unchanged. A verification barrier
returns the original independent report after release; a staging barrier
returns the original stage after its real copy completes.

| Case | Fixed boundary | Owner action and required result |
| --- | --- | --- |
| Disabled queued start | Before acquiring the runner semaphore, snapshotting or reservation acquisition | Disable the exact recipe version. The queued job fails with `recipe_disabled`; no workspace, command, fixture check, native ownership receipt, verification publication, package digest or attachment is created. |
| Revoke during verification | After the actual verifier returns, before report publication | Revoke the bound version. The job fails with `recipe_revoked`, joins actual native ownership and publishes no verification or attachment. |
| Revoke during attachment | After the actual staging copy, before result admission | Revoke the bound version. The job fails with `recipe_revoked`; the stage is discarded and no attachment is admitted. The independently verified package digest may already be recorded. |
| Update an active job | After actual verification, before publication | Add and enable immutable `1.0.1`, then disable `1.0.0`. The admitted original job succeeds and attaches with its original `1.0.0` digest. Both registry records survive restart with their exact evidence. |
| Cancel native ownership | After an actual native launch acknowledges its held handle, before the builder consumes it | Confirm one pending ownership receipt and record whether the held process was live at acknowledgment. Send cancellation through the owner route, observe durable `cancel_requested` or `canceled`, then release. The job becomes `canceled`; actual ownership, reservation and workspace settle before restart. |
| Host crash with withheld proof | At the same acknowledged live native boundary | Withhold the exact private receipt inode through directory descriptors, kill the held host and join the held native client, then wait for actual native completion proof. Restart while that proof is missing: the interrupted job retains its workspace, ownership record and reservation; consumed-plan replay and a fresh reviewed replacement start both fail. Restore the exact proved inode and hash; the actual job-specific recovery route settles ownership without replay, verification publication or attachment. |
| Stale import revision | After actual verification, before publication | Revise the import through its owner route. The old job fails with `stale_import_revision`; its result does not attach to the newer revision. |
| Stale source snapshot | While queued, before snapshot execution admission | Mutate the exact materialized source file identified by this case's owned draft. Releasing the queue yields `source_identity_mismatch`, zero commands and no verification or attachment. The original external source remains unchanged. |
| Unavailable capability | Before enablement | Add a valid closed recipe bundle with one unavailable capability. Enablement rejects `recipe_capability_unavailable`; zero preparation jobs and ownership receipts exist. The proposed record and blocker survive restart. |

Every lifecycle case removes its original external source and authoring bundle
before a normal host restart. The driver then reads the actual stored job and
recipe records and checks their exact bindings, terminal state, error code,
recipe digest, state, epoch, evidence and any native receipt hash. Verification,
package/receipt digests and progress must also remain equal across restart. It requires zero pending
preparation ownership receipts, zero preparation reservation files and removal
of any job workspace. The capability case checks persisted proposed metadata
and the same blocker after restart. Both native cancellation and host-crash
cases require the held native process to have been live at acknowledgment.
The actual owner import route is read after the final normal restart as well:
its revision, source identity and attachment must equal the pre-restart values.
This includes absence of a late attachment after crash proof restoration.

The host-crash case intentionally joins the held native UI process after killing
the host. It claims no final UI receipt for that interrupted flow: its receipt
size is zero and hash is null. Its independent evidence is the actual native
completion frame, exact restored inode and hash, persisted job binding, owner
route admission failures and subsequent recovery/restart checks. Receipt bytes,
instance nonce and credentials are never reported. Restoration uses an exclusive
FD-relative hard link followed by unlink, so it cannot overwrite a replacement
at the original path. The final proof has one link and the same inode and bytes.

## Reproduction and evidence

Run with a Python 3.12 parent and absolute paths to a sealed host and native
binary:

```sh
uv run --python 3.12 python scripts/verify_phase3_recipe_lifecycle.py \
  --host /absolute/path/to/sealed-host \
  --native /absolute/path/to/McpmeMenuBar \
  --output /absolute/path/to/new-receipt.json
```

`--case` selects one or more named cases. Output files are created exclusively
with mode `0600`; an existing receipt is never overwritten. The driver checks
every entry in the selected host's artifact manifest, its clean source commit,
the native executable hash and imported module origins. It records its own
source and trusted factory hashes. Child environments are minimal and private
routes use the actual owner token over the owned Unix socket.

The [sealed checkpoint receipt](evidence/independent-recipe-lifecycle-6a26f7b.json)
binds the results to source `6a26f7b709fa480067c6b9e8b81330c8c5ce3d9d`, all 1,785
artifact hashes, build manifest SHA-256
`072a8b9af333c2a069469ebf9e78c9db26c22f9c2bda9eb020601062ccd0053c`, and native SHA-256
`562925d19a7f49072562b8bb7a678387b7149a8ac96d9019e27ca2f6fb3ac1c8`.

Reports retain only fixed case, stage, state and error-code values, booleans,
bounded counts, durations, hashes and recipe version metadata. They never
retain source, prompts, tokens, worker output, owner credentials or exception
text. If ownership cannot be proved settled, cleanup retains the isolated state
directory and reports its path instead of erasing recovery evidence.

## Remaining gates

This checkpoint proves the listed real development boundaries. It does not
close the complete A0R or release gate. Still required are other host/helper
crash boundaries and deliberately mismatched receipt retention for this exact
independent-recipe flow; mutations during source copying, resolution and
sealing; every late success/publication winner; remove/deprecate and native
recipe-management controls; and final signed distribution acceptance. The
source-snapshot test changes bytes after review and before execution; it does
not claim a concurrent source-copy experiment.

The cancellation case records liveness at the acknowledged native boundary.
It does not claim that the child is still computing when the owner request
arrives, hard aggregate process or memory bounds, or arbitrary descendant
escape resistance. Those require their own native executor evidence. A normal
restart retaining a bounded terminal receipt does not prove every crash boundary.
The explicit crash case above covers the first acknowledged preparation native
owner with its actual proof withheld and restored; it does not prove helper
crashes, substituted proof identities or every later publication race.
