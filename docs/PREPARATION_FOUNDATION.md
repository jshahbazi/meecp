# Preparation implementation: foundation

This is the first implementation slice of the [automatic preparation plan and reviews](https://github.com/jshahbazi/mcpme/pull/23). It adds static recipe management, preparation contracts and atomic result admission. Automatic execution remains unavailable. A recipe showing `enabled` has passed metadata compatibility checks; its `runnable` value remains `false`.

## Implemented boundary

| Area | Behavior |
| --- | --- |
| Portable descriptor | Strict schema 1 for an explicitly selected Python callable or Python MCP module, full tool schemas, pinned dependencies, artifact identities, declared access/resources and synthetic fixtures. |
| Recipe bundle | Closed tree with `mcpme.recipe.json`, `mcpme.recipe.lock.json`, exact component pins, bounded input/form metadata and retained license/provenance files. Addition and inspection execute no recipe or source code. |
| Recipe development kit | `recipes scaffold` produces a complete metadata example; `recipes lock` refreshes hashes after authoring; `recipes check` validates its closed tree without registering it. These operate locally. |
| Registry | Immutable recipe ID/version/digest, static addition, enable/disable/deprecate/revoke/remove, evidence identity checks and retained bytes. Removing a revoked recipe preserves its revocation. |
| Preparation planning | Checks the selected recipe, dependency lock, source license, module path, fixtures and import requirements. Reports the missing runner/verifier capability explicitly. |
| Internal job contract | Durable identity, caller-key idempotency, transitions, cancellation, reservation settlement, interrupted recovery, locked receipts and exact-result admission. This contract is exercised with synthetic host fixtures; there is no execution endpoint. |
| Attachment | Package validation and copying occur outside the database write transaction. A short transaction chooses the admission winner and publishes the attachment. Owned staging and crash recovery protect live copies. |
| Management | Owner-only recipe and preparation metadata routes; CLI access to these routes. Existing management grant scopes remain unchanged. |

Descriptor, recipe and job contracts have independent versions. New JSON identities use the domain-separated canonical serialization in [preparation_contracts.py](../backend/mcpme/preparation_contracts.py). Existing source identities and package schema/lock digests retain their original algorithms. Duplicate keys, nonfinite values, extra fields, credentials recognizable by the intake patterns, and excessive depth/size fail before persistence. Initial planning rejects regular-expression schema keywords, dynamic references and recursive references to bound static schema work.

## One admission transaction

Recipe state and preparation jobs use the existing import database, connection and transaction lock. Draft revision/state, sticky recipe revocation, job cancellation, verifier binding and exact package digest are checked in the same transaction that publishes the attachment and job success. An invalidation that commits first prevents a late result. Success that commits first remains successful when a later preparation cancellation arrives.

Installation operations and installation grants retain their existing registry database and authorization rules. This slice creates no new preparation execution grant and delegates no owner credential. Only internal host code can record verification or invoke managed admission; recipe/API callers cannot report a passing gate. The runtime runner and verifier identities currently identify metadata-only contract versions and confer no runtime acceptance.

Preparation receipts are ordinary locked artifacts named `mcpme.preparation.json`. They bind job/source/descriptor/recipe/policy identities, dependency/runtime and fixture identities. The final package digest lives in the job record, avoiding a digest cycle. The internal verifier must also compare the candidate's declared interface/access/resources/lifecycle with the selected descriptor. A receipt by itself establishes no runtime behavior.

Canceling a request or shutdown awaiting file work does not stop its underlying thread. The supervisor retains that work, its database and owner lock until it settles, including across repeated canceled shutdown attempts. Concurrent static file work is bounded. The current attachment copier does not offer immediate byte-level cancellation; draft invalidation closes result admission while copying settles. Job reservation release requires explicit ownership settlement. Interrupted work with unsettled ownership blocks a replacement attempt.

Same-user processes remain inside the trust boundary. Cryptographic validation detects changed bytes. Supported services do not delegate installation authority to recipes. Arbitrary same-user code is not isolated by a job descriptor, an advisory lock or a passing fixture.

## Authoring and registering a recipe

The following commands create an example using the existing callable component, check it statically, then register and enable its metadata:

```sh
mcpme recipes scaffold --output /absolute/path/to/new-recipe --id local.example --version 1.0.0 --component python-function-v1 --json
mcpme recipes check --path /absolute/path/to/new-recipe --json
mcpme recipes add --path /absolute/path/to/new-recipe --json
mcpme recipes enable --id local.example --version 1.0.0 --json
mcpme recipes inspect --id local.example --version 1.0.0 --json
mcpme recipes list --json
```

The scaffold includes reusable bounded input/form metadata, synthetic fixture metadata, full template notices and an exact tree lock. After editing metadata and selecting a new version, run `mcpme recipes lock --path /absolute/path/to/new-recipe --json`, then `recipes check`. Locking preserves declared provenance/license records and captures file hashes; it does not establish publisher trust. Registration never runs an installer, matches source by executing a hook, or imports a Python module. The first catalog recognizes the exact bundled `python-function-v1` and `existing-mcp-v1` components; unsupported components and executable workers cannot be enabled.

IDs are namespaced and versions immutable. The same ID/version with different bytes fails. Explicit descriptors select their immutable recipe independently of overlapping variants. A new host capability or executable worker requires implementation and independent acceptance before it is available through this mechanism.

The management API exposes `GET/POST /v1/recipes`, `POST /v1/recipes/{inspect|enable|disable|deprecate|revoke|remove}`, `POST /v1/preparations/plan`, `GET /v1/preparations`, `GET /v1/preparations/{job_id}` and `POST /v1/preparations/{job_id}/cancel`. Registry action bodies contain `recipe_id` and `version`; addition selects an absolute local `path`. Planning supplies `import_id`, `revision` and the descriptor object. There are no public start, verifier-evidence or result-admission routes.

## Verification and remaining release gates

Synthetic checks cover strict serialization/schema bounds, static nonexecution, independent recipe authoring, immutable collisions, evidence/lifecycle retention, stale source/lock bindings, cancellation and revocation winners, transaction rollback, process-independent staging leases, management responsiveness, request and shutdown cancellation, exact import tool requirements, and concurrent startup/edit/cancel cleanup. Test packages used for admission are synthetic sealed fixtures; these checks do not represent a newly executed preparation or model workload.

Run `uv run --locked python -m pytest -q` for regression coverage. After building a development CLI host, `uv run --locked python scripts/verify_preparation_foundation.py --host /absolute/path/to/host` checks a relocated copy with a minimal PATH, hostile global Python settings, a fresh temporary state directory and a synthetic source that fails if imported. It verifies registry/planning/CLI access, restart retention and zero installed slots.

The full A0/A0R and first usable A1/A2 gates remain open. Next implementation packets can run in parallel against these contracts:

1. Ship a pinned licensed private preparation toolchain that works without development PATH, checkout, `uv` or managed-Python cache. Resolve and verify every dependency's retained notices, including required supplements.
2. Implement the owned builder and independent protocol/runtime verifier, including cancellation, full interface fidelity, relocation and restart checks. Test Python and existing MCP separately.
3. Add immutable binary-artifact intake and the exact CPU ONNX recipe, including weight/layout/class checks and measured maximum input resource limits.
4. Build native generated forms, recipe management and real preparation progress, then complete the independently authored recipe through actual preparation, attachment and restart with zero host edits.

Executable extension workers, general CLI, generic local LLM and integrated agent execution keep their separate ownership, compatibility and authority gates. Installation continues through the existing exact-plan workflow after attachment.
