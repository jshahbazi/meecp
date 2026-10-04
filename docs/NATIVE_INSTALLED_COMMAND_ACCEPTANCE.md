# Native installed command acceptance checkpoint

Status: the bounded development path from an ordinary command directory through the native generated form and installed SDK passes. A3 still requires final integrated signed-app acceptance and independent review. Public command execution remains disabled; aggregate process and memory enforcement and complete lifetime coverage remain unresolved.

## Exercised path

`scripts/verify_phase3_native_command.py` accepts an explicit packaged host, compiled native application, and compiled synthetic command. It validates the host's closed artifact inventory, relocates the host and native input using the shared onboarding harness, and runs with private state, home, temporary storage, and the bundled Python and MCP SDK. The selected source has exactly four files: a synthetic executable, closed input and output schemas, and its fixture notice. It has no mcpme manifest, descriptor, preparation receipt, or lock.

The native `--directory-native-acceptance` driver supplies the folder selection to the production `beginImport` handler used by Choose Files and drop. It then uses the production setup save, generated form, review, preparation start, poll, and matching-attachment handlers. The fixture simulates the selected folder and form answers; it does not claim physical dialog interaction. The form selects the documented `json-stdin-stdout-v1` interface, fixed arguments, schemas, effect declaration, synthetic input and expected output, and deadline. The host prepares and probes the exact `mcpme.native-command` 1.0.1 recipe through the real measured command helper and SDK verifier.

The driver requires the attached source revision, import revision, receipt binding, package digest, recipe identity, and complete tool contracts to agree. It installs through the existing authorized CLI plan/apply workflow. Actual MCP clients make three HTTP and three stdio calls: after installation, after deleting the original source and restarting, and after copying the sealed package, deleting the original import state, installing in fresh state, and deleting the copied source package. Ordinary native preparation and installed-slot shutdown must settle all known ownership and reservations.

The host is enabled only by a private development embedding that supplies the existing typed `HostCommandAdmission` and `CommandPreparationResourceAdmission` and exact builtin family to `Supervisor`. Ordinary CLI/API payloads, environment, inventory, selected files, or recipes cannot supply these admissions. A separate ordinary-host control requires automatic preparation and recipe execution to remain false, a static command form to remain nonexecuting, and preparation start to create no job.

## Scope and stale authority guards

The native context requires canonical POSIX paths, owner-only directories, owner-only regular single-link fixture and marker files, a fresh nonce with at most five minutes of validity, an exact current manager file identity, and the selected source directory's device and inode. It consumes the nonce once with exclusive no-follow creation. Before every owner request and before consuming its callback, it rechecks the current context and source/manager identity. Requests use a fixed route allowlist and finite request count; polling and wait deadlines are bounded. Reports are exclusive owner-only files containing identities and fixed status metadata. Caller input, outputs, credentials, and raw exception text are never included.

Compiled controls include a valid Python-produced context reaching the exact-once claim and denials for stale expiry, wrong nonce, replay, same-byte manager inode substitution, source inode substitution, symlinks, hardlinks, wrong permissions, an existing output, and an output symlink. Cleanup controls join held children and preserve authority when a separate fault-control state has unknown legacy ownership, a command reservation, an unsettled command journal, or malformed authority. Cleanup validates the exact journal schema and published format-3 completion through the existing read-only verifier; a state label alone or an empty journal with the wrong version cannot establish settlement.

## Actual topology and retained recovery state

The harness also runs the existing actual installed-command controls for fork/exec descendants, file descriptors, denied group escape, cancellation, held backend death, and held ancestor-group death. The isolated command owner survives ancestor-group death and publishes valid settlement; that proves command settlement for the exercised invocation.

