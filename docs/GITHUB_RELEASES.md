# GitHub protections and installer builds

## Repository protections

The default-branch `Core` ruleset requires pull requests, one approving review, resolved review threads, and the `CI` check from GitHub Actions. New commits dismiss stale approvals. The check must pass against the current base branch. Branch deletion and force pushes remain blocked.

The repository owner is the only current collaborator. Administrator bypass is limited to pull-request merges so the owner can complete a solo change through a recorded PR. This exception can bypass review and check requirements when deliberately used; it does not allow direct pushes. Add another reviewer and remove the bypass for independent approval enforcement.

Actions use read-only default tokens and cannot approve pull requests. Allowed actions are limited to GitHub-owned actions and the pinned `astral-sh/setup-uv` revision used by these workflows. All actions must use full commit SHAs. Fork workflows require approval from a maintainer for all external contributors.

GitHub secret scanning and push protection are enabled. The native non-provider-pattern setting currently remains disabled: GitHub accepted the setting request without enabling it and exposes no control for it on this repository's settings page. The required `CI` check adds a pinned Gitleaks scan of the fetched Git history, including generic API credential and private-key rules. Scanner output and finding contents are suppressed; detection or scanner failure blocks the check. Pattern scanning has false positives and incomplete coverage, and it does not replace credential rotation after an exposure. Vulnerabilities can be reported privately using the link in [SECURITY.md](../SECURITY.md).

## CodeQL scanning

The repository's [CodeQL workflow](../.github/workflows/codeql.yml) scans Actions, C/C++, Python, and Swift in parallel using the default query suite. Swift uses the Apple silicon `macos-26` runner and autobuild; the other languages use `ubuntu-24.04` without a build. Actions are pinned to full commit SHAs.

Pushes to `main` and pull requests targeting `main` skip CodeQL when every changed file is under `docs/`, is Markdown, or is the root `LICENSE` or `NOTICE` file. Any change outside these paths, including a mixed documentation and source change, triggers all four analyses. Weekly scans and manual dispatch still analyze the full repository. The required `CI` workflow continues to build and verify installers for documentation changes because the host bundle includes documentation.

To activate this workflow when migrating from GitHub's default setup, first merge it into `main`, then disable default CodeQL setup in repository settings and manually dispatch this workflow from `main`. Default setup generates its own scans and does not honor this workflow's path filters. Confirm that all four analyses upload successfully before considering the migration verified. CodeQL is not a required status check in the current `Core` ruleset, so skipping documentation-only workflows does not block those pull requests.

## Development installer

The `CI` workflow scans fetched Git history, runs the locked Python tests, and builds and verifies a development DMG and app ZIP on the Apple silicon `macos-26` runner. The app uses an ad hoc signature and is not a notarized release. Each successful run retains both downloads and their build receipts as an artifact for seven days.

## Signed installer

The `Signed installer` workflow is dispatched manually from `main`. It rebuilds the checked-out commit on a fresh Apple silicon runner, scans history, runs tests, imports a Developer ID Application certificate into a temporary keychain, signs the app and disk image, submits both to Apple, staples their accepted tickets, and validates Gatekeeper assessment. It creates the distribution ZIP from the stapled app and verifies the extracted app. It never signs pull-request artifacts.

The signing job retains read-only repository permission. A separate Linux job downloads only this run's verified artifacts and uses `contents: write` to create a GitHub Release. That job has no Apple credentials. It checks each receipt against the source commit, app version, accepted notarization, artifact size, and SHA-256, creates a fresh tag pointing to that exact commit, and uploads all assets to a draft release. It checks the uploaded names, sizes, GitHub SHA-256 digests, and tag target before publishing. Failed uploads or verification before publication leave the draft unpublished.

The release tag is `vVERSION`, where `VERSION` is `[project].version` in `pyproject.toml`. The app's short version, host receipt, filenames, and tag use that version. Use three numeric components, such as `0.1.0`. Existing tags and releases are rejected before signing; published versions are never overwritten. For the next release, run `uv version --bump patch` (or set an explicit version), commit the updated `pyproject.toml` and `uv.lock`, and merge through the normal protected PR process.

Each release offers these direct downloads under [Releases](https://github.com/jshahbazi/mcpme/releases):

| Asset | Purpose |
| --- | --- |
| `mcpme-VERSION-arm64.dmg` | Regular disk image: open it and drag `mcpme.app` to Applications. |
| `mcpme-VERSION-arm64.zip` | App archive: extract it and move `mcpme.app` to Applications. |
| The two `.build.json` files | Signing, notarization, source, and verification receipts. |
| `SHA256SUMS.txt` | SHA-256 checksums for both downloads and their receipts. |

Both formats contain the same signed app with a stapled Apple ticket. The DMG also has its own signature and ticket. ZIP containers cannot themselves be signed or stapled; the contained app carries those protections. See [Apple's notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow).

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

Keep credentials out of Git and build logs. The helper writes credential files with owner-only permissions and captures credential-tool output. It registers the temporary signing keychain alongside the runner's existing user search list and checks signing before the app build. Unconditional cleanup restores the original search list, deletes the temporary keychain, and removes the private files. Missing or rejected credentials fail the release; the workflow does not fall back to an ad hoc signature.

To cut a release, open **Actions > Signed installer > Run workflow**, choose `main`, and approve the `macos-release` deployment. After both jobs succeed, download the DMG or ZIP from the GitHub Release. Releases persist independently of the backup Actions artifact `mcpme-release-COMMIT`, which expires after thirty days. Rejection or timeout from Apple fails the run without publishing installer artifacts or a release.

If publication fails after creating a draft, inspect that draft and the failed job. A complete draft can be recovered without signing again: download the original run's artifact into a fresh directory and use `publish_release.py resume --assets-dir DIRECTORY --release-id ID` with `GITHUB_REPOSITORY=jshahbazi/mcpme` and `GITHUB_SHA` set to the original built commit. Use the original source version in `pyproject.toml` and an authenticated GitHub CLI account with release write access. Recovery verifies the draft by its numeric release ID, its tag and source commit, and all five uploaded assets before publishing; it does not create tags, upload files, or replace assets. GitHub uses a temporary `untagged-...` URL for drafts, so verification reads the release by ID and requires its canonical version URL only after publication.

For an incomplete draft, delete that draft and any corresponding unpublished tag before rerunning the complete workflow. A published version requires a new version number; reruns do not replace its downloads.

Build-command failures report a fixed tool and operation code plus the numeric exit status, such as `build_command_codesign_verify_failed_exit_3`. Captured output, command arguments, paths, and credential values are omitted. A codesign verification exit status of 3 can indicate that the signature did not satisfy the requested requirement; an `input_unreadable` suffix indicates that codesign could not read an input. Inline requirements must start with `=`. Credential-import failures still report `apple_credentials_setup_failed`.

## Updating dependencies

Keep workflow action references pinned to official full commit SHAs. When updating `astral-sh/setup-uv`, update its selected-action allowlist entry in repository settings as well. Keep Python and uv versions compatible with the private-runtime builder, and change `uv.lock` through uv rather than editing it manually.
