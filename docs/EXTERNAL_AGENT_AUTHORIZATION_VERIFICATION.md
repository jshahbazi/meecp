# Native external-agent authorization verification

Date: 2026-10-07. Baseline: `main` at `38a109d`; implementation branch: `codex/external-agent-authorization-plan`.

## Original authorization implementation

The management export uses the installed launcher, an optional state selector, and a public profile UUID. It exports no credential file, bearer, or secret environment variable. The stdio facade initializes locally and exposes 42 static tools. After initialization, the separate private Unix bootstrap listener accepts bounded consent requests. Owner-authenticated native decisions publish a fresh credential only to its bound live connection. The reused grant ledger rejects prepared credentials until acknowledgment, and checks lease, generation, policy revision, expiry, and revocation on calls.

Remembered policy is explicitly scoped to this installation and macOS account. Reported client names are display labels. Verified application identity is not claimed. Base access allows inspection and optional setup planning; exact installation, preparation, and resolution authority requires a separate authoritative plan review. Approval does not replay a call. Internal Pi APIs remain owner-only. Explicit credential-file connections retain compatibility.

Registry schema 5 prevents an older host from opening state that includes leased session authority. A durable session marker also rejects missing broker associations or guards. Do not lower the registry version manually to downgrade; use a preserved pre-migration installation.

## Automated checks

```sh
uv run --locked pytest -q --disable-warnings \
  tests/test_native_authorization.py \
  tests/test_management_authorization.py \
  tests/test_management_authorization_facade.py \
  tests/test_native_authorization_ui.py \
  tests/test_meecp_management_cli.py \
  tests/test_meecp_management_mcp.py \
  tests/test_meecp_grants.py \
  tests/test_management_workflow_grants.py \
  tests/test_local_transport.py \
  tests/test_native_private_transport.py \
  tests/test_pi_setup.py \
  tests/test_meecp_registry.py
swift test
```

Result: **272 Python tests passed in 127.72 seconds**. The 42 existing protected-fixture cleanup warnings did not contain assertion failures. Swift: **18 passed, 3 installation lifecycle tests skipped**, zero failures. Native AppKit authorization and Pi self-tests are included in the Python result.

Coverage includes real OS peer credentials/process-start identity, private socket safety, prepared/ACK delivery and loss, request replay on another connection, approval/disconnect races, policy persistence across host restart, scope reduction/increase, renewal, revocation, missing associations, copied/unknown profile rejection, exact preparation/resolution/install bindings, authoritative review effects, every Pi route across base/action and seven legacy grant modes, credential absence, and malformed owner-plus-grant principals. Real SDK sessions establish immediate startup with a missing host or pending approval, use an approved session, require explicit exact-plan invocation, reconnect with a fresh session, and revoke it.

Native presentation tests cover scope reduction, duplicate clicks, a second queued request, expiry, stale host callbacks, compact/detailed plan review, and approval lifecycle isolation from Pi inline answers. Pi secret questions and provider settings defer authorization; existing sheets and modal windows are checked before presentation. Callback fencing retains the current Pi job/question/import/revision bindings.

## Claude Code startup

Installed Claude Code **2.1.293** initialized the credential-free management connection in **1.658 seconds**, using a temporary strict MCP configuration and an absent temporary installation. Its initialization event identified `meecp-management` as connected. Session persistence and ambient customization were disabled; no raw stream output was retained.

The process later returned a provider error. This establishes MCP connection startup; model-driven tool selection and invocation remain unverified. Claude credentials and client settings were not changed.

## Packaged native flow

Build a new development app and run the isolated interactive verifier:

```sh
uv run --locked python scripts/build_meecp_app.py --output /absolute/new/meecp.app
uv run --locked python scripts/verify_native_authorization.py --app /absolute/new/meecp.app
```

Allow the three synthetic requests in the native popup: base access, installation, and upgrade. The current verifier uses a private temporary installation and the bundled JSON Utilities package. It checks credential-free export, actual native decisions, separate installation/upgrade review, absence of automatic replay, explicit apply, remembered reconnect with a fresh credential, revocation, and owned cleanup. It prints fixed stage receipts. The two recorded original runs below predate the upgrade extension.

On 2026-10-07 the packaged verifier completed twice, including the final client fix. Both runs reported native host readiness, base approval, exact-plan approval, and `native_authorization_passed`. The verifier contains no approval API calls: fresh base and exact-install credentials require native owner decisions. It completed the explicit fixture installation, remembered reconnection, live revocation, and owned process/socket/state cleanup. The final run left no development app process running.

The final artifact is `dist/mcpme-authorization-20261007-final.app`. It derives from the complete development build, with the final client module copied into the bundle after its expiry regression fix. Existing host artifact hashes were verified before the refresh, the client and host/app receipt hashes were updated, the outer ad hoc signature was regenerated, and deep strict signature verification passed. Every recorded host artifact hash matched afterward; the packaged client matches the working source with SHA-256 `fdcac788f4664142c252ea9b856bbdfc589a08a6b6daced276fbdf7b1211a549`. The unchanged Swift source hash matched the build receipt. All three packaged native self-tests passed again. This is an incrementally refreshed development artifact, not a release build.

