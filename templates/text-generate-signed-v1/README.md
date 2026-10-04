# Frozen signed CPU text-generation composition

This trusted composition pairs `llamacpp-cpu-v1@1.0.8` with
`text-generate-v1@1.0.8`. It accepts exactly the Qwen2.5 0.5B Instruct Q4_K_M
artifact and Apache-2.0 license in `model-provenance.json`. The reviewed engine,
tokenizer, template and bounds are recorded in
[the compatibility evidence](../../docs/PHASE3_LLM_COMPATIBILITY.md).

The engine component supplies the exact private CPU runtime and single slot.
The task component supplies bounded nonstreaming text generation, the MCP
schemas, one shared serialized queue, and the authenticated adapter. Component
identities bind trusted builder/verifier code, adapter/policy source, frozen
Python dependencies, the complete reviewed host Python module closure, shared
transport, and these assets including the managed launcher's C source. Native
reaper and actual private dependency bytes are also bound by the durable job's
runtime identities. The declarative recipe contains those
two exact component references and no executable hooks.

Model bytes and license text enter through durable artifact capture. For a
folder or document supplemented with a capture, source-validation files remain
the original snapshot records. Capture metadata and the separate bound input
reference carry the model identity. A job pins the collection until the runner
joins all build/probe work and releases that job's reference.

The distribution builder must explicitly receive the frozen signed archive
through `--textgen-signed-asset`. The archive must match the committed manifest
and complete member inventory. It supplies the signed engine, direct launchers,
private Python and fixed wheel installation. App builds preserve those signed
bytes. Managed preparation copies that exact closure without dependency
acquisition or signing. The legacy supervisor remains an independently checked
original input and is absent from the runtime. See
[the signed runtime contract](../../docs/TEXTGEN_SIGNED_RUNTIME.md) for production,
provenance, signature validation, and immutable versioning requirements.

`TextGenerationFamily.verify` accepts the runner's journaled owner. It starts
the engine and adapter as separate native-owned foreground groups, checks real
MCP schemas and synthetic inference, exercises the actual 440-input/64-output
token boundary and bounded concurrent queue, and validates status/authentication.
Both processes settle before package rehashing and a report. Reported memory is
the conservative sum of their individual native `wait4` peaks, not a simultaneous
aggregate or an OS memory limit.

The managed acceptance harness supports `--source-mode captured` and
`--source-mode folder`. The latter captures a real original instructions
snapshot, then supplements it with the model collection. Both check source,
capture, descriptor and recipe bindings in the sealed preparation receipt.
`--install-lifecycle` installs the same attached import revision through the
normal plan/apply flow, then exercises inference, memory contention, transport
failure retention, native reap and host crash/restart. Those CLI lifecycle
checks do not establish menu/UI acceptance. Actual composed runs require the
fresh immutable integrated host and independently admitted family.

The core family registration, capability admission, distribution wiring and
managed native acceptance remain separate gates. Recipe visibility alone must
not enable execution. This family does not establish support for other GGUF
files, Metal, embeddings, multimodal input or JevK5 scoring.
