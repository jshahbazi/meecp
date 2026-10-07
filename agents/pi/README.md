# Private Pi worker

mcpme embeds `@earendil-works/pi-coding-agent` 1.0.2 behind a JSONL host broker.
The worker runs an actual Pi coding session with the user's selected provider,
model, and API key. The host controls every filesystem write, subprocess, and
download requested by the model. The worker does not execute Pi's built-in
tools, discover extensions, load MCP servers, or read Pi's stored credentials.

The SDK's credential store, settings, conversation, model store, and resource
loader are supplied explicitly. Conversation and credentials stay in memory.
There are no session files, automatic catalog refreshes, or agent retries.
Automatic context compaction keeps long coding sessions usable; summaries and
the conversation remain in memory and use the selected provider. Unknown
built-in model IDs fail with a fixed error. The
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

- `capabilities`: no arguments; discover current host facilities and package requirements.
- `list_files`: optional `path`, `recursive`, `offset`, and `limit` (up to 500).
- `read_file`: `path`, optional byte `offset` and `limit` (up to 131,072).
  Paged reads return `content`, `next_offset`, and `eof`.
- `write_file`: `path`, `content`.
- `run_command`: `argv`, optional `cwd`, `timeout_seconds` (up to 1,800), and
  `network` boolean. Network access defaults to disabled. Optional `env` supplies
  nonsecret values; `credentials` maps environment names to opaque secret IDs
  returned by this job's `ask_user` requests. The host injects values privately
  and suppresses command output when credential bindings are present.
- `start_process`: `argv`, optional `cwd`, `network`, `env`, and `credentials`;
  returns a process ID. Credential-bearing process output is suppressed.
- `read_process`: `process_id`; inspect bounded recent output and current state.
- `stop_process`: `process_id`; stop and reap the owned background process.
- `fetch_url`: `url`, `path`, optional expected `sha256`.
- `extract_archive`: `archive_path`, `target_path`, optional `strip_components`
  (0 through 16).
- `prepare_environment`: `runtime`, optional `target_path`; use an available
  private-runtime helper.
- `install_dependencies`: `ecosystem`, `project_path`, optional `target_path`.
  Ecosystem names are open strings. The host reports available helpers through
  `capabilities`; other environments can be built with acquired toolchains and
  network-enabled package-manager commands.
- `ask_user`: `kind` (`text` or `secret`), `question`, optional `options`, and
  `credential_name` for a secret request. A secret reply provides an opaque
  binding ID without exposing the value to the model.
- `check_package`: no arguments. The host independently checks `package/` and
  runs its MCP fixtures, returning fixed failure feedback for repair.

Reply with `tool_result`, the same `id`, a boolean `success`, and string `output`.
Broker frames carry task data for execution and are not logs. Do not persist them.
The terminal frame is `done` with `success: true` or `failed` with a fixed `code`.
Closing stdin or sending `cancel` aborts the session and pending tools. The worker
never forwards assistant text, provider errors, or credentials to stdout/stderr.
The host must independently verify the generated package before installation.
The worker bounds a run to 1,024 broker calls, 512 coding turns, and twelve final
verification/repair cycles. The host also enforces its deadline and resource
budgets. A repair cycle uses the same in-memory session; context compaction does
not create a persistent transcript or restart the integration from scratch.

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
