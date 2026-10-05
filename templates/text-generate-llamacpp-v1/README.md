# Exact CPU text generation package

This development generator packages only the selected Qwen2.5 0.5B Q4_K_M fixture and llama.cpp build 11146. It implements package schema 1, host API 1, the existing `model-service` process topology and tools-only MCP 2025-11-25. It adds no host schema, recipe registration or public preparation capability.

Run with the locked development environment and a clean, hash-verified private host build:

```sh
uv run --frozen python templates/text-generate-llamacpp-v1/prepare.py --host /absolute/private-host --engine-build /absolute/JevK5.app --weights /absolute/qwen2.5-0.5b-instruct-q4_k_m.gguf --output /absolute/new-package
```

The destination must not exist. Preparation copies and verifies the exact weights, tokenizer and engine closure, copies the selected private Python/runtime dependencies, retains distribution/license records, compiles native exec launchers, and seals the complete package lock. It performs no model download, package resolution or dependency installation. The output records source-module, generator, launcher, model, tokenizer, selected engine and private-host identities. The actual package lock binds every shipped byte. This foreground development generator is invoked from the source checkout; managed registration must package its trusted assets and supply controlled immutable inputs through the host's preparation component contract.

The engine launcher replaces itself with the exact private engine under the host's directly held PID. Arguments fix CPU operation, zero GPU layers, no KV offload, one slot, context 512, batch/microbatch 128 and two threads. Engine environment excludes user plugin, dyld and llama argument overrides. The former JevK5 supervisor wrapper is verified as part of the selected closure and omitted from the output because the host owns the engine.

The adapter depends on engine readiness and receives the private endpoint/key/instance through its inherited descriptor. Every public/private adapter route uses the unchanged host/origin/authentication policy. `text_generate` accepts exactly `text` and `max_output_tokens`; `text_status` takes no arguments. SDK preprocessing preserves literal strings. One adapter instance owns the shared bounded generation queue.

An uncertain transport sets the adapter's fatal fence and makes the service exit. The adapter returns no assertion that the sibling engine has settled and never sends signals to its PID. The host observes adapter exit, closes admission, and reaps the entire dependency graph. Only verified host completion releases the resource reservation. Ordinary caller cancellation remains joined within the shared adapter until the request thread settles.

The manifest declares 1,024 MiB CPU memory with no GPU use. Host headroom is additional. This declaration participates in admission; it is not an operating-system memory ceiling. Native managed preparation, maximum workload memory measurement and final independent A4 acceptance remain in [the compatibility ledger](../../docs/PHASE3_LLM_COMPATIBILITY.md).
