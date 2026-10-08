# meecp

![meecp: local MCP services with their own home](assets/brand/meecp-banner.png)

**Give your AI tools a home on your Mac.**

meecp runs local MCP tools and models from your macOS menu bar. Add an app, connect it to your AI assistant, and keep an eye on it while you work. MCP (Model Context Protocol) lets assistants call those tools; meecp takes care of keeping them running. An app is a configured local tool or model service with its own controls and connection settings.

Run a JSON validator alongside a local decision model. Connect several clients to the same model. Stop an app when you're done with it. Manage them all in one place.

## What you can do

- **See what's running.** The menu groups apps that need attention, running apps, and stopped apps. See incoming requests, recent traffic, and work in flight at a glance. Click an app to open its live details to the right, with controls, failures, latency, tools, and connection setup.
- **Manage apps in one place.** Search and filter the app sidebar, then select an app for live requests, tools, connection details, and recent operations. Start, stop, or restart a service. Choose **Manual** or **Automatic when meecp opens**, or remove the app while keeping its data for recovery.
- **Connect your assistant.** Copy an app's connection configuration and add it to your MCP client. Its endpoint stays stable across restarts.
- **Share a local model.** Use a model of your choice through a prepared package, and connect multiple assistants to the same running app.
- **Bring your own tools.** Choose source, paste a project link, or describe an app. The bundled Pi agent prepares its MCP integration with your configured model, then opens package review.
- **Give an assistant management access.** Copy a management connection and approve it in the native app. Remember inspection/planning access, review each execution plan, and revoke access from Approved Connections. Explicit file grants remain available for unattended clients.

meecp requires **Apple silicon and macOS 26 or later**. Current capabilities and limitations are described below.

## Install

