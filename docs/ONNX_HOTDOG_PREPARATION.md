# Captured CPU ONNX hotdog preparation

This lane supplies the actual host-owned ONNX task implementation for acceptance
gate A2. Public automatic execution and slot installation remain gated. The
shared directory onboarding, family registry and native admission paths are
integrated and verified by the host owner separately.

## Accepted input

The recipe accepts exactly one captured ONNX weights entry and captured license
entries. The weights must have SHA-256
`121b1f682e2df1aff1c16dacb7ce4bedc26df016a500b18743349c16724ad85d`
and size 10,701,557 bytes. Every referenced model license must match the retained
full AGPL-3.0 text. Format labels, filenames, a matching class label or a supplied
hash do not establish eligibility for another export. Local selection retains
the `publisher_unverified` origin record.

The original captured collection and revision remain the source identity. A
folder's supplementary captured collection can be validated by the component's
helper while preserving the folder revision. The current family routes captured
collection sources; folder candidate routing and combined source bindings still
require shared host integration. Source validation binds the exact captured entry
hashes and sizes. It declares no original dependency lock: the runtime project
and frozen wheel closure belong to the pinned host component.

The builder acquires a durable job reference before materialization. Each copy
uses `ArtifactCaptureStore.open_artifact`, which holds a durable read lease and
returns a verified file descriptor. The worker streams into new private files,
checks exact size and SHA-256, and retains every captured entry and license in
the package. Selected original paths never enter package identity or runtime
configuration. The runner releases the job reference after all owned commands,
probe processes and publication threads join; restart recovery uses the exact
persisted job owner.

## Task and runtime

