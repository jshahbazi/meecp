# Conversational Add App

Status: implemented in current source, 2026-10-14. This plan owns the approved
Focused conversation mock-up and its integration with native MeeCP. Manage
Apps retains its installed-app list, lifecycle controls and connection details.
Add App uses one conversation for initial requirements, agent configuration,
preparation, questions, recovery and installation review.

## Contracts

1. The native management window retains separate Installed Apps and Add App
   workspaces. Menu-bar and installed-app Add actions open the same persistent
   Add App workspace. Changing workspaces preserves the exact draft and owned
   preparation; closing the window does not stop the host job.
2. The initial composer accepts an app description, with optional source/link,
   model/runtime resource or prepared-package selection. Initial provider/model
   configuration appears inline when needed. A user-authorized setup continues
   after those settings are saved, without losing the original brief or source
   binding. Configuration alone sends no source to a provider. Model files,
   including GGUF, use **Model or runtime files…** with an app description;
   they are not text-source selections.
3. The accepted draft intent is distinct from subsequent conversation text.
   Editing or sending a reply cannot silently revise the durable draft, alter
   its source revision, or invalidate package readiness. Explicitly editing the
   request follows the existing draft revision and fresh-attempt contracts.
4. Free-form messages guide the actual active Pi session through the pinned
   SDK's steering API. Submission and readback require owner authority and the
   exact job/import/revision/source binding. Pending questions use their exact
   answer route. Credentials use secure fields and opaque private references.
5. Live user and assistant text is transient conversation data, bounded in
   memory and plain text in the native UI. Only completed assistant text is
   exposed; thinking, tool output, raw provider objects, keys and secret answers
   are excluded. No transcript enters job files, events, diagnostics,
   UserDefaults or recovery checkpoints. After a host restart, the UI shows the
   authoritative job and retained files rather than reconstructing a transcript.
6. Host progress and inline actions reflect current state. Apple handoffs use
   the existing verified OS action and readiness continuation. Resource
   selection/capture, cancellation, admission, fresh-session resume and
   installation retries retain their owning lifecycle and identity checks.
7. A verified package uses the existing configuration and exact installation
   plan. Its tools, declared access, startup choices and required settings remain
   reviewable. Installation progress and failed/recovery-required operations
   follow the existing controls; they do not become simulated chat outcomes.
8. The MeeCP agent is the only native preparation workflow. Source/link and
   resource selection, prepared-package import, configuration, review,
   installation and recovery remain available in the conversation. Importing
   an already prepared package can proceed directly to review without a Pi
   provider. Manual setup and **Have your agent do it** have no native controls,
   controller wiring, polling or restored execution.

## Implementation and acceptance

The original integration and compact layout refinement were qualified in
[the acceptance record](CONVERSATIONAL_APP_SETUP_VALIDATION.md). That record
describes the source and delivered builds checked at each checkpoint; its
earlier method-navigation checks do not establish acceptance for the subsequent
native workflow retirement described below. Standalone delivery qualification
is recorded separately from source checks.

- Native AppKit conversation surface composed with the existing Pi, resource,
  configuration, package review and management controllers.
- Owner-only bounded in-memory conversation/steering transport with request
  identity, exact context, cursor reset and terminal retention limits.
- Inline provider configuration, suggested question choices and secure answers.
- Real pinned SDK steering and completed-message tests using a synthetic local
  provider; no live account or model is needed for transport qualification.
- Native checks for initial setup, configured/unconfigured providers, reply vs
  draft intent, stale callbacks, question changes, secret clearing, host restart,
  saved-source/resource revision changes, package review, and workspace navigation.
- Build/seal the standalone app and check Manage Apps and Add App navigation.
  Preserve the user's installed runtime state across the update.

Broad setup success rates and model quality remain outside this UI change.

## Layout refinement

New requests and saved drafts without a conversation use a compact layout: the
composer follows the visible content, with unused height below the form. Once
an agent conversation or installation review exists, the thread expands and
the composer stays at the bottom. The content column is bounded to 900 points
and remains usable at the management window's minimum size.

Agent settings uses a native card bounded to 600 points, grouped provider and
model fields, and a secure API-key field. Advanced settings discloses a custom
endpoint and opens automatically for a saved endpoint or validation error.
Collapsing Advanced preserves its value. First-time configuration uses Continue;
editing an existing configuration uses Save settings. Credential clearing and
exact-draft continuation retain their existing contracts.
The ordinary composer is hidden during settings and secure-input steps; its
text is retained and reappears when the step closes.

App details groups Source and Saved files separately. It no longer presents
unlabelled intake and technical information as one broad Details section.

## Native workflow retirement

On 2026-10-14, the user requested retirement of the native Manual setup and
**Have your agent do it** workflows. Their native integration, actions, polling,
restoration and layouts are preserved as commented reference material in
[LegacyAddAppWorkflowReference.swift](../Sources/MeecpMenuBar/LegacyAddAppWorkflowReference.swift)
and the retired alternative-method section of
[AddAppConversationView.swift](../Sources/MeecpMenuBar/AddAppConversationView.swift).
Legacy implementation types remain compiled for isolated compatibility
fixtures. The app does not instantiate or wire them, poll or restore their
workflows, or expose them through Add App. Their former native acceptance
entrypoints report that the workflow is retired.

Saved drafts and prepared packages are retained. A legacy incomplete draft
whose source kind Pi cannot prepare shows an actionable choice to create a new
agent draft with source/instructions or import an already prepared package.
It does not start an old preparation controller or automatically resume a
legacy attempt. Compatible agent drafts keep the normal explicit resume path;
verified package review and installation recovery keep their existing paths.

The host's preparation and import APIs, CLI and external-management
authorization are separate compatibility capabilities. They retain their
existing source, draft, grant and exact-plan checks. Retiring native workflow
controls does not remove those contracts or broaden their authority.

