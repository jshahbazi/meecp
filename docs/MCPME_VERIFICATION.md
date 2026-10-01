# mcpme Phase 1 verification

This document records the validation of the native host, deterministic package, and shared JevK5 package. Generated reports contain allowlisted counts, booleans, timings, and identities; they do not contain prompts, responses, credentials, or exception dumps.

## Reproduce

```sh
uv run --locked python -m pytest -q
uv run --locked python scripts/verify_mcpme.py --host dist/mcpme-host --package dist/json-utilities-package
uv run --locked python scripts/verify_phase1.py --host dist/mcpme.app --model-package dist/jevk5-package --json-package dist/json-utilities-package --output test-artifacts/mcpme-phase1-verification.json
```

The first command includes the legacy decision contract and cache tests, native reaper ownership tests, protocol/HTTP tests, model graph and resource tests, and migration failure/recovery tests. The second copies the host to a relocated path, installs two deterministic services, invokes real HTTP and stdio clients, and checks independent controls and cleanup.

The model verifier copies the packaged host to a temporary directory and runs it with a minimal environment. It also relocates the model package, exercises real SDK clients, and writes an exclusive-create metadata report. Choose a new report path when repeating a run. The pinned model and an Apple silicon Mac are required.

## Verified on 2026-10-01

The complete automated suite passed: **219 tests in 37.90 seconds**. These include the original decision contract, probability/cache behavior, HTTP authentication and policy, independent controls, client/session cleanup, native ownership/reaper failure, bounded graph startup, resource reservation, and migration interruption/recovery cases. `git diff --check` also passed.

The abrupt-client regression starts real client subprocesses that exit without DELETE. Six clients repeatedly exceed a two-session capacity alongside a surviving client; idle/request deadlines reclaim their actual target processes, expired sessions return 404, capacity becomes reusable, and the survivor remains responsive. All seven target children are cleaned up. Legacy pause/resume has a separate finite lifecycle deadline covering declared startup and cleanup; ordinary tool deadlines and the short status deadline remain independent.

The real-model integration run passed **18 of 18 checks** with two installed services, six synthetic model decisions, two deterministic calls, and one direct scorer comparison. Two HTTP clients and a stdio client shared the same engine PID and instance; the engine reported one inference slot. The exact original tool schemas and model identity were preserved. Literal-string evidence produced a maximum absolute probability difference of **0** against the original scorer on that engine. Fresh, repeated, and changed inputs produced cache-hit flags **false, true, false**. Model installation took **44.72 seconds** in this run; this includes verification, copying, startup, and protocol checks and is not inference latency.

Independent stop/start checks, generation and cache reset after restart, graceful shutdown, and supervisor SIGKILL with surviving native reapers passed. A controlled exit through the engine's owned reaper left the deterministic service usable. The existing JevK5 listener on 19385 was unchanged throughout. The initial metadata report is `dist/phase1-initial-verification-v2.json`; the command above is the release-artifact verification gate.

Native UI verification used an isolated private state directory. The UI planned and installed two prepared projects on distinct ports, showed both Ready, stopped one while the other remained Ready, and completed Start and Restart. Details exposed schemas and identity; connection copy returned a credential-free configuration. Closing the window left the supervisor and services running, and Command-M reopened it. Stop All stopped both projects. Quit exited the app and supervisor, closed both listeners, and left no owned-process records. The saved setup file was mode 0600 and retained the reviewed plan and operation for reopening.

## Legacy transfer

The transfer implementation has automated success, failure, concurrency, interruption, rollback, and forward-recovery coverage. The recovery bundle and original state are retained. The old bundle's moved path prevents macOS from reporting its previous login preference; the owner confirmed that Start at Login was disabled. A live plan must use `--legacy-login-disabled`, which records `owner_confirmed_disabled` explicitly.

The real 19385 transfer is a separate deliberate operation after the packaged-app verification gate. The checks above do not claim that the live installation has already transferred. Inspect its durable record with `migrate-jevk5 inspect TRANSFER_UUID` for the actual outcome. Client registrations are not rewritten automatically.

## Claim boundaries

The execution profile uses trusted local packages with the current user's permissions. Access declarations are not an OS sandbox. The initial GPU policy admits one resident managed GPU package; multi-engine contention and fairness belong to Phase 3. Reaper-loss tests establish blocked replacement and retained reservations, not guaranteed target cleanup after losing its reaper. Decision transport parity does not establish new model-quality or calibration results.
