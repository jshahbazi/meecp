# Bounded renderer pause proposal

This is reviewable code for a proposed authorized retry. No renderer has been paused by this workstream. Another benchmark window must wait for coordinated local model/build work to drain.

`scripts/launch_jev_sol_retry.py` invokes the existing `monitor_jev_sol_retry.py`, `benchmark_jev_sol_retry.py`, acceptance result and acceptance audit unchanged. The 200% CPU guard remains unchanged. It requires a fresh benchmark directory and creates an adjacent private `-launcher` directory containing content-free progress and scalar receipts. The existing monitor reports its own zero suspensions; the launcher receipt separately records the requested external renderer pause.

Suspension requires `--suspend-renderer`, an explicit PID, exact expected `ps` start time and full executable path. The path is resolved through macOS `proc_pidpath`; renderer processes can rewrite `ps comm`. A dry run verifies these without creating files, launching children or sending signals. The target must belong to the current UID, match the Codex renderer bundle path and `--type=renderer` role, be at least 60 seconds old, and have an initially running state. The installed ChatGPT app's versioned Codex Framework renderer is accepted alongside the standalone Codex renderer. Mismatches, already stopped processes and stale identities are rejected.

An independent detached watchdog verifies identity and the monotonic deadline again immediately before `SIGSTOP`. It watches a control pipe whose only writer belongs to the launcher, then checks the same identity before sending `SIGCONT` on normal release, launcher pipe closure after exit/crash, or its deadline. `--max-seconds` cannot exceed 1200 and reserves ten seconds for restoration observation; the default benchmark/watchdog wait budget is 1190 seconds. The identity check normally uses three `ps` reads with two-second timeouts. This reserve targets a total pause within twenty minutes; OS scheduling and system calls prevent an absolute restoration-time guarantee. Launcher timeout sends termination only to its owned monitor, allowing the monitor's existing worker/reaper cleanup to execute. Launcher finally also releases the watchdog. Its receipt keeps `monitor_exit` and `launcher_exit` separate.

The review command has this shape, with live verified values supplied by the owner:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/launch_jev_sol_retry.py \
  --output test-artifacts/NEW-FRESH-OUTPUT \
  --max-seconds 1200 \
  --suspend-renderer \
  --renderer-pid VERIFIED_PID \
  --expected-starttime 'VERIFIED_PS_LSTART' \
  --expected-executable 'VERIFIED_FULL_CODEX_RENDERER_EXECUTABLE' \
  --dry-run
```

After explicit authorization, remove only `--dry-run` from the reviewed command. The stopped renderer can freeze the app UI across Codex and ChatGPT windows, including other chats.

## Practical limits

- Identity inspection and `kill(2)` are separate system calls. A check-to-signal race remains; there is no atomic conditional signal guarantee.
- The watchdog protects against loss of its launcher, including `SIGKILL`, through pipe closure and its own deadline. Watchdog termination, machine shutdown, OS failure, failed observation or failed signaling can prevent resumption. No software guarantee of restoration is claimed.
- A changed or unavailable identity prevents `SIGCONT`. This protects a reused PID, and can leave a still-stopped renderer requiring manual recovery if identity observation fails.
- A launcher killed abruptly can leave the owned benchmark monitor running. The watchdog restores only the renderer. Normal termination and timeout use the existing monitor cleanup; a stuck monitor is reported as unsettled.
- Process sampling still does not establish continuous GPU/thermal isolation. This proposal supplies no new performance result or production acceptance.

All 38 focused tests passed with process observation, subprocesses, signals and timing mocked. A live CLI dry run verified the proposed renderer's identity and fresh output destination without sending signals or creating children. Independent Sol re-review found no remaining blocker to presenting this proposal. Actual suspension and restoration remain untested; human authorization is pending.
