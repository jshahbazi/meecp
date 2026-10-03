# Managed Python and ONNX probe confinement checkpoint

Status: source implementation checkpoint from `68a7533`, 2026-10-03. Public
automatic preparation remains disabled. This integrates the reviewed
[private Python probe plan](PREPARATION_PROBE_CONFINEMENT.md) into managed
ordinary stdio verification. Supported public isolation, hard aggregate
resource ceilings and complete installed/native acceptance remain open.

## Host selection and ownership

`PreparationRunner._verify` selects confinement for its common stdio verifier,
including schema 1 Python recipes, ResolvedPython and ONNX. The trusted
`confine_private_python=True` argument belongs to the host call; it is absent
from recipe descriptors, source metadata, package manifests and public routes.
The verifier validates the sealed candidate against the full selected contract
before capturing `ProbeConfinementV1` in a joined worker. Unknown launcher
bytes fail before native process creation. Only the two exact published Python
and ONNX launchers establish equivalence with the direct private interpreter.

The plan binds package digest, locked bootstrap/interpreter, private probe
workspace and derived home/temp directories, helper path/hash, policy bytes,
sandbox binary and exact current platform. The existing prelaunch and
post-shutdown plan checks remain in effect. The supplied process owner must
use the exact reaper, and `expected_reaper` remains pinned through the owned
native launch task. The actual MCP SDK still checks full tools, schemas,
fixtures and negotiated capability/protocol values. Shutdown precedes final
package and plan revalidation.

Capture, launch validation and final hashing join before cancellation can
release a reservation or delete the workspace. Runner authority monitoring
still cancels verification after revocation or revision changes. The atomic
store attachment winner remains unchanged. A completed failed callback is now
removed from the attempt's task set even when its joined result raises.

Journaled owners also pass a mandatory exact job/attempt admission callback to
the owned native creation task. It checks current revision, recipe authority,
host family admission, cancellation and deadline immediately before creation,
including when no graph recorder is enabled. An incoming trusted probe guard
adds checks and cannot replace that job callback; both require synchronous
`True`.

A joined prelaunch worker captures metadata before repeating full package
hash validation and the actual plan launch check. It requires the same metadata
after successful validation and carries that immutable baseline across later
awaits. The native callback checks all locked files plus manifest/lock,
internal directory membership, reaper/sandbox files, confined directories and
ancestors. Package file changes and ancestor substitutions invalidate the
baseline; files are never recaptured after an await as new admitted bytes.
Outside ancestors use stable inode/mode/owner identity, so unrelated sibling
creation cannot invalidate the package. All byte hashes stay off the event
loop. The guard enforces 20,004 file records and 41,024 directory records as
conservative construction bounds, in addition to the validator's 20,000
artifact and 40,000 tree-node limits.

The trusted async `preparation_verifier.capture_confined_probe_launch` helper
returns the launch tuple and this immutable guard after joining the worker.
Controlled host callers pass `admit=guard.admit` and
`expected_reaper=plan.reaper` to `ProcessOwner.launch`; each plan owns a
separate private probe workspace. Job authority remains the managed owner's
additional mandatory callback.

The optional trusted runner constructor flag `probe_graph_accounting=True`
creates a single `probe` foreground member in an owned joined factory. Its
requested executable is the exact `sandbox-exec` wrapper; confinement binds
the subsequent exec into the private interpreter/bootstrap. The graph binds
the sealed package digest and exact job binding. Its report remains on the
trusted owner and preserves the existing verification/store schema. Default
recording is false. Terminal foreground RSS and missing/nonlaunch/unknown
states are observations, not durable completion proof or aggregate ceilings.

Direct library callers retain the existing default `confine_private_python=False`
and optional typed host plan. A true selection cannot be combined with a
supplied plan. Specialist command and TextGen family callbacks keep their
existing interface and checks. Family source validation, acquisition,
construction, schema 1 identities and immutable recipe/template bytes remain
unchanged. Disable continues to deny a new job while allowing an already
admitted job to finish; revoke invalidates active authority.

Runner/verifier implementation identities change and invalidate stale plans.
ResolvedPython 1.0.1 and ONNX 1.0.0 component digest inputs do not change, so
this checkpoint does not mint replacement bundles. The later coherent
TextGen source closure belongs to the combined integration checkpoint.

