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

The native captured and folder-supplement paths passed on the clean combined
host from `68a7533ce99098a7540bc75bf3f3f01f497c72a2`, using the frozen harness
from `94fe28719b59b857fcf930d266cf92f2051d5936`. The actual run used the exact
1.0.5 family and recipe, whose bundle digest is
`35355ea07d02f5271b19a718e19da8ed286f8cbf9176a1c2400a50bf95f75527`.

The independent preflight verified all 4,782 host files and exact membership,
all 32 textgen runtime files, all 20 pinned engine closure files, the native
binary, the three frozen harness files and the original model. The build receipt
SHA-256 is
`1c1e384ca716303c363aa9d9e8ee6b2d70f29b3e6d30b96f96c4a78bd595542d`.
The actual compiled native binary SHA-256 is
`2136568277298f7642be88f09d3d6d307931266d1095ad12d4c6f0086794d41e`.

| Actual case | Elapsed seconds | Reviewed and attached revision | Preparation peak bytes | Independent relocation peak bytes |
| --- | ---: | ---: | ---: | ---: |
| Captured draft | 206.084 | 2 | 918,978,560 | 920,109,056 |
| Folder supplement | 228.420 | 3 | 925,007,872 | 918,011,904 |

Each case completed five preparation commands, acquired 30 wheels and checked
one declared fixture. Both installed transports checked the complete tool
contracts and fixture three times across the initial run and two normal-host
restarts. Six actual bounded generations per case each returned eight output
tokens. The live three-client check used one shared engine, two model residents
and three host owners including the gateway. Both cases admitted four queued
requests, rejected four excess requests and rejected all three invalid
input/output/context requests.

Actual SDK cancellation returned the required MCP error. The harness observed
the unchanged owned instances and reservation while the underlying inference
settled: 167 live ownership samples for capture and 169 for supplement. Both
cases then generated again through the same engine. Enabled and disabled recipe
state survived separate normal-host restarts, and both installed transports
remained callable after disablement. The second slot was denied with
`resource_blocked` under the isolated 1,088 MiB budget; the first slot retained
its exact 1,024 MiB declaration plus 64 MiB headroom reservation.

Both cases deleted their selected inputs before restart. The supplemented
folder kept its original source namespace and identity through attachment.
After all host processes joined, the harness removed the complete captured
state and probed an independently relocated installed package with a new owned
engine and adapter. All captured ownership counters were zero. All owned
children joined and the isolated workspace was removed.

The relocated host's full membership and hashes were checked before product
imports and again after both cases. An independent postflight checked every
original host file and exact membership, and confirmed the original native
binary, model and frozen harness remained unchanged.

The bounded mode-0600 receipts are retained in the native acceptance worktree's
ignored `dist` directory:

| Receipt | SHA-256 |
| --- | --- |
| `native-textgen-preflight-68a7533.json` | `790a032dc7d36a33c9453c1b2a1af306ca3ec215345225500fd1e414d76c7ae2` |
| `native-textgen-68a7533.json` | `6e3ac2c00c1bd1101381212d0d7952cdda6dc35a02eadd1f5686dde5e5e67878` |
| `native-textgen-postflight-68a7533.json` | `043cd7532b32463bf45a51cc6f6efffe2409fd711a7f8af748016d346ecf88f6` |

The 17 focused harness checks passed before the actual run. Public execution
remained disabled: normal native capture adopted its settled draft, then
stopped at preparation review without creating a preparation or resolution job.

## Preserved failure evidence

The first native run against the sealed CLI host from
`2f1a448eaf2d2918380a649cbbc41671a748613f` verified its exact 4,702-file payload
and current recipe version 1.0.4. An actual plan review required 38.185 seconds,
exceeding the native client's original 30-second request deadline. The focused
[deadline checkpoint](NATIVE_PLAN_REVIEW_DEADLINE.md) records that failure and
the verified repair.

The repaired native binary completed capture and reviewed revision 2, then the
build correctly rejected this host with `textgen_private_engine_required`.
Its preparation toolchain lacked all 32 required textgen runtime artifacts.
All owned children joined; the failed run and isolated state are retained.

The later complete host from
`d968b84aa702c554aa396aed1349304dbe1bab85` reached actual native attachment,
installation, generation and queue checks, then failed with
`textgen_cancel_sdk_response_mismatch`. The legacy stateless HTTP service
returned successful generation after an actual SDK cancellation notification.
The focused stateful-session repair preceded the successful 1.0.5 run. The
failed native receipt and private state remain retained; its receipt
`native-installed-textgen-d968b84-reviewed-native-r3.json` has SHA-256
`d9fdc94850405b108b323701fb944e0dce7b1d03885a9538b034d5d8defcddc7`.

This checkpoint does not establish language quality. Live crash/revision/cancel
races, filesystem and network isolation, a hard aggregate memory limit,
distribution signing and the final Phase 3 adversarial review remain open.
The receipt deliberately keeps `a4_scope_complete` false.
