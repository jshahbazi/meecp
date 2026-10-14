# Conversational Add App acceptance

Date: 2026-10-14. Scope: the approved Focused conversation mock-up integrated
with native Add App, initial agent settings, live agent messages and existing
Manage Apps/package workflows. The owning contract is
[Conversational Add App](CONVERSATIONAL_APP_SETUP.md).

## Source qualification

The release Swift build passes. The native fixtures below pass:

- `--add-app-conversation-ui-self-test`: actual workspace composition, inline
  initial provider save/continuation, stale callbacks, secure answers, question
  suggestions, bounded readback, live-message identity retries, reply versus
  durable draft intent, explicit request editing, revision changes and
  Manage Apps/Add App navigation.
- `--management-ui-self-test`: existing installed-app, source, recovery,
  configuration, capture and setup controls.
- `--pi-setup-ui-self-test`: existing exact-bound question and preparation UI.
- `--pi-resources-ui-self-test`: retained-resource selection/capture controls.

The host and worker checks exercise actual pinned SDK steering against a local
synthetic provider. They cover acceptance while an owned host tool is blocked,
terminal steering races, startup steering, cancellation and tool cleanup,
idempotent UUID messages, exact draft/source fences, owner/Host/Origin checks,
history bounds/expiry and absence of durable conversation text. Secret values,
thinking, tool results and raw provider errors stay outside the UI transcript.
The pinned SDK worker suite passes all twelve fixtures.

The host command
`uv run --locked pytest -q tests/test_pi_conversation.py tests/test_pi_setup.py tests/test_pi_questions.py tests/test_pi_onboarding_recovery.py`
passes 97 cases with no skips, including both real SDK duplex cases. The worker
command `node --test agents/pi/worker.test.mjs` passes twelve cases with no skips.
Focused Ruff checks (`F,UP,B`) and `git diff --check` pass.

The initial app request is retained as the durable draft intent. Ordinary replies
use separate transient storage and do not increment its revision or invalidate
a verified plan. Source selection, resource capture and explicit request editing
retain the existing draft revision and fresh-attempt rules. Required document
role selection opens its source controls rather than remaining hidden.
Conversation polling preserves existing message views, including selected text,
and adds/removes only changed or evicted events. Native fixtures check message
view identity across unchanged polls, appends and eviction. Tab sizing fixtures
check that the conversation fills its workspace before and after resizing.
The first-run fixture disables Create app for an empty request. A lazy-window
fixture loads the authoritative draft before creating the management window,
then checks the restored intent, source, document role and startup choices. An
explicit deletion still marks the request dirty; restoration does not mask edits.

## Standalone delivery

The standalone app built from clean implementation commit
`42d3a4139a1fb87de076600b784aed8e3ac94185` was installed at
`/Applications/meecp.app` and relaunched through the existing verified native
installer. The delivered and installed copies pass deep strict code-signature
verification, the host's complete artifact inventory, the Pi runtime inventory,
native Metal helper receipt checks and current implementation source hashes.
The native input hash matches the builder's compiled binary. The installed
runtime's Pi inventory is identical to the candidate on which all twelve real
SDK worker fixtures passed; subsequent changes affect native UI only.

The delivered app passes the combined conversational and existing management
native self-tests. Earlier standalone candidates also passed the existing Pi
setup/resource self-tests. Native visual inspection checked Manage Apps,
full-width Add App, the pinned composer and the initial inline provider/model
form using an isolated local state directory. Entering a public synthetic app
request saved it before showing initial agent settings; canceling settings kept
the draft. No provider key or paid model request was used in that UI check.

The same isolated state exposed the lazy-window restoration bug: the backend
and authenticated owner API retained the complete draft while the newly opened
conversation remained empty. The final native fixture reproduces loading the
record before creating the window and verifies all new controls hydrate from
that record. Final interactive reinspection was unavailable after the Mac
locked; the restoration and subsequent UI refinements are qualified by native
fixtures and the shipped code/source identity checks.

Before updating, the owner API confirmed no active setup, installation operation
or tool request. The prior running-app identities, ports and startup policies
were captured as task-local metadata. After relaunch the owner API returned 200,
and the previously running ColGrep app was restored to Ready with its original
manual startup policy and public port 19389. The isolated UI-check host stopped
and its manager connection was removed during the update. User drafts and
retained resources were preserved.

