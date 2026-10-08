# existing-mcp-v1

Template version 1. Package schema 1, host API 1, Apple silicon macOS 26+, tools-only MCP `2025-11-25`.

Use this for an independently launchable foreground stdio MCP server whose session state belongs to one child process, or a tools-only Python MCP factory served by one owned HTTP resident. Stdio generator paths are a selected native executable, a Python module, or a Python script. Python paths receive the same private CPython and locked dependency closure as [python-function-v1](../python-function-v1/README.md). The native executable path requires all non-system runtime dependencies in the selected snapshot and relocated executable-relative library paths.

For an existing Python module:

```sh
uv run --locked --project /absolute/path/to/meecp python /absolute/path/to/meecp/templates/prepare.py existing-mcp \
  --setup-export /absolute/path/to/setup-export.json \
  --module my_server --tool-spec /absolute/path/to/tools.json \
  --source-license LICENSE --package-id local.my-server --name "My Server" \
  --output /absolute/path/to/agent-workspace/server-package
```

For a prebuilt native server, replace `--module my_server` with `--executable bin/server`. The executable path is relative to the exported snapshot. Add `--extra-executable PATH` only for selected runtime artifacts requiring an executable mode. For Python scripts, use `--script server.py`. Repeated `--entrypoint-arg=VALUE` options supply fixed, nonsecret arguments, including values beginning with `--`. Every tool declaration belongs in `tools.json`, as one object or a list using `name`, `description`, `input_schema`, optional `output_schema`, and optional MCP `annotations`.

The generator verifies selected file hashes and writes complete package and lock metadata. It does not start the supplied server, audit its semantics, or relocate native libraries. The selected snapshot must already contain the complete native closure. The current bounded source-folder intake excludes native binary files; use the prepared-package route for those artifacts until executable intake is supported. The native generator is available for agent-owned exact exports containing selected executables. A Node server requires a separately pinned, bundled Node runtime and a reviewed package recipe; it must not rely on global Node or `npx` downloads. Native snapshots are bounded by intake's file limits; large runtimes need another preparation path.

Check that stdout contains only MCP messages, errors and logs never disclose caller data, all calls have finite deadlines, and the server does not launch detached children. The Python bootstrap disables ordinary logging and discards stderr; third-party tool handlers still require review for exception details returned in MCP responses. Verify tool schemas and structured results with the locked SDK. A declaration in `tools.json` must match the server's actual `tools/list` result exactly. The low-level SDK's default initialization advertises `experimental: {}`; remove that empty capability from initialization options so the server advertises only `tools`, as in the callable adapter.

For the HTTP profile, revise the draft requirements to `profile: "mcp-http"` and select a Python module factory that returns a configured low-level `mcp.server.Server`. The factory registers the declared `tools/list` and `tools/call` handlers and returns once. It does not bind a listener, run an event loop, or start another server. Add `--http-factory create_server` to the module generator command above. The generator supplies the private HTTP wrapper, authentication policy, one adapter process, identity readiness declaration, and `descriptor-v1` injection contract. It imports no selected source during preparation.

The host launches that resident after an authorized install. An inherited `--meecp-config-fd` supplies its selected loopback port, private key, generation, nonce, and finite lifecycle bounds. The wrapper binds `127.0.0.1`, authenticates every route, checks Host and Origin, exposes authenticated `/health`, and serves stateful Streamable HTTP at `/mcp` with explicit DELETE session termination. Public HTTP and stdio clients share the resident through the host gateway. Restart creates a new generation and private key. The package needs no original source folder, global interpreter, or agent workspace at runtime.

Supply optional `--configuration-spec /absolute/path/to/configuration.json` for the closed typed configuration declaration and `--access-spec /absolute/path/to/access.json` for declared access and secret names. The host delivers configured values and resolved secret material through `MCPME_SETTINGS_FD`; factories may read that descriptor once at startup. Only nonsecret declarations and secret reference IDs belong in requirements or exported files. Never print settings or place secret material in source, package metadata, argv, or environment values. Supply `--validation-spec /absolute/path/to/validation.json` for at most four synthetic `{ "tool", "arguments", "expected" }` fixtures. Inspection validates their tool schemas and 16 KiB per-fixture bound without execution; activation must receive each expected structured result before exposure.

The factory path supports the locked tools-only SDK contract. An arbitrary HTTP executable or a server that selects its own endpoint, lacks private authentication or generation readiness, ignores injected configuration, or cannot terminate sessions is blocked with `http_factory_contract_required`, `http_configuration_required`, or `http_identity_contract_required`. Servers requiring sampling, roots, elicitation, resources, prompts, tasks, detached daemons, or adoption of a running service remain blocked. Report the specific missing capability without a raw execution dump.

Follow [package authoring](../../docs/PACKAGE_AUTHORING.md) for dependencies, access declarations, synthetic verification, and returning to the exact draft revision.
