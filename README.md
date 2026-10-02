# mcpme

![mcpme: local MCP services with their own home](assets/brand/mcpme-banner.png)

**Give your AI tools a home on your Mac.**

mcpme runs local MCP tools and models from your macOS menu bar. Add a project, connect it to your AI assistant, and keep an eye on it while you work. MCP (Model Context Protocol) lets assistants call those tools; mcpme takes care of keeping them running.

Run a JSON validator alongside a local decision model. Connect several clients to the same model. Stop a project when you're done with it. Each project has its own controls and connection settings, all in one place.

## What you can do

- **See what's running.** Check each project's status from the menu bar, or open **Manage Projects** for details and installation progress.
- **Control projects individually.** Start, stop, or restart a service. Choose which projects start with the host and which you start yourself.
- **Connect your assistant.** Copy a project's connection configuration and add it to your MCP client. Its endpoint stays stable across restarts.
- **Share a local model.** JevK5 clients use one loaded model per project, so connecting another assistant doesn't load another copy.
- **Bring your own tools.** Install a prepared package, or start from source code and use an external coding agent to prepare it.

mcpme currently requires **Apple silicon and macOS 26 or later**. It's an early development build; the supported features and remaining work are listed below.

## Try it

Build the app and give it a first project: JSON Utilities, a small tool that checks whether a JSON document is valid and reports its top-level type. No model download is needed.

You'll need Xcode Command Line Tools and `uv`. From the repository root:

```sh
uv python install 3.12.13
uv sync --locked
uv run --locked python scripts/build_mcpme_app.py
uv run --locked python scripts/prepare_json_package.py
open dist/mcpme.app
```

The app bundles its own Python runtime and dependencies. Once built, it can run without `uv`, Homebrew, or the checkout. Builders require a fresh output path; for another app build, pass `--output dist/mcpme-dev.app`.

1. Open **Add Project…** from the menu bar and choose `dist/json-utilities-package`.
2. Check **Start after install**, then select **Review Package** to see the tools and declared access.
3. Select **Add & Start Project**.
4. Once the project is ready, use **Copy Connection Configuration** and add it to your MCP client's settings.

Your assistant can now call `validate_json(document)`. Connection formats vary by client; copying the configuration doesn't change its settings for you. If you move the app, copy the configuration again to get its new path.

Closing the Projects window keeps services running. Quitting mcpme stops the processes it owns.

## Bring your own tools

A useful script can become a tool your assistant calls. **Add Project** accepts local source folders and Markdown or text setup instructions, as well as prepared packages.

Describe what you want your assistant to do, then use **Copy Agent Instructions** to hand preparation to your coding agent. The agent builds a package in a separate workspace. Bring it back with **Import Prepared Package…**, review the tools and access it declares, and install it. Your draft is saved, so you can close the window and pick up later.

The agent does the work of exposing your code through MCP. mcpme inspects the source without running it, then checks the returned package before installation. This workflow is still being built out: reference-document services and dropping in raw model weights aren't supported yet. See the [onboarding guide](docs/PROJECT_ONBOARDING.md) for the workflow and delivery plan.

## Put a local model to work

JevK5 is the first model packaged for mcpme. It evaluates evidence against a question you supply and returns probabilities for the possible answers. You can ask a yes/no question, choose among named alternatives, or score something against ordered criteria.

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

The result includes probabilities, model and profile identity, token count, latency, and cache information. `jevk5_status()` reports readiness and aggregate counts. The assistant chooses when to call it and what to do with the result. Probabilities need validation against your own task before you use them as decision thresholds.

To add JevK5, build the original app using the instructions below, then run `uv run --locked python scripts/prepare_jevk5_package.py`. Add the resulting `dist/jevk5-package` through **Add Project**. If you already use the standalone JevK5 app, the [migration guide](docs/MCPME_STATUS.md#import-the-existing-jevk5-installation) covers transferring that installation.

## Where things stand

The current host runs prepared local tool and model packages, with JevK5 and JSON Utilities tested together. Source imports, saved drafts, and external-agent preparation are the next layer of onboarding now in development.

Downloads, upgrades, removal, a management MCP interface, and agent preparation inside the app are still ahead. Builds are locally signed development artifacts; notarization and an automatic updater are pending. The development bundle's start-at-login behavior still needs verification.

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

<details>
<summary>Standalone JevK5 app: build, connection, and runtime details</summary>

The original JevK5 Menu Bar app remains separately buildable. It bundles the Q8 model, Metal inference runtime, and Python server. Build tools are Xcode Command Line Tools, `uv`, and the tested Homebrew llama.cpp runtime:

```sh
uv sync --locked
uv run --locked python scripts/fetch_model.py
uv run --locked python scripts/build.py
open dist/JevK5.app
```

To reuse an existing GGUF, pass `--model-source /absolute/path/to/jevk5-4b-v0.3-Q8_0.gguf` to the builder. It verifies the model's size and SHA-256 before bundling it. Models, runtime files, and generated apps are ignored by Git.

Builds use the local `llama-server` executable, with an optional `--llama-server` override. Packaging copies and relocates its dependencies, checks for remaining Homebrew links, and signs the app. `runtime-manifest.json` records the bundled library hashes. The measured artifact used llama.cpp 0.5.0/build 11146, ggml 0.25.3, and Python 3.12.13; Python dependencies are pinned in `uv.lock`.

Opening `dist/JevK5.app` loads the model and serves MCP at `http://127.0.0.1:19385/mcp`. The menu shows readiness and counts, offers pause/resume and restart, and copies connection configuration. Pausing unloads the model; decision calls fail until you resume. Quitting stops both owned service processes.

Use **Copy MCP Configuration** to get the bundled stdio adapter's path for your client. The adapter opens the app when needed, reads its token privately, and connects to the shared backend. Copy the configuration again after moving the app.

For Codex, `uv run --locked python scripts/install_codex.py` registers this standalone build. It preserves other servers and existing timeouts, adding 30-second startup and 180-second tool timeouts if absent. Restart the JevK5 MCP connection to load the tools into an existing session. Remove the registration with `codex mcp remove jevk5`.

Streamable HTTP clients can use `http://127.0.0.1:19385/mcp` with `Authorization: Bearer <token>`. The token lives in `~/Library/Application Support/JevK5/connection.json`, readable by your account. Prefer the stdio adapter, which handles the token for you. Keep the connection file out of Git, issues, and chat.

For `choice`, supply 2–16 distinct labels in `criteria`, or an ordered object mapping IDs to descriptions. For `score`, supply a list of 2–16 descriptions for ordered levels 0 through N−1. Boolean (`noul`) questions can optionally describe `true` and `false`. Requests exceeding the 8192-token context budget are rejected. JevK5 has no retrieval or general chat interface.

Decisions run one at a time. Prompt caching applies only when the full token sequence matches the immediately preceding successful completion. Changing the evidence, question, or ordered options disables reuse. Errors, pause, restart, and quit clear that identity. There is no response history or disk response cache; live inference state stays in memory while the model runs.

The `m5-benchmark-v1` profile uses temperatures 1.22 and 0.93, matching the prior benchmark and pinned upstream GGUF card. Each result identifies its profile so calibration changes can be tracked.

See the [JevK5 design](docs/DESIGN.md) and [verification report](docs/VERIFICATION.md) for the original app's runtime contract and synthetic integration checks.

</details>
