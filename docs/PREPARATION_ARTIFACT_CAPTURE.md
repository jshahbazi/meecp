# Local preparation artifact capture

`preparation_artifacts.py` provides code-free local capture and verification. It does not enable automatic preparation, add a source variant, acquire network data, extract archives, load a model, or execute selected files. The ONNX recipe and the complete A2 acceptance path remain separate work.

## Contracts

`AcquisitionRequestV1` has `schema_version: 1`, `kind: local-files`, and at most 64 `LocalFileAcquisitionV1` entries. Each entry declares a unique logical name, role, absolute local selection, bounded format label, optional expected SHA-256/size, and sorted license names. Roles are `model-weights`, `runtime-file`, `license`, and `validation`.

Every non-license entry requires at least one reference to a selected license entry. License entries use the explicit exemption: their own reference list is empty and their format is UTF-8 text. Capture converts references into exact logical name/SHA-256 pairs. An unrelated source notice or a path outside the captured collection cannot satisfy the reference.

`CapturedArtifactCollectionV1` is a closed portable manifest with entries sorted by unique logical name. Each entry records `files/<name>`, exact byte count and SHA-256, declared format, captured license references, and a local-selection origin. Its canonical digest uses the separate domain `captured-artifact-collection-v1`. The legacy descriptor domain `artifact-collection-v1`, folder-source identities, and existing model intake contracts are unchanged.

Absolute selection paths appear only in the acquisition request. They are absent from the portable collection. Origin always records `publisher_trust: publisher_unverified`; a supplied expected SHA-256 is retained as byte-verification metadata. Neither a supplied nor a locally computed hash proves publisher identity or license applicability. A format label does not prove model compatibility; the selected recipe must perform that validation.

## Owned capture and use

`ArtifactStore(root)` uses a private directory outside the legacy import store. `capture(request, *, disk_ceiling_bytes, cancel_event=None, deadline=None)` is synchronous. Its async coordinator must reserve aggregate disk, run it in a tracked worker, signal cancellation, and join that worker before releasing its reservation or removing job inputs. `deadline` is an absolute monotonic timestamp. This library does not start a thread or process.

Bounds are 16 GiB per file, 32 GiB per collection, and 64 files. The per-capture disk ceiling includes captured bytes and the manifest; capture keeps a 512 KiB manifest allowance while copying. Free disk is checked before and during writes. Source ancestry is bounded to 128 components. Reads, hashes, credential-marker checks, and copies use bounded chunks without retaining complete large artifacts in memory.

Capture traverses and pins directory descriptors without following links. It rejects nonregular files, hardlinks, symlinks, changed ancestors, changed file identity/content, declared hash/size mismatches, recognized credential filenames/markers, and recognized archive names/headers. These checks do not establish that arbitrary binary bytes contain no secret; callers must select artifacts intended for distribution. No archive is parsed or expanded.

An owned staging tree is checked before publication. Its complete directory moves atomically with native no-replace semantics (`renameatx_np` on macOS or `renameat2` on Linux); unavailable platforms/filesystems fail closed. Publication is serialized between captures; an existing digest can be reused only after verifying its entire closed manifest and files. Failure or cancellation cleans only unpublished owned staging through held descriptors. Foreign or corrupt existing objects are never overwritten, including empty directories.

The atomic move commits cache bytes without granting execution authority. Late cancellation rejects this capture's result and retains the complete published object, preserving another reader's immutable reference. No error path deletes a globally visible collection piecemeal. The owning coordinator must account for committed cache storage and apply its separate reference/retention policy.

A process crash before the atomic move can leave an unpublished private staging tree; after the move it leaves a complete verifiable object. Neither window creates an empty/partial digest claim that poisons retry. Orphan staging association, reservation retention and collection remain the owning coordinator's responsibility. This store never adopts an unrecorded stage discovered by scanning.

If an owned staging inode's location cannot be confirmed, capture preserves it and reports `artifact_capture_reconciliation_required`. The coordinator must retain the relevant storage reservation until it can establish ownership and settle cleanup; it must not delete a replacement tree or treat a directory scan as adoption authority.

Collection directories have mode `0700`; manifests and artifact files have mode `0400`. These permissions remain under the current user's control. Reverification detects mutation; the files are not protected from a malicious process running as that user.

`verify(digest, *, cancel_event=None, deadline=None)` validates canonical metadata, digest, exact tree membership, regular-file ownership/modes, byte counts, SHA-256, and descriptor identities before use. `open_artifact(digest, name, *, cancel_event=None, deadline=None)` yields a verified read-only file descriptor in a context manager. Consumers stream captured bytes while that context remains open. They must discard materialized output if its final content/identity check fails. The descriptor cannot outlive the context, and the original local path is never a worker input.

Host source-collection integration, revision/authority binding, job materialization and retention, real ONNX inference, and public capability admission remain outside this store.
