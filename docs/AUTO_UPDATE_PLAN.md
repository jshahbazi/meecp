# Host application updates

Status: planned design. No updater is implemented. This covers the signed `meecp.app` host only; installed packages, models, runtimes, and package-owned data remain separate.

Use Sparkle 2, pinned through Swift Package Manager and a committed `Package.resolved`. Bootstrap through one manually installed release that embeds Sparkle, `SUFeedURL`, and `SUPublicEDKey`; existing builds have no updater and cannot discover that release themselves. The earlier proposal to reuse a separately published notarized ZIP is obsolete. Releases now offer a notarized DMG; the updater design must use that payload and validate its Sparkle integration before implementation. Keep the private key in protected release signing infrastructure. The archive-signing key and Apple Developer ID identity have separate jobs. See [Sparkle setup and signing](https://sparkle-project.org/documentation/) and [publication](https://sparkle-project.org/documentation/publishing/).

Prefer a supported Sparkle 2.9 or later release with signed appcasts and release notes, using `SURequireSignedFeed` and `SUVerifyUpdateBeforeExtraction`. Protect and back up the signing key. Define a rotation procedure: replace only one trust root at a time, and use a Developer ID signed DMG when rotating the EdDSA key under verification before extraction. Test key rotation separately from routine updates once DMG payload support is validated. These requirements follow the [upstream signing and rotation guidance](https://sparkle-project.org/documentation/).

Provide **Check for Updates** and a first-launch choice for automatic checks, normally once per day. Downloads can be automatic by user choice; installation presents **Install and Restart** and **Later** so ongoing services can finish. Keep the stable channel as the initial scope. Disable production checks in ad hoc builds, UI previews, and read-only/translocated launches. Send no usage profile, project data, credentials, or tool activity with update requests.

## Release identity and publication

The source version is `[project].version` in [pyproject.toml](../pyproject.toml), currently `0.1.0`. The signed workflow selects a later version dynamically in [.github/workflows/release-dmg.yml](../.github/workflows/release-dmg.yml), and [.github/scripts/publish_release.py](../.github/scripts/publish_release.py) verifies the GitHub release. [The app builder](../scripts/build_meecp_app.py) currently sets every `CFBundleVersion` to `1`, which prevents Sparkle from distinguishing releases. Introduce a monotonically increasing release build number, pass it through builders and receipts, and set `sparkle:version` to that number. Match `sparkle:shortVersionString` to the actual bundle display version, filename, receipt, and release tag. Validate both identities before publication. [Sparkle version fields](https://sparkle-project.org/documentation/publishing/#internal-build-numbers).

The feed-hosting destination needs selection before implementation. A stable HTTPS feed served through the existing GitHub repository is sufficient; no new backend is necessary. It must support atomic publication of the XML and its feed signature. Archive URLs must identify immutable versioned assets, and withdrawn items must leave those assets intact. Start with full archives; defer delta updates and beta channels.

Publish in this order: build the selected version; sign, notarize, staple, and verify the app and archive; generate the Sparkle signature from that exact verified archive; upload the archive; then publish the appcast item only after the archive is available and its digest is checked. Verify the live feed against the release asset. An appcast item must never point to an unpublished or mutable asset.

## Replacement lifecycle and recovery

Integrate the programmatic `SPUStandardUpdaterController` into [MeecpApp](../Sources/MeecpMenuBar/main.swift). Extract a shared shutdown coordinator from `stopHost`: stop admission, drain/cancel work within deadlines, close gateways, stop owned runtimes, and confirm cleanup. Use Sparkle's relaunch-postponement callback to wait for it, and preserve the ordinary application-termination path for quit-triggered installs where that callback may not run. Add a host preflight that blocks replacement on active installs/migrations or unresolved `ownership_lost`; a running-app exit alone cannot prove ownership reconciliation. Never signal a discovered PID or steal a port. [Sparkle delegate contract](https://sparkle-project.org/documentation/api-reference/Protocols/SPUUpdaterDelegate.html).

Preserve start policies, explicit Stop wake suppression, slot UUIDs, stable ports, invocation credentials, registry state, package directories, model caches, and package-owned data. Restart follows existing saved policies; manual services do not resume automatically. MCP sessions end and clients reinitialize. Keep the install path stable so copied client configurations remain valid.

Before replacement, verify feed/archive signatures, Apple signature/notarization, version, architecture, minimum macOS version, and disk space. Use Sparkle's installer and authorization support for bundle replacement. Treat its failure and interruption behavior as release acceptance work; do not assume post-launch rollback. An optional later meecp health check may verify manager connection, supervisor ownership, registry readability, and a harmless status probe, then offer an explicitly retained previous app. That policy must never restore stale package or runtime data snapshots.

The existing external state directory is preserved. Failed updates retain packages, models, credentials, slot records, event metadata, and post-exposure writes.

## Delivery and acceptance

1. Define bundle version/build identity, feed/channel policy, preferences, and fixed failure states; confirm `CFBundleVersion` on installed and development builds.
2. Add Sparkle 2 and extend the custom app builder to embed its framework, preserve symlinks, set bundle-relative runpaths, and sign nested code bundles from the inside out before sealing the app. The current Mach-O-only signing loop needs explicit framework/helper/XPC bundle handling. Add protected archive/feed signing and appcast generation to the release workflow.
3. Implement manual checking, first-launch preference, candidate validation, explicit install/restart approval, normal shutdown, relaunch, and visible failure recovery.
4. Add optional meecp health recovery only after signed-build replacement behavior is measured.

Acceptance requires a real signed update to preserve state, slots, ports, credentials, packages, model caches, start policies, and Stop suppression; altered signatures/digests, lower versions, wrong architecture, unsupported macOS, missing notarization, and insufficient disk to fail closed; development builds to avoid the production feed; bounded draining with no leaked children, listeners, locks, or reservations; and interruption or failed post-launch checks to preserve newer external data.

The read-only DMG already offers **Install and Restart**, with verified staging, graceful shutdown, atomic replacement, and recovery on a confirmed launch failure. See [installation behavior and validation limits](MACOS_DISTRIBUTION.md). The automatic updater described here remains planned; its trust feed, release keys, and restart preflight are additional work.
