# Derived-input preparation checkpoint

An unlocked Python callable and a foreground tools-only MCP server now have a
complete internal path from explicit resolution through generated questions,
owned package construction, SDK verification and atomic attachment. This does
not activate public execution or complete the installed/native Phase 3 gates.

The original folder, source revision and descriptor-v1 digest domains remain
unchanged. Descriptor v2 selects the exact host component `resolved-python-v1`
and binds the original candidate ID, immutable resolution result digest and
original source license. Its tool schemas come from static callable inspection
or the successfully settled SDK discovery result. Every discovered tool needs
a synthetic input and expected structured result before preparation can start.
No discovery runs while reading inspection or forms.

New metadata enablement records schema-2 recipe evidence over the exact selected
and transitive component records, including their schemas and dependencies.
Unrelated catalog additions do not invalidate that evidence. Schema-1 evidence
is read unchanged: the host accepts its exact current catalog, or reconstructs
the previously shipped two-component catalog and verifies that exact identity
and selected closure. Unknown historical catalogs fail closed. This migration
does not rewrite evidence, lifecycle state, epochs or immutable recipe bundles.

`PreparationForms.context`, `form` and `resolve` accept optional
`resolution_digest` and `resolution_available` keywords. Forms expose a closed
host-default resolution policy, required operations, exact result digest and
separate resolution/execution availability. `PreparationStore` accepts the
shared `ResolutionStore` through `resolutions=`. Selection carries the exact
result to its new revision; reloading that revision restores answers and the
accepted result. Later metadata edits require an explicit new carry.

`ResolutionStore.prepare_result_binding` inspects and hashes off-loop before a
write transaction. Its in-process proof cannot be reconstructed from JSON.
`commit_result_binding` performs bounded source/revision/result-authority checks
inside the selection transaction. It changes neither the original request nor
result digest. Rollback removes the carry together with the selection. The
standalone `bind_result` helper prevalidates before opening its own transaction;
calling it from an existing transaction fails closed. The builder revalidates
the saved result, then checks every exact file hash again while copying bytes
into its private dependency project. Selection alone grants no execution.

The owned build uses the private frozen project and lock for wheel acquisition
and installation. Source stays in its original namespace. The same declared
MCP module or no-argument foreground entry point used for discovery is written
into the relocatable host-owned launcher. No generated lock is inserted into
the original source snapshot or reported as an original `SourceBinding` file.

Receipt v2 binds the current job/import revision, original source, exact
descriptor and recipe, original file validations, derived input collection and
its producer/request/policy references, private runtime, fixtures and expected
package contract. Folder inputs without captured binary artifacts have no
captured collection reference; their existing empty artifact-selection identity
stays in `JobBinding`. Captured-only sources require their actual collection.
Receipt-v1 parsing and serialization retain their original meaning.

Additional families enter through reviewed host objects passed as `families=`
to `PreparationStore`. Dispatch uses their exact component reference, never a
client-supplied module or recipe name. A family provides `component`,
`source_kind`, `validate_inputs`, `bound_inputs`, `prepare`, and
`release(job_id, descriptor)`. Optional `inspect_source` and `form_provider`
cover new input shapes. Release runs after all worker callbacks, file threads
and native ownership proofs settle, including explicit restart reconciliation.
The captured-source record adapter reads exact manifest entries, without
pretending they belong to a legacy folder inventory.

Actual private-host tests exercise an ordinary folder with neither project nor
lock and a declared `worker:main` MCP server with two dynamically listed tools.
Each resolves with a fresh private cache, answers generated questions, builds,
passes independent SDK fixture verification, attaches, relocates, removes its
original directory and attachment, then passes SDK verification again. Focused
tests additionally cover rollback, stale selection during a paused hash,
canceled draft admission, immutable request preservation, runtime mismatch,
receipt closure, default recipe preservation, failed recovery retries and the
durable-authorization/worker-registration race.

Verification on this checkpoint:

- `uv run pytest -q`: 1,959 passed, 12 opt-in skips, 160.50 seconds.
- `uv run pytest -q tests/test_recipe_component_evidence.py`: 5 passed,
  including the additional unchanged legacy-default seed regression.
- `MCPME_RESOLUTION_HOST=/Users/johnshahbazian/.codex/worktrees/phase2-onboarding/mcpme/dist/mcpme-phase3-native-artifacts-host-final uv run pytest -q tests/test_preparation_derived_build.py`:
  7 passed, 51.75 seconds. This uses that built host's sealed private toolchain
  and native reaper with the current checkout's owned implementation. It does
  not claim the new implementation has passed installed-host/native acceptance.
