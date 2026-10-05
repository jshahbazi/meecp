# Agent provider configuration

The Supervisor owns a private provider registry for deliberately entered provider
credentials. Configuration does not contact a provider, transfer source, or enable
agent execution. Both advertised execution flags remain false. Live acceptance
still requires a separately reviewed execution gate and a user-selected provider.

## Storage and authority

The registry uses the existing `SecretStore` in the dedicated
`agent-provider-config` state directory. This is private local file storage, not
macOS Keychain: directories use mode 0700 and records use mode 0600, with the
existing owner, regular-file, no-follow and bounded-read checks. The Supervisor's
exclusive state ownership and an in-process registry lock serialize changes.
These checks do not isolate credentials from other code running as the same user.
No environment variables, CLI sessions, auth files or existing slot secrets are
searched or imported.

Credential values enter only through explicit owner requests. Lists, review
records and identities contain metadata and opaque random generations. They do
not contain key values or key-derived hashes. Named immutable secret records stay
private; public metadata does not expose their storage references. Request error
responses use fixed codes and never include validation inputs or exceptions.

Every provider update changes its configuration generation. Rotation also changes
the credential generation and revokes the old secret record. Removal changes the
configuration generation, retains the credential generation for metadata binding,
marks the provider removed and revokes its secret. Old reviews and captured
backend resolvers reject changed generations. Restart reloads the same generation
and does not manufacture a new approval. The host still applies its source,
provider, request, budget, cancellation and adoption checks.

Metadata publication precedes revocation. An interrupted write can leave an
unreferenced private secret record, and interrupted revocation can leave its
previous value on disk. Neither grants dispatch authority: resolution requires the
current active provider and exact configuration and credential generations.

## Owner API

All routes retain the existing local Host, Origin and authentication checks.
Grants, including preparation grants, cannot manage providers. JSON mutation
bodies are limited to 64 KiB and reject unknown fields.

| Method and path | Fields / result |
| --- | --- |
| `GET /v1/agent-providers` | `schema_version`, trusted `adapters`, provider metadata, `live_execution_enabled: false` |
| `POST /v1/agent-providers` | `schema_version: 1`, `adapter_id`, `name`, `models`, `configuration`, `credentials` |
| `POST /v1/agent-providers/{id}` | `schema_version: 1`, `expected_generation`, `name`, `models`, `configuration`, `credentials` |
| `POST /v1/agent-providers/{id}/remove` | `schema_version: 1`, `expected_generation` |

HTTP success responses add `api_version: 1` to the schema-versioned result;
mutation responses are `{api_version: 1, schema_version: 1, provider: ...}`.
The agent preparation routes use the same wire envelope. Route errors, including
owner denials, also include `api_version: 1`, as required by the production native
request parser. Existing LocalPolicy authentication and transport failures retain
their shared legacy contract. Internal registry methods return their existing
schema-versioned objects. Provider metadata has
`provider_id`, `adapter_id`, `name`, `models`, `configuration`, `generation`,
`credential_generation`, `credential_status`, `status`, `destination` and
`execution_available: false`. Status is `active` or `removed`; credential status
is `configured` or `unavailable`. Adapter metadata declares its destination,
closed configuration schema, credential field descriptors and
`model_selection: explicit_ids`.

The initial trusted adapter is `openai-responses-v1`. Its only destination is
`https://api.openai.com/v1/responses`, configuration must be `{}`, and credential
input must be `{api_key: ...}`. Updating with `credentials: {}` retains the key;
entering `api_key` rotates it. Keys are 1–4096 visible ASCII characters. Names are
1–80 characters without surrounding whitespace or ASCII controls. Models are
1–16 unique explicitly entered IDs matching
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. The registry permits 16 active providers and
128 total records, including removed tombstones.

Route errors have the shape `{api_version: 1, error: {code: ...}}`. Generation conflicts return
`agent_provider_configuration_stale` with HTTP 409; unknown IDs return
`agent_provider_not_found` with HTTP 404. Owner denial returns `grant_scope` with
HTTP 403. Other rejected requests return fixed errors with HTTP 400. Mutations
have no idempotency replay protocol: after an unknown outcome, refresh the list
and current generations before taking another action. Native clients clear the
entered secret before sending and never refill it from responses.

## Preparation and adapter extension

Preparation options expose configured profiles with their names and generations
while execution remains disabled. Scope selects `provider_id` and `model`; the
host issues a review with `provider_generation` and the trusted destination. A
client compares this generation with its selected options entry. Configuration
or key changes require another review.

Only trusted host registration admits adapters. The adapter protocol supplies
closed metadata, configuration and credential validation, a fixed destination,
an implementation digest and a backend factory. The host binds these facts and
the opaque generations into provider/request identity. Clients cannot register
adapters, supply endpoints or select arbitrary backend code. Future trusted
adapters can use this seam without rewriting host dispatch. This checkpoint
advertises and implements only OpenAI Responses.

## Verification boundary

Registry tests use synthetic credentials, isolated state and mock transports.
They cover persistence, rotation/revocation, stale and in-flight reviews, owner
route authorization, closed inputs, private-file checks, default Supervisor
registration and a synthetic trusted adapter extension. They do not discover
credentials, make live provider calls, or establish live-provider compatibility.
The native wire control compiles the production response parser and private HTTP
transport, exercises the real Supervisor over its owner Unix socket, and verifies
that schema-only and invalid API-version responses remain rejected.
