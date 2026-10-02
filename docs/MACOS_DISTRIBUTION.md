# macOS installation and distribution

mcpme fits a drag-to-Applications disk image: its native app, Python interpreter,
host dependencies, artwork, documentation, and licenses are already bundled in
one relocatable `mcpme.app`. Projects, packages, credentials, and settings live
outside that bundle in `~/Library/Application Support/mcpme`. Installation does
not require a privileged installer or a system service.

The current target is Apple silicon running macOS 26 or later. Tool packages and
models are installed separately; the host disk image does not embed JevK5 weights.

## Build a development DMG

From the repository root, with Xcode Command Line Tools and `uv` installed:

```sh
uv python install 3.12.13
uv sync --locked
uv run --locked python scripts/build_mcpme_dmg.py
```

The command builds current source into a temporary app, then produces
`dist/mcpme-0.1.0-arm64.dmg` and a companion `.dmg.build.json` receipt. The filename
uses the app's version. The volume contains `mcpme.app`, an Applications shortcut,
and `Install.md`. Build dependencies are unnecessary on the destination Mac.

Both the app and the image are local development artifacts. The app has an ad hoc
signature; the image is unsigned and has not been notarized. Packaging in a DMG
does not change that trust status. A downloaded copy may be blocked by Gatekeeper.

To package a previously built app or choose another output:

```sh
uv run --locked python scripts/build_mcpme_dmg.py --app dist/mcpme.app --output dist/mcpme-local.dmg
```

Builders refuse to replace an existing image or receipt. Use a fresh `--output`
path for a subsequent build. `--app` packages that artifact's source snapshot,
which can differ from the current checkout; its embedded build receipt is copied
into the disk-image receipt. Without `--app`, the current source is rebuilt.

## Install

1. Quit a running copy of mcpme before replacing it.
2. Open the DMG and drag `mcpme.app` onto the Applications shortcut.
3. Eject the DMG, then open `/Applications/mcpme.app`.
4. Find mcpme in the menu bar and open **Add Project** or **Manage Projects**.

Replacing the app preserves existing projects and settings in Application Support.
If macOS asks to replace an existing app, approve replacement only when that is the
intended update. Copy MCP connection configurations after installing at the final
location; their command paths are absolute. Old configurations pointing into a
development checkout or a mounted image need to be replaced in the client.

## Verification performed by the builder

The builder verifies the input app's strict recursive code signature, creates a
compressed read-only UDZO image using macOS `hdiutil`, and verifies its checksum.
It mounts the image read-only, checks the Applications shortcut and installation
instructions, and verifies the mounted app's signature. It copies the app to an
isolated test Applications directory, ejects the image, verifies the copy's
signature, and runs the bundled CLI help command with a minimal environment from
a path containing spaces. That checks the private interpreter and host imports
without using the user's live state.

The receipt records those checks, app build provenance, version, platform,
size, and SHA-256. These checks establish image integrity and basic relocation;
they do not establish a successful GUI launch, login registration, Gatekeeper
acceptance, or tool execution on another Mac. Use the existing host/MCP verification
scripts for protocol and lifecycle checks, and test the finished release on a
separate Mac before distributing it publicly.

On 2026-10-01, a DMG built from the current workspace passed the builder checks.
The app was also copied from the mounted image and tested after ejection using
`scripts/verify_mcpme.py` against JSON Utilities in isolated temporary state. All
16 host checks passed: two independent slots, four HTTP MCP calls, one stdio MCP
call, credential and management isolation, stable restart, and owned-process
cleanup. The local aggregate report is
`test-artifacts/dmg-install-validation/verification.json`.

## Public release requirements

[Apple's distribution workflow](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
supports a disk image for direct distribution. Public releases require additional
work in the app build pipeline:

1. Obtain a **Developer ID Application** identity with its private key in the
   signing Mac's Keychain. No valid signing identity was available on the inspected
   build machine on 2026-10-01.
2. Sign each bundled Mach-O executable and library, then the app, with Developer
   ID, secure timestamps, and the hardened runtime where required. Sign from the
   inside out; Apple's guidance advises against using `codesign --deep` for signing
   distribution code. Audit Python extension loading and apply only the runtime
   entitlements that it actually requires.
3. Create the DMG and sign it with Developer ID Application, a secure timestamp,
   and a unique disk-image identifier. Submit this outermost container using
   `xcrun notarytool` with a Keychain credential profile, require an **Accepted**
   result, and staple/validate the ticket on that DMG with `xcrun stapler`.
   Keep credentials outside source and build logs.
4. Test the downloaded, quarantined artifact on a separate Apple silicon Mac:
   drag installation, first GUI launch, Add Project, a real MCP call, Quit cleanup,
   and replacement while preserving state. Validate Start at Login separately.
5. Publish the verified image and checksum to the user-selected release destination.

See [creating distribution-signed code](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac)
and [notarization requirements](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).
The current DMG builder performs local packaging and verification; it does not
perform Developer ID signing, notarization, upload, or release publication.
