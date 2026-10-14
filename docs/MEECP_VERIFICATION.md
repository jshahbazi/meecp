# meecp Phase 1 verification

This document records the validation of the native host, deterministic package, and shared JevK5 package. Generated reports contain allowlisted counts, booleans, timings, and identities; they do not contain prompts, responses, credentials, or exception dumps.

## Reproduce

```sh
uv run --locked python -m pytest -q
uv run --locked python scripts/verify_meecp.py --host dist/meecp-host --package dist/json-utilities-package
uv run --locked python scripts/verify_phase1.py --host dist/meecp.app --model-package dist/jevk5-package --json-package dist/json-utilities-package --output test-artifacts/mcpme-phase1-verification.json
```

The first command includes the legacy decision contract and cache tests, native reaper ownership tests, protocol/HTTP tests, model graph and resource tests, and migration failure/recovery tests. The second copies the host to a relocated path, installs two deterministic services, invokes real HTTP and stdio clients, and checks independent controls and cleanup.

The model verifier copies the packaged host to a temporary directory and runs it with a minimal environment. It also relocates the model package, exercises real SDK clients, and writes an exclusive-create metadata report. Choose a new report path when repeating a run. The pinned model and an Apple silicon Mac are required.

## Verified on 2026-10-01

The complete automated suite passed after the VS Code interoperability correction: **221 tests in 38.72 seconds**. These include the original decision contract, probability/cache behavior, HTTP authentication and policy, independent controls, client/session cleanup, native ownership/reaper failure, bounded graph startup, resource reservation, and migration interruption/recovery cases. `git diff --check` also passed.

The abrupt-client regression starts real client subprocesses that exit without DELETE. Six clients repeatedly exceed a two-session capacity alongside a surviving client; idle/request deadlines reclaim their actual target processes, expired sessions return 404, capacity becomes reusable, and the survivor remains responsive. All seven target children are cleaned up. Legacy pause/resume has a separate finite lifecycle deadline covering declared startup and cleanup; ordinary tool deadlines and the short status deadline remain independent.

The final bundled-host integration run passed **18 of 18 checks** with two installed services, six synthetic model decisions, two deterministic calls, and one direct scorer comparison. Two HTTP clients and a stdio client shared the same engine PID and instance; the engine reported one inference slot. The exact original tool schemas and model identity were preserved. Literal-string evidence produced a maximum absolute probability difference of **0** against the original scorer on that engine. Fresh, repeated, and changed inputs produced cache-hit flags **false, true, false**. Model installation took **29.12 seconds** in this run; this includes verification, copying, startup, and protocol checks and is not inference latency.

Independent stop/start checks, generation and cache reset after restart, graceful shutdown, and supervisor SIGKILL with surviving native reapers passed. A controlled exit through the engine's owned reaper left the deterministic service usable. The existing JevK5 listener on 19385 was unchanged throughout this isolated run. The final metadata report is `test-artifacts/mcpme-phase1-verification.json`.

Native UI verification used an isolated private state directory. The UI planned and installed two prepared projects on distinct ports, showed both Ready, stopped one while the other remained Ready, and completed Start and Restart. Details exposed schemas and identity; connection copy returned a credential-free configuration. Closing the window left the supervisor and services running, and Command-M reopened it. Stop All stopped both projects. Quit exited the app and supervisor, closed both listeners, and left no owned-process records. The saved setup file was mode 0600 and retained the reviewed plan and operation for reopening.

## Legacy transfer

The transfer implementation has automated success, failure, concurrency, interruption, rollback, and forward-recovery coverage. The recovery bundle and original state are retained. The old bundle's moved path prevents macOS from reporting its previous login preference; the owner confirmed that Start at Login was disabled. A live plan must use `--legacy-login-disabled`, which records `owner_confirmed_disabled` explicitly.

After the packaged-app verification gate and explicit owner approval, the live transfer reached `committed` / `completed` with no error. Its synthetic decision traversed the preserved port 19385. The native UI showed JevK5 Judge Ready on 19385 with start-with-host policy and JSON Utilities Ready on 19386 with manual policy. The supported native status query confirmed that the legacy app had exited. The recovery bundle and original state remain available.

The live migration verifier passed **17 of 17 checks**. It compared the preserved invocation credential privately, proved that the legacy lock remained held, checked the original tool schemas and model identity, and connected two HTTP clients plus a stdio client to one shared engine. Anonymous, wrong-credential, Host, and Origin checks passed; the legacy credential could not access host management or the JSON slot. One pause/resume cycle kept JSON Utilities usable, changed the model generation and engine instance, and reset its cache. This pass used two synthetic model calls and two deterministic calls, then left both projects Ready. Its metadata report is `test-artifacts/mcpme-live-migration-verification.json`; the credential-free connection export is `test-artifacts/mcpme-jevk5-client.json`.

A subsequent native Quit and relaunch verified the committed transfer across a real app restart. All ten recorded app, supervisor, reaper, and target processes exited; both ownership ledgers were empty, both public ports were released, and the legacy lock was free while the host was down. The new app owned a new supervisor, reacquired the lock and port 19385, and automatically returned JevK5 to Ready in a new generation. JSON Utilities remained stopped under its manual policy and was explicitly started again. The legacy app remained absent.

The post-restart probe passed **13 of 13 continuity checks**, including the committed record, preserved port and credential, held legacy lock, original schemas/model identity, shared engine through real HTTP and stdio clients, access restrictions, and a JSON Utilities call. It made no model decisions or lifecycle changes and left both projects Ready. Its metadata report is `test-artifacts/mcpme-live-migration-restart-verification.json`.

