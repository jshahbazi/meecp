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
original plan. Hashing runs in a joined worker; repeated cancellation cannot
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

This policy uses the existing deprecated macOS custom Seatbelt mechanism.
Passing exact-build controls does not establish a supported public isolation
API or compatibility with another platform. The current ordinary verifier
callers retain their existing behavior; default automatic preparation is not
enabled by this optional host seam. Fresh real prepared Python/ONNX packages,
combined installed/native artifacts and supported-policy admission still need
their own acceptance evidence. A4 requires a separate policy for its owned
engine/adapter communication. Memory, CPU, process and private-disk accounting
remain separate resource gates.
