# Phase 2 onboarding verification

Date: 2026-10-01. This record covers the first source-import and external-preparation milestone, not the full Phase 2 acceptance matrix.

## Automated coverage

The locked Python suite checks static intake/exclusions, private state, exact source identities, document roles, resumable draft revisions, sealed package return, cancellation/expiry, stale/tampered results, submission finalization, import-bound plans, duplicate apply across different plans, and restart recovery between durable installation intent and draft submission. Host integration verifies an imported tool using a real MCP SDK client. Existing package, process, gateway, JevK5, migration, privacy, and installation-recovery regressions remain included.

Commands:

```sh
uv run --locked python -m pytest -q
swift build -c release --product McpmeMenuBar
uv run --locked python scripts/build_mcpme_app.py --output dist/mcpme-phase2.app
dist/mcpme-phase2.app/Contents/MacOS/mcpme --native-self-test
uv run --locked python scripts/verify_phase2.py --host dist/mcpme-phase2.app/Contents/Resources/host
```

Results on Apple silicon macOS 26:

| Check | Result |
| --- | --- |
| Full locked suite, isolated Phase 2 worktree | 274 passed in 39.78 seconds. |
| Import-specific tests | 41 passed, including interrupted cleanup retry, bounded discovery metadata, and ancestor-symlink replacement. |
| Follow-up import/host tests after compact prepared-package inventories | 51 passed; prepared inventories reference the verified lock and preserve space for operation identities. |
| Template-specific tests | 8 passed, including real SDK tools/schema/result/error checks. |
| Native release build and ad hoc bundle verification | Passed. |
| Native draft/private-export self-test | `native_contracts_passed`. |
| Relocated bundled-host verifier | All reported checks passed: raw Python CLI intake/export/preparation/return/install, direct private-runtime prepared-package intake, duplicate plans recovering one operation, HTTP and stdio SDK calls, and installed-draft recovery after host restart. |
| Native visual check in a separate test installation | File chooser accepted a synthetic Python folder; intent editing and Save Draft produced the visible Waiting for prepared package state. Controls remained readable and reachable. |

A separate preparation acceptance run also passed 13 checks, including deleting the original source, moving the generated private-runtime package, attaching it and deleting the agent's package directory, and calling the installed arithmetic tool through a real MCP client. Its package contained 2,906 artifacts and 74,137,296 bytes. The generated adapter omits unsupported `experimental` capability declarations and disables bytecode writes from interpreter startup so candidate execution preserves the sealed artifact tree.

The isolated worktree excludes concurrent branding changes. Development apps were built into new paths and native testing used a separate state directory. Existing installed projects and the default application state were not changed.

## Claim boundary

Source intake, attachment, and planning never execute source/build code or package entrypoints. Agent preparation runs separately. Apply performs supervised candidate initialization and exact tool-schema checks. A source identity declaration does not prove semantic fidelity to arbitrary user intent. Synthetic package/client checks do not establish model decision quality or calibration.

Downloads, scoped management grants/MCP, upgrades/data rollback, removal, raw model preparation, reference-document services, and integrated preparation are pending. This milestone does not validate interruption/recovery at every future upgrade stage, enabled login-item transfer, drag-and-drop accessibility on every input device, or public distribution signing/notarization. The build remains a development artifact.
