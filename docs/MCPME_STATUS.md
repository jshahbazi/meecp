# mcpme implementation status

Implemented against [MCPME_DESIGN.md](MCPME_DESIGN.md). The host has a native Apps window, package verification, durable registry, CLI, independent deterministic services, and a JevK5 model package with one shared engine. An app is a configured local tool or model service; the backend API calls these installations slots. The original JevK5 app remains separately buildable. See [Phase 1 verification](MCPME_VERIFICATION.md) and [Phase 2 onboarding](PHASE2_ONBOARDING.md) for acceptance evidence and limitations.

## Milestone scope

Phase 1's **two working slots** acceptance is complete for prepared local packages: JevK5 and JSON Utilities have independent controls, stable endpoints, real MCP clients, and verified supervision and cleanup. The native prepared-package setup and deliberate legacy transfer are included in the [recorded verification](MCPME_VERIFICATION.md).

Phase 2 includes drag-and-drop, source-folder and Markdown/text intake, resumable versioned drafts, external-agent handoff, static package return, and import-bound installation plans. The prepared-package path uses the same review and installation operation. Installed apps support changing startup policy and removal with retained data. The [management milestone](MANAGEMENT_MCP.md) implements scoped grants and a management MCP facade, with owner CLI bootstrap, expiring/revocable credentials, and exact-plan one-operation binding. Reference-document services, raw model intake, downloads, upgrade/rollback, and integrated provider preparation remain pending. Phase 2 as a whole is incomplete.

Automatic Start at Login registration and next-login launch remain unverified for the development bundle. The tested legacy transfer preserved an explicitly owner-confirmed disabled preference; it did not validate enabled login-item transfer.

## Supported scope

- Apple silicon, macOS 26 or later.
- Prepared local `mcp-stdio` packages with one foreground CPU-only executable per client session.
- Prepared local `model-service` packages with a declared singleton dependency graph, authenticated private HTTP adapter, and supervisor-owned engine processes.
- MCP revision `2025-11-25`, tools, structured results, progress and tool-list notifications, cancellation, and bidirectional ping.
- Clients may advertise additional optional capabilities during initialization. The tools-only gateway advertises no unsupported client callbacks to packages; those extra features remain unavailable.
- Stable UUIDs and bound public loopback ports; default allocation prefers 19386–19485.
- Independent invocation credentials, with a separate owner-only management credential.
- Manual or start-with-host policy, editable for installed apps. New slots default to manual. Explicit Stop persists suppression across host restarts.
- Durable installed-app removal that closes its endpoint, stops owned processes, and revokes its connection while retaining package and data for recovery.
- Static package planning, snapshotted artifacts, idempotent apply, candidate initialization/schema checks, and asynchronous operation records.
- Native reapers for gateways and package processes, session deadlines and leases, and durable ownership-loss blocking.

The supplied JSON Utilities package exposes `validate_json(document)` and returns a validity flag and top-level type. JevK5 exposes its original `jevk5_decide` and `jevk5_status` tools. Every connected JevK5 client shares one adapter, inference lock, and engine for that slot generation. The adapter never launches or kills the engine. The supervisor starts dependencies, checks authenticated readiness, and stops them in reverse order.

## Native app

Build the current source into a fresh app output. The menu groups apps by attention needed, running, and stopped, and displays incoming requests, last-minute traffic, and requests in flight. Click an app for the companion panel on the right: live activity, failures, latency, model/cache statistics where available, Start/Stop/Restart, tools, declared resources/access, and connection copy. Ports are in details. Stop All and Quit remain available. **Manage Apps** opens a searchable workspace with apps grouped by attention needed, running, and stopped. Selecting an app shows live request activity, its tools and connection, startup choices, recent operations, and removal controls in the same window. Search and All/Running/Attention filters keep large app lists manageable; selection follows the app identity as status changes, and clears if that app disappears or is filtered out. **Add App** accepts supported folders and text documents, saves a resumable draft, and offers external preparation or prepared-package review. Installation and lifecycle operations remain visible after closing and reopening the window. Copying connection configuration is a separate action and never edits a client's settings.

