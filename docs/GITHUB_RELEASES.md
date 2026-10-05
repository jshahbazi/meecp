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

The `CI` workflow scans fetched Git history, runs the locked Python tests, and builds and verifies a development DMG on the Apple silicon `macos-26` runner. The app uses an ad hoc signature and is not a notarized release. Each successful run retains the DMG and its build receipt as an artifact for seven days.

## Signed installer

The `Signed installer` workflow is dispatched manually from `main`. It rebuilds the checked-out commit on a fresh Apple silicon runner, scans history, runs tests, imports a Developer ID Application certificate into a temporary keychain, signs the app and disk image, submits both to Apple, staples their accepted tickets, and validates Gatekeeper assessment. It never signs pull-request artifacts.

The signing job retains read-only repository permission. A separate Linux job downloads only this run's verified artifacts and uses `contents: write` to create a GitHub Release. That job has no Apple credentials. It checks each receipt against the source commit, app version, accepted notarization, artifact size, and SHA-256, creates a fresh tag pointing to that exact commit, and uploads all assets to a draft release. It checks the uploaded names, sizes, GitHub SHA-256 digests, and tag target before publishing. Failed uploads or verification before publication leave the draft unpublished.

The **Version bump** dropdown offers `major`, `minor`, and `patch`, with `minor` selected by default. The workflow takes the highest numeric version among all published stable `vMAJOR.MINOR.PATCH` releases and `[project].version` in `pyproject.toml`, then increments the selected component. Major resets minor and patch to zero; minor resets patch to zero. Drafts, prereleases, and unrelated tags do not advance the baseline. With `0.1.0` as the baseline, the choices produce:

| Selection | Next version |
| --- | --- |
| `major` | `1.0.0` |
| `minor` (default) | `0.2.0` |
| `patch` | `0.1.1` |

The selected version is passed explicitly into the build and publication jobs. The app's short version, host receipt, installer receipts, filenames, and `vVERSION` release tag all use it. The workflow keeps the checked-out source unchanged, records its exact commit in the receipts, and leaves `pyproject.toml` and `uv.lock` as the development baseline. Each new dispatch advances from the published release history, so repeated releases do not require source version commits. Existing tags and releases are still rejected before signing and again during publication; published versions are never overwritten.

Release notes include every merged PR whose merge, squash, or rebase commit is included after the previous release's tag and through the exact built commit. The previous release is the most recently published stable `vMAJOR.MINOR.PATCH` release; drafts, prereleases, and unrelated tags are ignored. The first release covers the built commit's complete history. Each entry links to its PR and quotes the full opening description, preserving its Markdown and validation details. Entries follow merge order. Empty descriptions are identified explicitly, and a release with no new PRs says so. A full changelog link also exposes any direct commits.

The publisher reads every page of release, commit, and PR results before creating a tag or draft. PRs are listed in creation order regardless of their current open/closed state, so closing or reopening an older PR does not shift the page boundaries. API failures, incomplete commit results, or a previous release outside the built commit's ancestry stop publication. Unmerged PRs and PRs merged after the built commit are excluded even when they merge while signing is running. The publication job has `pull-requests: read` alongside its release write permission. Draft recovery preserves the notes captured during initial publication.

Each release offers these direct downloads under [Releases](https://github.com/jshahbazi/mcpme/releases):

| Asset | Purpose |
| --- | --- |
| `mcpme-VERSION-arm64.dmg` | Disk image: open `mcpme.app` and choose **Install and Restart**. |
| `mcpme-VERSION-arm64.dmg.build.json` | Signing, notarization, source, and verification receipt. |
| `SHA256SUMS.txt` | SHA-256 checksums for the DMG and its receipt. |

The DMG contains the signed app with a stapled Apple ticket and carries its own signature and ticket. See [Apple's notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow).

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
| `MCPME_COMMAND_SIGNED_ASSET_BASE64` | Canonical base64 encoding of the exact reviewed `command-native-v1` signed helper archive. |

Keep credentials out of Git and build logs. The helper writes credential files with owner-only permissions and captures credential-tool output. It registers the temporary signing keychain alongside the runner's existing user search list and checks signing before the app build. Unconditional cleanup restores the original search list, deletes the temporary keychain, and removes the private files. Missing or rejected credentials fail the release; the workflow does not fall back to an ad hoc signature.

After the history scan, the workflow prepares the signed command archive before
tests and Apple credential import. `.github/scripts/prepare_command_asset.py`
bounds and decodes the environment input, checks the fixed manifest size/hash,
and admits the complete archive and both signatures in a fresh private stage.
It publishes a fresh mode-0600 archive under `RUNNER_TEMP`, supplies its canonical
path to the DMG builder through `--command-signed-asset`, and removes the admission
stage. Missing or invalid input fails with a fixed diagnostic; archive bytes and
tool errors are never logged. Existing output is never overwritten. See
[the signed helper contract](SIGNED_COMMAND_HELPERS.md#distribution-integration).

To cut a release, open **Actions > Signed installer > Run workflow**, choose `main`, select **Version bump** (default `minor`), and approve the `macos-release` deployment. After both jobs succeed, download the DMG from the GitHub Release. Releases persist independently of the backup Actions artifact `mcpme-release-COMMIT`, which expires after thirty days. Rejection or timeout from Apple fails the run without publishing installer artifacts or a release.

If publication fails after creating a draft, inspect that draft and the failed job. A complete draft can be recovered without signing again: download the original run's artifact into a fresh directory and use `publish_release.py resume --assets-dir DIRECTORY --release-id ID --version VERSION` with `GITHUB_REPOSITORY=jshahbazi/mcpme` and `GITHUB_SHA` set to the original built commit. Use the exact release version from the original filenames and receipts, the original checked-out source, and an authenticated GitHub CLI account with release write access. Recovery verifies the draft by its numeric release ID, its tag and source commit, and all three uploaded assets before publishing; it does not create tags, upload files, or replace assets. GitHub uses a temporary `untagged-...` URL for drafts, so verification reads the release by ID and requires its canonical version URL only after publication.

For an incomplete draft, delete that draft and any corresponding unpublished tag before rerunning the complete workflow. To release newer code after publication, start a new dispatch and select the next version bump. Retrying publication of an already published version does not replace its downloads.

Build-command failures report a fixed tool and operation code plus the numeric exit status, such as `build_command_codesign_verify_failed_exit_3`. Captured output, command arguments, paths, and credential values are omitted. A codesign verification exit status of 3 can indicate that the signature did not satisfy the requested requirement; an `input_unreadable` suffix indicates that codesign could not read an input. Inline requirements must start with `=`. Credential-import failures still report `apple_credentials_setup_failed`.

## Updating dependencies

Keep workflow action references pinned to official full commit SHAs. When updating `astral-sh/setup-uv`, update its selected-action allowlist entry in repository settings as well. Keep Python and uv versions compatible with the private-runtime builder, and change `uv.lock` through uv rather than editing it manually.
