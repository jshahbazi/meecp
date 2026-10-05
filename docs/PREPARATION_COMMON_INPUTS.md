# Common preparation input and result contracts

Status: static contract seed. This module enables no execution capability.
The builder, job store, API and installed-host acceptance gates require explicit
integration. Existing descriptor-v1, receipt-v1, folder-source and JevK5 model
intake identities are unchanged.

## Descriptor adapter

`preparation_inputs.adapt_descriptor(value, *, draft, recipe, registry)` accepts
descriptor bytes, a JSON object, or an existing descriptor-v1/v2 model. It invokes
the existing version-specific parser and supplied host-owned `ComponentRegistry`.
An existing model is revalidated and retained as the exact same object.

The returned `DescriptorView` exposes `descriptor`, `digest`, `package`, `tools`,
`access`, `resources`, `lifecycle`, `validation`, `artifacts` and `source_bindings`.
Sequence views are tuples. The original model's nested dictionaries remain its
own; mutation after validation fails `assert_unchanged()`, `digest` and binding.

`view.expected_package_contract(source_revision, *, profile)` returns the common
package expectation. The host selects the admitted profile; the adapter does not
infer a profile or execution support from a source language or recipe ID.
Existing v2 CPU/resource and package-profile restrictions still apply.

## Immutable bound inputs

`bind_preparation_inputs(view, *, draft, captured_collection,
derived_collection=None, derived_inputs=(), runtimes=(), capabilities=(),
expected_package_contract)` returns `BoundPreparationInputsV1`.

The bound object contains the unchanged original source revision, descriptor
version/digest, recipe/package identities, complete component/source-validation
bindings, captured and derived collection references, runtimes, capabilities,
original dependency locks and the exact expected-package-contract digest.
Collections use immutable tuples, including nested original-file records.

`CollectionReference` has `kind`, `schema_version: 1` and `digest`. The captured
kind is **`captured_artifact_collection`**, whose separate manifest contract uses
`captured-artifact-collection-v1`. This name deliberately differs from the
existing JevK5 `artifact_collection` source contract. The derived kind is
`derived_input_collection`; its storage implementation must independently
validate the referenced manifest. Neither reference snapshots a mutable path.

Collection references are accepted only after the caller verifies captured
manifest closure, byte identities, licensing/provenance and descriptor artifact
selection. Runtime references similarly identify a separately verified private
runtime closure. This module does not perform acquisition, storage or runtime
verification, and parsing a reference does not establish that verification.

Original source files remain exclusively in `SourceValidationReference.files`.
The adapter preserves the existing `SourceBinding` digest. Binding checks each
original file against the draft's original selected records. An original lock
must resolve to exactly one original file selected by that component; components
without a lock need no placeholder hash.

`DerivedInputReference` separately records output name/role/hash/bytes, captured
derived-collection identity, exact producer component, original source revision,
resolution request/policy digests and parent identities. Its roles include
dependency locks/projects, interfaces, adapters and runtime configuration.
Parents must resolve to bound source/file, descriptor, captured collection,
runtime or earlier derived-reference identities. Ordered derivations reject
forward references and cycles. A derivation cannot become an original file.

The storage manifest for derived bytes must not embed references containing its
own digest. Capture byte-entry identities first; bind collection references and
producer lineage outside that manifest. Changing derivations changes the bound
input digest while preserving original source identity.

`parse_bound_inputs(value)` rejects unknown fields/versions and rechecks internal
closure. It does not admit a job or establish trust in a supplied producer.
Callers must validate resolution receipts and authority before accepting a
derivation; a declared producer or policy digest is not evidence by itself.

## Builder results and receipt selection

`validate_builder_result(value, inputs)` requires a `BuilderResultV1` to identify
the exact bound inputs, ordered consumed identities, expected package contract,
candidate package identity and candidate digest. Candidate IDs are opaque local
handles; their paths are resolved by the host. Produced derivations identify an
admitted selected producer and already bound/earlier produced inputs.

The host must independently verify candidate bytes and produced collection
manifests, compare the actual package contract, seal the receipt and run probes.
A result contains no installation, attachment or verification authority.

`PreparationReceiptV2` contains the existing job binding, complete common input
binding and fixture digest. Source, descriptor, recipe and captured collection
must match the job. Inputs carry optional typed original/derived locks and
runtime references; the receipt has no mandatory synthetic dependency-lock hash
and no final package digest that would create a self-reference.

`parse_preparation_receipt(value)` dispatches separately to the original
`PreparationReceipt` model for version 1 or `PreparationReceiptV2` for version 2.
It does not modify the existing receipt parser or serialized bytes. Persist and
hash new tuple-bearing models with `model_dump(mode="json")` and the shared
bounded canonical serializer. New digest domains are
`preparation-bound-inputs-v1`, `preparation-expected-package-v1`,
`preparation-derived-input-reference-v1`, `preparation-builder-result-v1` and
`preparation-receipt-v2`.

The contract tests cover a legacy descriptor digest golden, unchanged v1 receipt
bytes, fake non-Python normalization without execution, original/derived identity
separation, closure failures, mutation, exact result consumption and strict
versions. A fake component establishes static routing only; actual ONNX, CLI,
general LLM and service-connection execution remain independently gated.
