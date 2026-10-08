# Phase 2: source imports and external preparation

This first Phase 2 milestone implements source intake and resumable preparation. It builds on Phase 1's sealed local packages, reviewed installation plans, and supervised runtime checks. The full Phase 2 acceptance in [MEECP_DESIGN.md](MEECP_DESIGN.md) remains open.

## Supported flow

Add App accepts a prepared package, a local source folder, or a UTF-8 Markdown/text document through a file chooser or drag-and-drop. Drafts appear in Manage Apps and survive window closure and host restart. An unfinished source draft asks what tools the assistant should receive. Documents require an explicit `instructions` or `reference` role. Reference information remains blocked on a supported document-service template.

## First app without package assembly

Open **Add App** and select **Try JSON Utilities**. The bundled starter is compiled during the native app build, signed, locked, and statically validated before the outer app signature seals it. Selecting it creates a normal prepared-package draft; **Review Package** and **Add App** retain the same snapshot, authorization, and supervised candidate checks as other installations. Select **Start after install** to add and start it. No user compiler, model download, package archive URL, or development checkout is required.

**I already have a package download link** reveals the manual publisher form. **Enter Package Link…** requires the publisher's direct HTTPS ZIP URL, byte count, and SHA-256; it does not discover or generate apps. Existing download jobs remain visible. Supported automatic directory/service preparation is the next milestone and is not advertised as runnable by this change.

Verify an actual bundled starter through relocated private host storage and real MCP clients:

```sh
uv run --locked python scripts/verify_meecp.py --host /absolute/path/to/meecp.app/Contents/Resources/host --package /absolute/path/to/meecp.app/Contents/Resources/starter-packages/json-utilities
```

The app build receipt records the starter package identity, digest, and lock hash. It binds the package after nested executable signing; the final app signature then seals that exact package.

The 2026-10-03 usability check completed **Try JSON Utilities → Review Package → Add & Start App → Open Installed App** in a fresh private native installation. The UI reported JSON Utilities ready with `validate_json`; the relocated bundled-host verifier passed all 16 checks, including real HTTP/stdio calls, independent Stop, restart and cleanup. The regression run passed 1,514 Python tests and 18 Swift tests, with one filesystem and three opt-in native installer lifecycle skips. The native contract and management UI self-tests passed. An independent adversarial review led to fixes for host restart state, strict JSON syntax, incomplete draft navigation, and bundled documentation; the native checks also covered saved partial drafts, explicit discard of invalid edits, and Retry Connection followed by a new starter draft. See [the review and fix record](reviews/USABLE_ONBOARDING_ADVERSARIAL.md). This check establishes the included-app flow; automatic directory preparation and actual remote-provider integration remain separate acceptance work.

Source intake reads metadata and selected content without importing modules or running source/build code. It snapshots an explicit file list and SHA-256 identities in owner-only staging. Folder discovery selects supported source files and excludes hidden paths, credentials, environments, caches, and generated directories. Review the selected file list in the exported specification; use repeated `--file` arguments to narrow CLI intake. Recognized credential content is rejected; authors must still select nonsecret source.

The first source snapshot limits are 2,048 files, 16 MiB per file, 64 MiB total, and 10,000 scanned entries. Draft metadata is capped at 512 KiB. List discovery returns compact summaries; inspect/export returns the full draft, and the native editor fetches it before editing. Prepared-package inventories remain in the verified package lock rather than duplicating thousands of runtime files in a draft. Raw weight artifacts and binary document parsers are outside this intake milestone. Prepared model packages remain supported through the existing package path.

## CLI

Use the installed launcher's absolute path, or `scripts/meecp` in a development checkout. The supervisor must be running with the same state directory.

```sh
scripts/meecp imports create --source /absolute/path/to/source --name "My Tool" --intent "Expose the JSON validation function" --json
scripts/meecp imports list --json
scripts/meecp imports inspect DRAFT_UUID --json
scripts/meecp imports export DRAFT_UUID --revision 1 --json
```

Export emits a versioned specification and generated agent instructions on demand. Save that JSON in a private file in the external agent's workspace, or use Copy Agent Instructions in the native window. The draft stores selected source references and structured nonsecret choices. It stores no generated instruction history or build logs. The handoff includes the draft/revision, snapshot location, resolved source identity, supported template IDs, and the package return command.

Use the [package-authoring guide](PACKAGE_AUTHORING.md) and bundled `templates` in that separate workspace. Preparation runs only under the external agent session's authority. The host does not infer an MCP interface from arbitrary source or launch an agent when instructions are copied.

