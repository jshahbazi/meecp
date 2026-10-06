# Private Pi worker

mcpme embeds `@earendil-works/pi-coding-agent` 1.0.2 behind a JSONL host broker.
The worker runs an actual Pi coding session with the user's selected provider,
model, and API key. The host controls every filesystem write, subprocess, and
public download requested by the model. The worker does not execute Pi's built-in
tools, discover extensions, load MCP servers, or read Pi's stored credentials.

The SDK's credential store, settings, conversation, model store, and resource
loader are supplied explicitly. Conversation and credentials stay in memory.
There are no session files, automatic catalog refreshes, agent retries, or compaction
requests. Unknown built-in model IDs fail with a fixed error. The
`openai-compatible` provider accepts an explicit model ID and compatible endpoint;
its conservative context/output budgets are 32,768/8,192 tokens.

## Host protocol

Launch `pi-runtime/bin/node pi-runtime/worker.mjs` with an empty private working
directory and a sanitized environment. If the machine uses an explicitly
configured extra TLS trust store, `NODE_EXTRA_CA_CERTS` must be set when Node
starts. The worker clears inherited environment variables before loading Pi.

Send one `start` frame with `provider`, `model`, `api_key`, `prompt`, and optional
`base_url`. Credentials belong in stdin, never command-line arguments.

The worker emits fixed `progress` stages and `tool_call` frames with `id`, `name`,
and `arguments`. Supported host tools are:

- `read_file`: `path`.
- `write_file`: `path`, `content`.
- `run_command`: `argv`, optional `cwd` and `timeout_seconds`.
- `fetch_url`: `url`, `path`, optional expected `sha256`.
- `install_dependencies`: `ecosystem`, `project_path`, optional `target_path`.
  Python preparation returns the private runtime paths and locked dependencies.
  Node dependency preparation currently returns an explicit unsupported error.
- `check_package`: no arguments. The host independently checks `package/` and
  runs its MCP fixtures, returning fixed failure feedback for repair.

Reply with `tool_result`, the same `id`, a boolean `success`, and string `output`.
Broker frames carry task data for execution and are not logs. Do not persist them.
The terminal frame is `done` with `success: true` or `failed` with a fixed `code`.
Closing stdin or sending `cancel` aborts the session and pending tools. The worker
never forwards assistant text, provider errors, or credentials to stdout/stderr.
The host must independently verify the generated package before installation.

## Reproducible runtime

`uv run python scripts/build_pi_runtime.py` downloads official Node 24.15.0 for
macOS arm64, checks its pinned SHA-256, then uses that Node's npm with the checked-in
lockfile. Dependency scripts are disabled and npm tarball integrity is checked.
The output contains Node, worker source, locked dependencies, provenance, licenses,
and a complete hash inventory. CLI links, documentation, test folders, declaration
files, and source maps are omitted from the dependency closure. All packaged files
are regular files. Source and licenses are retained in Git; runtime downloads,
dependencies, and output bundles are ignored.

The host builder includes this payload in `host/pi-runtime`. The native app
builder refreshes its inventory after signing changes native bytes. Inventory
verification belongs to the host before worker launch.

`npm ci --ignore-scripts --no-audit --no-fund && npm test` runs real SDK tests
against a synthetic loopback provider without a paid model request. To check the
bundled closure, run the same test file with the bundled Node and set
`MCPME_PI_TEST_WORKER` to the absolute bundled worker path.

The worker's host tools are an API boundary. They require the host's
workspace/process/network confinement; the
Node process alone provides no execution sandbox.