Request counters cover the current host session and survive individual app restarts. The recent rate uses one-second buckets over the last minute. Authentication failures and background HTTP status/health polls are excluded. No request content is recorded. Missing telemetry is shown as unavailable; a lost connection marks the snapshot stale. If the private activity channel loses events, counts remain unavailable until the host restarts because that session's totals are incomplete. Model decision/cache statistics have their own runtime reset scope.

For native layout review without starting services or reading installation state, run the native executable with `--ui-preview` for the menu or `--manage-preview` for Manage Apps. Both show explicitly labeled synthetic apps and activity. Lifecycle and configuration actions are inert; the Manage Apps copy action previews feedback without changing the clipboard, and setup actions are disabled. Set `MCPME_PREVIEW_SCENARIO` to `empty`, `many`, `unavailable`, or `recovery` to inspect those states. Temporary preview bundles can use `McpmeManagePreview: true` and `McpmePreviewScenario` in Info.plist when launched without arguments. Run `--management-ui-self-test` for isolated AppKit selection and action regression checks. Use an isolated `--ui-state-dir /absolute/path` for real service verification.

Closing the window leaves the host running. Quit waits for owned processes to drain and exit. The UI starts one supervisor and checks that private discovery identifies that exact process. An already running external supervisor produces a visible ownership conflict. **Start at Login** uses ServiceManagement for mcpme; each installed app's manual or start-with-host policy remains separate.

The supported draft and handoff commands are documented in [Phase 2 onboarding](PHASE2_ONBOARDING.md). Model selection and download wizards remain pending.

### Manage installed apps

The **Installed Apps** workspace keeps app selection and detail scrolling stable during live refresh. Host disconnects replace live counters with unavailable values and disable changes; model-specific statistics remain adapter-specific. The **Add App** workspace organizes the existing resumable setup into Source, Preparation, and Review sections. Switching workspaces preserves the draft and installation plan. A completed installation offers **Open Installed App** to reach activity and connection controls.

Select an installed app in **Manage Apps** to see its details, choose **Manual** or **Automatic when mcpme opens**, and remove it. Startup changes affect future mcpme launches without starting or stopping the current runtime. An explicit Stop remains suppressed across host restarts; Start clears that suppression. Removal closes the endpoint, stops owned processes, revokes the connection credential, and removes the app from the active list. Its source remains untouched, and the host retains its sealed package, configuration, and data for recovery. Retention cleanup and data deletion are not part of removal.

The CLI uses the existing slot UUID and technical policy values:

```sh
scripts/mcpme slots configure SLOT_UUID --start-policy host --json
scripts/mcpme slots configure SLOT_UUID --start-policy manual --json
scripts/mcpme slots remove SLOT_UUID --json
scripts/mcpme operations get OPERATION_UUID --json
```

Configuration uses owner-authenticated `PATCH /v1/slots/{id}` with `{"start_policy":"manual"}` or `{"start_policy":"host"}`. Removal uses owner-authenticated `POST /v1/slots/{id}/remove` and returns a durable operation ID; poll the operation to confirm completion. App terminology does not rename the slot/package contracts or change connection identities.

Startup changes and removal are blocked while installation or another removal is unfinished, ownership is lost, or migration requires recovery. Resolve the retained operation or ownership record before retrying; the controls cannot establish that an unowned process has exited.

## Build and run

From the repository root:

```sh
uv sync --locked
uv run --locked python scripts/build_reaper.py
uv run --locked python scripts/prepare_json_package.py
scripts/mcpme serve --reaper "$PWD/runtime/mcpme-reap"
```

Keep the foreground supervisor running. Its default private state directory is `~/Library/Application Support/mcpme`. Use the global `--state-dir /absolute/path` option before the command to select a separate development installation. Quit with Ctrl-C or SIGTERM; owned processes are drained and reaped.