## Bounded evidence

The added tests exercise the actual ImportStore/RecipeStore/PreparationStore,
runner authority, native completion receipts, copied private Python runtime,
SDK probe and atomic attachment. Only toolchain discovery and the tiny
synthetic package builder are controlled. Original source removal precedes
the job; both exact launcher variants pass a one-tool, one-fixture probe.
The ONNX launcher fixture runs a tiny wire server and loads no model.

Negative operations deny an external canary read/stat, package write, fork
and session change. Private home writes succeed. Existing policy controls
also cover TCP, UDP, Unix sockets, process groups and spawn attributes.
Unknown launchers create no native process. Fixed barriers after actual plan
capture test bootstrap/runtime mutations, workspace replacement and helper
replacement before launch, all with zero native creations. Capture held in a
worker remains joined under repeated cancellation, active recipe revocation
and import revision changes. Each denied attempt has no attachment, owner,
pending receipt, callback, worker, retained reservation or workspace after
settlement. An active disable case preserves existing recipe semantics.

Additional barriers hold actual native creation after journal registration
while monitor polling is explicitly held. Cancel/revoke/revision deny creation
with recording both disabled and enabled; inflight disable still completes.
The same controls run after the recorder's actual joined executable hash.
Bootstrap, runtime module, locked asset, new root/nested member and probe
directory mutations deny creation at both boundaries. A mutation immediately
after successful worker hashing proves that the baseline is not refreshed.
These controls preserve real store, journal, policy, SDK and native ownership
code; only scheduling is controlled.

Author verification passed 200 selected tests, with 69 opt-in skips and four
memory-named deselections. Three of those static memory checks passed in a
separate invocation; the large allocation control was intentionally excluded
from this small-fixture lane. After extraction of the trusted async guard
helper, all 75 managed admission, confinement, policy and ordinary verifier
checks passed again. Credential scanning passed. All 102 tracked template
files and their executable modes, and the six Python/ONNX component source
files, remain equal to `68a7533`.

The retained mixed-directory Python package from the frozen `341de90` host
passed this working-source host-selected verifier using its unchanged helper:
one tool, one fixture, 69,173,248 foreground RSS bytes against 128 MiB, zero
remaining owners and a removed private workspace. The original source was
absent and full package digest validation preceded and followed execution.
The actual native metadata callback checked 2,741 records in 4.203 ms. The
maximum observed 2 ms heartbeat gap was 17.572 ms during this complete probe;
this is a measured fixture result, not a worst-case latency guarantee.
Receipt SHA-256:
`aa09fe81d670e0bdc9224b49e763ce662581863ad4fe7efdea5a50f9f68fd745`.
That frozen host does not contain this integration; this receipt is a
working-source verifier checkpoint.

## Remaining gates

The policy uses the existing deprecated custom Seatbelt mechanism. Exact
current-build negative controls do not establish a supported public App
Sandbox API or another OS/kernel policy. Production/public preparation stays
disabled. Actual A2 ONNX inference with this integration, a coherent rebuilt
installed host, signed distribution, A4 communication confinement, aggregate
owned-tree physical memory enforcement, CPU/process ceilings and full
lifetime/private-disk accounting remain separate acceptance gates. Native
foreground RSS is a terminal per-process observation, not simultaneous
aggregate memory or a hard kernel ceiling.

The native-creation callback and optional recorder depend on the immutable
`6eabf261` process-graph checkpoint. Author verification of this integration
and independent review of that dependency are separate evidence. Final combined
artifact acceptance remains required. The original trusted selected-app target
and later stronger CLI resource conditions are distinguished in
[the scope reconciliation](PHASE3_TRUST_AND_RESOURCE_SCOPE.md).

## Independent managed checkpoint review

Root independently matched all 16 frozen source/control hashes at `925ae0f`.
The selected common guard and actual native admission suites passed 66 checks
with no skips or failures. Controls pause the scheduled native task or joined
graph hash while monitor polling is held; lost job authority or changed package
and workspace identities result in zero native creations and joined settlement.
The checkpoint is integrated as `c0051b6`.

The combined common/A4 review receipt has SHA-256
`9921872b7eb67b6ef9ecb03339b4fd6b46ac1569451520153647003e02f79b9e`.
Actual current ONNX inference and the final installed artifact remain open.
