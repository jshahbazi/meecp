# Native installed text-generation acceptance

`scripts/verify_phase3_native_textgen.py` exercises the existing visible model
capture handlers through the frozen native acceptance driver. It selects a GGUF
and its required license, adopts the settled draft in the parent, answers the
generated questions, reviews and runs the managed preparation, and verifies
attachment to that same reviewed revision. Cases cover a new captured draft and
an ordinary folder supplemented by captured model inputs. The folder's source
namespace remains bound independently of the captured collection.

The exact fixture is Qwen2.5 0.5B Instruct Q4_K_M, 491,400,032 bytes, SHA-256
`74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.
The license comes from the selected host's `templates/text-generate-v1` assets.
The harness preserves the original model. It authors no preparation manifest,
descriptor, tool contract, dependency lock or recipe registration.

Each run copies and checks every artifact of a clean built host, copies the real
native executable, uses a temporary home and minimal path, and imports the MCP
SDK and product code from that relocated host. Only its private serve factory
enables execution and admits the exact `text-generate-v1` task reference. The
ordinary CLI retains its production denials. A normal-host native capture run
must settle and adopt its draft while preparation remains unavailable.

Recipe inspection and lifecycle calls use the version declared by the selected
host's family. The native receipt must match that exact enabled recipe and the
bound component references. Preserved earlier recipe versions do not replace
the current default selection.

The installed path uses the CLI's exact attached import and revision for plan,
apply and operation polling. Real SDK clients verify all tool schemas and pinned
fixtures over HTTP and stdio, actual generation token and byte bounds, three
clients sharing one engine, the live four-caller queue, and rejected excess,
input and context requests. An official MCP cancellation notification refers to
the request ID emitted by the SDK. The harness checks resident ownership and its
reservation through inference settlement, then invokes the same engine again.
It does not discover or signal process IDs.

After deleting the selected inputs, the actual host restarts and both transports
invoke again. An isolated memory policy denies a second real model slot while
retaining the first slot's declared 1,024 MiB plus 64 MiB headroom reservation.
Enabled and disabled recipe state survives independent normal-host restarts;
the installed runtime remains callable after recipe disablement. Finally, the
exact installed package is relocated, the complete original captured state is
deleted, and the production text-generation verifier launches an independent
owned engine and adapter. It checks actual tokenizer boundaries and measures
their joined peak memory before verifying unchanged package hashes.

`fixtures_checked` counts the descriptor's declared validation fixtures exactly.
The production probe also requires four concurrent token-boundary generations,
three rejected invalid inputs and one final identity/accounting status call
before returning. Those additional checks do not change the declared fixture
count. The installed native harness records its separate live queue,
cancellation, generation and rejection evidence explicitly.

Failures retain the isolated private artifacts. Unsettled native ownership
also leaves an explicit marker and prevents evidence deletion. Child commands,
SDK requests and independent owners are joined before cleanup. Reports contain
identities, hashes, counts, bounds and fixed diagnostic codes; no prompts,
responses, model bytes, keys, raw command output or exception text are emitted.

## Evidence status

The additive source and focused failure checks are ready. The first native run
against the sealed CLI host from
`2f1a448eaf2d2918380a649cbbc41671a748613f` verified its exact 4,702-file payload
and current recipe version 1.0.4. An actual plan review required 38.185 seconds,
exceeding the native client's original 30-second request deadline. The focused
[deadline checkpoint](NATIVE_PLAN_REVIEW_DEADLINE.md) records that failure and
the verified repair.

The repaired native binary completed capture and reviewed revision 2, then the
build correctly rejected this host with `textgen_private_engine_required`.
Its preparation toolchain lacked all 32 required textgen runtime artifacts.
All owned children joined; the failed run and isolated state are retained.
A successful installed native receipt from a fresh sealed host containing the
reviewed engine closure is required before this checkpoint is described as
verified.

This checkpoint does not establish language quality. Live crash/revision/cancel
races, filesystem and network isolation, a hard aggregate memory limit,
distribution signing and the final Phase 3 adversarial review remain open.