To build a relocatable CLI distribution with a private Python interpreter and locked dependencies:

```sh
uv run --locked python scripts/build_mcpme_host.py
dist/mcpme-host/bin/mcpme serve --reaper "$PWD/dist/mcpme-host/bin/mcpme-reap"
```

Move the complete `mcpme-host` directory to relocate it. Its `bin/mcpme` launcher computes its runtime paths from its own location. Runtime use does not require uv, Homebrew, or the checkout. Build scripts require uv and Xcode command-line tools. Builders refuse to overwrite an existing output; choose a new `--output` directory for another build.

Build the native app around a fresh copy of that private host:

```sh
uv run --locked python scripts/build_mcpme_app.py
```

To prepare JevK5, first build or retain the verified original `dist/JevK5.app` using the [legacy build instructions](../README.md). The preparer verifies its recorded relocated engine hashes and the pinned model before copying them:

```sh
uv run --locked python scripts/prepare_jevk5_package.py
```

The package contains its own interpreter, locked dependencies, adapter, engine and licenses. Installation never follows the editable preparation directory. The generated app and packages are development artifacts with ad hoc signatures. Developer ID signing and notarization are pending.

## Install and connect

In another terminal, use the same launcher and state directory as the running supervisor:

```sh
scripts/mcpme capabilities --json
scripts/mcpme validate --package "$PWD/dist/json-utilities-package" --json
scripts/mcpme install plan --package "$PWD/dist/json-utilities-package" --name "JSON Utilities" --start --json
scripts/mcpme install apply --plan PLAN_UUID --json
scripts/mcpme operations get OPERATION_UUID --json
scripts/mcpme slots list --json
scripts/mcpme connections export --slot SLOT_UUID --json
```

Replace the uppercase UUID placeholders with values returned by preceding commands. `--start` belongs to **plan** in this milestone so the plan includes the activation effect. Omit it to install a stopped slot. Apply accepts the exact saved plan; repeated or concurrent applies return the same operation and slot. A failed operation requires a new plan. Planning performs no package execution; apply starts the candidate to verify its protocol and tool schemas.

`connections export` emits a stdio client configuration using the launcher's actual absolute path. In the app bundle it selects `Contents/MacOS/mcpme`. It never emits the invocation credential or changes a client's settings. Export again after relocating the app. A bundled `connect` command can open the app when its default installation is offline; a standalone CLI distribution or custom state directory requires an already running supervisor. Manual Stop still suppresses execution until Start.

```sh
scripts/mcpme slots start SLOT_UUID --json
scripts/mcpme slots stop SLOT_UUID --json
scripts/mcpme slots restart SLOT_UUID --json
scripts/mcpme slots inspect SLOT_UUID --json
scripts/mcpme connect --slot SLOT_UUID
```

Lifecycle commands return operation IDs. Poll `operations get` for completion. `connect` speaks MCP stdio and is intended for an MCP client. It resolves an owner-only Unix socket and credential privately, forwards protocol messages, and attempts DELETE on EOF or shutdown. The management CLI and native UI also use private sockets and fail closed without TCP fallback. Public HTTP endpoints and their Host/Origin/bearer checks remain available. Abrupt client loss is reclaimed by the gateway's lease. It does not retry tool calls.

## Package contract

The authoritative schema is in `backend/mcpme/contracts.py`; `scripts/prepare_json_package.py` and `scripts/prepare_jevk5_package.py` produce complete examples. Each package contains `mcpme.package.json`, `mcpme.lock.json`, every declared artifact, and referenced license files. The lock records exact sizes, hashes, source identity, provenance, executable status, and build/toolchain information.

The validator rejects unresolved source revisions, unknown fields, incompatible versions/profiles, missing or extra artifacts, unsafe paths, links, special files, mismatched executable modes, invalid tool schemas, external schema references, and changed content. All package-relative executable/cwd paths must remain within the verified tree. Source identity is a full commit hash or a SHA-256 snapshot identity. Metadata and total artifact sizes are bounded; the model-capable package limit is 64 GiB.

