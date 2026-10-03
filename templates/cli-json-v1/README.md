# CLI JSON preparation spike

This unregistered development template prepares one deterministic, foreground Python stdlib CLI using the private CPython runtime and locked MCP SDK. It exposes tools-only MCP 2025-11-25 with bounded JSON stdin/stdout, fixed argv positions, exit mappings and a deadline. The macOS native launch profile denies command forks/spawns, network, external-source reads and package writes. The adapter journals only bounded ownership/resource metadata outside the sealed package.

Directory inspection and generated questions supply the inline command contract without a package manifest, lock or runtime tree. A directory may optionally provide `mcpme.cli.json` with the same contract. `CliJsonFormProvider` and `CliJsonComponent` remain available for integration; no shared catalog, routing, capability or dispatcher is changed here.

The package generator requires exact reviewed native `mcpme-reap` and `mcpme-cli-launch` artifacts. The runtime requires an explicit owner-only `MCPME_CLI_STATE_DIRECTORY`. Source code cannot choose arbitrary executables, shell strings or existing services. Packages and native helpers must pass admission before installed execution is supported.

Use `uv run --locked python scripts/verify_phase3_cli.py` for the real directory/forms/build/SDK/relocation checkpoint. See [the ownership report](../../docs/PHASE3_CLI_SPIKE.md) for implementation, tests, evidence boundaries and required integration. A3 remains open for descendant-capable topology, installed-host acceptance, live aggregate memory enforcement and independent release review.
