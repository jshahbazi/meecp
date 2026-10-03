# Confined command ownership v2

Status: isolated implementation for independent review on macOS 26 arm64. No recipe, catalog, package builder, existing `ProcessOwner`, v1 receipt reader or installed application uses this API yet. **A3 remains open**, including aggregate process-count and memory enforcement and installed-package integration.

## API and admission

`backend/mcpme/command_execution_v2.py` exposes `CommandExecutorV2`, immutable `CommandSpecV2`, `CommandResultV2` and sanitized `CommandExecutionError`. Compile `scripts/mcpme-command-reap.c` as a separate helper and pass its exact SHA-256 when constructing the executor. The helper must be an owner-owned regular executable, without group/other write access or hard links. No fallback to the existing reaper is permitted.

The constructor requires a host `admit(spec) -> bool` callback. It must verify current preparation/runtime authority, an immutable verified package, the complete package digest, exact executable and supported effect declaration. Passing a digest string is a binding, not package validation. The host invokes admission before launch and again before returning output. Exceptions and revocation suppress output. The callback must be bounded and synchronous; it runs inside the serialized execution lane.

```python
executor = CommandExecutorV2(helper, pinned_helper_sha256, private_state,
                             admit=verify_current_admitted_package)
result = await executor.execute(command_spec, bounded_input_bytes)
await executor.close()
```

The spec fixes the package root, exact executable, working directory, immutable argument tuple, package digest, input/output byte ceilings, invocation deadline, TERM grace, settlement deadline and reservation units. Executable and working directory must resolve inside that package. Arguments never pass through a shell. The target environment is empty. Raw input and output remain transient. The bounded binary execution spec uses an anonymous temporary descriptor, unlinked before launch, and is closed in the target. There are no prompt, response, output or argument logs.

Only the target's standard input/output/error descriptors survive exec. Standard error goes to `/dev/null`. The receipt, journal lock, host control channel and spec descriptor never reach selected code. A target may fork and exec the same pinned executable. Other executables and `posix_spawn` are denied. The profile admits package and named system reads; filesystem mutation, network connections, external signals and undeclared operations are denied. The admission review must check that these exact permissions suit the selected application. This implementation does not broaden a package's declared effects or enable general shell commands.

## Native ownership and receipt

The host durably creates the pending receipt and journal reservation before starting the native owner. It holds a private journal lock for the executor lifetime. The native owner watches both its actual parent relationship and a control pipe. Host cancellation, control closure, owner death or its own invocation deadline initiates bounded cleanup.

Trusted child setup creates the isolated session, closes unrelated descriptors and installs the inherited Seatbelt profile before selected code executes. The profile denies `setpgid`, `setsid` and `posix_spawn` syscall entries. The third denial is necessary because both spawn group/session attributes bypass the first two entries. Fork and exec remain allowed. This policy is independently motivated by [the native feasibility packet](PREPARATION_CLI_DESCENDANT_BOUNDARY.md); it is a separately admitted topology.

The helper observes leader exit using `waitid` with `WNOWAIT`, retaining the exact child identity. It repeats signals only to that held group and queries `proc_listpgrppids` using a fixed two-entry buffer. Exactly one returned entry equal to the held leader is required. Other identifiers are discarded and never logged or signaled individually. Zero, errors and a foreign singleton permanently poison the current proof; a full buffer remains unsettled until a later singleton or the deadline. After reconfirming held exit, the helper waits for the exact leader and writes completion. A readiness re-read after exit closes the race where the session marker arrives between the first nonblocking read and `waitid`.

The fixed 301-byte private receipt begins with `MCPMECR2` and binds a random instance, random nonce, helper SHA-256, policy/platform digest and full command-spec digest. The policy digest includes the fixed policy version, OS product version, kernel release/version and architecture. A `C` marker records proved group settlement and an encoded target exit status. A separate `N` marker records proved nonlaunch, either because host `Popen` never succeeded or because the native owner validated its receipt and never called a successful fork. The status is synced before completion; completion is then synced. The helper invalidates the marker after a sync error where writing remains possible; the active host also records an observed receipt-error exit as a permanent fail-closed journal condition, even if a positive marker remains visible. Version-1, truncated, extended, mismatched or nonprivate receipts cannot settle this journal.

Membership proof failure, native-owner death, receipt failure or settlement timeout leaves the reservation pending. Cancellation joins the native helper within the declared cleanup budget plus a bounded startup margin; if that join cannot finish, the exact existing helper handle remains retained for later reaping, the receipt stays pending and new execution is blocked. No helper is killed to make an unsettled invocation appear joined.

Restart only validates exact private receipts. It never discovers processes, reads a PID from the journal, adopts a service or signals a recovered PID. A native owner that survives host SIGKILL can finish cleanup and write its receipt; a new host can then release the old reservation. If the native owner dies first, the reservation remains pending even when a synthetic fixture later exits on its own.

## Validation and remaining scope

Run `uv run --locked pytest -q tests/test_command_execution_v2.py`. The real native cases cover fork/exec inheritance, nonleader group/session escapes, both spawn attributes, child/grandchild cleanup, concurrent forks, descriptor and environment closure, denied file/network/signal effects, timeout/output limits, repeated cancellation, cancellation during actual forking, host SIGKILL before and during forking, native-owner death, receipt-only restart, old/foreign/truncated receipts, authority revocation and journal-before-launch ordering. Test-only compiled variants force query errors, zero, foreign singleton, full-buffer timeout, receipt-write failure and missed initial readiness observation. Production code has no fault-injection switches.

Reservations are accounting units, not measured or enforced aggregate resource consumption. Per-process limits cannot establish a whole-tree memory or process-count bound. Production admission also needs independent complete-profile review, supported-OS validation, package/executable sealing, cancellation/settlement integration with the host's reservation ledger, receipt-format review and installed artifact verification. The current platform gate permits macOS 26 arm64; measured evidence is from macOS 26.7.1 build 25G241. No claim is made for other OS builds until verified.
