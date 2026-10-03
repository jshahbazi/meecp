# Installed CLI integration contract

Status: proposed implementation contract, 2026-10-03. **A3 remains open.** This report changes no runtime or advertised capability. It builds on reviewed command ownership commits `755615b` + `babd029`, including publication-format-3 receipts, and the original CLI spike `fbe87c5` / `05b8f1e`. The original gate requires installed command execution, bounded argv/input/output, exit mapping, cancellation/deadlines, descendant cleanup, crash/restart, private closure and declared effects. The no-fork spike and this proposal do not replace that gate.

## Decision and topology

Own `CommandExecutorV2` in the backend `Supervisor`. Do not instantiate it inside the CLI MCP adapter, gateway worker, recipe worker or another process whose outer owner may kill its group. Prefer a host-owned MCP protocol session connected to the existing gateway through private pipes. Selected command code runs only behind the native command helper.

```text
Native app
  Backend / Supervisor
    Existing owner -> gateway worker       [protocol and client transport]
    Host MCP session task                   [bounded pipes to gateway]
    Command native owner H                  [direct child of backend]
      Confined command leader T             [separate session/group]
        Confined descendants                [remain in T's group]
```

The unsafe nested layout is `outer reaper -> wrapper W -> command owner H -> target T`. If W is a group leader, H inherits W's group while T creates its own session. An outer `kill(-W, SIGKILL)` kills W and H together; it does not reach T. H then cannot settle T or publish completion. A longer outer grace interval cannot prove safety because H can be stopped or delayed. This is a structural counterexample, not a new claim of an installed failure: `ProcessOwner.launch()` currently defaults to `owned_group=False`, and ordinary gateway/package launches use that default. The original spike's adapter nevertheless owns its executor, so copying that placement into a group-supervised wrapper would introduce this failure.

The proposed topology removes that dependency: implement wrapper-pipe closure as revocation of its host lease; H is a sibling owned directly by the backend. Native app `stopHost()` currently signals only its held backend `Process` (`Sources/McpmeMenuBar/main.swift`). Its forced SIGKILL bypasses `Supervisor.close()`, so installed acceptance must prove H independently settles through its parent-relationship/control-pipe checks. Source cannot move into H's group because the inherited policy denies `setpgid`, `setsid` and `posix_spawn` before source exec.

Before admission, additionally isolate **H itself** from ancestor group shutdown, using a reviewed pre-exec session boundary such as `Popen(start_new_session=True)` with real launch/cancel tests. The present helper launcher does not set this option. Creating only T's session does not protect H. Verify H's exclusion from every group that installed shutdown actually signals, using live owned fixture relationships; never reconstruct ownership from recorded PIDs. Session isolation does not establish immunity from unrelated system-wide/coalition termination. Native-owner loss still leaves ownership and reservations unresolved. These installed-topology checks are required evidence, not properties established by this report.

## Closed host boundary

The preferred integration preserves public MCP HTTP/stdio messages. `Gateway.new_session()` already requests `launch_session` and receives two descriptors; `Supervisor.serve_channel()` already binds that request to an active worker, slot generation and session limit. Extend that host seam with a distinct **protocol-session handle**, backed by bounded host tasks/pipes rather than an `OwnedProcess`. Do not fabricate a process, PID or version-1 ownership receipt for that handle. Gateway shutdown closes the handle and cancels its invocation leases; only the command executor owns native cleanup.

This requires an explicit versioned package/runtime binding. Current `PackageManifest` schema 1 accepts only `mcp-stdio`, `mcp-http` and `model-service`; silently skipping a schema-1 entrypoint would change its contract. The common-contract owner should add a closed `host-command` profile in a new schema revision, with command-contract path/digest, exact runtime/executable binding, effects, limits and required host component identity. Legacy schema-1 bytes and interpretation remain unchanged. A file named `cli.json`, a recipe label or an executable filename conveys no authority.