Stdio packages declare one process per session. Model packages declare a bounded acyclic graph of singleton engine and adapter processes. Each receives generation-specific configuration through an inherited descriptor. Credentials never enter arguments. JevK5's bootstrap replaces itself with the real engine using `exec`, preserving the native reaper's owned child identity. The adapter receives its declared dependencies' private endpoints and credentials. It echoes a fresh nonce and generation on its authenticated readiness route; the unmodified engine requires an authenticated response and a live owned process, with anonymous access rejected.

Package programs must stay foreground and must not spawn undeclared persistent workers or daemons. Access declarations are descriptive. Trusted package code runs with the account's permissions; this is not an OS sandbox. Hash verification establishes agreement with the saved package identity and does not establish publisher trust. Candidate execution verifies the actual protocol and declared tool contracts on this machine.

Model admission reserves the declared memory estimate plus headroom. The private `resource-policy.json` defaults to 75% of physical memory with 1 GiB headroom per model. The initial GPU policy permits one resident managed GPU package at a time. A second is visibly `resource_blocked`; deterministic services remain available. Estimates are not measured RSS or GPU attribution. This policy covers mcpme-owned services; other applications can still consume memory and GPU resources.

The example's full artifact tree is copied into private host storage during planning and verified again before activation. Editing the preparation directory later does not change the accepted package. Package processes receive a minimal environment, private data/temp locations, stdio, and no management credential or launch-control descriptor.

## Ownership and recovery

The supervisor keeps each public port bound and passes its listener to the corresponding worker. It never probes and later assumes a port is free. Port conflicts on restart are visible; endpoints are not silently moved. A stopped worker returns `slot_stopped`, and management remains separately available.

Each gateway can request only its own declared per-session entrypoint through a private channel. The supervisor launches that executable through a native reaper, then transfers its pipes. The channel accepts no executable, arguments, PID, or other-slot selection. Cancellation around the ownership transfer completes cleanup before another transfer can use the channel.

DELETE, expiration, stop, restart, and gateway death reclaim session children. SSE or POST disconnection alone preserves an unexpired session and its process. Requests have finite deadlines; open event streams do not renew an idle lease. Session mappings, event replay, framing, and message sizes are bounded. Responses and protocol history are never written to disk.

A surviving native reaper terminates and reaps its target if the supervisor dies, including SIGKILL. Losing the reaper itself records `ownership_lost`, closes the affected slot, stops its remaining owned processes, and retains the missing instance's record. Other slots continue. The host never guesses that an old PID is safe to signal. Restart cannot bypass this block.

Any unfinished ownership record after host restart conservatively requires reconciliation, even if its surviving reaper probably cleaned up successfully. This milestone provides no automated reconciliation command. A user must establish that the previous owned targets exited before an operator repairs the retained state; ordinary Start/Restart cannot clear it.

Interrupted installations become `recovery_required` and cannot be controlled into exposure. Controls are also rejected while an install is active. Once the durable exposure marker permits calls, later failures preserve the selected slot and its data. They cannot trigger automatic discard or rollback to older data. Automated recovery, upgrades, data deletion, and retention cleanup are pending; preserve the private state directory when investigating a blocked operation.

Management accepts the local owner's credential or an owner-issued scoped grant. The separate stdio management MCP facade requires a grant file and checks authority through the manager for each call. Changing startup policy and removing an app require owner authority. Pinned downloads, first-call start, standalone `mcp-http` packages, and upgrades remain unsupported and are reported by `capabilities`.

## Import the existing JevK5 installation

Migration is an explicit CLI operation. Ordinary installation creates a new slot and does not take over port 19385. The import preserves 19385 and the old invocation credential, adds only the JevK5 compatibility routes `/api/status`, `/api/control`, `/api/decide`, and retains the old `backend.lock` while mcpme owns the imported installation. The old credential can pause/resume only this slot and cannot manage the host or other slots.

