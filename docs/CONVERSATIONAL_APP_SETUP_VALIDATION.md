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
