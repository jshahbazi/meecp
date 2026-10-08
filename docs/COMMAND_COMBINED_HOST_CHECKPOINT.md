# Combined CLI host checkpoint

This development integration combines the reviewed host-command implementation with the common preparation host at `6a26f7b709fa480067c6b9e8b81330c8c5ce3d9d`. A3 remains open. Production command admission remains disabled.

## Distribution contract

The CLI builder compiles, ad-hoc signs and hashes `bin/meecp-command-reap`. Its `command-execution.json` inventory binds helper/source identities, macOS 26 arm64, explicit package schema 2, component `macos26-arm64-command-v3`, and publication receipt format 3. The inventory declares disabled production admission and the remaining aggregate-resource and signed-native-app gates. No runtime code interprets this inventory as permission. The existing trusted Supervisor constructor remains the sole development admission seam. The app builder refreshes the inventory's helper hash after signing before it refreshes the enclosing host receipt.

The host receipt hashes every payload file, including private Python, installed dependencies and launchers. The runtime copier rejects external or directory symlinks, bounds traversal and bytes before copying, emits regular files, and excludes bytecode. The harness requires bytecode-disabled execution and an exact complete file set.

Textgen component and recipe 1.0.4 bind the combined backend implementation. Previous immutable bundles remain byte-identical. The scanner adds only the new exact provenance-file path to its existing exact public-tokenizer-digest exception; rules and value matching remain unchanged.

`scripts/verify_phase3_command_host.py` must run with the selected relocated host's Python and requires a clean source receipt. It checks every receipt artifact hash before parsing the command inventory. It imports the implementation and MCP SDK exclusively from that host, constructs development admission, and installs sealed synthetic command packages. It checks both MCP transports/full schemas, fork/exec settlement, forbidden group escape, descriptor closure, cancellation, backend SIGKILL and held ancestor-group SIGKILL followed by durable command reconciliation. Ancestor-group death can leave the legacy gateway receipt unresolved; the harness does not infer or fabricate gateway completion.

## Automatic preparation still missing

The original CLI requirement in `AUTOMATIC_PREPARATION.md` includes the supported source-to-recipe/build/probe/attachment route. Installing an already constructed schema-2 fixture does not prove that route. The earlier no-fork CLI component exists only in its separate topology-spike branch; this combined tree registers no CLI preparation family. This patch does not register the fork-capable profile as a preparation family or enable CLI discovery.

The next minimal family should introduce a separately versioned `host-command-json-v1` component and descriptor that binds one selected executable closure, fixed argv entries, named bounded variable positions, full structural schemas, exact effects and the approved helper/policy/receipt identity. It should use existing revision-bound source selection, immutable recipe registry, controlled builder and durable job receipt machinery. The builder must produce explicit schema-2 host-command packages, with the package/closure/tool/generation authority checked again at the Supervisor seam. Its probe must use the same host-owned invocation facility rather than spawn a nested helper under the existing probe owner group. The common verifier and attachment gate currently expect legacy package profiles; admitting schema 2 there requires explicit versioned receipt/profile validation and independent mutation/cancel/restart tests. Directory, build and temporary source deletion must be exercised through final attachment before the automatic CLI path is claimed.

This family remains blocked for public enablement until the original A3 aggregate process/memory and final signed relocated native-app gates have actual evidence. Per-process rlimits and sampled RSS are not aggregate enforcement. The current reservation is host admission accounting, not an OS-enforced whole-tree memory limit.

## Evidence

The clean full-closure build is source commit `2f1a448eaf2d2918380a649cbbc41671a748613f`. It was built to a new path, then moved to `/private/tmp/mcpme-combined-cli-2f1a448-relocated` before execution. Its receipt binds 4,702 files; the complete file set and every hash still matched after verification. These are development CLI receipts, not signed native-app acceptance.

| Identity | SHA-256 |
| --- | --- |
| Host `build.json` | `547a776ec167080057261889cf5b2f3cc1a7b64fd0a0da50b3643eecb0095984` |
| Ad-hoc signed command helper | `d5fca15cf1469965a8411229add585477aaf70289a7f669f0fc9a3f725974496` |
| Reviewed helper source | `b99f87541598427df51a39486d384a5855813ea3decac55a1d091cafb5e17933` |
| Private Python executable | `36984af9f922aec72ba5b03e811ee92512268785007192e20f826cbd07cf8d04` |
| Private MCP SDK entry module | `f66ae17959a1420edb67624dcd79da20860543e1d513b7d5c86ec0250cf8af72` |
| Textgen 1.0.4 recipe bundle | `cff04c91645f213b65ae6071c44dca2005bf546e8b2b10e6e4d466aeb34bc257` |

