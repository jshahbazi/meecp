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

## Standalone delivery

Standalone code seal, bundled-worker transport, visual inspection and installed
source/runtime identity checks are pending the fresh build. This paragraph will
be replaced with measured results after delivery; source fixtures alone do not
establish those outcomes.

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
