# Scoped management and MCP

Management MCP supports native connection approval with remembered, revocable consent. Explicit owner-issued file grants remain available. The local owner CLI remains available. Ordinary app connections expose only that app's tools. The backend API identifies installed apps as slots. Phase 3 adds [exact preparation/resolution execution grants and recipe lifecycle tools](MANAGEMENT_PREPARATION_WORKFLOWS.md).

## Authority

Issue grants through the local owner CLI using the running host's state directory. The host returns metadata and an absolute credential-file reference. It never returns the credential value. The file's parent is owner-only (`0700`), and the file is owner-only (`0600`). Consumers reject symlinks, hardlinks, unsafe permissions, and wrong ownership. The registry stores a SHA-256 token hash with expiry, revocation, scope, and installation-operation binding.

| Grant | Allowed operations | Scope |
| --- | --- | --- |
| Read | Capabilities, app/draft/plan/operation inspection, draft export | Host-wide management metadata and source references |
| Prepare | Read, draft creation/revision/attachment/cancellation/expiry, installation and upgrade planning | Host-wide preparation; no package execution or installation/upgrade apply |
| Lifecycle | Inspection and selected Start/Stop/Restart actions | Selected app UUIDs and their current package digests |
| Install | Plan inspection, exact-plan apply/retry, operation/result inspection | One saved plan and one durable installation operation |
| Upgrade | Upgrade plan inspection, exact-plan apply/retry, bound operation/result inspection | One saved upgrade plan, reviewed installed slot snapshot, and one durable operation |
| Recipe management | Static registration and explicitly selected state changes | Exact recipe ID/version/digest and registration path |
| Preparation execution | Exact-plan start/retry, bound draft/job inspection and cancellation | One complete saved preparation plan and attempt key |
| Resolution execution | Exact-plan start/retry, bound draft/job inspection and cancellation | One complete saved resolution plan and attempt key |

Read and preparation grants include all drafts and management metadata. Lifecycle grants permit normal restart-generation changes; replacing an app's package invalidates their access. Changing an installed app's startup policy and removing it require owner authority; no scoped grant permits these operations. An installation grant covers the complete saved plan, including package/source binding, target UUID/port, start effects, and resource choices. Changing the plan rejects apply. A second plan for the same draft cannot use the first plan's grant.

Install and upgrade grants expire after 15 minutes. Read/prepare/lifecycle grants default to one hour and accept 60 seconds through 24 hours. The three workflow modes default to five minutes and accept 1–3,600 seconds. Requests check their ledger for expiry/revocation each time. Apply rechecks after acquiring its installation lock and after upgrade package verification yields. The installation/upgrade operation and grant's one-operation binding commit in the same SQLite transaction. Concurrent retries return that operation, including after host restart or a completed upgrade advancing the slot generation. Interrupted runtime operations retain the existing recovery requirements. An in-process installation retry also repairs a transient draft-submission failure and schedules the original pending plan.

Revocation blocks further requests. Installation operation cancellation remains unsupported. Preparation/resolution execution grants also fence active work; owner revocation drains only the verified original job and retains reservations until owned cleanup settles. Deleting a credential file does not revoke copied tokens: use the revoke command. Files are reopened for every CLI/MCP call, so a missing file fails closed. Management MCP never falls back to the owner credential.

These API scopes follow the same-user trust boundary. They do not isolate another process running with the owner's macOS account permissions.

Opening the registry migrates earlier schemas through schema 5, preserving slots, settings, operations, and explicit grants. Schema 5 prevents earlier hosts from interpreting leased session grants without their lease/revision guards. Earlier builds reject this state, so downgrading requires a separately preserved pre-migration state directory. Do not manually lower the schema version while session grant records remain.

## CLI

Use your installed launcher's absolute path. These examples use the checkout launcher and default installation:

```sh
scripts/meecp authorize --prepare --ttl-seconds 3600 --json
scripts/meecp authorize --read-only --json
scripts/meecp authorize --slot SLOT_UUID --action start --action stop --ttl-seconds 3600 --json
scripts/meecp install inspect --plan PLAN_UUID --json
scripts/meecp authorize --plan PLAN_UUID --json
scripts/meecp authorize --upgrade-plan PLAN_UUID --json
scripts/meecp grants list --json
scripts/meecp grants inspect GRANT_UUID --json
scripts/meecp grants revoke GRANT_UUID --json
```

