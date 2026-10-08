# meecp rename

The program is named **meecp**. The canonical repository is [github.com/jshahbazi/meecp](https://github.com/jshahbazi/meecp); repository links and release automation use this destination. Current source and documentation use `meecp` for the Python packages, command launcher, build scripts, app bundle, release artifacts, and management MCP tools. The native Swift target and source directory are `MeecpMenuBar`; operator-facing environment settings use `MEECP_`.

Build with `uv run --locked python scripts/build_meecp_app.py`, then open `dist/meecp.app`. A relocated CLI host uses `dist/meecp-host/bin/meecp`. The architecture and implementation guides are [MEECP_DESIGN.md](MEECP_DESIGN.md) and [MEECP_STATUS.md](MEECP_STATUS.md).

## Compatibility identities

Some existing installation and package identities retain their original spelling:

| Identity | Retained value and purpose |
| --- | --- |
| Private installation state | `~/Library/Application Support/mcpme`; preserves installed apps, settings, credentials, and recovery records. |
| Signed bundle identifier | `com.jshahbazi.mcpme`; preserves the application's macOS identity. |
| Private socket directory | `/private/tmp/mcpme-<uid>-<state-hash>`; preserves discovery for existing local clients. |
| Package settings descriptor | `MCPME_SETTINGS_FD`; remains the published package process contract. |
| Immutable builtin recipe IDs | `mcpme.python-callable`, `mcpme.resolved-python`, `mcpme.native-command`, `mcpme.onnx-hotdog`, `mcpme.text-generation`, `mcpme.agent-python-adapter`, `mcpme.agent-unlocked-python-adapter`, and `mcpme.local-openai-text`. |
| Versioned digest and wire identities | Existing `mcpme` namespaces remain stable so saved plans, receipts, and sealed packages keep their identities. |
| Hosted release secret | `MCPME_COMMAND_SIGNED_ASSET_BASE64`; retains the configured GitHub environment secret name. |
| Frozen signed helpers | `mcpme-command-reap` and `mcpme-command-resource-reap`; preserves the reviewed archive filenames and signatures. The original `mcpme-command-resource-v1.h` include remains available to compile the unchanged signed-helper source. |
| Frozen TextGen launcher | `mcpme_textgen`; a compatibility entry point forwards to `meecp_textgen` and preserves exit status. |
| Dependency-index credentials | Existing `UV_INDEX_MCPME_PI_DEPENDENCIES_*` and `UV_INDEX_MCPME_RESOLUTION_*` variables remain valid package-building contracts. |

New packages use `meecp.package.json` and `meecp.lock.json`; preparation and recipe descriptors use `meecp.prepare.json`, `meecp.recipe.json`, and `meecp.recipe.lock.json`. New preparation receipts use `meecp.preparation.json`; existing receipts retain the filename bound by their artifact lock. Readers also accept a complete corresponding legacy `mcpme` filename pair. Mixed or duplicate descriptor pairs are rejected. Package process launchers accept `--meecp-config-fd` and the existing `--mcpme-config-fd` alias.

New management exports use the `meecp-management` connection key and `meecp_*` tool names. Existing `mcpme_*` management tool calls remain accepted aliases; both management connection names are reserved when naming ordinary app connections.

Current renamed builtin recipe contents are published under new patch versions, with their artifact locks recomputed. Existing recipe-version directories retain their original bytes and spelling so a saved immutable version never resolves to different content.

The active recipe versions are Python callable 1.0.1, resolved Python 1.0.2, ONNX hotdog 1.0.1, text generation 1.0.22, native command 1.0.2, agent Python adapter 1.0.1, unlocked agent adapter 1.0.2, and local OpenAI text 1.0.1. Current release-policy and analysis-guide references bind the renamed source hashes; their supported components, providers, and permission scopes remain the same.

The JevK5 model, `jevk5_decide` and `jevk5_status` tools, and separately buildable legacy `JevK5.app` keep their names.

## Recorded evidence

Historical verification records preserve the original names of measured bundles, source files, receipts, immutable Git revisions, and artwork prompts. Their hashes and measured results describe those original artifacts. Renaming the current source does not establish a new verification result for the renamed build.
