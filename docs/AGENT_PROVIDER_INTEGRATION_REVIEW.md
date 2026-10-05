# Provider and recipe discovery integration review

Source checkpoint `8d93ae501c91c8bcca78ca8d76d91ac630e0a7b5` integrates the
independently reviewed provider backend and API envelope repair with trusted
agent recipe discovery. Public preparation and live execution remain disabled.
No real credentials or live provider calls were used.

## Independent checkpoints

Provider checkpoint `cd7c0405fd922367418cac19c63a272c8f12c5b5` passed 337
independent checks, including 28 additional fault/race controls, with two opt-in
skips. Its production native parser and private HTTP transport passed 15 actual
owner-socket scenarios and three invalid-version controls. Review verified 13
published locked component/bundle files and six legacy source files unchanged.
Owner CRUD, namespace separation, private credential retention, generation
fencing, default gates and cancellation/recovery remained within the reviewed
scope. See [provider configuration](AGENT_PROVIDER_CONFIGURATION.md).

Recipe discovery checkpoint `3c3c5756e7dc11ccf59baea7d94a0ced3a574d9a` passed
257 independent checks, including 45 additional controls. Sixty-four opt-in
pinned-scanner integration cases were skipped. All 604 archived files remained
unchanged; 24 frozen family/bundle files matched the parent. Controls covered
exact size/discovery limits, hidden files, unusable and ambiguous locks, stale
guide/component identities, aliases, versions, lifecycle states and overlapping
families. Discovery passed with filesystem reads, credential resolution, network
calls and process creation blocked. See [recipe choices](AGENT_RECIPE_CHOICES.md).

Private review packets are mode `0600`:

| Packet | SHA-256 |
| --- | --- |
| `/private/tmp/mcpme-agent-provider-registry-independent-review-cd7c0405.json` | `3137b838ead3664344b68667fbdf5d2d2914a182164c2c3844d19b92e45b067e` |
| `/private/tmp/mcpme-agent-recipe-choice-independent-review-3c3c5756.json` | `7305107d46611d4a32c20b1616f6d3c4147bc126f77a4a80ea7f24ab745b17d7` |

## Combined source evidence

The integrated source passed 204 focused checks with no skips in 49.512
seconds. This includes provider CRUD, actual socket/native parsing, source
selection, guide/recipe discovery, locked/unlocked adoption and existing host
regressions. Integration preserves the provider-owned destination/generation
metadata and the bounded text/identity discovery inventories.

Both actual request-only pipelines passed against an exact composition of 126
source files and the frozen private runtime substrate:

| Pipeline | Test duration | Evidence |
| --- | --- | --- |
| Locked unfamiliar Python directory | 22.379 seconds | Four encoded simulated requests, actual private build, confined SDK, matching attachment, source/state removal and confined relocation. |
| Unlocked unfamiliar Python directory | 29.792 seconds | The same path plus ordinary owned dependency resolution from an inferred unpinned requirement. |

All owners joined. Each pipeline checked all 4,782 frozen runtime files before
and after. Every source-composition hash remained unchanged. There were zero live
calls; these runs do not establish a newly built host or signed installed app.

Composition packet SHA-256:
`92f8fe31a8f86285886a768b8902c736bdf7c598e6327932de295052782ebc98`.
Locked actual receipt SHA-256:
`6684ae4c88948b171d38a5bbe6589f77cf61fc87b55b934800551b32db526833`.
Unlocked actual receipt SHA-256:
`f19aa9e48bbf9a03ff87631527623591958c9a50dca567103eb0964f16d7e250`.
All three receipts are separate private files; historical receipts remain intact.
The root integration packet is
`/private/tmp/mcpme-agent-provider-root-integration-review-v1.json`, mode `0600`,
SHA-256 `f44212380fa76feca783ab5842fb890c13ea78ff08363d6d4f8465b7e6d0ebc7`.
It rechecks both review packet hashes, all 126 current source identities and both
actual pipeline receipts and their positive ownership/removal/probe assertions.

## Native repair and integration

Independent native review requested changes to checkpoint `cdf5fa55`: after an
explicit recipe disappears, a second refresh can silently select a different
recommendation; the credential editor also omits the approved private-file
storage disclosure. Both have actual AppKit reproductions. Its positive nine
owner-socket CRUD/review requests, secure-editor/undo clearing and closed gates
passed. Its original failure evidence is preserved; the following separate
checkpoints repair those controls before integration.

The author froze the two-defect repair as `7e702d62` in a new worktree. Independent
re-review cleared both original controls, the 22 repeated-refresh controls and
two actual nine-request owner-socket flows. The latter also consumed the current
host's unlocked-recipe recommendation. Provider calls, agent jobs and ordinary
preparations remained zero; all owners joined and all gates stayed closed.
The private review receipt SHA-256 is
`c01e15753d7fe62dc688e5c8d63738cff9192bc7a7dbfbc3c008786eb4b13e75`.
This narrow clearance preserves the original failure evidence.

A further actual AppKit observation found that
`NSPopUpButton.addItem(withTitle:)` collapses identical titles. Guided recipe
aliases and duplicate provider names can therefore lose their distinct exact
references in the pickers. Repair `7fc0a1387f1809c0ebe7a786d52f9b8a5f4c4373`
uses explicit menu items and bounded identity suffixes, preserving exact recipe,
provider and model references across duplicate labels and refresh. Different-author
review cleared actual AppKit duplicate/collision, cardinality, retained UUID,
model disappearance, exact recipe version/digest and direct Edit controls, plus
the actual current owner-socket flow with zero provider calls. The immutable
private review receipt is
`/private/tmp/mcpme-agent-picker-identity-independent-review-v1.json`, mode `0600`,
SHA-256 `c15f106913050823ffba41d913ed3ad0fc2141f540193b7b0a4de552a1ee31ba`.

The original native setup, sticky-choice/disclosure and picker repairs are
integrated as `079d4ce`, `b0265af` and `916283b`. The combined source passed 55
focused native/wire/provider/recipe-choice checks with no skips in 15.591 seconds.
These checkpoints are pushed on `codex/phase3-llm-integration`.

## Remaining acceptance

Native handoff `937ce1f` independently cleared the original baseline negative
and repaired production parent/socket controls. It is integrated as `4f56d91`;
the scoped review receipt SHA-256 is
`2599e7f06e3440180de1340b2a85109f45bb57cc575c3be5d0b1ad81ff1eb29d`.
Three ordinary form requests covered a pending unlocked proposal and blocked
missing/unsupported proposals; 16 parent denial categories passed. These were
metadata/form reads with four simulated provider calls, zero live calls and
zero resolution/build jobs. See [native handoff](AGENT_NATIVE_RESOLUTION_HANDOFF.md).
Text-generation recipe
1.0.6 is minted and its exact source bindings and historical lifecycle retention
pass root verification; see [recipe refresh review](TEXTGEN_RECIPE_1_0_6_REVIEW.md).
Selected live-provider compatibility, final combined regressions, newly built
native host, signed installed/relocated acceptance and whole-phase adversarial
review remain open. Legacy LocalPolicy rejection envelopes retain
their existing behavior; this repair versions the separate agent/provider route
responses.