Download the Apple silicon `.dmg` from the [latest release](https://github.com/jshahbazi/meecp/releases/latest). Public installers are signed with Developer ID and notarized. Each release includes checksums and a `.dmg.build.json` verification receipt; see [GitHub releases](docs/GITHUB_RELEASES.md).

Open the disk image, open `meecp.app` on the image, and choose **Install and Restart**. The prompt shows the destination in `/Applications` or `~/Applications`. The installer verifies the new bundle, gracefully quits the current copy, replaces it, and opens the installed app. Installed apps and settings remain in `~/Library/Application Support/mcpme`; services follow their saved startup policies after restart.

The installer waits up to 75 seconds for the current app to quit. A refusal or timeout stops replacement. A confirmed launch failure restores the previous bundle; an unconfirmed launch leaves a recovery copy. Installing manually by dragging the app from the DMG to Applications requires quitting meecp before copying, then opening the installed app. See [macOS distribution](docs/MACOS_DISTRIBUTION.md) for installation and recovery details.

The installer includes **JSON Utilities**, a small local tool you can try immediately. Open **Add App…**, expand **Advanced setup options**, select **Try JSON Utilities**, review its tool and access, then select **Add App** or **Add & Start App**. No package download, compiler, or model is needed. Other apps can be added from a prepared package or through the Pi setup workflow below. Application updates currently use this installation flow.

## Build from source and try JSON Utilities

Build meecp and give it a first app: JSON Utilities, a small tool that checks whether a JSON document is valid and reports its top-level type. No model download is needed.

You'll need Xcode Command Line Tools and `uv`. From the repository root:

```sh
./build.sh
```

The script installs the pinned Python runtime, syncs locked dependencies, and builds the current checkout without running tests. Each run creates a fresh app bundle under `dist/` and prints its path and launch command. To choose a destination, run `./build.sh dist/meecp-dev.app`; relative destinations use the repository root. Existing app bundles are preserved.

For the direct builder and a separate JSON Utilities package:

```sh
uv python install 3.12.13
uv sync --locked
uv run --locked python scripts/build_meecp_app.py
uv run --locked python scripts/prepare_json_package.py
open dist/meecp.app
```

The app bundles its own Python runtime and dependencies, Pi SDK, and verified Apple silicon Node runtime. Once built, it can run without globally installed Pi, Node, `uv`, Homebrew, or the checkout. Builders require a fresh output path; for another app build, pass `--output dist/meecp-dev.app`.

1. Open **Add App…** from the menu bar and choose `dist/json-utilities-package`.
2. Check **Start after install**, then select **Review Package** to see the tools and declared access.
3. Select **Add & Start App**.
4. Once the app is ready, use **Copy Connection Configuration** and add it to your MCP client's settings.

Your assistant can now call `validate_json(document)`. Connection formats vary by client; copying the configuration doesn't change its settings for you. If you move the app, copy the configuration again to get its new path.

Closing the Apps window keeps services running. Quitting meecp stops the processes it owns. Changing an app's startup choice affects future launches. An explicit Stop remains in effect until Start clears it. Removing an app stops its owned processes and closes its connection; its source, package, and retained data remain available for recovery.

To build a development DMG from current source at an explicit output path:

```sh
uv run --locked python scripts/build_meecp_dmg.py --output dist/meecp-dev.dmg
open dist/meecp-dev.dmg
```

Local builds are ad hoc signed development artifacts. The DMG builder creates a fresh app and verifies the mounted image, copied app, and private runtime; its receipt is `dist/meecp-dev.dmg.build.json`. Public releases use the protected-main signing and notarization workflow described in [GitHub releases](docs/GITHUB_RELEASES.md).

## Bring your own tools

A useful script can become a tool your assistant calls. In **Add App**, choose a source folder or Markdown/text setup file, paste a public HTTPS project link, or describe the app you want. For a single source script, choose its containing folder.

Describe what your assistant should be able to do. Open **Model Settings…** to choose the provider and exact model ID and save your API key; an existing local OpenAI-compatible endpoint is also supported. Select **Set Up App**. Pi investigates the request, acquires runtimes and dependencies, writes the integration, builds and tests it, and repairs failures in a private workspace. It can use networked commands and background services and ask inline questions for missing information or credentials. You do not select or author recipes.

After independent package and MCP fixture checks, meecp opens the generated package for review. Check its tools, access, configuration, and startup choices, then select **Add App** or **Add & Start App**. Drafts and progress survive closing the window; **Cancel Setup** stops setup work. See [Pi setup](docs/PI_SETUP.md) for model configuration, data sharing, execution boundaries, and current limits.

Python and Node helpers provide private runtimes and locked dependencies. Other toolchains can be downloaded, extracted, and built with project-specific commands; native compilers are discovered when available. The final integration exposes MCP tools through a relocatable stdio launcher, which can own child services. Independent checks support structured, text, image, audio, and variable outputs. General reliability across arbitrary projects has not been established. Prepared packages and earlier external-agent handoff remain available through **Advanced setup options** and the [package-authoring guide](docs/PACKAGE_AUTHORING.md).

If a publisher gives you a prepared package link, expand **Advanced setup options** in Add App, open **I already have a package download link**, and select **Enter Package Link…**. Supply the publisher's URL, size, and checksum, then review the verified package. This is a manual download option; there is no public package browser. The owner CLI supports the same [pinned downloads](docs/PINNED_DOWNLOADS.md).

Packages can declare [typed configuration and secret references](docs/CONFIGURATION_SECRETS.md). The owner selects nonsecret settings and private credential references in the installation plan; package processes receive the selected values through a private descriptor.

Advanced Add App paths support pinned package downloads, typed configuration and secret references, and raw pinned JevK5 weights with a selected verified engine. Reviewed [upgrades and recovery](docs/UPGRADES_RECOVERY.md) preserve app identity and data. Reference-role document onboarding remains unsupported. Pi setup uses the normal package review/install contract.

## Let an assistant manage apps

meecp has a separate [management MCP connection](docs/MANAGEMENT_MCP.md). Choose **Copy Management Connection** and paste the copied instructions and JSON configuration into your agent, then approve inspection/planning in the native popup. Remembered approval applies to this integration under your macOS account. Each connection receives a fresh scoped credential that expires and can be revoked. The installed facade can launch its containing app once when needed. Explicit owner-issued file grants remain available for read, prepare, selected app controls, and one reviewed installation plan.

Preparation access can save drafts and attach packages, but installation requires its own grant. Changing startup policies, removing apps, and issuing or revoking grants remain owner actions. Copy the management configuration into your MCP client using the guide; each app's ordinary MCP connection continues to expose its own tools.

## Put a local model to work

[JevK5-4B](https://github.com/allebee/jevk5) is the first model packaged for meecp. It evaluates evidence against a question you supply and returns probabilities for the possible answers. You can ask a yes/no question, choose among named alternatives, or score something against ordered criteria.

For example, an assistant can call `jevk5_decide` with:

```json
{
  "state": "Refunds require a receipt and a purchase within 30 days. The customer bought 12 days ago and has no receipt.",
  "question": {
    "type": "noul",
    "instructions": "Is a refund permitted under the policy?"
  }
}
```

The result includes probabilities, model and profile identity, token count, latency, and cache information. `jevk5_status()` reports readiness and aggregate counts. Calls share one model engine and serialize complete decisions; the cache reuses only identical consecutive token sequences. The assistant chooses when to call it and what to do with the result. Probabilities need validation against your own task before you use them as decision thresholds.

To prepare JevK5-4B from source, first build meecp as above. The current model packager also needs the legacy `dist/JevK5.app` bundle and the pinned GGUF in `models/`. Have a compatible Metal-enabled Homebrew `llama-server` available: the verified runtime is llama.cpp build 11146 with ggml 0.25.3, and the builder expects that Homebrew library/plugin layout. See the [runtime verification](docs/VERIFICATION.md) and [model manifest](model-manifest.json).

```sh
uv run --locked python scripts/fetch_model.py
uv run --locked python scripts/build.py --llama-server /absolute/path/to/llama-server
uv run --locked python scripts/prepare_jevk5_package.py --legacy-app dist/JevK5.app --output dist/jevk5-package --host dist/meecp.app/Contents/Resources/host
```

The fetch verifies the pinned revision, size, and SHA-256; the model download is about 4.48 GB. Replace the `llama-server` placeholder with the actual executable path. Add the resulting `dist/jevk5-package` through **Add App**. If you already use the standalone JevK5 app, the [migration guide](docs/MEECP_STATUS.md#import-the-existing-jevk5-installation) covers its explicit port, credential, and login-policy transfer.

## Where things stand

The current host supports `mcp-stdio` packages for CPU tools, authenticated `mcp-http` server factories, and `model-service` packages with a shared model engine. JevK5-4B and JSON Utilities have been tested together. Source imports, saved drafts, internal Pi setup, external-agent preparation, scoped management MCP and reviewed upgrades are implemented. The host currently permits one resident GPU package; first-call wake loading remains unsupported.

**Start at Login** is implemented through macOS ServiceManagement; registration and next-login behavior for the development bundle still need verification. Reviewed package upgrades and exposure-aware recovery are implemented. Live request counters are limited to the current host session, while model-runtime statistics reset with that runtime.

Services listen on `127.0.0.1`, require authentication, and check Host/Origin headers. The host does not log tool inputs, outputs, or raw exception payloads. Packages run with your account's permissions, so install code you trust. The [implementation guide](docs/MEECP_STATUS.md) covers supported package types, resource limits, and recovery.

## Development and docs

Run the repository tests with:

```sh
./test.sh
```

The script runs the locked Python suites for the host and hotdog demo, Swift tests, and the four native app self-tests below. It works from any working directory and stops at the first failure. Swift compiles the test targets and debug executable as needed; the script does not package an app or DMG.

Individual checks can also be run directly:

```sh
uv run --locked python -m pytest -q
(cd examples/hotdog-or-not-hotdog && uv run --locked --group build python -m pytest -q)
swift test
.build/debug/MeecpMenuBar --native-self-test
.build/debug/MeecpMenuBar --dashboard-ui-self-test
.build/debug/MeecpMenuBar --management-ui-self-test
.build/debug/MeecpMenuBar --pi-setup-ui-self-test
```

For the opt-in installer lifecycle checks, run `MEECP_RUN_INSTALL_LIFECYCLE_TESTS=1 swift test`; they create and replace synthetic app bundles. See [macOS distribution](docs/MACOS_DISTRIBUTION.md) for the checks and their limits. UI previews are available through `.build/debug/MeecpMenuBar --ui-preview` and `--manage-preview`, without modifying host state.

- [Rename and compatibility](docs/RENAME.md): current program names, retained installation identities, and legacy package support.
- [Implementation guide](docs/MEECP_STATUS.md): CLI usage, builds, package contracts, and migration.
- [Host design](docs/MEECP_DESIGN.md): architecture and component contracts.
- [Source onboarding](docs/PHASE2_ONBOARDING.md), [package authoring](docs/PACKAGE_AUTHORING.md), and [management MCP](docs/MANAGEMENT_MCP.md): preparation, review, and assistant access.
- [Pi setup](docs/PI_SETUP.md): internal coding agent, model settings, dependency support, and execution/verification contracts.
- [macOS distribution](docs/MACOS_DISTRIBUTION.md) and [GitHub releases](docs/GITHUB_RELEASES.md): installation and release verification.
- [Verification](docs/MEECP_VERIFICATION.md): tested behavior and known limits.
- [Visual identity](docs/BRAND.md): artwork, icons, and the [menu bar preview](assets/brand/icon-preview.html).
- [Model provenance](model-manifest.json), [model card](docs/MODEL_CARD.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
