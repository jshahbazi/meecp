# Installed native Python onboarding acceptance

`scripts/verify_phase3_native_onboarding.py` exercises the compiled AppKit
preparation view against a relocated packaged host. It uses fresh temporary
state, a private home, minimal `PATH`, empty attempt caches and the host's
bundled MCP SDK. The ordinary production CLI is launched separately and must
reject resolution planning and preparation start. Only the harness's explicit
trusted development `serve` factories enable execution in its isolated child.

The three cases supply ordinary source, a license and, for the MCP application,
normal `pyproject.toml` app metadata declaring `worker:main`. The callable has no
project metadata. None supplies a preparation descriptor, dependency lock or
separate tool-specification file. The MCP server constructs two tool declarations
dynamically during actual discovery.

| Case | Native preparation input | Installed checks |
| --- | --- | --- |
| Ordinary callable | Inferred `double(n)`; synthetic answers in the generated form | Exact full tool declaration, structured result and schema conformity through HTTP and stdio, followed by source deletion and host restart |
| Dynamic MCP | Declared foreground `worker:main`; bounded synthetic answers for both discovered tools | Exact descriptions, input/output schemas and annotations through both SDK transports, calls to both tools, source deletion and restart invocation |
| Independent recipe | Outside-host CLI `recipes scaffold --component resolved-python-v1`, `check`, `add`, `enable`; exact native recipe selection and generated recipe questions | Actual preparation, attachment, install and both SDK transports; enabled state survives restart; disabled state survives a second restart; installed runtime remains callable after disabling the recipe |

Every case validates the native receipt against actual settled host jobs and the
attached import. Source identity remains unchanged across the native parent's
metadata revision adoption, and attachment preserves that reviewed revision.
The sealed schema-2 receipt must identify derived
inputs, retain zero original dependency locks and match the reviewed revision.
Installation follows `install plan --import ID --revision N`, `install apply`
and polling of the real installation operation. Each synthetic intake explicitly
selects installation start and host startup policy; preparation and install
planning must preserve both choices before restart invocation is attempted.

The harness retains each `Popen` child until it is reaped. Shutdown joins its
owned children and checks empty installed ownership ledgers, a read-only count
of the separate preparation ownership ledger and preparation and resolution
reservation directories. Cleanup handles only the isolated state's
known socket names. It never signals a discovered PID.
If shutdown cannot prove settlement, it retains the isolated state and records
its directory for reconciliation instead of deleting ownership evidence.

## Run

Build the native executable from the same reviewed source checkpoint and build
the CLI host with its complete preparation toolchain. Supply the resulting paths
and a new report path:

```sh
swift build --product McpmeMenuBar
uv run --locked python scripts/verify_phase3_native_onboarding.py \
  --host /absolute/path/to/built-host \
  --native /absolute/path/to/.build/debug/McpmeMenuBar \
  --output /absolute/path/to/new-report.json
```

The focused harness failure checks are:

```sh
uv run --locked python -m pytest -q --tb=no tests/test_native_onboarding_acceptance_harness.py
```

They verify that a timed-out owned child is reaped and that an unsettled installed
or preparation ledger retains the exact isolated evidence after workspace exit.

The packaged resolver requires verified TLS trust for the reviewed public PyPI
origins. The harness does not inherit an ambient certificate selection, package
cache, proxy configuration or global interpreter. A trust failure is a failed
acceptance run and receives a fixed diagnostic.

## Evidence and limits

The [2026-10-03 passing receipt](evidence/native-installed-onboarding-67d4975.json)
records the final installed run. All 1,605 packaged host artifacts matched their
recorded hashes before execution. The immutable pair was:

| Identity | SHA-256 or Git commit |
| --- | --- |
| Host source commit; clean build | `67d4975230ef9e055d1bdc910958d878b7110e25` |
| Host `build.json` | `ee658719ccb62d364b49e8b8b0953c69531726703162ae5a2dcf0e30c25b9642` |
| Compiled native executable | `abde3a076a4b25426d09aa7edb2a86cd154ba5bf5628851b6988f143011b4f10` |
| Native `main.swift` source bytes | `7a32cd96d9b6b305908fe46500c14c4257a982c358071773f978997263397a55` |
| Passing receipt bytes | `85f9390b55101df9f5e2ebb13a6776f0ffc5929fdb1524b41b5b645dc710698e` |

The native source uses the `e717eaee41d32c511fe267e3cceee0b2461b607a`
checkpoint with this branch's acceptance-command revision, attachment-identity
and receipt-bound checks. Normal native UI behavior is unchanged. Preparation
and management UI contracts passed; Swift tests passed with 18 tests and three
opt-in installer lifecycle skips. All three focused harness ownership checks
passed. The full Python suite for this native checkpoint passed with 1,974 tests
and 12 skips. A separate invalid-native run failed with `native_receipt_missing`,
joined every owned child and preserved production CLI denials.

| Case | Tools / fixtures checked | HTTP / stdio calls | Elapsed seconds | Resolution process peak bytes | Independent probe peak bytes |
| --- | --- | --- | --- | --- | --- |
| Ordinary callable | 1 / 1 | 2 / 2 | 40.677 | 56,115,200 | 68,517,888 |
| Dynamic MCP | 2 / 2 | 4 / 4 | 46.264 | 87,375,872 | 69,353,472 |
| Independent recipe | 1 / 1 | 3 / 3 | 49.062 | 56,492,032 | 68,927,488 |

Each case kept its original source identity, adopted metadata revision 2 and
attached the independently verified package to that same revision. Each
preparation acquired 29 wheels totaling 8,396,577 bytes and completed five
private commands. The dynamic MCP resolution separately acquired the same
closure, completed seven commands and discovered two tools. Callable resolution
completed one command without acquiring wheels. Original source locks were
absent in every case. The original source and outside-host recipe bundle were
deleted before successful restart invocation.

The independent recipe digest was
`016ac1773988f135ab6a4cb5fd263bfbbf64f78074d5fbce0ca4d9edb3726484`.
Its exact enabled state and epoch survived the first restart, its disabled state
and epoch survived a second restart, and its installed runtime remained callable
through both SDK transports after disabling the recipe. All owned children
joined; installed and preparation ownership ledgers and reservation directories
were empty on shutdown. Ordinary production CLI execution remained disabled.

The mode-0600 report contains artifact and binary hashes, full tool-contract
hashes, native receipt hashes, exact source/review/attachment revisions, recipe
and resolution identities, measured stage counters, fixture counts, elapsed
time and foreground memory peaks. Declared app and resolution budgets are
reported separately. The peaks are the maximum measured resolution foreground
process and the independent probe process. Aggregate live memory remains
unverified.
It contains no source text, synthetic answer bodies,
credentials, raw command output or exception dumps. Failure diagnostics contain
only a bounded stage, case, error code and exception type.

Passing this harness establishes installed native Python and existing-MCP
functionality for these synthetic inputs and this immutable host/native pair.
The full A1 and A0R gates additionally require their crash, revision and
cancellation races and recipe lifecycle/admission failure cases. Probe
filesystem/network isolation and hard aggregate live-memory enforcement need
independent adversarial evidence. ONNX, CLI, generic LLM, integrated-agent and
broader-runtime gates retain their own acceptance requirements. Public
automatic execution remains disabled until the release gates are satisfied.
