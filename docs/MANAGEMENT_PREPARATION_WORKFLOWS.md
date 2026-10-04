# Management preparation workflows

Phase 3 adds recipe and preparation lifecycle tools to the scoped management MCP facade. The owner first reviews a saved execution plan, then issues a credential bound to that plan and one attempt. A scoped MCP connection cannot mint authority or enable an unavailable runner.

## Tools and grants

The facade exposes 40 typed tools. The existing 18 draft, installation and app tools retain their scopes. The additional tools are:

| Tools | Operation |
| --- | --- |
| `mcpme_list_recipes`, `mcpme_get_recipe`, `mcpme_get_recipe_schema` | Read versioned recipe metadata and manifest/lock schemas |
| `mcpme_register_recipe` | Statically validate and register one exact reviewed bundle |
| `mcpme_enable_recipe`, `mcpme_disable_recipe`, `mcpme_deprecate_recipe`, `mcpme_revoke_recipe`, `mcpme_remove_recipe` | Change one exact recipe version's registry state |
| `mcpme_inspect_preparation_source`, `mcpme_get_preparation_form` | Inspect a saved draft and its focused questions without executing source |
| `mcpme_plan_preparation`, `mcpme_plan_preparation_form` | Review an exact descriptor or normalized form answers and execution policy |
| `mcpme_plan_resolution` | Review dependency/interface resolution for an exact source candidate |
| `mcpme_start_preparation`, `mcpme_list_preparations`, `mcpme_get_preparation`, `mcpme_cancel_preparation` | Start, inspect and cancel preparation jobs within the connection's scope |
| `mcpme_start_resolution`, `mcpme_list_resolutions`, `mcpme_get_resolution`, `mcpme_cancel_resolution` | Start, inspect and cancel resolution jobs within the connection's scope |

Read and prepare grants can inspect recipes, sources, forms and job metadata. Prepare grants can request code-free plans. All four earlier grant modes still deny preparation/resolution start and cancel, recipe mutation, and grant issuance/revocation.

Three separately owner-issued modes permit the new mutations:

| Mode | Captured authority |
| --- | --- |
| `preparation-execution` | Complete saved preparation plan, plan digest, and one owner-selected UUID idempotency key |
| `resolution-execution` | Complete saved resolution plan, plan digest, and one owner-selected UUID idempotency key |
| `recipe-management` | Exact recipe ID, version and bundle digest; explicit state actions; exact absolute bundle path when registering |

Execution scopes retain every saved plan field, including the draft/revision/source identity, descriptor or resolution request, recipe/components/artifacts, execution policy and producer/runtime identities. Preparation's mutable `job_id` pointer is excluded from the captured plan comparison and bound separately at admission. Any explicitly required service probe or other family authority remains a separate validated contract inside the plan; a workflow credential does not manufacture secret or network authority.

An execution credential can inspect its bound draft and only its associated job. Job lists are filtered to that job. It cannot revise the draft, inspect unrelated jobs, acquire artifacts, read or administer secrets, transfer source to a provider, install a package, or issue another grant. Installation still requires its own reviewed installation plan and authority.

Recipe registration validates declarative bundle bytes and begins in the proposed state. A grant cannot add trusted executors or workers. Enabling registry metadata does not override component compatibility, reviewed release policy, or the host's execution gates. The mutation callback rechecks the active grant, exact reference and action inside the recipe transaction, including idempotent state changes.

## Owner issuance and use

Create and inspect a plan through the existing preparation or resolution commands. Issue an execution credential after reviewing the returned scope:

```sh
scripts/mcpme authorize --preparation-plan PLAN_UUID --plan-digest PLAN_SHA256 --idempotency-key ATTEMPT_UUID --json
scripts/mcpme authorize --resolution-plan PLAN_UUID --plan-digest PLAN_SHA256 --idempotency-key ATTEMPT_UUID --json
scripts/mcpme --credential-file /absolute/private/grant.json preparations start --plan PLAN_UUID --plan-digest PLAN_SHA256 --idempotency-key ATTEMPT_UUID --json
scripts/mcpme --credential-file /absolute/private/grant.json management export --json
```

