# Automated onboarding review

Date: 2026-10-14. Boundary: [PR #60](https://github.com/jshahbazi/meecp/pull/60),
initial head `5ac7d23518a0531b0ba7d3f81d014330a5f52133` against
`f0cf2b46df988994b0a12317cd625473c008801b`, followed by the corrective changes
recorded below. This is a source, integration and scoped platform review.

Corrective implementation and installed build:
`69cba31a16925dfff3486ceab9b5dcaf3ce8faea`. The later receipt update changes
documentation only.

Verdict: **Conforms within the recorded local acceptance scope after correction.**
The initial implementation had concrete correctness and confinement defects;
passing earlier checks did not establish those failure paths. Physical
first-time Metal component acquisition remains unverified on a fresh Mac.

## Governing requirements and evidence

The baseline is [the seven onboarding contracts](AUTOMATED_APP_ONBOARDING.md),
[the host design](MEECP_DESIGN.md), and the repository instructions on ownership,
authentication, privacy, artifact identity and resource accounting. The user's
implementation request authorizes corrections within that scope. Broader model
quality, multiple resident GPU scheduling, installed-package isolation and
commercial qualification remain outside this addition.

| Requirement | Implementation and checked evidence | Status |
| --- | --- | --- |
| Contract 1: retain independent verified resources and reuse downloads | `pi_resources.py` plus `PiSetup.capture_resources`, `_retain_download` and `_fetch_url`; archive-format reuse, original-path denial, hash/catalog identity and cancellation regressions; actual 5.1 GB capture/recovery acceptance. | Conforms |
| Contract 2: fresh-session resume and exact draft/source reconciliation | `pi_recovery.py`, `PiSetup.resume`/`matching_job`, and `PiSetupView.bind`/`poll`; fresh-host resource/file/opaque-credential recovery, stale binding, duplicate resume and late UI reply checks. | Conforms |
| Contract 3: distinguish compiler, GPU and host budget readiness | `pi_native.preflight`, `ToolchainManager.ensure` and `PiSetup._run`; actual confined shader/compute acceptance, compiler-unavailable prebuilt coverage, settled timeout and ownership-loss checks. | Conforms |
| Contract 4: trusted tool selection and automatic component continuation | `pi_toolchain.py` discovery/selection/acquisition plus `PiSetup._continue_system`; replacement/revocation, fixed Apple command, bounded acquisition failure, first-launch readiness and continuation checks. Actual selected developer tools and Git TLS run in confinement. | Conforms within the provisioning boundary below |
| Contract 5: managed native admission and owned cleanup | `PiSetup._native_command`, `_resource_work`, `PiToolBroker` and `ProcessOwner`; exclusive admission, cancellation, retained uncertain ownership, listener confinement, synthetic cleanup sentinels and mutable-input disk ceiling checks. | Conforms |
| Contract 6: retry the exact prepared package and settings | `installation_retry.py` and native retry controls; coalescing, stale/corrupt/recovery-required rejection, preserved settings/start policy, and real authenticated MCP installation with zero Pi calls. | Conforms |
| Contract 7: authoritative progress and supported recovery actions | `PiSetupView`, `PiResourcesView` and setup stage reporting; release UI regressions for revision changes, starting during polling, opaque archive selection and continuation/cancellation controls. | Conforms |
| Cross-cutting host boundaries | Existing `LocalPolicy` authenticates routes and enforces Host/Origin; new Pi management routes remain owner-only. Tool discovery bypasses only obsolete toolchain pins, retaining workspace/runtime-root and process ownership checks. Generic commands retain GPU denial. Public provenance and aggregate outcomes are recorded; raw model/command data and credentials are excluded. | Conforms within the reviewed paths |

## Findings corrected

| Priority | Trigger and consequence | Correction and regression |
| --- | --- | --- |
| P1 | A background process replaced `.toolchain` with an external symlink during compiler probing; host cleanup could delete external files with the fixed probe names. | Hold the original no-follow directory descriptor; inspect and delete only relative to it. External sentinels survive failure, false success, timeout and ownership-loss cases. |
| P2 | Apple installation replaced a pinned Xcode/SDK/Metal directory; the old pin prevented discovery from checking its replacement. | Fixed host discovery can reinspect trusted candidates while ordinary commands reject stale pins. Removal/untrusted replacement revokes previous roots, commands and environment entries. |
| P2 | An optional compiler probe hit a settled timeout/output limit and aborted CPU or prebuilt setup. | Report compiler unavailability or try another candidate; cancellation, uncertain ownership and unclassified launch failures still propagate. |
| P2 | Component acquisition hit a settled timeout/output bound, or its trusted installation disappeared while acquiring. | Return the existing OS continuation with a fixed failure code; revoke stale provenance/grants when reinspection fails. |
| P2 | The Apple `/usr/bin/git` launcher failed inside confinement despite selecting Xcode. | Discover and Apple-verify actual developer tools; use their paths for advertised capabilities, known argv and child PATH while preserving bundled runtime precedence and C++ driver alias semantics. Real Git TLS and owned start/read/stop pass. |
| P2 | Cancel arrived while resource import was queued or waiting for prior setup cleanup; input still captured and advanced the draft. | Allocate request cancellation before those waits, join duplicate requests, and cancel before input reads/publication. |
| P2 | Backend download retention and the native picker omitted supported opaque archive suffixes. | Align with the host archive contract. All supported suffixes retain verified bytes and reuse without another network transfer; containers remain unopened during capture. |
| P2 | A selected input or checkpoint grew after estimation and consumed disk budget charged to another operation or to fresh source files. | Bound actual work to its admitted charge and isolate the checkpoint subbudget. Growth is rejected before copying, publication or a new agent session. |
| P2 | A draft revision change or setup start superseded a pending current-job lookup but left the UI polling latch set. | Invalidate the request epoch and release the latch. Late null/ready responses cannot replace the new attempt or stop subsequent polling. |

Port tests also now select an actually available local port instead of assuming
19386 is unused. The real Git TLS fixture performs the same developer discovery
as production setup. These changes preserve the assertions while removing
environment-dependent failures.

## Verification and limits

The integrated backend run passed 321 checks before the final held-directory
cleanup correction; the affected toolchain/native/recovery paths were rechecked
after that correction: all 95 affected checks passed. Redirect/port checks passed 61 tests, and all nine real
Pi SDK worker checks passed. Release compilation and both native UI self-tests
passed. See [the acceptance record](AUTOMATED_APP_ONBOARDING_VALIDATION.md) for
final installed-build checks and the exact artifact boundary.

No live user state, GPU inference or toolchain installation was used by the
independent reviewers. Native acceptance is confined to the development Apple
silicon Mac and pinned Stark/ColGrep fixtures. Apple component acquisition and
first-launch failure paths use controlled runners because the component is
already installed; the review does not assert a physical fresh-Mac install.
Apple account/license decisions remain user actions. Broad localization quality,
performance and unfamiliar-project onboarding success remain unqualified.
