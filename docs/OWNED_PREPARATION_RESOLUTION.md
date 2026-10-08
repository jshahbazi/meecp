# Owned dependency and interface resolution

This private Phase 3 core resolves a selected Python candidate under explicit plan/start authority. It does not enable the public preparation runner or establish the A1 native/build/install gate.

`ResolutionStore(imports)` shares the import SQLite transaction lock and keeps separate `resolution_plans`, `resolution_jobs`, and `accepted_resolutions` tables. `OwnedResolutionRunner(store, host_root, reaper, reserve_resources=..., release_resources=..., enabled=False)` exposes:

- `await plan(import_id, revision, candidate_id, policy, expires_in=300)` performs static validation and binds the exact source revision, candidate, policy, private toolchain, loaded SDK dependency closure, reviewed implementation and sandbox executable. It executes no source and downloads nothing.
- `await start(plan_id, plan_digest, idempotency_key)` atomically admits a bounded job. An identical key returns the same job; a changed plan under that key fails.
- `store.get(job_id)`, `store.list_summaries()`, `await cancel(job_id)`, `await recover()`, and `await close()` expose durable lifecycle and joined shutdown. Resource callbacks use `resolution:<job UUID>` identifiers and receive a `ResolutionPolicyV1` with memory, disk and wall budgets.
- `store.resolve_result(import_id, revision, candidate_id, result_digest)` rechecks source/candidate identity and every derived file, then returns `(ResolutionResultV1, derived_directory)`. Invoke it off the event loop.

The closed policy requires `resolve_dependencies`; Python MCP candidates also require `discover_interface`. The network scope is exactly `pypi.org` and `files.pythonhosted.org`. The initial implementation supports the pinned private CPython 3.12.13, uv 0.12.13 and MCP SDK 1.30.0 on the existing Apple silicon host.

## Derived inputs and revision changes

The source snapshot is never edited. A separate project preserves each declared runtime dependency and adds the pinned preparation SDK requirement. Source URL/local/VCS dependencies, dynamic project metadata, custom uv settings, dependency groups and incompatible Python/SDK requirements fail explicitly. Optional dependency groups are not activated. This operation is explicit dependency re-resolution; existing descriptor-v1 paths and original locks retain their semantics.

The result contains a separately identified dependency project, lock and, for discovery, full interface metadata. Every derived reference binds the original source revision, request and policy digests, producer, private runtime and immutable derived collection. The result supplies no builder, attachment or installation authority. No generated lock enters an original `SourceBinding`.

When selection advances a draft's metadata revision, call `prepare_result_binding(import_id, revision, candidate_id, result_digest)` off-loop before its write transaction, then `commit_result_binding(proof, new_revision)` inside that transaction. Only bounded metadata and authority checks occur under the transaction. The accepted reference commits or rolls back with selection. The standalone `bind_result` convenience method performs both phases and must not be called inside a transaction. The helper preserves the original result/request/digests. Later revisions require another explicit carry; reads never broaden authority automatically. Final execution binds both the immutable result digest and the selected current revision and rechecks derived bytes while consuming them.

## Network and execution boundaries

The loopback broker requires credentials and exact Host matching, rejects Origin and request bodies, and exposes only mapped public-registry wheels and hash-bound core metadata. It uses public-IP-pinned TLS, rejects redirects, bounds requests/bytes/metadata, and checks artifact hashes and sizes. uv receives a named index password through its private environment. The sealed lock replaces only exact observed broker mappings with canonical upstream registry/wheel URLs and rejects remaining ephemeral locators or credentials.

The broker implements the [PyPA Simple Repository API](https://packaging.python.org/en/latest/specifications/simple-repository-api/) and hash-bound `.metadata` sidecars. The [uv index configuration](https://docs.astral.sh/uv/concepts/indexes/) supplies the named default registry; no-build, no-sources, private cache and disabled interpreter downloads constrain the reviewed command. A macOS sandbox independently denies all network except the exact broker loopback port and permits only the pinned interpreter as a child executable. Flags alone are not the network boundary.

Discovery installs hash-checked wheels into a copied private runtime, then starts only the selected module or declared no-argument entrypoint. A real SDK client initializes and lists complete tool schemas under a deadline; it never calls tools. Discovery cannot fork, access the network, write source/runtime/derived inputs, or read native ownership receipts. It may write only private home/tmp directories. Unsupported capabilities, pagination, tool metadata and malformed schemas fail closed.

Every child is registered in the native completion journal before spawn. Cancellation joins copying, broker requests, SDK tasks and owned-process cleanup. Missing native proof retains workspace and reservation for reconciliation; recovery neither guesses PIDs nor reruns interrupted resolution. Successful publication occurs after native settlement. Temporary source/runtime/cache/index bytes are pruned, while exact derived files and receipts remain.

Wall and disk checks run during work. Native child peak memory is checked at completion; this is measured peak enforcement, not an OS address-space ceiling. The broker has independent metadata/request bounds. Public activation still requires the original integrated resource and lifecycle gates.

## Verification

Focused tests exercise strict contracts, original-source preservation, revision/idempotency barriers, accepted-reference rollback/carry, result tampering, native pending-proof recovery, actual child cancellation, joined thread cancellation, broker authentication/hash/URL checks, bounded gzip and actual macOS sandbox denials.

Opt-in cases use a real built private host and fresh empty caches:

```sh
MEECP_RESOLUTION_HOST=/absolute/path/to/private-host uv run pytest -q tests/test_preparation_resolution.py
```

The unlocked-callable case resolves a real registry closure without an original lock. The foreground MCP case installs real pinned runtime wheels and discovers a dynamic schema from a running SDK server. The subsequent [derived build checkpoint](DERIVED_PREPARATION_BUILD.md) verifies generated questions, owned package construction, SDK fixtures, attachment and relocation. Installed/native acceptance and public activation remain separate gates.