```sh
scripts/meecp imports attach DRAFT_UUID --revision 1 --package /absolute/path/to/prepared-package --json
scripts/meecp install plan --import DRAFT_UUID --revision 1 --json
scripts/meecp install apply --plan PLAN_UUID --json
scripts/meecp operations get OPERATION_UUID --json
```

Attach validates the complete package statically and seals a copy. For source imports, its manifest must declare the exact exported `source.revision`. The host verifies package identity; that declaration alone does not prove that an agent implemented the requested behavior correctly. Review the actual tools, access declarations, resources, and start effects in the plan. Apply performs the existing supervised MCP initialization/schema checks. Client registration remains a separate action.

`--import` and `--package` are mutually exclusive. Import plans take the name/start choices from that exact draft revision. Package mode keeps its existing `--name`, `--start`, and `--start-policy` arguments. Install without `--start` still runs a candidate to verify it, then leaves the app stopped.

## Revisions and recovery

```sh
scripts/meecp imports revise DRAFT_UUID --revision 1 --intent "Expose only validation" --json
scripts/meecp imports cancel DRAFT_UUID --revision 2 --json
scripts/meecp imports expire DRAFT_UUID --revision 2 --json
```

An edit increments the revision and clears the attachment. A stale return or plan fails. Repeating attachment of the same package to the same revision returns the existing result; replacing it requires an explicit edit. Editing the original source or agent output does not alter the sealed attachment. Revalidating a tampered attachment prevents planning/apply.

Inactive cancel/expiry removes only host-owned draft staging and closes unapplied port reservations. If deletion is interrupted after the inactive state commits, the next host startup resumes that cleanup. Source folders and agent workspaces remain intact. Drafts show a 30-day retention deadline; expiry is explicit in this milestone, with no background cleanup scheduler. Submitted artifacts remain retained for installation/recovery.

An import plan includes its revision and package digest in its first durable publication. Revising or canceling during package staging rejects planning and reclaims that attempt's files and port reservation. Canceling a planning request waits for the staging worker to finish before deleting its files.

Apply checks the revision/digest before writing a durable installation intent and submitting the draft, without yielding between those steps. The registry intent contains its import binding and owns a separate candidate snapshot. On restart, reconciliation repairs a crash between the intent write and draft submission before draft mutation can occur. Concurrent applications of different plans for the same draft revision recover one operation. Interrupted installations retain Phase 1's `recovery_required` behavior; this milestone does not add automatic runtime reconciliation.

Submitted drafts reject edits, return, cancel, and expiry while the operation is active, and remain finalized afterward. Failed/canceled operations retain their submitted draft as history; another attempt requires a new draft. Installation-operation cancellation is available before exposure; submitted draft cancellation remains a separate inactive-draft action. Follow owner-authorized recovery through the linked operation.

## Completion checks and later work

Scoped owner-issued grants and a separate management MCP facade are implemented in the [management milestone](MANAGEMENT_MCP.md).

Pinned prepared-package ZIP downloads are implemented through the owner CLI and API; see [pinned downloads](PINNED_DOWNLOADS.md). They return a statically verified package for the existing install plan, with exact byte/hash pins, durable progress, cancellation, and retry. Native download controls are implemented. Partial byte-range resume remains unsupported; interrupted jobs retry safely.

Typed nonsecret configuration and immutable owner-managed secret references are implemented through owner CLI/API planning and private runtime descriptors; see [configuration and secrets](CONFIGURATION_SECRETS.md). Import requirements bind the same choices to the draft revision. Native typed forms and installed-settings edits through reviewed generation upgrades are implemented.

- Authenticated descriptor-configured MCP HTTP factories use a shared resident service with bounded sessions and explicit DELETE cleanup.
- [Reviewed upgrades](UPGRADES_RECOVERY.md) use private candidate data snapshots, durable exposure permission, pre-exposure rollback and selected-generation recovery. Removal retains all private data generations.
- Raw pinned JevK5 GGUF and engine intake, resumed engine selection, export-driven preparation and actual model-client acceptance are implemented.
- [The completion audit](PHASE2_COMPLETION.md) records passed committed-build checks. Reference-document services and integrated provider preparation remain later work.

Read [verification](MEECP_PHASE2_VERIFICATION.md) for the checks performed on this milestone. The repeatable bundled-host acceptance command is `uv run --locked python scripts/verify_phase2.py --host /absolute/path/to/host`. It uses only synthetic source and a temporary private installation. Neither prepared-package declarations nor synthetic integration checks establish publisher trust or model quality.
