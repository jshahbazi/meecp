# mcpme first implementation milestone

Implemented on 2026-10-01 against [MCPME_DESIGN.md](MCPME_DESIGN.md). This milestone provides package verification, a durable registry, a CLI supervisor, and independent deterministic MCP slots. The existing JevK5 app and its model behavior remain available through their original entrypoints.

## Supported scope

- Apple silicon, macOS 26 or later.
- Prepared local `mcp-stdio` packages with one foreground CPU-only executable per client session.
- MCP revision `2025-11-25`, tools, structured results, progress and tool-list notifications, cancellation, and bidirectional ping.
- Stable UUIDs and bound public loopback ports; default allocation prefers 19386–19485.
- Independent invocation credentials, with a separate owner-only management credential.
- Manual or start-with-host policy. New slots default to manual. Explicit Stop persists suppression across host restarts.
- Static package planning, snapshotted artifacts, idempotent apply, candidate initialization/schema checks, and asynchronous operation records.
- Native reapers for gateways and package processes, session deadlines and leases, and durable ownership-loss blocking.

The supplied JSON Utilities package exposes `validate_json(document)` and returns a validity flag and top-level type. It is compiled with Swift Foundation into a standalone executable. Two installations have separate identities, public ports, credentials, and session processes. No model is loaded by this example.

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

This distribution is a development CLI host. The native menu bar UI, `mcpme.app`, login-item integration, Developer ID signing, and notarization are subsequent work.

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

`connections export` emits a stdio client configuration using the launcher's actual absolute path. It never emits the invocation credential or changes a client's settings. Client configurations after relocation should be exported again. The supervisor must already be running; automatic app launch is pending.

```sh
scripts/mcpme slots start SLOT_UUID --json
scripts/mcpme slots stop SLOT_UUID --json
scripts/mcpme slots restart SLOT_UUID --json
scripts/mcpme slots inspect SLOT_UUID --json
scripts/mcpme connect --slot SLOT_UUID
```

Lifecycle commands return operation IDs. Poll `operations get` for completion. `connect` speaks MCP stdio and is intended for an MCP client. It resolves the endpoint and credential privately, forwards protocol messages, and attempts DELETE on EOF or shutdown. Abrupt client loss is reclaimed by the gateway's lease. It does not retry tool calls.

## Package contract

The authoritative initial schema is in `backend/mcpme/contracts.py`; `scripts/prepare_json_package.py` produces a complete example. Each package contains `mcpme.package.json`, `mcpme.lock.json`, every declared artifact, and referenced license files. The lock records exact sizes, hashes, source identity, provenance, executable status, and build/toolchain information.

The validator rejects unresolved source revisions, unknown fields, incompatible versions/profiles, missing or extra artifacts, unsafe paths, links, special files, mismatched executable modes, invalid tool schemas, external schema references, and changed content. All package-relative executable/cwd paths must remain within the verified tree. Source identity is a full commit hash or a SHA-256 snapshot identity. The initial package limit is 2 GiB; models use a future profile.

The manifest declares only the supported one-process-per-session topology. Package programs must stay foreground and must not spawn persistent workers or daemons. Access declarations are descriptive. Trusted package code runs with the account's permissions; this is not an OS sandbox. Hash verification proves agreement with the saved package identity. It does not establish publisher trust or independently prove that an arbitrary native program has no undeclared dynamic dependencies. Candidate execution verifies the actual protocol and declared tool contracts on this machine.

The example's full artifact tree is copied into private host storage during planning and verified again before activation. Editing the preparation directory later does not change the accepted package. Package processes receive a minimal environment, private data/temp locations, stdio, and no management credential or launch-control descriptor.

## Ownership and recovery

The supervisor keeps each public port bound and passes its listener to the corresponding worker. It never probes and later assumes a port is free. Port conflicts on restart are visible; endpoints are not silently moved. A stopped worker returns `slot_stopped`, and management remains separately available.

Each gateway can request only its own declared per-session entrypoint through a private channel. The supervisor launches that executable through a native reaper, then transfers its pipes. The channel accepts no executable, arguments, PID, or other-slot selection. Cancellation around the ownership transfer completes cleanup before another transfer can use the channel.

DELETE, expiration, stop, restart, and gateway death reclaim session children. SSE or POST disconnection alone preserves an unexpired session and its process. Requests have finite deadlines; open event streams do not renew an idle lease. Session mappings, event replay, framing, and message sizes are bounded. Responses and protocol history are never written to disk.

A surviving native reaper terminates and reaps its target if the supervisor dies, including SIGKILL. Losing the reaper itself records `ownership_lost`, closes the affected slot, stops its remaining owned processes, and retains the missing instance's record. Other slots continue. The host never guesses that an old PID is safe to signal. Restart cannot bypass this block.

Any unfinished ownership record after host restart conservatively requires reconciliation, even if its surviving reaper probably cleaned up successfully. This milestone provides no automated reconciliation command. A user must establish that the previous owned targets exited before an operator repairs the retained state; ordinary Start/Restart cannot clear it.

Interrupted installations become `recovery_required` and cannot be controlled into exposure. Controls are also rejected while an install is active. Once the durable exposure marker permits calls, later failures preserve the selected slot and its data. They cannot trigger automatic discard or rollback to older data. Automated recovery, upgrades, uninstall, and retention cleanup are pending; preserve the private state directory when investigating a blocked operation.

Management uses the local owner's credential in this milestone. Plan-scoped grants, the management MCP facade, import drafts, pinned downloads, first-call start, HTTP/model execution profiles, GPU/memory admission, and JevK5 migration remain unsupported and are reported by `capabilities`.

## Verification

```sh
uv run --locked python -m pytest -q
uv run --locked python scripts/verify_mcpme.py --host dist/mcpme-host --package dist/json-utilities-package
```

The tests include the original JevK5 regressions, package/path/hash and private-state rejection cases, idempotency and recovery races, native parent/reaper failures, cancellation during descriptor transfer, and real SDK clients over HTTP and stdio. The two-slot integration checks independent controls and credentials, stable ports, structured output, and private snapshot installation. The separate bundle verifier copies the host to a relocated path and runs CLI installation and real clients with a minimal environment.

These establish this deterministic milestone. Full Phase 1 acceptance still requires JevK5 extraction/migration, its model and cache parity checks through slots, the native management UI, and the final app build.
