# Automatic CLI preparation: implementation gap and minimum next contract

This proposal is grounded in the combined host at `308f6d5`. It does not register a family or enable CLI execution. The installed schema-2 command profile proves one runtime seam. It does not yet deliver directory selection, questions, controlled assembly, independent probe and attachment.

## Existing reusable seams and actual blockers

| Current component | Reuse and required change |
| --- | --- |
| `PreparationStore.inspect_source`, `preparation_inspection.inspect_source`, and `preparation_components.read_snapshot` | Family inspection already merges revision-bound candidates without execution. Add a trusted CLI inspector; the combined tree currently has none. The no-fork CLI component is in a separate spike branch. |
| `PreparationDescriptorV2`, `ComponentRegistry`, `SourceBinding` | Reuse the common envelope and exact component reference. A new component owns a closed CLI input schema and binds every selected executable/runtime file and the original source revision. Validate the bounded structural tool-schema subset before the common generic JSON-schema validator can process unsupported keywords. |
| `PreparationForms` family `form_provider` | Reuse typed questions, revision fencing, selection and stale-answer rejection. Populate forms from static evidence and ask for unresolved interface details; never execute `--help` during inspection. An unfamiliar executable without sufficient interface evidence remains unresolved. |
| `PreparationRunner.enabled_families`, `family.prepare`, `family.verify` | The family admits by exact component key; the generic runner does not imply admission. Preparation uses private controlled tools. The family needs a specialist verifier because the generic verifier accepts only `mcp-stdio`. |
| `imports._choices`, `PreparationStore._bound_descriptor`, `_candidate_contract`, `DescriptorView.expected_package_contract` | These currently accept only `mcp-stdio`, `mcp-http` and `model-service`. Add explicit host-command handling; preserve existing profile semantics and digest domains. |
| `BoundPreparationInputsV1`, `BuilderResultV1`, `PreparationReceiptV2`, attachment validation | Existing common identities can carry component/runtime references, but their present expected package contract omits the complete command block and package schema. Introduce an explicit command-specific contract digest/version and validate it during reserve, build result, probe, seal and attachment. Do not silently reinterpret old receipts. |

The inspector implementation imports `read_snapshot` from `preparation_components.py`; there is no separate CLI dispatcher to enable. New files and narrowly reviewed registration/profile seams are required.

## Minimal family and input contract

Introduce a separately versioned trusted `HostCommandJsonFamily` and `host-command-json-v1` component, with `target_profile = "host-command"` and folder support through `supports_source`. A declarative recipe references the exact component digest; it cannot supply executable host implementation. Its identity includes the inspector/form/builder/verifier, bounded schema rules, reviewed native helper source, helper binary/runtime identity, platform policy and receipt format.

The component input should bind:

- Candidate ID and original folder revision, plus an exact sorted file closure with hashes, sizes, executable modes, licenses and provenance. Start with one already built arm64 executable and its explicitly supported packaged runtime closure. Arbitrary compiler scripts, shell expansion, PATH lookup and package-manager downloads remain unsupported until separate trusted build components exist.
- Fixed package-relative executable, sealed working directory, fixed argv, empty environment, JSON stdin/stdout, discarded stderr and the current zero-success/nonzero-error mapping. The reviewed installed profile has fixed argv only. Named variable argv positions require a new typed mapping with exact string conversion and length bounds; that extension is not implemented here.
- Exact full MCP tool metadata and bounded structural input/output schemas, request/output byte ceilings, deadline, queue bound and synthetic expected fixtures. No regex, schema references or runtime code in forms. Unknown effects, network/write requirements, credentials or unsupported runtime dependencies remain explicit blockers under the current no-network/no-write target policy.
- Exact helper/component/policy/receipt identity and the complete schema-2 command contract. The helper must come from the verified host inventory and trusted constructor authority, never from a selected directory or recipe bundle.

The product asks about missing callable behavior, argument fields, output shape and synthetic examples through typed questions. It must not present a manual manifest-editing step as completion. Static supported interface metadata may prefill those answers. Automatic inference for an arbitrary binary is not supplied by this proposal; unrecognized behavior stays unresolved or requires a separately proven bounded analysis backend.

## Build, probe and receipt ownership

The initial builder only copies the validated snapshot closure into a fresh private output, supplies licenses/provenance, emits `HostCommandManifest` schema 2, and seals a complete lock. It may reuse `_copy`, source/file bindings and job disk/cancellation checks, but must not call a user build script or nested `Popen`. Adding a compiler or interpreter later requires a separately pinned build/runtime component and independently verified derived artifacts.

The probe must execute through a **backend-owned** `CommandExecutorV2`, with durable job/attempt reservations and exact pre-install authority. A slot UUID does not exist yet: introduce an explicit job/attempt authority variant bound to import revision, descriptor/component/recipe digests, sealed candidate digest, synthetic fixture digest, helper/platform/receipt identity, scope/expiry and cancellation. Do not fabricate a slot or expose management owner tokens. The current family `verify(..., owner=JournaledPreparationOwner)` interface alone cannot safely grant that capability: injecting the executor inside a worker owned by that legacy process group recreates the ancestor-group-kill trap. Add a trusted backend verifier service/callback that owns the helper independently and returns only bounded typed proof.

On cancel, restart or helper loss, pending command receipts must retain the job's reservation and private candidate workspace just as installed commands retain slot ownership. Job recovery needs a command-format-3 journal branch; the current legacy `ProcessOwner` recovery must not certify its descendants. Durable proof publication, unknown ownership and stale output fences retain their existing strict meanings.

Probe the exact candidate closure and full schemas with declared synthetic fixtures, bounded output, exit/deadline/cancel behavior and cleanup. Seal a command-specific receipt only after settlement. Bind command contract and helper/policy identity into both expected package and verifier identities. Recheck all of them at attachment and again at the installed Supervisor admission seam. Native formats remain separate: package schema 2, preparation descriptor 2, existing preparation receipt 2 and native receipt 3 are not interchangeable version numbers. A new receipt/envelope version is required if existing fields cannot represent the explicit command contract without changing old digest semantics.

## Acceptance before enabling

Use an unfamiliar directory fixture through real import/inspection/questions, recipe selection, managed build/probe and attachment. Delete the original directory and build workspace, relocate the final host, call the tool over both private SDK transports, then restart and repeat. Mutate source, schema, command argv, closure, recipe/helper identity and generation between each boundary. Include cancellation and backend/ancestor-group/helper death during build/probe/publication, and verify no attachment or reservation release without exact settled proof. Confirm the native question/assemble flow consumes the same receipts and preserves auth/origin checks.

The aggregate process-count/memory enforcement and final signed relocated native-app proof remain independent A3 gates. This minimum family is an implementation plan, not evidence that those gates or arbitrary CLI compatibility have been achieved. Default public CLI admission stays closed.
