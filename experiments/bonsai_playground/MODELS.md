# Playground model provenance

## Bonsai 1.7B

- Publisher: Prism ML.
- Repository: [prism-ml/Ternary-Bonsai-1.7B-gguf](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf).
- Immutable revision: `983b5dec2ff16aab79990711ba0f828a499a7e6a`.
- File: `Ternary-Bonsai-1.7B-Q2_0_g64.gguf`.
- Download: https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/resolve/983b5dec2ff16aab79990711ba0f828a499a7e6a/Ternary-Bonsai-1.7B-Q2_0_g64.gguf
- Size: `490163968` bytes.
- SHA-256: `6d0ecb3d9055969b5cde332b6fdb60e67ed3599e9f73e56b977731e1467e5c91`.
- Format: mainline-compatible ternary `Q2_0`, group size 64.
- License: Apache-2.0; see the pinned publisher [LICENSE](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/blob/983b5dec2ff16aab79990711ba0f828a499a7e6a/LICENSE) and [NOTICE](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/blob/983b5dec2ff16aab79990711ba0f828a499a7e6a/NOTICE.txt).

## MiniCPM5 2B Claude Fable5 1 Thinking Agentic

- Upstream base: [openbmb/MiniCPM5-2B](https://huggingface.co/openbmb/MiniCPM5-2B), immutable revision `f97400052a43d642bbc6e9975e2397e3ae6a6b52`.
- Fine-tune: [GnLOLot/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic](https://huggingface.co/GnLOLot/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic), immutable revision `934bcdfc20af23e97971704ab21ae3a8bdf7373c`.
- Quantization repository: [mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF](https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF), immutable revision `02dba166d01676e36710a7de183d6a5e0689b5f5`.
- File: `MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-IQ2_XXS.gguf`.
- Download: https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF/resolve/02dba166d01676e36710a7de183d6a5e0689b5f5/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-IQ2_XXS.gguf
- Size: `799476064` bytes.
- SHA-256: `de17e225b332223a86fb8f26238779ba2607f5e95ee995f5abae31e0c50fef0e`.
- GGUF metadata: architecture `llama`; 42 blocks; embedding length 2048; context length 131072; BOS ID 0; EOS ID 1; `add_bos_token=false`; `add_eos_token=false`; quantization version 2.
- Tensor types: `IQ2_XXS` (247 tensors), `Q4_K` (42), `F32` (85), `Q2_K` (6), and `Q5_K` (1). These names use the installed ggml enum (`GGML_TYPE_IQ2_XXS = 16`, `GGML_TYPE_IQ2_XS = 17`); the GGUF contains mixed tensor types.
- Embedded `tokenizer.chat_template`: compared with the exact upstream `chat_template.jinja` at the pinned base revision. It is not byte-identical: the embedded template adds tool-call conversion logic for `<tool_sep>` and changes the final tool-call condition to `message.tool_calls and not has_tool_sep`. The shared base rendering, BOS handling, role formatting, reasoning prefill, and generation prompt structure are otherwise retained.
- License: Apache-2.0. The exact Apache license text used by the MiniCPM project is [licenses/MiniCPM5-2B-LICENSE](licenses/MiniCPM5-2B-LICENSE), retrieved from the primary [OpenBMB/MiniCPM license at immutable commit `316cfb1cea39f39cfa16b4f5703b77495c2340be`](https://github.com/OpenBMB/MiniCPM/blob/316cfb1cea39f39cfa16b4f5703b77495c2340be/LICENSE).

### Q4_K_M variant

- Repository and immutable revision: [mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF](https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF/tree/02dba166d01676e36710a7de183d6a5e0689b5f5), `02dba166d01676e36710a7de183d6a5e0689b5f5`.
- File: `MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-Q4_K_M.gguf`.
- Download: https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF/resolve/02dba166d01676e36710a7de183d6a5e0689b5f5/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-Q4_K_M.gguf
- Size: `1561319776` bytes.
- SHA-256: `7a255ce8710a9d17eba9afe141ef2b7c1c3a2e9b9da9b51ce693b342df54710b`.
- Metadata matches the IQ2 artifact: Llama architecture, 42 blocks, 2048 embedding, 6144 feed-forward, 16/2 attention heads, 131072 context, and the same embedded chat template. Tensor types are `Q4_K` (253), `Q6_K` (43), and `F32` (85).

Controlled runtime comparison: the MiniCPM IQ2 artifact failed the same greeting/follow-up probe on both CPU and Metal with the same runtime and prompt format, while the MiniCPM Q4_K_M artifact passed. The IQ2 artifact remains retained for CLI negative-profile testing and is excluded from the GUI model selector.

## Qwen3.5 2B Q4_K_M

- Quantization repository: [unsloth/Qwen3.5-2B-GGUF](https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/tree/f6d5376be1edb4d416d56da11e5397a961aca8ae), immutable revision `f6d5376be1edb4d416d56da11e5397a961aca8ae`.
- Base model: [Qwen/Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc), immutable revision `15852e8c16360a2fea060d615a32b45270f8a8fc`.
- File: `Qwen3.5-2B-Q4_K_M.gguf`.
- Download: https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf
- Size: `1280835840` bytes.
- SHA-256: `aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223`.
- Local model path: `models/qwen3.5-2b/Qwen3.5-2B-Q4_K_M.gguf`.
- Runtime probes passed the greeting and `42` follow-up using the same local runtime. The GGUF reports Qwen3.5 architecture; its native chat template emits no BOS token and opens active reasoning with `<think>`.
- License: Apache-2.0; see [licenses/Qwen3.5-2B-LICENSE](licenses/Qwen3.5-2B-LICENSE), the primary official license copy.
