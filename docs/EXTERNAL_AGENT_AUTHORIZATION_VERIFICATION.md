# Native external-agent authorization verification

Date: 2026-10-07. Baseline: `main` at `38a109d`; implementation branch: `codex/external-agent-authorization-plan`.

## Implemented behavior

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
  tests/test_mcpme_management_cli.py \
  tests/test_mcpme_management_mcp.py \
  tests/test_mcpme_grants.py \
  tests/test_management_workflow_grants.py \
  tests/test_local_transport.py \
  tests/test_native_private_transport.py \
  tests/test_pi_setup.py \
  tests/test_mcpme_registry.py
swift test
```

Result: **272 Python tests passed in 127.72 seconds**. The 42 existing protected-fixture cleanup warnings did not contain assertion failures. Swift: **18 passed, 3 installation lifecycle tests skipped**, zero failures. Native AppKit authorization and Pi self-tests are included in the Python result.

Coverage includes real OS peer credentials/process-start identity, private socket safety, prepared/ACK delivery and loss, request replay on another connection, approval/disconnect races, policy persistence across host restart, scope reduction/increase, renewal, revocation, missing associations, copied/unknown profile rejection, exact preparation/resolution/install bindings, authoritative review effects, every Pi route across base/action and seven legacy grant modes, credential absence, and malformed owner-plus-grant principals. Real SDK sessions establish immediate startup with a missing host or pending approval, use an approved session, require explicit exact-plan invocation, reconnect with a fresh session, and revoke it.

Native presentation tests cover scope reduction, duplicate clicks, a second queued request, expiry, stale host callbacks, compact/detailed plan review, and approval lifecycle isolation from Pi inline answers. Pi secret questions and provider settings defer authorization; existing sheets and modal windows are checked before presentation. Callback fencing retains the current Pi job/question/import/revision bindings.

## Claude Code startup

Installed Claude Code **2.1.293** initialized the credential-free management connection in **1.658 seconds**, using a temporary strict MCP configuration and an absent temporary installation. Its initialization event identified `mcpme-management` as connected. Session persistence and ambient customization were disabled; no raw stream output was retained.

The process later returned a provider error. This establishes MCP connection startup; model-driven tool selection and invocation remain unverified. Claude credentials and client settings were not changed.

## Packaged native flow

Build a new development app and run the isolated interactive verifier:

```sh
uv run --locked python scripts/build_mcpme_app.py --output /absolute/new/mcpme.app
uv run --locked python scripts/verify_native_authorization.py --app /absolute/new/mcpme.app
```

Allow the two synthetic requests in the native popup. The verifier uses a private temporary installation and the bundled JSON Utilities package. It checks credential-free export, actual native base approval, exact installation review, absence of automatic replay, explicit apply, remembered reconnect with a fresh credential, revocation, and owned cleanup. It prints fixed stage receipts and keeps the default installation untouched.

On 2026-10-07 the packaged verifier completed twice, including the final client fix. Both runs reported native host readiness, base approval, exact-plan approval, and `native_authorization_passed`. The verifier contains no approval API calls: fresh base and exact-install credentials require native owner decisions. It completed the explicit fixture installation, remembered reconnection, live revocation, and owned process/socket/state cleanup. The final run left no development app process running.

The final artifact is `dist/mcpme-authorization-20261007-final.app`. It derives from the complete development build, with the final client module copied into the bundle after its expiry regression fix. Existing host artifact hashes were verified before the refresh, the client and host/app receipt hashes were updated, the outer ad hoc signature was regenerated, and deep strict signature verification passed. Every recorded host artifact hash matched afterward; the packaged client matches the working source with SHA-256 `fdcac788f4664142c252ea9b856bbdfc589a08a6b6daced276fbdf7b1211a549`. The unchanged Swift source hash matched the build receipt. All three packaged native self-tests passed again. This is an incrementally refreshed development artifact, not a release build.

The initial failed harness runs used an unresolved `/var/folders` temporary path; the host correctly rejected its symlink ancestor. The verifier now resolves its private temporary scope before launching. The final client regression also confirms that expiry of a request for more access retains the existing inspection session and its original scope ceiling.

## Limits

This is a local development implementation. Public signing/notarization and a release workflow were not run. The installed default application was not replaced. Strong signed-launcher attribution, other Claude distribution variants, real model-driven invocation, and exhaustive manual interactions across all macOS activation/settings/modal variants are not established by these checks. The implementation uses explicit account-scoped consent and conservative presentation deferral for those identity/focus boundaries.
