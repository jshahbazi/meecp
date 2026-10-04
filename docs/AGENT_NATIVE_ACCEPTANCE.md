# Native generated-adapter acceptance

This developer-only fixture drives the production native agent controls and the
ordinary preparation controls against a private packaged manager. It does not
enable a public execution path or establish live-provider compatibility.

`--agent-native-acceptance` accepts a closed, owner-only fixture. A short-lived
marker binds its nonce to the existing manager file identity. The fixture,
marker, workspace and state identities are checked before requests and before
responses are consumed. Nonces are consumed once. Reports are created exclusively
with mode `0600`. The route fence admits agent review/start, ordinary form,
resolution and preparation actions; it does not admit installation or provider
configuration. The separate Python harness performs explicit installation.

The native driver selects a simulated provider, a model, an exact enabled recipe,
source-content paths and disjoint metadata-only paths. It calls the actual
`AgentPreparationView` review and send handlers. A succeeded, settled proposal
with no candidate must reach the production parent's **Review Preparation**
callback at the same saved revision. Only then does the driver review and start
ordinary dependency resolution, answer the generated form, review preparation,
and start the governed build. The attachment must match the reviewed revision
and package digest.

`scripts/agent_acceptance_fixture.py` is a narrow request-only simulation. It
parses the production backend's encoded reviewed source and trusted role guide.
It derives the function, argument, arithmetic factor, tool schema and fixture
from those bytes. A reviewed `humanize` import produces an unpinned requirement;
the ordinary resolver chooses and locks its version. No lock hash, pinned version,
source contents or generated proposal is supplied through a hidden closure.
The simulation uses `httpx.MockTransport` and a synthetic nonsecret credential.

`scripts/verify_phase3_native_agent.py` admits only the existing exact unlocked
agent family through explicit trusted factories. The ordinary packaged CLI
remains disabled. Its scalar and dependency cases require real native requests,
four simulated provider calls, actual governed resolution and preparation, a
matching attachment, installation, real HTTP and stdio SDK calls, source removal,
restart, and a confined SDK probe of the relocated package. The original unknown
binary asset must survive unchanged. Raw child output stays in memory. Failure
to prove ownership settlement retains the isolated workspace.

The source controls include compiled rejection cases for unknown fields,
unsupported disclosure scope, overlapping/duplicate paths, path escapes, bad
recipe identity, expired or mismatched markers, manager inode replacement,
hardlinked/private-mode failures, nonce replay, and existing/dangling outputs.
An initial draft test caught a replay that could write a failure report; replay
now returns before reporting. A test invocation with a nonexistent base parent
failed before its fixtures ran and was repeated in a fresh private directory.

The initial composed runs also exposed two harness assumptions: the native
route fence needed the existing preparation/resolution collection polling
routes, and CLI intake needed explicit automatic mode to preserve unknown
binary files. Those failed receipts remain immutable. The schema-2 relocation
check uses the ordinary version-aware descriptor parser.

The corrected preliminary composition passed both cases using the frozen signed
A1 host and an ad hoc native binary compiled from this slice. Each case completed
four simulated calls, matching attachment, two HTTP and two stdio calls across
restart, and the relocated confined probe after captured-source removal. All
owners joined. Its receipt SHA-256 is
`4f47e195ed17014b9047cbce7182384a070334806ea122b60a79f559656cbd88`.
The focused native, fixture, owner-wire, relocation and prepared-Python signing
checks passed 57 tests. Their receipt SHA-256 is
`1f0ff23921bc5d8e62a086a43109f79bb355e9953ca3daf70a6679cebf4bc8ba`.
A matching newly signed composition is a subsequent acceptance step.

Physical dialog interaction, live credentials/provider selection, real external
provider behavior, public execution, and whole-phase adversarial review remain
open. No backend, published component, recipe, or signed runtime profile is
changed by this native acceptance slice.
