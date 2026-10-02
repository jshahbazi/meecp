# existing-mcp-v1

Template version 1. Package schema 1, host API 1, Apple silicon macOS 26+, tools-only MCP `2025-11-25`.

Use this for an independently launchable foreground stdio MCP server whose session state belongs to one child process. The supported generator paths are a selected native executable, a Python module, or a Python script. Python paths receive the same private CPython and locked dependency closure as [python-function-v1](../python-function-v1/README.md). The native path requires all non-system runtime dependencies in the selected snapshot and relocated executable-relative library paths.

For an existing Python module:

```sh
uv run --locked --project /absolute/path/to/mcpme python /absolute/path/to/mcpme/templates/prepare.py existing-mcp \
  --setup-export /absolute/path/to/setup-export.json \
  --module my_server --tool-spec /absolute/path/to/tools.json \
  --source-license LICENSE --package-id local.my-server --name "My Server" \
  --output /absolute/path/to/agent-workspace/server-package
```

For a prebuilt native server, replace `--module my_server` with `--executable bin/server`. The executable path is relative to the exported snapshot. Add `--extra-executable PATH` only for selected runtime artifacts requiring an executable mode. For Python scripts, use `--script server.py`. Repeated `--entrypoint-arg=VALUE` options supply fixed, nonsecret arguments, including values beginning with `--`. Every tool declaration belongs in `tools.json`, as one object or a list using `name`, `description`, `input_schema`, optional `output_schema`, and optional MCP `annotations`.

The generator verifies selected file hashes and writes complete package and lock metadata. It does not start the supplied server, audit its semantics, relocate native libraries, or convert an HTTP service to stdio. The selected snapshot must already contain the complete native closure. The current bounded source-folder intake excludes native binary files; use the prepared-package route for those artifacts until executable intake is supported. The native generator is available for agent-owned exact exports containing selected executables. A Node server requires a separately pinned, bundled Node runtime and a reviewed package recipe; it must not rely on global Node or `npx` downloads. Native snapshots are bounded by intake's file limits; large runtimes need another preparation path.

Check that stdout contains only MCP messages, errors and logs never disclose caller data, all calls have finite deadlines, and the server does not launch detached children. The Python bootstrap disables ordinary logging and discards stderr; third-party tool handlers still require review for exception details returned in MCP responses. Verify tool schemas and structured results with the locked SDK. A declaration in `tools.json` must match the server's actual `tools/list` result exactly. The low-level SDK's default initialization advertises `experimental: {}`; remove that empty capability from initialization options so the server advertises only `tools`, as in the callable adapter.

The current host rejects native `mcp-http` packages. HTTP-only servers need a reviewed stdio adapter that fits the host-owned process contract, or must wait for the HTTP profile. Servers requiring sampling, roots, elicitation, resources, prompts, tasks, detached daemons, secret injection, adoption of a running service, or shared heavyweight state are blocked until the relevant host support exists. Report `unsupported_execution_profile`, `unsupported_process_topology`, `secret_reference_unavailable`, or the specific missing capability without a raw execution dump.

Follow [package authoring](../../docs/PACKAGE_AUTHORING.md) for dependencies, access declarations, synthetic verification, and returning to the exact draft revision.
