# Private Python probe confinement

Status: additive source checkpoint, 2026-10-03. Public preparation remains
disabled. This checkpoint advances the common A1/A2 isolation gate; it does not
complete installed Python/ONNX acceptance, A4 confinement, signed distribution
or aggregate resource enforcement.

`ProbeConfinementV1` is a host-created launch plan for the exact published
Python and ONNX private-interpreter launchers. It verifies their complete
launcher bytes, matching locked interpreter/bootstrap artifacts, package
digest, private workspace, native reaper, sandbox binary and current platform
identity. An unknown launcher is rejected. A recipe or descriptor cannot
select a broader policy.

The confined launch invokes the same private interpreter and bootstrap
directly, preserving declared arguments while avoiding the launcher's shell
and directory-discovery subprocesses. Private Python and copied dependencies
remain inside the sealed package. The plan supplies isolated home and temporary
directories within the caller's private probe workspace.

The applied policy permits package/system-runtime reads and private probe-data
writes. It denies external file contents and metadata except the literal
ancestor directories needed to resolve those admitted paths. Package writes,
network connections, fork, spawn and process-group/session changes are denied.
Only the exact private interpreter may execute. The Darwin syscall exclusions
use the installed arm64 SDK ABI and remain bound to the exact current OS,
kernel, sandbox binary and policy digest.

The SDK verifier optionally accepts this typed plan from trusted host code.
It checks the plan before launch and after owned shutdown/package revalidation.
Directory identities prevent an identical-mode replacement from satisfying the
original plan. Their identities are checked again after the final file hash.
The plan checks that its home/temp paths are derived from its workspace and
that the package and workspace are disjoint, including for a reconstructed
typed plan. A supplied process owner must use the plan's actual native
helper; the owner pins that helper through the scheduled native launch task.
Hashing runs in a joined worker; repeated cancellation cannot
return while that worker still reads the package. The common verifier and
resolution implementation identities include the confinement module bytes.

## Evidence and remaining gates

The source regression packet passed 69 checks with five unrelated opt-in
installed-host cases skipped. Actual copied-private-runtime execution through
the native reaper and locked MCP SDK passed a complete schema/fixture probe.
Independent negative operations denied external canary reads/stat, package
writes, TCP, UDP, Unix-socket connections, fork, session/group changes and both
spawn escape attributes. Private home/temp writes succeeded. Unknown launchers,
widened policies, replaced directories and repeated cancellation were also
checked. These cases use synthetic canaries and emit no source/request/output
content to test diagnostics.

An additional 16 probe/verifier checks passed after binding the supplied owner
to the exact reaper. The retained actual mixed-directory Python package from
the [native builder checkpoint](MIXED_PYTHON_BUILD_CHECKPOINT.md) also passed
this policy with one real SDK schema/fixture probe, the original source absent,
exact current ResolvedPython 1.0.1/component/package/descriptor bindings, an
observed peak of 68,468,736 bytes against 128 MiB and zero remaining owned
processes. Its private receipt SHA-256 is
`6d83baff4505f19a3ac9111ab74005f8bff2a3bdfb15d66077b03bdd7ea02b65`.
This source-level policy execution used the unchanged helper from the frozen
mixed host; that earlier host does not contain this new confinement module.

The independent follow-up reproduced three trusted-host authority races:
rebound writable directories, directory replacement during the final hash and
an owner helper change during the joined hash. The corrections reject all
three, plus a helper change at the actual native launch-task barrier with zero
native creations. The final focused source packet passed 68 checks with five
opt-in host cases skipped; the independent ownership packet passed 34 checks
and 16 binding/FD/directory negative controls. Repeated post-settlement
cancellation joined every worker. The corrected real SDK probe passed one tool
and one fixture at 68,485,120 foreground bytes against 128 MiB, denied four
canonical/Data-alias external read/stat controls and left zero owned processes.
Its independent receipt SHA-256 is
`5325161ee717293cd2d81d3ecdc5cc71ac15bd2a5d0979aa5de0ba9a35ff48f3`.
These overlapping packets are separate checks, not additive test totals.

This policy uses the existing deprecated macOS custom Seatbelt mechanism.
Passing exact-build controls does not establish a supported public isolation
API or compatibility with another platform. The
[managed probe integration](MANAGED_PROBE_CONFINEMENT_CHECKPOINT.md) selects
this plan in the common managed stdio verifier. Direct library calls retain
their existing default, and public automatic preparation remains disabled.
Fresh real prepared Python/ONNX packages,
combined installed/native artifacts and supported-policy admission still need
their own acceptance evidence. A4 requires a separate policy for its owned
engine/adapter communication. Memory, CPU, process and private-disk accounting
remain separate resource gates.