This acceptance record is a documentation follow-up to the installed source
commit; it does not change the delivered implementation or rebuild historical
artifacts.

## Compact layout refinement

The follow-up implementation at clean commit
`e9dfe0824e6326d62511abaf2ef271512b5ec423` replaces the large empty band
between a new request and its composer with a compact form. The content column
is capped at 900 points. The outer column has an explicit native frame so a
hidden form's preferred width cannot shrink the tab workspace. Active
conversations and installation review retain the expanding thread.

Agent settings is a rounded card capped at 600 points, with grouped provider
and model fields and optional Advanced endpoint controls. The ordinary composer
is hidden while settings or a secure-input step is active; its text is retained.
Cancel restores editable request controls immediately. App details groups Source
and Saved files, with shorter copy and no stale reconnect notice for a new draft.

The release build and all four native self-tests above pass, including from the
standalone bundle. Added checks cover compact height, full tab sizing before and
after resize, settings disclosure expanding the viewport, retained composer text,
the 600-point form and narrow stacked fields, hidden endpoint preservation and
existing-versus-initial save actions. An independent source review found no
concrete lifecycle or credential regression.

Live native inspection used an isolated host state and then the installed app.
It checked the compact request form, opening and closing App details, first-time
and saved agent configuration, expanded Advanced fields, and visible form
actions without a disabled message area or a scroll jump. No provider key was
entered and no model request was sent during this UI inspection.

The standalone build was installed at `/Applications/meecp.app` and relaunched
through the verified native installer. Candidate and installed receipts match
this clean implementation commit. Complete host and worker inventories, native
input/source hashes, Metal helper receipts and deep strict code signatures pass;
the installed native executable is byte-identical to the qualified candidate.
The owner API returned 200, and the previously running ColGrep app returned to
Ready with its original startup policy and port. Backend/SDK source was unchanged
in this UI follow-up; its earlier transport and model qualification retains its
original scope.

## Native workflow retirement

Clean implementation commit `7460e5f7f61c8130aa26882090074ee0101d0144`
retires native Manual setup and Have your agent do it. Their integration is
preserved as comments in
[LegacyAddAppWorkflowReference.swift](../Sources/MeecpMenuBar/LegacyAddAppWorkflowReference.swift),
with the retired presentation snippets in
[AddAppConversationView.swift](../Sources/MeecpMenuBar/AddAppConversationView.swift).
The native app no longer constructs, wires, polls or restores those workflow
controllers. The attachment menu offers only source, link, model/runtime files
and prepared-package selection. Shared agent setup, resources, configuration,
installation/upgrade/recovery and Manage Apps remain active. Low-level
compatibility types and separately authorized backend/CLI APIs retain their
existing scope.

The release build and all four current native UI self-tests pass, both from the
source executable and standalone bundle. The management fixture now checks the
active agent/shared controls and absence of retired controllers and Objective-C
actions. Historical manual preparation/capture checks no longer form part of
that current management result. The conversation fixture checks actionable
legacy-reference-draft handling, retained intent, disabled reference onboarding,
and model-file routing to resources. Nine retired native UI/operational
entrypoints each return exit 2 with `native_setup_workflow_retired` before any
old workflow begins.

Independent review identified the remaining GGUF source-picker path and
ambiguous reference-document choice. The source picker now offers folders,
apps/packages and text; dropped model files receive guidance for the active
resource action. Saved reference-role values remain readable, while the retired
reference preparation option is disabled for new selections.

Before replacement, the owner API confirmed terminal legacy preparation,
resolution, capture and publisher-download jobs, no preparation/capture/
resolution reservations, and no active setup, installation or tool request.
The standalone bundle was installed and relaunched. Candidate and installed
copies pass complete host/worker inventories, current source/native input
hashes, Metal helper receipts and deep strict signatures; the installed native
executable is byte-identical to the qualified candidate. Live inspection of the
installed Add menu confirms the alternative workflow entry is absent. The
owner API returned 200 and the running ColGrep app returned to Ready with its
original startup policy and port. Saved draft identifiers and legacy capture/
download references are retained as inert metadata and do not resume old native
workflows.

This documentation follow-up does not change the installed implementation.
The prior transport, model and Metal acceptance records retain their scope;
this retirement does not repeat or expand those claims.

## PR integration with current main