The ancestor-group control also kills the legacy gateway reaper. Its legacy gateway handles existed inside the killed backend; the driver has joined the backend's original held `Popen`, but has no surviving held handle or receipt proving that legacy gateway joined. Its durable legacy authority therefore remains unknown. The harness explicitly restarts recovery, requires `ownership_lost`, requires the slot reservation to remain, denies three replacement starts, opens no new worker or listener, and requires legacy and command authority hashes to survive shutdown unchanged. It never adopts or signals recorded PIDs. The private workspace is retained and requires reconciliation. The report distinguishes joined driver children from the unknown legacy child and sets `all_ownership_settled` to false. Expected conservative retention does not establish complete topology cleanup.

The first complete native/installed run, v5, remains a failed report because its unconditional empty-legacy-ledger check encountered this retained authority. That report was preserved and independently inspected through actual recovery before the v6 harness made the deliberate fault's expected contract explicit. Ordinary positive cleanup still requires empty legacy ownership, empty reservations, and settled command journals. Unexpected retention in any other control fails acceptance and preserves the workspace.

## Immutable local evidence

The host comes from clean source `509b8077f196911c3764b3bb113dbe9b54352ad3`; its build record and 4,903 artifact entries are checked before and after acceptance. Native build inputs are separately recorded and checked before and after compilation. This checkpoint changes no backend, producer, existing recipe, or component bytes.

| Artifact | SHA-256 | Result |
| --- | --- | --- |
| Native executable `McpmeMenuBar-a3-directory-v3` | `ef6299b04f06e80f223dbc9f1367c5256db6ddca925de7e6813b523875668066` | Compiled, unsigned local acceptance |
| Compiled synthetic command | `97aae633db1f13a514117d43613d1fb0d0a5b42f8c8138f522ba2371b1f753ec` | Actual native fixture |
| Native build receipt | `5efab9f1ddbb2377abd87cccbfff99c9226b28eaba8ee24a85b739323bb50dc5` | Compiler joined; input bytes preserved |
| Native directory actual v5 | `50b65210063f108cd54c40a96497b4371817833fd1cd968ad9a2ee50261dccf8` | Failed; original retained authority disclosed |
| Independent ancestor-loss recovery v1 | `7116dbab9385631a63eb28b95cedb517521c2bd640183a304ef0712aea2251a1` | Actual recovery fenced; authority retained |
| Native directory actual v6 | `a48c9650c541097bf75ba7c5d8f7a6756ca475be97892727173765365d890def` | Ordinary path and deliberate retention contract passed |
| Native directory actual v7 | `66046f94b61d8a11a91a60df975b3e3217581f8d01a26520a191aabdc25b6845` | Same actual path passed with receipt-backed cleanup |
| Seven native contract checks | `90b208fba60daa4945e837911f126c3d1241d9ef4358dae1560a0c3da4563baa` | All passed on the recorded native binary |

Private receipts are `/private/tmp/mcpme-a3-native-checkpoint-4t1_djs9/build-receipt-v1.json`, `/private/tmp/mcpme-a3-native-directory-actual-v5.json`, `/private/tmp/mcpme-a3-ancestor-loss-recovery-v1.json`, `/private/tmp/mcpme-a3-native-directory-actual-v6.json`, `/private/tmp/mcpme-a3-native-directory-actual-v7.json`, and `/private/tmp/mcpme-a3-native-selftests-v1.json`. The v7 receipt names the preserved workspace and exact retained authority hashes. Focused checks passed 66 cases before the final receipt-backed cleanup tightening, followed by all 11 cleanup and harness controls on the final source. Swift tests passed all 21 cases with native installer lifecycle tests enabled and no skips.

The measured proof uses `held-group-plus-helper-windowed-footprint-v1`, observes three group members and more than 8 MiB of scoped footprint, and explicitly reports `hard_aggregate_enforced: false` and `lifetime_coverage_complete: false`. Observed windows are not hard limits or complete lifetime evidence. This local artifact is unsigned. Final signed relocation must preserve the complete matching application bundle and verify its signatures; copying its signed Mach-O out of the bundle cannot establish that result.