Use the actual plan digest and one newly selected attempt UUID. A wrong plan or key is denied. Concurrent retries return only the original job created by that grant. An execution grant cannot adopt an owner job or another grant's job, even if their plan and key match.

Registering a recipe requires an exact reviewed bundle digest and path:

```sh
scripts/mcpme authorize --recipe RECIPE_ID --recipe-version VERSION --recipe-digest BUNDLE_SHA256 --recipe-action register --recipe-action enable --recipe-path /absolute/private/recipe --json
scripts/mcpme --credential-file /absolute/private/grant.json recipes add --path /absolute/private/recipe --json
scripts/mcpme --credential-file /absolute/private/grant.json recipes enable --id RECIPE_ID --version VERSION --json
scripts/mcpme recipes schema --json
scripts/mcpme grants revoke GRANT_UUID --json
```

Grant issuance and revocation are owner-management routes. The MCP tool list has no grant administration tool. Workflow credentials use the existing owner-only credential-file format and management export path. Tokens are returned only through the private credential file; metadata responses contain its reference.

New modes default to 300 seconds and accept 1–3,600 seconds. Their expiry fences active work as well as queued admission. Revocation commits first, then the owner route drains only the grant's verified original job association. Expiry/revocation monitors cancel active owned work. The original process handles, journal and resource reservations remain held until cleanup settles. If an association is corrupt, cleanup does not select a replacement job; the revocation stays durable and execution fails closed.

Deleting a credential file prevents that file from being used, but copied tokens remain active until expiry or explicit revocation. Credential and facade failures never fall back to owner authentication. Earlier installation grant semantics remain unchanged: revoking an install grant does not cancel an already durably authorized installation operation.

## Persistence and compatibility

Workflow grants share `imports.sqlite` and its transaction with preparation/resolution job admission. The database stores saved scope contracts and token hashes. The job, authority-kind row, exact grant/job association, and consumed-plan pointer commit together; a failed admission rolls them all back.

Every new preparation/resolution job has schema version 2 and an explicit owner or execution-grant marker. An independent authority-kind row records that marker at creation. A missing, changed or deleted grant association cannot turn a granted job into an owner job. Existing schema-1 owner jobs remain byte-preserved and are recognized only without a new authority marker/kind row.

The import database migrates versions 0/1 to version 2 transactionally. Draft contracts remain schema version 1 and saved draft/job bytes are preserved. Older hosts reject database version 2 before reading jobs; unknown newer versions are denied. Downgrading requires a separately preserved compatible state directory.

The workflow ledger retains at most 4,096 records independently of the earlier grant ledger. It retains authority history for jobs and reports `grant_limit` when full. List returns the latest 100 records across both ledgers and their combined retained count. Unexpected private directory entries are not reclaimed.

Grant checks run under the import store lock and admission transaction. Existing source, component, recipe, artifact, release-policy and worker checks continue at execution/native creation/publication boundaries. A grant is an additional requirement. It cannot relax the host's signed artifact, current authority or supported-family checks. These controls retain the same-user trust boundary; a process with the owner's account privileges can edit private state.

## Verification scope

Focused controls exercise both job kinds, atomic duplicate admission and rollback, complete captured-plan changes, corrupt/missing associations, revoked/expired authority, database migration/reopen, and an original pre-upgrade reader rejecting the new database. Real MCP SDK clients exercise static recipe tools, legacy denials, exact job visibility/retry, and owner revocation joining a synthetic owned child. The child fixture uses an explicitly private test runner and does not establish public package preparation, provider, model, signed distribution, or install acceptance. An additional real SDK control proves an exact execution grant cannot enable a disabled runner.

Final acceptance must bind these new backend bytes to a newly produced host and execute the admitted public workflows. Earlier signed app receipts remain evidence for their original backend versions.
