# Phase 2 onboarding verification

Date: 2026-10-01. This record covers the first source-import and external-preparation milestone, not the full Phase 2 acceptance matrix. The subsequent scoped-grant and management MCP milestone is recorded in [MANAGEMENT_MCP.md](MANAGEMENT_MCP.md).

## Automated coverage

The locked Python suite checks static intake/exclusions, private state, exact source identities, document roles, resumable draft revisions, sealed package return, cancellation/expiry, stale/tampered results, submission finalization, import-bound plans from their first durable publication, revision/cancel races during staging, canceled staging workers, duplicate apply across different plans, inactive-staging cleanup after restart, and restart recovery between durable installation intent and draft submission. Host integration verifies an imported tool using a real MCP SDK client. Existing package, process, gateway, JevK5, migration, privacy, and installation-recovery regressions remain included.

Commands:

```sh
uv run --locked python -m pytest -q
swift test
swift build -c release --product McpmeMenuBar
uv run --locked python .github/scripts/scan_secrets.py
uv run --locked python scripts/build_mcpme_app.py --output dist/mcpme-phase2-transport.app
dist/mcpme-phase2-transport.app/Contents/MacOS/mcpme --native-self-test
uv run --locked python scripts/verify_phase2.py --host dist/mcpme-phase2-transport.app/Contents/Resources/host
```

Results on Apple silicon macOS 26:

| Check | Result |
| --- | --- |
| Full locked suite before main integration | 281 passed. |
| Full locked suite after main integration, 2026-10-02 | 420 passed in 42.13 seconds. |
| Full locked suite after merging security updates through main `93447d5` | 463 passed in 45.71 seconds. |
| Repository history credential scan after main integration | `secret_scan_passed`. |
| Native private-transport suite after security integration | 5 passed. |
| Import-specific tests | 44 passed, including interrupted cleanup recovery on startup, bounded discovery metadata, and ancestor-symlink replacement. |
| Import/host tests after the independent audit | 58 passed; import bindings exist at first publication, stale planning reclaims staging, canceled copying completes before reclamation, and inactive cleanup rejects symlink replacements. |
| Template-specific tests | 8 passed, including real SDK tools/schema/result/error checks. |
| Native release build and ad hoc bundle verification | Passed. |
| Native draft/private-export self-test | `native_contracts_passed`. |
| Relocated bundled-host verifier | All reported checks passed: raw Python CLI intake/export/preparation/return/install, direct private-runtime prepared-package intake, duplicate plans recovering one operation, HTTP and stdio SDK calls, and installed-draft recovery after host restart. |
| Native visual check in a separate test installation | File chooser accepted a synthetic Python folder; intent editing and Save Draft produced the visible Waiting for prepared package state. Controls remained readable and reachable. |

A separate preparation acceptance run also passed 13 checks, including deleting the original source, moving the generated private-runtime package, attaching it and deleting the agent's package directory, and calling the installed arithmetic tool through a real MCP client. Its package contained 2,906 artifacts and 74,137,296 bytes. The generated adapter omits unsupported `experimental` capability declarations and disables bytecode writes from interpreter startup so candidate execution preserves the sealed artifact tree.

The initial isolated worktree excluded concurrent branding changes. PR preparation on 2026-10-02 integrates the current main branch's branding, CI, release tooling, and private-socket transport while preserving bundled preparation templates and their provenance inventory. Native draft controls retain their delegates and extended import/attachment timeouts. The relocated host ships and hashes the shared transport module and uses safe-path Python launchers. Development apps use new output paths and native testing uses a separate state directory. Existing installed projects and the default application state were not changed.

## Independent audit corrections

The follow-up review reproduced a defect where a revision or cancellation during asynchronous staging left a durable package plan without its import binding. The corrected implementation validates the binding before publication and includes it in the first atomic plan write. Failed planning removes its plan, staged package, and reserved listener; a canceled request waits for the copying worker before reclamation. Tests observe the initial durable write and reject the persisted plan after restart and cancellation.

The review also reproduced canceled/expired staging surviving an interrupted deletion. Startup now reclaims staging for drafts already committed as inactive, validates their identities and private directories, and sanitizes cleanup errors. Original sources and agent packages remain preserved. This recovery does not expire active drafts by deadline.

## Claim boundary

Source intake, attachment, and planning never execute source/build code or package entrypoints. Agent preparation runs separately. Apply performs supervised candidate initialization and exact tool-schema checks. A source identity declaration does not prove semantic fidelity to arbitrary user intent. Synthetic package/client checks do not establish model decision quality or calibration.

At this milestone, downloads, scoped management grants/MCP, upgrades/data rollback, removal, raw model preparation, reference-document services, and integrated preparation were pending. Scoped grants/MCP were added subsequently. This milestone does not validate interruption/recovery at every future upgrade stage, enabled login-item transfer, or drag-and-drop accessibility on every input device. Public signing/notarization is implemented in the protected-main release workflow and was not exercised by this Phase 2 verification. The tested local bundle remains an ad hoc development artifact.

Later milestones add [pinned downloads](PINNED_DOWNLOADS.md) and [typed configuration with secret references](CONFIGURATION_SECRETS.md), each with a separate bundled-host verifier and its own acceptance boundary. The settings verifier uses a synthetic prepared stdio package, private owner input, reference-only plans, real MCP transports, host restart, and revocation. It does not establish the pending upgrade/data-recovery or raw-model acceptance.
