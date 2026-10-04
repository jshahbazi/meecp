# Closed Phase 3 release authority

This checkpoint implements distribution-owned admission and keeps every shipped
gate closed. It does not record final public acceptance, enable a provider,
select a model, or mint a TextGen recipe. Root integration must finish the
backend closure before minting TextGen 1.0.9. Existing published components,
recipes and signed runtime assets retain their original bytes.

The fixed artifact is `templates/release-policies/phase3-v1.json`. It is outside
the TextGen backend component closure, so an exact TextGen component key can be
recorded after that closure freezes without making the digest self-referential.
The artifact has no API, environment, state-setting, endpoint, executable-path,
factory or latest-version selector. Changing it requires a new reviewed signed
distribution. A running manager never reloads replacement authority.

## Exact JSON contract

All fields below are required, unknown fields are rejected, and input is bounded
to 64 KiB. Component references are exact `{id, version, digest}` objects using
the existing closed component schema. Duplicate references, component sets,
adapter IDs, model IDs and candidate kinds are rejected.

| Field | Type and bounds | Authority |
|---|---|---|
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
The first registered transport is OpenAI Responses; no other transport is
advertised or implemented by this checkpoint. Credentials remain deliberately
entered owner state and never appear in release policy or review digests.

Command records delegate to the separately authored
`preparation_release_command_admission.py`. The command family cannot be
advertised through a public preparation policy without both installed and
measured-preparation admission. Inventory JSON remains byte identification, not
authority. Hard aggregate enforcement and complete lifetime coverage remain
false; the original measured same-user scope is unchanged.

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

The verified result retains an immutable baseline of host/app file and directory
identities plus exact original policy bytes. Small synchronous fences check
policy/build/outer-seal members at metadata commits. Joined off-loop full checks
cover review, controlled execution, publication, resolution consumption and
agent adoption. Replacements fail closed; checks never capture a new baseline.
Startup inspection also joins repeated cancellation. These are checks within
the existing same-user filesystem and process boundary, not a claim to resist
that user replacing the running interpreter or privileged filesystem forgery.

The complete canonical policy digest and exact verified host manifest digest
bind both preparation and agent admission identities. This also binds the
command adapter and other backend implementation bytes across host upgrades.
Preparation/resolution producer identities and held agent plans
therefore change when any release-authority record changes. Old reviews cannot
gain authority through a restart with a different policy. Critical artifact
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
eligibility and current credential status. Adding credentials grants no
authority. Rotation/removal changes existing provider-generation fences. The
public host rejects mixing source admission with private simulated/live flags
or injected providers. Test transports below are isolated test instrumentation.

## Validation and remaining gates

Focused controls cover closed/ambiguous policy input, file kinds and membership,
signature metadata rejection, realistic large build inventories, held artifact
replacement, cancellation joins, ordinary/proposal separation, current producer
and source changes, recovery, publication and result consumption. A four-call
request-inspecting mock transport produces an actual proposal and exercises
adoption; it provides no live-provider evidence. Synthetic signature metadata
controls provide no actual signed-app acceptance claim.

Before activation, integration must compose the command admission module,
centrally mint the new immutable TextGen closure, build a coherent signed app,
and pass each family's final ordinary native/installed/relocated path under its
draft exact policy. Final reviewed literal keys can then be committed in this
external artifact without changing backend bytes. A5 live acceptance still
requires the user's selected provider/model and explicitly entered credentials.
Whole-phase adversarial review and public release remain open.
