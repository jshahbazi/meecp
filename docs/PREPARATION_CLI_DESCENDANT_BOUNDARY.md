# macOS CLI descendant-boundary spike

Status: native feasibility evidence, 2026-10-03, macOS 26.7.1 build 25G241 arm64. **A3 remains open.** This checkpoint adds isolated C fixtures and tests. It changes no production launcher, reaper, receipt contract, catalog entry or public capability.

## Concrete result

A Seatbelt Unix-syscall filter on this host can permit ordinary fork and exec while preventing the tested process-group escapes. Deny **all three** of `SYS_setpgid`, `SYS_setsid` and `SYS_posix_spawn`. Denying only the first two is insufficient: both `POSIX_SPAWN_SETPGROUP` and macOS `POSIX_SPAWN_SETSID` escaped in actual native tests.

The syntax comes from the installed Apple profiles `/System/Library/Sandbox/Profiles/airlock.sb` and `dyld-support.sb`, which use `syscall-unix` and `syscall-number`. The SDK defines the three syscall numbers as 82, 147 and 244; fixtures use SDK constants rather than hardcoded values. The candidate rule is:

```scheme
(deny syscall-unix
  (syscall-number SYS_setpgid SYS_setsid SYS_posix_spawn)
  (with no-log))
```

The actual C fixture substitutes numeric constants while composing its profile. This rule is tested both in an otherwise permissive profile, to isolate its effect, and a deny-default profile with explicit fork, executable, package/system-read permissions. Sandbox restrictions survived fork and exec. The restricted profile's supported execution path is `fork` followed by `execve`; programs requiring `posix_spawn` need an adapter or a different verified contract. This is a compatibility condition that must be shown during recipe admission.

Apple's published [`kern_exec.c`](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/kern_exec.c) applies spawn group/session attributes internally, explaining why blocking the standalone syscall entries does not block those spawn paths. Public source is supporting evidence; these results come from the installed OS, not an assumption that its kernel exactly matches the published revision.

## Correcting the existing evidence boundary

The previous CLI foreground fixture invokes `setsid()` after the native reaper has already made that target a session leader. It returns `EPERM` even without a sandbox. The new control proves this for all four profiles. Consequently, that older case establishes no independent sandbox denial of `setsid`. Its fork-denial checks remain separate evidence for the no-fork case.

The new escape checks fork first, then invoke `setsid` or `setpgid` from a nonleader. The plain controls succeed and change group; the syscall-filter cases fail with `EPERM`. Apple documents this leader restriction in [`setsid_internal`](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/kern_prot.c).

## Proposed settlement contract

The current production reaper waits for its held leader, signals the group and publishes a completion receipt. Group signals alone do not establish completed descendant cleanup: the spike repeatedly observed three members immediately after SIGKILL was submitted.

The candidate stronger algorithm is:

1. Create the isolated session in trusted launcher code and apply the verified inherited boundary before any selected code executes. The untrusted target receives no ownership-control or completion capability.
2. Observe leader exit with `waitid(..., WEXITED | WNOWAIT)`. Keep that child unreaped throughout cleanup, preserving the leader PID and group identity.
3. Signal only that held group. After the grace interval, repeat group SIGKILL while checking for quiescence within a finite settlement deadline.
4. Call `proc_listpgrppids(held_leader, two_entry_buffer, sizeof buffer)`. Require **exactly one returned entry equal to the already held leader**. A full two-entry buffer, zero, error, permission failure or any different entry is not success. Discard every other returned identifier; never adopt or individually signal it.
5. Reconfirm held-leader status, then `wait4` that exact child. Publish a new, explicitly stronger group-settled receipt only after all checks pass. An old leader-only completion frame must never satisfy this topology.

The [published kernel implementation](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/proc_info.c) holds the process-list lock across live and zombie lists and filters by group. A two-entry buffer is sufficient to distinguish a sole known leader from any larger group without retaining discovered identities. This is a read-only group-membership assertion, not process discovery for signaling. Correctness also depends on the proved no-escape boundary and isolated session: an empty descendant set cannot grow once its only remaining member is an exited, unreaped leader.

`kill(-group, 0)` is unsuitable as the success test. The local zombie-only group returns `EPERM`; permission denial can have other causes. The fixture uses that only as a diagnostic. It independently observes closure of inherited witness pipes and confirms the leader remains waitable. Pipe EOF itself is not a production completion proof because untrusted descendants can close those descriptors early.

If the membership proof, leader identity or deadline fails, production must retain the job's reservation and pending receipt, report unsettled ownership, and deny automatic retry/adoption. Losing the native owner cannot be repaired by reading a PID from storage. A restart accepts only the new exact-bound receipt after its native owner finishes.

## Measured checks and remaining gates

Run:

```sh
uv run --locked pytest -q tests/test_cli_group_boundary_spike.py
```

The checkpoint passes **38 real native checks**: 24 plain/filter/restricted fork, nonleader session/group, fork-exec and spawn-attribute cases; four leader-EPERM controls; five child/grandchild settlement cases; and five concurrent-fork cleanup cases. All fixture child processes are finite; only directly owned groups receive signals. No discovered identifier is logged or used as a signal target. The settlement fixture applies the inherited syscall boundary before forking and repeatedly kills the pinned group until the exact singleton check succeeds or its deadline expires. Parent fixture cleanup is not a durable production receipt implementation.

Before production admission, independently review and version-pin the complete effect profile, including external signal/Mach/privilege paths, executable identity, inheritance and supported OS builds. Add actual reaper/journal integration with cancellation during fork, host SIGKILL, native-owner loss, restart, deadline/query failure, truncated snapshots and old-receipt rejection. Preserve reservations for every unsettled case.

Aggregate live memory and process-count enforcement also remain open. Allowing fork means a per-process memory limit alone cannot enforce the invocation's total budget. A broker that launches each declared child under an individual native ownership capability is an alternative for cooperative applications and can reserve each child before launch; it is a different protocol requiring its own implementation and compatibility proof. The no-fork checkpoint, this fork-capable spike and that broker proposal must retain distinct acceptance claims.