A compatibility alternative is a reviewed thin MCP wrapper that sends arguments over a per-session private invocation channel to the **host-owned** executor. It must hold no executor/native-owner object, accept no caller-selected paths/environment/helpers, and receive no management or receipt capability. This retains a real `mcp-stdio` entrypoint but adds another protocol, lifecycle and closure proof. It is not the preferred first implementation and has no acceptance evidence yet.

| Binding | Required host-owned value |
| --- | --- |
| Installation | Slot ID, generation, immutable installed package digest, active installation/upgrade lease |
| Tool | Exact tool name and full input/output MCP schema digest; closed CLI contract and component version/digest |
| Executable | Verified private runtime, script, working directory, permitted effects, artifact hashes and complete notices/closure |
| Ownership | Exact installed helper hash, admitted OS/policy identity, publication format 3, private invocation-state root |
| Invocation | Opaque invocation ID, session/request identity, authority revision, deadline and budget reservation identity |

The host resolves these values from its validated package and current slot state. Public requests supply only the advertised tool arguments. The synchronous executor `admit(spec)` callback checks a host-created immutable admission ticket after queueing and before result publication; it cannot acquire the supervisor's asynchronous resource lock. Create and persist that ticket under the host admission/resource lock first, then recheck its cancellation/generation/expiry state synchronously. A source/package value must never create or extend the ticket.

Two small executor-contract additions are needed before integration: bind an admission-ticket digest into the command-spec digest, and expose a prelaunch invocation identity/receipt reference for the host's durable reservation map. The present spec binds package/command data, and its journal allocates its own identity; these are not yet an installed slot/generation authority ledger. Register the host reservation and exact receipt reference before native launch. Preserve the existing nonce/helper/policy/spec binding and format-3 publication barrier.

## Request, result and lifecycle rules

Use private pipes or socketpairs transferred by descriptor, with no TCP listener, filesystem rendezvous token or management bearer token. An inherited descriptor is scoped to the exact worker/session generation. The command receives only stdin/stdout/stderr; host-control, receipt, state-directory and management capabilities never reach source. The host protocol implementation loads only trusted code and statically parsed contracts, never imports a selected package module.

Keep the existing tools-only initialization, full `tools/list` definitions, `tools/call` input schema, `CallToolResult`, structured output and error mapping. Resolve argv from the fixed template and declared bounded string substitutions; reject NULs and byte overflow. Encode one bounded JSON stdin object, discard stderr, parse one bounded closed output object, and apply the declared nonzero-exit mapping. Check the **serialized MCP envelope** limit after adding both text and structured output; a bounded stdout object can grow when escaped/duplicated in the response. Reject duplicate keys, nonfinite values, unexpected fields and extra output. Do not log requests, arguments, outputs or raw errors.

One host invocation lane per admitted slot/package runs one command at a time across all sessions. Add a finite queue and global host admission before submission; the current executor's `asyncio.Lock` does not itself bound queued callers. Queue waits consume the original request deadline. Charge transient request/result buffers separately and retain reservation state through native settlement. `reservation_units` is accounting, not an aggregate memory guarantee.

Cancellation, MCP session deletion, worker/channel loss, slot stop, upgrade, uninstall and backend shutdown first close admission and revoke affected tickets. Then cancel and join command tasks, reconcile exact format-3 final receipts, and release only proved reservations. A result belongs to its original session/request/generation and cannot cross an upgrade or cancellation winner. A pending receipt blocks replacement execution, package deletion and cleanup of the old private state. A native-owner failure is not a successful cancellation.

On restart, load invocation/reservation mappings before opening gateways or admitting a replacement package. Read only exact published `.command-v3.complete` receipts; staged C/N content, old formats and missing final names remain pending. Never adopt, signal or replay a stored PID/invocation. Successful prepublication payload sync followed by final publication can settle after host death; failed sync cannot. Losing the final directory entry leaves the reservation pending. See [the reviewed receipt contract](COMMAND_EXECUTION_V2.md) for payload durability versus directory-entry persistence.

