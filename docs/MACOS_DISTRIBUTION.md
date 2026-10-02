# macOS installer distribution

mcpme targets Apple silicon and macOS 26 or later. The DMG contains `mcpme.app`,
an Applications shortcut, and `Install.md`. The application includes its locked
Python runtime and server dependencies; tool packages and model weights are
installed separately.

## Development installer

Build from the repository root with the managed Python 3.12.13 runtime installed:

```sh
uv python install 3.12.13
uv sync --locked
uv run --locked python scripts/build_mcpme_dmg.py --output dist/mcpme-dev.dmg
```

The app uses ad hoc signatures. The DMG is unsigned and unnotarized. Its install
instructions and build receipt identify it as a development artifact. Do not
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
  --sign-identity 'Developer ID Application: Your Name (TEAMID)' \
  --notary-profile mcpme-release \
  --keychain /path/to/release.keychain-db \
  --output dist/mcpme-release.dmg
```

`--keychain` scopes both signing identity lookup and notarization credentials to
the selected Keychain. Omit it when the certificate and profile are accessible
through the default Keychains. Keep passwords, certificate exports, API keys,
and their contents out of the repository and command output.

The builder:

1. Builds the Swift app and private host with the locked dependencies.
2. Rejects escaping runtime symlinks and non-system absolute native dependencies.
3. Signs every bundled Mach-O library, Python extension, interpreter, and native
   helper before the outer app, using the selected Developer ID certificate,
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
8. Publishes the DMG and receipt only after all checks succeed.

The two notarization submissions let both the image and the installed app carry
their own tickets. A rejected submission, unavailable identity, tool error,
failed signature, or failed assessment prevents publication. Tool output is
captured without printing certificate material, credentials, exception dumps,
or Apple notarization logs. Error reporting uses fixed messages.

Passing `--sign-identity` without `--notary-profile` builds a Developer ID signed
installer that explicitly says it is unnotarized. It is not labeled as a
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
The adjacent DMG `.build.json` is the final distribution record: it includes
the actual signing and notarization modes, both accepted submission IDs, ticket
and Gatekeeper checks, final image SHA-256, and byte count. It embeds an updated
application record containing the app's notarization result.

The local development build and the contract tests can be exercised without
Apple credentials. The Developer ID and notarization path requires a successful
credentialed run before a release can be claimed. The isolated CLI check does
not exercise every native menu action, installed tool package, or model; run
those acceptance checks separately when preparing a product release.

See [GitHub release setup](GITHUB_RELEASES.md) for the protected release workflow
and its secrets. Apple's primary references are
[notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution),
[packaging Mac software for distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution),
and [the current notarization tool](https://developer.apple.com/documentation/technotes/tn3147-migrating-to-the-latest-notarization-tool).