No login item was registered or unregistered during this owner-confirmed disabled transfer. The new development bundle also reports `not_found` through ServiceManagement, so automatic login registration has not been verified. Inspect the durable transfer record with `migrate-jevk5 inspect TRANSFER_UUID` for its current outcome. Client registrations are not rewritten automatically.

## VS Code initialization regression

VS Code Insiders 1.141.0 advertised roots, sampling, elicitation, tasks, and MCP app extensions while requesting the supported `2025-11-25` revision. The gateway incorrectly treated these optional client offers as package requirements, rejected initialization with HTTP 400 `unsupported_profile`, and the stdio bridge returned `-32000`. The earlier SDK checks used empty client capabilities and did not expose this interoperability defect.

The gateway now validates the client's initialization and supplies an empty client-capability object to the private package, reflecting the gateway's implemented callbacks. It still rejects unsupported package capabilities, malformed offers, and operations outside the tools-only profile. Regression tests cover the installed VS Code advertisement through both the stdio bridge and private HTTP transport, assert that no unsupported callback is promised upstream, and verify tool discovery and cleanup. The focused transport/policy suite passed 46 tests; the complete suite result appears above.

The corrected app was rebuilt, signed, and deployed at the existing canonical path. The packaged stdio probe passed **14 of 14 checks** with the full installed VS Code capability advertisement: initialization, original tool schemas, ready pinned-model status, and the prior continuity/authentication checks succeeded. It made no model decisions and one deterministic call, leaving both projects Ready. All thirteen bundled host Python files matched the tested source. The metadata report is `test-artifacts/mcpme-vscode-handshake-verification.json`.

Actual VS Code Insiders 1.141.0-insider verification used a new empty window with the same global `jevk5` configuration. Start reached **Running** and **Discovered 2 tools**; a server-only Restart reached Running again with a new discovery of both tools and no error metadata. No model calls were made. Only the verification window was closed; the original working window and configuration were preserved. Its existing MCP connection may need its own server restart to load the correction. Both projects remained Ready afterward.

## Automatic ownership recovery on reinstall or relaunch (2026-10-14)

Foreground receipt format 2 records kernel lifetime and audit-token identities
before a private gate permits execution. Format 3 preserves a separate native
group owner through loss of the outer lifecycle proxy. Startup settles inherited
same-boot receipts before opening replacement gateways. The foreground helper
confirms the old target ended; grouped recovery requires the retained owner's
completion after descendant cleanup. Pending pre-exec frames settle under an
independent native lock, including interrupted identity writes. Cancellation
joins recovery before closing its receipt capability.

The kernel boot UUID scopes signaling authority. Missing or mismatched boot
metadata, damaged execution-authorized frames, unavailable identity checks, and
loss of both group owners cannot authorize guessed PID or process-group signals.
Older receipts retain their normal native completion path on graceful shutdown.
Registry reconciliation precedes deletion of the final durable ledger proof, so
an interruption can replay cleanup. A one-second port retry applies only after
confirmed inherited settlement and retains the saved port. Persistent conflicts
keep their ordinary error.

The regression cases include real native foreground and grouped process loss,
stopped group owners, exited leaders with live descendants, interruption before
spawn acknowledgment, cancellation during cleanup, and unrelated process/group
protection. They also cover saved policy and Stop suppression, selected data and
connection preservation, interrupted install/upgrade recovery, and a real SDK
tool call after recovery. The engineering checks qualify the supported local
process profiles; they do not qualify a Developer ID release or a new macOS ABI.
The remaining UI warning describes recovery on reopening meecp without asking
the user to restart the Mac.

The integrated receipt/relaunch/lifecycle/process/registry/host/upgrade run passed
**209 tests** with one existing opt-in case skipped. The policy, implementation
identity, packaged preflight, and signed-command authority checks passed **93
tests**. Swift tests passed **21 tests** with three opt-in installer cases skipped;
all four native UI/contract self-tests passed. Native compilation also passed
with `-Wall -Wextra -Werror`. A fresh ad hoc bundle built successfully with the
updated TextGen implementation identities and unchanged signed-command helpers.

The local development app was replaced at `/Applications/meecp.app` through
verified staging, graceful shutdown, and atomic replacement, with a separate
bundle backup. No manual ownership-record repair was needed for this replacement.
The previously Ready manual app was resumed; its runtime generation advanced
once through the normal Start path. All five registered apps retained their IDs,
ports, selected package/settings/data paths, startup policies, and Stop flags.
Per-app connection and settings files and the saved data-file hashes matched the
private pre-install snapshot. Post-launch inspection found no ownership blocks
or slot error codes and five fresh same-boot identity-bearing receipts. Installed
recovery Python modules and native binaries matched the tested source/build, and
strict deep signature verification passed. The Mac was not restarted.

## Claim boundaries

The execution profile uses trusted local packages with the current user's permissions. Access declarations are not an OS sandbox. The initial GPU policy admits one resident managed GPU package; multi-engine contention and fairness belong to Phase 4 in the updated roadmap. Reaper-loss tests cover relaunch recovery for the identity-bearing foreground profile and the grouped profile with a surviving inner owner. Missing legacy proof and loss of both group owners remain blocked with retained reservations. Decision transport parity does not establish new model-quality or calibration results.
