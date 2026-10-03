# Combined CLI host checkpoint

This development integration combines the reviewed host-command implementation with the common preparation host at `6a26f7b709fa480067c6b9e8b81330c8c5ce3d9d`. A3 remains open. Production command admission remains disabled.

## Distribution contract

The CLI builder compiles, ad-hoc signs and hashes `bin/mcpme-command-reap`. Its `command-execution.json` inventory binds helper/source identities, macOS 26 arm64, explicit package schema 2, component `macos26-arm64-command-v3`, and publication receipt format 3. The inventory declares disabled production admission and the remaining aggregate-resource and signed-native-app gates. No runtime code interprets this inventory as permission. The existing trusted Supervisor constructor remains the sole development admission seam. The app builder refreshes the inventory's helper hash after signing before it refreshes the enclosing host receipt.

The host receipt hashes every payload file, including private Python, installed dependencies and launchers. The runtime copier rejects external or directory symlinks, bounds traversal and bytes before copying, emits regular files, and excludes bytecode. The harness requires bytecode-disabled execution and an exact complete file set.

Textgen component and recipe 1.0.4 bind the combined backend implementation. Previous immutable bundles remain byte-identical. The scanner adds only the new exact provenance-file path to its existing exact public-tokenizer-digest exception; rules and value matching remain unchanged.

`scripts/verify_phase3_command_host.py` must run with the selected relocated host's Python and requires a clean source receipt. It checks every receipt artifact hash before parsing the command inventory. It imports the implementation and MCP SDK exclusively from that host, constructs development admission, and installs sealed synthetic command packages. It checks both MCP transports/full schemas, fork/exec settlement, forbidden group escape, descriptor closure, cancellation, backend SIGKILL and held ancestor-group SIGKILL followed by durable command reconciliation. Ancestor-group death can leave the legacy gateway receipt unresolved; the harness does not infer or fabricate gateway completion.

## Automatic preparation still missing

The original CLI requirement in `AUTOMATIC_PREPARATION.md` includes the supported source-to-recipe/build/probe/attachment route. Installing an already constructed schema-2 fixture does not prove that route. The current automatic CLI component still binds the earlier no-fork topology. This patch does not register the fork-capable profile as a preparation family or enable CLI discovery.

The next minimal family should introduce a separately versioned `host-command-json-v1` component and descriptor that binds one selected executable closure, fixed argv entries, named bounded variable positions, full structural schemas, exact effects and the approved helper/policy/receipt identity. It should use existing revision-bound source selection, immutable recipe registry, controlled builder and durable job receipt machinery. The builder must produce explicit schema-2 host-command packages, with the package/closure/tool/generation authority checked again at the Supervisor seam. Its probe must use the same host-owned invocation facility rather than spawn a nested helper under the existing probe owner group. The common verifier and attachment gate currently expect legacy package profiles; admitting schema 2 there requires explicit versioned receipt/profile validation and independent mutation/cancel/restart tests. Directory, build and temporary source deletion must be exercised through final attachment before the automatic CLI path is claimed.

This family remains blocked for public enablement until the original A3 aggregate process/memory and final signed relocated native-app gates have actual evidence. Per-process rlimits and sampled RSS are not aggregate enforcement. The current reservation is host admission accounting, not an OS-enforced whole-tree memory limit.

## Evidence

Source and relocated artifact receipts will be recorded after the complete combined regression and installed bundled-runtime verification. This file records implementation scope, not an A3 completion claim.
