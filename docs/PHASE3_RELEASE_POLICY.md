# Phase 3 release authority

## Current source after the CI repair

The tracked source uses TextGen engine/task/recipe 1.0.21. Pi changes the complete
backend source closure, so the old 1.0.14 recipe no longer matches the registered
component identities. A fresh immutable recipe binds the current closure. The
current release policy also binds the updated unlocked Python adapter; each
family and recipe remains pinned to an exact version and digest.

| Current source binding | SHA-256 |
| --- | --- |
| Release policy | 29d0359e9846afff79061c0fd0d0bf265f88fd78deb6c62193af1ba614ca9dcc |
| TextGen engine | 438e6822bfc22187f91b51f496a6575db5a1bfeb55e7ddf3216d60a98883554d |
| TextGen task | ad814ce4186bd3474fe9759991208d80eca0f491d449a00c38aee44b23c5b1d2 |
| TextGen recipe | 492417b5993266c3c8d1cc3c8f37c5f8282349d4956ba36272f68221018e4cf7 |

The recipe was generated with `write_textgen_recipe` using `uv run --locked` and
contains nine files. Focused cold-host, capture, recipe upgrade, policy, and
lifecycle controls pass. Upgrade coverage includes 1.0.14, retained enabled
snapshots, owner opt-outs, and collected tombstones. Versions 1.0.15 through
1.0.20 already exist in repository history, so their identities are not reused.

The signed v10 runtime/live acceptance below retains its original source and
TextGen 1.0.13. It has not been rerun for 1.0.21. Frozen selected13 and selected14
policy bytes are retained in `tests/fixtures/phase3-selected-textgen-1-0-13-policy.json`
and `tests/fixtures/phase3-selected-textgen-1-0-14-policy.json`, respectively.

## Recorded signed v10 acceptance

Snapshot: 2026-10-04. **Supported Phase 3 source/runtime acceptance is complete;
verified working-branch publication is complete.** Runtime source is
24bae0f8fd0aa91ebaaba5ee2b0d7d4020cc9498, with TextGen recipe/components
1.0.13. Current v10 management, selected live agent, CLI/cross-build upgrade,
TextGen create/supplement and ten-case recipe lifecycle pass. Corrected complete
artifact/signature/direct entitlement checks, final preservation and supported
whole-phase review pass. D09 is Complete from first source/delivery publication
77e2828e077403291c605544efac73aa6041b573, with exact origin/codex/phase3-public-acceptance
SHA verified 2026-10-05 01:16:15 UTC at https://github.com/jshahbazi/mcpme.git.
The documentation recording commit records this runtime anchor without
rebuilding or rewriting its immutable artifact/receipt evidence.

The exact recorded signed v10 authority used templates/release-policies/phase3-v1.json,
SHA-256 8a9a8fd69217352b49e0e338d4d652df8cebcac86609edae1190a88cd2d9a592.
Changing this reviewed artifact requires a new signed distribution. A running
manager does not reload replacement authority. There is no endpoint, environment,
state-setting, executable path, provider factory or latest-version selector
that grants public authority.

Original published components/recipes, the signed TextGen runtime asset and
fixed command helpers retain their recorded bytes. Recorded producer 13 engine,
task and recipe bind the reviewed frozen backend closure. Catalog fingerprint
scope is its explicit top-level implementation/asset inventory. Public admission
also binds the complete verified host manifest, resolution producer files and
held artifact fences, including nested native TLS; the narrower catalog hash
alone is not a full nested-host closure claim.

## Recorded signed v10 admitted composition

The recorded policy has six exact family heads and seven exact recipe closures:
the ordinary Python/MCP, ONNX, CLI, TextGen and first service composition plus
the sole unlocked Python agent component. Existing candidate kinds, command
platform pins and service references retain their reviewed authority. The
TextGen engine/task/recipe references in this acceptance record are 1.0.13.

