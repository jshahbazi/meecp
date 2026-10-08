# Service connection implementation contract

Status: implemented source checkpoint; independent review and final signed public acceptance remain required. This work
implements the service-only input required by `AUTOMATIC_PREPARATION.md:43` and
the Phase 3 directory-or-service flow in `MEECP_DESIGN.md`. The registry advertises the local connector's static scope. Execution requires the exact release-policy family and component closure.

## First supported connection

The first recipe exposes generate and check_connection tools backed by an owner-selected
local HTTP endpoint implementing a closed Chat Completions subset. It uses an
exact numeric `127.0.0.1` address, explicit port, fixed `/v1` base path and
`POST /v1/chat/completions`. Bearer authentication is required. There is no DNS,
proxy, redirect, URL user information, query, fragment, custom header, endpoint
discovery, ambient credential lookup, or fallback to a different protocol.

The owner selects a model identifier and an existing opaque secret reference.
Secrets are deliberately entered through the existing private secret store;
neither source metadata nor packages contain their values. This is not a
preparation-agent provider configuration. It does not authorize live remote
providers or host model execution.

Three trusted components separate protocol, transport/authentication, and task:
`openai-chat-completions-v1`, `loopback-http-bearer-v1`, and
`service-text-generate-v1`. Their first versions are `1.0.0`; exact implementation digests are recorded in the new recipe's service-provenance.json. A closed
registry supplies labels, input schemas and compatible combinations. UI and
dispatch code must not contain provider-name branches. The initial recipe is
`mcpme.local-openai-text@1.0.0` and is statically seeded. Its final public execution admission remains separate.

## Input and owner routes

`POST /v1/imports/service` accepts exactly:

```json
{
  "schema_version": 1,
  "name": "Selected local service",
  "intent": "Expose text generation",
  "start": false,
  "start_policy": "manual",
  "connection": {
    "schema_version": 1,
    "protocol": {"id": "openai-chat-completions-v1", "version": "1.0.0", "digest": "<frozen digest>"},
    "transport_auth": {"id": "loopback-http-bearer-v1", "version": "1.0.0", "digest": "<frozen digest>"},
    "task": {"id": "service-text-generate-v1", "version": "1.0.0", "digest": "<frozen digest>"},
    "endpoint": {"scheme": "http", "host": "127.0.0.1", "port": 8080, "base_path": "/v1"},
    "model": "owner-selected-model",
    "secret_references": {"api_key": "<owner-selected secret UUID>"},
    "limits": {"timeout_seconds": 30, "max_input_bytes": 16384, "max_output_tokens": 512, "max_response_bytes": 1048576}
  }
}
```

These are schema examples, not literal executable component references. All
models forbid extra fields, booleans in integer positions and nonfinite values.
Component identities must be one explicitly registered compatible combination.
The source has `kind: service_connection`, `contract_version: 1`, the normalized
connection, `revision: sha256:<domain-separated canonical connection digest>`,
and `files: []`. It has no filesystem locator or source snapshot directory.
Existing directory identities and source-digest algorithms do not change.

Creation is static and performs no HTTP request or secret-value resolution.
Secret metadata checks establish an active selected ID. Changing the connection
uses an exact-revision owner route, `POST /v1/imports/{id}/service`; it creates a
new source revision and invalidates plans/proposals through ordinary draft
lifecycle fences. No mutation silently carries an attachment to changed input.
Existing cancel/expire and import inspection operate on the draft. Export must
identify a service connection explicitly without pretending it is a source tree.

`GET /v1/service-connectors` returns bounded trusted component/form metadata and
current availability. The native setup entry is “Connect a local service”; its first closed protocol uses a numeric port, model ID and existing-secret picker. CLI intake is
`imports create-service` with bounded flags; no hand-authored JSON is required. Both use the same closed normalizer. Management
MCP initially receives no new authority through its old `prepare` grant.

## Preparation and installed execution

The family produces a normal descriptor-v2 and generated form, with an empty
original-file binding and exact service metadata identity. Adapter source,
project/lock, runtime configuration and licenses are new job-derived inputs,
recorded as such in the locked preparation receipt. The generated package keeps
the service source revision. It never substitutes the digest of a manufactured
directory for the selected connection.

The existing preparation job remains the single build/probe/adoption owner.
The new family uses its own builder and verifier; frozen Python/agent/command
builders and their component source arrays remain unchanged. Current generic
Python forms continue rejecting preparation secrets. The PyPI download policy
continues governing build acquisition alone.

A service probe needs an explicit closed grant in the reviewed version-2 execution policy: exact source/descriptor/component identities, endpoint origin and
path, method, opaque auth reference, synthetic input digest, maximum call count,
request/response bytes and deadline. Native/API/MCP plan review must display
this network and credential use before start. Descriptor binding alone is not
permission for an undocumented network side effect.

The service verifier launches the sealed private adapter through the existing
owned process journal. It supplies only the selected credential over an
inherited settings FD, revalidating immediately before delivery. A new narrow
confinement profile permits Seatbelt localhost TCP at the exact reviewed port; the sealed transport separately fixes AF_INET 127.0.0.1 without DNS. It denies
original source/build/global-runtime access and package writes. No unconfined
fallback is permitted. It uses actual MCP initialization/tool comparison and a
bounded synthetic completion call, checking structural response compatibility.
That probe makes no model-quality or semantic-answer claim.