Replace uppercase placeholders with returned identities. Issuance/revocation requires local owner authority; scoped credentials cannot issue or revoke grants. `--plan` authorizes the existing plan's start choices. It does not add Start authority to a stopped-install plan.

Select a scoped credential before an ordinary management command:

```sh
scripts/meecp --credential-file /absolute/private/grant.json install apply --plan PLAN_UUID --json
scripts/meecp --credential-file /absolute/private/grant.json operations get OPERATION_UUID --json
```

Global `--state-dir` and `--credential-file` options precede the command. Scoped management credentials cannot be used for `serve`, app `connect`, or app connection export. App invocation credentials never authorize management routes.

The earlier registry retains at most 4,096 grants. When issuance reaches that limit, it retires expired/revoked history and reclaims its credential files before admitting a new grant. Active records and installation operations remain intact. If all records are active, issuance reports `grant_limit`. The separate workflow ledger retains at most 4,096 records and preserves its job authority history; it reports `grant_limit` when full. List returns the latest 100 records across both ledgers and their combined retained count; inspect/revoke accept an exact known grant UUID.

## Connect an external agent

Choose **Copy Management Connection** in the native app's menu, or open **Approved Connections**. This creates an unapproved public profile and copies an agent prompt followed by the JSON registration containing the installed launcher, state selector, and `management connect --profile PROFILE_UUID`. Paste the complete text into your agent. No credential path or secret is exported. The prompt is:

```text
Add meecp as a local stdio MCP server using the configuration below. Connect and request inspection and setup-planning access. Wait for me to approve the meecp popup, then list the installed apps to confirm the connection works.
```

The owner CLI can create the JSON registration:

```sh
scripts/meecp management export --json
```

Add that registration to your MCP client. Initialization and discovery complete locally before approval. After initialization, the app asks whether to allow inspection and setup planning. You can reduce access to inspection, deny access, or remember it for this macOS account. A reported client name is unverified display metadata. Remembered consent applies to the profile, installation, and account; it does not authenticate a particular application. Each connection receives a fresh one-hour session credential over a private Unix lease. The facade keeps it in memory; disconnect, revocation, expiry, and host restart invalidate its authority.

**Approved Connections** lists profiles and active sessions, with **Disconnect** and **Revoke Access**. Copying a registration does not approve access, start Pi, or bind consent to an Add App draft. Per-app **Copy Connection** retains its invocation-only behavior.

The catalog adds `meecp_request_access` and `meecp_get_authorization_status`. Access requests return promptly. Pending calls return structured authorization errors; after allowing access, explicitly repeat the intended call. Preparation/resolution execution, installation, and upgrade application require a separate request with an exact saved plan ID/digest and, for execution, an idempotency key. The native review approves only that operation, including the exact upgrade plan and slot snapshot. Approval never automatically replays a tool call. Recipe mutation, app lifecycle changes, startup policy, removal, grant administration, and all internal `/v1/pi/*` APIs remain outside native inspection/planning consent.

Bare `management connect` supports one-connection consent; remembering requires a profile. If the containing installed app is absent, the facade attempts one bounded launch. An unavailable app/UI produces an authorization error with instructions to open meecp. Unattended clients can use an explicit file grant.

To export a management registration for an existing file grant:

```sh
scripts/meecp --credential-file /absolute/private/grant.json management export --json
```

This compatibility configuration invokes the installed launcher with the state directory, credential-file reference, and `management connect`. It contains no credential value. Export again after relocating the app or credential file. Invalid explicit credential files fail without opening a consent request or selecting owner authority.

The facade exposes 45 typed tools: the existing 40 management tools, connection access/status tools, and the three upgrade tools `meecp_inspect_upgrade_plan`, `meecp_plan_upgrade`, and `meecp_apply_upgrade`. The [workflow contract](MANAGEMENT_PREPARATION_WORKFLOWS.md) lists operation names and scope. There are no grant-issuance tools, unrestricted shell tools, startup-setting/removal tools, or automatic retries of failed calls. Tool visibility does not confer authority: the host rejects operations outside the connection's grant. Read grants may inspect saved upgrade plans; prepare grants may create upgrade plans; an exact upgrade grant may apply only its saved plan against its reviewed slot snapshot and binds one durable operation. Upgrade cancellation and recovery remain owner-only.

