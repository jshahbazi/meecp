# Deterministic CLI ownership checkpoint

Status: development support-matrix spike, 2026-10-03. The original [A3 CLI acceptance gate](AUTOMATIC_PREPARATION.md#acceptance-and-release-gates) remains open. Automatic CLI preparation is unregistered and unavailable.

The implemented `macos-no-fork-v1` case owns one foreground command on macOS arm64/macOS 26. The dedicated [native launcher](../scripts/mcpme-cli-launch.c) installs a deny-default kernel sandbox and replaces itself with the exact private executable. The existing native reaper retains that child's identity until `wait4` completes. Source code cannot fork, spawn another interpreter, open a network connection or write package files. It can read the package and the reviewed system startup paths. Filesystem metadata queries remain permitted.

This establishes a bounded foreground support case. It does not establish descendant-capable CLI support. The separate real [escape fixture](../tests/fixtures/cli/escape.c) proves that an unconfined child can create a new session, remain alive after its leader's native completion receipt and then exit through a held private pipe. The fixture requires no signaling of a discovered or adopted PID. A completed receipt from the existing process-group reaper proves held-leader settlement. It cannot prove arbitrary descendant settlement.

## Implemented contract

[CliJsonSpec](../backend/mcpme/preparation_cli.py) defines one JSON object input and output, a fixed private Python stdlib script, an argv array with explicit bounded string-field substitutions, JSON stdin byte bounds, stdout byte bounds, a finite deadline and sanitized nonzero-exit mappings. NUL arguments, undeclared fields, unknown versions, unknown topology, shell fields, unbounded argv substitutions, duplicate output keys, nonfinite output and schema mismatches fail. Stderr is discarded. Input/output content is transient and does not enter the journal.

[CliExecutor](../backend/mcpme/cli_execution.py) serializes each complete invocation, journals an opaque instance/nonce before launch and supplies the native reaper with a pre-opened completion capability. Cancellation and timeout settle the held command before releasing its reservation. Restart reads exact private receipts and never discovers, adopts, signals or replays a prior process. Missing, corrupted or lost-owner receipts retain their declared memory reservation and block new execution. Replacing a terminal receipt with a pending frame reinstates its original reservation. Per-state history is bounded to 256 invocations.

The journal stores contract identity, reservation, outcome and bounded measurements. It stores no arguments, stdin, stdout, keys, exception text or PIDs. It uses mode-0700 directories, mode-0600 files, a single-owner lock and atomic/fsynced journal writes. The trusted adapter requires an explicitly supplied external owner-only `MCPME_CLI_STATE_DIRECTORY`; no package or source value selects an existing process.

The source validator, directory inspector and [form provider](../backend/mcpme/preparation_cli_forms.py) run no source. An ordinary directory containing `cli.py` or `__main__.py` receives the default `mcpme.cli-json` candidate and generated questions for tools, bounded schemas/argv, source license and synthetic expected behavior. Answers generate an inline component descriptor. The user authors no package manifest, dependency lock or runtime tree. An optional `mcpme.cli.json` can supply the same command metadata. Both paths retain the original source digest.

The [package generator](../templates/cli-json-v1/prepare.py) reuses the private CPython 3.12.13 and locked MCP 1.30.0 closure, copies the reviewed helpers and adapter, seals every artifact and preserves schema-1 source identity. It runs no selected source while building. This is a development/external builder; installed-worker dispatch and helper/toolchain admission have not been registered.

## Verification

Run the focused tests with:

```text
uv run --locked python -m pytest -q tests/test_preparation_cli.py tests/test_preparation_processes.py tests/test_mcpme_ownership_receipts.py tests/test_preparation_forms.py tests/test_preparation_components.py tests/test_mcpme_templates.py
```

The CLI tests use a real copied private interpreter and separately compiled C executable, fork, descendant-request and escape fixtures. They exercise literal argv, stdin/output limits, schemas, exit mapping, deadline, repeated cancellation, held-command SIGKILL, owner SIGKILL, wrapper SIGKILL, restart, tampered receipts, source identity and kernel effect denials. Static generator tests explicitly use a nonexecuted runtime fixture and make no execution claim.

Run the actual package check with:

```text
uv run --locked python scripts/verify_phase3_cli.py
```

The harness answers generated directory questions, builds a real private runtime package, checks full tool definitions through the real MCP SDK, makes two functional calls across adapter restart and validates native receipts. It relocates the package, removes original source and build/helpers, excludes global development tools from PATH, runs from an unrelated directory and verifies the sealed tree remains unchanged. It creates no installed slot and does not exercise the native setup window. Its synthetic recipe reference tests normalization and binding; it does not prove trusted catalog registration or recipe lifecycle acceptance.

The run reports the foreground command's measured `ru_maxrss` separately from the 128 MiB declaration. Over-budget results are rejected after exit. There is no live physical-memory enforcement or aggregate adapter/reaper/command measurement in this spike, so memory acceptance remains open. This distinction appears in the machine-readable harness result.

## Recorded clean checkpoint

Implementation commit `fbe87c589bf9ee066f4de231dbdbbc9b2e6e4385` passed 112 focused tests in 18.10 seconds. Its unchanged clean checkout then ran the actual package harness in 6.418 seconds. The sealed package contained 2,949 artifacts and had digest `8f4921e7ee64b9cb407f3c6966d83f046b60f52fcd0d0f023c6cc2b7ab0d2072`.

The harness passed generated directory questions with no source manifest, two real SDK calls across adapter restart, exact full tool definitions, relocated private-runtime execution after deleting original source/build/helpers and package mutation verification. Native receipt reconciliation ended with zero pending receipts and zero reserved memory. The command's measured foreground peak was 24,543,232 bytes against a 128 MiB declaration. The result reports `memory_enforcement: post-exit-foreground-measurement-only` and `A3: open`. These measurements describe the synthetic no-fork fixture on this macOS arm64 host.

## Integration and remaining gates

Common owners can add the static component catalog/reference, register the directory inspector and generated form provider, and supply a reviewed private native helper/toolchain plus job-bound state directory. Public execution must remain disabled while the original A3 gates are open. Component identity covers the source validator, executor, form provider, native launcher, adapter and generator bytes.

Broader descendant support needs ownership enforcement before admission. One implementable bounded route is a host-owned invocation broker: direct command fork/spawn remains kernel-denied, and each declared child is launched through its own native ownership capability and completion receipt. Command, broker and descendants retain reservations until every held child settles. This requires an explicit broker protocol, runtime closure and real child/grandchild/escape/crash/restart fixtures; it is not implemented by this checkpoint. Arbitrary daemonizing commands still require a separately verified kernel ownership/containment facility or rejection. PID discovery and signaling cannot substitute for held ownership.

Remaining A3 release evidence includes the original descendant/process-group acceptance, actual installed-host prepare/attach/install/invoke/restart path, admitted helper identities, aggregate live resource enforcement, bounded queue/cancellation races, installed cleanup and independent adversarial review of an immutable integrated artifact. The no-fork support case does not redefine A3.
