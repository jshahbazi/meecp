# Public Mach footprint API checkpoint

An actual owned-self fixture called `task_set_phys_footprint_limit(mach_task_self(), 128, &old_limit)` on macOS 26.7.1, build 25G241, Darwin 25.6.0, arm64. It returned `KERN_NO_ACCESS` (8). The initialized old-limit output remained unchanged. No child, allocation, mutation or terminal-usage branch ran. This is one factual negative on this platform, not a claim that the API can never be authorized.

The selected SDK declares the function in `mach/task.h`. Published [XNU implementation](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/task.c#L7114-L7137) checks `proc_check_footprint_priv` before setting a single task's limit. A declaration alone does not establish unprivileged access, aggregate domain charging, immutable limits or descendant inheritance. The reviewed published source is not asserted to equal the running kernel.

The generated C fixture uses only its own task port. It requests a modest cap; if permitted on another platform, its bounded child lowers and raises only its own cap, touches at most 1 MiB, reports inheritance/mutation return values and joins through the exact parent `wait4`. Child and parent alarms bound failure lifetime. It uses no privilege, entitlement, system setting, external task lookup or discovered-PID signal. The pytest harness compiles the actual SDK declaration and stores a private numeric receipt with source, helper, header and platform identities. Raw compiler/target diagnostics are never printed by the test.

Current development evidence:

- Source SHA256: `61e295b30b1fff846f36bc8631bc2477ca477f2b7d0c3de0c7a7711f942bad54`.
- Compiled helper SHA256: `81192d2e2fcad0b27c0161f92463898995d2e89391333cf3b4de984611336436`.
- Selected SDK `mach/task.h` SHA256: `d759d2351dc24b53c3e30984c6ccde154518273bf9edf7f14bdc4bdafb27e636`.
- Actual check: `uv run --locked pytest -q --tb=no tests/test_command_footprint_api_probe.py`; one test passed. No model or GPU work ran.

Inheritance, target mutation resistance and terminal accounting remain unestablished because permission failed before those checks. Even successful per-task settings would need separate proof of an aggregate descendant quota. The hard aggregate and lifetime gates remain false. Subsequent fixed-graph accounting must retain separate RSS, footprint and declaration scopes and rely on existing exact native ownership, without enabling public execution.
