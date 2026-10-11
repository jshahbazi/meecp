# Sol retry with sleep-inclusive restoration deadlines

`scripts/launch_jev_sol_retry_awake.py` is an additive, versioned launcher for the authorized bounded renderer pause. The original launcher, monitor, benchmark harness, CPU guard, benchmark protocols, native acceptance results and acceptance audit remain unchanged. This launcher adds an idle-sleep assertion, sleep-inclusive deadlines and a separate power-continuity receipt. Mocked verification is complete; this document supplies no new timing result or live restoration evidence.

The earlier completed twelve-block window in `test-artifacts/jev-sol-bf16-renderer-window-20261011` entered actual clamshell sleep. Its timing is excluded. Native probability acceptance and power/timing eligibility are separate records. A completed harness result does not qualify a sleeping benchmark window.

## Clock and maximum pause

Python `time.monotonic()` and `time.perf_counter()` on this Mac use `mach_absolute_time`, which excludes sleep. The frozen harness continues using its existing performance clock. This launcher uses `mach_continuous_time`, which advances during sleep, to bound the pause and all launcher/watchdog control waits.

The local macOS SDK `usr/include/mach/mach_time.h` declares `uint64_t mach_continuous_time(void)`, `kern_return_t mach_timebase_info(mach_timebase_info_t)`, and a timebase structure containing two `uint32_t` members, `numer` and `denom`. The ctypes declarations match these signatures. Tick-to-nanosecond conversion uses Python integer multiplication followed by integer division: `ticks * numer // denom`. It avoids unsigned overflow and floating-point truncation of large absolute clocks.

`--max-seconds` is an integer in `(10, 1200]`. Launcher and watchdog share one absolute continuous deadline, with ten seconds reserved from that budget for restoration observation. The default work deadline is 1190 continuous seconds from launcher window start. The watchdog validates the deadline and checks it again immediately before `SIGSTOP`, including after the identity recheck. Each selector or monitor wait polls at most one second so that a sleep/wake event cannot leave it waiting out the original active-clock timeout after wake.

This is a deadline for requesting restoration when the OS can execute the watchdog. No userspace process can run or restore a renderer while the machine is asleep. Scheduling, process observation timeouts and failed system calls prevent an absolute real-world restoration guarantee. A machine that sleeps for more than the budget can therefore pass the wall-time bound while no software is executing; the watchdog requests restoration promptly after wake, and that timing window is excluded.

## Owned idle-sleep assertion

Before arming the watchdog or launching the monitor, the launcher owns a child executing `/usr/bin/caffeinate -i -w LAUNCHER_PID`. The local `caffeinate(8)` manual identifies `-i` as prevention of idle system sleep and `-w` as association with the named process until that process exits. No global power settings change. It does not establish that lid closure or explicit sleep is disabled. Physical power/lid conditions still have to keep the machine awake.

Failure to spawn caffeinate, or an observed early exit, fails the launcher. Its PID, observed running status and owned cleanup result are recorded privately. A running child is process-lifecycle evidence; this launcher does not claim to have independently queried the OS assertion registry. Normal cleanup terminates and reaps only the owned child, escalating to a kill of that child if necessary. `-w` also releases its assertion if the launcher exits abruptly.

Renderer identity, native `proc_pidpath`, owner UID, exact start time, renderer role, age and running-state checks are retained. The detached watchdog still rechecks identity before `SIGSTOP` and `SIGCONT`. The launcher releases the control pipe before potentially lengthy owned-monitor cleanup. The unchanged monitor owns benchmark descendant cleanup. SIGINT/SIGTERM execute cleanup; SIGKILL of the launcher can leave the benchmark running, as with the original launcher. The detached watchdog restores only the renderer.

## Predeclared power-continuity criterion

Each process independently records elapsed continuous nanoseconds and elapsed `time.monotonic_ns()` active nanoseconds from its own window start through restoration/cleanup. The accepted clock condition is nonnegative elapsed values and an absolute continuous-minus-active difference of **at most 2,000,000,000 ns**. A larger positive gap detects material machine suspension; a larger negative gap also excludes an anomalous clock comparison. This fixed two-second tolerance covers short sampling offsets. It is not a criterion fitted to a new timing result.

The launcher exits nonzero if either its power-continuity check fails or the watchdog exits nonzero. A material sleep gap excludes the entire new timing window even if the frozen monitor and harness completed. Future performance rows can use the unchanged harness `perf_counter` timings only when the launcher and watchdog both completed successfully and the separate power receipt passes. They still require the frozen benchmark/acceptance criteria. Native probability acceptance remains independently attributable.

## Private receipts and source bindings

The fresh benchmark output has an adjacent mode-0700 `-launcher` directory. Receipts are written to mode-0600 temporary files, fsynced, then atomically published with a create-only hard link. Existing destinations are never replaced.

- `WATCHDOG.json` distinguishes `release`, `disconnect`, `deadline`, and failure/interruption restoration triggers. It records actual resume status, stop attempt, independent clock comparison and watchdog source hash.
- `POWER.json` records the launcher's independent clock comparison, fixed gap threshold, caffeinate PID/lifecycle, `completed` or `excluded` power status, exclusion reason and timing eligibility. It explicitly does not evaluate native probability acceptance.
- `LAUNCHER.json` separates monitor exit from launcher exit, records a fixed authored completion reason, cleanup status and bound-source hashes. Its source bindings include this versioned launcher, the original launcher, unchanged monitor/harness, both frozen protocols and both native acceptance records. The unchanged monitor retains its own downstream artifact bindings.
- `PROGRESS.jsonl` is the unchanged monitor's content-free output in a private file.

No prompt, evidence, probability vector, process arguments, executable identity, start time or exception dump is added to these receipts. Renderer identity is exchanged only over the launcher's private pipes and checked in memory. Identity checking and signaling remain separate system calls, with a check-to-signal race. Failed observation, watchdog loss or OS shutdown can prevent restoration.

## Review command

Use freshly verified renderer identity and a fresh destination:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/launch_jev_sol_retry_awake.py \
  --output test-artifacts/NEW-FRESH-AWAKE-OUTPUT \
  --max-seconds 1200 \
  --suspend-renderer \
  --renderer-pid VERIFIED_PID \
  --expected-starttime 'VERIFIED_PS_LSTART' \
  --expected-executable 'VERIFIED_FULL_CODEX_RENDERER_EXECUTABLE' \
  --dry-run
```

The dry run checks identity, bounds, destination and native clock availability without creating receipts, children or signals. An actual run removes `--dry-run` within the authorized coordinated benchmark window. The renderer pause can freeze the app UI across Codex and ChatGPT windows.

## Focused verification

`tests/test_launch_jev_sol_retry_awake.py` mocks all subprocesses, process observations, signals and runtime clocks. It covers uint64 tick conversion with large uint32 timebase values, post-wake deadlines with a frozen active clock, at-most-one-second selector polling, the predeclared gap threshold, exact identity preservation, actual restoration triggers, create-only receipt publication, caffeinate launch/exit/cleanup failure, monitor timeout cleanup and unchanged monitor arguments. No test launches a model, pauses a renderer or changes system power settings.
