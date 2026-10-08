# Command release admission

The ordinary release loader can supply command execution authority only through
the source-approved `CommandReleasePolicyV1` record. The record contains the exact
`host-command-json-v1` component at version `1.0.1` and separately pinned installed
and measured-preparation platform-policy digests. The initial release policy
keeps the command record null. Final signed native, topology and recovery
acceptance must pass before the integration owner activates it.

The accepted matrix is macOS `26.7.1`, Darwin `25.6.0`, arm64, kernel build
`xnu-12377.161.15.700.19~2/RELEASE_ARM64_T6050`. Both execution implementations
already bind the complete product version, kernel release/build and architecture.
The new gate compares their current digests to the accepted constants. A future
kernel or OS update requires new acceptance and source approval; it does not
inherit command admission merely because its major version remains 26.
After complete artifact and helper verification, an authentic unsupported
platform returns the fixed `command_release_platform_unavailable` code. The
ordinary release loader can then withhold the command family while retaining
other approved families. Invalid artifact, source, helper, signatures or malformed
platform metadata remain admission failures and cannot become that downgrade.

The trusted caller derives the packaged host root from its installed module.
The gate requires that same module location inside the enclosing meecp app. It
checks the complete closed host inventory and current artifact hashes, the exact
existing format-3 and format-4 inventory schemas, original C/header source
hashes, all ten original command component modules, executable permissions, and
the signed native app context. It rejects unsafe links, hardlinks, writable or
foreign-owned members, duplicate JSON keys, unknown fields, changed source,
missing or additional unlisted files, and mismatched build metadata. Held file
and ancestor identities are checked again after signature inspection, so a
same-byte inode replacement cannot pass that boundary.

Signature inspection requires the reviewed Developer ID certificate and Team,
hardened runtime, a secure timestamp, and empty entitlements for the app and both
command helpers. It invokes only bounded system signature inspection commands.
No selected-source or packaged executable runs during admission. The release
loader performs this synchronous work off the event loop. The complete host
build metadata uses the shared bounded artifact parser; ordinary small command
inventories retain their existing contract parser.

Successful admission constructs the existing `HostCommandAdmission` and
`CommandPreparationResourceAdmission` together. The installed helper identity
continues to use format 3; the measured preparation helper uses format 4 and
the fixed trusted `ResourcePolicyV1`. Source, recipe, public request, environment
and user configuration cannot choose paths, helper identities, budgets or a
platform policy. Inventory presence alone produces no authority.

The constructor requires the release loader's already verified
`ReleaseArtifactProof` and checks its original complete artifact baseline before
and after command-specific inspection. It does not reload policy or recapture
replacement files. `ReleasedHostCommands` retains this same proof for installed
execution. Session and invocation entry join complete artifact checks off the
event loop. Existing generation/package admission and result publication also
check the held critical policy, build receipts, native executable and outer app
seal. The existing executor's synchronous `before_native_create` callback checks
that authority and the held helper, inventory and command-module identities
immediately before creating each native process. Cancellation
joins an outstanding proof worker before returning. Revocation keeps existing
executors available for owned cleanup and retains unresolved reservations.

The original command component modules and published recipe `1.0.1` remain
byte-for-byte unchanged. Their existing per-attempt package/authority checks,
native creation fence, ownership receipts, cancellation and recovery remain
mandatory. Unknown ownership retains reservations and cannot authorize replay,
PID adoption or replacement. This constructor does not establish final runtime
or recovery acceptance on its own.

The existing inventories continue to say `production_admission: disabled` and
`configuration_authority: inventory-only`. The monitor's `result_admissible`,
`hard_aggregate_enforced` and `lifetime_coverage_complete` fields remain false.
Measured preparation retains the narrower held-group-plus-helper windowed
footprint contract under the existing same-user trust scope. There is no hard
aggregate quota, complete lifetime accounting or finite overshoot claim.

The focused controls use synthetic artifact metadata and explicit mocked
signature replies to exercise the constructor. They test null policy, unknown
components/platforms, closed inventory/build records, complete membership,
helper/source changes, executable permissions, links, owner/mode checks,
same-byte and restored-byte replacements, ancestor replacement, late additions,
platform changes during inspection, and strict signature/entitlement policy.
They perform no signing, credential access, provider calls or model inference.
Actual admission through this gate belongs to the final signed release candidate.

A read-only control against the frozen A5 signed host verified its complete
inventory, large build manifest, original command component and helper signature
policy. It also found `python/site-packages/.lock` with mode `0666`. The gate
rejects that member. A fresh host producer must remove or normalize this transient
dependency-install lock before final release admission. The frozen signed app
and earlier receipts retain their original bytes and modes.

The new admission and shared release-loader modules enter the complete TextGen
producer closure. Integration must mint a new immutable producer/component and
recipe version after all backend changes are final. Earlier bundles and the
frozen signed runtime asset are preserved rather than restamped.