The PR preparation merges main at
`bdfa708` to retain its runtime ownership recovery, Apple-verified Xcode launcher,
and current native capability fixtures. The automation work already merged in
PR #60 is outside the new conversation UI diff. The verified Xcode launcher is
kept in the conversation's system-action path with current host exclusions and
stale-callback guards.

The former manual/external-agent native acceptance cases are retained as
explicitly skipped historical checks. Shared backend and current agent checks
remain active. Historical command-line acceptance scripts reach the retired
native entrypoints and cannot establish current workflow acceptance.

Swift, JavaScript and Python source syntax checks and the staged whitespace
check pass. No tests, builds, installation or model runs were repeated during
PR preparation, as requested. The preceding installed acceptance records apply
to their recorded commits; they do not qualify the merged PR tree.

## PR CI follow-up

The first PR CI run, `38623955883`, reported 89 failures. The conversation
changes modify the complete Python source closure used by TextGen components;
87 recipe/policy/lifecycle checks rejected the preceding immutable identities.
TextGen component 1.0.24 and recipe 1.0.25 bind the changed source, with exact
catalog and release-policy references. Earlier recipe directories and their
model/runtime provenance remain unchanged. Current hashes and native/signed
acceptance limits are recorded in [release authority](PHASE3_RELEASE_POLICY.md).

The remaining two failures also occur on main at `bdfa708`. Their old hardware
control established only that an unconfined Metal device existed. A separate
fixed test control now verifies the required residency feature family, residency
allocation, shader execution and exact arithmetic before confined acceptance.
Only an absent device or an explicitly unsupported feature family can skip;
unknown statuses, control failures and target sandbox failures remain fatal.
This changes test admission without widening the production GPU sandbox.

After the CI failure report, 345 focused recipe/policy/upgrade checks passed,
with 492 explicitly opt-in scanner integrations skipped. The Metal classifier
and both native target checks passed locally: 30 checks total. These runs
preserved the installed app state. They qualify the focused repairs on this
Mac; hosted CI must independently establish the runner outcome. No full local
suite, model quality campaign or standalone app installation was repeated.

## Installer CI residency admission follow-up

Signed installer run `38639559404` failed before importing Apple credentials:
5,621 tests passed, 615 skipped, and the two Metal acceptance fixtures failed
with `metal_compute_control_failed_residency`. The unconfined control passed the
GPU family check but could not create a residency set. The run did not retain
Metal's error domain/code. Follow-up hosted run `38647206410` established that
both the residency set and `NSError` were nil (`residency_none_0`). A nil error
does not prove an unsupported feature and remains a fatal hardware-control
failure. The repository has no registered hardware runner.

The control now maps only `MTLDeviceErrorDomain` with
`MTLDeviceErrorNotSupported` to a distinct unsupported-hardware status (`77`).
Apple documents that error as an unsupported device feature; the classification
requires the macOS 26.4 SDK and runtime. Missing errors, other domains/codes,
unexpected output, and subsequent compute failures remain fatal. Production
helpers and confined executions still cannot classify their failures as skips.

The native regression check exercises the exact classifier with real `NSError`
instances, including nil, incorrect-domain, and unknown-code errors. It and the
Python admission checks passed locally together with both previously failing
native target checks: 39 passed after adding bounded error-identifier
diagnostics. Error descriptions and `userInfo` are never emitted. This
establishes local classification and compute behavior.

The two physical GPU acceptance tests now carry the `metal_hardware` marker.
The default local suite and `./test.sh` still run them. Hosted CI and the signed
installer explicitly select `-m "not metal_hardware"` and run the classifier
regressions before that suite. They retain all other standard tests and import
Apple credentials only after testing. This hosted profile does not qualify GPU
execution; the local hardware checks provide that scoped evidence. A future
qualified hardware runner should run `pytest -m metal_hardware` separately.

## Limits

These fixtures qualify UI, transport and lifecycle behavior. They do not measure
model instruction-following, general unfamiliar-app setup success, or ColGrep
localization quality. The prior
[automated onboarding acceptance](AUTOMATED_APP_ONBOARDING_VALIDATION.md)
owns native Metal execution and retained-resource qualification. This change
reuses those host actions and does not repeat its model/Metal campaign.

Conversation text is bounded RAM state. Ready/failed history expires after five
minutes; canceled/interrupted history and host restart clear it. Saved drafts,
retained files and authoritative attempt metadata remain available. Resumption
starts a fresh model session.