## Implementation dependencies

1. **Common contracts/catalog:** version the host-command profile and CLI component; the old `CliJsonSpec.topology` accepts only `macos-no-fork-v1`. Define generation-bound admission tickets and receipt/reservation mapping. Public enablement stays off.
2. **Host packaging:** build/sign/hash the final native helper in the bundled host; bind its post-packaging identity and admitted OS policy. Reuse the pinned private Python/source/notice closure where valid. No runtime clang, Homebrew, package-manager downloads or discovery from PATH.
3. **Supervisor:** own executors and bounded lanes; extend resource/recovery/upgrade/uninstall shutdown ordering. Existing seams are `owner`/`launch`/`serve_channel`, preparation reservation methods, `reconcile_owned_receipts`, `close_lost_slot` and `close`. Reconcile v3 invocation state before gateways reopen; finish executor shutdown/reconciliation before removing slot/package state. Keep v1 process ownership and v3 command receipts distinct.
4. **Protocol handoff:** implement host MCP session handles behind `Gateway.new_session`, retaining its private descriptor transport and existing HTTP/stdio authentication, Host/Origin and message bounds. Add the admission-ticket/spec bridge and atomic prelaunch reservation registration.
5. **Recipe and verification:** update static forms/validator/generator and installed dispatch. Run real bundled SDK calls through both client transports after prepare, attach and authorized install, then independently review the immutable installed artifact.

## Resource boundary remains unresolved

| Primitive | What the evidence supports | What it does not prove |
| --- | --- | --- |
| `setrlimit` | CPU/data/descriptor/address-space limits apply at process scope. Published XNU routes `RLIMIT_AS` to the current VM map. | A combined physical-memory ceiling for a freely forking command tree. Per-process ceilings can multiply with children. |
| `RLIMIT_NPROC` | Apple's documented limit counts processes for a user ID. | An isolated per-invocation process allowance; unrelated same-user activity affects that count. |
| RSS/footprint sampling, `wait4` peaks, pressure notifications | Measurements and reactive policy inputs. | A hard bound before allocation/fork, or the simultaneous tree peak from a sum of separate peaks. |
| Memorystatus / coalitions | XNU documents per-process memory limits and privileged coalition management. | A demonstrated supported unprivileged API that creates this invocation's enforceable memory/process domain. No such proof was obtained here. |

Sources: Apple's [setrlimit manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/setrlimit.2.html), pinned [resource-limit implementation](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/kern_resource.c), [coalition privilege model](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/coalition.c), and Apple's [memorystatus overview](https://github.com/apple-oss-distributions/xnu/blob/main/doc/vm/memorystatus.md). Published source is not a claim that the installed kernel exactly matches it.

No sampled-RSS watchdog or inherited per-process limit closes the aggregate gate. A cooperative child-launch broker with direct fork denied is a different compatibility contract requiring its own implementation/proof; it cannot relabel this fork-capable A3 result. Privileged services, dedicated user identities or virtualization would change deployment/authority scope and are not proposed implementation dependencies here.

## Required installed evidence

The next acceptance packet must show actual host-owned topology, descendant fork/exec/escape attempts, wrapper/group death with H surviving to settlement, backend and app SIGKILL, helper death retaining reservations, slow/failed prepublication sync across restart, queue/deadline cancellation, cross-session fairness/bounds, generation revocation during publication, uninstall/upgrade with pending ownership, tampered helper/package/receipt rejection, full schemas and functional calls on both MCP transports, relocated sealed closure with source/build/network unavailable, and no package mutation or content leakage. Aggregate tree resource enforcement needs separate actual evidence before A3 release. The next useful implementation is the host authority/reservation/session seam; none of these installed guarantees is supplied by this report alone.