Upgrade access is split by action. `meecp_inspect_upgrade_plan` is read-scoped, `meecp_plan_upgrade` requires preparation authority, and `meecp_apply_upgrade` requires a separate owner-issued upgrade grant bound to the exact plan digest, slot UUID and expected slot generation. The native popup displays the candidate, configuration and exact plan identity before the owner approves that grant. A remembered base connection never includes upgrade application authority.

```sh
scripts/meecp --credential-file /absolute/private/upgrade-grant.json upgrade apply --plan PLAN_UUID --json
```

An agent's upgrade sequence is: `meecp_plan_upgrade` (omit `settings` to retain the
current settings), `meecp_inspect_upgrade_plan`, `meecp_request_access` with
`kind=upgrade`, the exact plan ID and digest, then an explicit `meecp_apply_upgrade`
after the popup approves it, followed by `meecp_get_operation`.

The facade and CLI use the owner-only manager Unix socket, preserve Host/Origin checks, and have no TCP/proxy fallback. MCP uses revision `2025-11-25`, advertises only tools, bounds frames and responses to 1 MiB, permits at most 16 pending requests, and applies finite call/write deadlines. Malformed/oversized frames close the connection. Errors contain stable codes without source contents, credentials, or raw exceptions. EOF and signals shut down the facade.

## Verification

Regressions cover scope denial, private files, expiry/revocation, registry migration, plan changes, duplicate apply, binding rollback, restart persistence, transient draft-save recovery, capacity reclamation, frame/output limits, and EOF/signals. Real SDK clients exercise preparation, review, installation, app invocation, and revocation.

Recorded local results, 2026-10-02: 589 Python tests passed; all 5 Swift private-transport tests passed; the native application self-test passed. Independent recheck added coverage for blocked-output EOF, invalid request IDs, 48 sequential canceled calls, disconnection after durable apply, alternate-plan grant rejection, and revocation after durable authorization. The blocked-output and invalid-ID regressions failed against the initial implementation and passed after correction. Integration with current app management also verified all four grant modes deny owner-only startup settings/removal, and retry after removal cannot recreate an app. An ad hoc app build passed both the relocated management verifier and the existing raw-Python Phase 2 verifier. The management verifier confirmed preparation scope, exact-plan authorization, duplicate apply, restart binding, lifecycle scope, and revocation using real SDK clients and a temporary installation. These checks used synthetic fixtures; they do not complete the full Phase 2 recovery matrix or exercise public signing/notarization.

For a separately built bundled host, prepare only the synthetic JSON Utilities fixture and run:

```sh
uv run --locked python scripts/prepare_json_package.py --output dist/management-json-package
uv run --locked python scripts/verify_management.py --host /absolute/path/to/host --package dist/management-json-package
```

The verifier copies the host and fixture into temporary paths with spaces, selects a temporary HOME/state directory and minimal PATH, uses the bundled launcher, and tests real MCP clients. It verifies scopes, duplicate apply, host-restart binding, lifecycle actions, and revocation. It stops/reaps the temporary host and preserves the default installation. The existing Phase 2 verifier continues to exercise raw Python preparation.

The owner CLI/API supports [pinned prepared-package downloads](PINNED_DOWNLOADS.md) and [configuration with secret references](CONFIGURATION_SECRETS.md). Existing grants and MCP tools grant no download or secret-store authority; an exact install grant covers only its reviewed plan's references. Native configuration/download controls, reviewed upgrades/recovery and pinned raw model preparation are implemented. Upgrade application uses the exact upgrade grant described above; operation cancellation/recovery and secret-store administration remain owner-only. Reference-document services remain later work. Owner-authorized app removal retains data; deleting retained data remains unsupported. Internal [Pi app setup](PI_SETUP.md) is implemented through owner-only `/v1/pi/*` routes and remains outside the scoped management MCP catalog.

## Native authorization contract

The [external-agent authorization contract](EXTERNAL_AGENT_AUTHORIZATION_PLAN.md) describes the adopted scope, private bootstrap protocol, remembered consent, and acceptance criteria. Local verification evidence and remaining acceptance limits are recorded in [native authorization verification](EXTERNAL_AGENT_AUTHORIZATION_VERIFICATION.md).
