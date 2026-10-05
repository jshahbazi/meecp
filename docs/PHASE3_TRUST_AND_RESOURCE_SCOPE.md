# Phase 3 trust and resource acceptance

Phase 3 prepares owner-selected trusted applications under the existing macOS
same-user boundary. The host reviews exact inputs and operations, owns its
supported process graph, reserves declared resources, measures the advertised
workload and rejects stale or invalid results. This is the release target in
[the application design](MCPME_DESIGN.md) and the original
[automatic preparation plan](AUTOMATIC_PREPARATION.md).

The original plan at `1b881e9` already states this same-user boundary and
distinguishes resource declarations from measured workload peaks. Its A1 and A2
gates require actual automatic preparation, private runtime closure, real MCP
calls, restricted sealed probes, matching attachment, installation and relocation.
A2 additionally requires measurements at maximum advertised image limits. A4
requires actual pinned inference, bounded tokens and queue, shared memory/GPU
admission and settlement before cancellation releases a reservation.

These requirements remain mandatory. Original source, build tools and global
runtime must be unavailable during sealed probes. Network and package-write
restrictions must pass where the selected recipe permits them; platform limits
must be recorded. A current exact-platform custom Seatbelt test supplies only
its tested restriction evidence and its documented maintenance limitation.
Vendor support classification does not replace actual negative controls.

Measured acceptance must retain its precise scope. Native foreground RSS,
separate process maxima, sampled task footprint and declared reservation are
different quantities. Missing memory measurements cannot become invented
positive values. Workload excess must fail verification or reduce the advertised
input limits. Reservations remain held until owned work settles, and unproven
ownership cannot be cleared by elapsed time or PID discovery.

Later CLI checkpoints added a stronger aggregate physical-memory/process release
condition. Their format-4 reports still have false hard-enforcement and lifetime
coverage flags. That condition needs a separate explicit reviewed reconciliation
for the fork-capable CLI profile, including positive measured evidence and
overload behavior. It remains open. It does not become a universal original
A1/A2/A4 requirement through the shared status ledger.

A non-bypassable hostile-code quota or general hostile same-user containment
would require a separately defined execution profile and compatibility evidence.
The current release target supplies no such claim. The actual public Mach
footprint call returned `KERN_NO_ACCESS`; further sampling cannot convert that
negative result into a hard-cap guarantee.

All A0, A0R and A1–A5 acceptance gates still apply, including lifecycle races,
actual installed/relocated artifacts and the independent final adversarial review.
No capability is enabled by this scope clarification. Additional runtime or
provider services retain A6 compatibility and authority gates.