| Authority | Exact admitted value |
| --- | --- |
| Agent component | agent-unlocked-python-adapter-v1@1.0.0 |
| Component digest | 7d37411ca8119c2433fa44a59080d89a48c3618f7f909bbcb2977646b445882f |
| Provider adapter | openai-responses-v1 |
| Adapter digest | f651b36ab4b3d4fe6479457e5a31f129b1339c41536e02bfb53cbbf4025cebc1 |
| Sole model | gpt-6.1-sol |
| Unlocked resolution | Strict Boolean true |
| TextGen engine digest | 899aa082f51bbe079968ce4540fa801215adcb9f8a2fc622844e4e63ee133c9e |
| TextGen task digest | 936c30b0ede738344713d37d62cb71dbfea59b7375dfa24ea023d9693bf9274a |
| TextGen recipe digest | c5fff2ecc6d6018f61fe3c65f929be389381c572149d9dbebffc11c716123465 |

Historical signed source 2fa verifies the selected OpenAI tuple with four real
calls, zero retries and full native resolution/preparation/attachment/installed
source-free acceptance. Its public_release_accepted=false scope remains
historical. Earlier v9 failed at agent_handoff with unexplained cause and unknown
remote-call count. The diagnostic-only current source adds bounded observation;
the bounded v10 attempt independently passes four real calls, zero automatic
retries and the full native/installed/source-free flow on exact current source.
The earlier v9 cause remains unexplained; diagnostics do not establish a
behavioral fix. Eight real calls are verified across the two successful batches,
plus the failed batch whose count is unknown. Supported runtime acceptance is
complete; verified branch publication is complete. The three additional
capability gates remain separate.

Credentials are explicit private owner state, excluded from release policy,
review digests and public receipt content. Configuration/rotation does not add
a provider, model, recipe, secret privilege or execution permission. The first
runtime service connector is separately tested using an owned numeric-loopback
OpenAI-compatible protocol fixture with zero real remote-provider/model calls.

## Exact JSON contract

All fields below are required, unknown fields are rejected, and input is bounded
to 64 KiB. Component references are exact `{id, version, digest}` objects using
the existing closed component schema. Duplicate references, component sets,
adapter IDs, model IDs and candidate kinds are rejected.

| Field | Type and bounds | Authority |
| --- | --- | --- |
| `schema_version` | Integer `1`, not Boolean | Format only |
| `policy_id` | 1–128 identifier characters | Reviewed policy identity |
| `preparation.families` | At most 32 component references | Exact composite family implementations |
| `preparation.recipe_component_sets` | At most 32 arrays, each 1–32 references | Exact complete registered recipe closures |
| `preparation.resolution_candidate_kinds` | Unique subset of `python-callable`, `python-mcp` | Ordinary inspected original candidates only |
| `agent.components` | At most 16 component references | Exact registered trusted guide/family bindings |
| `agent.providers` | At most 16 records | Exact accepted adapter implementation/model combinations |
| `agent.providers[].adapter_id` | 1–128 identifier characters | Explicit trusted adapter registration |
| `agent.providers[].adapter_digest` | Lowercase SHA-256 | Actual registered adapter implementation |
| `agent.providers[].models` | 1–16 unique identifiers | Explicit accepted model IDs, no discovery |
| `agent.unlocked_resolution` | Strict Boolean | Separately permits durable unlocked proposal resolution |
| `command` | `null` or the record below | Separate command/platform gate |
| `command.component` | Three strings `[id, version, digest]` | Source-approved CLI implementation |
| `command.installed_platform_policy` | Lowercase SHA-256 | Source-approved installed command platform matrix |
| `command.resource_platform_policy` | Lowercase SHA-256 | Source-approved measured preparation matrix |

Every selected reference must match the current registered component digest.
Every dependency must appear in its exact recipe closure. Agent components must
also appear in preparation families and singleton recipe closures, and match
registered closed guides. Provider policy alone cannot authorize any family.
Only the exact accepted OpenAI Responses adapter and sole model above are
admitted. Credentials never appear in release policy or review digests.

Command records delegate to `preparation_release_command_admission.py`. The
command family cannot be advertised through a public preparation policy without
both installed and measured-preparation admission. Inventory JSON identifies
bytes and grants no authority. Hard aggregate enforcement and complete lifetime
coverage remain false; the original measured same-user scope is unchanged.

