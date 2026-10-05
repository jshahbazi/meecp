# macOS installer distribution

mcpme targets Apple silicon and macOS 26 or later. The DMG contains `mcpme.app`
and an Applications shortcut. The application includes its locked
Python runtime and server dependencies; tool packages and model weights are
installed separately.

## Development installer

Build from the repository root with the managed Python 3.12.13 runtime installed:

```sh
uv python install 3.12.13
uv sync --locked
uv run --locked python scripts/build_mcpme_dmg.py --output dist/mcpme-dev.dmg
```

The app uses ad hoc signatures. The DMG is unsigned and unnotarized. Its
build receipt identifies it as a development artifact. Do not
present that artifact as a public release.

The output and its adjacent `.build.json` receipt must both be fresh paths.
Existing files are never overwritten. Generated bundles, runtime copies, and
disk images belong in ignored build directories.

## Developer ID release

You need an Apple Developer Program membership, a **Developer ID Application**
certificate with its private key, and valid notarization credentials stored in a
Keychain profile. An Apple Development certificate or an ad hoc identity is
rejected for this mode. Supply the exact certificate name or its SHA-1 identity
as reported by `security find-identity -v -p codesigning`.

```sh
uv run --locked python scripts/build_mcpme_dmg.py \
  --command-signed-asset /path/to/command-native-v1.tar.gz \
  --sign-identity 'Developer ID Application: Your Name (TEAMID)' \
  --notary-profile mcpme-release \
  --keychain /path/to/release.keychain-db \
  --output dist/mcpme-release.dmg
```

The active Phase 3 command policy requires the exact reviewed
[signed command helper archive](SIGNED_COMMAND_HELPERS.md). The app signing
certificate must match the archive's pinned certificate. The builder verifies
and preserves the two helpers; recompiling or signing a replacement pair does
not reproduce their immutable identity.

`--keychain` scopes both signing identity lookup and notarization credentials to
the selected Keychain. Omit it when the certificate and profile are accessible
through the default Keychains. Keep passwords, certificate exports, API keys,
and their contents out of the repository and command output.

The builder:

1. Builds the Swift app and private host with the locked dependencies.
2. Rejects escaping runtime symlinks and non-system absolute native dependencies.
3. Preserves the reviewed command helper signatures and signs the remaining
   bundled Mach-O libraries, Python extensions, interpreters, and native helpers
   before the outer app, using the selected Developer ID certificate,
   hardened runtime, and secure timestamps. It verifies signatures against that
   exact certificate. It does not use `codesign --deep` to sign code.
4. Refreshes host artifact hashes after signing and seals the application build
   receipt in the final app signature.
5. Uploads a temporary ZIP of the app for notarization, requires `Accepted`,
   staples and validates its ticket, and assesses the app with Gatekeeper.
6. Creates the DMG containing the stapled app, signs the image, submits it for
   notarization, staples and validates the image ticket, and assesses the image
   with Gatekeeper.
7. Mounts the image read-only, copies the app into an isolated path containing
   spaces, ejects the image, verifies the copied signature and ticket, checks
   Gatekeeper again, and executes the bundled CLI with a temporary home and a
   minimal system `PATH`.
8. Publishes the DMG and its receipt only after all checks succeed. Existing
   outputs are never replaced; a publication failure removes newly published
   outputs.

The app's version comes from `[project].version` in `pyproject.toml`. On the
read-only DMG, open `mcpme.app` and choose **Install and Restart**.
The installer checks signature, version metadata, Apple silicon architecture,
and minimum macOS version, then stages the signed app and gracefully quits the current copy with a
75-second wait, atomically replaces the selected installation, and launches the
result. The prompt shows the destination. A single running installation in
`/Applications` or `~/Applications` takes precedence. Otherwise an existing
`/Applications/mcpme.app` is preferred, then an existing personal copy. A new
copy goes in `/Applications` when writable and `~/Applications` otherwise.
An unwritable existing system installation requires a manual Finder copy with
administrator authorization. Active Phase 3 release admission requires the
bundle files and embedded host to be owned by the running user; an
administrator-owned `/Applications` ancestor is allowed. Root-owned bundle
members fail startup and do not receive development authority. A user-owned
`~/Applications/mcpme.app` is supported. Because destination selection prefers
an existing system copy, quit and remove that copy before selecting a personal
installation. Updates also require a writable destination parent and a
user-owned installer lock. A refused quit or timeout prevents replacement.
A confirmed launch failure restores the prior bundle. An unconfirmed launch
retains the installed candidate and a recovery folder beside it; read that
folder's `RECOVERY.md` before restoring its previous bundle. Normal startup
policies apply after restart; manually configured services do not auto-resume.
The manual DMG path remains available, but requires quitting mcpme before
dragging the app to Applications. See [Host application updates](AUTO_UPDATE_PLAN.md) for the
separate, still-planned automatic updater.

The two notarization submissions let both the image and the installed app carry
their own tickets. A rejected submission, unavailable identity, tool error,
failed signature, or failed assessment prevents publication. Tool output is
captured without printing certificate material, credentials, exception dumps,
or Apple notarization logs. Error reporting uses fixed messages.

Passing `--sign-identity` without `--notary-profile` builds a Developer ID signed
installer whose receipt records that notarization was not requested. It is not labeled as a
notarized release. The native app builder also accepts `--sign-identity` and
`--keychain` when an app is needed independently of a DMG.

`--app /path/to/mcpme.app` packages a copy of an existing app. For a release,
that input must already be signed by the selected certificate, with a
compatible application build receipt. The source app is not modified by
stapling. An ad hoc app cannot be upgraded to a release solely by signing the
outer DMG; build a Developer ID app first.

## Receipts and validation boundaries

The app's sealed `Contents/Resources/app-build.json` records the native build and
its signing mode. The notarization phase occurs after that seal, so its release
receipt directs readers to the distribution builder for the later result.
The adjacent DMG `.build.json` is the final disk-image distribution record: it includes
the actual signing and notarization modes, both accepted submission IDs, ticket
and Gatekeeper checks, final image SHA-256, and byte count. It embeds an updated
application record containing the app's notarization result.
The receipt does not change the sealed app after signing.

The local development build and the contract tests can be exercised without
Apple credentials. The Developer ID and notarization path requires a successful
credentialed run before a release can be claimed. The isolated CLI check does
not exercise every native menu action, installed tool package, or model; run
those acceptance checks separately when preparing a product release.

The Swift suite exercises staging, signature failure, quit failure, concurrent
installation, changed destinations, atomic replacement, and launch recovery:

```sh
swift test
```

In a logged-in macOS session, opt into native lifecycle tests as well:

```sh
MCPME_RUN_INSTALL_LIFECYCLE_TESTS=1 swift test --filter AppInstallationTests
```

These launch signed synthetic apps in a temporary directory, replace one from
a real read-only image, and verify a refused quit leaves it running and prevents
replacement. They do not touch the user's installed app or Application Support.
The install callback confirms macOS opened the app; it does not establish host
or model health. A notarized downloaded build still needs a release acceptance
check for Gatekeeper, app translocation, administrator permissions, and login items.

See [GitHub release setup](GITHUB_RELEASES.md) for the protected release workflow
and its secrets. Apple's primary references are
[notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution),
[packaging Mac software for distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution),
and [the current notarization tool](https://developer.apple.com/documentation/technotes/tn3147-migrating-to-the-latest-notarization-tool).