The recipe generates one `detect_hotdog` tool. It accepts canonical base64 bytes
and an optional finite confidence threshold from 0.05 through 0.95. Its image
limits are 1 MiB encoded bytes, 1,048,576 decoded pixels and 4096 pixels per
dimension. JPEG, PNG, WebP, BMP and the first GIF frame are supported. Images
remain in memory. PNG compressed metadata is capped at 64 KiB per chunk and
256 KiB in total, independently of the pixel limit. Pillow documents the
[metadata decompression limits](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#png).

The task uses letterboxed 640 × 640 RGB tensors, selects the winning COCO class,
filters class 52 (`hot dog`), applies stable nonmaximum suppression and returns
clipped boxes in original image coordinates. Structured results omit timing so
synthetic MCP fixtures have exact deterministic expectations. An empty detection
list means `not_hotdog`; the task does not provide a calibrated negative
probability. Recognition quality is a separate acceptance question.

The private runtime pins CPython 3.12.13, MCP 1.30.0, ONNX Runtime 1.30.0,
NumPy 2.5.3, Pillow 12.3.0 and jsonschema 4.26.0. uv.lock freezes transitive
dependencies and wheel hashes. Only compatible wheels are acquired through the
host-controlled transport. Installation runs offline, without an index or source
build, with required hashes and copied files. Full dependency notices and the
private Python license closure are retained. Exact upstream FlatBuffers and
HTTPX full texts supplement their wheels' missing texts; all additions enter
the sealed package lock. ONNX Runtime's LICENSE and complete ThirdPartyNotices.txt
are retained from the exact wheel and also indexed in its distribution metadata.

The adapter hashes the exact model bytes before parsing them and loads those
verified bytes through [ONNX Runtime's Python API](https://onnxruntime.ai/docs/api/python/api_summary.html).
It explicitly selects CPUExecutionProvider and rejects a different provider,
input/output count, name, shape, dtype, COCO label map, task or export metadata.
The accepted graph has float input `images [1,3,640,640]` and float output
`output0 [1,84,8400]`. External model data paths cannot be selected through the
byte-based session. Model parsing runs inside an owned private process.

Decode and inference are serialized. Additional concurrent tool calls receive
the fixed `detector_busy` error. The package declares one session, a 2 MiB
request allowance, a 60-second operation deadline and at least 512 MiB reserved
memory. These are admission contracts; the actual native process peak and
workload measurements must pass before promoting the capability.

## Host integration

`OnnxPreparationFamily(capture_store)` exposes the component, form provider,
static inspection, exact input binding, private builder and joined-work release
hook. `builtin_onnx_components()` adds one catalog record without changing old
Python records. `write_onnx_recipe(path)` writes the immutable declarative
`mcpme.onnx-hotdog` 1.0.0 bundle. The host generates questions and the complete
descriptor; the user selects an artifact and app choices.

The component uses descriptor version 2. Its selected input is the closed
`model_name`, `license_names`, `task: coco-hotdog`, `profile: cpu-stdio` object.
Recipe step configuration is a separate closed empty object. Recipe enablement
admits its declarative metadata and grants no execution authority.

`prepare_onnx_bound` accepts the descriptor, draft, verified private toolchain,
new private output path, controlled command callback and cancellation callback,
plus the capture ledger, job UUID and controlled wheel acquisition callback. It
does not implement process creation or network transport. One explicit owned
`onnx_model_validation` command validates the model, runs real blank-image
inference, checks controlled tensors and exercises the maximum accepted image
workloads before sealing. Its output contains bounded counts and timings only.

## Verification

The focused host tests cover exact captured identity/license rejection, immutable
component and recipe identities, resource/schema/fixture binding, actual captured
file descriptor materialization after source removal, and explicit job pin
release. The locked task environment tests postprocessing, bad tensors,
thresholds, base64 canonicalization, byte/pixel boundaries, compressed metadata,
animation selection and exact runtime layout/provider checks.

Run the development acceptance harness against a fresh committed built host:

```sh
uv run --locked python scripts/verify_phase3_onnx.py \
  --host /absolute/path/to/built-host \
  --model /absolute/path/to/exact/yolo11n.onnx
```

The harness verifies the host artifact hashes, performs real durable capture,
removes the selected source copies, resolves the host-generated form, starts the
controlled version-2 runner, probes the actual SDK and attaches the same revision.
It relocates the sealed package, deletes captured state and builder inputs, then
repeats real SDK probing. It checks an exact 1 MiB image request, maximum pixels,
concurrent busy rejection and boundary errors with a private environment. Default
cleanup removes its synthetic state; uncertain ownership retains the private
proof directory for reconciliation. The original file supplied through `--model`
is never modified or removed.

The harness reports metadata, hashes, counts, declared budgets and measured peaks.
It does not install a slot. A2 remains open until the shared directory/captured
input path and native/install acceptance pass; the complete Phase 3 goal includes
further app families and independent adversarial review.

### Measured captured-input checkpoint

The default-cleanup harness passed on 2026-10-03 against clean committed CLI host
`54fe9d8892044beb363815ed540a4b6bde65f320`, verifying all 1,624 host artifact hashes.
It acquired 35 locked wheels (40,779,828 bytes), completed six owned commands,
attached the package to the selected revision, then ran three real SDK fixtures
after removing selected source copies, captured state and builder inputs.

The builder validated the exact model, ran three real blank images, six controlled
tensor cases, seven maximum image workloads and three boundary rejections. Its
peak RSS was 254,083,072 bytes; the slowest maximum workload took 0.021038 seconds.
The relocated SDK probe peaked at 264,929,280 bytes. The separate exact 1 MiB /
1,048,576-pixel SDK call took 0.075586 seconds and peaked at 278,298,624 bytes,
within the declared 512 MiB reservation. Four concurrent calls produced three
fixed busy rejections; both SDK boundary cases were rejected. Total harness wall
time was 32.795048 seconds, with zero remaining owned work, native receipts or job
references. Public execution stayed disabled and no slot was installed.

These are measured synthetic workload bounds for this pinned export and machine.
They establish package behavior and resource admission evidence, not recognition
accuracy on real photographs. The frozen integration host must repeat the harness
after the family is registered in the product and complete the native gate.