The initial failed harness runs used an unresolved `/var/folders` temporary path; the host correctly rejected its symlink ancestor. The verifier now resolves its private temporary scope before launching. The final client regression also confirms that expiry of a request for more access retains the existing inspection session and its original scope ceiling.

## Management upgrade follow-up

The catalog now exposes **45 tools**, including `meecp_plan_upgrade`,
`meecp_inspect_upgrade_plan`, and `meecp_apply_upgrade`. Native inspection/planning
consent permits planning against an installed slot. Applying requires a separate
exact upgrade approval bound to the saved plan digest, old installed snapshot,
candidate package/settings, and one durable operation. Approval never replays apply.
The popup shows current and candidate versions, resources, declared access, tools,
and configuration/secret-reference field names. Cancellation and recovery remain
owner operations.

The final focused regression run passed **381 Python tests in 155.48 seconds**
with the same 42 fixture cleanup warnings. It includes the original authorization
suites plus `test_native_upgrade_authorization.py`,
`test_management_upgrade_grants.py`, `test_meecp_upgrade_host.py`, and
`test_meecp_upgrade_cli.py`. New checks cover source/plan/slot changes, native ACK,
revocation/expiry across lock and package-work yields, duplicate approval before
ACK, atomic operation binding, terminal retries, wrong grant families, and continued
responsiveness of other leases during request/detail/approval package verification.
An initial race test exposed durable base revocation being ignored by the native
association guard; the corrected guard checks its underlying ledger without
recursion. The final run includes that regression.

Swift tests passed: **18 passed, 3 opt-in application lifecycle tests skipped**.
The packaged native contract self-test passed. AppKit authorization and Pi UI
self-tests passed in the Python suite.

The development artifact is `dist/meecp-management-upgrades-20261007.app`, produced
by the standard app builder. Its authorization module was then refreshed with the
final revocation fix; host/app receipts and the sidecar were updated and the ad hoc
outer signature regenerated. Deep strict signature verification passed and all
**13,078 recorded host artifacts** matched their hashes after the refresh.

The isolated packaged verifier completed with `native_upgrade_passed` and
`native_authorization_passed`. Through real stdio SDK calls and three native owner
decisions, it installed JSON Utilities 1.0.0, reviewed/applied 1.0.1, preserved one
slot UUID, port, invocation connection, and synthetic data marker, advanced the
generation, and returned the same operation on retry. It also checked no automatic
work on approval, remembered reconnect, live revocation, and owned cleanup. The
verifier contains no approval API calls.

## Independent review before PR

The independent review found and corrected two execution blockers. The native
upgrade parser omitted supported `mcp-http` and `host-command` profiles, so their
complete reviews could not reach the approval popup. It now accepts all four
supported profiles and rejects a candidate whose profile differs from the installed
app. Independently generated broker review envelopes from valid packages in each
profile passed the actual Swift parser; the HTTP scenario also exercises native
presentation and approval in the coordinator self-test.

The native association guard also read the owner-thread SQLite registry from
preparation worker threads. That caused `sqlite3.ProgrammingError` during real
worker admission and monitoring. Worker checks now open and close a bounded
read-only connection to the same private registry inode; the owner connection
retains its thread affinity. Eleven regressions cover native consent/ACK followed
by worker admission and monitoring, durable base/action revocation and expiry,
profile revocation, disconnect, missing/replaced/unsafe registry state, lock
contention, and URI metacharacters in the state path. These preparation checks use
synthetic fixtures and do not perform a public package build.

The final affected suite, including preparation runner/resolution regressions,
passed **442 tests in 238.51 seconds**, with nine opt-in private-runtime cases
skipped and the existing 42 cleanup warnings. Swift passed **18 tests**, with
three opt-in installation lifecycle cases skipped. Hotdog tests with the locked
build dependency group passed **45 tests**. The history secret scan and all five
packaged native self-tests passed. A broader repository run was stopped after
569 passes and one skip because review fixes changed its source snapshot; it is
not evidence of a complete full-suite pass. Full repository CI remains separate.

The refreshed development artifact is
`dist/meecp-management-upgrades-20261007-reviewed.app`, rebuilt with the standard
app builder. The real stdio/native verifier passed again with
`native_upgrade_passed` and `native_authorization_passed`. Its three approvals
were driven through the actual native UI in the isolated synthetic installation;
no approval API was used. The UI showed the retained app identity and port, current
version 1.0.0, candidate version 1.0.1, and clone/rollback policy. The verifier
confirmed completion, one installed entry, preserved data/connection, one-operation
retry, remembered reconnect, live revocation, and owned cleanup.

During PR creation, `main` advanced to `0937ace` with initialization connection-reuse
guidance. The branch incorporated that update without conflicts. The combined
management facade, MCP catalog, CLI, gateway, and service-protocol suites passed
**103 tests in 43.23 seconds**. This checks the advisory `instructions` addition
alongside immediate startup and the native authorization flow. The development
bundle's two affected Python modules and documentation were refreshed, with receipt
hashes and its ad hoc signature updated.

## Limits

This is a local development implementation. Public signing/notarization and a release workflow were not run. The installed default application was not replaced. Strong signed-launcher attribution, other Claude distribution variants, real model-driven invocation, and exhaustive manual interactions across all macOS activation/settings/modal variants are not established by these checks. The implementation uses explicit account-scoped consent and conservative presentation deferral for those identity/focus boundaries.
