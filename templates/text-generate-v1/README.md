# Managed CPU text-generation composition

This trusted composition pairs `llamacpp-cpu-v1@1.0.0` with
`text-generate-v1@1.0.0`. It accepts exactly the Qwen2.5 0.5B Instruct Q4_K_M
artifact and Apache-2.0 license in `model-provenance.json`. The reviewed engine,
tokenizer, template and bounds are recorded in
[the compatibility evidence](../../docs/PHASE3_LLM_COMPATIBILITY.md).

The engine component supplies the exact private CPU runtime and single slot.
The task component supplies bounded nonstreaming text generation, the MCP
schemas, one shared serialized queue, and the authenticated adapter. Component
identities bind trusted builder/verifier code, adapter/policy source, frozen
Python dependencies and these assets. The declarative recipe contains those
two exact component references and no executable hooks.

Model bytes and license text enter through durable artifact capture. For a
folder or document supplemented with a capture, source-validation files remain
the original snapshot records. Capture metadata and the separate bound input
reference carry the model identity. A job pins the collection until the runner
joins all build/probe work and releases that job's reference.

The distribution builder must explicitly call
`bundle_textgen_assets.bundle(host_root, engine_resources_root)` after creating
the private preparation toolchain, before final host hashes. The engine source
must match every committed closure hash. The helper verifies the legacy
supervisor but omits it, compiles the direct process launcher at distribution
build time, and refreshes the toolchain manifest. Managed preparation copies
that immutable closure and installs frozen wheels through the durable runner's
controlled acquisition callback. It never discovers a developer runtime.

`TextGenerationFamily.verify` accepts the runner's journaled owner. It starts
the engine and adapter as separate native-owned foreground groups, checks real
MCP schemas and synthetic inference, exercises the actual 440-input/64-output
token boundary and bounded concurrent queue, and validates status/authentication.
Both processes settle before package rehashing and a report. Reported memory is
the conservative sum of their individual native `wait4` peaks, not a simultaneous
aggregate or an OS memory limit.

The core family registration, capability admission, distribution wiring and
managed native acceptance remain separate gates. Recipe visibility alone must
not enable execution. This family does not establish support for other GGUF
files, Metal, embeddings, multimodal input or JevK5 scoring.