The frozen installed harness ran with the relocated `python/bin/python3.12 -B -I`, explicit synthetic fixture and new private workspace. HTTP and stdio preserved complete schemas and functional output; fork/exec descendants settled; `setsid`, `setpgid` and spawning a new group were denied; descriptor closure and cancellation joined; backend SIGKILL and held ancestor-group SIGKILL recovered through durable command receipts. The normal constructor rejected the same package before installation. No development environment, source package or external Python was used for target execution or SDK imports.

Before that execution, a separate copied payload was verified with the trusted source-side inventory checker. Appending synthetic bytes to `python/lib/python3.12/json/decoder.py` and, separately, `python/site-packages/httpx/__init__.py` caused rejection before importing or executing any target implementation. Restoring those bytes restored complete verification. This negative control did not execute the modified runtime or dependency.

The complete combined regression passed **2,239 tests with 69 skips** in 285.98 seconds. The subsequent build-only closure change passed **242 distribution/preparation tests with two skips**, including bounded copying and complete receipt membership. The pinned scanner integration passed **71 tests**; the full tracked-source and fetched-history credential scan passed after each packaging freeze. The 116-case initial native/installed/build packet passed, including existing failure/receipt races. No old textgen component-identity failure remains in this combined full-suite result. `git range-diff` confirms the four imported commits are patch-identical to `66921d1`, `0e3fae2`, `5f46784` and `7c85ef0`.

Local sanitized evidence files are `/tmp/mcpme-combined-full-tests.txt`, `/tmp/mcpme-combined-cycle-tests.txt`, `/tmp/mcpme-combined-scanner-tests.txt`, `/tmp/mcpme-combined-cycle-scan.txt`, `/tmp/mcpme-combined-cli-2f1a448-negative.json`, and `/tmp/mcpme-combined-cli-2f1a448-proof.json`. They contain test summaries or bounded outcome/identity metadata. The harness returns `production_admission: disabled`, `aggregate_os_limits_proven: false`, and `signed_native_app_proven: false`.

Independent review found that concurrent substitution of a validated managed-runtime source file with a FIFO could block the builder. The repair uses nonblocking, no-follow open and requires the held descriptor to remain the same regular device/inode/size/mtime/ctime before and after copying. Owned-subprocess regressions cover FIFO substitution, replacement with another regular inode and a write to the same inode, all under a finite test deadline. This build-only correction does not change command target authority, helper bytes or textgen component identity.

The review also confirmed that Python 3.12 raises RuntimeError for cyclic runtime paths. The final producer normalizes only the path-resolution seam to a fixed ValueError, including the initially selected Python path. Actual root/entry cycles and the build CLI diagnostic are covered. The final relocated runtime independently recomputed the textgen 1.0.4 component references and matched the immutable recipe exactly.

Independent review reran the 81 original native/installed cases (all passed), then separately repeated the complete private-runtime protocol/lifecycle proof and the runtime/dependency/unlisted-file negative controls on the 4,702-file artifact before the final diagnostic-only correction. It also reproduced and verified the FIFO/regular-inode/same-inode-write repairs in owned, bounded subprocesses. No model evaluation or native UI run was performed for this A3 packet.

Final independent review cleared producer `2f1a448`: all 4,702 files and negative controls passed, both cycle cases returned fixed diagnostics before copying, and all 47 builder tests passed. Runtime bytes matched the independently executed predecessor; only documentation and the requirements export comment header differed, with identical dependency semantics. The final independent static receipt SHA-256 is `a8fee80207b58d63b0534455eeeaea326ddba8a543b568324553ad7ce669f8ff`; the cycle/byte-identity receipt is `2dd95813d69dd06f642bb3e93b1d2c275d9e9bbbcec06779d0695be7117bf2c7`. No remaining finding was reported within that focused review scope. The complete automatic-family implementation proposal is [Automatic CLI preparation gap](COMMAND_AUTOMATIC_PREPARATION_GAP.md).
