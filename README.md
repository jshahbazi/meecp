# mcpme

![mcpme: local MCP services with their own home](assets/brand/mcpme-banner.png)

**Give your AI tools a home on your Mac.**

mcpme runs local MCP tools and models from your macOS menu bar. Add an app, connect it to your AI assistant, and keep an eye on it while you work. MCP (Model Context Protocol) lets assistants call those tools; mcpme takes care of keeping them running. An app is a configured local tool or model service with its own controls and connection settings.

Run a JSON validator alongside a local decision model. Connect several clients to the same model. Stop an app when you're done with it. Manage them all in one place.

## What you can do

- **See what's running.** The menu groups apps that need attention, running apps, and stopped apps. See incoming requests, recent traffic, and work in flight at a glance. Click an app to open its live details to the right, with controls, failures, latency, tools, and connection setup.
- **Manage apps in one place.** Search and filter the app sidebar, then select an app for live requests, tools, connection details, and recent operations. Start, stop, or restart a service. Choose **Manual** or **Automatic when mcpme opens**, or remove the app while keeping its data for recovery.
- **Connect your assistant.** Copy an app's connection configuration and add it to your MCP client. Its endpoint stays stable across restarts.
- **Share a local model.** Use a model of your choice through a prepared package, and connect multiple assistants to the same running app.
- **Bring your own tools.** Install a prepared package, or start from source code and use an external coding agent to prepare it.

mcpme currently requires **Apple silicon and macOS 26 or later**. It's an early development build; the supported features and remaining work are listed below.

## Try it

Build mcpme and give it a first app: JSON Utilities, a small tool that checks whether a JSON document is valid and reports its top-level type. No model download is needed.

You'll need Xcode Command Line Tools and `uv`. From the repository root:

```sh
uv python install 3.12.13
uv sync --locked
uv run --locked python scripts/build_mcpme_app.py
uv run --locked python scripts/prepare_json_package.py
open dist/mcpme.app
```

The app bundles its own Python runtime and dependencies. Once built, it can run without `uv`, Homebrew, or the checkout. Builders require a fresh output path; for another app build, pass `--output dist/mcpme-dev.app`.

1. Open **Add App…** from the menu bar and choose `dist/json-utilities-package`.
2. Check **Start after install**, then select **Review Package** to see the tools and declared access.
3. Select **Add & Start App**.
4. Once the app is ready, use **Copy Connection Configuration** and add it to your MCP client's settings.

Your assistant can now call `validate_json(document)`. Connection formats vary by client; copying the configuration doesn't change its settings for you. If you move the app, copy the configuration again to get its new path.

Closing the Apps window keeps services running. Quitting mcpme stops the processes it owns. Changing an app's startup choice affects future launches. An explicit Stop remains in effect until Start clears it. Removing an app stops its owned processes and closes its connection; its source, package, and retained data remain available for recovery.

To build a drag-to-Applications installer from current source:

```sh
uv run --locked python scripts/build_mcpme_dmg.py
```

Open `dist/mcpme-0.1.0-arm64.dmg`, open `mcpme.app` on the image, and choose **Install and Restart**. The installer verifies the new bundle, gracefully quits the current copy, replaces it, and opens the installed app. The prompt shows the destination in `/Applications` or `~/Applications`. Your installed apps and settings are preserved; services follow their saved startup policies after restart.

The installer waits up to 75 seconds for the current app to quit. A refusal or timeout stops replacement. A confirmed launch failure restores the previous bundle; an unconfirmed launch leaves a recovery copy. Installing manually by dragging the app from the DMG to Applications requires quitting mcpme before copying, then opening the installed app.

This is an ad hoc development build. See [macOS distribution](docs/MACOS_DISTRIBUTION.md) for signing, installation, and verification, and the [host application update plan](docs/AUTO_UPDATE_PLAN.md) for the proposed automatic updater.

## Bring your own tools

A useful script can become a tool your assistant calls. **Add App** accepts local source folders and Markdown or text setup instructions, as well as prepared packages.

Describe what you want your assistant to do, then use **Copy Agent Instructions** to hand preparation to your coding agent. The agent builds a package in a separate workspace. Bring it back with **Import Prepared Package…**, review the tools and access it declares, and install it. Your draft is saved, so you can close the window and pick up later.

The agent does the work of exposing your code through MCP. mcpme inspects the source without running it, then checks the returned package before installation. This workflow is still being built out: reference-document services and dropping in raw model weights aren't supported yet. See the [onboarding guide](docs/PROJECT_ONBOARDING.md) for the workflow and delivery plan.

Phase 2 provides [resumable source imports and external preparation](docs/PHASE2_ONBOARDING.md), plus [scoped management grants and a separate management MCP connection](docs/MANAGEMENT_MCP.md). The [package-authoring guide](docs/PACKAGE_AUTHORING.md) and versioned templates support preparation in an external agent workspace. Downloads and upgrade/rollback remain pending.

## Put a local model to work

[JevK5-4B](https://github.com/allebee/jevk5) is the first model packaged for mcpme. It evaluates evidence against a question you supply and returns probabilities for the possible answers. You can ask a yes/no question, choose among named alternatives, or score something against ordered criteria.

For example, an assistant can call `JevK5_decide` with:

```json
{
  "state": "Refunds require a receipt and a purchase within 30 days. The customer bought 12 days ago and has no receipt.",
  "question": {
    "type": "noul",
    "instructions": "Is a refund permitted under the policy?"
  }
}
```

The result includes probabilities, model and profile identity, token count, latency, and cache information. `JevK5_status()` reports readiness and aggregate counts. The assistant chooses when to call it and what to do with the result. Probabilities need validation against your own task before you use them as decision thresholds.

To add JevK5-4B, build the original app using the instructions below, then run `uv run --locked python scripts/prepare_JevK5_package.py`. Add the resulting `dist/JevK5-4B-package` through **Add App**. If you already use the standalone JevK5-4B app, the [migration guide](docs/MCPME_STATUS.md#import-the-existing-JevK5-4B-installation) covers transferring that installation.

## Where things stand

The current host runs prepared local tool and model packages, with JevK5-4B and JSON Utilities tested together. The first Phase 2 onboarding milestone adds source imports, saved drafts, and external-agent preparation.

Downloads, upgrades, and agent preparation inside the app are still ahead. Scoped management MCP is available through an owner-issued grant. Local builds are ad hoc development artifacts; the protected-main [release workflow](docs/GITHUB_RELEASES.md) signs and notarizes public installers. An automatic updater is pending; see the [host application update plan](docs/AUTO_UPDATE_PLAN.md). The development bundle's start-at-login behavior still needs verification.

Services listen on `127.0.0.1`, require authentication, and check Host/Origin headers. The host does not log tool inputs, outputs, or raw exception payloads. Packages run with your account's permissions, so install code you trust. The [implementation guide](docs/MCPME_STATUS.md) covers supported package types, resource limits, and recovery.

## Development and docs

Run the repository tests with:

```sh
uv run --locked python -m pytest
```

- [Implementation guide](docs/MCPME_STATUS.md): CLI usage, builds, package contracts, and migration.
- [Host design](docs/MCPME_DESIGN.md): architecture and planned features.
- [Verification](docs/MCPME_VERIFICATION.md): tested behavior and known limits.
- [Visual identity](docs/BRAND.md): artwork, icons, and the [menu bar preview](assets/brand/icon-preview.html).
- [Model provenance](model-manifest.json), [model card](docs/MODEL_CARD.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
