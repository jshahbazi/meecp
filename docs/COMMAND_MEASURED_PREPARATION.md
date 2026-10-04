# Command measured preparation acceptance

This checkpoint supplies positive measured verification for owner-selected trusted
native commands. It reconciles the later CLI aggregate-resource release condition
with the original same-user scope in [Phase 3 trust and resource acceptance](PHASE3_TRUST_AND_RESOURCE_SCOPE.md).
The required ownership boundary remains enforced for the supported fork/exec
topology. Independent checkpoint review, final bundled native controls and public
capability enablement remain separate acceptance steps.

## Acceptance contract

The original automatic preparation plan requires bounded command execution,
explicit effects and private closure, actual SDK fixtures, enforceable descendant
ownership, cancellation and crash recovery. Its same-user scope does not promise
a universal allocation-time physical-memory quota. This checkpoint admits measured
fixture results only when exact native settlement, current preparation authority,
package identity, successful exit, complete functional SDK checks and valid sampled
resource windows all agree. Missing or excessive observed usage rejects the result.

The format-4 monitor's `result_admissible`, `hard_aggregate_enforced` and
`lifetime_coverage_complete` claims remain false. The common verification report
uses a separate, narrower measured-workload contract. Its existing
`peak_memory_bytes` field contains the maximum observed sequential-read sum of the
held command group and known helper. `measurement_scope` explicitly identifies
`held-group-plus-helper-windowed-footprint-v1`. This is neither an atomic
simultaneous peak nor complete lifetime accounting. There is no finite overshoot
claim. The host backend is separately reserved; these numbers do not include it.

## Same invocation and durable ownership

The host supplies `CommandPreparationResourceAdmission`, containing the exact
resource helper, its SHA-256, the accepted platform-policy digest and the trusted
resource policy. Neither source, recipe, form nor public request supplies this
capability. The workload footprint threshold is the smaller of that policy and
the descriptor's reserved memory. Process count and observed CPU thresholds are
host policy. Existing format-3 probes remain available and continue reporting
unavailable measured memory as zero, which cannot satisfy verified attachment.

The selected command executes once for each SDK fixture. Its bounded output stays
in memory. The same format-4 invocation supplies its output, exit, metrics and
settlement proof. The preparation owner durably records both installed format-3
helper identity and the separate preparation format-4 helper and resource-policy
identities, together with job, attempt, fixture, candidate and invocation spec
digests. The verifier re-reads each completion and matches it to that invocation
before publishing content-free evidence. There is no measurement replay.

Strict metadata variants select separate `command-probe-v3` and
`command-probe-v4` directories and journal versions. Recovery acquires the existing
writer lock and validates every intent, policy, helper, spec and exact completion.
Missing, malformed, cross-format or inconsistent proof retains ownership and
reservations. Recovery remains available with new execution admission disabled.
Neither elapsed time nor discovered process IDs clear ownership.

The owner captures a fixed metadata baseline before the original full byte
validation. It includes every locked file, manifest, lock, internal directory,
ancestor and helper. Full internal metadata includes ctime so restoring bytes
and mtime does not restore the baseline. External ancestors use stable directory
identity. This same baseline is checked after each joined full validation and
immediately before native helper creation. That final synchronous callback also
checks current job binding, registered attempt, fixture, session and deadline.
It performs no package-byte hashing. A rejection after durable invocation setup
publishes the existing proven-nonlaunch completion and settles its reservation.
An unsupported asynchronous callback is rejected and its coroutine is closed or
its task canceled and joined; only literal `True` permits native creation.

## Native ownership and exit sampling

The helper creates the original leader's session before applying inherited
deny-default confinement. Fork is supported. `setsid`, `setpgid` and
`posix_spawn` are denied, and exec is restricted to the selected executable.
Command children do not inherit control, spec, completion or directory descriptors.
The helper holds the original leader with `waitid(WNOWAIT)` until its process
group contains only that held leader. Cancellation, owner death, deadline,
observed overload and invalid measurement enter the same TERM/KILL settlement
path. Completion publishes only after settlement; helper loss retains reservation.

Actual repeated probes exposed a leader-exit race: BSD visibility can disappear
before `waitid` becomes ready. The revised helper policy,
`macos26-arm64-monitored-fork-exec-v2`, permits a bounded wait for that original
direct child's exit proof within the existing read-window budget. Only after a
fresh `WNOWAIT` proof may cached terminal usage be read, and its birth must already
have been observed with a stable nonzero terminal timestamp. No enumerated
descendant receives this exception. Changed membership and unavailable descendant
metrics still invalidate the attempt and trigger cleanup. Conforming workloads
can therefore fail conservative verification during unstable observed windows.

The helper executable, source and sampler-header hashes change in the next host
inventory; receipt format 4 and its stronger false flags are preserved. Existing
format-3 installed execution and its receipt bytes are unchanged. Installed apps
retain their existing declared-budget admission and native ownership controls;
the new reactive monitor is scoped to preparation verification.

## Checkpoint verification

Actual fixtures cover a command with two simultaneously allocating descendants,
positive SDK verification, memory/process overload, cancellation with retained
reservations, exact metadata/receipt corruption, and host SIGKILL followed by
reopened SQLite recovery. A deterministic native control reproduces the old
leader race and verifies the new held-exit path. Existing native fault controls
continue rejecting unsupported policy, unknown identities, failed metrics,
membership changes, counter overflow, insufficient storage, missed intervals and
read-window excess.

The managed path captures an application directory, removes the original source,
resolves ordinary questions, builds, probes and attaches. It then relocates the
attached closure, removes preparation state, installs through a real Supervisor,
removes the relocated input and calls the installed app over HTTP and stdio MCP.
This source-level test controls distribution discovery only. It does not replace
the final signed application and native UI acceptance packet.
