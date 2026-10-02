# Phase 2: source imports and external preparation

This first Phase 2 milestone implements source intake and resumable preparation. It builds on Phase 1's sealed local packages, reviewed installation plans, and supervised runtime checks. The full Phase 2 acceptance in [MCPME_DESIGN.md](MCPME_DESIGN.md) remains open.

## Supported flow

Add Project accepts a prepared package, a local source folder, or a UTF-8 Markdown/text document through a file chooser or drag-and-drop. Drafts appear in Manage Projects and survive window closure and host restart. An unfinished source draft asks what tools the assistant should receive. Documents require an explicit `instructions` or `reference` role. Reference information remains blocked on a supported document-service template.

Source intake reads metadata and selected content without importing modules or running source/build code. It snapshots an explicit file list and SHA-256 identities in owner-only staging. Folder discovery selects supported source files and excludes hidden paths, credentials, environments, caches, and generated directories. Review the selected file list in the exported specification; use repeated `--file` arguments to narrow CLI intake. Recognized credential content is rejected; authors must still select nonsecret source.

The first source snapshot limits are 2,048 files, 16 MiB per file, 64 MiB total, and 10,000 scanned entries. Draft metadata is capped at 512 KiB. List discovery returns compact summaries; inspect/export returns the full draft, and the native editor fetches it before editing. Prepared-package inventories remain in the verified package lock rather than duplicating thousands of runtime files in a draft. Raw weight artifacts and binary document parsers are outside this intake milestone. Prepared model packages remain supported through the existing package path.

## CLI

Use the installed launcher's absolute path, or `scripts/mcpme` in a development checkout. The supervisor must be running with the same state directory.

```sh
scripts/mcpme imports create --source /absolute/path/to/source --name "My Tool" --intent "Expose the JSON validation function" --json
scripts/mcpme imports list --json
scripts/mcpme imports inspect DRAFT_UUID --json
scripts/mcpme imports export DRAFT_UUID --revision 1 --json
```

Export emits a versioned specification and generated agent instructions on demand. Save that JSON in a private file in the external agent's workspace, or use Copy Agent Instructions in the native window. The draft stores selected source references and structured nonsecret choices. It stores no generated instruction history or build logs. The handoff includes the draft/revision, snapshot location, resolved source identity, supported template IDs, and the package return command.

Use the [package-authoring guide](PACKAGE_AUTHORING.md) and bundled `templates` in that separate workspace. Preparation runs only under the external agent session's authority. The host does not infer an MCP interface from arbitrary source or launch an agent when instructions are copied.

```sh
scripts/mcpme imports attach DRAFT_UUID --revision 1 --package /absolute/path/to/prepared-package --json
scripts/mcpme install plan --import DRAFT_UUID --revision 1 --json
scripts/mcpme install apply --plan PLAN_UUID --json
scripts/mcpme operations get OPERATION_UUID --json
```

Attach validates the complete package statically and seals a copy. For source imports, its manifest must declare the exact exported `source.revision`. The host verifies package identity; that declaration alone does not prove that an agent implemented the requested behavior correctly. Review the actual tools, access declarations, resources, and start effects in the plan. Apply performs the existing supervised MCP initialization/schema checks. Client registration remains a separate action.

`--import` and `--package` are mutually exclusive. Import plans take the name/start choices from that exact draft revision. Package mode keeps its existing `--name`, `--start`, and `--start-policy` arguments. Install without `--start` still runs a candidate to verify it, then leaves the project stopped.

## Revisions and recovery

```sh
scripts/mcpme imports revise DRAFT_UUID --revision 1 --intent "Expose only validation" --json
scripts/mcpme imports cancel DRAFT_UUID --revision 2 --json
scripts/mcpme imports expire DRAFT_UUID --revision 2 --json
```

An edit increments the revision and clears the attachment. A stale return or plan fails. Repeating attachment of the same package to the same revision returns the existing result; replacing it requires an explicit edit. Editing the original source or agent output does not alter the sealed attachment. Revalidating a tampered attachment prevents planning/apply.

Inactive cancel/expiry removes only host-owned draft staging and closes unapplied port reservations. If deletion is interrupted after the inactive state commits, the next host startup resumes that cleanup. Source folders and agent workspaces remain intact. Drafts show a 30-day retention deadline; expiry is explicit in this milestone, with no background cleanup scheduler. Submitted artifacts remain retained for installation/recovery.

An import plan includes its revision and package digest in its first durable publication. Revising or canceling during package staging rejects planning and reclaims that attempt's files and port reservation. Canceling a planning request waits for the staging worker to finish before deleting its files.

Apply checks the revision/digest before writing a durable installation intent and submitting the draft, without yielding between those steps. The registry intent contains its import binding and owns a separate candidate snapshot. On restart, reconciliation repairs a crash between the intent write and draft submission before draft mutation can occur. Concurrent applications of different plans for the same draft revision recover one operation. Interrupted installations retain Phase 1's `recovery_required` behavior; this milestone does not add automatic runtime reconciliation.

Submitted drafts reject edits, return, cancel, and expiry while the operation is active, and remain finalized afterward. Failed/canceled operations retain their submitted draft as history; another attempt requires a new draft. Installation-operation cancellation remains unsupported.

## Remaining Phase 2 work

- Scoped owner-issued grants and a separate management MCP facade.
- Pinned downloads and broader package/configuration/secret-reference support.
- Upgrades with candidate data snapshots, exposure-aware recovery, rollback, and removal preserving data by default.
- Raw model/engine intake and end-to-end model preparation acceptance.
- Supported reference-document services and integrated provider preparation.

Read [verification](MCPME_PHASE2_VERIFICATION.md) for the checks performed on this milestone. The repeatable bundled-host acceptance command is `uv run --locked python scripts/verify_phase2.py --host /absolute/path/to/host`. It uses only synthetic source and a temporary private installation. Neither prepared-package declarations nor synthetic integration checks establish publisher trust or model quality.
