# Automated app onboarding and Metal preparation

Status: implementation in progress, 2026-10-14. This plan owns the improvements requested after the Stark/ColGrep onboarding journey. MeeCP owns acquisition, environment preparation, verification and recovery; user interaction is limited to missing task requirements, credentials and unavoidable operating-system dialogs.

## Contracts

1. Setup resources are separate from text source intake. Selected large model files, runtime trees and archives are captured into private immutable storage with content identity, origin and license metadata. Downloads and prepared native dependencies can become reusable resources. Resources belong to the import identity, survive failed/interrupted attempts, and are mounted inside each attempt's permitted workspace. Existing large-artifact capture bounds, identity checks and disk admission apply; do not raise text intake limits.
2. Interrupted setup is distinct from explicit cancellation. A resume creates a new attempt and a fresh agent session using the exact current draft/source binding, retained working files and verified resource identities. It does not replay a conversation or resurrect discovered processes. Repeated resume requests join the unique matching active attempt. The UI reconciles jobs and installation operations by draft identity and follows the authoritative active/ready attempt.
3. Preparation begins with a host capability report: actual selected tools, Metal compiler/SDK availability, native device/compute readiness, memory/disk budgets, acquisition facilities and resource inventory. Compiler availability is independent of runtime GPU access. Unsupported engine operations remain real implementation work and cannot be silently substituted.
4. MeeCP selects a functioning installed developer toolchain without changing global `xcode-select`. It automatically acquires the supported Metal component through Apple's documented Xcode command when needed and available. If Apple's installer or account/license flow requires user action, MeeCP presents the native action and continues after tools become available. A verified prebuilt runtime containing compiled Metal assets avoids a compiler requirement. Toolchain provenance records the actual selected identities.
5. GPU preparation uses an explicit host-managed native command capability with bounded argv, workspace access, memory/time limits, exclusive GPU admission, loopback-only listeners and owned descendant cleanup. Pi cannot widen the generic command sandbox. Host resource accounting covers native preparation alongside existing probes, captures, preparations, resolutions and installed apps. Real engine/model and search fixtures remain required by package verification.
6. A terminal installation failure exposes Retry Installation. The host creates a fresh prepared-package draft and plan from the same verified bytes and effective settings, retaining the original attempt as history. Duplicate retries coalesce. Recovery-required operations follow their existing recovery path. The selected package/settings/start policy cannot change silently; installation authority remains exact-plan bound.
7. Progress names the actual stage and offers the next supported recovery action. Technical codes remain available in details. Host failures are repaired or classified before asking the user to diagnose compiler, GPU, filesystem or port policies. A completed stage and preserved artifact must not be needlessly repeated.

## Implementation ownership

- Host resource store and clone-aware package materialization.
- Host Metal/toolchain discovery, provisioning and managed native execution.
- Setup resume, preflight and authoritative identity reconciliation.
- Installation retry composed from existing immutable draft/plan APIs.
- Native resource picker, progress/recovery controls and OS-action continuation.

## Acceptance

Exercise a fresh GUI-shaped environment, large local and downloaded resources, an interrupted download/setup and host restart, explicit cancellation, stale draft/source rejection, concurrent resume/retry requests, an occupied registered port, a terminal installation failure and a recovery-required operation. Verify replay uses retained artifact identities, installation retries make no setup-model calls, and native GPU reservations are retained until owned processes settle. Test compiler selection and provisioning independently from a real Metal compute probe, actual Stark model loading/generation and ColGrep semantic indexing/search. Build the standalone app, verify its seal, and perform native UI recovery checks. Retain only aggregate outcomes and artifact/provenance identities; no prompts, responses, tokens, raw command output or credentials.

The commercial two-Mac benchmark, broad engine/model quality qualification, multiple resident GPU scheduling, automatic host updates and full Xcode/account installation remain governed by their existing plans. This work supplies automated onboarding facilities and their scoped acceptance.
