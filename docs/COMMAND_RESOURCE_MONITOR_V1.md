# Native command resource monitor, version 1

This monitor implements measured accounting and cancellation for the existing confined fork/exec topology. Its stronger result-admission, lifetime and hard-enforcement flags remain false. The later [measured preparation contract](COMMAND_MEASURED_PREPARATION.md) permits a trusted caller to retain bounded output transiently and independently accept a narrower measured workload after actual SDK and exact ownership checks. The monitor establishes no kernel aggregate physical-memory/process ceiling. Final signed/relocated integration remains an open gate.

## Protocol and authority

The new resource helper, executor and policy use a separate spec/receipt format 4, journal and helper identity. Existing `meecp-command-reap.c`, `CommandExecutorV2`, format-3 bytes and admission behavior remain unchanged. The exact current OS product version, kernel release/build, architecture, helper bytes, confinement-policy version, command identity and resource-policy digest are bound. A trusted host constructor supplies `ResourcePolicyV1`; no package, source, recipe or public API can choose it.

Construction also requires a trusted `expected_platform_policy` digest and rejects disagreement with the actual current identity before opening state or launching a helper. The trusted host admission callback remains required. Supplying the current digest in development tests only binds those tests; it supplies no release acceptance evidence. This API has no public registration.

The policy bounds the observed footprint including helper overhead, group membership, cumulative observed own CPU, sample interval, maximum read window/gap, identity table and sample count. Group-member counts conservatively include zombies. The scope is the held target group plus the known helper; the backend's own memory remains separately reserved.

## Measurement and control

Each pass enumerates only the held leader's group using a fixed maximum-plus-one buffer. It reads process usage and birth/group metadata, rechecks identity around usage reads, then repeats membership enumeration. Duplicate/foreign identities, missing leader, truncated enumeration, failed reads, changed membership/identity, decreasing CPU counters, arithmetic overflow, exceeded read window/gap or bounded storage exhaustion latch a fixed failure reason. No telemetry-derived PID is signaled or adopted.

The source waits behind an anonymous native gate until the first valid held-leader sample. On this tested Darwin build, BSD-info queries reject zombies while cached rusage remains available. Only the original leader with a current `waitid(WNOWAIT)` exit proof may use cached terminal usage without BSD-info: its birth must match a previously observed identity and its exit timestamp must be nonzero and stable. Unknown or enumerated descendants never receive that exception. Missing terminal information remains an invalid window; it does not weaken settlement proof or manufacture lifetime coverage.

Footprint sums are windowed sums across sequential reads, not an atomic simultaneous total. The report records maxima of these windowed sums, helper footprint, group count, valid/attempted passes and longest read window/gap. A threshold breach or invalid pass enters the native owner's held-leader TERM/KILL and exact group-settlement path. It does not merely stop sampling.

Cumulative observed CPU retains a maximum own counter per birth identity and sums checked increments. Child roll-up fields are excluded to avoid counting a child's usage twice. Mach counters are converted using the actual timebase with checked 128-bit arithmetic. Disappeared identities retain their last observed usage; this is a lower-bound observation, not complete lifetime CPU accounting.

Stable windows cannot prove that no short-lived descendant or allocation peak occurred between them. `lifetime_coverage_complete` and `hard_aggregate_enforced` are therefore always false. `sample_windows_valid` describes only the checks performed on observed windows. There is no finite overshoot claim. Unobserved short-lived descendants are tested explicitly and never reported as fully accounted.

## Settlement and publication

The helper retains the original leader with `waitid(...WNOWAIT)` through group settlement. It publishes the final fixed-size numeric report together with the exact settlement receipt only after payload/mode fsync, closing writable completion descriptors and FD-relative no-replace publication. There is no later fallible payload operation that can invalidate a visible proof. Missing directory entries after a crash retain ownership; payload durability and directory-entry persistence are distinct claims.

An exit or threshold race cannot turn incomplete telemetry into an admissible result. Helper loss, missing proof, changed policy, malformed completion/proof metrics or an unsettled group preserve pending ownership/reservations. A sampler failure enters termination; ownership may settle only after exact native settlement proof, even though the measured window remains invalid. Recovery requires exact format-4 proof and never adopts processes or replays source. Old format-3 readers cannot certify this contract. The executor discards command output after bounded drainage and exposes only a content-free attempt report, fixed exit/reason values and settlement status. No common `VerificationReport` or store contract is changed by this checkpoint.

## Primary-source grounding

The selected Xcode SDK declares `proc_listpgrppids`, `proc_pid_rusage`, the process-group/birth fields in `proc_bsdinfo`, and separate own/child counters plus physical footprint/start time in `rusage_info_v1`. Published [XNU usage collection](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/kern_resource.c#L3204-L3277) reads cached zombie usage or live task usage and keeps child roll-up fields separate. Its [task accounting](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/task.c#L6387-L6392) supplies Mach-time own counters; [task usage collection](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/bsd_kern.c#L1186-L1219) exposes them with footprint. These source observations inform the implementation; they do not establish that the published XNU revision is identical to the running kernel. Actual small fixtures and exact platform binding are required.

Custom Seatbelt syscall policy remains exact-platform experimental enforcement. This checkpoint does not establish a supported App Sandbox or hard aggregate resource boundary. It does not declare such a boundary impossible.

## Development checks

Small generated native fixtures exercise three simultaneously live allocations, footprint/process/CPU thresholds, bounded short-lived descendants, conversion against `CLOCK_PROCESS_CPUTIME_ID`, overflow rejection, invalid metric/identity/membership/windows/gaps/capacity, existing group/exec/effect/FD restrictions, repeated cancellation, helper death, exact-policy rejection and missing/old/stronger-claim receipts. A held-host SIGKILL during slow final payload sync verifies that no proof is visible early; both later successful publication and permanent sync failure retain the correct recovery behavior. No models or GPU workloads run in this checkpoint.

The helper-loss fixture has its own short alarm solely to bound test orphan lifetime. Time passing never clears the unresolved journal reservation. Public registration, common attachment reports/stores, existing format-3 implementation and published recipe/helper identities remain unchanged.

## Bundled inventory

The host builder separately compiles, ad hoc signs and verifies `bin/meecp-command-resource-reap`. Its inert `command-resource-execution.json` binds exact helper source, sampler header and signed executable hashes; the full host artifact inventory binds both executable and metadata. All lifetime, hard enforcement and result-admission flags are fixed false, and production admission remains disabled. The app signing path refreshes only the executable hash before recomputing the full host inventory and sealing Resources. Source/header identities and closed gates remain unchanged. Packaging does not construct an executor, supply trusted platform acceptance, or establish signed native acceptance.
