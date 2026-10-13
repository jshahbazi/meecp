# jevk5-stark-v1

Version 1 of the experimental Stark JevK5 package generator. It creates package
`local.jevk5.stark` with schema 1, host API 1, `model-service`, and tools-only MCP
`2025-11-25` for Apple silicon macOS 26 or newer.

Run the generator from a Meecp checkout with a verified official build from
[`stark_dependency.py`](../../scripts/stark_dependency.py), the unchanged converted
BF16 JevK5 folder, and an existing bundled Meecp host's private runtime:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/prepare_stark_jevk5_package.py \
  --build /absolute/path/to/verified-stark-build \
  --model /absolute/path/to/verified-jevk5-bf16 \
  --runtime /absolute/path/to/bundled-meecp-host \
  --output /absolute/path/to/fresh-package-directory
```

The runtime argument accepts a host directory containing `python/`, `build.json`,
and `bin/meecp-reap`, an app containing `Contents/Resources/host`, or its Resources
directory containing `host/`. The legacy `bin/mcpme-reap` spelling is accepted
only when the selected host's build receipt binds those exact bytes.

The generator rejects an existing output, development-only Stark build receipts,
modified pins or worker bytes, changed input seals, incomplete or changed model
files, and model or source symlinks. It resolves only internal regular file links
in the selected private Python runtime, copies regular bytes, and removes Python
bytecode caches. It checks the worker and reaper's arm64/system-library closure,
compiles bounded native launchers with muted compiler output, writes the complete
package lock, and validates the result with the copied private interpreter.

Read [Stark JevK5 integration](../../docs/STARK_JEVK5.md) for the process graph,
configuration, behavior, acceptance scope, and rollback path. The existing
[`jevk5-llamacpp-v1`](../jevk5-llamacpp-v1/README.md) package remains separately
available. The experimental package uses a distinct package ID so both candidates
can be kept without replacing an installed package implicitly.

Consecutive whole-vocabulary caching defaults off. Add the explicit
`--consecutive-cache` flag when creating a separately sealed cache-enabled
experimental package. The selected setting appears in the package parameters
and preparation receipt.