Installed execution uses the existing settings-FD contract and manifests the
exact outbound origin and logical secret name. Endpoint/model/task limits remain
sealed; they are not mutable runtime endpoint selectors. HTTP and stdio clients
reach the same installed tool. Failed authentication, unavailable endpoint,
invalid response, oversize output, deadline, or cancellation returns a fixed
content-free error and leaves no owned late work.

The existing secret store uses immutable create/revoke IDs. Changing the original
service source to a new credential ID requires a revised connection and
preparation review. A portable package instead declares the logical `api_key`
role: a separately reviewed ordinary installation may select another host
reference, bound by the new draft revision and installation settings digest,
fresh secret resolution, and the actual installation smoke probe. This does not
rewrite the original source or claim that its preparation receipt tested the
replacement credential. The installed settings PATCH cannot change secret
references. The service-specific revocation hook cancels active probes and drains
affected installed service slots before reporting successful revocation. The current general secret store
only prevents subsequent resolution/launch; it does not erase a value already
delivered to a running child. That distinction must not be hidden.

## Integration ownership and acceptance

This lane owns additive service contracts, family, forms, adapter, builder,
verifier, tests and recipe/provenance files. Root integrates shared ImportStore,
source-independent receipt/export, catalog, Supervisor, API/CLI and native
changes. The management workflow author owns grant/facade changes and binds the
complete immutable plan; existing grants retain their closed execution scope.

Before activation, acceptance must cover real authenticated synthetic localhost
calls through prepared and installed HTTP/stdio MCP clients; exact emitted
request shape; forbidden origins/redirects/proxies; wrong/revoked credentials;
oversize/malformed/error responses; cancellation/deadline/queued revocation;
revise/cancel/expiry versus launch and attachment; source-free restart and
relocation; artifact/receipt tampering; owned cleanup; and absence of secret or
request/response material from logs, records and diagnostics. A final signed
candidate admits only the new exact component closure after these gates pass.

This supplies a functional no-folder service connection. It does not establish
remote TLS/provider compatibility, arbitrary OpenAI-compatible servers, other
protocols, streaming, or task quality. Those remain individually gated and must
not appear as supported choices. A localhost endpoint has origin/auth identity;
this design does not claim ownership or binary attestation of its external
server process within the documented same-user trust scope.

## Implemented authority variants and limits

Legacy directory identity and execution-policy-v1 canonical bytes remain unchanged.
Service policy schema 2 binds a service-probe-grant-v1 containing the original
canonical service revision, descriptor, three components, endpoint/method/path,
opaque selected secret UUID, fixed synthetic input digest, one call, body/response
bounds and deadline. Complete bound inputs use schema 2 and a distinct digest
domain; the service preparation receipt uses schema 3. The receipt carries
derivation, portable configuration and grant digests, never the host secret UUID.
Unknown fields, cross-kind variants, stale grants and receipt downgrade are denied.

The adapter supports Content-Length JSON responses, one selected model, one
assistant text choice, and stop/length completion reasons. Transfer/content
encoding and streaming are rejected. It bounds the complete HTTP operation,
including connection, headers and slow response bodies. MCP result serialization
also has a 1,044,480-byte ceiling. A second call while one is active returns
service_busy before making another request. The settings descriptor has a whole
ten-second read budget and a 524,288-byte ceiling.

A fresh current-authority check follows post-spawn secret resolution and precedes
descriptor delivery. Revocation is recorded before owned cleanup starts. Cleanup
continues across other invalid or unsettled scopes and reports a fixed incomplete
code if any affected ownership cannot settle. It never claims to stop or cancel
computation in the independently operated service.

## Source checkpoint evidence and remaining gates

Actual synthetic acceptance uses current development-parent source, the preserved
v4 private CPython/uv toolchain, and only the additive service template in a
separately inventoried private composition. No signed input is modified. Five
ordinary build commands acquire the exact locked wheel closure; the confined
adapter performs one synthetic check. Full managed receipt/admission/attachment,
installation, authenticated HTTP and stdio SDK calls, deletion of the derived
attachment and recipe author folder, and host restart are exercised. Wrong-secret
revocation leaves the service slot usable; selected-secret revocation drains its
owned adapter/gateway. Cancel-during-probe and post-resolution revoke controls
settle with no attachment.

Actual Seatbelt controls cover selected-port success and other-port,
other-loopback-address, numeric non-loopback address, outside-write, fork/spawn, setsid and setpgid denial.
Actual AppKit action handlers use the production peer-checked private HTTP
transport against owner UDS routes; reconnect, offline, malformed selection,
duplicate-label identity and service probe disclosure controls are exercised.
These controls do not establish physical UI acceptance or final signed release
compatibility.

Author validation: 71 service controls passed, including four actual private
toolchain cases and the native owner-UDS action; 333 adjacent source controls
passed with five existing actual-host opt-ins skipped. Swift passed 18 tests with
three existing installer opt-ins skipped. A subsequent metadata-only capability
change passed 31 owner/public-admission controls. Service intake is always
described separately from execution; its execution flag requires the exact
admitted and enabled complete service recipe closure. These counts are scoped
checks, not a claim that the full Phase 3 suite or final release acceptance ran.

The new immutable recipe is metadata only. Final integration must compose the
management lane's database-version-2 old-reader fence and full-plan grant
association, independently review this checkpoint, mint the complete current
host producer centrally, then admit the literal service task head and exact
three-component closure through the fixed release policy. Final signed public
native/SDK lifecycle acceptance remains open. Remote TLS/provider compatibility,
actual model behavior/quality, and external service process settlement remain
unadvertised separate gates.