An otherwise valid signed distribution can run on a supported host OS whose
exact kernel/product tuple has not passed command acceptance. The command gate
validates the full artifact, component, helper and signatures before reporting
the fixed `command_release_platform_unavailable` outcome. Only that outcome
removes the exact command family and every recipe closure containing it from
effective preparation admission. Independent families, ordinary Python
resolution and admitted agents retain their authority. The effective admission
still binds the full original policy digest and verified host manifest; a
reduced capability set has its own digest. Invalid policies, helpers,
signatures or held identities remain fatal admission failures. The accepted
command platform matrix never expands from observed runtime values.

## Signed artifact and current authority

Ordinary source checkouts and standalone development hosts return no release
admissions, regardless of the tracked policy's contents. Their manual package
and SDK paths remain available after a distribution policy is activated. This
context comes only from the running module's lexical and resolved location;
there is no environment or API override. An app ancestor in either location
keeps strict app validation, including links into or out of an app. The private
policy-loading test seam continues to reject invalid active artifacts outside
an app.

An empty closed policy grants no execution. A nonempty policy in an app must
belong to the exact running module's enclosing
`*.app/Contents/Resources/host`. The loader checks a clean build receipt, complete
host file membership and hashes, the exact policy member digest, enclosing app
receipt linkage, regular files, ownership and modes. It requires the reviewed
Developer ID leaf, Apple chain, Team ID, hardened runtime, timestamp and valid
strict app seal. Public signer identifiers are fixed source constants shared
with command admission. No signing or identity discovery occurs in the loader.

Ad-hoc app builds need an explicitly closed packaged policy to retain manual
operation. A receipt claiming `ad_hoc` does not downgrade active-policy
validation; malformed, missing or Developer ID receipts cannot select a closed
fallback. The app producer can package the closed policy before creating its
manifest and seal without changing backend component bytes. Active-policy
acceptance remains specific to the reviewed Developer ID distribution.

The verified result retains a fixed captured baseline of host/app file and
directory identities plus exact original policy bytes. Small synchronous
fences check policy/build/outer-seal members at metadata commits. Joined
off-loop full checks cover review, controlled execution, publication,
resolution consumption and agent adoption. Replacements fail closed; checks
never capture a new baseline. Startup inspection also joins repeated
cancellation. This baseline is enforced by the implementation, not by a
language-level immutable-object guarantee.

Only the exact validated enclosing app root may tolerate cosmetic ctime
changes. Its held no-follow directory descriptor retains the original
device/inode/type, UID/GID, permissions, link count, size, mtime, filesystem
flags and ordered POSIX ACL identity. All descendants and files keep their
strict ctime checks. ACL/flag/group, permission, inode and structure changes
still revoke authority; inspection errors deny. No serialized field, client
input or constructor flag selects this exception, and no failed check
rebaselines it. These controls allow observed macOS root metadata changes
without claiming protection against privileged forgery or an owner replacing
the running interpreter.

The complete canonical policy digest and exact verified host manifest digest
bind both preparation and agent admission identities. This also binds the
command adapter and other backend implementation bytes across host upgrades.
Preparation/resolution producer identities and held agent plans therefore
change when any release-authority record changes. Old reviews cannot gain
authority through a restart with a different policy. Critical artifact
replacement also suppresses current capability/provider availability metadata.

## One public resolution owner

`PublicAgentResolutionStore` and `PublicAgentResolutionRunner` extend the frozen
unlocked implementation without modifying its published source. They preserve
ordinary original-candidate resolution and recognize agent proposals only from
the actual durable proposal journal. Request markers confer no authority.

The agent domain requires the exact admitted component, current original source,
proposal/project proof, dependency-resolution-only operation and the existing
closed PyPI wheel broker scope. Its producer is `python-agent-resolution-v1`
version `1.0.1`; ordinary public resolution remains `python-resolution-v1`
version `1.0.1`. Both domains share one task registry, journal, recovery loop and
reservation owner. Context-local producer selection resets after each task.
Analysis and adoption still execute no source and resolve no dependencies.

Provider list/review flags reflect source admission, exact adapter/model
eligibility and current credential status. Rotation/removal changes existing
provider-generation fences. The public host rejects mixing source admission
with private simulated/live flags or injected providers. Private test transports
remain isolated instrumentation and do not establish live compatibility.

## Accepted validation and capability boundaries

