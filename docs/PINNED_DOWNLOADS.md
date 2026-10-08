# Pinned package downloads

The owner CLI can fetch a prepared package ZIP using its exact SHA-256 and byte size. Downloading performs no package execution or installation. A completed job returns a private sealed package path and package digest; use that path in the existing reviewed installation flow.

In **Add App**, reveal **I already have a package download link** and select **Enter Package Link…**. The publisher supplies the URL and verification values; this form has no package discovery service. For an immediate first app, choose the bundled **Try JSON Utilities** route instead. Packages are produced by authors or preparation tooling, rather than assembled by the ordinary user.

## Fetch, inspect, and install

Use the same launcher and state directory as the running supervisor. Obtain the pins through a source you trust; a matching hash establishes agreement with those pins, not publisher trust.

```sh
scripts/meecp downloads fetch --url https://downloads.example.com/package.zip --sha256 ARCHIVE_SHA256 --size ARCHIVE_BYTES --package-digest PACKAGE_DIGEST --json
scripts/meecp downloads list --json
scripts/meecp downloads inspect DOWNLOAD_UUID --json
scripts/meecp downloads cancel DOWNLOAD_UUID --json
```

`--sha256` and `--size` are required. `--package-digest` optionally binds the package's manifest and lock identity as well. Fetch returns promptly with a `download_id`. Inspection reports status, stage, received bytes, attempt, and a stable error code. Wait for `completed` before using its `package_path`:

```sh
scripts/meecp install plan --package /absolute/private/download/package --name "My App" --json
scripts/meecp install inspect --plan PLAN_UUID --json
scripts/meecp install apply --plan PLAN_UUID --json
scripts/meecp operations get OPERATION_UUID --json
```

Planning displays actual tools, access declarations, resource estimates, and activation effects. Apply runs the existing supervised candidate checks. No app starts because a download completes.

Concurrent fetches with the same normalized URL and pins share one job. Repeating a completed fetch returns that job. Repeating a failed or canceled fetch starts another attempt under its existing identity. Cancellation reports `canceling` until the owned writer finishes and partial staging is removed. A completed job remains completed. Shutdown and restart preserve completed packages; interrupted jobs report `download_interrupted` and can be retried. Partial byte-range resume is unavailable, so a retry starts from byte zero.

## Network and archive contract

The URL must use HTTPS with a public destination and no credentials, query, or fragment. Connections verify TLS against the original hostname and use a validated resolved address. The downloader supplies no authentication and ignores proxy environment settings. Redirects are rejected; select a direct HTTPS artifact URL. Error responses contain stable codes, with no server body, raw network exception, or request content.

ZIP entries start at the package root: `meecp.package.json`, `meecp.lock.json`, and the exact locked artifact tree. There is no enclosing folder. Extraction rejects traversal, links, special files, duplicate or conflicting names, excessive entries, and excessive expanded bytes. The complete extracted tree passes the existing manifest, artifact hash, executable mode, license, platform, and schema checks before publication. Paths remain within owner-only staging.

Jobs and partial bytes belong to the host's private `downloads` directory. The host retains at most 4,096 jobs and runs at most four downloads concurrently. List returns the latest 100 summaries and the total retained count; inspect an individual ID for its full record. Archive bytes are capped at 64 GiB plus bounded ZIP metadata, and the verified artifact tree retains the package's 64 GiB limit. Completed package retention has no automatic reclamation in this milestone. Download authority is owner-only; existing read, prepare, lifecycle, and install grants do not authorize these routes. The management MCP tool set is unchanged. The native Add App window supports fetch, saved-job inspection, progress, cancel, retry and completed-package review.

The owner API is `POST /v1/downloads` with `{"url":...,"sha256":...,"size":...,"package_digest":...}` (the final field is optional), `GET /v1/downloads`, `GET /v1/downloads/{download_id}`, and `POST /v1/downloads/{download_id}/cancel` with `{}`.

## Prepare an archive

An author can create a ZIP from an already validated local package without executing it:

```sh
uv run --locked python scripts/prepare_package_archive.py --package /absolute/path/to/package --output /absolute/path/to/package.zip
```

The helper emits the archive SHA-256, byte size, and package digest. It preserves executable declarations, retains the complete license/provenance tree, rejects a changed package, and refuses to overwrite an existing archive. Publish the archive and pins through the chosen distribution channel separately.

The repeatable bundled-host check is `uv run --locked python scripts/verify_downloads.py --host /absolute/path/to/host --package /absolute/path/to/json-utilities-package`. It uses a synthetic byte transport inside the relocated private runtime, then normal owner CLI and real MCP clients in a temporary installation. This tests packaged download staging and installation without relying on a live publisher. Network policy and TLS framing have separate focused tests; the synthetic transport does not establish public publisher availability.

[Typed configuration and secret-reference injection](CONFIGURATION_SECRETS.md) are available through owner CLI/API installation planning. [Reviewed upgrades and recovery](UPGRADES_RECOVERY.md) and raw pinned model intake are implemented. [The completion audit](PHASE2_COMPLETION.md) records final artifact verification.