Build a recoverable copy with the supported native handoff commands, preserving the original bundle, then build mcpme with its exact executable identity recorded:

```sh
uv run --locked python scripts/build_jevk5_recovery.py
uv run --locked python scripts/build_mcpme_app.py --output dist/mcpme-with-migration.app --legacy-recovery-app dist/JevK5-recovery.app
```

Use that app's CLI while it is running:

```sh
dist/mcpme-with-migration.app/Contents/MacOS/mcpme migrate-jevk5 plan --package "$PWD/dist/jevk5-package" --legacy-app "$PWD/dist/JevK5-recovery.app" --json
dist/mcpme-with-migration.app/Contents/MacOS/mcpme migrate-jevk5 apply TRANSFER_UUID --json
dist/mcpme-with-migration.app/Contents/MacOS/mcpme migrate-jevk5 inspect TRANSFER_UUID --json
```

Planning snapshots and verifies the package, records the recovery locations and old login/pause preferences, and presents the transfer effects. Apply requests the old application's supported termination, retires its login item, acquires its lock and port without evicting another owner, installs and verifies a synthetic decision, then commits the transfer. Login preference transfer can require macOS approval; an unavailable or unverified setting is surfaced rather than assumed. The old bundle, state, and migration record are retained.

Recovery bundles also receive the updated private-socket backend and safe Python launcher, while retaining their model and runtime files. Quit an original JevK5 build before planning migration: a running legacy service must provide the updated owner-only socket to read its pause preference safely. Otherwise planning returns `legacy_transport_upgrade_required`. Offline planning sends no status probe to the fixed TCP port. Migration verification and rollback readiness use private sockets without a TCP fallback.

If macOS reports `not_found` for a moved legacy bundle, planning normally blocks. After confirming that the old app's Start at Login is disabled, add `--legacy-login-disabled` to the plan command. The record labels that fact as owner-confirmed, not an observed ServiceManagement setting. This flag does not bypass a known enabled or approval-required login item.

Transfer stages are `staged`, `old_stopped`, `endpoint_acquired`, and `committed`. Duplicate applies recover the same transfer. A failure closes candidate admission and preserves recovery artifacts. Restart reacquires the legacy lock before binding a committed slot's endpoint. Interrupted transfers and ownership loss cannot be bypassed with Start. `migrate-jevk5 rollback TRANSFER_UUID` is available before candidate exposure and restores the recorded old app only after both lock and port are free. After exposure, `migrate-jevk5 recover TRANSFER_UUID` can restart and verify the selected generation using its existing data, once ownership is known and installation completed. It never restores older data. Unresolved ownership or an unfinished installation still requires reconciliation.

The old stdio registration still points to the old launcher. Export a replacement with `connections export --slot SLOT_UUID` and update the specific client entry you intend to change. Migration does not rewrite client configuration.

Removing a migrated JevK5 app records a durable completed transfer tombstone before releasing the retained legacy lock. It preserves the original source, bundle, and legacy data. Removal does not restart the legacy app or reenable its login registration. The removed transfer cannot be resumed as an active migration. A new transfer of the retained legacy installation returns `removal_recovery_required` while its predecessor's slot or unfinished removal operation remains; finish removal recovery before planning or applying that transfer.

## Verification

```sh
uv run --locked python -m pytest -q
uv run --locked python scripts/verify_mcpme.py --host dist/mcpme-host --package dist/json-utilities-package
```

The tests include the original JevK5 regressions, package/path/hash and private-state rejection cases, idempotency and recovery races, native parent/reaper failures, cancellation during descriptor transfer, and real SDK clients over HTTP and stdio. The two-slot integration checks independent controls and credentials, stable ports, structured output, and private snapshot installation. The separate bundle verifier copies the host to a relocated path and runs CLI installation and real clients with a minimal environment.

The model and native acceptance commands and their results are recorded in [MCPME_VERIFICATION.md](MCPME_VERIFICATION.md). Model transport parity verifies this integration; it does not establish decision quality or confidence thresholds for a new task.