Full producer 13 Python v6 passes 4,223 tests with zero failures and one
filesystem-dependent casefold skip. All 27 actual private build, acquisition,
resolution, SDK and confinement opt-ins pass; their simulated providers remain
simulated evidence. Current Swift v6 passes 21, zero failures/skips. Diagnostic
request/callback/privacy controls and scanner 15 unit/eight actual new13-bundle
controls plus full history/source pass.

The repaired native TLS ownership path has independent pure ABI/negative
controls and eight actual signed v9 native cases: 22 actual Security evaluations,
five returned owned CFErrorRefs/five matching releases, zero outstanding errors.
Trust, hostname, expiry and fetch-denial controls pass without AIA fetches or
Keychain trust changes. V9 ordinary Python/MCP/ONNX five-case and service
acceptance also pass. The final review accepts those v9 receipts only within
the independently verified unchanged scope. Current v10 native binary, manifests
and signature are separately bound.

Current signed v10 has complete corrected root/peer app/host/source/signature
and actual direct entitlement checks. Source 24bae0f8 contains six approved
diagnostic/scanner path changes from 3ddaaddc; all 805 other Git blobs, including
backend, policy, templates and assets, match. That permits review of unchanged
backend/runtime source evidence. It does not imply a full Python re-run at 24bae0f8 or byte-identical outer v9/v10
apps. All host Python bytes, 162 effective requirements and nonartifact toolchain
metadata match. Of 33 changed host Mach-O members, 32 loadable bodies match with
CMS excluded; the newly compiled host/bin/mcpme-handoff body differs even when
UUID is masked. Full 4,223 Python/all 27 host opt-ins remain source 3ddaaddc/signed v9
receipts transferred only for unchanged code scope with independent input proof.
They did not rerun on v10. Fresh current management/live/CLI/TextGen/lifecycle independently validate the
current helper in their recorded runtime scopes.
The retained failed investigative transfer receipt is a valid negative evidence
result, not a product finding.

Current v10 completed/revoked management runs pass exact grants, idempotency,
restart association and selected revoke, with six SDK sessions exited and known
owned work joined. The wrapper relocated_app_unchanged field is null; management
relocated-app preservation is unclaimed. Live acceptance proves local functional
and known-owned behavior, not remote cancellation or settlement. All five current v10 runtime workflows pass independently, including ten recipe
lifecycle cases with source preservation, known-owned joins and zero external
calls/timeouts/forced stop. Final full source/app/pinned-asset preservation passes: all 7,722 app files,
811 source files, five pinned assets and strict signature remain exact with
known-owned joins and zero provider calls. The final review clears qualified unchanged-scope evidence transfer and
supported source/runtime acceptance. First verified working-branch publication
is complete at 77e2828e077403291c605544efac73aa6041b573; remote SHA verified
2026-10-05 01:16:15 UTC. This later documentation record does not claim its
future commit was built/tested or passed remote CI. Failed earlier receipts remain preserved.

A0R.06, A6.02 and A6.04 remain separate capabilities: executable preparation
workers; additional providers including DeepSeek, GLM, Bedrock and local
services; broader runtimes/model families, GPU/Metal, resident vision and GUI
automation. The selected OpenAI tuple and first runtime connector do not admit
these extensions.

Production native handler checks do not establish physical clicks. Functional
SDK/tensor/generation/generated-adapter checks do not measure recognition or
general model quality. Remote/unowned work settlement remains unknown;
deliberate owner-loss tests retain state/charges for reconciliation. The existing
same-user/trusted-engine scope, unproved wildcard-listener/hostile-worker
containment, and false hard-aggregate/full-lifetime claims remain unchanged.
Developer ID signing is verified; notarization, public distribution and updating
the user's installed app are unclaimed.

Current source/artifact/receipt bindings and accepted runtime gates are recorded
in [final review](PHASE3_FINAL_REVIEW.md), [final acceptance](PHASE3_FINAL_ACCEPTANCE.md), [status](PHASE3_STATUS.md),
[live acceptance](LIVE_AGENT_ACCEPTANCE.md) and
[management acceptance](PHASE3_PUBLIC_MANAGEMENT_ACCEPTANCE.md). Historical
1.0.12 policies/bundles and the selected12 policy SHA-256
4c5569f10a50a360e5c8e1cffc26cf637f5795d3a2562fc635ca2d9d419e4375
remain immutable fixtures rather than current authority.
