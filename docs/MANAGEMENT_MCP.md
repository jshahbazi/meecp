# Scoped management and MCP

This Phase 2 milestone adds owner-issued management grants and a separate tools-only MCP connection. The local owner CLI remains available. Ordinary project connections expose only that project's tools.

## Authority

Issue grants through the local owner CLI using the running host's state directory. The host returns metadata and an absolute credential-file reference. It never returns the credential value. The file's parent is owner-only (`0700`), and the file is owner-only (`0600`). Consumers reject symlinks, hardlinks, unsafe permissions, and wrong ownership. The registry stores a SHA-256 token hash with expiry, revocation, scope, and installation-operation binding.

| Grant | Allowed operations | Scope |
| --- | --- | --- |
| Read | Capabilities, project/draft/plan/operation inspection, draft export | Host-wide management metadata and source references |
| Prepare | Read, draft creation/revision/attachment/cancellation/expiry, installation planning | Host-wide preparation; no package execution or installation apply |
| Lifecycle | Inspection and selected Start/Stop/Restart actions | Selected project UUIDs and their current package digests |
| Install | Plan inspection, exact-plan apply/retry, operation/result inspection | One saved plan and one durable installation operation |

Read and preparation grants include all drafts and management metadata. Lifecycle grants permit normal restart-generation changes; replacing a project's package invalidates their access. An installation grant covers the complete saved plan, including package/source binding, target UUID/port, start effects, and resource choices. Changing the plan rejects apply. A second plan for the same draft cannot use the first plan's grant.

Install grants expire after 15 minutes. Other grants default to one hour and accept 60 seconds through 24 hours. Requests check the registry for expiry/revocation each time. Apply rechecks after acquiring its installation lock. The operation and grant's one-operation binding commit in the same SQLite transaction. Concurrent retries return that operation, including after host restart. Interrupted runtime operations retain the existing recovery requirements. An in-process retry also repairs a transient draft-submission failure and schedules the original pending plan.

Revocation blocks further requests. It does not cancel an operation with durable authorization; operation cancellation remains unsupported. Deleting a credential file does not revoke copied tokens: use the revoke command. Files are reopened for every CLI/MCP call, so a missing file fails closed. Management MCP never falls back to the owner credential.

These API scopes follow the same-user trust boundary. They do not isolate another process running with the owner's macOS account permissions.

## CLI

Use your installed launcher's absolute path. These examples use the checkout launcher and default installation:

```sh
scripts/mcpme authorize --prepare --ttl-seconds 3600 --json
scripts/mcpme authorize --read-only --json
scripts/mcpme authorize --slot SLOT_UUID --action start --action stop --ttl-seconds 3600 --json
scripts/mcpme install inspect --plan PLAN_UUID --json
scripts/mcpme authorize --plan PLAN_UUID --json
scripts/mcpme grants list --json
scripts/mcpme grants inspect GRANT_UUID --json
scripts/mcpme grants revoke GRANT_UUID --json
```

Replace uppercase placeholders with returned identities. Issuance/revocation requires local owner authority; scoped credentials cannot issue or revoke grants. `--plan` authorizes the existing plan's start choices. It does not add Start authority to a stopped-install plan.

Select a scoped credential before an ordinary management command:

```sh
scripts/mcpme --credential-file /absolute/private/grant.json install apply --plan PLAN_UUID --json
scripts/mcpme --credential-file /absolute/private/grant.json operations get OPERATION_UUID --json
```

Global `--state-dir` and `--credential-file` options precede the command. Scoped management credentials cannot be used for `serve`, project `connect`, or project connection export. Project invocation credentials never authorize management routes.

The registry retains at most 4,096 grants. When issuance reaches that limit, it retires expired/revoked history and reclaims its credential files before admitting a new grant. Active records and installation operations remain intact. If all records are active, issuance reports `grant_limit`. List returns the latest 100 records and the retained count; inspect/revoke accept an exact known grant UUID.

## Connect an external agent

Export a management registration for a particular grant:

```sh
scripts/mcpme --credential-file /absolute/private/grant.json management export --json
```

The returned `mcpServers` configuration invokes the installed launcher with the state directory, credential-file reference, and `management connect`. Configure that entry in the intended MCP client. Export contains no credential value. Export again after relocating the app or credential file. A running supervisor is required for management calls; initialization does not open the app or start a project.

The facade exposes 18 typed tools: capabilities; draft list/create/inspect/revise/export/attach/cancel/expire; project list/inspect; installation plan/inspect/apply; operation inspection; and Start/Stop/Restart. There are no grant-issuance tools, unrestricted shell tools, upgrade/removal tools, or automatic retries of failed calls. Tool visibility does not confer authority: the host rejects operations outside the connection's grant. An agent can prepare with one connection and use a separately owner-issued exact-plan connection after review.

The facade and CLI use the owner-only manager Unix socket, preserve Host/Origin checks, and have no TCP/proxy fallback. MCP uses revision `2025-11-25`, advertises only tools, bounds frames and responses to 1 MiB, permits at most 16 pending requests, and applies finite call/write deadlines. Malformed/oversized frames close the connection. Errors contain stable codes without source contents, credentials, or raw exceptions. EOF and signals shut down the facade.

## Verification

Regressions cover scope denial, private files, expiry/revocation, registry migration, plan changes, duplicate apply, binding rollback, restart persistence, transient draft-save recovery, capacity reclamation, frame/output limits, and EOF/signals. Real SDK clients exercise preparation, review, installation, project invocation, and revocation.

Recorded local results, 2026-10-02: 525 Python tests passed; all 5 Swift private-transport tests passed; the native application self-test passed. Independent recheck added coverage for blocked-output EOF, invalid request IDs, 48 sequential canceled calls, disconnection after durable apply, alternate-plan grant rejection, and revocation after durable authorization. The blocked-output and invalid-ID regressions failed against the initial implementation and passed after correction. An ad hoc app build passed both the relocated management verifier and the existing raw-Python Phase 2 verifier. The management verifier confirmed preparation scope, exact-plan authorization, duplicate apply, restart binding, lifecycle scope, and revocation using real SDK clients and a temporary installation. These checks used synthetic fixtures; they do not complete the full Phase 2 recovery matrix or exercise public signing/notarization.

For a separately built bundled host, prepare only the synthetic JSON Utilities fixture and run:

```sh
uv run --locked python scripts/prepare_json_package.py --output dist/management-json-package
uv run --locked python scripts/verify_management.py --host /absolute/path/to/host --package dist/management-json-package
```

The verifier copies the host and fixture into temporary paths with spaces, selects a temporary HOME/state directory and minimal PATH, uses the bundled launcher, and tests real MCP clients. It verifies scopes, duplicate apply, host-restart binding, lifecycle actions, and revocation. It stops/reaps the temporary host and preserves the default installation. The existing Phase 2 verifier continues to exercise raw Python preparation.

Pinned downloads, secret injection, upgrades/rollback/removal, raw model preparation, and document services remain pending. Integrated provider preparation remains a later extension.
