# python-function-v1

Template version 1. Package schema 1, host API 1, Apple silicon macOS 26+, MCP `2025-11-25`, locked MCP Python SDK `1.30.0`.

Use this for one explicitly selected callable with keyword inputs and a JSON object result. Both synchronous and asynchronous functions are supported. The callable runs inside its own foreground slot process. The adapter validates schemas, serializes calls within that process, returns structured content, suppresses ordinary source stdout/stderr, and returns content-free error codes. It does not import the source during preparation. It has no shared state across client sessions. A synchronous function that ignores cancellation remains bounded by the host process deadline and reaper; it cannot receive another concurrent call in the same adapter.

Read [package authoring](../../docs/PACKAGE_AUTHORING.md), identify the function and side effects, then write its tool specification. `tool.example.json` and `function.example.py` provide a synthetic addition fixture. Replace the schema and hints with the requested interface; determinism alone does not justify read-only or retry hints.

Run the common generator explicitly in the external agent workspace:

```sh
uv run --locked --project /absolute/path/to/mcpme python /absolute/path/to/mcpme/templates/prepare.py python-function \
  --setup-export /absolute/path/to/setup-export.json \
  --module arithmetic --function add_numbers \
  --tool-spec /absolute/path/to/tool.json \
  --source-license LICENSE \
  --package-id local.arithmetic --name "Arithmetic" \
  --output /absolute/path/to/agent-workspace/arithmetic-package
```

The generator copies only the exported, hash-verified snapshot file list. It writes the complete manifest, lock, source receipt, adapter, relocatable launcher, private CPython runtime, locked wheel closure, and licenses. `source_revision` is exactly the export's `source.revision`. The original checkout and global Python are unnecessary when this package runs. The output directory must be new. A failed preparation may leave its incomplete output; choose a new directory after addressing the content-free error.

The default dependencies are the mcpme root `uv.lock`. Additional dependencies need an agent-owned dependency project copied from the root `pyproject.toml` and `uv.lock`, resolved with `uv`, and passed through `--dependency-project`. Keep `mcp==1.30.0`; custom resolutions using another SDK are rejected. Editable packages, source-distribution builds, and runtime dependency downloads are outside this generator's supported path. Source modules are copied from the selected snapshot rather than installed as an editable package.

Use `--access-spec` for a JSON object containing declared `filesystem`, `network`, and `writes` fields when the function needs access. These declarations are reviewed metadata, not an OS sandbox. Secret references are unavailable in this milestone. Default memory is the export requirement or 128 MiB, with eight sessions and a 60-second operation deadline; choose measured limits with `--memory-mb`, `--max-sessions`, and `--timeout-seconds`.

Before returning the package, use a real MCP client to initialize, compare `tools/list` with the requested schema, and call the tool with synthetic input and expected output. For the example, `{ "left": 2, "right": 3 }` must produce `{ "total": 5 }`. Verify a rejected argument and a raised exception produce sanitized errors. Move the complete package, remove access to the original source, and repeat the call through `bin/service`. Preparation success is static validation; runtime checks remain necessary.

See the [return path](../../docs/PACKAGE_AUTHORING.md#return-to-the-draft). Attach uses the same draft ID and revision from the export. Attachment never runs the function or installs it.
