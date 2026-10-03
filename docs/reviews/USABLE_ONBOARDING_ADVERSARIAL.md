# Usable onboarding: adversarial review and fixes

Date: 2026-10-03

The initial onboarding change at `7d131b5` added a bundled JSON Utilities app and moved publisher downloads behind an explicit choice. An independent Astra agent examined that change against `origin/main`, including the executable starter and distribution contents. Four actionable findings were reproduced or established from the request lifecycle. All four were fixed and reviewed again.

| Finding | Resolution | Evidence |
| --- | --- | --- |
| Host restart could leave setup and download requests marked busy after their stale callbacks were suppressed. | Clear all host request state at termination and start, while retaining generation checks that reject stale responses. | Native management self-test clears each flag and pending set. Live isolated host termination, Retry Connection, and a new starter draft completed. |
| The JSON example accepted trailing commas and rejected legal large numbers. | Validate RFC 8259 syntax with an iterative scanner that does not materialize numbers. Preserve the exact document string when decoding its request. Bound input to 256 KiB UTF-8 and 512 containers; limits return a fixed tool error. | Tests use the real compiled executable: trailing commas, malformed numbers and escapes, `1e400`, huge exponents, Unicode, escaped surrogates, BOM rejection, exact limits, and continued operation after errors. Manifest and advertised tool descriptions match. |
| Missing required configuration or secret references prevented saving an attached draft and navigating to another app. | Permit valid partial choices in draft revisions while preserving the verified attachment and recalculating blockers. Planning and installation keep strict completeness and secret availability checks. Invalid entered values offer Keep Editing or Discard Edits and Continue. | Backend persistence, stale revision, invalid value, missing reference, revoked reference, plan and apply tests. Native partial settings self-test. Live rename of an incomplete draft, starter selection, saved draft resumption, blocked review, and both navigation choices. |
| A linked review document was omitted from distributed documentation. | Copy documentation recursively into the native app and standalone host; record nested host documentation and asset hashes. Preserve cache exclusions and signing order. | Focused app and host packaging tests verify nested files, references, receipts, and signed reaper hashes. |

The fix review independently passed 65 focused tests and reported no remaining actionable defect in those four fixes. Live native checks then exposed a fifth problem in the saved draft selector. A list poll begun before a draft save or creation could temporarily omit the current draft or report its prior state. Both reviewers also reproduced AppKit's collapse of identical popup titles, which broke selection when two JSON Utilities drafts shared a name and state. The selector now inserts separate menu items, selects by represented draft ID, and retains current metadata when inventory is missing or has an equal or older revision. Native self-tests cover missing, stale, equal-revision stale-state, newer, and duplicate-title inventory entries. The final adversarial pass examined this fix, independently ran the rebuilt management self-test, and reported no remaining actionable findings.

Validation of the implemented changes:

- Full Python suite: 1,514 passed; one case-sensitive filesystem check skipped on this volume.
- Swift suite: 18 passed; three native installer lifecycle tests remain opt-in and were skipped.
- Native contract and management UI self-tests passed.
- Bundled-host acceptance: all 16 checks passed, with four actual HTTP tool calls and one stdio call, two independent slots, minimal environment, relocation, source snapshot independence, restart, credential isolation, and cleanup.
- Native UI: included starter reached Ready; incomplete drafts stayed saved; invalid unsaved edits followed the selected keep/discard choice; host reconnection restored onboarding.
- Secret scan and whitespace checks passed.

The native acceptance run used a fresh private temporary installation. Its host connection was removed and owned process ledgers were empty after Quit. No production installation was replaced.

The review also identified a future architectural requirement for service-only apps. [Automatic preparation](../AUTOMATIC_PREPARATION.md) now calls for a versioned service-connection identity containing nonsecret settings and opaque credential references, followed by a sealed generated adapter and actual connector validation. Directory preparation, remote provider connectors, and automatic execution are still implementation work. The modular roadmap places automatic preparation in Phase 3 and multiple model engines in Phase 4; its design does not establish runtime acceptance for GLM, DeepSeek, Bedrock, or local inference services.

Development app and DMG checks establish local build and installation behavior. Public Developer ID signing, notarization, and release publication require the release workflow.
