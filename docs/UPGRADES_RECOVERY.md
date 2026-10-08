# Reviewed upgrades and interruption recovery

An upgrade keeps the app's UUID, public port, invocation credential, display name
and startup policy. It selects a new immutable package and settings generation.
Changing installed configuration uses this same review and recovery path.

The candidate must have the same package ID and execution profile. Inspecting and
planning it performs static validation and copies sealed artifacts; it executes no
package code. The owner reviews the exact tools, access declarations, configuration
and secret reference IDs before applying the plan.

## Plan and apply

```sh
scripts/mcpme upgrade plan --slot SLOT_UUID --package /absolute/path/to/package --json
scripts/mcpme upgrade inspect --plan PLAN_UUID --json
scripts/mcpme upgrade apply --plan PLAN_UUID --json
scripts/mcpme operations get OPERATION_UUID --json
```

Supply `--settings /absolute/private/settings.json` to change configuration or
secret references. The file has the same private envelope and permission rules as
[installation settings](CONFIGURATION_SECRETS.md). Without it, the candidate must
accept the installed choices. To edit settings without changing code, select the
installed package path and review a new upgrade plan with the new settings.

The plan binds the current slot generation, package digest, settings digest and
state pointers. A changed slot requires a new review. Repeated applies of the same
plan return the same durable operation, including its completed or recoverable
result.

## Data and admission

The host closes admission, reaps old writers and clones the old private data tree
into `slots/SLOT_UUID/generations/OPERATION_UUID/data`. Candidate initialization,
tool-contract checks and declared synthetic smoke fixtures use that clone. They
cannot mutate the old data through the host-provided data path.

Snapshots reject symlinks, hard links, special files, unsafe permissions, ambiguous
names, changed files and insufficient disk space. They are bounded to 20,000 files
and 64 GiB. Package children inherit `umask(0077)` so ordinary new data files and
directories stay private. A package that deliberately changes permissions must
restore this boundary before a future upgrade can snapshot its data.

Selection of the candidate pointer and the operation's selection marker is one
registry transaction. The host then durably records permission to expose that
generation before opening its ordinary gateway. During preparation the authenticated
gateway returns `updating`; management progress stays available.

Before exposure, failure or cancellation reaps the candidate and checks the old
package's readiness and MCP contract before restoring the endpoint. Business
fixtures are not replayed on old data or on exposed candidate data during recovery
and restart. The generation
number advances so stale sessions cannot become current again. After exposure,
failure retains the selected candidate data and closes admission. The host never
automatically restores old data after a generation could have accepted writes.

The marker records permission, rather than an observation of a client request.
Completed stopped installations/upgrades also commit that permission for a later
explicit Start. This conservative boundary preserves data when a crash prevents
the host from knowing whether a write reached the candidate.

Old package, settings and data generations remain private recovery artifacts.
Removing the app retains them along with the selected data; it does not delete
user data.

## Cancel, stop and recover

```sh
scripts/mcpme operations cancel OPERATION_UUID --json
scripts/mcpme operations recover OPERATION_UUID --json
```

Cancel applies to an installation or upgrade before exposure. It durably records
the request and waits for owned file workers and process cleanup before reclaiming
candidate authority. A cancellation request returns promptly; poll the operation
for its terminal outcome. After exposure, Cancel returns
`installation_already_exposed` or `upgrade_already_exposed`.

Recover retries a `recovery_required` operation using its recorded plan and selected
artifacts. Upgrade recovery restores the old generation only when no exposure
permission exists. Once exposure is recorded, recovery verifies the selected
candidate and preserves its current running or stopped intent. Stop remains
available for exposed recovery, including through the native Stop Candidate
control. A later Recover leaves an explicitly stopped app stopped. Accepted
client calls are never replayed automatically.

Port conflicts remain visible and do not move the endpoint or evict another
process. Missing selected data is an error; recovery never replaces it with an
empty directory. Corrupt or missing upgrade records remain recoverable failures
when a durable selection/exposure marker exists.

Native reapers retain exact child identity and write private completion receipts
after `waitpid`, including when the supervisor dies. Startup clears an ownership
entry only after its matching instance/nonce receipt proves completion. Losing the
reaper before that proof leaves `ownership_lost` and blocks replacement. No recovery
path discovers or signals a recorded PID.

## Owner API and scoped upgrade access

The following routes require the owner credential:

| Route | Request |
| --- | --- |
| `POST /v1/upgrade/plan` | `{ "slot_id": "UUID", "package": "/absolute/path", "settings": { ... } }`; settings optional |
| `GET /v1/upgrade/plans/PLAN_UUID` | Inspect the exact sealed plan |
| `POST /v1/upgrade/apply` | `{ "plan_id": "UUID" }` |
| `POST /v1/operations/OPERATION_UUID/cancel` | `{}` |
| `POST /v1/operations/OPERATION_UUID/recover` | `{}` |

Read the result through `GET /v1/operations/OPERATION_UUID`. The management MCP adds
read access to inspect a saved upgrade plan and preparation access to create one. An
owner-issued upgrade grant may apply only the exact saved plan digest, slot UUID and
expected generation captured in its scope; its first successful apply atomically binds
one durable upgrade operation and retries return that operation. It cannot change the
candidate, settings, slot snapshot or target. Upgrade cancellation and recovery remain
owner-only. Slot invocation credentials have no management authority.

The native owner review shows the candidate package, configuration, secret-reference
names and exact plan digest before issuing an upgrade grant. Approval is for that
one saved plan and slot snapshot; it does not authorize another upgrade or replay a
stale plan.

See [PHASE2_COMPLETION.md](PHASE2_COMPLETION.md) for the current acceptance evidence
and the pending final artifact checks.
