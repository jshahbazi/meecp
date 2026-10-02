# GitHub protections and installer builds

## Repository protections

The default-branch `Core` ruleset requires pull requests, one approving review, resolved review threads, and the `CI` check from GitHub Actions. New commits dismiss stale approvals. The check must pass against the current base branch. Branch deletion and force pushes remain blocked.

The repository owner is the only current collaborator. Administrator bypass is limited to pull-request merges so the owner can complete a solo change through a recorded PR. This exception can bypass review and check requirements when deliberately used; it does not allow direct pushes. Add another reviewer and remove the bypass for independent approval enforcement.

Actions use read-only default tokens and cannot approve pull requests. Allowed actions are limited to GitHub-owned actions and the pinned `astral-sh/setup-uv` revision used by these workflows. All actions must use full commit SHAs. Fork workflows require approval from a maintainer for all external contributors.

GitHub secret scanning and push protection are enabled. The native non-provider-pattern setting currently remains disabled: GitHub accepted the setting request without enabling it and exposes no control for it on this repository's settings page. The required `CI` check adds a pinned Gitleaks scan of the fetched Git history, including generic API credential and private-key rules. Scanner output and finding contents are suppressed; detection or scanner failure blocks the check. Pattern scanning has false positives and incomplete coverage, and it does not replace credential rotation after an exposure. Vulnerabilities can be reported privately using the link in [SECURITY.md](../SECURITY.md).

## Development installer

The `CI` workflow scans fetched Git history, runs the locked Python tests, and builds and verifies a development DMG on the Apple silicon `macos-26` runner. Its installer uses an ad hoc signature and is not a notarized release. Each successful run retains the DMG and build receipt as an artifact for seven days.

## Signed installer

The `Signed installer` workflow is dispatched manually from `main`. It rebuilds the checked-out commit on a fresh Apple silicon runner, scans history, runs tests, imports a Developer ID Application certificate into a temporary keychain, signs the app and disk image, submits both to Apple, staples their accepted tickets, and validates Gatekeeper assessment. It never signs pull-request artifacts. The job has no repository write permission and uploads artifacts without publishing a GitHub release.

The `macos-release` environment permits only `main` and requires approval by the repository owner. Self-approval is enabled for the current solo-maintainer setup. Signing requires an Apple Developer Program membership, a valid Developer ID Application certificate with its private key, and an App Store Connect API key that can submit notarization requests.

Add these environment secrets under [Settings > Environments > macos-release](https://github.com/jshahbazi/mcpme/settings/environments):

| Secret | Content |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | Base64-encoded export of the Developer ID Application certificate and private key as a password-protected `.p12`. |
| `MACOS_CERTIFICATE_PASSWORD` | Password protecting the `.p12` export. |
| `MACOS_SIGNING_IDENTITY` | Exact certificate name, such as `Developer ID Application: Example Team (TEAMID1234)`. |
| `APPLE_API_KEY_P8_BASE64` | Base64-encoded private `.p8` key for the App Store Connect API. |
| `APPLE_API_KEY_ID` | API key identifier. |
| `APPLE_API_ISSUER_ID` | Issuer identifier for the API key. |

Keep credentials out of Git and build logs. The helper writes credential files with owner-only permissions and captures credential-tool output. It registers the temporary signing keychain alongside the runner's existing user search list and checks signing before the app build. Unconditional cleanup restores the original search list, deletes the temporary keychain, and removes the private files. Missing or rejected credentials fail the release; the workflow does not fall back to an ad hoc signature. A real signed release remains unverified until the credentials are supplied and Apple accepts a workflow run.

To build a release, open **Actions > Signed installer > Run workflow**, choose `main`, and approve the `macos-release` deployment. Download `mcpme-notarized-COMMIT` after success; it contains the DMG and its build receipt and expires after thirty days. Rejection or timeout from Apple fails the run without publishing an installer artifact.

Build-command failures report a fixed tool and operation code plus the numeric exit status, such as `build_command_codesign_verify_failed_exit_3`. Captured output, command arguments, paths, and credential values are omitted. A codesign verification exit status of 3 can indicate that the signature did not satisfy the requested requirement; an `input_unreadable` suffix indicates that codesign could not read an input. Inline requirements must start with `=`. Credential-import failures still report `apple_credentials_setup_failed`.

## Updating dependencies

Keep workflow action references pinned to official full commit SHAs. When updating `astral-sh/setup-uv`, update its selected-action allowlist entry in repository settings as well. Keep Python and uv versions compatible with the private-runtime builder, and change `uv.lock` through uv rather than editing it manually.
