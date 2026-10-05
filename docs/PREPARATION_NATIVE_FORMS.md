# Host-generated preparation questions

The owner API generates typed questions from an immutable source snapshot and compatible, enabled recipe metadata. The native setup workspace submits opaque answers and reviews the resulting host-selected scope. It does not construct a descriptor or execute recipe UI hooks.

Cold hosts seed a pinned, host-owned Python callable recipe for ordinary setup; users do not author or add a recipe bundle to obtain these questions. Host-supplied preferred references select that compatible default among third-party recipes. Explicit recipe choices and saved selections take precedence. Disable, revoke, remove and version-collision decisions survive restart; startup never re-enables an opted-out default. See [bundled recipe metadata](BUILTIN_PREPARATION_RECIPES.md).

This checkpoint supplies setup and review infrastructure. Public automatic preparation remains disabled until the installed and relocated A0, A0R, A1 and A2 acceptance gates in [the Phase 3 plan](AUTOMATIC_PREPARATION.md) pass. Missing dependency resolution and dynamic MCP interface discovery remain explicit blockers. Static review cannot create a job, claim runtime verification or install an app.

## API

`POST /v1/preparations/form` accepts `import_id`, `revision` and optional `candidate_id` and `recipe` references. Its closed schema-1 response binds the exact source revision, selected candidate, compatible recipe inventory, typed fields, proposed tools and blockers through a domain-separated form digest.

`POST /v1/preparations/plan-form` accepts that identity and an `answers` object keyed by opaque field IDs. Unknown fields, primitive coercion, stale forms and invalid schema values are rejected. The host resolves and validates the descriptor before atomically recording the selection and advancing the draft revision. The response contains the current draft and a review. Publicly disabled execution returns null plan IDs and explicit blockers. An internally enabled acceptance host may return the existing expiring, single-use execution plan; starting it still requires exact owner intent.

Both routes require owner authority. Existing scoped metadata and attachment grants cannot call them. Inspection and full source/recipe validation run in bounded, host-owned threads; closing or canceling an HTTP caller cannot release their database ownership early.

## Revision and restart behavior

A changed resolved selection advances the draft revision and invalidates prior plans and attachments. Draft and selection writes share one database transaction. SQL failure rolls both back. Concurrent edits reject stale selection without replacing newer choices. Identical choices submitted against the current revision are idempotent.

An exact old-revision POST retry remains stale. After a lost response, fetch the current draft and reload its questions. The host restores the saved candidate, compatible recipe and typed answers from the same source identity. Current app names and resource requirements take precedence over earlier selections. Disabled, revoked or removed recipes cannot be restored as eligible choices.

The native workspace adopts the returned review and draft together. View generations, import IDs, source identities and revisions fence late responses. Closing the workspace leaves a durable job running; cancellation targets the owned job ID explicitly. Polling uses bounded host stages and counters rather than worker output.

## Bounds and representation

The host accepts at most 256 fields, 128 candidate choices, 128 recipe choices, 128 choices per field, 128 proposed tools and 128 blocker codes. Supported kinds are string, integer, number, boolean, choice, local file, object and read-only schema preview. Boolean and numeric values are distinct. Strings use Unicode code-point length constraints; objects and field schemas have a 16 KiB compact JSON byte ceiling. The complete contract also retains the shared 512 KiB, depth and node limits.

V1 recipe file inputs retain their original relative-source-path meaning and render as strings. Generic local-file questions require an absolute selected path; a path answer supplies no acquisition or execution authority. Required recipe inputs missing editable metadata produce `recipe_form_incomplete` instead of a form that appears ready.

Only reviewed host code implements an onboarding provider. A new compatible recipe can add metadata questions without a native renderer or core dispatch edit. Additional runtime families still require their own source validation, acquisition, builder, independent verifier and acceptance evidence.
