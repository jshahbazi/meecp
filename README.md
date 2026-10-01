# mcpme

Project repository: [jshahbazi/mcpme](https://github.com/jshahbazi/mcpme).

`mcpme` is being developed as a native macOS menu bar host for independently installed MCP service slots, including models and deterministic tools. The [modular host design](docs/MCPME_DESIGN.md) defines slot management, agent installation, and bringing your own programs or models.

The [mcpme implementation guide](docs/MCPME_STATUS.md) covers the native Projects window, prepared local packages, independent deterministic slots, and JevK5 as a shared model slot. It includes build instructions, CLI usage, and the explicit legacy import workflow. [Verification evidence](docs/MCPME_VERIFICATION.md) records the tested scope.

The existing JevK5 Menu Bar implementation keeps JevK5-4B available as a local MCP decision tool. Its built app contains the Q8 model, Metal inference runtime, and its own Python server. It requires Apple silicon and macOS 26 or newer. The instructions below apply to that working baseline.

JevK5 applies a supplied criterion to supplied evidence and scores declared alternatives. It supports boolean probability (`noul`), categorical choice (`choice`), and expected score (`score`). It has no retrieval or general chat interface. An MCP client chooses when to call it.

## Run

Open `dist/JevK5.app`. The app's symbol appears in the menu bar. It loads the model and exposes the MCP service at `http://127.0.0.1:19385/mcp`. The menu shows readiness and aggregate counts, can pause/resume the model, copy MCP configuration, restart the service, and register launch at login. Pausing unloads the model; MCP decision calls fail until resumed. Quitting stops both owned service processes.

The generated app is a local ad hoc signed development build. It has no notarization, automatic updater, or Intel build. The source and model provenance remain in this repository. Large models and bundles are ignored by Git.

## Connect an MCP client

To register this local build with Codex, run `uv run scripts/install_codex.py`. This adds the bundled stdio command to `~/.codex/config.toml`, preserves other servers, and supplies 30-second startup and 180-second tool timeouts if absent. The desktop app and CLI share this configuration. Restart the JevK5 MCP connection in Codex settings to load its tools into an existing session. Setup follows the [official MCP configuration documentation](https://developers.openai.com/codex/mcp). To remove this registration, run `codex mcp remove jevk5`.

Use **Copy MCP Configuration** in the menu. For a client supporting the common JSON `mcpServers` format, the configuration looks like:

```json
{
  "mcpServers": {
    "jevk5": {
      "command": "/Users/johnshahbazian/development/mcpme/dist/JevK5.app/Contents/MacOS/jevk5-mcp",
      "args": []
    }
  }
}
```

The bundled stdio adapter opens the app if necessary and connects to its shared backend. Adding more clients does not load additional copies of the model. After moving the app, copy the configuration again so its executable path follows the new location. Different clients have different setup formats; the command above is the executable to register.

Clients supporting Streamable HTTP can use `http://127.0.0.1:19385/mcp` with `Authorization: Bearer <token>`. The per-install token is in `~/Library/Application Support/JevK5/connection.json`, readable only by your account. Prefer the stdio adapter, which reads the token itself. Never paste that connection file into Git, issues, or chat.

The tools are:

- `jevk5_decide(state, question)`: a typed decision with probabilities, model/profile identity, input tokens, latency, and exact-repeat cache metadata.
- `jevk5_status()`: readiness and aggregate counts.

Example tool arguments:

```json
{
  "state": "Refunds require a receipt and a purchase within 30 days. The customer bought 12 days ago and has no receipt.",
  "question": {
    "type": "noul",
    "instructions": "Is a refund permitted under the policy?"
  }
}
```

For `choice`, provide 2–16 distinct labels in `criteria`, or an ordered object mapping IDs to descriptions. For `score`, provide 2–16 descriptions for ordered levels 0 through N−1. Boolean questions can optionally provide descriptions for `true` and `false`. Prompts that exceed the 8192-token context budget are rejected. Confidence is model probability, not a validated decision threshold for your task.

## Build from source

Build tools: Xcode Command Line Tools, `uv`, and the tested Homebrew llama.cpp runtime. The packaged app itself does not need these tools at run time.

```sh
uv sync --frozen
uv run scripts/fetch_model.py
uv run scripts/build.py
open dist/JevK5.app
```

To reuse an existing verified GGUF, supply its path to `uv run scripts/build.py --model-source /absolute/path/to/jevk5-4b-v0.3-Q8_0.gguf`. The builder verifies the model's byte count and SHA-256, copies it into `models/`, and bundles it. APFS clone copying reduces duplicate disk allocation while preserving independent files.

Builds use the current local llama-server executable (override with `--llama-server`). Packaging recursively copies and relocates its dependencies, including Metal and CPU plugins, verifies no linked Homebrew path remains, and signs the finished app. `runtime-manifest.json` inside Resources records the bundled library hashes. To reproduce the measured local artifact, use llama.cpp 0.5.0/build 11146, ggml 0.25.3, and Python 3.12.13; Python dependencies are pinned in `uv.lock`.

Run the repository checks with `uv run --locked python -m pytest`. The app integration script and [verification report](docs/VERIFICATION.md) record synthetic end-to-end tests against the original packaged model; the modular host has its own [Phase 1 verification](docs/MCPME_VERIFICATION.md).

## Caching and local data

One shared lock serializes complete decisions. Prompt caching is enabled only when the full token sequence equals the immediately preceding successful completion. Changing evidence, criterion, or ordered options disables reuse. Errors, pause, restart, and quit clear the remembered identity. There is no response history or disk response cache. The model keeps live inference state in memory while running.

The named `m5-benchmark-v1` profile explicitly uses temperatures 1.22 and 0.93, matching both the prior benchmark and the pinned upstream GGUF card. Profile identity is returned with each result so users can assess and version calibration changes. This app does not establish probability calibration for a new task.

All local routes require a random bearer token and validate Host/Origin. Both services bind to 127.0.0.1. Request bodies, model outputs, and exception payloads are not logged. Processes running as your user can read the connection file and are within the app's trust boundary.

See [design](docs/DESIGN.md), [model provenance](model-manifest.json), the pinned [upstream model card](docs/MODEL_CARD.md), and [third-party notices](THIRD_PARTY_NOTICES.md).
